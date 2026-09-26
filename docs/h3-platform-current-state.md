<!-- context-meta
{
  "status": "current",
  "last_verified_at": "2026-09-20T18:06:21+08:00",
  "verified_commit": "471b3004b14a4c7add9088e3d448a2eb41f7e087",
  "runtime_verification": "read_only_metadata",
  "authoritative_sources": ["../deploy/h3-fleet/README.md", "../deploy/h3-mcp/README.md", "../deploy/h3-video-studio/README.md", "../deploy/runtime-release/README.md"]
}
-->

# H3 平台当前状态

## 2026-09-26 Ivan 视频现场恢复与服务依赖修复（已部署）

- 2026-09-25 正式 API 的双 RTX 3060 同时执行两条 `i2v / 15 秒 / 16:9`，两份 362 帧、864×480、含音轨视频均完整解码；其后 NX5 正式生图作为 Ivan 第三条视频首帧的跨模态链路也完成。运行中终止空闲 preview worker 后，main 任务仍完成。文件与耗时证据见 `media-acceptance-20260925.md`；内容画质未由客户验收。
- 2026-09-26 Ivan 根目录只剩 24.325 GiB，低于 worker 的 25 GiB 启动门槛；worker 连续失败使 Fleet 随 `Requires=` 反复重启。清理回收站中两个已删除的旧缓存目录后，根目录剩余 29.390 GiB，两路 ComfyUI 与 Fleet 恢复；NVMe 视频目录剩余 45.663 GiB，数据库无活动/排队任务。
- 提交 `a988756d6` 已将 worker 重试间隔由 15 秒改为 120 秒，Fleet 对 worker 改用 `Wants=`。发布后受控停止 worker，Fleet 保持同一 PID，容量接口返回 `503 capacity_unavailable`；恢复 worker 后两路 ComfyUI 和容量接口正常，Fleet PID 未变。运行 unit 哈希及最后资源快照见 `media-acceptance-20260925.md`。

## 2026-09-25 Ivan 正式双路验收故障与修复候选

- 两条正式 API 的 15 秒 i2v 请求分别进入两张 3060，但 worker 在模型加载时因宿主机 swap 比启动基线增加约 1 GiB 而重启。故障当时可用内存约 43 GiB、worker cgroup 未记录 `max`/OOM 事件；两条执行进入待核对状态，不能记为双路成功。
- worker 保护改为监控自身 cgroup 的 swap 增长，保留宿主机 8 GiB swap 绝对上限、16 GiB 可用内存下限、磁盘、OOM 和温度保护。该改动须随正式 worker 发布并重新完成真实双路验收；源代码修复和隔离测试不代表生产通过。
- 单一 ComfyUI worker 异常退出时只重启该 GPU lane，另一条 lane 的进程保持运行；10 分钟内连续三次失败则停止自动重试该 lane，避免无限重启。宿主机资源硬保护仍会停掉整组 worker。单路故障隔离还须在两张卡空闲时做现场验收。
- NX5 正式 API 的 1280×720 文生图和 1376×768 图片编辑已在同一晚完成；图片质量仍由人工验收。视频生产发布及故障恢复的现场结果另见本轮交付报告。

## 2026-09-24 双 3060 配方接入候选（Git 已提交，正式未发布）

- 新增固定 `h3-i2va-480p15-3060-v1` 工作流，来源于已完成的双 RTX 3060 隔离测试；模板哈希固定，图形或参数变化须使用新版本。只有 15 秒、16:9、图生视频及原生音频能进入双路规则。
- Fleet 准入只对 `main`、`preview` 两条已验收 GPU UUID 的 RTX 3060 lane 开放此配方的双路；`preview_only` 对这个固定配方有窄例外。一轮两个名额用完后等两条都退出，避免沿用含残留内存的基线。原 `long.max_parallel=1` 和其他质量/预览规则保持。
- 普通 preview I2V 的首帧改为 Lanczos 等比居中裁剪。u24 4060 Ti 与 Ivan 双 3060 经同一独立 API 的三卡真实并发已通过，三份 15 秒视频完整解码，证据见 `outputs/media-validation-20260924/video-three-card/REPORT.md`。方形首帧的上下边缘被截断，画质仍需人工验收。验收时 Ivan 正式 worker 未启用，正式 offload 仅余约 6 GiB；没有生产切换。

