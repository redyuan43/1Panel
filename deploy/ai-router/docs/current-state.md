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

## 2026-09-24 WorkBuddy 本轮提示词优化（隔离 worktree 实验）

- 从 ivan 的 `/opt/WorkBuddy/resources/app.asar` 只读核对了 WorkBuddy 增强提示词的两段模板及只发送输入框文本的处理器；实验使用该版本的原模板。
- 控制台新增默认关闭的 `routing.prompt_enhancement.enabled`，只对 `workbuddy-public` 与 `workbuddy-qwen36-shared` 生效。选路及历史身份沿用原文，目标模型确定后最多对当前文本进行一次独立优化调用，模型可见请求副本使用合格改写；长文本、代码、边界不清、目标不支持输出上限或优化失败均发送原文。
- 额外调用独立计入账号速率与云端预算，审计只记录状态、原因、长度、token 与目标，不记录正文。本节记录隔离工作区实现；尚未部署或激活生产开关，真实请求结果以本次验收报告为准。

## 2026-09-24 nx1 视觉启用后的专用路由校准（已部署）

- 08:26:51 的 Home Assistant 请求 `21c1561d55e841a4bab22199d313b5b9` 同时包含图片和 `response_format=json_object`，Router 在候选检查中因 nx1 端点仅登记 `text` 返回 422 `no_compatible_model`，没有调用 nx1。这个请求发生在 nx1 视觉服务 08:27:38 重启之前。
- 08:27:38 后的 nx1 用户服务命令行包含 Ornith 主模型和 mmproj；08:34 后尝试 128K 时因显存不足崩溃，后来重新调整为 96K。最新 `/v1/models` 报告 multimodal、n_ctx=98304、模型 ID 为 `/home/nx/weight/Ornith-1.5-35B-A3B-IQ2_S.gguf`，Router 容器到该服务的健康检查返回 200；原 Router 注册仍是 `text`、旧容器模型路径，端点有效状态仅覆盖 `enabled=true`。
- 一次受控真实请求向 nx1 发送合成红色 PNG、`response_format=json_object`、`max_tokens=128`，返回 HTTP 200，正文是可解析 JSON `{"color":"red"}`，`finish_reason=stop`。这是单图短请求的语义证据，不证明多图、长上下文或所有 JSON 形态。
- 已把 nx1 注册的图片模态、`json_object`、模型路径和 96K 上下文对齐上述证据；定向绑定与账号允许列表不变。基于四个当时运行镜像各自叠加单文件注册表补丁，逐台排空／替换四个 Router 实例；其他 17 个端点的注册配置未改变。两台 API 镜像分别为 `sha256:32283094640597be4632ddaf776792a07db7d618a0cded26dea45bfcaf64e075`、`sha256:306110301e8675c274997ac1e7be321829e36bf5ffc6a7afb92dc7064efa1a08`，两台 Control 均为 `sha256:27fa3cc6f44e8a3ac52fc380c114f467be30866179881af1e4581040b0437c98`。
- 发布后 `home-assistant` 请求 `5b8e37f673b949ad9aefa34b9b448d59`（图片＋`json_object`）选中 nx1 并以 HTTP 200 成功终结，无路由错误。四实例注册哈希一致、设置哈希未变、健康正常、零重启；两台 API 均非 draining，nx1 仍健康并声明 96K。证据见 `outputs/nx1-vision-route-20260924/verification.json`。这是实际选路与请求成功证据；该业务回复的具体内容未在本轮审阅。
- 代码审查后给端点声明增加 `max_images=1`，只开放已实测的单图范围。`nx1-max-images-20260924-r2` 已以原四台运行镜像为基线，仅叠加这一行注册配置并逐台排空替换；运行镜像分别为 API local `sha256:824d7b696baaa184775e47531611a8ebdbedd66745fad96110e3112cab2ffb88`、API tail `sha256:86bf7a609a3bfb5657ec3f37b095f5c7debd86507f7cb4364556efb518b07ca7`、Control 两台 `sha256:0983cd5ed2c8609fe8818bf8c2b54fca3922310e122615258e5d098498609035`。四实例注册／设置哈希一致、健康、零重启，API 均非 draining；两台 API 的真实双图 Chat 和 Responses 请求均在上游前返回 422 `no_compatible_model`，审计记录 `endpoint_id=null`、`upstream=null`。一条单图 Chat＋`json_object` 请求选中 nx1 并返回 200，但 `finish_reason=length`，不据此判断回复内容质量。证据见 `outputs/nx1-max-images-20260924-r2/verification.json`。
- 09:21 和 09:29 的 Home Assistant 双图业务请求被上限 1 阻断，后者 lineage 为 `lineage-0737731cf6714ff08bf8e8ae327b90bf`、请求为 `5bd2727fd536444abd97366591600cec`；当时 nx1 健康且有空闲，拒绝发生在上游前。随后 nx1 直连同请求红／蓝双图，`json_object` 返回 `{"first":"red","second":"blue"}`、`finish_reason=stop`，证明两图短请求的顺序和颜色语义；源码已将上限改为 2、三图仍拒绝。`nx1-two-images-20260924-r3` 已基于前一运行镜像仅叠加该注册表变更，逐台排空并替换四个 Router 实例；注册哈希 `330498c16352963bd0b23ec84ba7edfdc55a2007bb88d2d3289c61df444b306a`，设置哈希不变，四台健康、零重启，两台 API 非 draining。两台 API 的双图 Chat 与 Responses 实路均选中 nx1、返回 200，且正确识别第一张红色、第二张蓝色；三图在候选检查阶段返回 422 `no_compatible_model`，未调用上游。tail 的 Responses 验证期间曾因并发槽位占用返回 429 `model_capacity_busy`，槽位空出后复测成功。证据见 `outputs/nx1-two-image-fix-20260924/verification.json`。
- 11:30:48 的 Home Assistant 请求 `cc649847160e40d8a02232c208c013d3`（lineage `lineage-40c23dc603d64ccb8b6551136b41910b`）含 5 个图片位置、4 张不同的图片，其中一张以相同字节出现两次。接收与有效请求的图片计数均为 5；运行中的两图上限在候选检查处返回 422 `no_compatible_model`，未调用 nx1。随后 nx1 直连同尺寸组合的五张合成图片，`json_object` 正确返回五图颜色顺序，HTTP 200、`finish_reason=stop`、用时 50.5 秒。这只证明该短请求的五图能力，不代表任意尺寸、复杂视觉任务或长上下文质量。源码将上限改为 5，六图仍拒绝。`nx1-five-images-20260924-r4` 从四台上一版运行镜像仅叠加 nx1 注册表变更，逐台排空并替换；注册哈希 `33cca9c5a29e771208aac4683a48731b73a073ac1eb9cf11fbfb778ab077bf61`，设置未变，四台健康、零重启、API 均非 draining。两台 API 的五图 Chat／Responses 实路分别返回 200 且正确识别五图顺序；六图在候选检查处返回 422，未调用上游。tail 一次 Responses 试验因容量占用返回 429，槽位空出后复测成功。发布后 Home Assistant 的五图请求 `1c109b7e8c5c48cb84d50d6f1a3b2d0e` 也选中 nx1，审计为 HTTP 200、归档已完成；未审阅该业务回复内容。证据见 `outputs/nx1-five-image-fix-20260924/verification.json`。
- 审查后的 `a7b2b0894` 只增强五图真实输入形态的回归测试，不改变应用文件。应用户要求，`nx1-five-images-review-20260924-r5` 从四台已验证的 r4 运行镜像仅更新 OCI 版本／审查提交标签，镜像文件层与 r4 完全相同，逐台排空替换四个 Router 实例。四台镜像、注册／设置哈希、健康、零重启均已回读，API 均非 draining；发布后五图 Chat 实路选中 nx1、HTTP 200、`finish_reason=stop` 且正确识别五图顺序，六图 Chat／Responses 在上游前返回 422。运行证据见 `outputs/nx1-five-image-review-20260924/release-r5/verification.json`。
- 此外，单实例 `max_concurrency=1` 曾使 check-boards 并发请求返回 429 `model_capacity_busy`，服务不可用期间还有 503 `unhealthy_or_stale`。放开图片能力不解决这两类错误。

