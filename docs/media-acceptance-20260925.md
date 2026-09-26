# 2026-09-25 媒体代码审查、生产发布与真实验收

状态：三条正式视频任务均已完成，包含 NX5 生图作为 Ivan 视频首帧的跨模态链路；2026-09-26 再核验时发现并处理了 Ivan 磁盘门槛导致的服务重启。时间均为北京时间。

## 版本与范围

| 部件 | 运行版本 | 核验 |
| --- | --- | --- |
| Router API local/tail | Git `943a2332b`，镜像 `sha256:e465c25a…` | 两实例运行、零重启；镜像 OCI revision 和源码哈希匹配 |
| Router Control local/tail | Git `943a2332b`，镜像 `sha256:417cbd159…` | 两实例运行、零重启；镜像 OCI revision 和源码哈希匹配 |
| 媒体适配器 | `20260925-video-options-2e270a521`，源码提交 `2e270a521` | release 清单、运行 cwd 与健康检查通过 |
| Ivan 双 3060 worker | `b67ef8bd9`，脚本 SHA-256 `256db1fdcf037fc236f93f96596139c33ad115f0f8cc7aa26553487f7cd02948` | 两 lane 健康，Fleet 独立运行 |
| NX5 图片 | `nx5-image`，文生图配方 `qwen21-t2i-landscape-candidate-v1`、编辑配方 `qwen21-edit-nx5-v1` | 正式 API、适配器任务、NX5 ComfyUI history 和输出哈希相互对应 |

Router 初次滚动发布因生产 V100 observer 外部包缺失而自动回滚；补入与原运行包相同的私有 overlay 后逐实例排空到零并发布成功。Redis、LiteLLM、模型服务、u24 文本及 V100 未替换。

## 代码审查与修复

- `943a2332b`：修复全候选 `history_incompatible` 被误报为 503；补齐 Chat/Responses、流式/非流式测试；将已在线配置值并回 Git。
- `9f144772b`：Ivan worker 不再因其他进程引起的宿主机 swap 增长误停两路；改看本 cgroup swap，同时保留宿主机 8 GiB swap、16 GiB 可用内存、磁盘、OOM 和温度保护。
- `2e270a521`：仅有 `minimax-h3` 授权也能获得视频能力选项；不支持的组合在建任务前返回 422，已配置但执行端未就绪仍返回 503。
- `b67ef8bd9`：单个 ComfyUI worker 异常退出只重启本 lane；10 分钟内三次失败停止自动重试，另一 lane 保持运行。
- 未跟踪的旧验收脚本、示例、草稿和 `.pending.yaml` 未进入生产提交；其中旧 `media-model-api.md` 仍描述“未上线”，不作为当前状态来源。

相关测试：Router 全量回归退出码 0（2827 通过、10 跳过），新增历史预检 8 个组合通过；媒体相关 150 个目标用例通过、最后的视频准入用例 7 个通过；Fleet 全量旧基线 1292 通过，新 worker 相关 136 通过，单 lane 监督器 4 个用例通过。`git diff --check` 通过。

## NX5 真实生图

| 请求 | 时间 | 模型/配方 | 输出 | 耗时 | SHA-256 |
| --- | --- | --- | --- | ---: | --- |
| 文生图 `img_19116e6bd5864656b5d4198996aec17e` | 00:56:13–00:57:44 | `qwen-image-2.1` / `qwen21-t2i-landscape-candidate-v1` | 1280×720 PNG，1,533,704 B | 91.26 秒 | `fe4bc1f74a9fe985dbdb32297ef1160a1ee0d76166643c87484df3cee2d99158` |
| 参考图编辑 `img_34320d133deb46ecaf4c0427e01f5ac5` | 00:57:44–01:01:27 | `siyuan-image` / `qwen21-edit-nx5-v1` | 1376×768 PNG，1,665,792 B | 222.77 秒 | `242ee9079816804554366bf823e60ee3326612c8a2b1ffe88b28809b40580d40` |

