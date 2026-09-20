<!-- context-meta
{
  "status": "current",
  "last_verified_at": "2026-09-20T18:06:21+08:00",
  "verified_commit": "471b3004b14a4c7add9088e3d448a2eb41f7e087",
  "runtime_verification": "read_only_metadata",
  "authoritative_sources": ["../deploy/h3-fleet/README.md", "../deploy/h3-mcp/README.md", "../deploy/h3-video-studio/README.md", "../deploy/runtime-release/README.md"]
}
-->

# H3 平台当前状态

## 组件边界

- `deploy/h3-video-studio/`：项目、流程和人工审批驱动的视频工作室。
- `deploy/h3-fleet/`：执行节点、资源准入、调度和状态汇总。
- `deploy/h3-mcp/`：Studio/Fleet 的工具、认证和 WorkBuddy 集成边界。
- `deploy/runtime-release/`：不可变发布、外部依赖指纹、切换与回滚。

具体接口和操作步骤以各组件 README 为准。本页只记录跨组件边界和带日期运行快照。

## 生产运行快照

**已部署并只读核验**：`/home/ai/.local/state/1panel-runtime/current` 当前解析到
`20260914-ee51dd6b3-r2`。release 工具验证结果为 `ok=true`，绑定 Git 提交
`ee51dd6b3bb3fe83ef5896c630ea1dd611bbefcf`，release SHA256 为
`0c155da561d49092b6cf18610235824ac2c23c6c663bc747602f49d28c9ce690`。

以下用户级服务在核验时处于 active/running，重启计数为零：

- `h3-studio-ivan.service`
- `h3-studio-ivan-preview.service`
- `h3-working-set-status.service`

正式 Studio 和预览 Studio 的 WorkingDirectory 指向 `current` 下对应不可变组件；状态
发布器指向 `current/h3-fleet`。本次没有提交项目、生成视频、调用模型或执行端到端质量
验收，因此不能从进程状态推断业务输出质量。

## 不变量

- H3 准入保持 fail-closed；资源预算、cgroup、模型、素材和人工审批缺证据时不放宽。
- 配置、发布工件、systemd 实际 WorkingDirectory、进程 cwd 和客户端可见结果分别验证。
- 不可变 release 验证通过不代表外部模型、venv 或节点当前可用；按任务核验对应外部组件。
- 未知写入结果使用原 operation ID 查询，不盲目重放或创建替代操作。
- GPU/真实媒体测试按主机串行，执行前确认范围，完成后复验原生产服务。

## 更新触发

Studio/Fleet/MCP 的接口、准入、审批、调度、运行目录或 release 工具变化时，同步更新
本页。生产切换后记录新的 release 路径、提交、清单哈希和 systemd 实际值；历史验收
文档继续保留，但不自动代表当前运行状态。
