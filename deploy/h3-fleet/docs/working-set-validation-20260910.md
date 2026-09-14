# Working-set 渐进并发真实验证：2026-09-10

用户在完成离线算法解释后授权“继续直到完成”。本轮仅部署独立 Ivan 实验运行器，不提交 Git、不重启生产服务、不改变保护阈值、不回收系统缓存。

## 固定工作负载

| case | worker | GPU | 目的 |
| --- | --- | --- | --- |
| A4_C1 | fast / 18188 | RTX 4060 Ti 16GB | A4 + 人物 LoRA 1.0，补齐缺失成片 |
| A4_C0 | main / 18189 | RTX 3060 12GB | A4 + 触发词，补齐缺失成片 |
| A4_C05 | preview / 18190 | RTX 3060 12GB | A4 + 人物 LoRA 0.5，第三路复验，不覆盖原成片 |

三条均保留原粉色卧室提示词、seed、480×864、362 帧、24 fps、4 步及完整音频。候选预算每条 18 GiB，VRAM 预算分别 14500/11000/11000 MiB；数字是保护预算，不代表已经认证三卡并发。

新增第三个独立 Comfy worker 使用普通 r2/Core 0.34.0、reserve-vram=3、独立 input/output/temp/user/SQLite。没有 FastH3/VSA patch，没有改变生产 GPU 服务。先启第一条，满足真实采样进展与连续稳定观察后才判断下一条；不足则继续等待，而不是通过减预算强行加路。

## v2：保护停机与原因

- 运行器 SHA256：`70ba4eaaddf7c0e57d68aab6052b7d80eb4e79aa941ec7b4e096863282093e7f`。
- 批次目录：`/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909/A4-working-set-v2`。
- C1 prompt：`2b1203cb-60eb-4504-bf84-4375eee806e9`，到达 sampler 3/4 后被保护中断。
- 14:54:15 的 host Swap 为 6,251,286,528 字节；0.139 秒后 `/proc/swaps` 为 6,251,274,240 字节，相差 12,288 字节。触发 `host_swap_inventory_mismatch`。
- 当时 pswpout、PSI 累计无增长，OOM/high/max 均为 0。不能把这次中断说成成片或并发成功。
- 报告保留失败状态；C0/C05 从未提交。三 worker 清理确认、租约释放；通过 `restore-admission.json` 恢复真实 preflight 观察到的生产 admission 状态，生产 PID 未变。

## 采样修复与 v3

不是扩大原来一页的匹配容差，而是把 meminfo / proc swaps / zram / cgroup swap 改成紧邻采集，最多三次一致性重读。

- before/after meminfo 和每次有效 proc Used 总和都计入峰值包络，不允许重读吞掉超过 8 GiB 或增长 1 GiB 的观测。
- 任意 disk swap 使用、backing 或其他安全异常立即拒绝，不用重读覆盖。
- 未完成与已完成的 attempt 都保留；持续不一致仍 fail-closed。
- 相邻峰值包络比较保留真实增长信号，同时避免固定一页统计偏差不断伪造新增长。
- 报告分别保留最终 Swap 值与期间峰值，不把恢复到低位当成没有增长过。

修复后 Fleet 全量 **848 passed**，定向测试与独立只读审阅通过。原保护策略 SHA256 始终为 `cb21b65c1bc443b39e50e452e7a667d25868f8c889eb3a361ea67a0740f4b792`。

v3 运行器 SHA256：`46b043831b65b519ae942fafb4dd68cdfdd88b52debe61317384ef20a8f008e0`。
批次：`pink20260910-A4-working-set-v3`。
部署：`/mnt/ivan-ext4-offload/h3-a4-working-set-20260910-v3`。
证据：`/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909/A4-working-set-v3`。

原七组及旧 A4_C05 视频全部保留。状态页按最新 run_id 显示真实任务、GPU、等待原因；C05 明确标识复验，不把原视频当成本轮产物。只发布有独立成功执行证据的 C1/C0。质量仍由用户人工确认。

