# AI Router API 调用指南

本文面向 AI Agent、内部服务和经授权的第三方应用，说明如何通过
1Panel AI Router 调用本地与云端模型。

## 1. 接入信息

| 项目 | 值 |
| --- | --- |
| Tailnet Base URL | `http://ai-X10DRG.taild500c8.ts.net:4000/v1` |
| Tailnet IP Base URL | `http://100.91.42.28:4000/v1` |
| AI 主机本地 Base URL | `http://127.0.0.1:4000/v1` |
| 推荐模型 ID | `auto` |
| 认证方式 | `Authorization: Bearer <client-api-key>` |
| Chat API | `POST /v1/chat/completions` |
| Responses API | `POST /v1/responses` |
| 模型列表 | `GET /v1/models` |

跨主机访问必须使用 Tailscale 私网地址。不要把端口 `4000` 暴露到公网。

客户端应使用单独分配的 API Key。API Key 通过环境变量、systemd
`EnvironmentFile` 或其他密钥管理器注入，不得写入源码、Git、日志、URL
或浏览器前端。

管理员在 AI Router 控制台的“客户端账号”页创建服务账号并生成 Key。不同
应用应使用不同账号，而不是仅为同一个 `1panel` 账号生成多把 Key；账号才是
RPM、TPM、最大并发和模型权限的隔离边界。

```bash
export AI_ROUTER_BASE_URL="http://ai-X10DRG.taild500c8.ts.net:4000/v1"
export AI_ROUTER_API_KEY="<client-api-key>"
```

`AI_ROUTER_ADMIN_KEY` 是管理控制面的密钥，不能用于普通模型调用，也不能发给
第三方。

轮换 Key 时，先在同一账号下生成新 Key并更新调用方，确认新 Key工作后再撤销
旧 Key。撤销对后续请求立即生效，但不会中断已经开始的模型推理。创建页面关闭
后无法再次查看完整 Key。

## 2. 自动路由策略

普通 Agent 和第三方应用应使用：

```json
{
  "model": "auto"
}
```

生产默认策略为 `local_first`：

1. 根据协议、工具能力、结构化输出、上下文长度和健康状态筛选兼容模型。
2. 优先选择有可用容量的本地模型。
3. 首选本地模型繁忙时立即尝试其他本地 deployment，不在单个 GPU 前长时间排队。
4. 本地容量全部繁忙时，仅在云端已启用、允许自动升级且预算充足时使用云端。
5. 云端关闭时只使用本地；全部本地繁忙返回
   `429 all_local_capacity_busy`。
6. 没有任何模型满足协议、能力或上下文要求时返回
   `503 no_eligible_model`。

显式指定模型时，Router 不会跨模型静默替换。只有确实要求固定模型、量化或
专项能力时才应显式指定模型 ID。

### Codex Pro Sol

复杂、多文件、架构、调试、安全和并发类编程任务使用 `model=auto` 即可。
Router 会把这类请求标记为软性的 `subscription-frontier` 偏好，优先尝试
`codex-pro/gpt-5.6-sol`；Sol 账号繁忙、冷却或不可用时立即回退兼容本地
模型，之后才考虑受预算保护的 DeepSeek。

只有必须固定使用 Sol、并且可以接受账号繁忙时直接返回 `429` 的调用方，才
显式指定：

```json
{
  "model": "codex-pro/gpt-5.6-sol"
}
```

Sol 的首轮生产安全上下文为 131072 tokens。模型目录报告的配置上下文为
272000 tokens、更高上限为 872000 tokens；超过 131072 的范围在完成独立
长上下文验收前不会用于自动路由。

## 3. 查询模型

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${AI_ROUTER_API_KEY}" \
  "${AI_ROUTER_BASE_URL}/models"
