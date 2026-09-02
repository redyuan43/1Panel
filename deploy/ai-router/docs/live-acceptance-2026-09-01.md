# 智能路由真实验收报告

日期：2026-09-01

## 覆盖范围

- AI：`huihui/Qwen3.8-27B-Q4-DFlash2`
- Edge：`RadixArk/Qwen3.8-Flash-Next-NVFP4`
- Ivan：`huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF`
- AMD：`Qwen/Qwen3.8-Flash-Next-ROCmFP4-FAST-imatrix-MTP`
- Cloud：`deepseek/deepseek-v4-flash`
- Qwen3-ASR-0.6B：用户明确要求本轮排除

## 真实推理

| 场景 | 结果 | 路由证据 |
| --- | --- | --- |
| AI 显式模型 | 通过 | `node=ai`，返回 `AI正常` |
| AI 两轮会话 | 通过 | 同一 `v10032` worker，第二轮 `cached_tokens=50` |
| AI 重启后两轮会话 | 通过 | 同一 `v10032` worker，第二轮亲和 `hit`、`cached_tokens=27` |
| AI 18111 恢复测试 | 通过 | 双卡 worker 直接返回 `WORKER_18111_OK` |
| Edge 显式模型 | 通过 | `node=edge`，返回 `ROUTER_EDGE_EXPLICIT_OK` |
| Ivan 直连 | 通过 | `18104` 返回 `IVAN-READY` |
| AMD 128K 直连 | 通过 | `18106` 返回 `AMD_128K_DIRECT_OK` |
| AMD 128K 显式模型 | 通过 | 原错误标签为 `node=ivan`，返回 `AMD_ROUTER_STAGE_ONE_OK` |
| AMD 128K 两轮会话 | 通过 | 同一 endpoint，第二轮亲和 `hit`、`cached_tokens=6030` |
| DeepSeek 显式模型 | 通过 | `node=cloud`，返回 `CLOUD_TOPLEVEL_OK` |
| auto general | 通过 | 按质量分选择 Edge |
| auto Edge 切换后复验 | 通过 | `reason=quality_score`，返回 `EDGE_AUTO_DEFAULT_OK` |
| auto code | 通过 | 按 code 分数选择 Edge |
| auto long-context | 拒绝 | 无专项质量分时返回 `503 no_eligible_model` |
| Ivan 端点恢复 | 通过 | `ivan-ms-7b17:18104` 已恢复并确认为独立设备 |

## 质量基准

| 模型 | general | code | batch |
| --- | ---: | ---: | ---: |
| AI Qwen3.8 27B | 50 | 0 | 0 |
| Edge Qwen3.8 Flash Next | 100 | 100 | 100 |
| Ivan Qwen3.8 Flash Next UD-Q4_K_XL | 100 | 0 | 0 |
| AMD ROCmFP4 128K | 未评测 | 未评测 | 未评测 |
| DeepSeek V4 Flash | 100 | 100 | 100 |

原始报告位于 `/opt/1panel/ai-router/benchmarks/`。

## AI 物理池

AI 节点共有 7 张 GPU、6 个物理 worker：

| 端口 | GPU | KV | 安全上下文 | 2026-09-01 状态 |
| --- | --- | --- | ---: | --- |
| 18110 | V100 PCIe 32GB | F16/F16 | 196608 | 通过 |
| 18111 | V100 SXM2 16GB + P40 | Q8_0/Q8_0 | 262144 | 重启恢复后通过 |
| 18112 | P40 | Q8_0/Q8_0 | 65536 | 通过 |
| 18113 | P40 | Q8_0/Q8_0 | 65536 | 通过 |
| 18114 | P40 | Q8_0/Q8_0 | 65536 | 通过 |
| 18115 | P40 | Q8_0/Q8_0 | 65536 | 通过 |

7 个并发真实请求期间，网关观测到最多 6 个物理容量锁，且单 worker
容量从未超过 1。`18111` 首次推理触发 CUDA unknown error，网关将该请求
重试到 `18110`，客户端最终收到 HTTP 200。

GPU0 在 2026-09-01 05:20:15 已出现 `Xid 79` 和 `GPU has fallen off the bus`，
随后 `Xid 154` 要求 GPU reset。本次测试暴露了潜伏故障，但不是故障发生时间。

经用户确认，SSH AI 于 2026-09-01 13:36 完成整机重启。重启后 7 张 GPU
全部重新枚举，当前启动周期未发现新的 NVIDIA Xid；`18110` 至 `18115`
六个 worker 全部健康，模型池报告 `ready_workers=6`、
`available_workers=6`。`18111` 真实推理返回 HTTP 200 和
`WORKER_18111_OK`，耗时约 3.74 秒。

## 云端保护

- DeepSeek 密钥仅保存在 `/opt/1panel/ai-router/router.env`。
- 云端月预算上限为 5 USD。
- 普通 `auto` 有健康本地候选时不会选择云端。
- 只有显式模型、显式 `cloud-frontier` 层级或本地无候选时才允许进入云端。
- 本轮测试后的保守预算账本约为 0.001691 USD。

