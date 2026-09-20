<!-- context-meta
{
  "status": "current",
  "last_verified_at": "2026-09-20T18:06:21+08:00",
  "verified_commit": "471b3004b14a4c7add9088e3d448a2eb41f7e087",
  "runtime_verification": "read_only_metadata",
  "authoritative_sources": ["../config/defaults.yaml", "../config/registry.yaml", "../ai_router/policy.py", "../ai_router/routing_modes.py", "../ai_router/errors.py"]
}
-->

# AI Router 当前状态

## 证据边界

本页同时描述 Git、未提交工作区和生产运行镜像，三者不得混用。动态模型 ID、窗口、
阈值和端点以 [默认策略](../config/defaults.yaml)、[模型注册表](../config/registry.yaml)
及运行时覆盖为准。

本次生产核验只读取容器、镜像、源码哈希和脱敏后的运行时目标设置；没有调用真实
模型、修改设置、drain、重启或部署。

## 存档消费者修复上线（2026-09-20 19:21 UTC+8）

- 独立 archive-worker 已更新为 `1panel-ai-router-archive-worker:20260920-ackfix-r2`；
  镜像 ID：`sha256:3c15b68f099c68540efd1f7cd38ee142f68486e70e4f7fc47e6cfd706ed39237`。
- 修复 ACK 响应丢失后错误重试已消费事件、空索引导致消费者退出，以及 Python 3.10
  空闲轮询超时异常兼容问题。真实独立 Redis 进程测试与相关单测共 24 项通过。
- 本次仅替换存档消费者，API Local、API Tail、Redis 容器未替换。消费者健康、源码
  哈希验证通过；本次未发起真实模型测试。下面的旧生产快照不代表当前全部镜像。
- 发布、回滚覆盖文件及验证记录：`outputs/archive-ack-fix-20260920/`（仓库根目录下）。
  后续 Compose 操作应使用该目录的 `compose.override.json`，避免旧覆盖文件回退消费者。

## 生产运行快照（此前记录）

- API Local 与 API Tail 均处于运行状态、重启计数为零，但未配置容器 healthcheck；
  这只能证明进程存在，不能证明真实推理成功。
- Local API 镜像 ID 为
  `sha256:b5956be7efef5c64d59d22d89d7adf72f07bec09068fa474591b5ef29a0892ab`，
  Tail API 镜像 ID 为
  `sha256:fd2a40685f26ab249527501cd8dbcce408b09179da1ef59072465fe521dd264b`；
  两者 OCI revision 均为 `567f20f548a7e0064e35c97cde3b24f2fab70474`。
- Control 与 LiteLLM 仍运行前一基础镜像，其 OCI revision 为
  `32196dc72e1a4b1356075a81ce70678fbbd3a396`。因此不能用单一版本号描述整个 Router。
- 两个 API 容器的部分源码哈希彼此不同，也与当前工作区并非全部一致；生产事实应按
  具体实例和文件核验。

## 时间窗口路由

**已部署并只读核验**：生产 API 的 objectives 已启用、模式为 efficiency、非
observe-only，schedule 已启用；运行时窗口和工作/非工作时段候选与默认配置中的
schedule 区块一致。

该机制只在效率路由进入云端 Flash 候选阶段时替换对应任务组的 Flash 顺序。它不会
覆盖显式模型、提示指令、客户端绑定、模态/上下文/授权等硬约束，也不会取消本地优先。
因此一次请求未选择预期云模型时，应先检查它是否实际进入云端 Flash 分支，再检查
当前时区窗口和运行时覆盖，不能只看墙钟时间。

实现和边界测试位于 `ai_router/routing_modes.py` 与 `tests/test_routing_modes.py`。

## 大上下文与压缩

**工作区实现、尚未在生产 API 镜像发现**：当显式模型、提示指令或其他定向选择把
请求约束到上下文不足的目标时，Router 不再自动压缩并继续请求，而是在推理前返回：

- HTTP `422`
- 业务错误码 `context_too_large_for_selected_model`
- 面向客户端的建议：切换更大上下文模型、由客户端压缩，或开始新会话

自动路由仍可在账号权限、上下文策略和目标能力均允许时使用现有压缩机制。该改动的
实现证据在 `ai_router/policy.py`、`ai_router/api.py`、`ai_router/errors.py`，目标测试在
`tests/test_context_policy.py`。在完成候选镜像验证和明确部署前，不得描述为生产已生效。

## 路由与身份不变量

- 用户显式模型和有效提示指令不能被时间窗口或普通 fallback 静默覆盖。
- 上下文、模态、工具历史、授权、隐私、预算与容量在选择模型前检查。
- `siyuan/auto` 是公开身份；内部端点、真实模型、策略和审计证据不得通过公开响应泄漏。
- 会话亲和、模型迁移和客户端绑定需要分别审计，不能仅凭最终模型反推原因。
- 非流式超时属于请求级失败，不应自动冷却共享端点；是否已部署仍需按运行镜像复核。

## 更新触发

修改路由模式、策略、上下文/压缩、错误契约、身份、历史兼容、运行时设置或注册表时，
同步更新本页。部署后用实际镜像 ID、revision 和必要源码哈希替换“工作区实现”状态；
真实推理只有在另行授权后才能作为验收证据。
