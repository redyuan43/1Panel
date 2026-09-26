# 2026-09-27 4060 Ti 纳入正式视频池

## 链路与开放范围

客户端 ComfyUI 只提交一次 `i2v / 15 秒 / 16:9` 的生成请求，并在任务完成后接收视频。1Panel 媒体适配器按已验收能力选择执行端：u24 RTX 4060 Ti 优先，Ivan 两张 RTX 3060 为后续候选。H3 Fleet 留在设备侧，负责固定配方、ComfyUI worker 准入、任务状态和文件映射；1Panel 不接收客户端的 ComfyUI 工作流图。

u24 使用一张卡、一个 Fleet lane、一个 ComfyUI worker。4060 Ti 原有 ninfer 文本服务经同端口 TCP 代理接入；视频准备时代理停止接收新文本请求，等待已建立连接结束，再停文本服务并启动视频 worker。Fleet 确认任务空闲且空闲窗口届满后，控制器停止视频 worker，恢复文本服务。1Panel 可以继续从持久输出目录归档已完成的视频。V100 不属于该切换单元。

仅以上组合已作为正式候选。文生视频、竖屏、其他时长和高分辨率仍不开放。已完成文件从 u24 持久输出目录读取，使 ComfyUI worker 停止后原任务仍可下载。任何无法确认的 Fleet 状态都阻止文本与视频在 4060 Ti 上同时运行。

## 代码审查与隔离检查

- 修复了媒体适配器重启后、已预约但尚未发送任务时可能跳过 GPU 切换检查的问题。
- 将 u24 策略收紧为单卡单任务；拒绝复用 Ivan 的双卡资格配置。
- 控制器启动及运行时核对文本与视频服务状态；状态不一致时关闭新请求准入。
- 校验 ninfer 原始启动命令及 ComfyUI 插件链接，避免原设备或运行时变化后继续切换。
- 正式任务完成后发现控制器误用 `127.0.0.1:19390` 查询仅绑定 Tailscale 的 Fleet，导致文本未自动恢复。按实际监听地址修复，新增能复现这一故障的测试；r4 现场确认自动恢复成功。
- 代码审查发现无条件依赖文本 unit 会在视频占用状态重启时造成双服务竞争；改由控制器读取持久状态、确认 Fleet 空闲后恢复文本，且新增重启状态测试。
- 进一步修复同一排队任务反复探测时持续刷新空闲计时、长期占用 4060 Ti 的边界；超过空闲窗口后可回退 Ivan，并加入状态测试。r5 在 u24 无视频活动时发布，文本入口复验 200。
- `test_video_single_release.py` 10 项通过；u24 切换、文本包装、持久输出与相关 Fleet 目标测试 22 项通过；u24 容量配置通过策略解析；`git diff --check` 通过。

## 生产验收记录

正式 `/v1/videos` 使用一个临时内部账号和同一 API Key，三条请求均为 `minimax-h3 / i2v / 15 秒 / 16:9`，首帧来自 NX5 先前真实生图 `img_19116e6bd5864656b5d4198996aec17e`。时间均为北京时间。三个任务 ID 与 u24/Ivan Fleet 的执行 ID 一致；u24 使用 `fast / preview / 6 步`，Ivan 两卡使用 `main`、`preview / quality / 固定 14 步配方`。步数和配方不同，耗时不作为同质量性能比较。

| 正式任务 | GPU | 提交至完成 | 耗时 | 公开文件 | 技术结果 |
| --- | --- | --- | ---: | ---: | --- |
| `vid_91f0462223d04b50876c2047ab4d06c9` | u24 4060 Ti | 00:00:26–00:14:26 | 839.85 秒 | 2,485,105 B | 864×480、362 帧、15.083 秒、H.264/AAC，完整解码 |
| `vid_0ba4dd0e2c324e1e86c9b0cfda3fc232` | Ivan 3060 main | 00:04:05–00:28:52 | 1486.69 秒 | 2,384,905 B | 同上，完整解码 |
| `vid_96b7a41722714081b7b8c3cc6f0f411a` | Ivan 3060 preview | 00:04:05–00:29:22 | 1517.13 秒 | 4,134,225 B | 同上，完整解码 |

三卡同时处于 Fleet `running`，GPU 遥测也同时显示工作负载；两张 3060 没有落到同一 lane。监测文件保存在 AI 私有状态目录的 `acceptance-20260927-three-card-metrics.jsonl`。4060 Ti 显存峰值 14,739 MiB、温度峰值 86°C；两张 3060 分别为 8,930/8,840 MiB、83/64°C。V100 显存始终约 14,848 MiB，没有参与切换；Ivan 视频盘以 `df -BG` 采样始终显示 46 GiB 可用，高于 40 GiB 门槛。

Fleet 原文件 SHA-256 依次为 `2f849f16ef6b0a2508493f9e42a22dba43f56b951f7717082675e8af9644ade5`、`7d58b914729b6cbabed8dea7a6e72ca728683a4a9c9b80f866108ebb648aa88c`、`5c2bcf934320c0d2f00ab2c0bdf682265fed53cb50029994a28000fabe5e0889`，逐一等于 1Panel 保存的源文件哈希。1Panel 按既有机制去除视频元数据，因此公开归档哈希不同；从正式客户 API 再下载的三份文件与各自公开归档哈希完全一致。u24 ComfyUI worker 停止后，使用原执行 ID 从 Fleet 再取文件仍返回 200，大小和原文件哈希不变。

正式接口还验证了竖屏、14 秒、文生视频均返回 `422 media_no_compatible_executor`，账号任务数没有增加；相同幂等键重试返回原任务 ID。验收后临时 Key 已撤销、账号已停用，原 Key 请求返回 401。

最终现场：AI 媒体适配器运行 `20260926-u24-handoff-909f26cf1` release，u24 Fleet/handoff 运行 `20260927-u24-handoff-909f26cf1-r5` release；u24 文本服务 `/health` 和 `/v1/models` 均为 200，视频 worker 为 inactive，4060 Ti 显存回到约 12,519 MiB。Ivan Fleet 和双 worker 均 active，三卡 Fleet 任务均已完成、无新的活动任务。u24 Fleet/handoff 已设为 user systemd 开机启动；实际整机重启恢复未测试。视频画质仍须客户人工验收，技术验收不代替画质评价。

## 回滚与人工核对

AI 媒体适配器的旧 release 为 `20260925-video-options-2e270a521`；原视频池配置与环境文件在 `/home/ai/.local/state/siyuan-video-production/` 的 `before-u24-20260926` 备份中。u24 原 ninfer unit 保存在 `~/.local/state/siyuan-video-u24-production/original-ninfer.service`。发生未知视频占位时必须先按原任务 ID 和 Fleet 数据库核对；不得直接同时启动文本与视频 worker。确认无活动任务后，禁用 u24 pool 条目、恢复旧媒体 release 和环境，再停止 u24 handoff/Fleet、移除 ninfer drop-in 并恢复原文本 unit。核对 18086 文本健康及 V100 进程后结束回滚。