## 运维控制台

2026-09-01 15:31，正式控制台完成部署：

- 本地地址：`http://127.0.0.1:4001`
- Tailscale 地址：`http://100.91.42.28:4001`
- 四个 API/控制面容器使用镜像
  `sha256:8ebab09e37e56d752e2aeb3ad12f6f903ab0b7bed4c989423862d6aae2653d4a`
- Redis、LiteLLM 和六个模型 worker 未重启
- 页面展示节点健康、物理 worker、运行中请求、最近请求、路由原因、
  缓存亲和、耗时、流量分布、云端预算和运行告警
- `dashboard-lifecycle-20260901` 在页面中先显示为 `running`，完成后转为
  `succeeded`，实际部署为 V100 32GB worker，HTTP 200，耗时 22.71 秒
- Windows WorkBuddy 从正式地址访问页面成功，并通过 `model=auto`
  路由到 Ivan，返回 `WORKBUDDY_DASHBOARD_OK`，耗时 3.56 秒
- 桌面 1440px 和手机 390px 浏览器验收通过
- 自动化与单元测试共 22 项，全部通过

## Edge 生产模型切换

经用户确认，2026-09-01 15:43 至 15:59 完成 Edge 生产模型切换：

- 停止并禁用 `qwen38-nvfp4-vllm.service`，旧端口 `18400` 已关闭。
- 启用并启动 `qwen38-flash-next-vllm.service`，容器
  `qwen38-flash-next` 保持 `running`，重启次数为 0。
- 正式端口恢复为 `18300`，公开模型为
  `RadixArk/Qwen3.8-Flash-Next-NVFP4`。
- `/health` 返回 HTTP 200，`/v1/models` 返回
  `max_model_len=500000`。
- 后端报告 GPU KV cache 容量为 529940 tokens，单个 500000-token 请求
  的理论最大并发约为 1.06；路由器仍按单路并发管理 Edge。
- 直连真实推理返回 `EDGE_FLASH_DEFAULT_OK`。
- 网关显式模型请求返回 `ROUTER_EDGE_EXPLICIT_OK`，路由头为
  `node=edge`、`deployment=edge-qwen38-flash`。
- 网关 `model=auto` 请求返回 `EDGE_AUTO_DEFAULT_OK`，路由原因为
  `quality_score`，确认该模型已成为当前默认本地选择。
- 控制台报告 4/4 端点健康、6/6 worker 就绪、无告警，并记录上述两次
  Edge 请求为 HTTP 200。

500000 是当前服务启动配置和 KV cache 容量验证值。模型原始配置为
262144，并通过 YaRN 参数扩展；在完成真实满长度请求前，路由注册表继续将
262144 作为已验证安全上下文，避免把配置值误报为稳定实测值。

## AMD 128K 接入

2026-09-01 16:14，AMD 全 GPU 服务完成正式接入：

- Tailscale 主机为 `ivan-superai.taild500c8.ts.net`，地址
  `100.90.114.26`；SSH 别名为 `AMD`。
- 正式 API 为 `http://ivan-superai.taild500c8.ts.net:18106/v1`，
  `/health` 返回 HTTP 200。
- 模型为 `Qwen/Qwen3.8-Flash-Next-ROCmFP4-FAST-imatrix-MTP`，
  上下文 131072、MTP 3、KV Q8、路由并发 1。
- 约 119954-token 的冷轮与缓存轮已通过；缓存轮 prompt 阶段由
  584.79 秒降至 0.241 秒，decode 约 21.66 tok/s。
- `ivan-ms-7b17:18104` 属于另一台 Ivan 设备；两者可以作为两路独立物理
  容量调度。
- 服务单元 `qwen38-flash-next-amd-rocmfpx-128k.service` 已从
  `active/disabled` 调整为 `active/enabled`；用户 `Linger=yes`，
  unit 已挂入 `default.target`，具备无人登录开机自启条件。
- Router 模型列表已公开该模型。由于没有同口径质量基准，
  `auto_candidate=false`，当前只允许显式模型调用。
- 网关第一轮 6022 prompt tokens，返回 `AMD_ROUTER_STAGE_ONE_OK`；
  第二轮当时固定到错误命名的 `ivan-qwen38-rocmfpx-128k`，路由亲和为 `hit`，
  6050 prompt tokens 中 6030 tokens 命中缓存，返回
  `AMD_ROUTER_STAGE_TWO_OK`。
- `model=auto` 复验仍选择 Edge，返回 `AUTO_AFTER_AMD_OK`。
- 控制台报告 4/4 端点健康、6/6 AI 物理 worker 就绪、无告警。
- LiteLLM 与四个 Router 容器使用新镜像
  `sha256:58b538072a291279161ad4ca5bb2146bf0da578229ac7202c8fb01566e21d33e`，
  重启次数均为 0；Redis 和所有模型进程未重启。

## 并发分流与设备拆分终验

2026-09-01 17:07 至 17:24 完成容量分流部署和真实并发终验：

