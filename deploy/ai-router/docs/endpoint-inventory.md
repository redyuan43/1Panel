# 模型端点清单

本文件保留初始只读盘点。2026-09-01 的正式部署、真实推理、质量基准和
故障注入结果见 `live-acceptance-2026-09-01.md`，该报告优先于本文件中的
“尚未验证”历史记录。

## 上游与扩展边界

- 仓库：`/home/ai/github/1Panel`
- 上游：`https://github.com/1Panel-dev/1Panel.git`
- 分支：`dev-v2`
- 审计提交：`3ab10848c`
- 1Panel 开源主体：未修改
- 本地新增：`deploy/ai-router`
- 接入方式：1Panel Custom Provider 指向独立 OpenAI 兼容路由服务

## SSH AI

| 项目 | 记录 |
| --- | --- |
| 访问地址 | `http://127.0.0.1:18103/v1`；容器内使用 `host.docker.internal` |
| 健康检查 | `http://127.0.0.1:18103/health` |
| 模型 ID | `huihui/Qwen3.8-27B-Q4-DFlash2` |
| 模型工件 | `Huihui-Qwen3.8-27B-abliterated-Q4_K.gguf` |
| Draft 工件 | `Qwen3.8-27B-DFlash2-Q4_K_M.gguf` |
| 编排 | 健康信息报告 `ray_actor` |
| Speculative | 健康信息报告 `draft-dflash`，`draft_n_max=5` |
| 配置/安全上下文 | 注册配置 262144；当前健康池上限 196608，P40 worker 为 65536 |
| 路由并发 | 每个物理 worker 为 1；健康时池总并发上限为 6 |
| API 兼容 | Chat Completions、Responses、流式、工具和结构化输出已验证 |
| 模态 | 生产只开放文本；小图直连通过，但高分辨率 mtmd chunk 在 P40/V100 均失败 |
| Responses | Router 直接绑定物理 worker，不经过 LiteLLM 逻辑池 |
| 服务管理方式 | `bonsai-local-pool-v2.service`，`active/enabled` |

正常拓扑为 6 个物理 worker：

| 端口 | GPU 拓扑 | KV cache | 安全上下文 |
| --- | --- | --- | --- |
| 18110 | Tesla V100-PCIE-32GB | F16/F16 | 196608 |
| 18111 | Tesla P40 | Q8_0/Q8_0 | 65536 |
| 18112 | Tesla P40 | Q8_0/Q8_0 | 65536 |
| 18113 | Tesla P40 | Q8_0/Q8_0 | 65536 |
| 18114 | Tesla P40 | Q8_0/Q8_0 | 65536 |
| 18115 | Tesla P40 | Q8_0/Q8_0 | 65536 |

路由器为每个物理 worker 独立控制一路并发，并在池级公平排队。Chat
Completions 会把同一会话固定到具体 `worker_id` 和端口；物理 worker
不可用时，才会在同模型内切换 worker。

2026-09-01 20:42:03，原 `18111` 使用的
`GPU-4c63c711-9570-75db-760d-c6679c760754` 再次出现 `Xid 79` 掉总线，
随后 `Xid 154` 要求 GPU reset。2026-09-02 的恢复尝试确认旧
`llama-server` 线程无法被 SIGKILL 清理，新 P40 worker 均报 CUDA
initialization error。2026-09-02 物理移除该 V100 SXM2 16GB 后重新启动，
系统稳定识别 5 张 P40 和 1 张 V100 PCIe 32GB；六个 worker 均完成真实
文本生成，启动后未出现新 Xid、GPU reset 或 OOM。

同日使用 271x210 PNG 直连 AI 池，模型正确返回“红色”；约 4 MiB Base64
的 1024x1024 红色 BMP 也曾在 P40 worker 返回“红色”。但随后来自 nx4 的
更高分辨率图片在 P40 和 V100 上均出现
`failed to find a memory slot for batch of size 920`，因此 AI 保留 mmproj
用于后续运行时修复，不进入生产视觉候选。

## SSH Edge

