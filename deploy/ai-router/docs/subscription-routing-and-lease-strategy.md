# 订阅模型路由、并发与租约策略

更新日期：2026-09-02

## 1. 目标

AI Router 同时管理本地 GPU、云端 API 和订阅账号。三类资源的容量形式不同：

- 本地 GPU 的容量由物理 worker 数量决定。
- 普通云端 API 的容量由供应商 RPM、TPM、并发和预算决定。
- Codex Pro、GLM Coding Plan 等订阅服务的容量由账号、供应商限流和本地配置
  共同决定。

Router 必须在不混淆模型能力与物理容量的前提下实现：

1. 本地模型优先处理普通请求。
2. 本地容量不足时，优先使用 GLM-5.3-Flash。
3. Codex Pro 账号允许多个不同会话并行调用。
4. 每个请求使用独立租约，完成后只释放自己的容量。
5. 同一会话保持顺序和亲和，不允许并发修改同一份历史。
6. OAuth 刷新锁只保护凭据更新，不锁住整个模型调用。
7. 远程供应商不可用时快速切换，不让请求长时间排队。

## 2. 第一性原理

### 2.1 模型、账号和请求不是同一个对象

模型描述能力，例如上下文、图像、工具和质量。账号代表访问供应商的凭据和
配额。请求才是正在消耗容量的实际作业。

```text
模型：gpt-5.6-sol
  |
  +-- 账号 primary
        |
        +-- 请求 A
        +-- 请求 B
        +-- 请求 C
```

不能因为只有一个账号，就把它永久理解成只能服务一个会话。正确做法是为账号
配置并发容量，并为每个在途请求建立独立租约。

### 2.2 并发容量与会话亲和必须分离

并发容量回答的是：

> 这个账号现在还能不能再接一个请求？

会话亲和回答的是：

> 这次续问应该优先回到哪个账号或物理 worker？

请求结束后立即释放容量租约，但会话亲和可以继续保留 24 小时。亲和记录不会
持续占用并发容量。

### 2.3 显示顺序不等于路由顺序

管理页面中的“物理 Worker 与订阅槽位”只是资源清单。Codex Pro 出现在表格
底部，不表示它是最后 fallback，也不表示它是一张 GPU。

生产页面应将以下内容分开显示：

- GPU 物理 worker。
- 远程订阅账号及并发容量。
- 模型端点及其路由资格。
- 实际 fallback 顺序。

## 3. 目标路由顺序

### 3.1 普通自动请求

```text
会话原绑定节点
  → 兼容的本地 GPU
  → DeepSeek V4 Flash
  → GLM-5.3-Flash
  → Codex Pro GPT-5.6 Sol
  → 422/429/503
```

- 健康的会话优先回到原 deployment 或订阅账号。
- 新请求优先使用本地模型。
- 普通文本和研究任务的云端顺序是 DeepSeek、GLM、Sol。
- 普通编程任务的云端顺序是 GLM、Sol、DeepSeek。
- 复杂编程、架构、安全和调试任务的云端顺序是 Sol、GLM、DeepSeek。
- 普通多模态的云端顺序是 GLM、Sol；复杂多模态编程为 Sol、GLM。

### 3.2 复杂编程主动升级

复杂、多文件、架构、调试和安全任务属于主动能力升级，不等同于容量 fallback。

目标策略：

```text
确定性规则或本地评估器判定复杂编程
  → 会话原节点
  → 完整合格的本地池
  → Codex Sol
  → GLM
  → DeepSeek
```

不能通过伪造质量分控制供应商顺序。Router 应使用明确的远程 fallback 配置，
例如：

```yaml
routing:
  remote_fallback_order:
    complex_code:
      - codex-pro-gpt-5.6-sol
      - zhipu-glm-5.3-flash
      - cloud-deepseek-v4-flash
```

质量分继续只表示模型在具体任务上的评测结果。

### 3.3 显式模型请求

用户明确指定模型时不得替换：

- 显式 GLM 请求只调用 GLM。
- 显式 Codex 请求只调用 Codex。
- 显式本地模型只在该模型族的兼容物理 worker 之间调度。
- 容量满时等待最多 3 秒，然后返回 `429` 和 `Retry-After: 1`。