- 最终镜像为
  `sha256:65348546368f7d17a902be17ea5855cf7960c2ef0a55bed02d993343e6be7d8b`。
  LiteLLM 与四个 Router/控制台容器均使用该镜像，重启次数为 0；Redis
  容器未重建。
- 注册表纠正为 5 个端点：AI、Edge、Ivan、AMD 和 DeepSeek Cloud。
  `ivan-ms-7b17:18104` 与 `ivan-superai:18106` 是两台独立设备，不再共用
  `ivan` 节点身份。
- Ivan 显式请求通过 Router 返回 `ROUTER-IVAN-READY`，路由头为
  `node=ivan`、`deployment=ivan-qwen38-flash-128k`。
- AMD 显式请求初次因生产环境缺少新变量
  `AI_ROUTER_AMD_BACKEND_KEY` 返回 500；补齐非敏感占位值并只重建
  LiteLLM 后，复测返回 `ROUTER-AMD-READY`，路由头为 `node=amd`、
  `deployment=amd-qwen38-rocmfpx-128k`。该 500 已修复，不是残留故障。
- 8 路真实 `model=auto` 并发全部返回 200，分布严格为 Edge 1 路、
  AI 6 个不同物理 worker、DeepSeek 1 路。AI 请求记录
  `capacity_spillover`，云端请求记录 `cloud_capacity_fallback`；容量等待
  约 1.41 至 4.97 毫秒，没有进入 120 秒队列。
- 7 路显式 AI 并发中，6 路分别占用 6 个不同 worker，第 7 路约
  3.23 秒返回 `429 model_capacity_busy` 和 `Retry-After: 1`，没有跨模型
  替换。
- Edge、Ivan 和 AMD 的单路容量边界均通过。占用请求运行时，第二条显式
  同模型请求约 3.08 至 3.09 秒返回 `429 model_capacity_busy`，没有静默
  改投其他节点。
- 会话亲和实测中，首轮固定到 Edge；Edge 被占用后，续轮等待
  3053.99 毫秒迁移到 DeepSeek，记录 `affinity=migrated`、
  `capacity_attempts=2`，并从完整 Chat 历史准确返回首轮随机口令。
- 终验后 Redis 中 deployment capacity、client parallel、queue 和 cooldown
  键均为 0。控制台为 5/5 端点健康、6/6 AI worker 就绪、活动请求 0、
  无告警。
- AI 当前启动周期未发现 NVIDIA Xid、OOM 或 GPU reset；Edge、Ivan、AMD
  服务日志未发现 OOM、GPU fault、reset、Traceback 或 fatal。Ivan 仅出现
  正常的 prompt-cache 淘汰提示。
- 单元测试共 32 项，全部通过；Python 编译、前端语法、Compose 配置和
  `git diff --check` 均通过。

Ivan 当前只完成短请求和单路并发验证，131072 是服务配置上限，不等于已经
完成 128K 长输入实测。AMD 的约 120K 冷轮和缓存轮证据仍有效。Ivan 与 AMD
都保持 `auto_candidate=false`，等待 Luna/Terra 五模型随机 pilot 后再决定
是否进入生产自动路由。

## 24 小时亲和与 Prefix Cache 终验

2026-09-01 17:56 至 18:05 完成会话亲和和本地缓存终验：

- 生产 `affinity.ttl_seconds=86400`，即 24 小时；允许范围为 5 分钟至
  24 小时。每次成功续轮刷新租约。
- 亲和是软锁，只保存会话到 deployment/worker 的映射，不独占 GPU。测试中
  在 A 会话两轮之间插入独立 B 会话，B 均能正常使用同一硬件。
- 没有显式会话头的 Chat 请求通过完整历史自动恢复 conversation ID。四个
  A2 请求均恢复 A1 的 conversation ID，亲和状态为 `hit`。
- AI A2 精确返回 A1 使用的
  `v10032-GPU-008619db-68ab-5b8f-bf82-8fcd838af68b` 物理 worker，
  证明 6-worker 池按 worker 维度保持缓存亲和。
- Edge 使用 vLLM APC；Ivan、AMD、AI 使用 llama.cpp prompt cache。它们
  不共用 `PREFIX_CACHE=1` 环境变量，但都支持等价的 token-prefix KV 复用。

| 节点 | A1 prompt/耗时 | B 后 A2 prompt/耗时 | A2 缓存命中 |
| --- | ---: | ---: | ---: |
| Edge | 8562 / 9.03 秒 | 8587 / 2.03 秒 | 6400，74.845% |
| Ivan | 6245 / 76.01 秒 | 6276 / 2.16 秒 | 6221，99.696% |
| AMD | 6244 / 23.22 秒 | 6274 / 1.33 秒 | 6204，99.455% |
| AI | 6244 / 9.89 秒 | 6274 / 1.39 秒 | 6218，99.679% |

Edge 当前 vLLM 不在单次 OpenAI `usage` 中返回 cached token 数，但
Prometheus 暴露 `prefix_cache_queries_total` 和
`prefix_cache_hits_total`。Router 已在持有 Edge 单路容量锁期间读取请求
前后 counter 差值，并将本次命中数写入审计和控制台。复验 A2 的审计记录为
`cached_prompt_tokens=6400`、`cache_hit_ratio=0.74845`；后端同时报告
`enable_prefix_caching=True`。

