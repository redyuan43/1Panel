# 动态端点管理

更新日期：2026-09-16

## 作用

端点管理用于在不修改 `registry.yaml`、不重启 Router、也不操作 GPU
模型进程的情况下调整生产路由。

静态注册表是能力上限和回滚基线，Redis AOF保存运行时覆盖、草稿、验证结果和
全局 revision。local 与 tail 两个 Router在每次请求开始时检查 revision，
只有版本变化时才重建有效 Registry。

## 状态

- **启用**：允许显式调用。
- **停用**：拒绝新请求，并从客户端 `/v1/models` 隐藏；在途请求继续完成。
- **自动候选**：启用端点是否允许参加 `model=auto`。
- **草稿**：尚未影响生产的能力或容量修改。
- **动态配置**：验证并激活后的 Redis覆盖。

停用不会停止 systemd、Docker、vLLM、llama.cpp 或云端服务，也不会删除会话
历史、亲和记录或 prefix cache。

## 可编辑内容

管理页允许在静态注册表上限以内修改：

- 安全上下文和配置上下文。
- 最大并发。
- 任务和输入模态。
- Chat、Responses、工具、`tool_choice`、结构化输出和流式能力。

页面和服务端都会阻止能力扩张。例如注册表只声明文本的 Edge 不能从页面开启
图像；上下文和并发也不能超过已登记的安全上限。新增能力或提高安全上限必须先
通过独立真实验收，再更新版本化注册表。

## 操作流程

1. 在“模型节点”中选择“编辑”。
2. 修改字段并保存草稿。
3. 验证草稿。当前验证包括配置一致性、能力上限和实时健康检查。
4. 验证通过后激活，两个 Router实例在下一请求同步生效。
5. 使用“恢复基线”可以清除该端点的动态配置和草稿。

停用、启用和自动候选开关属于运行操作，会立即生效，不需要创建草稿。

## 管理 API

```text
GET    /api/endpoints
PATCH  /api/endpoints/{endpoint_id}
POST   /api/endpoints/{endpoint_id}/validate
POST   /api/endpoints/{endpoint_id}/activate
DELETE /api/endpoints/{endpoint_id}/draft
POST   /api/endpoints/{endpoint_id}/actions/{action}
POST   /api/endpoints/{endpoint_id}/reset
```

所有写请求需要管理密钥。配置类请求应携带最新的 `expected_revision`；版本不一致
返回 `409 endpoint_revision_conflict`。临时维护动作 `drain` / `resume` 不修改配置
revision，并且可幂等重复调用。

可用 action：

```text
enable
disable
auto-enable
auto-disable
drain
resume
```

`drain` 会阻止新请求进入该 endpoint，并返回两个 Router实例中仍在运行的请求
数量；不会把请求静默切换到云模型。`resume` 只在 endpoint 健康时清除维护标记。

## 客户端旧模型名兼容

`routing.client_route_bindings` 在请求进入路由策略前，把指定内部账号的旧请求
契约规范化为唯一的路由意图。规则按 `client_id + requested_model` 精确匹配，同时
显式声明可被替代的历史 Tier；未匹配的账号、模型和 Tier 保持原行为。目标 endpoint
必须唯一对应一个公开模型，账号也必须显式获准访问目标模型，否则请求失败。规范化
完成后，权限、模态、图片数、上下文、健康和容量仍由正常路由流程检查；目标不可用时
不会切换到其他本地节点或云模型。

```yaml
routing:
  client_route_bindings:
    - client_id: home-assistant
      requested_models:
        - auto
        - huihui/Qwen3.8-27B-Q4-DFlash2
      target_endpoint_id: ai-qwen38-27b
      ignored_route_tiers:
        - local-large
```

上游请求使用真实目标模型与 endpoint；审计同时保留客户端请求、规范化目标和被替代
的历史约束；对客户端返回的协议字段 `model` 保留其原始请求值。该功能仅对
`disclosure_mode=internal` 的账号生效。

重要审计事件包括：

```text
endpoint_draft_updated
endpoint_validation_started
endpoint_validation_completed
endpoint_activated
endpoint_enabled
endpoint_disabled
endpoint_auto_enabled
endpoint_auto_disabled
endpoint_maintenance_drained
endpoint_maintenance_resumed
endpoint_reset
```
