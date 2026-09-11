# MCP 设置与 WorkBuddy 认证接入记录

## 已部署

- 设置页：https://ai-x10drg.taild500c8.ts.net:8445/mcp-settings.html
- OnePanel 薄代理同步支持设置页、原生认证包及受保护管理接口。
- Studio 不可变发布：`/home/ai/.local/state/h3-studio-ivan-production/releases/20260910-mcp-settings-r1`。
- 两个 Control 镜像：`sha256:400ed940a7fd82ff20032457b56d839a781914a25a50841ae9f6a2ca0bd4b3ef`。
- 私有备份与回执：`/home/ai/.local/state/h3-studio-ivan-production/deployment/mcp-settings-20260910-r1`。
- 管理功能：专用 Token 一次性发放、撤销、草稿接单开关、双实例心跳、最近成功认证与工具调用。没有把心跳当作客户端在线。
- 管理入口要求现有 OnePanel 管理身份或明确列入管理员名单的 Tailscale 身份；视频 Token 不授予管理权限。

## 验证证据

- 旧基线与实际候选各 495 项测试通过，均零失败、零跳过；9 项隔离浏览器交互通过。
- 已安装原生 connector 的候选测试不再重复安装 connector；旧测试基线继续适用。
- 真实 Tailscale 浏览器设置页与状态 API 返回 200，发放按钮可用，未出现页面脚本错误。
- 真实 OnePanel 会话、跨站拒绝、临时 Token 发放、九工具发现、历史草稿查询和撤销后 401 均通过。
- 原六个项目内容哈希不变；其余容器身份与启动时间不变；验收后 Fleet 执行 0、排队 0。
- 本轮没有调用提示词确认或生成工具，没有新增 GPU 推理。MCP generation 硬开关仍关闭，不影响原工作室已有生成流程。

## 客户端状态与后续操作

Ivan WorkBuddy 官方 CLI 已安装并启用 `siyuan-h3-connect@h3-studio-private` 0.2.0，声明原生 Token 表单依赖。
安装成功不等于原生 GUI 授权成功，原生表单未完成实际用户验收。

后续只读检查发现现有 `siyuan-h3-studio` 普通 MCP 配置没有 Authorization 头。
用户明确改为要求直接配置并由其自行测试，因此本次采用现有 HTTP MCP 的 headers 方式，
专用 Token 仅通过 SSH 输入流传到客户端私有配置，不放入聊天、命令参数或发布包。
这是相对于最初“只用原生表单”的显式交付方式调整；本机 MCP JSON 中的凭证并非加密凭证库，
不要分享此配置或将其纳入 Git。通过设置页可以撤销；部署目录中的 `workbuddy-direct-auth.json` 仅记录密钥编号与交付状态。

只修改 `C:\Users\Ivan\.workbuddy\mcp.json` 中 H3 条目的认证信息并保留其他配置，修改前创建备份。
用户随后对原有 `siyuan-h3-studio` 点击重连；不需要再次导入向导或复制 Token。实际客户端连接仍由用户验收，不作成功声明。

## 回滚

先通过设置页暂停 MCP 写操作，保持生成关闭并核对在途任务。不能取消健康任务或重提历史任务。
仅在不需要新版本对账时，恢复备份中的 Studio drop-in 配置及两个 Control 旧镜像
`sha256:1b03dfed91dc698ed473ea450615c4435dcf3ea16224f696a7dfb669789733e1`，保留所有原 Compose 覆盖文件。
本次新增 Studio 配置为 `50-h3-mcp-settings.conf`；不要移除更早发布的 `40-h3-mcp.conf`。
客户端回滚只恢复 H3 条目并撤销新密钥，不覆盖并行新增的其他 MCP 配置；不得回滚账户数据库或历史视频。