## v3 实际结果：单路成片，12GB 显卡 OOM

- A4_C1 在 4060 Ti 成功生成，prompt `f5feede7-b73c-4a1d-8bb1-62c1de08ab81`，执行 551.179 秒。
- C1 运行期间，C0 加路预算未通过；调度器没有降低 18 GiB 候选预算或 72 GiB 保护线。C1 完成、卸载且资源重新稳定后才启动 C0。
- A4_C0 在 3060 上于 15:17:42 发生 `torch.OutOfMemoryError`，节点 `10 / SamplerCustomAdvanced`；当时已分配 8.26 GiB，申请 2.42 GiB，CUDA 可用仅 52.50 MiB。任务执行约 20.25 秒，没有有效成片。
- 这是 **GPU 显存 OOM**；没有 cgroup OOM 或 Xid 不能解释为“没有 OOM”。主机 working-set 准入通过不代表该配方的显存预算已经验证。
- 原始 `history.json` 与 journal 均保留。v3 原报告保持 `failed`、`peak_parallel=1`、`peak_parallel_running=1`、`parallel_validated=false`；C05 从未提交，不宣称第三卡验证完成。
- 清理后确认所有实验队列空、租约释放、模型卸载，并通过 SHA 固定的恢复工具恢复 preflight 的 `draining=false`。生产 PID 未变。
- 该配方不能继续沿用“12GB / 11000 MiB 预算已足够”的假设，不盲试另一张 12GB 卡。本次补片改用已经成功的 16GB 实验卡，VRAM 预算 14500 MiB；主机预算仍为 18 GiB。

失败批次中的成功 C1 只能通过显式部分导出模式发布：保留原始失败 batch-report，重新核对该任务的非缓存成功 history、音视频及 SHA，标明整批失败，不把失败批次改为成功，也不导出失败 C0 或未提交 C05。

## v4 单卡补片

`pink20260910-A4-working-set-v4-C0-fast` 只补 A4_C0，使用同一 working-set 保护器、同一原始配方、同一 4060 Ti。单任务模式不构成并发验证。C05 已有旧成片保留，不为凑三路而继续试跑未经验证的 12GB 组合。

- C0 成功，prompt `c238c443-0560-4548-a7ec-4cae57cfc65a`，执行 **559.614 秒**。
- 终态 `generated_pending_quality_review_drained`，`peak_parallel=1`，`parallel_validated=false`，lease 已释放、模型卸载已确认。
- 已确认 coordinator 退出，使用绑定 preflight 与源码 SHA 的恢复工具恢复接单；没有重启生产服务。
- v4 运行器 SHA256：`41faa8196cee60417022c864d871fe5ab79d0f9570b008aa4a808b82e4c92ac9`。
- v4 对账模块 SHA256：`cda95a52e1d0319ca97e0b13d5a6aeed6e5ab502c929bd9cee921d1ed47dfb42`。
- GPU history 中的 execution_error / execution_interrupted 现在单独写入任务与资源对账，CUDA OOM 不再被通用 history 校验错误遮盖，也不混同于 cgroup OOM / Xid。

## 预测与实际对账

单位为 GiB；峰值是周期采样高水位，不冒充连续硬件峰值。

| 项目 | v3 成功 C1 | v4 成功 C0 |
| --- | ---: | ---: |
| admission projected | 55.645 | 58.172 |
| 实际 aggregate memory.current 峰值 | 59.194 | 64.347 |
| aggregate 相对 baseline 峰值增量 | 3.193 | 3.965 |
| worker 对账窗口峰值增量 | 3.465 | 3.965 |
| 最低 host available | 64.967 | 62.632 |
| aggregate pgsteal 回收累计量 | 1.269 | 0 |
| host Swap 最终增量 | -45056 字节 | -229376 字节 |
| host / cgroup Swap 峰值增长 | 0 / 0 | 0 / 0 |
| host / cgroup PSI avg10 峰值 | 全部 0 | 全部 0 |

