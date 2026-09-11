# 执行身份修复与窄发布

## 根因与修复

原 `Contract.update` 把所有重新进入 queued 的状态变化当成新 attempt。准备阶段 running 返回资源队列 queued 时，run_id 被重置，原观察线程后续更新被 StaleRun 拒绝；Fleet 任务仍运行，Studio 却停留在排队状态。

修复后，running/queued 往返不更换 run_id；同一未对账完成的 execution 恢复观察也不换号。明确终态重试才建立新 attempt，旧回调仍被拒绝。比较实验横幅明确不包含工作室任务，不再把实验空闲描述为全局无任务。

## 验证与上线

- 旧条件能够复现回队换号失败。
- 修复后源及 Studio overlay：12 passed；真实磁盘候选直接加载：12 passed，3.15 秒。（复跑须显式设 `H3_CONNECTOR_TEST_STUDIO_ROOT=<已叠加候选或当前线上 release>`；不设会因缺 overlay 安装的 `connector_api.py`/`browser_tasks.py`/`input_view.py`/`multimodal_client.py` 出现 7 failed / 3 errors 假失败。18:02 独立复验 `tests/test_router_contract_runs.py` 仍为 12 passed，记录见 `audit-20260911-run-lifecycle-and-banner.md`。）
- 比较横幅测试通过（空闲、运行、过期或缺失状态断言）；diff check 通过。
- 候选 64 文件摘要检查通过，只变更 app/router_contract.py 与 frontend/comparison-status.js。
- 已切换 Studio 至 `/home/ai/.local/state/h3-studio-ivan-production/releases/20260911-status-fix-r1`。本次未切换 Control/Fleet、未重启 GPU worker、未修改资源准入保护。
- 备份、页面回执与观察采样：`/home/ai/.local/state/h3-studio-ivan-production/deployment/status-fix-20260911-r1`。

## 单次真实页面重试

项目 `7732e9926474` 原执行 `studio_7732e9926474_preview_499ac7280ebd44c5acd465c7810ac6f0` 已取消。部署后通过原执行对账恢复 Studio 的 cancelled 状态，不重复提交旧执行。

用户授权后从页面点击一次重试，保留已批准的原首帧、提示词和规格：

- run_id：`run_0b39e130010049809ec46f878ec42e29`
- execution_id：`studio_7732e9926474_preview_f07c367ec7004f6a8980009c264252f7`
- Fleet task：`2d3f113d2c3d43f8b161ac53b25378a1`
- 首帧摘要：`a80da8df3ab36cb5af5ce8d96792f69a045ae435a41febdc6efec8415232c63a`

最初响应 running，随后资源等待 queued，run_id 保持不变。真实成片及后续阶段结果须以审计采样为准，不能把提交成功当成成片成功。

12:42:36 实查：稳定窗 969.3 秒且 ready=true，PSI avg10 为零，pswpout 未增长。当前阻断为 gpu_vram_headroom：配置要求 14500 MiB，后端可用 7234.5 MiB；旧任务取消后进程仍占用 8266 MiB，且无已验证热模型记录可供准入复用。新任务 backend prompt 为空、真实采样事件为零，未发现新增 OOM。持续采样中的 run_id 未变化。本轮未绕过显存保护或擅自重启后端；取消后的模型释放/热状态对账是另一处待解决阻断，不宣称已开始生成或完成端到端验收。

## 回滚边界

旧不可变版本及数据库备份保留。需要回滚时先停止新接单，保留在途执行身份和对账；任务终结后再恢复旧 Studio 工作目录。不可在运行中回退到已知会丢失观察线程的旧逻辑，不恢复旧数据库覆盖新执行，不重复提交任务。
