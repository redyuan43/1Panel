# 渐进并发：工作集准入与实测峰值

本改动仅用于 `run_parallel_comparison.py --progressive` 实验运行器。不改变 Fleet 生产调度、不部署、不启动 GPU 推理。原有七组视频和新增对照记录不变。

## 准入公式

所有计算使用整数字节，GiB = 1024³。每次尝试加路计算：

```text
reclaimable_file = max(0, min(file, inactive_file) - file_dirty - file_writeback - unevictable)
effective_reclaimable = floor(reclaimable_file * applied_reclaim_factor)
effective_working_set = memory.current - effective_reclaimable
history_margin = max(2 GiB, ceil(measured_peak_delta * 10%), configured_margin)
candidate_budget = max(manifest_static_floor, historical_budget_floor, measured_peak_delta + history_margin)
remaining_peak = sum(max(0, expected_peak_budget - observed_worker_peak_delta_so_far))
current_worker_delta = worker_current - worker_baseline
released_peak_regrowth_reserve = sum(max(0, observed_worker_peak_delta_so_far - current_worker_delta))
reserved_running = remaining_peak + released_peak_regrowth_reserve
projected = effective_working_set + reserved_running + candidate_budget + global_safety_margin
```

- `reclaim_factor` 默认 **0.5**，范围 `[0, 1]`，配置值和实际使用值均记录；不是保证能回收的比例。
- 全额扣除 unevictable 是保守处理，可能重复扣除，但不会把不可驱逐页当成可回收页。active_file、anon、slab 不打折。
- 无可靠历史时使用原 manifest 的静态预算，不推测更小值。本次真实 C0/C1 manifest 均为 **18 GiB**。
- 历史记录取相同 case/profile 的最大峰值增量；保留历史预算上界，并额外保留 manifest 下限。历史峰值上升时增加预算，不自动下调。
- `observed_worker_peak_delta_so_far` 是本任务提交前独立 worker 基线以上的**单调实测峰值**，不是当前值；任务采样结束到确认卸载之间继续保留未使用峰值预算。
- 额外安全补充：达到过峰值又回落的内存，不能永久抵扣后续 decoder 的预算。因此保留用户要求的 `remaining_peak`，另加 `released_peak_regrowth_reserve`。否则 current 已下降而旧峰值仍在抵扣，会低估再次达到峰值的空间。此项只增加预留，不放宽准入。
- `current_worker_delta` 保留符号：如果占用跌到提交前 baseline 以下，也保留回升到 baseline + expected peak 的空间，不能把负增量截成零而少预留。
- 全局余量默认且最低 **2 GiB**；历史余量最低 **2 GiB 或 10%**，两者取大。两项余量用途不同，均保留。

必须同时满足 `projected <= min(72 GiB, existing_limit)`、`host_available - reserved_running - candidate_budget - global_margin >= 16 GiB`，以及原有磁盘、显存、租约、进程身份、采样器实际进度、连续稳定观察等门禁。host available 不再加一遍 cache credit，避免重复计算可回收页。

原始 `memory.current > 72 GiB` 运行期硬保护仍保留；工作集公式只改善**加路预测**，不替代硬保护。没有提高 cgroup limit、减少已验证预算、drop_caches、swapoff、memory.reclaim 或生产服务重启。

## 采样与 fail-closed

每次采样持久化原始和解析后的 `memory.stat`，包括 anon、file、inactive_file、active_file、file_dirty、file_writeback、unevictable，以及内核提供的 workingset/pgscan/pgsteal 等其他字段；同时记录 `memory.current`、host available、host/cgroup swap、pswpin/out、host/cgroup PSI、worker cgroup 和 GPU 显存。

memory.stat 前后紧邻重读 memory.current，准入使用这两次与原始资源采样的最大值，记录采样起止时间；窗口超过 1 秒则本次估算无效、等待加路。内核文件不是原子快照，不把旧 current 与较晚 cache 的差值当作精确瞬时值。

- 缺失或非法 memory.stat：折扣归零，`working_set_telemetry_unavailable`，仅等待加路，不因为这个可选估算数据缺失去停止健康任务。
- host/cgroup swap 增长或新 pswpout：折扣归零，并重置稳定观察窗口。持续增长时不能借缓存放宽。
- PSI 非零或累计 stall 增长：折扣归零，不加路；仍须重新满足至少 60 秒连续稳定窗口。
- 原有 OOM/Xid、身份变化、温度、硬内存边界等停止条件不削弱。
- `admission_waiting` 原因和完整预测写入 `admission.jsonl` / `report.json`；完成采集、确认卸载后自动重新计算下一条准入。
- 只能串行完成时保持 `peak_parallel=1, parallel_validated=false`。CPU 模拟并发不是实际三卡验证。

## 历史与对账

每条任务冻结 `profile_binding`、`profile_key`、`peak_budget`、`admission_baseline` 和提交时的 `admission`。profile 绑定 case、原始完整 workflow SHA、validator SHA、GPU UUID、worker unit/endpoint、输出规格和显式运行环境版本标识 `runtime_profile_id`。默认 `unverified` 不允许导入历史；变更模型、精度、工作流或硬件不可沿用不匹配的历史。

