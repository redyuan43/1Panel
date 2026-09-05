# 智能路由策略 v2

更新日期：2026-09-02

## 当前状态

`intelligent_v2` 的代码、配置校验、审计图 v3 和隔离测试已经实现。GLM
已完成文本、流式、工具、JSON、图片和 262K 边界实测，仓库默认切换为：

```yaml
routing:
  strategy: intelligent_v2
```

GLM-5.3-Flash 注册表使用 `auto_candidate: true`。官方配置上下文仍为 1M，
但 Auto 的安全上下文仅使用真实通过的 `262144`，不会把未实测的 1M 当作
硬资格上限。Responses 协议通过 Router 适配器转换，并在生产发布后继续执行
端到端复验。

## 决策模型

v2 将 Router 分为两个连续阶段：

1. 约束匹配：模态、协议、工具、结构化输出、完整上下文、历史兼容、健康和容量。
2. 阶段内排序：只在同一阶段内比较质量、负载、延迟和上下文余量。

只要有本地候选完整满足请求，云端质量分就不能越过本地阶段。

```text
认证请求
  -> 确定性画像
  -> prompt_tokens + requested_output_tokens
  -> 会话亲和
  -> 本地完整约束匹配
       -> 充分：本地阶段内排序
       -> 不足或全忙：按画像进入固定云端顺序
  -> 容量租约
  -> 历史预检与协议翻译
  -> 上游请求
```

云端顺序由 `routing.remote_fallback_order` 明确配置：

| 画像 | 云端顺序 |
|---|---|
| `general` / `batch` | DeepSeek -> GLM -> Sol |
| `agent_text` | DeepSeek -> GLM -> Sol |
| 普通 `code` | GLM -> Sol -> DeepSeek |
| 复杂编程、架构、安全、调试 | Sol -> GLM -> DeepSeek |
| 普通多模态 | GLM -> Sol |
| 多模态复杂编程 | Sol -> GLM |

DeepSeek 对图片请求始终不合格。长上下文不会覆盖语义画像，只参与完整上下文
资格判断。

`remote_fallback_order` 现在可以通过控制台“策略设置”页签的可视化流程图
编辑（intelligent_v2 分支卡片内的回退链编辑器），无需再手改
`config/defaults.yaml` 或 `/data/settings.yaml`；仍然通过既有
`PUT /api/settings` 接口保存，校验规则不变（每个画像必须是非空且不重复的
端点 id 列表）。

云端端点支持自身配置的并发容量，不使用本地物理卡保护策略。同一会话一旦选择
某个云端模型，只要该模型仍满足健康、模态、协议、工具和完整上下文约束，就持续
使用该模型；确实不可用时才按上表顺序选择不低于当前层级的下一个专家。

## 上下文与压缩

资格公式固定为：

```text
required_context_tokens =
  prompt_tokens + requested_output_tokens
```

客户端申请的 `max_tokens`、`max_completion_tokens` 或
`max_output_tokens` 不会被降低。默认压缩模式为：

```yaml
compaction:
  enabled: true
  mode: explicit_only
```

只有以下任一条件成立时才允许生成中立历史胶囊：

- 客户端账号设置 `allow_compaction: true`。
- 请求包含 `X-1Panel-Allow-Compaction: true`。
- 管理员明确把模式改为 `automatic`。

未经允许且没有完整兼容模型时返回：

```text
422 no_compatible_model
```

允许压缩时只替换历史消息，不改变客户端输出上限；压缩后重新计数并重新运行
完整候选资格检查。

客户端已经自行压缩上下文时，应发送：

```text
X-1Panel-Context-Compacted: true
```

该标记表示压缩已经发生，不是压缩权限。Router 会跳过旧亲和和旧存储历史并开启
新的缓存时期；显式会话 ID 保持不变，没有显式 ID 时生成新的推断 ID。