## 2026-09-24 设置局部更新覆盖全量配置（已恢复并部署修复）

- 2026-09-23 23:36 +08:00，管理接口收到只包含 `routing` 的 `PUT /api/settings`，却将它当作完整运行设置写入。`identity.enabled` 因覆盖退回默认 `false`，公开模型请求在选路前返回 `503 public model identity is unavailable`；同次覆盖还使 cloud、failover 等运行设置退回默认值。请求 `d8085405fe3549e6aff207fba7076ab2` 属于这一故障窗口，未产生路由轨迹。
- 从策略修订 18 的更新前快照恢复其他设置，保留修订 19 中 `routing.client_route_bindings` 的变化；恢复后为修订 20。四个运行实例读取的配置哈希一致，`identity.enabled=true`。这证明配置已恢复，不等于已验证该 WorkBuddy 请求成功完成推理。
- 管理接口先将提交的可编辑字段合并进现有运行覆盖项，再验证和写入；未提交的字段保留原覆盖或继续继承默认值。审计中的 `sections` 仍只记录本次提交字段。回归测试覆盖 `home-assistant` 与 `check-boards` 两个独立账号共同绑定 Ornith、公开账号不受绑定影响，以及路由局部更新保留身份和云端配置。`client_route_bindings` 是列表，提交该字段时必须提供完整列表；列表内容按整体替换。
- R1 已逐台发布到两台 Control（镜像 `sha256:4da0bfd290adacfd9777a6cf6f901853d1c680fa468adb2ec44be7f0c006d3ad`）；兼容修订 R2 又从 R1 镜像叠加窄补丁并逐台发布（镜像 `sha256:bd2a175b43796f017713617f384e79e22726efd360b1304074ee9fd0c3fa6d32`）。两台 Control 的源码哈希、配置哈希、健康、零重启次数和两条绑定已回读；详见 `outputs/settings-partial-update-20260924/release-r2/verification.json`。API 与模型后端未替换，未重放报错请求或调用真实模型。