`measured_peak_history.py` 仅离线读取本地已复制的报告、指标、工作流、视频工件，逐个检查 SHA、成功状态、卸载/租约释放、采样覆盖和准确身份。单 worker 可用 aggregate 增量；并发报告必须使用独立 worker subtree，不能把整个批次增量归给某一任务。来源不完整时回退静态预算，并保留已保存历史预算下限。不得把失败/中断任务记录当成已验证成功峰值。

历史采集还继承成功任务的 `candidate_budget_bytes` 下限。运行内 `peak_delta_bytes` 与预算用 `budget_peak_delta_bytes` 分开：后者额外纳入首个完成边界后的同 worker 样本、同 worker 对账高峰，仅用于增加预算，不能丢掉已观察到的解码边界峰值；不借用 aggregate 的并发峰值。

`resource_reconciliation` 记录提交时 projected、实际采样 aggregate 峰值/增量、每个 worker 峰值增量、最低 host available、PSI 峰值、swap 变化、OOM/Xid 及预测差额。硬保护触发样本先写证据，再执行已有停止流程。

这里“实际 peak”指采样高水位，不声称捕获两个采样点之间的瞬时峰值。`pgsteal × page_size` 只表示整个 cgroup 的累计回收量，可能重复回收或包含匿名页，不能称为该任务净释放的 file cache。单独记录 file 下降 proxy；缺失 pgsteal 时实际回收量为 `null`，不伪造。并发期间 aggregate 对账不能单独归因于某个任务。不自动校准或下调 factor/margin，后续人工审查证据。

## 配置与离线验证

```text
--reclaim-factor 0.5
--global-safety-margin-gib 2
--peak-safety-margin-gib 2
--peak-safety-margin-ratio 0.1
--runtime-profile-id <已审计的环境版本>
--peak-history <本地历史清单> --peak-history-sha256 <SHA256>
```

配置及历史解析后的预算写入冻结 contract；改变后不能复用旧 prepared run。没有 `--execute` 时仍仅进行本地 CPU 准备。

```bash
python3 -m pytest -q deploy/h3-fleet/tests
```

本次 51.6 GiB / 18 GiB dry-run 见 `experiments/optimization-20260909/a_realism_ops/working_set_dry_run.md`。缺失的历史 memory.stat 不能用当前样本回填，也不能把假设清洁缓存量的算例当成真实准入通过。

## 2026-09-10 11:10 离线验收（部署前）

| 范围 | 结果 |
| --- | --- |
| 本次 admission / history / reconciliation / progressive 专项 | 214 passed，已包含在 Fleet 全量中 |
| H3 Fleet `tests` 全量 | 827 passed |
| A LightX2V / B VDN / C Realism / D FastH3 / A Realism 实验配方 | 32 / 49 / 36 / 47 / 26 passed，合计 190 |
| H3 Studio Python 全量 | 69 passed；4 个既有 FastAPI on_event 弃用警告 |
| Studio 导航 Node 测试 | 7 passed |
| dry-run 重现 | 24 个内置断言通过，JSON 与保存工件逐字节一致 |

去重后的 pytest/Node 合计 **1093 passed**。实验配方测试因使用同名 `prepare` / `test_prepare` 模块，按各自目录独立运行；不把跨目录导入冲突当作业务回归，也未为此修改旧测试。覆盖本次用户要求的全部十类场景，以及峰后回落、低于 baseline、OOM 同时丢失 worker 身份、慢采样窗口等边界。

代码遵循模块分工：纯工作集计算、离线历史校验、纯数据对账与已有 coordinator 集成；没有引入新的第三方依赖。原有无关 Router 工作区修改未动，没有 commit/push。

本轮代码和测试均在 AI worktree，本轮未部署新算法到 Ivan，未启动新的 GPU 推理。旧实验批次已受控停止，C0 未提交，原生产 admission 经核验恢复；没有重启生产服务或回收缓存。**真实二路/三路渐进并发仍未验证，等待用户确认后才部署实验版本。**

本地完整日志与相对旧冻结运行器的代码 diff：
`/home/ai/.local/state/h3-studio-ivan-production/deployment/optimization-20260909/admission-working-set/`

- `admission-unit-tests.txt`
- `fleet-regression.txt`
- `studio-regression.txt`
- `navigation-regression.txt`
- `admission-key.diff`

冻结旧运行器 SHA256：`ba0fcb07379be96dbb4140615a00d021fd0bfc7af041ef92cf95413e419e17aa`。
本轮新运行器 SHA256：`70ba4eaaddf7c0e57d68aab6052b7d80eb4e79aa941ec7b4e096863282093e7f`。

上述是授权部署前的验收记录。后续用户授权的真实运行、采样时序修复及结果另见 `working-set-validation-20260910.md`，不要把本节离线测试结果当作真实并发证明。
