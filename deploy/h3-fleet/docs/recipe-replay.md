# 配方调度 CPU 离线回放

## 边界

`scripts/replay_recipe_scheduling.py` 是独立分析侧车，不导入 Fleet main，不采集 telemetry、不读取 GPU、不调用网络、API、systemd 或生成任务。不修改现有调度器、预算或运行目录。

对同一 trace 分别运行两种策略：

- `strict_fifo`：旧 FIFO 的限定参照模型。按 `(created_at,prompt_id)` 排队，只考虑队首；按输入 backend 顺序找第一个空闲、具备资格且预算允许的后端。队首不能运行时，不越过它。它不是声称逐行重现某历史部署版本。
- `throughput`：直接调用当前 `app.throughput.plan_assignments`，保留原候选窗口、稀缺资格、等待保护及评分逻辑。不重新实现其排序，也不强制它必须优于 FIFO。

两者共用到达时间、backend/物理 GPU 资格、每后端时长矩阵、完整峰值预算和稳态条件。规划器使用同一时长矩阵作为 ETA；这是离线已知时长假设，不是线上预测精度证明。报告记录归一化 trace SHA256 和规划器源码 SHA256；文件输入另记原始文件 SHA256。

## 准入模型

```text
running_reserved = provided_running_remaining + sum(active_job_full_budget)
projected = provided_effective_workingset + running_reserved + candidate_budget + global_margin
allow = stable_for >= 60s AND projected <= configured_limit <= 72GiB
        AND backend explicitly enabled/qualified AND physical GPU idle
```

- 单任务预算最少 18GiB，global margin 最少 2GiB，等待保护最少 900s；可更保守，不可下调。72GiB 是上限，不是可抬高的默认建议。
- 初始 `t=0` 开始模拟安静观察；每次提交或完成导致 active owners 改变时重新计时，达到连续 60s 才允许下一次提交。每个窗口最多提交一条，不做一次三路同时启动。
- `effective_workingset_bytes` 是**不含回放任务**的固定基线。`running_remaining_bytes` 是可选外部剩余预留，全程不释放。回放任务从开始到完成始终保留全预算，不按耗时比例削减，不凭部分进度释放 remaining。
- 同一 UUID 下不同 backend/端口只算一个物理 GPU。未知 backend、缺资格、缺时长、未知 recipe 均拒绝整份输入，不自动填 ETA 或换后端。显式 disabled 后端保持不可用。
- 本模型不复刻实时 PSI/Swap、主机 minRAM、raw cgroup、磁盘、PID/lease、采样或故障门禁，也不声称通过它们；模拟期间资源基线固定、安静窗口成立、输入时长在并发下不变。**回放 allow 不是任何真实 GPU 准入授权**。
- 时长由输入给出，可包含冷加载、采样、decode 和卸载；不得将不同环境测量混用且不标明。这里不推断缓存/预热或并发争用造成的时长变化。

## 运行入口

在 `deploy/h3-fleet` 目录：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/replay_recipe_scheduling.py --synthetic
PYTHONDONTWRITEBYTECODE=1 python3 scripts/replay_recipe_scheduling.py --trace /absolute/path/trace.json
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_replay_recipe_scheduling.py tests/test_throughput.py
```

仅输出 JSON 到 stdout，不写回输入文件。必须显式指定 `--synthetic` 或 `--trace`，不提供默认假实测。输入错误退出码 2；不可调度的有效输入输出 `status=blocked`，makespan/吞吐/比较收益为 null，保留已完成和 pending 明细，防止把未完成工作算作收益。

## 输入格式

以下例子明确是 **synthetic**，不是设备实测：

```json
{
  "version": 1,
  "trace_kind": "synthetic",
  "provenance": "Invented one-job format example; not a hardware measurement",
  "stable_seconds": 60,
  "protected_seconds": 900,
  "resources": {
    "effective_workingset_bytes": 17179869184,
    "running_remaining_bytes": 0,
    "global_margin_bytes": 2147483648,
    "cgroup_limit_bytes": 77309411328
  },
  "backends": [
    {"id": "fast", "gpu_uuid": "synthetic-gpu", "enabled": true, "qualified_recipes": ["A4", "B8"]}
  ],
  "jobs": [
    {
      "prompt_id": "001-b8",
      "recipe_id": "B8",
      "created_at": 0,
      "candidate_budget_bytes": 19327352832,
      "eligible_backend_ids": ["fast"],
      "duration_seconds_by_backend": {"fast": 600}
    }
  ]
}
```

`created_at` 是相对回放 `t=0` 的非负秒数，不是 UNIX 时间戳。所有 bytes 都是整数。duration 必须对每个 eligible backend 显式提供正有限秒数；不能凭卡型号补值。资格是输入证据的声明，脚本不会现场验证。

使用实测 duration 时显式改为 `trace_kind=measured`，并在非空 `provenance` 标明各测量来源、运行环境、冷/热加载、预算及采样口径。缺少某个 job/backend 组合的实测时长时，缩窄 eligible 集合或提供另外一份明确 synthetic 假设，不冒充实测。即使所有单路时长来自实测，结果仍标为 `counterfactual_replay_not_live_benchmark`，不是已实现的真实并发收益。

## 显式 synthetic dry-run 报告

命令：`python3 scripts/replay_recipe_scheduling.py --synthetic`。以下数字来自实际执行这个 CPU 回放，不是 Ivan 实测：

- 所有任务于 `t=0` 到达，顺序为 B8、B8、A4、A4。
- synthetic fast 声明支持 A4/B8；synthetic main/preview 声明支持 A4。B8 仅 eligible fast；A4 eligible 三个 backend。
- 两条 B8 各 600s；两条 A4 在所有 eligible backend 上均 900s。这些时长、资格和内存均为人为夹具。
- 基线 effective working set 16GiB；每任务 18GiB；外部 remaining=0；margin=2GiB；上限 72GiB。三路预算恰为 `16+3×18+2=72GiB`，不靠减预算换并行。

| 指标 | strict FIFO | throughput |
| --- | ---: | ---: |
| 完成任务 | 4/4 | 4/4 |
| makespan（秒） | 1740 | 1320 |
| completed/hour | 8.275862 | 10.909091 |
| 平均 queue wait（秒） | 600 | 270 |
| 最大 queue wait（秒） | 840 | 720 |
| peak parallel | 3 | 3 |

| 任务 | FIFO 开始→完成（秒） | throughput 开始→完成（秒） |
| --- | --- | --- |
| 001-b8 | 60→660 | 60→660 |
| 002-b8 | 720→1320 | 720→1320 |
| 003-a4 | 780→1680 | 120→1020 |
| 004-a4 | 840→1740 | 180→1080 |

此夹具模型差为 420s，比例 `1740/1320=1.318182`；收益没有硬编码。两者峰值都是三路，差别是队首阻塞让可用 3060 模型通道晚启动，而不是凭空多出显卡。

反例回归：全部 B8 时两者仅一路、2640s、比例 1；基线改为 51GiB 时完整预算使两者串行；把 A4 时长改为 100s 时两者 makespan 都为 1320s、比例 1。不能用这里的 synthetic 比例宣传真实加速，也不进行画质或配方选优。

指标口径：makespan 是从回放 `t=0` 到最后完成，含初始稳定窗口、后续窗口和到达间隔；completed/hour=`completed×3600/makespan`，并非长期稳态容量估计；queue wait 从各任务到达至实际启动，包含保护窗口。未完成 trace 不输出完整批次吞吐或收益。
