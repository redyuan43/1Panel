# AGX Cerebellum 接入与上下文验收

现场日期：2026-09-04。本文只记录本次 AGX 服务，不替代其他节点的验收。

## 端点与运行配置

| 项目 | 现场值 |
| --- | --- |
| 主机 | Jetson AGX Orin，约 61 GiB 统一内存 |
| Tailscale DNS | `agx.taild500c8.ts.net` |
| API | `http://agx.taild500c8.ts.net:8080/v1` |
| 健康与负载 | `/health`、`/slots` |
| Router endpoint | `agx-qwen36-cerebellum-256k` |
| Router 模型 ID | `deucebucket/Qwen3.6-35B-A3B-Cerebellum-Q3_K_M` |
| 后端模型 ID | `Cerebellum-v1-Q3_K_M.gguf` |
| 工件 | `/data/models/Cerebellum-v1-Q3_K_M.gguf` |
| 后端 | `/home/agx/llama.cpp-mtp/build-agx-cuda/bin/llama-server` |
| 服务 | `agx-cerebellum.service`，systemd，单 slot |
| GPU | Orin 单集成 GPU，41/41 层 offload |
| 量化 / KV | Cerebellum Q3_K_M 混合精度 / K、V 均为 q8_0 |
| Speculative | 未启用 |
| 模型训练上下文 | 元数据 `n_ctx_train=262144` |
| 原配置 | `-c 131072 --parallel 1` |
| 本次配置 | `-c 262144 --parallel 1` |
| 256K KV cache | 启动日志实测 2720 MiB，原 128K 为 1360 MiB |

独立覆盖文件来源为 `config/agx-cerebellum-256k.conf`，安装到 AGX：
`/etc/systemd/system/agx-cerebellum.service.d/256k.conf`。
原始 unit 没有修改，副本保存在：
`/data/agx-runtimes/cerebellum-256k-20260904-184218/original.service`。

## 验收方法与证据

本地持久证据根目录：
`/home/ai/.local/state/ai-router-acceptance/20260904-agx256k/`。

`scripts/validate-llamacpp-context.py` 经明确 `--execute` 才发送推理请求；
每次先确认模型工件和单 slot 容量，并等待空闲。测试使用独立随机记录和五个
不可预知的值，分布在输入开头、四分之一、中间、四分之三和末尾。
用后端 `/apply-template`、`/tokenize` 测量完整 prompt，再核对响应中的
实际 usage、完成原因、SSE 结束标志和五个检索值。设置 `cache_prompt=false`，
保留输出预算，不截断长文来绕过容量限制。

每次测试的 `request.json`、`response.sse`、`preflight.json`、`report.json`
均保留，已有报告不能覆盖。所有数据都是合成测试内容。

| 测试 | 实际输入 | 输出 | 检索 | 冷缓存耗时 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 128K 边界 | 130431 | 101 | 5/5 正确 | 361.744 秒 | `context-128k/report.json` |
| 256K 边界 | 261493 | 100 | 5/5 正确 | 944.759 秒 | `context-256k/report.json` |

两轮报告的 `cached_tokens=0`。128K 首 token 耗时 353.998 秒，
256K 首 token 耗时 932.143 秒。以上测试仅证明对应
合成输入的处理和检索结果，不代表所有长文任务、多模态长上下文或持续并发质量。

## 协议能力

直连报告：`protocol-direct/report.json`，12 项中 11 项通过。

- 已通过：Chat JSON/SSE、原生 Responses JSON/SSE、工具 `auto`、`none`、
  `required`、指定函数、并行工具、工具结果续轮、JSON Schema。
- 未通过：`response_format.type=json_object` 返回 Markdown fenced JSON，
  不符合纯 JSON 协议。注册表不声明此能力，Router 应在选模时排除该组合。
- 图像：已加载 `/data/models/Cerebellum-mmproj-BF16.gguf`。2026-09-04 用户
  明确要求先开放、稍后实测，注册表已声明 `text + image`；
  `vision_status=user-enabled-live-validation-pending`，
  `vision_context_status=unverified`。这不代表图片识别、图文混合或 256K
  图文上下文已通过验收，之前的长上下文与协议结果仍只证明文本路径。