## 2026-09-24 Ivan 视频生产发布（后续状态）

- 上节记录的是三卡隔离验收当时的状态。随后 Ivan 的视频任务 I/O 迁到 NVMe，
  两张 3060 的独立 Fleet/worker 与 Router 视频 API 已发布；正式 API 一条
  `i2v / 15 秒 / 16:9` 任务完成、归档并完整解码。证据见
  `outputs/video-release-20260924/REPORT.md`。
- 正式双路并发、三卡联合调度、客户 ComfyUI 图形工作流及画质仍未通过生产验收。
  u24 的 4060 Ti 继续承载文本服务，V100 原用途不变。

## 2026-09-24 媒体独立验收中的容量接口修复

- 独立 u24 Fleet 在 ComfyUI worker 停止时，容量查询曾因连接异常返回 500。
  `GET /api/router/capacity` 现在对上游 HTTP/连接及 JSON 解码错误返回
  `503 capacity_unavailable`；不会把未知队列当成空闲，也不公开上游错误文本。
- 三项隔离故障测试通过，并已在 u24 独立测试实例验证离线 worker 返回 503。
  修复已包含在 `5fd4f0e1b`，此次未发布到正式 Fleet，也未推送远端。
- 清理获批的旧 Ollama 模型后，独立 4060 Ti worker 完成 15 秒 i2v：362 帧、864×480、
  含音轨，客户 ComfyUI 保存/拆帧通过。调整了设备端 LoRA 模板，API 契约未改；
  方形首帧被拉宽，画质/比例策略未验收。测试后 ninfer 恢复、V100 原进程保留。
  证据见 `outputs/media-validation-20260924/REPORT.md`，不代表正式部署或其他组合验收。

## 组件边界

- `deploy/h3-video-studio/`：项目、流程和人工审批驱动的视频工作室。
- `deploy/h3-fleet/`：执行节点、资源准入、调度和状态汇总。
- `deploy/h3-mcp/`：Studio/Fleet 的工具、认证和 WorkBuddy 集成边界。
- `deploy/runtime-release/`：不可变发布、外部依赖指纹、切换与回滚。

具体接口和操作步骤以各组件 README 为准。本页只记录跨组件边界和带日期运行快照。

## 生产运行快照

**已部署并只读核验**：`/home/ai/.local/state/1panel-runtime/current` 当前解析到
`20260914-ee51dd6b3-r2`。release 工具验证结果为 `ok=true`，绑定 Git 提交
`ee51dd6b3bb3fe83ef5896c630ea1dd611bbefcf`，release SHA256 为
`0c155da561d49092b6cf18610235824ac2c23c6c663bc747602f49d28c9ce690`。

以下用户级服务在核验时处于 active/running，重启计数为零：

- `h3-studio-ivan.service`
- `h3-studio-ivan-preview.service`
- `h3-working-set-status.service`

正式 Studio 和预览 Studio 的 WorkingDirectory 指向 `current` 下对应不可变组件；状态
发布器指向 `current/h3-fleet`。本次没有提交项目、生成视频、调用模型或执行端到端质量
验收，因此不能从进程状态推断业务输出质量。

## 不变量

- H3 准入保持 fail-closed；资源预算、cgroup、模型、素材和人工审批缺证据时不放宽。
- 配置、发布工件、systemd 实际 WorkingDirectory、进程 cwd 和客户端可见结果分别验证。
- 不可变 release 验证通过不代表外部模型、venv 或节点当前可用；按任务核验对应外部组件。
- 未知写入结果使用原 operation ID 查询，不盲目重放或创建替代操作。
- GPU/真实媒体测试按主机串行，执行前确认范围，完成后复验原生产服务。

## 更新触发

Studio/Fleet/MCP 的接口、准入、审批、调度、运行目录或 release 工具变化时，同步更新
本页。生产切换后记录新的 release 路径、提交、清单哈希和 systemd 实际值；历史验收
文档继续保留，但不自动代表当前运行状态。
