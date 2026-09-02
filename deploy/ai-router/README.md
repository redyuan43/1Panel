# 1Panel AI Router

这是一个位于 1Panel 旁路的独立智能路由服务。它不修改 1Panel 开源版的
Go/Vue 主体，也不依赖企业版 AI Gateway。1Panel 通过 Custom Provider
接入公开 OpenAI 兼容 API，路由决策在外层策略代理完成，LiteLLM 只作为
内部协议适配器。

Agent 与第三方调用示例见
[`docs/api-client-guide.md`](docs/api-client-guide.md)。
加密训练对话归档与导出见
[`docs/training-archive.md`](docs/training-archive.md)。

## 当前能力

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `GET /v1/models`
- `GET /api/dashboard`：控制面运行总览、节点、物理 worker、请求记录和云端预算
- `GET/POST/PATCH /api/clients`：客户端账号、独立限额、模型权限和多 Key 管理
- `model=auto` 的能力、上下文、健康、质量、负载和层级筛选
- 显式模型严格匹配，不静默换成其他模型
- 会话亲和、同会话并发 `409`、每个逻辑部署一路并发
- 新请求容量满时立即尝试下一候选；会话亲和最多等待 3 秒
- 本地容量全部占满时按预算策略进入云端，否则返回 `429`
- 单次可控故障回退；流式请求和工具请求不自动重放
- 同模型换 worker 可携带完整历史；跨模型迁移必须生成加密迁移胶囊
- 独立设置页与 JSONL 审计日志
- 自动刷新的运维控制台，展示运行中请求、路由结果、告警和最近流量
- Redis 持久会话状态；LiteLLM 和 Redis 不暴露宿主端口
- 独立 SQLite 训练归档；完整对话压缩并加密后永久保存
- Codex Pro 订阅通过独立 OAuth 适配器接入，凭据不与桌面 Codex 共用
- 图片和音频二进制数据不按 Base64 文本计入 TPM；媒体使用独立保守 Token
  估算，请求体大小由 32 MiB 上限单独保护

## 客户端账号与 API Key

控制台的“客户端账号”页用于给 WorkBuddy、HA、Checkboard、AI Agent 和第三方
服务分配独立服务账号。每个账号独立设置允许模型、RPM、TPM 和最大并发，避免
不同业务共用 `client_id=1panel` 后互相占用限额。

一个账号可以同时拥有多把 Key。账号限额由这些 Key 共享，但每把 Key 可独立
记录标签、最后使用时间和撤销状态，从而支持无停机轮换。新 Key 明文只在创建
响应中显示一次；列表、日志和审计只显示脱敏提示。

账号和 Key 元数据保存在 Redis AOF。Key 使用由
`AI_ROUTER_STATE_KEY` 派生的 HMAC-SHA256 摘要索引，Redis 中不保存可恢复
明文。账号停用、模型权限修改和 Key 撤销由 local/tail Router 共享读取，对
后续请求立即生效；已经开始的模型请求允许正常结束。

`config/defaults.yaml` 中的 `clients.policies` 继续作为首次启动种子。对应环境
变量 Key 会幂等导入为 `legacy_env` Key，保持原调用方可用；迁移到管理台生成
的新 Key 后，可撤销旧 Key。撤销记录会持久保留，容器重启不会重新激活。

控制面接口均使用 `AI_ROUTER_ADMIN_KEY`：

```text
GET   /api/clients
POST  /api/clients
PATCH /api/clients/{client_id}
POST  /api/clients/{client_id}/keys
POST  /api/clients/{client_id}/keys/{key_id}/revoke
```

不提供账号硬删除。停用账号会拒绝新请求，同时保留历史请求和审计关联。

## 多轮上下文与物理缓存

会话 ID 按以下优先级识别：

1. `X-1Panel-Conversation-ID`
2. `x-litellm-session-id`
3. Responses API 的 `conversation`
4. 已记录的 `previous_response_id`
5. Chat 完整历史的稳定前缀哈希

没有显式稳定 ID 的请求仍可执行。Router 会根据 Chat 历史或 Responses
关联生成/恢复推断 ID，并返回
`X-1Panel-Conversation-Mode: inferred`；需要可靠缓存亲和的客户端仍应主动
传稳定会话 ID。

