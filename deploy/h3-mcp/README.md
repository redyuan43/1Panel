# WorkBuddy H3 MCP 发布与验收

## 当前多素材修复候选（2026-09-11，尚未上线）

最新实现、测试与剩余上线门禁见 `multimodal-release-20260911.md`。
新增六模式输入绑定、受限本地素材上传、后台按任务启动/排空，以及统一网页审批。
本节之后的 2026-09-10 发布说明是历史基线；不能据此把新增多素材能力宣布已上线。
新版本仍只允许具备对应执行证据的配置正式生成；参考/Hybrid 完整权重当前尚待下载完成。
现有四配方成片证据保留，不重复推理测试。

本模块复用 Studio 的项目、事务、不可变产物和 Fleet 调度。MCP 不保存第二套任务状态，
不调用额外策划模型，不修改显卡、内存或并发保护。
2026-09-10 MCP 与管理员设置已部署；客户端授权状态与生成开放状态必须单独核对，
见 `settings-release-20260910.md`，不能将安装或服务健康等同于客户端已连接。

## 实现组成

- `studio/connector_api.py`：九个工具的唯一 schema 来源、账号归属、草稿、审核和操作回执。
- `studio/fixture_import.py`：仅限离线运维的一次性验收视频导入库函数，不注册 MCP 工具或 HTTP 路由。
- `../ai-router/ai_router/h3_mcp.py`：Control 认证、限流、Streamable HTTP 与产物代理。
- `../ai-router/integrations/workbuddy/h3-studio/`：WorkBuddy 连接器元信息、私密凭证表单和操作 Skill。
- `scripts/prepare_release.py`：从当前不可变发布基线生成新候选，拒绝覆盖、漂移和秘密文件复制。

冻结配方在草稿保存与提交前都校验；执行图准备完成后再次校验绑定，防止排队期间版本变化。
原始提示词保留，修改草稿不会改写历史视频。查询操作 ID 读取同一 Studio 事务回执，
响应丢失不新建任务。确认提示词与开始生成是两个操作；视频审核不会自动启动后续阶段。

## 运行配置

仅 Control 启用以下配置；不向 API 实例增加 MCP。值是配置示例，不包含真实凭证：

```dotenv
AI_ROUTER_H3_MCP_ENABLED=true
AI_ROUTER_H3_MCP_CLIENTS=workbuddy-ivan-h3
AI_ROUTER_H3_MCP_WRITES_ENABLED=false
AI_ROUTER_H3_MCP_GENERATION_ENABLED=false
AI_ROUTER_H3_STUDIO_URL=http://127.0.0.1:14830
AI_ROUTER_H3_CONNECTOR_KEY_FILE=/run/secrets/h3-connector-key
AI_ROUTER_H3_MCP_PUBLIC_ORIGIN=https://ai-x10drg.taild500c8.ts.net:4001
AI_ROUTER_H3_STUDIO_PUBLIC_ORIGIN=https://ai-x10drg.taild500c8.ts.net:8445
```

Studio 对应设置 `H3_CONNECTOR_KEY_FILE` 为服务端专用内部密钥文件，
`H3_CONNECTOR_WRITES_ENABLED=false`、`H3_CONNECTOR_GENERATION_ENABLED=false` 默认关闭。
内部密钥只允许 `/api/router/connector/`，不解锁一般 Studio 或旧 Router 路由。
专用客户端账号必须在 Control allowlist 内并有 `siyuan-video` 权限。
账号校验沿用 Router；不得给客户端管理密钥、内部密钥或 Fleet 密钥。

草稿验收时只打开两侧 writes 开关，generation 保持关闭；清理已知运行任务的取消不受
新接单开关限制。能力返回同时反映 Studio 和 Control 的开关，不把空闲容量等同于可提交。
确认通过后开放 generation 只改变接单能力，不自动创建、恢复或提交任务。

## 发布与回滚

1. 获得本次维护窗口确认，重新核对 Studio/Fleet 运行任务与队列。不得用先前空闲快照代替。
2. 保存当前 Studio 发布路径、运行配置、项目 SQLite 一致性备份、两个 Control 镜像 ID、
   Compose 的全部覆盖配置和私有凭证引用；备份存入持久私有目录，不记录密钥明文。