| 项目 | 记录 |
| --- | --- |
| Tailscale DNS | `edge.taild500c8.ts.net` |
| API 地址 | `http://edge.taild500c8.ts.net:18300/v1` |
| 健康检查 | `/health` 返回 HTTP 200 空响应 |
| 负载检查 | `/metrics`，按 vLLM 指标解析 |
| 模型 ID | `RadixArk/Qwen3.8-Flash-Next-NVFP4` |
| 推理后端 | vLLM |
| 配置上下文 | 500000 |
| 初始安全上下文 | 262144，仍需真实长请求验证 |
| 路由并发 | 1 |
| GPU 拓扑 | 未验证 |
| 模态 | 当前生产只开放文本；视觉配置存在，但真实图像请求曾导致容器退出 |
| 服务管理方式 | `qwen38-flash-next-vllm.service`，用户级 systemd |
| Chat/Responses | 非流式、流式、单/并行工具、续轮和 JSON Object/Schema 已验证 |

2026-09-02 的视觉探测返回 HTTP 500 后，`--rm` 容器退出并使 `18300`
离线。原 systemd 服务随后恢复，健康检查和短文本 `EDGE_TEXT_OK` 均通过。
修复图像运行时并重新验收前，Edge 不进入视觉候选。

## SSH Ivan

| 项目 | 记录 |
| --- | --- |
| Tailscale DNS | `ivan-ms-7b17.taild500c8.ts.net` |
| API 地址 | `http://ivan-ms-7b17.taild500c8.ts.net:18104/v1` |
| 健康检查 | `/health` 返回 `{"status":"ok"}` |
| 负载检查 | `/slots` |
| 模型 ID | `huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF` |
| 真实工件 | Qwen3.8 Flash Next `UD-Q4_K_XL` |
| 推理后端 | llama.cpp |
| 配置上下文 | 131072 |
| 已实测上下文 | 本轮只验证短请求；128K 长输入未验证 |
| 路由并发 | 1 |
| KV cache | Q8 |
| Speculative | MTP 2 |
| 模态 | 生产开放文本、图像；mmproj + 1024 image tokens 图像切换实测通过 |
| 服务管理方式 | `qwen38-flash-next-mtp-128k.service`，`active/enabled` |
| Chat Completions | 直连返回 `IVAN-READY` |

服务运行在独立主机 `ivan-MS-7B17`，Tailscale 地址 `100.96.79.21`。当前
服务日志中已观察到的最大 prompt 约 3090 tokens，不能据此宣称 128K
已完成真实验证。历史确定性小题报告位于
`/opt/1panel/ai-router/benchmarks/report-ivan-20260901.json`。协议验收后已
参与 `auto`；缺失专项质量分只降低排序，不取消资格。

## SSH AMD

| 项目 | 记录 |
| --- | --- |
| Tailscale DNS | `ivan-superai.taild500c8.ts.net` |
| API 地址 | `http://ivan-superai.taild500c8.ts.net:18106/v1` |
| 健康检查 | `/health` 返回 HTTP 200 |
| 负载检查 | `/slots` |
| 模型 ID | `Qwen/Qwen3.8-Flash-Next-ROCmFP4-FAST-imatrix-MTP` |
| 推理后端 | llama.cpp |
| 配置/安全上下文 | 131072；约 120K 冷轮和缓存轮已通过 |
| 路由并发 | 1 |
| KV cache | Q8 |
| Speculative | MTP 3 |
| 显存 | 约 93.85 GiB |
| 模态 | 生产开放文本、图像；mmproj + 1024 image tokens 图像切换实测通过 |
| 服务管理方式 | `qwen38-flash-next-amd-rocmfpx-128k.service`，`active/enabled`，用户 `Linger=yes` |
| Chat Completions | 直连返回 `AMD_128K_DIRECT_OK` |

约 119954-token 实测中，冷轮 prompt 阶段为 584.79 秒、205.12 tok/s；
缓存轮只处理 4 个新 token，prompt 阶段为 0.241 秒。两轮 decode 分别为
21.12 tok/s 和 21.66 tok/s，MTP 接受率分别为 64.55% 和 61.88%。
完整结果位于远端
`/home/admin/github/qwen3.8_flash_next/results/20260901-amd-rocmfpx/20260901T1544-rocmfpx-128k-120k-cold-cached.json`。

AMD 与 Ivan 是两台独立设备，可以作为两路独立物理容量调度。协议验收后
AMD 已参与 `auto`；尚未完成的同口径质量基准只影响排序分。

## 尚未验证

- Edge/Ivan/AMD 的完整 GPU 拓扑和量化工件文件校验
- 500K/262K 长上下文边界
- AI 多图片和长时间视觉稳定性；Edge 图像运行时修复
- 确定性质量基准分