两条正式任务 `provider=local`、`endpoint=nx5-image`、`resource=nx5_gpu`。图片适配器内部任务分别为 `img_51b6…` 与 `img_8a9e…`，其 ComfyUI prompt ID 分别为 `f19343c6-96a4-4718-9aac-0b3204b63e27`、`dc02f640-7374-434e-bcf5-2c0271dafb17`；NX5 `/history/{id}` 均为 `success/completed`。适配器原始 PNG 与正式 API 下载的 SHA-256 逐一相同。文生图是红色湖边木屋，编辑后木屋外墙变蓝；视觉检查符合此次提示词，但商业质量仍需人工验收。

## Ivan 双 3060 真实并发

首轮两条任务因旧 worker 把宿主机 swap 增长误判为自身内存压力，在 00:56:31 一起中断；原执行进入 `reconciling`。核对工作进程重启、两条后端队列与 history 为空、无新输出后，按原任务 ID 取消；事前备份 Fleet SQLite。没有盲目重投或释放未知任务。

修复上线后，01:06:41 同时从正式视频 API 提交两条 `i2v / 15 秒 / 16:9`、原生音频任务，均使用固定配方 `h3-i2va-480p15-3060-v1`：

| 模型/任务 | GPU lane | 完成时间 | 端到端 | 文件 | 技术校验 |
| --- | --- | --- | ---: | ---: | --- |
| `siyuan-video` / `vid_ed486abbf5c84b5cb4e8df38a7f76fe3` | main | 01:31:21 | 1479.95 秒 | 2,157,561 B | 864×480，362 帧，15.084 秒，AAC，全帧解码成功 |
| `minimax-h3` / `vid_7287ed44d402428c9ae5f571f2753871` | preview | 01:31:58 | 1516.34 秒 | 7,668,005 B | 864×480，362 帧，15.084 秒，AAC，全帧解码成功 |

ComfyUI `execution_start` 分别为 01:06:46.071、01:06:46.115，`execution_success` 为 01:31:17.454、01:31:44.624；实际计算区间重叠约 1471.34 秒。743 个约 2 秒采样点：worker 内存峰值 16.403 GiB、worker swap 峰值 0、宿主机 swap 峰值 0.449 GiB、可用内存最低 38.365 GiB、根目录剩余最低 25.898 GiB、视频盘剩余最低 45.667 GiB；两卡显存峰值 9,880/9,255 MiB，温度峰值 79/63℃，cgroup `max/oom/oom_kill` 均为 0。两份视频逐帧计算均为 362 个不同画面帧，音轨非零（平均音量分别 -16.0 dB、-36.4 dB）。任务完成后两个 Fleet 执行均为 `completed`，正式 API 下载 SHA-256 与本地文件一致。

同请求 ID 重试返回原任务，账号视频任务总数保持 4；同 ID 改提示词返回 409 `idempotency_conflict`。未验收的 t2v、4 秒 i2v 与竖屏均在建任务前拒绝；新媒体适配器上线后两入口的有效首帧请求返回 422。

## 单路故障与跨模态链路

空闲时终止 preview worker：约 24.43 秒恢复，main PID 不变。第三条由 NX5 文生图的 PNG 作为首帧，进入 Ivan main；其运行中再次终止空闲 preview：约 23.57 秒恢复，main PID 不变，原执行继续 `running` 且 `reconciliation_required=false`。

第三条 `minimax-h3` 任务 `vid_b258088cd89b4bb49481f08ea092dbf1` 于 2026-09-25 01:36:44–02:01:57 完成，端到端 1512.92 秒。首帧 SHA-256 `fe4bc1f74a9fe985dbdb32297ef1160a1ee0d76166643c87484df3cee2d99158` 与 NX5 正式文生图输出完全相同；正式视频输出 3,273,716 B，SHA-256 `27d2d1c40be33ac7af91d8fc4199c072465f58834c5c51502501c8263fc9dc8a`。正式 API 于 2026-09-26 再查仍返回 `completed`。文件为 864×480、15.084 秒、362 帧、H.264/AAC；全段解码成功，362 帧哈希各不相同，音轨平均音量 -19.1 dB。运行中 preview 进程故障没有中断 main 的这条视频。这证明客户端 API→NX5 图片→首帧传入→Ivan 执行→客户端下载的技术链路；中帧未出现提示词里的红色木屋，不能据此声称内容或画质验收通过。