## 4. Codex Pro 多路并发

### 4.1 账号容量

每个账号具有独立的可配置并发容量：

```yaml
codex_accounts:
  primary:
    max_concurrency: 2
```

也可以通过环境变量提供默认值：

```text
AI_ROUTER_CODEX_ACCOUNT_MAX_CONCURRENCY=2
```

首轮从 2 路开始实测。确认没有持续账号级 `429`、响应串线或刷新冲突后，再考虑
提升到 4 路。不能假设供应商允许无限并发。

### 4.2 独立请求租约

账号容量为 4 时，可以存在四条独立租约：

```text
codex-primary:req-A
codex-primary:req-B
codex-primary:req-C
codex-primary:req-D
```

每条租约至少记录：

- `instance_id`
- `boot_id`
- `request_id`
- `lease_token`
- `account_alias`
- `conversation_id`
- `started_at`
- `last_heartbeat_at`

请求 A 完成时只能删除 A 的租约，不得删除 B、C、D。

Router 使用共享 Redis 原子检查：

```text
active_leases(account) < max_concurrency
```

检查与写入必须在同一个原子操作中完成，避免 local 和 tail 两个 Router 实例
同时取得最后一个容量位置。

### 4.3 Adapter 并发保护

Router 容量租约和 Codex Adapter 的 semaphore 必须使用相同容量：

```text
Router max_concurrency = 2
Adapter semaphore      = 2
```

两层用途不同：

- Router 租约负责跨实例全局容量。
- Adapter semaphore 负责适配器进程内的最终保护。

不能只修改其中一个。否则可能出现 Router 判断可用但 Adapter 返回 busy，或者
Adapter 接收了超过 Router 可观测范围的请求。

## 5. 会话、缓存与工具状态

### 5.1 不同会话可以并行

```text
会话 A → Codex primary → 请求 A1
会话 B → Codex primary → 请求 B1
```

A1 和 B1 可以同时执行，历史和工具状态按 `conversation_id` 隔离。

### 5.2 同一会话必须串行

```text
会话 A → 请求 A1 正在执行
会话 A → 请求 A2 到达
```

A2 必须等待 A1 完成或快速返回 `conversation_busy`，不能与 A1 同时修改历史。
否则可能出现回答顺序颠倒、工具结果匹配错误或缓存键不稳定。

### 5.3 请求完成与亲和保留

请求完整结束后：

- 释放请求租约。
- 释放账号并发容量。
- 保留 24 小时会话亲和。
- 保留完整且已加密的对话历史。
- 不删除供应商或本地模型的 prefix cache。

流式请求只有在完成事件、正常 EOF 或上游连接关闭后才能释放租约。

## 6. OAuth 与账号状态

### 6.1 OAuth 刷新锁

账号可以多路推理，但 refresh token 更新必须串行：

```text
请求 A 发现 token 即将过期 → 取得刷新锁并更新凭据
请求 B 同时到达           → 等待刷新完成后读取新 token
```

刷新锁只能覆盖：

1. 读取凭据。
2. 判断是否过期。
3. 调用刷新接口。
4. 原子写回新凭据。

不能让刷新锁覆盖完整推理，否则多路并发会退化为一路。

### 6.2 供应商限流

出现账号级 `429` 时：

- 不删除其他正在执行的请求租约。
- 将账号标记为短期 cooldown。
- 新 `auto` 请求立即尝试下一个远程 fallback。
- 显式 Codex 请求返回稳定的 `429`。
- 记录 `Retry-After`、错误类别和 cooldown 到期时间。
- 不记录账号 ID、access token 或 refresh token。

连续出现 `429` 时自动降低该账号的有效并发上限；稳定运行一段时间后再逐步恢复。

## 7. Router 重启与租约恢复

Router 重启后不可能续接已中断的 HTTP 响应，因此：

- 清理该 Router 实例上一个 boot ID 留下的租约。
- 不删除另一个 Router 实例的有效租约。
- 不把未完成的助手回答写入历史。
- 客户端使用原会话 ID 从最后一个完整轮次重试。