最终生产镜像为
`sha256:37505ffd59daf1f9263f8e16a9b731ff1c205ab69a939605973d6c10df3a96c9`。
LiteLLM 与四个 Router/控制台容器均使用该镜像，重启次数为 0；Redis 和
AI、Edge、Ivan、AMD 模型服务未重启。单元测试 38 项全部通过，Python
编译、前端语法和 Compose 配置检查通过。

终验后 5/5 端点健康、AI 6/6 worker 可用、活动 Router 请求为 0；
capacity、client request、conversation lock 和 cooldown 键均为 0。
AI、Edge 和 AMD 的系统日志未发现本轮新增 OOM、GPU fault 或 reset；
Ivan GPU 状态正常。检查期间 Ivan 出现了不属于本轮测试的外部直连请求，
其 prompt 约 2140 tokens、`max_tokens=260`；首条任务自然结束后又出现新任务。
Router 的活动请求和容量锁始终为 0，因此这些任务不属于本轮 Router 测试，
也未被中断。

## 本地能力对齐与 WorkBuddy 工具链终验

2026-09-01 22:04 至 22:39 完成协议层、路由优先级和 WorkBuddy 修复部署：

- AI、Edge、Ivan、AMD 与 DeepSeek 使用同一份能力矩阵。四个本地端点均
  标记为 Chat、原生 Responses、并行工具、`tool_choice`、JSON
  Object/Schema 和流式已现场验证，且全部 `auto_candidate=true`。
- 缺失质量分按 0 分参与排序，只降低优先级，不再取消协议合格端点的
  `auto` 资格。
- 生产 `routing.provider_priority=local_first`；权重为质量 0.50、负载
  0.20、延迟 0.10、上下文 0.10、成本 0.05、本机性 0.05。控制台可热更新
  `local_first|balanced|cloud_first`。
- 当前云端开关为关闭，`auto_escalate=true` 和月预算 5 USD 保持不变。
  本轮没有发起任何新的 DeepSeek 推理调用。
- 所有本地模型由 Router 直接调用真实 API；AI Responses 继续绑定具体物理
  worker。LiteLLM 保留给云端和内部任务，避免其改写本地 Responses 的
  reasoning 与结构化输出。
- 本地 Responses 同时保留标准 `text.format` 并镜像后端兼容的
  `response_format`。四台设备在故意要求忽略 schema 时，Chat 与 Responses
  仍均被约束为 `{"status":"ok"}`。
- Chat 的 `tool_calls/tool_call_id` 与 Responses 的
  `function_call/function_call_output/call_id` 已纳入历史、流式累加、哈希
  和迁移胶囊。缺失 ID 只在函数名唯一或全局仅一个未消费调用时补齐。
- 两个同名并行调用缺失 ID 时，正式入口返回
  `400 invalid_tool_history`，错误体只包含位置、候选数和稳定原因码，未调用
  本地或云端模型。
- 两个失败 WorkBuddy 会话
  `inferred-701d94c10a194daca9e145842f26df5c` 与
  `inferred-47c8161b90a146198b9c0b7990e8d848` 的旧云端亲和状态已定向删除，
  共删除 2 个 Redis key，没有清理其他会话。

真实协议矩阵覆盖四台设备的 Chat/Responses 非流式与流式、单工具、并行
工具、强制 `tool_choice`、工具结果续轮、JSON Object 和 JSON Schema。
最终矩阵全部通过。生产 Router 的专项结果如下：

| 场景 | 结果 | 路由证据 |
| --- | --- | --- |
| 四个本地显式 Chat 工具调用 | 全部通过 | `node=ai/edge/ivan/amd` |
| WorkBuddy 缺失 ID 唯一修复 | 通过 | Edge，`repaired=1`，返回 `WORKBUDDY_LOCAL_OK` |
| WorkBuddy 并行歧义 | 通过 | Router 直接返回 400 |
| auto Responses JSON Schema | 通过 | Edge 原生 Responses，纯 JSON |
| AI Responses 工具续轮 | 通过 | 同一 V100 32GB worker，`repaired=1` |
| Windows WorkBuddy 配置实测 | 通过 | `ivan-laptop` 返回 `IVAN_WORKBUDDY_LOCAL_OK` |

占用型 8 路 `model=auto` 并发全部返回 200，云端请求为 0，容量等待最大
4.69 ms。分布为 Edge 1、Ivan 1、AMD 1、AI 5。AI 未取得第 6 路不是调度
回归：`18111` 已因 2026-09-01 20:42:03 的新硬件故障不可用，内核记录
`Xid 79 GPU has fallen off the bus` 和 `Xid 154 GPU Reset Required`。
Router 检测到 AI 仅 5/6 worker 后立即溢出到 AMD。未获得授权，本轮没有
重置 GPU、重启模型池或重启主机。