AI 主机的 `18103` 是逻辑模型池。路由容器使用 host network，
Chat Completions 会根据健康信息选择具体
worker 端口，并把会话固定到 `worker_id`。每个物理 worker 独立保持一路并发，
当前 7 张 GPU 组成六个 worker：4 个单 P40、1 个 V100 32GB，以及 1 个
V100 16GB + P40 混合 worker。池最多可并行处理六个请求，同一 worker
不会超过一路并发。
Responses API 暂时通过 LiteLLM 访问逻辑池，因为物理 worker 的 Responses
POST 能力尚未完成真实请求验证，响应会标记为逻辑池亲和。

Edge、Ivan 和 AMD 当前各视为一个物理部署。跨物理部署不等于独占 GPU，
路由器只保存亲和关系和排队优先级。

会话亲和是面向 KV/prompt cache 的硬件软锁：同一会话会在 5 分钟至 24 小时内
固定到同一物理 deployment，生产默认 24 小时，每次成功续轮都会刷新租约。软锁
不会让 GPU 在空闲时被某个会话独占，其他会话仍可使用该硬件。没有显式会话
头的 Chat 客户端会通过完整历史前缀自动恢复会话 ID，因此 WorkBuddy 等只会
重发 messages 的客户端也能回到原 worker。

llama.cpp 后端使用按 MiB 限额的主机内存 prompt cache 保存被新任务换出的
KV，容量满后按 LRU 淘汰，不按亲和 TTL 无限占用显存。控制台记录实际
`cached_prompt_tokens` 和命中率；只有指标证明 RAM 层不足时，才评估
`--slot-save-path` 或 vLLM/LMCache 的 NVMe 二级缓存。

## 容量分流

容量繁忙和模型故障使用两条独立路径：

- 新的 `auto` 请求不在首选模型前长期排队，容量锁不可用时立即尝试下一个
  合格端点。
- 已绑定会话最多等待原 deployment 3 秒，以优先保留 KV cache；超时后先
  尝试同模型其他 worker，再按模型候选顺序迁移。
- 显式模型只能在同模型 deployment 间分配，不能跨模型静默替换。
- 容量繁忙不会写入健康 cooldown，也不消耗网络故障重试次数。
- 所有本地候选均忙时，`cloud_or_429` 会在云端开关、自动升级和预算均允许
  时使用云端；否则返回 `429 all_local_capacity_busy` 和 `Retry-After: 1`。

相关运行参数位于 `routing`：

```yaml
routing:
  provider_priority: local_first
  affinity_capacity_wait_seconds: 3
  new_request_capacity_wait_seconds: 0
  all_local_busy_policy: cloud_or_429
```

`provider_priority` 可在控制台热更新：

- `local_first`：优先使用全部兼容本地容量，本地全满后才考虑云端。
- `balanced`：本地与云端按质量、负载、延迟、上下文、成本和本机性评分。
- `cloud_first`：云端可用时优先，关闭或不可用时自动回到本地。

云端关闭时，三种模式都只选择本地端点。

## Router 重启与租约

两个 API 实例使用固定 ID `router-api-local` 和 `router-api-tail`，每次启动
生成新的 boot ID。容量、会话锁、客户端并发和队列成员均带有实例与 boot
归属。

某个 API 实例重启时，只清理该实例上一次运行留下的租约，不会删除另一个
实例仍在使用的容量。被中断的请求记录为
`request_interrupted_by_restart`，客户端应使用原会话 ID 重试；Router
不会把未完成的助手输出写入会话历史。

被清理租约对应的 deployment 会进入 `draining_old_request` 保护状态。下一次
调度前，Router 绕过健康缓存直接读取具体后端：

- AI 检查对应物理 worker。
- Ivan/AMD 检查 llama.cpp slots。
- Edge 检查 vLLM running/waiting。
- Codex 检查订阅账号 worker。

后端仍忙时不会写入故障 cooldown，也不会接收第二路请求；显示空闲后自动
清除保护标记并恢复候选。该过程只处理容量，不清理 prefix cache、会话亲和
或历史。

计划内重启先调用当前实例的管理接口：

```text
POST /internal/drain
GET  /internal/status
```

接口使用 `AI_ROUTER_ADMIN_KEY`。排空后实例拒绝新的推理请求，但健康和管理
查询保持可用。Uvicorn 与 Compose 的优雅退出上限均配置为 900 秒，覆盖长
上下文推理；重启操作仍需单独确认。

复杂编程和安全分析可被评估器标记为软性的
`subscription-frontier` 偏好。该偏好首先尝试
`codex-pro/gpt-5.6-sol`。GLM 使用 Coding Plan 订阅端点，官方声明支持
原生图像输入、1M 上下文和 128K 最大输出；当前注册能力尚未经过本路由真实
请求验收，因此保持 `auto_candidate=false`，只允许显式模型请求用于验收。
Sol 不可用时立即恢复 `local_first`，全部本地容量也不可用时才继续尝试受
预算保护的 DeepSeek。

