# Studio 四配方入口契约

## 交付文件

- 后端：`app/recipes.py`、`app/fleet.py`、`app/main.py`、`app/router_contract.py`。
- 前端：`frontend/app.js`、`frontend/index.html`。
- 测试：`tests/test_recipes.py`、`tests/recipes.test.cjs`、`tests/recipes_ui_smoke.py`。
- 文档：`docs/recipes.md`。不修改 Fleet 或共享部署脚本。
- 本地验收：完整 Python 97 passed，完整 Node 15 passed，Chromium 离线桌面/手机检查通过；仅现有 FastAPI on_event 弃用警告。

## 范围与历史

- 正式池仅 A4 / A4_C0 / A4_C1 / B8；新建符合范围的项目默认 A4。
- 仅 15 秒、480×864、24fps、native 音频、t2v 的 preview。帧数仍为 362；不调整高清、其他模式或任何并发保护。现有横版默认布局不变，切到竖版后启用配方选择。
- 创建时 `recipe_id` 可离线持久化；未知 ID、历史 ID 或范围外显式配方请求被拒绝。无 ID 的旧项目不补默认值，再运行必须显式选择正式配方。
- 开始阶段请求可携带 `{recipe_id,new_seed}`。选择新配方不会覆盖 `prompt_original` / `prompt_approved` 或修改 Seed（除非显式 new_seed）。C0 的 `r34l1sm` 仅由 Fleet 加入执行图；C1 是人像 Realism LoRA，不代表指定新人物。
- 预览重跑将原阶段存入 `stage_history`，新产物以 execution_id 独立命名；历史下载链接继续可用。原比较页及 R0/C0/C1/D4/A8/A4_C05 已发布文件不改动。

## Fleet HTTP 契约

Studio 复用现有 Bearer 密钥认证，不向浏览器暴露密钥。

1. GET `/api/router/options` 的 `recipe_catalog.enabled` 必须严格为 JSON `true`，目录中所选 ID 必须唯一且具有非空字符串 `version`。目录缺失、关闭或网络失败均禁止生成。Studio GET `/api/recipes` 代理此目录；静态选项仅用于离线创建，不代表执行资格。
2. GET `/api/router/capacity` 的 `recipe_capacity` 按 ID 透传白名单字段 `available_slots/eligible_lanes/reasons/recipe_version`。缺失不补零、不推断通道；版本不一致显示“容量未确认”。名额是快照，最终排队和资源准入始终由 Fleet 决定。
3. `FleetClient.submit_stage(..., recipe=..., prepared=...)` 先按 execution_id 对账。仅新执行读取最新目录，然后 POST `/api/router/recipe-workflow`：`{recipe_id,prompt,seed,filename_prefix}`。prompt 来自已批准 IR 原文，seed 和 prefix 与原构图规则一致；Studio 不再构造本地 T8 图用于该请求。
4. 构图响应 `{enabled:true,prompt:graph,recipe_binding:{recipe_id,recipe_version,graph_sha256,...}}`。Studio 检查 ID/版本及规范 JSON SHA256（UTF-8、sort_keys、紧凑分隔符、ensure_ascii=false），保存真实图及 binding，原样 POST `/prompt`。`extra_data.h3` 带 `recipe_id/recipe_version/contract` 及原 execution_id/stage/profile/studio。binding 必须与 Fleet 固定 canonical graph 校验结果一致，Fleet 负责最终严格结构校验及冻结。
5. 构图或本地证据保存失败属于“未提交”，不能标为结果未知。只有 `/prompt` 提交结果不明时沿用 SubmissionUnknown，对同一 execution_id 只查询，不重新构图/提交。对账期间不可换配方或 Seed。
6. GET `/api/jobs/by-execution/{id}` 保留既有任务状态语义；配方任务另读认证 GET `/api/router/executions/{id}`，取实际公开执行字段。阶段 `execution.contract` 保存构图 binding；`execution` 展示 `recipe_id/recipe_version/backend_id/runtime_version/gpu_uuid/execution_seconds`。不从选择器或 GPU 名称猜测实际后端、不把含排队总耗时当成执行秒数。

Studio 原 JSON 项目存储直接持久化新增字段，无数据库结构迁移。认证 Router 项目接口也支持创建和开始阶段透传 recipe_id；原版本化审批保护不变。

## 离线验收

在 Studio 根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider
node --test tests/*.test.cjs
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/recipes_ui_smoke.py
```

所有新增测试使用本地假响应、临时存储或静态前端执行环境；不发网络请求、不调用生成 API、不使用 GPU。真实 Fleet 启用、后端资格、服务端严格图校验及部署后的浏览器联调由主代理另行验收。本改动不部署、不操作服务，也不评画质或选优。

`tests/recipes_ui_smoke.py` 是真实 Chromium 离线 UI 入口：不启动 HTTP 服务，拦截全部浏览器请求并从本地 frontend 提供文件；API 为内存夹具，生成点击只记录假请求。可用 `--chromium /path/to/chrome` 指定已安装浏览器，不安装依赖。