预测使用 cache 折扣后的 working set，实际 aggregate 峰值包含仍驻留的 cache，两者不是相同口径；实际 raw peak 高于 projected 不等于分配超限，更不能据此自动调低余量。预测扣除可回收量不是强制或已发生的回收量。pgsteal 是整个 cgroup 累计页回收，可含匿名页和重复回收，不是该任务因果归属的净文件缓存释放。

v4 无 CUDA OOM、cgroup OOM/high/max 增量或 Xid；v3 的 C0 **有 CUDA OOM**，保留失败记录，不能用成功 C1/C0 补片的安全结果抵消。

C0 的 report / metrics / input workflow / video 已固定 SHA 并采集到 `A4-working-set-v4/verified-peak-history.json`。执行窗口 worker delta 为 4,255,813,632 字节，含结束后 accounting 的保守预算 delta 为 4,257,038,336 字节；下一次匹配同一完整 profile 时仍保留原 **18 GiB 成功预算下限**，不自动降为约 6 GiB。C1 所属批次失败，其对账保留为证据，不自动把失败批次提升为已验证预算历史。

## 回归与页面交付

- Fleet 最终全量 **909 passed**；五组实验配方 **190 passed**；实验恢复工具 **7 passed**。
- Studio Python **69 passed**，仅 4 个既有 FastAPI 弃用警告；前端 Node **10 passed**。去重合计 **1185 passed**。
- 51.6 / 18 GiB dry-run 的 24 个内置断言通过，输出与保存 JSON 逐字节一致；`git diff --check` 通过。
- C1 部分导出重新校验成功 history、工作流、原始/实际媒体元数据及 SHA；C0 通过成功批次正常导出。两条均通过 ffprobe 362 帧、24 fps、音频存在检查以及 ffmpeg 完整音视频解码，再写入原测试页。
- 页面地址：`https://ai-x10drg.taild500c8.ts.net:8445/comparison.html`。保留原七项和旧 C05，新增 C1/C0；质量状态仍为 `pending_human_review`。
- C1 SHA256：`25d80180e0f7a14a7f1ade05991383896a69079348a82f3767d151392eda05ca`。
- C0 SHA256：`bf5f22d36b5e21fcd8e98482574b33959ee5e24a530d7e0fa3e358417ac6711a`。
- AI 本地持久证据根：`/home/ai/.local/state/h3-studio-ivan-production/deployment/optimization-20260909`，包含 v3/v4 原始报告、metrics、admission、restore receipt、失败 journal、导出 provenance 和页面验证。
- 最终回归日志与关键 coordinator diff：`A4-working-set-v3/final-fleet-regression.txt`、`final-ui-regression.txt`、`final-coordinator.diff`；未提交或推送 Git。
- 15:42:53 实际浏览器验收：10 卡、10 视频全部加载；新 C0 连续播放至 15.083333 秒 ended，无媒体或 JS 错误，桌面/手机完整竖屏。原 8 条记录及 8 个视频 SHA 保持不变；C1 明确“本条成片已保留；所属并发批次失败”，C05 明确“本轮未提交、未运行”，旧预览不代表本轮产物。证据位于 `A4-working-set-final-ten-cards-ui/report.json` 及同目录截图。
- 15:40:25 最终现场审计：六队列空、`active=[]`、`lease=null`、`draining=false`；Fleet/三生产 PID 及启动标识未变，磁盘 swap 使用为 0。只读状态采集进程为更新状态文案而单独刷新，Studio/Fleet/生产 GPU 服务均未重启。

## 最终验收

算法实现、离线回归、实验部署、真实单路成片和两条缺片发布完成；**本轮二路/三路并发验证未通过**。原 C05 成片仍在，v3 的 C05 复验未执行，不得声称已完成三卡实测。后续如继续并发验证，应先独立降低当前配方/运行方式的真实显存峰值并重新验证 12GB 卡，而不是放宽内存门禁或重复盲试。