## Codex Pro 订阅适配器

适配器仅监听 `127.0.0.1:14010`，生产凭据位于：

```text
/opt/1panel/ai-router/codex-auth/accounts/<alias>/auth.json
```

AI 主机访问 ChatGPT 使用本机 `127.0.0.1:10808` HTTP 代理，该代理只注入
`codex-adapter` 容器，不影响 AI、Edge、Ivan、AMD 等本地/Tailscale 请求。

每个账号使用独立目录、刷新锁和一路并发。首次登录使用独立
`CODEX_HOME`，不要直接挂载正在被桌面 Codex、CLI 或 IDE 使用的
`~/.codex/auth.json`：

```bash
CODEX_HOME="/opt/1panel/ai-router/codex-auth/accounts/primary" \
  codex login --device-auth
```

适配器通过 Codex 模型目录确认账号包含 `gpt-5.6-sol`，并在健康响应中只
公开账号别名、可用状态和冷却时间，不公开 ChatGPT account ID 或任何令牌。
订阅调用不进入 DeepSeek 美元预算账本；HTTP 429 会只冷却对应账号。

对外显式模型 ID 为：

```text
codex-pro/gpt-5.6-sol
```

Responses 请求原生转发；Chat Completions 会转换为 Responses，并保留
工具调用、流式增量、加密 reasoning 状态和稳定的 prompt cache key。
ChatGPT Codex 订阅后端不接受 `previous_response_id`，Router 会用该 ID
恢复本地加密历史并重放完整 Responses input；同一会话仍固定到原账号，并
通过稳定 cache key 复用上游前缀缓存。

## 安全默认值

- API 仅映射到 `127.0.0.1:4000` 和指定 Tailscale IP 的 `4000`
- 设置页仅映射到对应地址的 `4001`
- Redis 与 LiteLLM 仅在 Compose 网络内可见
- Redis 仅绑定宿主回环 `16379`，LiteLLM 仅绑定宿主回环 `14000`
- Git 中只记录密钥环境变量名
- 运行时覆盖保存在 `/opt/1panel/ai-router/settings.yaml`
- 审计日志保存在 `/opt/1panel/ai-router/audit/router.jsonl`
- 会话迁移胶囊使用 `AI_ROUTER_STATE_KEY` 加密
- 客户端 Key 仅保存派生 HMAC 摘要，创建明文响应使用 `Cache-Control: no-store`
- 请求体默认最大 32 MiB，超限返回 `413 payload_too_large`
- 图片默认按每张 1024 tokens 参与上下文和 TPM 估算，Base64 原始字节不会
  被 tokenizer 当作普通文本

## 质量分与 auto

`config/registry.yaml` 只保存已有同口径报告支持的质量分。没有质量报告的
模型按 0 分参与排序，但只要协议、上下文、健康和容量满足，仍可作为
`auto` 候选；缺失质量分降低优先级，不再被误判为没有工具能力。
`auto_candidate: false` 只用于明确的运维禁用。

端点能力使用矩阵记录 Chat、Responses、单/并行工具、`tool_choice`、
JSON Object/Schema、流式和验证时间。Chat 的 `tool_calls/tool_call_id`
与 Responses 的 `function_call/function_call_output/call_id` 会完整保存。
缺失调用 ID 仅在唯一匹配时安全补齐；并行歧义、重复结果和跨轮错误返回
`400 invalid_tool_history`，不会调用任何本地或云端模型。

`benchmarks/cases.yaml` 和 `python -m ai_router.benchmark` 提供确定性评分入口。
基准会触发真实推理，应在部署后单独确认执行。审核报告后，才能把分数写入
相应端点的 `quality` 字段并改变质量排序。

## 双代理随机评测

快速随机评测由主任务编排两个只读子代理：

1. `gpt-5.6-luna` 根据随机种子和
   `benchmarks/luna-prompt.md` 生成 20 道题的 `manifest.json`。
2. 主任务使用同一 manifest 串行显式调用 AI、Edge、Ivan、AMD 和云端模型，
   并生成
   `raw-results.json` 和不含模型身份的 `anonymous-results.json`。
3. `gpt-5.6-terra` 根据 `benchmarks/terra-prompt.md` 匿名评分。
4. 主任务校验裁判 JSON 并生成 `routing-recommendation.json`。

运行入口：