## 2026-09-23 接单候选现场发布（中止，恢复基线）

- 实现提交 `fa1b5a1fec`，未推送；精确生产基础镜像上仅叠加接单补丁，
  local/tail 兼容关闭版本曾逐个排空部署，功能没有启用，归档 worker 未替换。
- V100 候选在关闭功能的启动阶段先后暴露打包依赖遗漏及 LMCache RPC 参数
  类型声明遗漏。后端未进入候选模型推理，故中止验收并恢复两端原版。
  后端注册问题已离线修复，不代表重新上线或性能验收通过。
- 只执行 5 次合成后端基线请求，无付费云端调用；精确 Router 候选源码
  88 项离线测试通过。原有扩大集合的端点数量断言失败没有通过改无关代码绕过。
- 最终运行状态以部署仓库
  `outputs/prefill-admission-review-20260923-r5/final-verification.json` 为准；
  新接单路径保持关闭，重新发布前须重建配套候选并完成启动与性能门禁。
  下方“未提交/未部署”节保留为本轮之前的离线快照。

## 2026-09-23 缓存感知接单分流（离线候选，默认关闭，未部署）

- 上线前 review 修复：重试去重分别使用物理组和 deployment 命名空间，避免合法
  云端 ID 与组名重名时被误判为已尝试；启动时严格检查组配置形状，能力响应不是
  JSON 对象时按版本契约不匹配处理，禁止推理而不抛出意外 AttributeError。
  后端同时修复已完成取消清理被误记为超时、导致持续拒绝接单的问题。均有离线
  回归，开关仍关闭；尚未提交、重启或部署，不代表真实性能门槛已经通过。
- 本轮扩大回归 258 项通过、1 项原有端点数量断言失败；补充能力响应回归后策略
  模块 25 项通过。后端 177 项、Observer 22 项通过，均为离线测试。
- `prefill_admission.enabled` 默认 false，`groups` 默认空。开发代码按物理服务组
  共享原子容量租约；cache 组可保留六路，idle 组只允许一路，别名不能扩大容量。