```

模型列表包含 `auto` 和当前允许该客户端访问的显式模型。客户端不要缓存模型
列表作为永久配置，端点健康状态与注册表可能变化。

每个模型条目同时返回 `modalities`、`input_modalities`、
`output_modalities`、`supportsImages` 和能力摘要。`auto` 的模态是当前所有
已启用自动候选的能力并集；实际请求仍会按健康状态、容量和请求模态筛选端点。

## 4. Chat Completions

### curl

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${AI_ROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "X-1Panel-Conversation-ID: agent:demo:conversation-001" \
  "${AI_ROUTER_BASE_URL}/chat/completions" \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "system", "content": "你是一个准确、简洁的助手。"},
      {"role": "user", "content": "用三点说明 prefix cache 的作用。"}
    ],
    "max_tokens": 512,
    "temperature": 0.2
  }'
```

### Python OpenAI SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["AI_ROUTER_BASE_URL"],
    api_key=os.environ["AI_ROUTER_API_KEY"],
    default_headers={
        "X-1Panel-Conversation-ID": "agent:demo:conversation-001",
    },
)

response = client.chat.completions.create(
    model="auto",
    messages=[
        {"role": "system", "content": "你是一个准确、简洁的助手。"},
        {"role": "user", "content": "检查这段 JSON 是否满足给定 schema。"},
    ],
    max_tokens=512,
)

print(response.choices[0].message.content)
```

### JavaScript OpenAI SDK

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: process.env.AI_ROUTER_BASE_URL,
  apiKey: process.env.AI_ROUTER_API_KEY,
  defaultHeaders: {
    "X-1Panel-Conversation-ID": "agent:demo:conversation-001"
  }
});

const response = await client.chat.completions.create({
  model: "auto",
  messages: [
    { role: "system", content: "你是一个准确、简洁的助手。" },
    { role: "user", content: "给出本次任务的执行摘要。" }
  ],
  max_tokens: 512
});

console.log(response.choices[0].message.content);
```

## 5. Responses API

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${AI_ROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "X-1Panel-Conversation-ID: agent:responses:001" \
  "${AI_ROUTER_BASE_URL}/responses" \
  -d '{
    "model": "auto",
    "input": "将下面任务拆成不超过五个可执行步骤。",
    "max_output_tokens": 512
  }'
```

后续轮次应继续传同一个 `X-1Panel-Conversation-ID`。如果客户端使用
`previous_response_id`，Router 也会恢复对应会话关系。

## 6. 图像输入

图像请求继续使用 `model=auto`。Router 会识别请求中的图像模态，只在已完成
真实视觉验收且当前健康的端点之间调度，不会把图片静默发送给文本模型。

截至 2026-09-02，生产视觉能力如下：

| 节点 | 图像路由 | 状态 |
| --- | --- | --- |
| Ivan Qwen3.8 128K | 启用 | mmproj + 1024 image tokens，图像切换实测通过 |
| AMD ROCmFP4 128K | 启用 | mmproj + 1024 image tokens，图像切换实测通过 |
| Codex Pro Sol | 启用 | Codex Responses 图像输入真实通过 |
| AI P40/V100 池 | 未启用 | 小图通过，但高分辨率 mtmd chunk 在 P40/V100 均耗尽 decode workspace |
| Edge Flash Next | 未启用 | 当前 vLLM 图像请求会导致容器退出 |
| DeepSeek | 未启用 | API 明确返回 `This model does not support image` |

Chat Completions 示例：

```json
{
  "model": "auto",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "请描述图片中的主要内容。"},
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/png;base64,<base64-data>"
          }
        }
      ]
    }
  ],
  "max_tokens": 256
}
```

Responses API 使用 `input_image`：

```json
{
  "model": "auto",
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "请描述图片中的主要内容。"},
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,<base64-data>"
        }
      ]
    }
  ],
  "max_output_tokens": 256
}
```

生产调用应控制图片尺寸和数量，并使用稳定的
`X-1Panel-Conversation-ID`。同一对话仍会优先回到原 deployment，但视觉
projector 和 prefix cache 都受后端容量与淘汰策略限制。

Router 不会把 `data:image/...;base64,...` 的原始字节当成普通文本 Token。
生产默认按每张图片 1024 tokens 参与上下文和 TPM 估算，原始图片仍完整转发
给后端。请求体由独立的 32 MiB 上限保护，超过上限返回
`413 payload_too_large`。

## 7. 流式输出

```bash
curl --no-buffer --fail-with-body \
  -H "Authorization: Bearer ${AI_ROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "X-1Panel-Conversation-ID: agent:stream:001" \
  "${AI_ROUTER_BASE_URL}/chat/completions" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "逐步分析这个问题。"}],
    "stream": true,
    "max_tokens": 1024
  }'