- 质量：没有同口径质量基准，`quality: {}`，不虚构排序分。

## Router 端到端结果

- `protocol-router-local/report.json`、`protocol-router-tail/report.json`：
  两个入口各 13 项通过，包含 Responses 工具调用与工具结果续轮。
  已知不支持的 JSON Object 不计入通过项。
- `json-object-rejection.json`：两个入口对显式 AGX JSON Object 请求返回
  `503 no_eligible_model`，原因明确为 `agx-qwen36-cerebellum-256k:capability`。
  检查前后 AGX task ID 均为 1230，没有误送后端或偷偷更换模型。
- `router-256k-replay/report.json`：经本地 Router 回放同一长请求，
  实际输入 261493、输出 100 tokens，5/5 检索正确，SSE 完整结束。
  此次命中 261489 tokens 缓存，耗时 17.727 秒，不能当作冷请求速度。
- 长请求完整 request ID：`c1516a10f196449c89564950e685d839`。
  路由轨迹确认 `endpoint_id=agx-qwen36-cerebellum-256k`、状态 `succeeded`。
  Router 计数为 261529，连同输出预留 512，总预算 262041，未超过窗口。
- 原生冷请求的预填充约 280.64 tokens/s、该次生成约 7.93 tokens/s；
  长上下文速度与短请求明显不同。

## 发布边界

256K 直连验收通过，静态条目开放显式调用，安全上下文登记为 262144，
自动候选默认为关闭；Router 端到端验收通过后，已使用既有动态端点管理器
单独激活，Redis 持久 revision 为 4。`activation.json` 确认唯一变动端点
为 AGX，当前有效配置 `enabled=true, auto_candidate=true`。
静态默认关闭用于发布验收门槛，不表示当前生产自动候选仍然关闭。
发布基于当前运行镜像叠加本次注册表和三个运行模块的小范围改动，
避免混入其他未提交改动。
不重启 LiteLLM、Redis、Codex Adapter 或其他 GPU 模型。

两个 API 已分别 drain、确认无在途请求、滚动替换，镜像 ID 为：
`sha256:b8bd096c6375ea6fc1eaf9cea2b8ad6a4aefbb4529cae98a7c15b81399317bac`。
最终两个入口健康、`draining=false`，`api.py`、`config.py`、`runtime.py`、
`registry.yaml` 的部署哈希均与本地一致，详见 `final-health.json`。
2026-09-04 经用户单独确认，两个管理台已逐个刷新到同一已验收镜像。
两个管理接口均返回 8 个端点，AGX 健康、上下文 262144、自动候选开启，
动态 revision 为 4；Tailscale 管理首页返回 200，HTTPS 证书校验通过。
此次仅替换管理台，Router API 启动时间及 AGX 模型 PID 均未改变。
没有执行 Git 提交或推送。

Router 复用已有 llama.cpp 直连、slot 健康检查与容量控制。
AGX 单 slot 在 Router 中只登记一路并发；直接访问 AGX 的其他客户端不共享
Router 的信号量，不能把检查空闲的瞬时结果视为全局原子租约。

默认自动路由输入上限仍为 196608 tokens，输出上限 65536；本次不放宽全局
限制。显式指定模型和模型总上下文上限需分别验收，并为输出保留空间。
模型目录同样显示 `contextWindow=262144`、`maxInputTokens=196608`、
`maxOutputTokens=65536`，这是预留完整输出空间的目录预算。
显式调用缩小输出上限到 512 时，上述 261493-token 输入已经真实通过。
原 Router upstream read timeout 为 900 秒，实测 256K 首 token 已超过该值。
因此本次增加有界的端点元数据 `upstream_read_timeout_seconds`，AGX 设置
1800 秒，其他端点继续使用原超时；连接、写入、连接池超时均不改变。
配置加载拒绝超出 1 至 3600 秒的值。

客户端并发租约从固定 900 秒调整为沿用既有 `queue.lock_ttl_seconds`，
当前为 3600 秒，与任务调度租约一致，避免长任务未完成时客户端名额过期。
正常请求完成仍主动释放；异常遗留租约的最长等待时间随任务租约变化。
这些变更分别位于 `api.py`、`config.py`、`runtime.py`，均有隔离单测。