- cache 目标须直连并通过版本、启用状态、服务组、容量和查询取消协议校验；只有带匹配能力标记
  的 HTTP 409 / `prefill_admission_busy` 才按正常容量事件处理，释放预算/租约，
  排除该组。显式目标、绑定/指令或已输出请求不得依此透明换模型。
- 已补齐后端固定源码的 scheduler/LMCache/HTTP 接入，明确取消查询后释放实际持有
  的读锁；支持 ACK 丢失重试，无法确认的原始提交保留有界栅栏并停止新增接单，
  需要显式缓存恢复，不能丢弃状态或重复解锁。不是实际 GPU 性能验收结论。
- 新路径只启用已登记的直接本地端点，未登记设备不计可用容量；固定目标的全部
  硬约束仍执行。拒绝后同时排除静态 endpoint 与物理组 deployment，优先再找兼容
  本地；固定目标、暗语/绑定/pin 或已经输出正文/工具的请求不得透明换模型。
- 每物理目标至多 dispatch 一次；共享 pre-output 窗口取内部 HTTP 客户端 read timeout
  和现有租约 TTL 的较小值，重选不重置容量等待、能力探测和 dispatch 的时间预算。
  内部 trace 记录组、拒绝及耗时；后端记录实际缓存与剩余量，不存正文或 token ID。
- 启动/端点配置更新和 dispatch 前验证匹配的 `cache-prefill-admission-v1` 后端，
  其实际 engine 必须已开启保护，并报告 `lookup-admission-v1`；缺接口、错组、容量
  或版本不匹配会拒绝启用/推理。不能仅依靠配置声称后端支持。开关切换需要重建
  Router 实例，不能把写入 settings 当作已改变在运行的 scheduler。
- 离线验证：专项 94 项通过（含 Chat/Responses、流式/非流式、本地重选、云端替身、
  local-only、固定模型/绑定、输出后禁止切换、租约归零及健康不降级）；核心 159 项
  通过、1 项原有失败（端点数断言 14，实际 15）。线程/归档用例在正常离线线程环境
  通过；没有改归档代码绕过前一次沙箱等待问题。未调用真实模型。
- 配置示意：`groups.v100={mode: cache, capacity: 6, deployments: [ai-qwen38-27b]}`；
  其余已经接入并通过健康门禁的 AMD/Edge/16G 端点使用对应物理组、`mode: idle`、
  `capacity: 1`。同 GPU 的别名必须在同一组；不把设备清单当成实际可用槽位。
- 后端候选由独立 qwen38-vllm-deploy 仓库的 `build-prefill-admission-candidate.py`
  从 17 个精确基础文件生成，需同时部署匹配 vLLM 和 LMCache 包；保留 TP4/MTP2、
  模型、KV 格式及预算，不沿用已取消的暂停 prefill 实验。先交付关闭功能的组合，
  未获现场性能门槛证明不得正式开启。
- 未部署、未重启、未发送真实或付费调用、未提交或推送。物理组键改变前必须协调
  排空相关 API；不能让旧部署租约键和新组键同时接流。关闭功能保持现有路径。
- 回滚先排空，再同步恢复 Router/后端开关及旧租约键；不能用仍返回新 409 的后端
  配不认识该契约的旧 Router 接流。现场验收仍需单独授权，最多 24 次合成请求、
  45 分钟含恢复，不用用户正文或付费云端；双路总完成时间退化 ≤10%，短增量加入后
  原 decode 保留 ≥80%、最大有效输出间隔 ≤1 秒，否则保持关闭并报告取舍。

## 2026-09-21 固定端点链路优化（已部署）

- 新增可信固定端点意图，只接受已认证的提示词指令、内部客户端绑定，以及只映射到
  一个 responder 的显式模型；普通 `auto`、多候选别名和管理员会话 pin 仍走原流程。
  客户端请求 header 不能直接形成固定端点意图。