3. 使用候选准备脚本从**当前线上**基线生成新目录。不得部署工作树或覆盖既有候选。
   依赖锁定线上现有 MCP `1.26.0`、SSE `3.4.11` 与核心包版本，拒绝借接入升级 SDK。
4. 本地 `sha256:...` 镜像 ID 使用命令级 `DOCKER_BUILDKIT=0 docker build --pull=false ...`。
   当前 BuildKit 会把这种本地 ID 当仓库名；不要因此改用未核对的浮动基线。
   候选构建成功仍不是部署成功，必须记录完整镜像 ID 和 `pip check` 结果。
5. 仅切换 Studio 和两个 Control；保留所有覆盖配置、持久项目、旧样片和下载目录。
   Router API、Fleet、GPU worker 与其他模型服务不重启。验收期间关闭 generation。
6. 用户通过 WorkBuddy 支持的连接器安装入口和本机私密表单配置独立 Token；
   不手写其私有加密数据库，不将 Token 放入 ZIP、Skill、聊天或 URL。
7. 实际客户端验证发现九个工具、创建验收草稿、确认、断线后按操作 ID 恢复；不调用启动工具。
   浏览器预览需要原 Studio/Tailscale 身份；凭证只在 MCP 中有效不代表自动登录浏览器。

失败时关闭 MCP 新写操作及 generation，保留查询和既有任务对账。
只停止新策略接单，不删除、不重提、不强行终止已提交的健康任务。
只有确认没有需要新版本对账的运行任务后，才能恢复旧 Studio/Control；
Windows 卸载仅移除该连接器，不覆盖其他 MCP、Skill 或模型配置。

## 测试边界

`tests/` 的 Studio/Fleet 测试使用独立 SQLite、模拟执行和合成字节，封锁网络与生成器子进程；
不把合成字节称为可播放成片或画质证据。协议测试使用真实 MCP SDK，但后端仍为模拟。
候选测试固定导入目录，避免把开发工作树测试误记为候选验证。
不执行 `*_smoke.py`、Windows 真实生成验收命令或 GPU 推理。

明确分别记录：离线回归、候选镜像、生产切换、真实 WorkBuddy 自然语言调用、历史成片证据。
未完成的阶段不能用前一阶段的通过替代；无需重复四配方 GPU 验证。

## 离线验收视频导入

`studio.fixture_import.import_fixture(connector, *, owner, task_id, expected_revision, operation_id,
source_path, source_sha256, authorization_reference)` 是供已授权本地运维包装器调用的库函数。
传入**已有**的 `ConnectorAPI` 和它持有的同一个 `Contract`，不要重新构造 Contract。
导入此模块不会启动 Studio、打开数据库、搜索历史视频或读取模型。现阶段只在隔离临时
Studio 上验证；未授权生产维护窗口前，不得把生产模块、数据库或产物目录传给它。

可执行入口为 `python -m app.fixture_import`，候选准备器只需把该文件作为显式独立运维模块
一并冻结到 `app/fixture_import.py`，**不要**从 Studio 启动入口或 MCP 安装函数自动调用它。
CLI 强制要求 `--execute` 和全部定位参数，无此标志时不加载 Studio 或数据库。示例参数均为
占位值，须在另行授权且数据目录已离线/隔离后填写，不能直接照搬生产路径执行：

```bash
H3_CONNECTOR_WRITES_ENABLED=true \
H3_CONNECTOR_GENERATION_ENABLED=false \
AI_ROUTER_H3_MCP_GENERATION_ENABLED=false \
"/path/to/studio/.venv/bin/python" -m app.fixture_import \
  --execute --data-root "/path/to/isolated-studio-data" \
  --owner "acceptance-client" --task-id "NEW_TASK_ID" \
  --expected-revision "CURRENT_64_HEX_REVISION" --operation-id "fixture-acceptance-001" \
  --source-path "/path/to/explicitly-authorized-source.mp4" \
  --source-sha256 "AUTHORIZED_64_HEX_SHA256" --authorization-reference "APPROVAL_RECORD_ID"
```