最终生产镜像为
`sha256:54b7cf720ca616a2140cf9c6e53cfcb39b3757de8b06e036713a18b35735b962`。
LiteLLM 与四个 Router/控制台容器均使用该镜像，重启次数为 0；Redis 和
AI、Edge、Ivan、AMD 模型服务未重启。单元与协议 fixture 共 61 项全部
通过，Python 编译、前端语法、Compose 配置和 `git diff --check` 通过。

终验时 5/5 注册端点健康、AI 5/6 worker 可用、活动请求 0；deployment
capacity、client parallel、conversation lock 和 cooldown 键均为 0。
Edge、Ivan、AMD 健康检查均返回 200；Router/LiteLLM 日志未发现新增
Traceback、HTTP 500 或 OOM。

## 未完成

- 1M DeepSeek、500K Edge、131K Ivan/AMD 和 262K AI 的边界上下文尚未全部做满长度测试。
- 本机没有运行中的 1Panel 服务，尚未注册实际 Custom Provider。
- Ivan 与 AMD 尚未完成本轮同口径随机质量基准；当前可以参与 `auto`，
  但缺失任务分会降低排序。
- 约 106K–116K 的完整 WorkBuddy 大工具上下文尚未在四台设备重新统一跑完。
- DeepSeek 真实兜底调用仍等待单独的生产调用确认。
- AI `18111` 的 V100/P40 worker 需要单独确认后执行 GPU/主机恢复。

## AGX Check Boards 自动路由接入

2026-09-01 完成 AGX `/data/github/check_boards` 的文本推理迁移：

- 新增独立客户端策略 `check-boards`，只允许调用 `model=auto`，限额为
  240 RPM、200 万 TPM、最多 4 路并发。专用 Key 分别保存在 AI 与 AGX 的
  `0600` 环境文件中，没有写入仓库、日志或前端。
- 通用 LLM、会话总结、任务发现、会话分诊和 Task Assistant 均改为
  `http://ai-X10DRG.taild500c8.ts.net:4000/v1/chat/completions` 与
  `model=auto`。TTS、Cardputer bridge、设备动作和远程会话采集 SSH 保持
  原链路。
- 非流式和流式客户端共用同一认证/亲和 helper；Task Assistant 会把原始
  会话 ID 哈希为 `check-boards:task-assistant:<digest>`，不暴露浏览器会话
  ID，并可复用 Router 的 24 小时 deployment 亲和。
- 旧的 `18105`、`18107`、Ivan `18104` 与旧模型组合会在启动时迁移到
  Router `auto`。AGX 生产持久设置已验证为 Router URL 与 `auto`。
- Router 单元测试 61 项通过；Check Boards 全量测试 420 项通过，Vite
  生产构建通过。原有三处未提交前端改动均保留。
- AGX 直连 Router 的短请求返回
  `CHECK_BOARDS_ROUTER_OK`，客户端识别为 `check-boards`，实际由 AI
  V100 32GB worker 完成。
- Check Boards Task Assistant 端到端新会话返回
  `CHECK_BOARDS_END_TO_END_OK`，无错误；Router 审计记录
  `status=200`、`client_id=check-boards`、`node=ai`、
  `reason=capacity_spillover`、`capacity_attempts=2`、
  `latency_ms=5722.97`。
- 同一 Task Assistant 会话续问返回 `CHECK_BOARDS_AFFINITY_OK`，使用相同
  哈希会话 ID 和同一 V100 32GB worker，路由记录
  `reason=conversation_affinity`、`affinity=hit`、
  `capacity_attempts=1`、`latency_ms=1098.73`。该 AI 后端未返回本次
  cached-token 数值，但 deployment 亲和已真实命中。
- 本轮两条验收请求均使用本地节点。云端保持关闭，没有新增 DeepSeek 请求。
- Router 本地与 Tailnet API 使用镜像
  `sha256:e846c948b9869f8325e5c10fd7138aa4a069128455c4a1e04ef00f1dbf5119a3`，
  重启次数均为 0；LiteLLM、Redis 与所有 GPU 模型服务未重启。
- `check_boards.service` 为 `active/running`、`NRestarts=0`，健康接口返回
  200。滚动重启期间 Check Boards 入口约 6 秒不可用。

## Codex Pro GPT-5.6 Sol 接入终验

2026-09-02 00:50 至 01:50 完成独立 Codex Subscription 适配器、生产注册、
自动路由和真实协议验收：

- OAuth 凭据独立保存在
  `/opt/1panel/ai-router/codex-auth/accounts/primary/auth.json`，目录权限
  `0700`、文件权限 `0600`，不与 Nano2、桌面 Codex 或 CLI 凭据共享。
- `codex-adapter` 仅监听宿主 `127.0.0.1:14010`，并仅在该容器内使用
  `127.0.0.1:10808` HTTP 代理。健康检查显示 `codex-primary` 可用、一路
  并发、无 cooldown。
- 账号模型目录包含 `gpt-5.6-sol`。目录报告上下文 272000 tokens、最大配置
  上限 872000 tokens；生产首轮安全上下文仍限制为 131072 tokens。
- 公开模型 `codex-pro/gpt-5.6-sol` 已启用
  `auto_candidate=true`，tier 为 `subscription-frontier`、rank 50。
  DeepSeek 保留为 rank 40 的第二远程兜底，订阅调用不进入美元预算账本。
