# WorkBuddy H3 连接器包

## 交付范围与状态

本包子交付仅为 `integrations/workbuddy/h3-studio/` 的五个静态包文件、
`tests/test_h3_workbuddy_connector.py` 与本文档，均相对于 `deploy/ai-router/`。
未修改共享 `siyuan-media` Skill、Router、SDK、MCP 服务端、运行配置或凭证。
没有部署、导入 WorkBuddy、调用生产 API 或执行 GPU 推理；不代表线上已可用。

```text
integrations/workbuddy/h3-studio/
├── connector-meta.json
├── mcp.json
├── token-schema.json
├── icon.svg
└── skills/h3-studio/SKILL.md
```

此目录是包源，不是已安装的 WorkBuddy 配置，也不是可独立启动的服务器。
整体代码交付已包含 Control H3 模块和共享 Skill 分流；这些不属于本包的修改范围，
也不由本包专项测试证明其运行效果。SDK、认证授权、工具执行和状态持久化仍在
服务端，不复制进连接器。整体运维交接确认现有线上基础环境已包含 MCP SDK
`1.26.0` 与 `sse-starlette 3.4.11`；本轮复用现有版本，不升级到 `2.2.0`，
避免改变其他功能的依赖。早期 `/tmp/h3-mcp-test-env` 中的 `2.2.0` 测试仅属
探索性证据，不作为最终版本验收。整体隔离测试环境现已切换到 `1.26.0` /
`3.4.11`（版本已只读核验），最终专项复测复用 `/tmp/h3-mcp-test-env`；本包不
安装或升级线上依赖。
本包仍是 staged 待交付工件，任何生产维护必须先提交计划，
待用户明确批准维护窗口后执行。

## 必要格式核验

