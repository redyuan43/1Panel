<!-- context-meta
{
  "status": "current",
  "last_verified_at": "2026-09-20T18:06:21+08:00",
  "verified_commit": "471b3004b14a4c7add9088e3d448a2eb41f7e087",
  "runtime_verification": "read_only_metadata",
  "authoritative_sources": ["../AGENTS.md", "../deploy/runtime-release/README.md"]
}
-->

# 1Panel 本地扩展项目上下文

## 项目地图

本仓库既包含 1Panel 上游代码，也包含本地模型与媒体运行组件：

- `core/`、`agent/`：1Panel Go 服务，各自拥有独立 Go 模块。
- `frontend/`：Vue 前端，命令和依赖以其 `package.json` 为准。
- `deploy/ai-router/`：统一模型入口、身份、上下文、预算、审计和路由策略。
- `deploy/h3-video-studio/`、`deploy/h3-fleet/`、`deploy/h3-mcp/`：H3 Studio、
  调度、认证和 WorkBuddy 集成。
- `deploy/runtime-release/`：不可变运行版本、外部依赖绑定、切换和回滚工具。

## 事实与证据层级

判断“当前是什么”时按以下顺序核验：

1. 当前运行进程、容器、systemd 与只读状态证据；
2. 实际运行镜像、OCI revision、不可变 release manifest；
3. 运行时配置与持久覆盖；
4. 当前工作区代码与测试；
5. Git 提交；
6. current-state 文档、历史验收和 Memory。

较高层证据不会自动证明完整功能成功。例如容器启动、健康检查和模型列表不能替代
真实推理；历史成功也不能证明当前实例仍相同。

## 当前核验快照

截至文档元数据中的时间：

- Git 分支为 `dev-v2`，基线提交见 frontmatter。
- AI Router 工作区存在大量未提交实现与测试，Git HEAD 不能代表整个工作区行为；
  详情见 [Router 当前状态](../deploy/ai-router/docs/current-state.md)。
- H3 正式运行目录指向一个已通过 release 工具校验的不可变版本；详情见
  [H3 平台当前状态](h3-platform-current-state.md)。
- 本轮只核验运行元数据，没有发送真实模型、视频或媒体请求，也没有改变服务状态。

## 状态标签

组件文档统一使用以下含义：

- **已部署**：已在实际运行实例核对镜像、release 或源码证据。
- **工作区实现**：只存在于当前未提交工作区，不能声称已部署。
- **已测试**：记录测试范围和结果，不等同于生产验收。
- **历史证据**：可用于定位与比较，当前状态必须重新核验。
- **待验证**：代码或配置存在，但缺少目标层级证据。

## 更新规则

- 组件行为、错误契约、路由策略、运行配置或发布流程变化时，同一变更更新对应
  current-state 文档。
- 易变化的模型 ID、端点、时间窗口和阈值只在权威配置中维护；状态文档描述行为并
  链接来源。
- 生产状态写明核验时间、范围和未执行项，不使用“当前正常”代替可复核证据。
- Memory 仅作为历史检索层，不能覆盖本仓库已提交规则和当前现场证据。
