# AMD 展示提交及缓存补审（2026-09-08）

## 版本与范围

第一版：`be1ea2ad1`，基于 `1551d1e36`，提交 AMD 进程内缓存声明、部署目录和 Edge 原生磁盘展示。未加入 AMD 磁盘恢复。
本修复提交基于第一版，补审 `3dc696e9e`、`1551d1e36`、`55623a3c4`、`275f54111`，同时检查与 `86deac3da` 逐请求计数契约兼容。
未 push、部署、重启模型、清空缓存或重放旧对话。媒体、注册表提示及其他任务的工作区改动未纳入。

## 确认问题与修复

### P1：把模型健康误报为缓存健康

原 `ai_router/cache_deployments.py:287` 只用模型/Worker 就绪状态生成 runtime_healthy；原 `_deployment_state` 据此返回 Healthy，summary 也累计该值。Edge 历史累计命中存在、但没有当前磁盘连接器探测时，页面仍亮绿，NX3/AGX 同样受影响。模型运行不能证明缓存层可恢复。

CPU 复现输入：Edge 模型 healthy=true，external hits=9000，无缓存服务证据。修前 state=healthy、healthy_count=1；修后 state=unknown、healthy_count=0。证据 `health-reproduction.json`。

修复位置：`ai_router/cache_deployments.py:362` 新增独立 cache_health；`:543` 健康状态必须同时满足模型和缓存证据。只有现有 LMCache 探测的明确服务健康且 Connector active 可确认缓存健康；探测时间缺失、未来或超过 120 秒均未知。未新增 Edge/NX 缓存探测，故这些设备显示“缓存健康未确认”，保留各自模型健康、历史计数、配置与验证声明。

LMCache 服务失败不再被 connector=false 遮蔽成普通配置差异；未知/非有限数字保持缺失。页面详情同样标注未知，不因旧摘要产生第二个“健康”结论。

### P1：按钮用另一份快照重算，可能执行相反动作

原 `ai_router/static/app.js:1165` 抛弃缓存行的 actionId，调用端点 toggle；后者根据 state.dashboard 重新推导动作和版本。缓存行显示 disable/rev9，但 dashboard 已是 disabled/rev10 时，点击停用实际发送 enable/rev10。

修复位置：`ai_router/static/app.js:1082` 将行版本绑定至按钮；`:1154` 提交明确动作及该版本，不查询 dashboard 判断相反动作。版本缺失、只读或非法动作不发请求。409 后刷新两个视图、保留选择、不自动重试。

`tests/test_cache_actions.cjs` 对原代码复现 expected disable / actual enable；新代码覆盖四种动作、409、缺版本、只读和选择保持，8 个场景通过。后端原有版本锁仍生效。

### P2：Edge 安装失败可能留下半套补丁

原 `integrations/edge-disk-cache/patch_simple_kv_disk.py:186` 先写 manager，再变换 worker；后者 anchor 漂移/语法失败会保留已修改的 manager。

修复位置：同文件 `:204`，两文件全部在暂存副本变换、语法及 marker 校验后写入；每文件原子替换，捕获写入失败时回滚已写文件，保留权限。回归覆盖 worker anchor 失败、无效 Python、第二文件写失败和正常更新。

这里保证可捕获失败的回滚，不声称两个文件在 SIGKILL/断电下有跨文件事务性。没有变更 QSA/GPU 事件算法。

### P2：LMCache 混合安装可宣称成功但缺少回滚备份

原 `scripts/patch-lmcache-operation-lifecycle.py:64` 遇 after hash 即跳过。部分文件已打补丁、使用空的新备份目录继续 apply 时，已打补丁文件的 before 备份缺失，后续 rollback 失败。

修复位置同文件 `:64`：apply 对所有已有 after 文件验证对应 before 备份；缺失/错误时在任何运行文件写入前拒绝。check 保持只读、不要求备份。新文件 before=null 无需原始备份。

真实八文件 bundle 在临时副本执行 check → apply(8) → reapply(0) → check(0) → rollback(8) → rollback(0)，各阶段逐文件 manifest hash 一致。