- 普通 `model=auto` 请求真实落到 AI 本地 worker；Git 工具链和复杂多文件
  安全编程请求真实落到 `codex-primary`，路由原因为 `preferred_tier`。
- 复杂编程保底判定覆盖多文件、跨仓库、架构、分布式、安全、威胁建模、
  并发竞态、调试、根因、回滚和迁移信号。Git/代码工具判定现在早于
  long-context 判定，约 100K 的代码工具请求不会再被误归类为普通长上下文。
- WorkBuddy 风格的 `git.status` 工具名由适配器稳定映射为上游合法名称，
  返回客户端时恢复原名；工具调用、结果续轮和同账号亲和均真实通过。
- Chat Completions 非流式与 SSE 流式、Responses 非流式与 SSE 流式、
  JSON Object、单工具和工具结果续轮均真实通过。流式请求均收到完成事件，
  JSON 响应为可解析的 `{"status":"JSON_OK"}`。
- Codex 后端不接受 `previous_response_id`。Router 改为恢复本地加密历史、
  重放完整 Responses input，并保持稳定 prompt cache key；两轮会话已验证
  固定到 `codex-primary`。
- Ivan 注册表恢复为 configured/safe 131072；Sol 注册表为 configured
  272000、safe 131072。滚动更新仅重启 Router 与控制台容器，Codex
  adapter、LiteLLM、Redis 和所有 GPU 模型服务均未重启。

真实路由证据：

| 场景 | 状态 | 节点与结果 |
| --- | --- | --- |
| 显式 Sol Chat | 200 | `codex-primary`，`SOL_ROUTER_OK` |
| 显式 Sol Responses 两轮 | 200 | 同账号亲和，记住 `REPLAY_42` |
| WorkBuddy `git.status` 工具续轮 | 200 | 原工具名恢复，`WORKBUDDY_SOL_OK` |
| 普通 auto | 200 | AI 本地 worker，`AUTO_LOCAL_OK` |
| Git 工具 auto | 200 | `preferred_tier`，`AUTO_SOL_OK` |
| 复杂多文件安全编程 auto | 200 | `preferred_tier`，`COMPLEX_ROUTE_OK` |
| Chat SSE | 200 | 完成事件与 `STREAM_CHAT_OK` |
| Responses SSE | 200 | 完成事件与 `STREAM_RESPONSES_OK` |
| JSON Object | 200 | `{"status":"JSON_OK"}` |

当前已知限制：

- AI 逻辑池内部请求仍可能选中已离线的 `18111` V100 worker，LiteLLM 会返回
  502。主 Router 按物理 worker 健康状态调度，不受该问题影响；复杂编程升级
  已有确定性保底。普通模糊任务的模型分类器仍可能 fail-open 为 general，
  后续应让内部评估器也绑定健康物理 worker。
- 2026-09-02 01:52 只读检查确认 Tailscale 到 AMD 主机约 3 ms，但
  `qwen38-flash-next-amd-rocmfpx-128k.service` 为 `inactive`，因此当前控制台
  为 5/6 端点健康。该 GPU 服务不在本轮允许重启范围内，未执行恢复。
- Sol 的 131K 以上上下文尚未完成独立生产验收，自动路由不会使用目录中的
  272K/872K 配置上限。
- 本轮未修改 Nano2，未重启任何 GPU 服务，也未执行 Git commit 或 push。

## 2026-09-02 多模态接入与 AMD 恢复

本轮按真实图片请求验收视觉能力，不依据模型配置文件或启动参数直接宣称支持。

| 节点 | mmproj/视觉配置 | 真实图片结果 | 生产注册 |
| --- | --- | --- | --- |
| Codex Pro Sol | Codex Responses 原生图像输入 | 通过，返回 `RED_BACKGROUND_WHITE_SQUARE` | `text,image` |
| Ivan Qwen3.8 128K | `mmproj-F16.gguf` + 1024 image tokens | 白方块图命中，纯红图返回 `OTHER` | `text,image` |
| AMD ROCmFP4 128K | `mmproj-F16.gguf` + 1024 image tokens | 白方块图命中，纯红图返回 `OTHER` | `text,image` |
| AI P40/V100 池 | `mmproj-model-bf16.gguf`，projector 暂留 CPU | 小图通过；更高分辨率图片在 P40/V100 均耗尽 mtmd decode workspace | 仅 `text` |
| Edge Flash Next | 模型含 vision config | 失败，图像请求返回 500 后容器退出 | 仅 `text` |
| DeepSeek | API 不支持当前模型的图像输入 | 返回 `This model does not support image` | 仅 `text` |

AMD 服务此前显示 `inactive` 的根因不是模型崩溃、OOM 或 GPU fault。
2026-09-02 01:10:44，`gpu-memory-guard` 在系统内存超过 90% 阈值时，没有
找到增长超过 512 MiB 的进程，随后按兜底规则终止了最大的
`llama-server`。该服务配置为 `Restart=no`，因此正常退出后保持 inactive。
恢复时已完成：