## 2026-09-26 磁盘门槛与服务恢复

当日 20:55 只读核验时，Ivan 根目录剩余 24.325 GiB，低于 worker 启动门槛 25 GiB；worker 报 `worker preflight resources unavailable`，systemd 累计重启约 7,623 次，Fleet 因 `Requires=` 随之反复停止和启动。两卡显存空闲、可用内存约 49 GiB、swap 约 249 MiB，视频 NVMe 剩余 45.663 GiB。逐项读取 worker 的实际采样和门槛，定位为根目录空间。仅清理 Ivan 回收站中已删除的旧 Espressif 分发包 `dist.2.2` 和旧设备模型缓存 `OptGuideOnDeviceModel.2.2`（合计约 5.1 GiB），根目录回升至 29.390 GiB；随后两路 ComfyUI `/system_stats` 均成功，Fleet/worker 恢复 active。Fleet 数据库只有 `completed=4`、`cancelled=2`，活动/排队任务为 0。

为防止下次资源不足时重启风暴，将 worker 失败重试间隔从 15 秒改为 120 秒，并把 Fleet 对 worker 的启动依赖改为 `Wants=`；worker 缺席时容量查询已有 `503 capacity_unavailable` 保护，Fleet 可以保留任务状态查询。配置已从提交 `a988756d6` 发布到 Ivan，两份运行 unit 的 SHA-256 分别为 `f2deaa60664e35990ceaae31709e0c738b027f587ee053002768b0d80bc7309c` 和 `4387a6ba62c5adef51fdd1672d4edb4e19a7c80b2a267c283aed2531cfbf77ef`。

## 账号与最终发布记录

夜间验收临时账号 `media-night-acceptance-20260925` 的三条成功视频任务已在正式 API 再查为 `completed`；验收 Key 已撤销，账号已停用，本地明文文件已改为 `REVOKED`。local API 用原 Key 复核返回 401。

发布后受控停止 worker，Fleet 仍保持同一 PID `3114577` 且 active；容量接口从 200 转为 `503 capacity_unavailable`。重新启动 worker 后，两路 ComfyUI 恢复，容量接口回到 200，Fleet PID 未变。最后核验 Ivan 根目录剩余 29.389 GiB、视频 NVMe 剩余 45.663 GiB；worker 与 Fleet 均 active，worker 重启计数在这次手动启动后为 0，活动/排队任务仍为 0。

2026-09-26 21:07 最后核验时 Router local/tail 的 API 镜像 ID 均为 `sha256:49cfda35d70fdbd4405567720b35cf56e404bb9a92af3e214dbfb29b0c92f01f`，Control 镜像 ID 均为 `sha256:0a9706fb612a42b5d4081c37055b1cc5553c08e82e51165d27895a65c5e523c7`，OCI revision 均为后续 Spark 提交 `f45d95d9a`，四实例运行且重启计数 0；local/tail API `/health` 均为 200。媒体适配器仍运行 `20260925-video-options-2e270a521` release，服务 active、重启计数 0、鉴权后 `/health` 为 200。Ivan worker 脚本仍为 `b67ef8bd9`，本次只更新其 systemd 单元。代码提交 `a988756d6` 与验收文档已推送至 `fork/dev-v2`；本报告的后续更正以该分支 Git 历史为准。

## 边界

本次正式开放范围仅为 15 秒、16:9、带首帧的 H3；t2v、竖屏、其他时长、高分辨率未开放。u24 4060 Ti 继续承载文本，V100 保持原用途，故本轮没有做生产三卡联合调度。NX6 正在执行其他任务，NX6/NX7 图片资格未在本轮扩大。历史 11 条旧创作视频任务长期显示 `in_progress`（最后更新时间在 9 月 9 日以前），未在本次修改或自动释放；它们不属于新单次任务。