跨 Provider 时会保留可见内容、工具调用和工具结果，同时删除 Provider 私有
ID、加密推理项和隐藏推理字段。DeepSeek 工具历史缺少
`reasoning_content` 时不会发送上游请求：Auto 会先跳过 DeepSeek，全部候选
均不兼容时返回：

```text
409 history_migration_required
```

## 接口与审计

成功响应新增：

```text
X-1Panel-Route-Strategy
X-1Panel-Route-Profile
X-1Panel-Context-Required
X-1Panel-History-Mode
X-1Panel-Context-Compacted
X-1Panel-Context-Compaction-Source
```

`/v1/models` 的 `auto` 条目新增：

```text
maxInputTokens = 196608
maxOutputTokens = 65536
contextWindow = 262144
modalities
```

这三个值描述 Auto 当前对所有已声明模态都可兑现的安全能力，不采用某个
纯文本候选更高但尚不能覆盖图片请求的上下文上限。

路由审计使用 `graph_version=3`，真实展示：

- 完整上下文公式。
- 本地候选是否充分。
- 云端专家分流及回退位置。
- 历史预检和协议标准化方式。
- 容量获取、重试和最终模型。

SQLite 会向前兼容增加 `route_profile`、`strategy_version` 和
`history_mode` 列，并建立画像索引。

## Codex 容量

Router 注册容量和 Adapter 进程内信号量均配置为 2：

```text
registry max_concurrency = 2
AI_ROUTER_CODEX_ACCOUNT_MAX_CONCURRENCY = 2
```

不同分支可以并行；同一个已完成父分支也可以同时产生多个兄弟分支。Router 不再
用推断出的会话 ID 串行化完整请求，而是只由底层部署容量控制并发。第三个显式
Sol 请求等待最多 3 秒后返回 `429 codex_account_busy`。已经绑定云端模型的续轮
遇到 `429`、`5xx` 或网络故障时不会静默切换模型；只有能力或上下文硬约束不满足
时才允许升级。

ChatGPT Codex 订阅通道不接受公开 Responses API 的
`max_output_tokens` 参数。Router 将该端点标记为
`output_token_limit: false`：带显式 `max_tokens`、
`max_completion_tokens` 或 `max_output_tokens` 的 Auto 请求会跳过 Sol，
显式 Sol 请求返回不兼容错误。Router 不会删除字段后伪装为已保留上限。

## GLM 验收结果

2026-09-02 已执行真实 Coding Plan 验收：

1. Chat 文本和流式。
2. 工具调用和 `tool_choice`。
3. `json_object`。
4. 图片输入。
5. `requested_output_tokens=65536`。
6. 文本输入实测 196018 tokens。
7. 图片输入实测 194040 tokens。

因此当前生产安全值为：

```text
GLM safe_context_tokens = 262144
WorkBuddy maxInputTokens = 196608
WorkBuddy maxOutputTokens = 65536
```

500K 尚未执行真实边界验收。未来通过后才可调整为：

```text
GLM safe_context_tokens = 500000
WorkBuddy maxInputTokens = 434464
WorkBuddy maxOutputTokens = 65536
```

不得继续使用未实测的 1000000 作为 Auto 安全上限。

## 配置位置

生产云端 Key 位于：

```text
/opt/1panel/ai-router/router.env
```

GLM 使用：

```text
AI_ROUTER_GLM_API_KEY=<coding-plan-key>
```

Ivan WorkBuddy 的模型配置位于：

```text
C:\Users\Ivan\.workbuddy\models.json
```

GLM Auto 已启用。Ivan 在线后同步上述 WorkBuddy 字段。

## 上线与回滚

生产发布排空并重建 Router、控制台、LiteLLM 与 Codex Adapter，不重启任何
GPU 模型服务。当前策略为：

```yaml
routing:
  strategy: intelligent_v2
```

发生能力错配、输出上限变化、跨会话串线、租约泄漏或历史兼容错误时，热切回：

```yaml
routing:
  strategy: legacy_v1
```

发布后至少观察 60 分钟，并人工审核前 50 条 Auto 路由轨迹。