- 在 AMD 启动脚本中校验并传入
  `/home/ivan/model-sources/qwen3.8-flash-next-amd/vision/mmproj-F16.gguf`。
- 将 `llama-server` 加入 `gpu-memory-guard` 保护名单并重启 guard。
- 启动 AMD 服务，确认 `/v1/models` 报告 `completion,multimodal`。
- 使用经过像素校验的 128x128 红底白方块 PNG 进行真实图像推理，结果正确。
- 复查约 98 GiB GPU 显存占用、guard 健康，未发现 OOM、GPU fault 或 reset。

Ivan 原服务已包含同名 mmproj。两台服务均按运行时提示增加
`--image-min-tokens 1024` 并完成重启。正式测试图
`ai-router-vision-red-white-square.png` 的 SHA256 为
`c881eff8d53ea9c604c3ddba3c060e35d18378d251984942141210aece535c3b`，
左上角像素为 `(253,0,0)`、中心像素为 `(255,255,255)`。

最初使用的 `ai-router-vision-red-square.png` 实际是纯红图，不含白方块；
该 fixture 名称与判定条件不一致，基于它产生的“漏检白方块”结论全部作废。
纠正后对 Ivan、AMD 分别以完全相同提示依次发送白方块图和纯红图：前者均返回
`RED_BACKGROUND_WHITE_SQUARE`，后者均返回 `OTHER`，同时验证了图像切换时
prefix cache 没有串用旧视觉状态。Ivan 冷图 prefill 明显慢于 AMD，路由排序
仍应考虑延迟和会话亲和。

最终通过生产 Router `http://127.0.0.1:4000/v1/chat/completions` 复验：

| 请求模型 | HTTP | 实际节点/deployment | 结果 |
| --- | --- | --- | --- |
| Ivan 显式模型 | 200 | `ivan/ivan-qwen38-flash-128k` | `RED_BACKGROUND_WHITE_SQUARE` |
| AMD 显式模型 | 200 | `amd/amd-qwen38-rocmfpx-128k` | `RED_BACKGROUND_WHITE_SQUARE` |
| Sol 显式模型 | 200 | `codex-pro/codex-primary` | `RED_BACKGROUND_WHITE_SQUARE` |
| `auto` | 200 | `ivan/ivan-qwen38-flash-128k` | `RED_BACKGROUND_WHITE_SQUARE` |

生产控制台仅为 Ivan、AMD、Sol 显示图像能力。AI 和 Edge 保持文本；
DeepSeek API 明确拒绝图像。

## WorkBuddy 图像能力发现修复

2026-09-02 检查 `ivan-laptop` 后确认，WorkBuddy 显示
`custom-local:auto` 不支持图片的直接原因是
`C:\Users\Ivan\.workbuddy\models.json` 将 `auto.supportsImages` 写死为
`false`，与 Router 实际视觉能力无关。

- 已备份原配置为
  `models.json.bak-vision-20260902-0910`。
- 已只修改 `auto.supportsImages=true`，保留 URL、API Key、上下文和其他模型
  条目。
- WorkBuddy 已在 console `SessionId=2` 完整重启，重启后 11 个进程正常。
- Router `/v1/models` 新增 `modalities`、`input_modalities`、
  `output_modalities`、`supportsImages`、`supportsToolCall` 和能力摘要。
- Tailnet 实测 `auto` 返回 `input_modalities=["image","text"]`、
  `supportsImages=true`、`capabilities.vision=true`。

Edge 的 vLLM 能识别模型视觉配置并初始化图像 encoder cache，但真实图片请求
返回 HTTP 500，随后 `--rm` 容器以状态 0 退出，端口 `18300` 离线。已启动
原 systemd 服务恢复文本模型；在完成独立运行时修复与图像复验前，Edge 不进入
视觉候选。

## WorkBuddy 大图 TPM 修复

2026-09-02 10:37:38，来自 `ivan-laptop` 的图片请求在模型选择前返回
`429 tpm_limit_exceeded`。Router 日志和 Redis AOF 记录该请求被 tokenizer
估算为 1167343 tokens，超过 `1panel` 客户端的 1000000 TPM。请求当时尚未
进入路由评估，因此不是视觉模型、GPU 容量或云端配额故障。

根因是多模态消息中的 `data:image/...;base64,...` 被完整当作普通文本送入
tokenizer。修复后：

- 图片、音频数据在计数副本中替换为媒体占位符，原始请求体仍完整转发。
- 图片默认按每张 1024 tokens、音频按 4096 tokens 估算。
- 请求体使用独立 32 MiB 上限，超限返回 `413 payload_too_large`。
- AI 小图直连虽然通过，但高分辨率图像在 P40/V100 均出现 mtmd decode
  workspace 不足，因此不加入生产 `auto` 视觉候选。

生产复验使用 1024x1024 红色 BMP，Base64 请求体约 4 MiB：