Codex 是远程服务，Router 清理租约后无法直接确认旧请求是否仍在供应商后台
运行。为避免立即超发，Adapter 应维护短暂的在途请求状态，并在连接关闭或超时
后释放。强制重启 Adapter 时，可以对对应账号设置短暂 drain 窗口。

## 8. 管理页面

建议拆分为两个表格。

### 8.1 GPU 物理 Worker

显示：

- GPU 和端口。
- deployment/profile。
- 上下文和 KV cache。
- 活动请求。
- 健康、冷却和配置漂移。

### 8.2 远程订阅账号

显示：

- Provider 和脱敏账号别名。
- 模型。
- 活动请求数/最大并发，例如 `1/2`。
- 账号状态和 cooldown。
- 最近一次 `429`。
- 上下文。
- 当前路由位置，例如 `远程 fallback #1`。

页面不得展示真实账号 ID、OAuth token、API key 或凭据路径。

## 9. 当前实现与差距

截至 2026-09-02：

- `intelligent_v2` 已实现并作为默认策略，保留 `legacy_v1` 热回滚。
- Codex 注册表 `max_concurrency=2`。
- ChatGPT Codex 订阅协议不支持硬输出上限参数；此类请求不会选择 Sol。
- Codex Adapter 使用可配置的两路 semaphore。
- Router 的 Redis deployment 租约已经支持 `capacity > 1`。
- OAuth 凭据刷新已经具有独立文件锁。
- GLM-5.3-Flash 当前为 `auto_candidate=true`，使用实测 262144 安全上下文参与
  自动 fallback。
- Codex Pro 与 DeepSeek 当前可以参与 `auto`。
- 审计图已升级到 `graph_version=3`。

上线后继续复验 Responses 适配、真实 Auto 分流和审计轨迹。
2. 把 GLM 安全上下文更新为最高实测通过值并启用 `auto_candidate`。
3. 更新 Ivan WorkBuddy 上下文声明。
4. 经确认后排空并重建 Router 与 Codex Adapter。

## 10. 验收计划

### 10.1 Codex 并发

- 两个不同会话同时调用同一账号，均得到正确且不串线的响应。
- 容量为 2 时，第 3 个 `auto` 请求立即转向当前画像的下一云端候选。
- 第 3 个显式 Codex 请求等待最多 3 秒后返回 `429`。
- A 完成后只释放 A 的租约，B 仍保持占用。
- 流式断开、上游错误和客户端取消均不会遗留租约。

### 10.2 会话和 OAuth

- 同一会话的并发请求被串行化。
- 不同会话可以并行。
- 两个请求同时遇到 token 过期时，只发生一次 OAuth 刷新。
- 刷新期间不会把全部推理过程锁成单路。

### 10.3 Fallback

- 普通请求在本地有容量时不调用远程。
- 本地全部繁忙后按请求画像使用固定云端顺序。
- 普通文本首先调用 DeepSeek。
- 普通编程首先调用 GLM。
- 复杂编程首先调用 Codex Sol。
- 云端关闭时只使用本地，全部繁忙返回
  `429 all_local_capacity_busy`。
- 显式模型请求不发生静默替换。

### 10.4 可观测性

- 页面显示 Codex `active/max`。
- 审计记录每次容量获取和 fallback 原因。
- Redis 中每个在途请求具有独立租约 token。
- 日志、审计和页面中不存在账号 ID、OAuth token 或 API key。

## 11. 上线顺序

1. 完成 GLM 文本、流式、工具、Responses 和图像最小真实验收。
2. 使用隔离环境验证 2 路并发和独立租约。
3. 将生产 Codex 容量提升到 2。
4. 启用 GLM `auto_candidate` 并记录最高实测安全上下文。
5. 将生产策略一次性切换为 `intelligent_v2`。
6. 更新 Ivan WorkBuddy 的输入和输出上限。
7. 滚动重启 Router 和 Codex Adapter，不重启任何 GPU 模型服务。
8. 观察账号级 `429`、错误率和响应串线至少 60 分钟，再决定是否提升到
   4 路。