```

流式响应一旦开始，不会跨模型自动重放。客户端应区分连接建立前失败与已经收到
部分 token 后失败，避免重复执行工具或产生重复输出。

## 8. 工具调用

Router 保留 Chat API 的 `tool_calls/tool_call_id`，以及 Responses API 的
`function_call/function_call_output/call_id`。

```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "查询北京天气。"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string"}
          },
          "required": ["city"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

执行工具后，必须把模型返回的原始 `tool_call_id` 写回工具结果：

```json
{
  "role": "tool",
  "tool_call_id": "call_123",
  "content": "{\"temperature_c\":26,\"condition\":\"sunny\"}"
}
```

不要自行生成、复用或删除调用 ID。Router 只会在能够唯一匹配时修复缺失 ID；
并行歧义、重复工具结果或跨轮错误会返回 `400 invalid_tool_history`。

## 9. 结构化输出

客户端可以使用 OpenAI 兼容的 `json_object` 或 `json_schema`。Schema 应尽量
小而明确，并在客户端再次执行确定性校验；模型输出不能替代业务输入验证。

```json
{
  "model": "auto",
  "messages": [
    {"role": "user", "content": "返回任务名称和优先级。"}
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "task",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "priority": {"type": "integer", "minimum": 1, "maximum": 5}
        },
        "required": ["name", "priority"],
        "additionalProperties": false
      }
    }
  }
}
```

## 10. 多轮上下文与缓存亲和

需要多轮对话、工具链或长上下文缓存时，客户端必须为同一对话发送稳定且不含
敏感信息的会话 ID：

```text
X-1Panel-Conversation-ID: <application>:<tenant>:<opaque-conversation-id>
```

建议使用随机 UUID 或不可逆内部 ID。不要使用用户姓名、邮箱、手机号、访问
令牌或原始 prompt。

生产亲和 TTL 当前为 24 小时，每次成功续轮会刷新。亲和是软锁：

- 同一会话优先回到原 deployment，以复用 prefix/KV cache。
- 该 deployment 空闲时仍可服务其他会话，不会被一个会话独占。
- 原 deployment 持续繁忙时，Router 最多等待 3 秒后迁移到兼容模型。
- 迁移后可能需要重新 prefill；跨模型且不能直接携带完整历史时才使用压缩胶囊。
- 后端 cache 容量有限并按自身淘汰策略回收，24 小时亲和不保证缓存永不淘汰。

Chat 客户端在每轮请求中应发送完整、合法的消息历史。不要仅靠 Router 替客户端
永久保存业务对话。

不提供会话 ID 时，Router 会为请求生成或根据完整历史恢复推断 ID，并在响应
中返回 `X-1Panel-Conversation-Mode: inferred`。当前没有用于强制无状态模式
的请求头；独立批处理只需省略会话 ID，并且不要把响应中的推断 ID复用于其他
业务任务。

## 11. 路由可观测性

响应包含以下头部，调用方应记录到结构化日志：

| 响应头 | 含义 |
| --- | --- |
| `X-1Panel-Route-Request-ID` | Router 请求 ID |
| `X-1Panel-Route-Node` | 实际执行节点 |
| `X-1Panel-Route-Model` | 实际执行模型 |
| `X-1Panel-Route-Deployment` | 实际物理 deployment/worker |
| `X-1Panel-Route-Reason` | 选择或溢出原因 |
| `X-1Panel-Prompt-Tokens` | Router 计算的输入 token |
| `X-1Panel-Affinity` | 会话亲和状态 |
| `X-1Panel-Capacity-Attempts` | 容量候选尝试次数 |
| `X-1Panel-Queue-Wait-Ms` | 容量等待时间 |
| `X-1Panel-Protocol` | `chat` 或 `responses` |
| `X-1Panel-Tool-History-Repaired` | 本次安全修复的工具历史数量 |
| `X-1Panel-Conversation-ID` | Router 最终使用的会话 ID |
| `X-1Panel-Conversation-Mode` | `stateful` 或 `inferred` |

排障和反馈至少携带 `X-1Panel-Route-Request-ID`，不要附带 API Key 或完整敏感
prompt。

## 12. 错误与重试

Router API 由本地与 Tailnet 两个实例提供。计划内更新会先将单个实例切换为
排空状态，再等待在途模型请求完成。排空实例返回：

```json
{
  "error": {
    "code": "router_draining",
    "message": "router instance is draining"
  }
}
```

客户端可重试另一个 Router 地址。若 Router 在请求执行期间异常重启，原
HTTP/SSE 数据流无法续传，客户端应使用相同的稳定会话 ID 重新发送请求。
Router 只从最后一份完整持久化历史继续，不会加入中断的助手输出。

重启后如果旧模型任务仍在底层 worker运行，显式模型可能暂时返回
`429 model_capacity_busy` 和 `Retry-After: 1`；`auto` 会尝试其他兼容
deployment。底层 worker显示空闲后会自动恢复，不需要客户端更换会话 ID。

| HTTP 状态 | 稳定错误码或场景 | 客户端处理 |
| --- | --- | --- |
| `400` | `invalid_request`、`invalid_tool_history` | 修正请求，不自动重放工具链 |
| `401` | `invalid_api_key` | 检查客户端密钥与权限 |
| `409` | 同一会话已有请求运行 | 等待当前轮完成后重试 |
| `413` | `payload_too_large` | 缩小图片、音频或请求历史 |
| `429` | `all_local_capacity_busy` 或客户端限流 | 遵循 `Retry-After`，使用抖动退避 |
| `503` | `no_eligible_model` | 缩小上下文/输出预留或调整所需能力 |
| `503` | `auth_store_unavailable` | 暂停重试并联系管理员检查 Redis，Router 不会绕过撤销状态 |
| `502/504` | 上游暂时失败或超时 | 仅在请求可安全重放时有限重试 |

推荐重试策略：

1. `429` 按 `Retry-After` 等待，并加入 0 到 250 ms 随机抖动。
2. 非流式、无副作用请求最多重试 2 次。
3. 已开始流式输出、已经执行工具或有外部副作用的请求不得盲目重放。
4. 为每次业务操作设置幂等 ID，避免调用方重试产生重复动作。

## 13. 第三方接入检查表

- 已加入 Tailscale tailnet，且只能访问 Router 的 Tailnet 地址。
- 使用独立、最小权限、可轮换的客户端 API Key。
- 默认使用 `model=auto`，不依赖某台机器或某个 GPU 的地址。
- 多轮请求提供稳定 `X-1Panel-Conversation-ID`。
- 正确保留工具调用 ID 和完整消息历史。
- 设置合理的连接、首 token、总请求和业务级超时。
- 记录路由响应头、HTTP 状态和 Router 请求 ID。
- 对 `409`、`429`、`503` 和流式中断采取不同处理。
- 不在日志、错误反馈、前端代码或仓库中保存 API Key。

## 14. WorkBuddy 配置说明

WorkBuddy 的 OpenAI-compatible 自定义模型可配置为：

```text
Base URL: http://ai-X10DRG.taild500c8.ts.net:4000/v1
API Key:  分配给 WorkBuddy 的客户端 API Key
Model:    auto
```

部分 WorkBuddy 页面会把 Provider 名和模型名组合显示为
`custom-local:auto`。实际发送给 Router 的模型 ID仍应为 `auto`。

WorkBuddy 的自定义模型目录还包含客户端本地能力开关。该条目应至少设置：

```json
{
  "id": "auto",
  "supportsToolCall": true,
  "supportsImages": true
}
```

如果 Router 已支持图像但 WorkBuddy 仍显示“不支持图片”，优先检查
`~/.workbuddy/models.json` 中 `auto.supportsImages`，然后完全重启
WorkBuddy 或重新进入模型选择页。Router 的 `/v1/models` 也会返回
`supportsImages` 和 `input_modalities`，供支持动态能力发现的客户端使用。