- `AI_ROUTER_DIRECTED_FAST_PATH_ENABLED` 默认关闭。开启后，固定端点请求仍执行工具、
  代码、模态、结构化输出、历史、上下文、预算、健康和容量判断，但不调用外部分流
  分类模型。候选投影和 token 计算只处理固定目标，最终准备复用同一结果；正文发生
  变化或压缩后指纹不一致时重新投影和计数。普通路由仍计算全部候选。
- `AI_ROUTER_DIRECTED_ASYNC_ARCHIVE_ENABLED` 默认关闭，并且只有现有 Redis 加密 outbox
  已开启时才生效。固定端点事件先交给进程内后台 worker，再沿用原 outbox 格式；上限
  为 64 个在途事件、128 MiB 去重后的规范化正文，关闭冲刷期限 120 秒。队列过载、
  worker 异常或关闭期间会等待同一 request token 的先前事件。Redis 入队使用稳定事件
  ID 和原子去重，回包结果未知时原事件留在队首安全重试，不越过后续事件；队列无法
  接收时转回原同步路径。关闭期限内仍不能冲刷会清理本机 Future、增加失败计数并让
  shutdown 明确失败，不再伪装成成功关闭；非固定端点保持原同步入队行为。
- 内容观测保留已经生成的规范化 JSON 字节和 SHA-256，后台归档直接复用；调用方后续
  修改对象不会改变已接收快照；确认是普通同步路由后立即释放这些额外字节。普通
  `auto` 不保留逐端点投影副本，固定目标才缓存并复用一份投影。实例状态增加后台事件
  数、正文字节、worker 健康、幂等重试、同步回退和冲刷失败计数；route trace 增加快
  路径来源、分类跳过、候选数、投影复用与实际归档模式，发生后台重试或同步回退时
  立即更新，不记录正文、reasoning、token ID 或凭证。
- 代码提交为 `9942dc7e5`。2026-09-21 按 local → tail 顺序排空并滚动替换两套 API；
  local 镜像为 `1panel-ai-router-directed-api-local:20260921-r1`
  （`sha256:714bcbf0b8b7457b23118e522d834e6a993c9c778a6a41f7640c9214f81d6b63`），
  tail 镜像为 `1panel-ai-router-directed-api-tail:20260921-r1`
  （`sha256:c4b54d156aa03c8d10fce6b105000be13a4695b74e4d62cb60939b62bef5774b`）。
  两实例均为新 boot、未排空、活动请求 0、重启计数 0；两个新开关和原 Redis outbox
  开关均为 true。后台 worker 健康，积压字节/事件、同步回退、worker 重试和冲刷失败
  均为 0。archive-worker 与 Redis 容器和镜像未变化。
- 本次没有发送真实模型请求，也没有生产性能 A/B；因此部署证据只覆盖进程、源码、
  配置和后台队列健康，不能据此声称真实请求延迟已经下降或普通自动路由线上等价。
- 首轮离线受影响集合 429 项通过；本轮 review 修复后，固定链路 25 项及路由、上下文、
  历史和工具协议扩展集合 247 项通过。用例覆盖 Chat/Responses、流式/非流式、分类调用
  边界、普通路由不保留候选缓存、投影失效、存档逐字段等价、快照隔离、幂等重试、
  过载回退和关闭失败清理。宿主缺少 `requirements-test-archive.txt` 中的 `redis`、
  `fakeredis` 及异步插件，因此原 Redis outbox 集成测试仍未在本轮重跑；没有安装依赖。
  旧基线中注册表 15 个端点与固定断言 14 的无关失败未通过修改策略掩盖。
- 同机 CPU 合成计数路径各运行 4 组：5.5 KB 普通 auto 为 10 个候选、p50/p90
  1.629/2.859 ms，固定端点为 1 个候选、0.344/0.402 ms；3.46 MB 分别为
  2.192/2.397 ms 与 0.496/0.607 ms。测试复用内容观测阶段已有指纹，普通路由沿用共享
  token 估算且不持有候选正文，固定路由仅保留一个目标投影。该数据只验证候选阶段，不包含
  上游、Redis 或真实模型延迟，不能替代发布后的 ABBA 验收。

## 证据边界