工作目录/模块搜索路径须指向冻结候选的包根目录。CLI 在导入 `app.main` 前将
`H3_STUDIO_DATA` 绑定为显式 `--data-root`，并复核加载后的数据目录、已有数据库路径。
`app.main` 导入时已有的 `router_contract.install` 只构造一次 Contract；
`existing_connector(module)` 从 `module.STORE._connect.__self__` 取得该实例并核对 module/store，
不会另建 Contract，不调用 `startup()`、初始化数据库、启动 scheduler 或恢复运行任务。
所选离线数据库必须已经具有 Studio 项目、批次和回执表；本入口不初始化或迁移空库。
不要求 MCP SDK，须使用 Studio 原解释器及其已锁定依赖，不能用 Control 的 SDK 环境替代。

调用前必须人工确认授权文件和目标客户，并使用 `h3_get_task` 取得当前 revision：

- 目标必须属于显式 owner；是 manual 新草稿，context 已批准且与不可变文本输出完全一致。
- preview 从未执行；其他视频阶段全部为 pending，且没有运行 ID、产物、执行记录、取消、
  待对账或批次关联；项目没有草稿编辑史、阶段历史、审核记录或此前的 fixture 导入。
- `H3_CONNECTOR_WRITES_ENABLED=true`；Studio 的 `H3_CONNECTOR_GENERATION_ENABLED` 和
  Control 的 `AI_ROUTER_H3_MCP_GENERATION_ENABLED` 都必须关闭（未设置按关闭处理）。
  库函数只检查本进程环境；未来连接生产数据前，仍须运维确认实际两侧配置与服务均已隔离。
- `source_path` 是管理员明确选定的绝对本地 `.mp4` 普通文件，不接受 URL、符号链接、目录、
  空文件、超过 512 MiB 的文件或目标项目内文件。`source_sha256` 必须事先明确给定；
  `authorization_reference` 是有界安全 ID 形式的授权记录号，不是密钥、路径或授权已自动验证的声明。

导入只复制源文件，不修改、移动或接管源项目。目标独立文件使用排他创建、0600 权限、
SHA-256 与复制前后源属性校验，以及文件/目录同步；新输出、fixture run、项目审计和回执
经同一 Contract 事务提交。普通失败回滚只删除本次创建的文件。进程被强制终止可能留下
**无数据库引用**的孤立文件；它不可通过产物接口读取，也不是成功证据，禁止自动采用或覆盖。

回执与现有工具共用 owner 命名空间：相同 operation_id 和参数返回原回执，不再次读取源；
不同参数冲突。响应丢失时先用现有 `h3_get_task({"operation_id": "..."})` 只读恢复。
只要原项目仍归该 owner，已提交回执可在 writes 暂停后重放；新导入仍受开关约束。

返回的 `preview.fixture_import` 与对应 `outputs[].fixture_import` 明示“导入验收视频，非本次生成”，
包含源哈希、授权记录号、导入时间、绑定 context/output/run ID，并标记
`generated_in_this_task=false`、`media_validation=not_performed`。源路径和 owner 私有审计不向客户返回。
不伪造 Fleet execution ID、GPU、后端、执行配方或耗时；`fixture_run_...` 只表示本次导入绑定。
草稿的配方和时长仍是配置，**不证明源视频按该提示词、配方或规格生成**。

导入后仍需客户通过现有鉴权产物入口实际播放及 Range 下载，并以最新 revision、精确 output ID、
fixture run ID 调用原 `h3_review_preview`。批准/拒绝不会触发生成，编辑草稿也不能允许第二次导入；
该验收项目始终不能调用 `h3_start_preview`，需要真实生成时必须另建项目。fixture 元信息只绑定
对应 output/run，原不可变 fixture 与其来源元信息留在历史里。测试只使用合成字节，不解码或读取真实视频，
因此验证的是协议、隔离、幂等与审核绑定，不是可播放性、生成效果或画质验收。