2026-09-10 核对了 [WorkBuddy 官方连接器文档](https://open.workbuddy.cn/docs/connector)：
MCP 包仅配置一个服务，远程使用 HTTPS；配置枚举拼写为 `streamableHttp`。
`auth_mode: token` 使用 `token-schema.json` 的本机表单，通过同名 `${VAR}`
占位符注入凭证。本包敏感字段为 `password`，无默认值，令牌只注入认证头，
不注入 URL。最低版本设为 `4.24.0`，覆盖 Token 表单和中英文示例字段。
这些是文档格式核验，不是已安装客户端兼容性测试；不承诺客户端凭证静态加密。

连接器标识为 `siyuan-h3-studio`，版本为 `0.1.0`。
预定服务地址为 [H3 MCP](https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3)，
采用 StreamableHTTP，连接超时为 30000 毫秒，而非渲染完成时限。
此次没有访问该地址；DNS、Tailscale 可达性、TLS 证书和工具发现均未验证。

授权部署并完成服务端检查后，用户才通过受支持的 WorkBuddy 连接器分发/安装流程
加载该目录，在本机私密表单填写管理员单独交付的个人 H3 最小权限令牌。
不要把令牌发给助手，不要手工把占位符替换成密钥，不生成含密钥的安装包或截图。
整体收尾已通过只读 SSH 在 `ivan-laptop` 查询
`(Get-Item WorkBuddy.exe).VersionInfo`，现场确认 `ProductVersion=5.5.4.0`、
`FileVersion=5.5.4`。这是已查实的已安装客户端版本，满足本包声明的最低版本，
但不是 UI 兼容性验收：连接器导入入口、原生 Token 表单、认证与自然聊天调用
仍未验收。此次版本检查没有安装软件、修改 MCP 配置或读取凭证；本文不虚构
一键安装命令，也不把版本满足门槛表述为安装或交互成功。
用户补充的只读发现：Windows 顶层 `~/.workbuddy/.mcp.json` 中已有 `ima-mcp`，
使用 `type: http` 与 URL、没有 headers；各连接器的私有表单凭证位于
`connectors/<UUID>/` 下，另有 `credentials/` 文件名证据。本任务未读取凭证内容，
也未改写任何客户端配置。这些证据不能证明远程自动安装、导入成功、凭证可用
或自然聊天调用已验证。不能把其他连接器的 `type: http` 条目视为本包格式依据，
也不能认为修改顶层 `.mcp.json` 会自动导入本包或生成 Token 表单。
不交付绕过原生表单的运维脚本，禁止写入或重建加密凭证库；认证只能通过客户端
原生私密表单完成。客户端内部存储结构不是稳定安装 API。

## 工具与流程契约

工具只有以下九个；具体请求体以运行时自描述 MCP schema 为准，本包不复制服务端 SDK：

| 工具 | 行为 |
| --- | --- |
| `h3_capabilities` | 发现能力、配方和当前限制。 |
| `h3_prompt_guidance` | 提供服务端蒸馏指导，不额外调用服务端 LLM。 |
| `h3_save_draft` | 保存草稿，不生成。 |
| `h3_confirm_prompt` | 精确确认，不启动。 |
| `h3_start_preview` | 经明确授权启动一次预览。 |
| `h3_list_tasks` | 列出本人任务。 |
| `h3_get_task` | 按任务 ID 查询当前状态，或按原操作 ID 恢复精确回执；两者恰好传一个。 |
| `h3_review_preview` | 记录用户针对精确输出/运行的明确通过或拒绝，不执行自动质检。 |
| `h3_cancel_task` | 按明确请求取消指定任务，等待终态证据。 |

规范标识名为 `task_id`、`operation_id`、`expected_revision`、`expected_output_id`；
类型、可空性及工具必填条件取决于当前 schema。无输出时不伪造输出 ID；若必要
审批绑定无法表达，则拒绝调用。每个写动作有独立操作 ID，未知结果先查询核对，
不自动重放；版本或输出改变使旧批准失效。

Studio 当前唯一权威 schema 源是仓库内 `deploy/h3-mcp/studio/connector_api.py` 的
`TOOL_DEFINITIONS`。已检查该模块导入不启动服务、不创建运行实例，专项测试
在隔离环境导入工具定义并校验样例参数；另以纯内存替身测试只读回执查询分支，
不调用安装函数或创建、生成、审阅、取消等写入方法。
不维护或跟踪第二份 JSON schema，也不跳过参数对齐；候选准备流程从同一份
`connector_api.TOOL_DEFINITIONS` 生成部署所需 schema 工件。运行时发现的 schema
仍是请求构造依据。源码对齐不等于 Control 已加载对应生成文件，也不等于远程
工具可调用；候选中的生成文件与部署后发现结果需要各自核验。

当前接口还包含这些不可省略的差异：

- `expected_revision` 是服务端返回的 SHA-256 字符串；确认与启动使用文本工件
  `context_output_id` 作为 `expected_output_id`，并非预览视频 ID。
- 启动另需 `expected_run_id`，只有从未运行才可为 `null`。审阅使用视频
  `output_id`、非空 `expected_run_id` 和用户明确给出的 `decision=approve|reject`，
  不是调用一个自动评估模型。取消绑定对应 `stage_id` 的运行。
- 保存成熟提示词要传 `verbatim: true`；`skill_sources` 只允许 `name`、`source`，
  路径/版本在对话中展示，不能混进请求。来源声明不是执行验证。
- `audio_policy=native` 表示原生声音；`mode=t2v` 是纯文生视频。
  `serial4060` 不属于当前请求参数，必须由服务端执行策略满足。
- 未知写入遵循“保留原 `operation_id` → `h3_get_task` 仅按该操作 ID 查询 →
  核对本人精确操作回执 → 恢复原始结果”。`task_id` 与 `operation_id` 恰好传一个，
  包括创建结果未知且无任务 ID 的情形。缺失、处理中或不匹配仍停在“结果未知”，
  不能换新操作 ID、新建替代任务或按最近任务猜测；原回执与当前任务状态分开处理。
  返回的 `receipt.operation_id`、`receipt.task_id`、`receipt.result_revision` 与
  `receipt.result` 需一致；原始结果在 `receipt.result`，不是随后变化的顶层任务。
  保留本地原请求上下文，不臆造服务端未返回的工具名或请求摘要验证证据。
- 当前源码能力响应已提供配方触发词、人物 LoRA 强度、采样设置与容量信息，
  蒸馏指导包含可用/已选 Skill、规则及规则集摘要。Skill 展示实际返回内容，不把
  声明、目录确认或空闲 lane 等同于已实测能力；实际配方明细、当前容量与
  `serial4060` 执行约束仍需在部署后的能力响应中核对，缺失时停止而不是编造。

Skill 将本地创意指导与服务端工具调用分开：优先使用当前可用且适用的 WorkBuddy
本地创意 Skills；没有适用技能或指导不足才取服务端蒸馏指导，不增加服务端 LLM。
成熟提示词逐字保留；展示原始输入与草稿、真实 Skill 来源及配方明细，才申请审批。
“确认提示词”停在已确认；针对已展示的精确任务/版本/输出说“确认并生成”，
可以先确认、核对回执，再启动一次，不把确认本身实现成启动。

本包范围固定为 `A4`（默认）、`A4_C0`、`A4_C1`、`B8`，
`15s`、`480x864`、`24fps`、`native`、`serial4060`。
配方算法和具体采样设置没有在此硬编码，必须由服务端提供可展示的明细。
`serial4060` 是执行档位约束，不是客户端直接分配 GPU 的指令。
不支持其他模式、第三方生成器、自动质量通过、自动重试、放大、付费，
也不回退旧通用媒体流程；主工程已完成配套共享 Skill 分流，本包不修改该文件。

## Agent Harness 工程约束映射

遵循同级工程的 `docs/agent-harness-engineering-standard.md`：不新增 Agent，
使用 WorkBuddy 单助手原生循环。固定状态操作由 MCP 服务端完成；只有创意整理
需要助手判断，本包不再增加规划模型。以下是语义映射，不是新增 wire schema：

| 可移植概念 | 本包中的映射 |
| --- | --- |
| `AgentCatalog` | `h3-studio` 创意与审批角色、九个工具、默认只读、有限写权限；生成预算一次、写操作自动重试零次、每轮状态查询最多三次。 |
| `TaskEnvelope` | 用户目标、原文与草稿、来源工件、配方、固定模式、任务/版本/输出绑定和本次授权动作。 |
| `CapabilityManifest` | `h3_capabilities` 返回的声明、已验证、当前健康与容量信息；缺失或不满足硬约束则停止。 |
| `ResultEnvelope` | 真实终态或待处理状态、摘要、首个致命证据、工件、完整任务/操作/输出/请求 ID 及可获得的指标；未返回指标标为未测。 |

有界交互遵循 `created -> preflight -> matched -> dispatched -> running -> validating`
及 `completed | failed | needs_context | cancelled` 终态语义；缺少输入/批准返回
`needs_context`。异步渲染仍运行时只报告运行状态与句柄，不伪造终态。
不承诺服务端已实现相同枚举。持久任务、取消资源释放、版本/输出并发保护及
幂等核对必须在整体上线验收中验证认证边界，Skill 指令不是安全强制执行机制。

单助手基线：同一创意只整理一份草稿，明确确认前无生成，明确授权后一次原生
串行预览；不启用多 Agent。未来验收须记录成功率、墙钟时间、调度/排队/推理/
工具/验证耗时、工具与模型调用数、网络往返与字节量、tokens、重试及人工干预。
本次只测包静态契约，不提供未测的性能收益或生成质量结论。

状态查询首次可立即执行，后续默认间隔 10 秒；只读失败后按 20 秒、40 秒退避，
若服务端给出 `Retry-After` 则等待两者中的较长时间。失败查询计入每轮最多三次
上限，不忙等、不追加隐藏请求；达到上限后报告当前状态，等待用户再次查询。
该策略不授权写操作重试、替代任务或后台监控。

## 离线验证与后续验收

仅运行专项测试，不需要密钥、HTTP 端点、服务启动或 GPU：

```bash
cd "/home/ai/github/1Panel/deploy/ai-router"
PYTHONDONTWRITEBYTECODE=1 "/tmp/h3-mcp-test-env/bin/python" -m pytest -o addopts="" -q -p no:cacheprovider "tests/test_h3_workbuddy_connector.py"
```

测试检查精确包文件集合、JSON 可解析及本地维护的官方字段子集约束、最低版本、
单一 HTTPS 传输、Token 占位符与密码表单一致性、无静态凭证、SVG 无活动内容、
Skill 元数据及关键指令契约。负例验证配置篡改与典型秘密形式能被拒绝。
恢复测试还对真实 `ConnectorAPI.call` 的只读分支使用内存 SQLite 回执与隔离的
所有权/任务状态替身，核对原回执与当前版本分离、查无回执/越权/双参数失败且
不产生新操作。它不初始化 Studio 应用，不执行创建、生成或部署。
它们不是 WorkBuddy 官方验证器，也不能证明助手实际遵循指令或检测所有秘密。
canonical schema 对齐仍只证明离线参数契约，不证明远程安装或自然聊天行为。

测试结果以本次最终隔离环境执行报告为准；必须包含参数对齐与未知操作恢复契约，
不能跳过后声称完成。这些测试不代替整体服务测试、桌面验收或真实推理证据。
2026-09-10 本包在 MCP SDK `1.26.0` / `sse-starlette 3.4.11` 环境最终专项结果为
`125 passed`，无跳过；Skill 格式验证通过。这不构成远端 SDK/传输联调验收。

以下是整体上线验收门槛，不表示对应源码尚未实现；执行前须获得明确维护计划与
窗口批准，不能因本包测试通过就启动生产维护：

1. 核验已交付 SDK/服务端实现及候选生成 schema，确保草稿/指导/确认不推理、不额外调用 LLM，
   启动、审阅、取消的精确绑定及账号隔离在服务器上强制执行。
2. MCP 初始化、发现、认证与断线回执核对，拒绝越权/旧版本/旧输出，确认操作
   不启动；确认后版本变化不得悄悄批准另一份草稿。
3. 已安装 WorkBuddy 的导入、Skill 激活、Token 私密表单与 TLS/Tailscale 连通验收；
   检查实际发行渠道是否要求审核以及 `source` 是否冲突。
4. 人工场景验收：本地技能可用/不可用、成熟原文、仅确认/确认并生成、过期批准、
   结果未知、审阅不通过、取消未终结、超范围模式与旧流程不能回退。
5. GPU 推理与真实视频质量验收必须另行授权；本次未执行。服务器健康、排队或
   离线测试通过均不能代替该验收。