本页同时描述 Git、未提交工作区和生产运行镜像，三者不得混用。动态模型 ID、窗口、
阈值和端点以 [默认策略](../config/defaults.yaml)、[模型注册表](../config/registry.yaml)
及运行时覆盖为准。

本次生产核验只读取容器、镜像、源码哈希和脱敏后的运行时目标设置；没有调用真实
模型、修改设置、drain、重启或部署。

## 请求记录与健康证据（2026-09-21 09:59 UTC+8，已部署）

版本 `request-health-20260921-r1` 已按各自运行镜像叠加限定补丁，滚动部署到两套
API 与两套管理端。镜像、源码、环境、挂载、健康及管理端实际页面资源均已核验，
四实例重启计数为 0；模型、Redis 和存档消费者等其他容器未变化。API 候选 113 项
测试通过，管理端 112 项通过、1 项旧缓存状态失败在未改基线复现；11 项 Node 与
隔离浏览器验证通过。未调用真实模型或注入故障，未改变路由策略。
发布镜像、基线 revision、验证与回滚工件见
[部署记录](../../../outputs/request-health-20260921-r1/README.md)。

- 请求页会话内的历史按新到旧排列；路由审计时间线继续按时间正序。展开的会话随
  页面刷新，已加载的旧页中仍在运行的请求按 ID 补查终态；分页、展开状态和滚动
  位置保留，过期响应不能把成功记录改回运行中。
  新旧页没有交集时仍保留旧记录，并从新页游标补齐中间缺口，不停止补查旧请求终态。
- 列表与单条详情增加可选 `token_summary`，分别展示入口估算、目标输入计数、
  上游实测输入/输出，以及目标输入加预留输出与窗口的比较。目标计数、窗口和
  实测值只使用该次尝试的历史证据，不读取今天的注册表来解释过去的请求。
  缺少证据显示未知、待返回或未提供，不把默认值零当作实测。
  调度完成后增加 `deployment_finalized` 快照；窗口必须匹配最终端点与部署 ID。
  旧记录仅有其他部署的窗口时显示未知，不用端点最大窗口代替。
- 管理员接口 `GET /api/route-traces` 支持重复参数 `request_ids`，每批 1–100 个，
  与游标互斥，其他过滤条件仍生效；超出边界返回 400 `invalid_trace_filter`。
  旧字段与原分页接口保留，没有数据库结构变更。
- 健康探测增加唯一 `probe_id`、实例/启动标识、起止时间、耗时，以及异常类型、
  异常链类型、失败阶段和 HTTP 状态码。失败与本实例观察到的恢复分别写入已有
  审计文件的 `health_probe_failed` / `health_probe_recovered` 事件。持续健康不
  重复写事件；恢复关联上一次失败编号。新事件不包含响应正文、密钥、URL 或
  原始异常消息；审计写入异常不改变健康判定。写盘使用单独的守护线程和最多
  256 条待写队列，不阻塞探测，也不占用默认线程池；与请求审计使用不同的写入锁。
  队列满时丢弃新事件并计数，
  写入线程恢复后输出丢弃告警；请求候选快照仍保存本次探测证据。正常关闭等待
  最多一秒排空，进程退出或持续磁盘阻塞时，未落盘事件不保证保留。
- 路由候选快照保存所用探测证据及其年龄，管理页区分 DNS/TLS、连接池、连接、
  读取、鉴权、限流、服务端异常与单纯记录过期。旧记录没有留下原因时明确显示
  原因未留存。本次不调整探测次数、超时、健康门槛、换模型策略或历史处理。
- 验证入口：`tests/test_health_evidence.py`、`tests/test_request_view_contract.py`、
  `tests/test_request_refresh.cjs`、`tests/browser_request_refresh.cjs`。浏览器夹具
  拦截全部网络请求，只加载本地静态文件和合成记录，不监听端口、不调用生产或
  真实模型。旧 DeepSeek 异常的详细证据未留存，根因仍不能据此倒推。

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

## 零有效输出恢复（2026-09-20 20:45 UTC+8 已部署）