```bash
python -m ai_router.pilot validate-manifest \
  --manifest "/opt/1panel/ai-router/benchmarks/runs/<run-id>/manifest.json"

python -m ai_router.pilot run \
  --manifest "/opt/1panel/ai-router/benchmarks/runs/<run-id>/manifest.json" \
  --run-dir "/opt/1panel/ai-router/benchmarks/runs/<run-id>" \
  --cloud-cost-cap-usd 1

python -m ai_router.pilot finalize \
  --run-dir "/opt/1panel/ai-router/benchmarks/runs/<run-id>" \
  --verdict "/opt/1panel/ai-router/benchmarks/runs/<run-id>/terra-verdict.input.json"
```

Pilot 固定输出为 `provisional` 和 `apply_to_production: false`。不得直接覆盖
正式质量报告、修改注册表质量分或改变端点的 `auto_candidate`。

## 配置

版本化配置：

- `config/defaults.yaml`：路由、队列、云端、评估器和客户端默认值
- `config/registry.yaml`：模型端点、能力、上下文和密钥变量名

运行时设置页只覆盖允许修改的策略段，不修改节点注册表。新增 Qwen3-4B、
Qwen3-ASR-0.6B 等边缘模型时，增加独立端点并声明准确模态、任务和安全上下文。
新增云端大模型时还需设置 `cloud: true`，并同时开启：

```yaml
cloud:
  enabled: true
  auto_escalate: false
  monthly_budget: 50
  allowed_providers:
    - openai
  allowed_models:
    - openai/frontier-model
```

云端端点还必须在 `metadata` 中声明 `provider`、
`input_cost_per_million_usd` 和 `output_cost_per_million_usd`。网关按请求的
prompt token 和最大输出预留成本，避免并发请求穿透月度预算。

订阅端点使用 `metadata.billing_mode: subscription`，不进入美元预算账本，
但仍必须显式加入云端允许列表。GLM-5.3-Flash 的 Coding Plan 模板为：

```yaml
cloud:
  enabled: true
  auto_escalate: true
  monthly_budget: 0
  allowed_providers:
    - zhipu-coding
  allowed_models:
    - zhipu/glm-5.3-flash
```

真实 Coding Plan Key 写入 `/opt/1panel/ai-router/router.env`：

```text
AI_ROUTER_GLM_API_KEY=<your-zhipu-coding-plan-key>
```

Coding Plan Key 与普通开放平台 Key 不通用。本模板使用专属 OpenAI 兼容
Base URL `https://open.bigmodel.cn/api/coding/paas/v4`。
验收期间显式指定 `model=zhipu/glm-5.3-flash`。只有完成文本、图像、工具、
流式、Responses 适配和长上下文实测后，才同时启用注册表的
`auto_candidate` 与运行设置的 `cloud.auto_escalate`。

## 1Panel 接入

在 1Panel Custom Provider 中设置：

```text
Base URL: http://127.0.0.1:4000/v1
API Key:  AI_ROUTER_1PANEL_API_KEY 的实际值
Models:   auto 以及 registry.yaml 中需要公开的模型 ID
```

当前 1Panel Custom Provider 不会自动为每个对话注入动态会话头。直接从该
Provider 发出的请求如果没有 Responses `conversation`，会按无状态请求处理。
需要缓存亲和的调用方必须传稳定会话 ID。

## 准备与启动

以下操作会构建镜像、创建持久目录并启动服务，执行前应单独确认：

```bash
cd "/home/ai/github/1Panel/deploy/ai-router"
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
sudo mkdir -p "/opt/1panel/ai-router/audit" "/opt/1panel/ai-router/redis"
sudo chown -R "10001:10001" "/opt/1panel/ai-router"
sudo "./scripts/install-tail-control-tls.sh"
sudo "./scripts/install-training-archive.sh"
docker compose config
docker compose build
docker compose up -d
```

真实密钥保存在 `/opt/1panel/ai-router/router.env`，tokenizer 固定挂载自
`/opt/1panel/ai-router/tokenizer`。生产请求不使用字符数估算；tokenizer
缺失时会关闭路由并返回明确错误。

## 验证边界

本目录实现和本地单元测试不代表生产部署完成。正式启用前仍需分别确认：

- Docker 镜像可构建
- 容器可解析 Edge/Ivan/AMD Tailscale DNS
- 四个本地后端和云端后端的真实 Chat Completions
- 物理 worker 的 Responses API 能力
- tokenizer 与各模型模板一致
- 确定性质量基准和安全上下文长请求
- 容量满载时的跨端点分流与云端预算兜底
- Luna/Terra 随机 pilot 的真实运行及人工审核
- 1Panel Custom Provider 的真实端到端调用