## 图像先行开放

2026-09-04 按用户明确要求，图像能力先开放、实测后补。本次仅叠加注册表，
不修改模型服务、量化、KV cache 或上下文启动参数。两个 API 和两个管理台
均已滚动更新，返回 `text + image`、`supportsImages=true`，动态自动候选
继续开启。视觉状态保留 `user-enabled-live-validation-pending`，
图文长上下文状态为 `unverified`，没有发送图片推理测试。

当前镜像：
`sha256:161535cacae89812b624ec503cb9e0c785cc4f73a51594ab55c0541cb20367ba`。
发布证据：
`/home/ai/.local/state/ai-router-acceptance/20260904-agx-vision-enabled/deployment.json`。
AGX 模型 PID 仍为 3040048，启动时间仍为 2026-09-04 18:43:31 CST。

## TPS 对照诊断

以下均为同一 Cerebellum GGUF 的后端解码速度，不把 prefill 或 HTTP
墙钟混算成生成 TPS：

| 时间（CST） | 配置窗口 | 实际输入 tokens | 输出 tokens | 解码 TPS |
| --- | --- | --- | --- | --- |
| 18:12:01 | 131072 | 42114 | 649 | 23.11 |
| 18:51:44 | 262144 | 130431 | 101 | 13.05 |
| 19:08:07 | 262144 | 261493 | 100 | 7.93 |
| 19:11:54 | 262144 | 261493，缓存命中 | 100 | 7.93 |
| 19:37:13 | 262144 | 42345，新对照 | 59 | 23.27 |

新 42K 对照保持当前 256K 配置不变，模型、GPU offload、Q8 KV、batch 和
并发参数均未调整。预填充为 94.616 秒，解码为 2.536 秒；相近历史长度的
速度恢复到此前约 23 TPS。因此当前主要差异是实际参与解码的历史从约 42K
增长到约 261K，不能把不同长度的吞吐比较解释为服务整体退化。

256K 缓存回放的 task 636 恢复到 `n_past=261489`，只预填充 4 tokens，
耗时 194.16 毫秒；但生成 100 tokens 仍耗时 12.612 秒。
缓存确实避免了整段重算，并没有消除长历史下逐 token 解码的成本。
扩容后的短请求日志仍约 36 至 38 TPS。

现场检查仍为 MAXN，GPU 时钟 1300.5 MHz、EMC 3199 MHz；42K 对照预填充
时 GPU 温度约 56 摄氏度，没有观察到当前降频迹象。系统已有约 6.5 GiB
swap 占用，但采样中没有持续大量换入换出，不应仅凭占用量断言换页风暴。

正确性边界：此次 42K 对照错误地把五个目标值均回答为 `archived`，
因此 `passed=false`，没有修改标签或算作检索验收通过。
报告及完整请求、响应保存在：
`/home/ai/.local/state/ai-router-acceptance/20260904-agx-tps-diagnosis/context-42k/`。
该失败需作为独立质量问题保留，不影响其真实计时数据。

MTP 只读检查：当前二进制提供 `--spec-type mtp`；但本 GGUF 只有
`blk.0` 至 `blk.39`，无 NextN/MTP 元数据和对应权重张量。
扩容前后的启动日志均显示未启用 speculative decoding，故此次不是
MTP 被关闭导致的速度下降。本次没有启用 MTP、替换模型或调整功耗。

## 回退

经授权停止新请求并等待 slot 空闲后，将新增的 `256k.conf` 移出 systemd
drop-in 目录，执行 daemon-reload，再仅重启 `agx-cerebellum.service`，
即可恢复原始 128K unit。随后复验 `/slots` 和真实短文本。

Router 发布基线镜像为
`sha256:9ed3271bb6bfb7cad0320257636cdbb7aab8a341711e18a72766ae4fa00dafc9`，
本地保留标签 `1panel-ai-router:pre-agx-20260904-184218`。回退时逐 API
实例 drain、等待请求完成、恢复原镜像；先通过动态端点管理器关闭 AGX
自动候选，避免未来恢复注册表时遗留开关意外生效。不清理历史记录。