新增受特性开关和客户端白名单约束的 `invalid_upstream_response` 恢复路径。默认配置
关闭；目标启用范围为 `workbuddy-public`。仅自动路由、本地端点在尚未向客户端输出任何
正文、推理、拒绝或工具调用时进入恢复：先原端点重试一次，仍为空时按请求进入时已经
解析的北京时间窗口选择云端 Flash（工作时段 GLM 5.3 Flash，非工作时段 DeepSeek
V4.1 Flash）。显式 seed 跳过原端点重试，直接进入时段云端候选。

单次请求最多三次推理尝试。显式模型、提示指令、会话 pin、`local_only`、普通 4xx、
传输中断以及已经输出有效内容的流不会进入该恢复路径；失败端点仅在本请求内排除，
不会写共享 cooldown。云端仍须通过上下文、模态、工具历史、授权、隐私和预算硬门禁。
恢复成功后沿用普通会话亲和，后续请求保持云端，不因时间窗口变化自动迁回本地。

流式实现只在白名单请求的首个有效公开 SSE 片段之前探测失败；有效片段一旦出现即
恢复普通透传，因此不会重放已公开文本或工具调用。Chat/Responses 的流式与非流式
隔离测试覆盖原端点重试、时段云端兜底、显式 seed、显式模型、开关关闭、部分流输出
和云端会话亲和。

生产运行时已将 `failover.empty_output_recovery.enabled` 设为 `true`，白名单仅含
`workbuddy-public`。API Local 镜像 ID 为
`sha256:7cdd05bf94e70516a8fe3ba08f0ab86e84acd04f9c53d1aa5f9c78bf25c7bcaa`，
API Tail 为
`sha256:986aa9646f7f787808f62218e15214be3e85635e331a266dd8079f12224d39e8`；
两者补丁哈希均为
`c58d87447f752c47914aad40ed4746a26ecdcee1809dea9664d56c506ab00d36`。
发布时逐实例排空并恢复接单，健康、运行配置和三处源码哈希核验通过，重启计数为零；
未发送真实模型请求。发布、回滚、配置备份和验证记录位于
`outputs/empty-output-recovery-20260920/`。

## 大上下文与压缩

**工作区实现、尚未在生产 API 镜像发现**：当显式模型、提示指令或其他定向选择把
请求约束到上下文不足的目标时，Router 不再自动压缩并继续请求，而是在推理前返回：

- HTTP `422`
- 业务错误码 `context_too_large_for_selected_model`
- 面向客户端的建议：切换更大上下文模型、由客户端压缩，或开始新会话

自动路由仍可在账号权限、上下文策略和目标能力均允许时使用现有压缩机制。该改动的
实现证据在 `ai_router/policy.py`、`ai_router/api.py`、`ai_router/errors.py`，目标测试在
`tests/test_context_policy.py`。在完成候选镜像验证和明确部署前，不得描述为生产已生效。

## 身份拦截输入边界（2026-09-20，工作区实现、尚未部署）

身份披露拦截只检查本轮原始用户问题。WorkBuddy 后移的动态上下文、工具说明、工作区
记忆和历史回复不参与判断；后续轮次仍可检查本轮明确的内部信息披露请求。判定依据是
请求目标，而不是是否出现“模型、节点、Router”等词；翻译、引用、代码、日志、公开
模型比较和用户自有基础设施问题正常路由。输出侧只做内部标识脱敏，不以回复内容触发
身份拦截。

分类器先执行低成本目标判断，长输入只在明确服务目标附近建立有界证据窗口，避免同步
正则阻塞请求路径；长度本身既不是放行条件，也不是拦截条件。翻译或改写豁免仅覆盖其
文本载荷，同一消息中后续独立的身份披露请求仍会拦截。shadow reviewer 同样只接收
本轮 `current_query`，不发送历史或路由处理后的动态上下文。

目标测试和身份、隐私、WorkBuddy、Router 核心扩大回归均已执行；受影响用例通过，
扩大回归仍有一个无关既有失败：注册表实际为 15 个端点，旧测试固定断言 14。该修改
尚未构建候选镜像、部署或发送真实模型请求。

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