## 其余审查结论

- AMD：只声明 llama.cpp 内存前缀缓存与 4 checkpoints；实时命中未知，不暗示磁盘恢复，页面只读。
- PR #3：缓存接口走现有管理员认证，响应 no-store；目录未携带凭据。与控制台计数/路由审计测试兼容；上面两项 P1 在合并版本仍存在，已实际复现后修复。
- WorkBuddy：历史、工具 ID/参数、图片、client/model/reset 隔离以及原始 lineage 关联的相关回归通过，未发现新的确认缺陷。`workbuddy_history` 明确设计为 24 小时/2048 项；`prefix_breaks` 的 30 天是另一张表的保留策略。本轮未扩大历史保留。
- LMCache：取消、异常传输、所有 store batch 完成、未知错误不安全释放等 shipped CPU 行为测试通过；运行前八个已安装文件 SHA 与 manifest after_sha256 一致。未重新做 GPU 事件/推理验证。
- Edge：保持磁盘容量、目录和既有恢复策略；本轮修补的是安装工具。磁盘重启恢复及真实长上下文性能没有新增验收证据，不据既有测试宣称全部边界已解决。
- 逐请求缓存计数：未修改 usage 解析/计算逻辑；正/零/缺失、尝试隔离、取消及流式契约相关测试通过。

## 验证记录

- 第一版暂存快照：14 项 Python 测试通过、JS 语法通过。
- 本轮组合：173 项唯一 CPU 测试最终通过，另有 175 个 subtests。首次组合 169 passed/4 failed，四项均为隔离 Router 镜像缺 GNU patch 可执行文件；只读挂载宿主 GNU patch 2.7.6 后，安装测试 10/10 通过。不为环境差异修改业务判断或跳过失败。
- Router 测试使用 `1panel-ai-router:amd-memory-cache-page-20260908-r1` 的生产依赖，无网络、无 GPU。覆盖 cache_deployments、endpoint_config、workbuddy_history、prefix_break、workbuddy_public_prefix、content_audit、usage_stream、usage_metric、cache_audit、control_audit、backend_patch_installation、lmcache_runtime、health_stall、fleet_adapter。
- LMCache shipped suite：57 passed，96 项 OTel 弃用警告，8.53 秒。使用既有 LMCache 运行依赖（Router 镜像不含该库）；全为 CPU 行为测试，没有发模型请求。
- Edge 已有验收逻辑测试 3 passed。JS 动作 8 场景及未知摘要 2 场景、AMD/Edge 展示 4 场景通过；app.js 语法与 diff whitespace 检查通过。
- 隔离浏览器：AMD 内存/4 checkpoints/命中未知/无操作按钮；Edge 模型健康但缓存健康未确认，累计命中不能改变结论；错误 key、普通客户端 key 拒绝；管理员可连接。刷新保留设备与详情页签；合成后端真实返回 409 后只发送一次操作、刷新版本且保留选择，管理员手动重试才发送新版本并成功。浏览器无 error 日志。未点击生产启停按钮。隔离夹具为 tests/ui_preview_cache_review.py；AI 的 null/stale 摘要分支未单独做浏览器变体，仅有 CPU 分支检查。

详细脱敏证据在 AI 的 `/home/ai/github/1Panel/experiments/cache-review-20260908/`：first-tests.txt、health-reproduction.json、baseline-actions-failure.txt、combined-tests.txt、install-tests-production-deps.txt、backend-review.txt、backend-lifecycle-tests.txt、BROWSER_REVIEW.md、browser-action-evidence.json。实验目录不提交；记录仅测试数据/计数/哈希/元数据，无真实提示、缓存文件或凭据。

已知旧 core/AI Pool 测试差异来自之前记录，本轮未跑全仓或将其视为已修复；本次选定套件没有未解决失败。生产仍运行既有版本，待维护窗口部署后才可确认线上页面与行为。回滚本修复时可 revert 此独立提交；本轮没有改生产配置或 runtime 文件，无需服务回滚。