| 项目 | 结果 |
| --- | --- |
| Request ID | `vision-base64-live-20260902` |
| Router 估算 | 1084 tokens |
| 后端 usage | 1043 prompt tokens |
| 路由节点 | `ai` |
| deployment | `p40-GPU-759c6d34-886e-99d0-c407-a7227d2554ce` |
| HTTP/内容 | `200` / `红色` |
| Router 延迟 | 142107.65 ms |

部署后 AI 模型池保持 `ready_workers=6/6`，请求结束后容量锁全部释放，内核日志
未出现新 Xid、GPU reset 或 OOM。

AI 池已完成最小代码接入：worker 启动时传入 mmproj，并使用
`--no-mmproj-offload` 避免 P40 显存被 projector 进一步占用；同时补充了掉总线
GPU 行的发现容错和服务优雅退出测试。单元测试 13 项通过。但此前 V100
`Xid 79` 留下的不可杀 `llama-server` 线程仍占用旧 systemd cgroup，健康 P40
的新进程均报 CUDA initialization error。该问题需要单独授权执行 NVIDIA
运行时复位或主机重启；恢复并通过真实图片请求前，AI 仍只注册为文本能力。

## 2026-09-02 Router 重启租约保护实现

本轮已完成代码、自动测试和生产 Router 滚动部署。部署镜像为
`sha256:4571d7d5c0646eca724a735b0f2e5cf5ab740536cc81b0c51c48e876c95c4a04`。
真实强制中断注入尚未执行，仍需单独确认。

- `router-api-local` 与 `router-api-tail` 使用固定实例 ID，每次启动生成新
  boot ID。
- 新租约 token 包含 `instance_id:boot_id` 前缀，覆盖物理 deployment、
  conversation lock、客户端并发和队列成员。
- 单实例启动只清理相同实例旧 boot留下的成员，不影响另一个 API 实例。
- 被清理的 deployment 写入共享 `draining_old_request` 标记；后续调度绕过
  健康缓存，实时检查 AI worker、llama.cpp slots、vLLM metrics 或 Codex
  worker。
- 后端仍忙时按容量繁忙处理，不写入故障 cooldown；空闲后自动删除保护标记
  并恢复候选。
- 旧实例状态中的在途请求写入
  `request_interrupted_by_restart`，不持久化不完整的助手输出。
- 新增管理接口 `/internal/drain` 和 `/internal/status`，以及控制台 Router
  实例表。Uvicorn graceful shutdown 和 Compose stop grace分别为 900 秒和
  910 秒。

自动测试覆盖：

- local 重启只清除 local租约，tail租约、会话锁和容量保持有效。
- deployment、客户端并发、会话锁和队列孤儿成员均被精确清理。
- 后端忙时阻止重新调度，空闲后自动恢复并写入对应审计事件。
- 排空实例拒绝新推理，但健康与管理接口继续可用。
- 原有非流式、流式、工具、结构化输出、容量分流和多模态测试全部继续通过。

自动测试共 86 项通过，其中 `test_core.py` 81 项、
`test_codex_adapter.py` 5 项；同时通过 Redis DB 15 隔离环境下的真实成员清理
测试、Python 编译检查、前端 JavaScript 语法检查、Compose 配置检查和
`git diff --check`。

生产部署与实测结果：

| 项目 | 结果 |
| --- | --- |
| local boot ID | `e629d1d0380647939913ce998d4f28f3` |
| tail boot ID | `1feb4b2227574f59ab8a23a59cd4e21b` |
| 两实例启动清理 | deployment/client/queue/conversation 均为 `0` |
| Router/控制台 | 4 个容器均运行新镜像，restart count 为 `0` |
| 模型健康 | endpoint `6/6`，AI worker `6/6`，Ivan/AMD 空闲，Codex 可用 |
| GPU 日志 | 部署后无新 NVRM Xid、GPU fault、reset 或 OOM |

真实显式 Ivan 请求 `restart-lease-ivan-20260902` 执行期间，Redis 容量成员
为
`router-api-local:e629d1d0380647939913ce998d4f28f3:cd37a983fa5849518172deda974ba18f`，
控制台显示 local 活动请求数为 1，Ivan `/slots` 同时显示 processing。请求
HTTP 200 完成后，三者同时恢复为空闲，证明正常响应结束会立即释放容量租约。

滚动部署后还观察到一条真实 Tailnet 请求
`84173c5d536345e4b41e68cc7bfd4f5d`。其租约前缀为
`router-api-tail:1feb4b2227574f59ab8a23a59cd4e21b`；约 143 秒执行期间，
tail 活动请求、Redis deployment 容量和 Ivan 物理 slot 始终一致为忙。HTTP
200 完成后，三层状态同时归零，未留下孤儿成员。

部署过程只滚动更新 Router API 和控制台，没有重启 LiteLLM、Codex adapter、
Redis 或任何 GPU 模型服务。会话亲和与 prefix cache数据未被清理。

仍需单独确认后执行的破坏性验收：

1. 对单个 Router实例调用 `/internal/drain`，验证 900 秒优雅排空。
2. 在真实长请求执行中强制终止一个 Router实例，验证请求被标记为
   `interrupted_by_restart`。
3. 验证旧后端仍忙时进入 `draining_old_request`，且物理后端空闲后自动恢复。
