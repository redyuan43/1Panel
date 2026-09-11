# GLM 5.3 / DeepSeek V4 Flash 定向暗语部署验收

## 当前交付状态

2026-09-09 已按用户确认部署，策略 r4 已激活。两个 API、两个 Control 和 LiteLLM 已更新。镜像以各服务原运行镜像为基线，仅覆盖本次文件/代码片段，未提交或推送 Git。工作区其他并行修改未带入此次部署。

| 规则 | 暗语 | 端点 | 上游模型 |
| --- | --- | --- | --- |
| tianshu | 按天枢协议处理 | zhipu-glm-5.3 | glm-5.3 |
| liuguang | 按流光协议处理 | cloud-deepseek-v4-flash | deepseek-v4-flash |

现有 Astra 数字暗语、GLM Flash 玉衡规则及其他定向不变。r3 → r4 仅增加两条规则、暗语配置修订号，以及云端白名单中的 `zhipu/glm-5.3`。新条目经控制接口保存草稿、离线验证和差异检查后激活；没有修改默认配置绕过策略激活。

另通过端点管理启用 GLM 5.3，端点配置版本 36 → 37；它保持 `auto_candidate=false`，不进入普通 Auto 候选。registry 文件的禁用默认值由持久端点覆盖启用，运行状态不能只根据 YAML 默认值判断。

## 接口及联网依赖

GLM 5.3 使用既有智谱 Coding 通道：

- Base URL：`https://open.bigmodel.cn/api/coding/paas/v4`
- Chat 请求：`POST /chat/completions`，上游模型 `glm-5.3`。
- 凭据引用：`AI_ROUTER_GLM_API_KEY`，不新增或输出密钥。
- Router 的 Responses 请求使用已有 Chat 适配器；不假设 Coding 账户具有原生 Responses 权限。
- 仅文本；必须保持思考开启，`reasoning_effort` 支持 `low/high/max`。不支持 `thinking.type=disabled`，也不能照搬其他模型的 `medium/xhigh`。
- 新端点的 registry 默认禁用，运行时通过端点管理启用，非 Auto 候选；32K 是保守配置，尚未进行边界实测，1M 为官方声明。已完成当前账户的短文本、工具调用生成和 Responses 流式实测，不代表完整长上下文或所有工具组合均已验证。

DeepSeek V4 Flash 沿用当前已注册通道：

- Base URL：`https://api.deepseek.com`
- Chat 请求：`POST /chat/completions`，上游模型 `deepseek-v4-flash`。
- 凭据引用：`AI_ROUTER_DEEPSEEK_API_KEY`。

2026-09-09 从 API 容器检查：两个域名 DNS 解析及 TLS 1.3 证书校验成功；容器未配置 HTTP/SOCKS 代理变量；两个凭据变量存在。随后通过 Router 完成真实推理，确认当次调用可用；这不保证未来余额、限额或供应商可用性。

实测发现当前 LiteLLM 1.84.0 的 OpenAI 模型识别会拒绝顶层 `reasoning_effort`（首个失败请求 `8bbabd4966054b6889e09028ec8ecde9`）。已仅对 GLM 5.3 和 DeepSeek V4 Flash 增加元数据开关，将该参数经 `extra_body` 原值透传，没有开启全局 `drop_params`。Chat 与 Responses-to-Chat 路径均有回归测试；24 项相关测试通过。

这两个模型均依赖 AI 主机访问供应商公网 HTTPS。暗语解析本身在 Router 本地执行，断网时仍可能识别暗语，但无法完成云端推理。定向目标不可用时返回错误，不切换到另一个模型；会话定向与全局规则配置分别保存。

## 部署与验收范围

1. 以运行镜像为基线组装仅包含本次变更的候选，排除当前工作区其他并行修改；保留当前策略、端点覆盖及镜像记录。
2. 新增 GLM 5.3 需要同时确保 Router registry 与 LiteLLM 模型映射一致，不能只改前端暗语。
3. 经确认执行最小账户/文本请求验收，通过 Router 调用；记录完整 request ID、实际端点、上游模型、最终输出及错误证据。失败时保留原始错误，不自动换供应商或模型。
4. 验证 Responses 转 Chat、流式和工具调用；只启用已验证能力。GLM 5.3 保持非 Auto 候选，不进入普通自动路由。
5. 部署涉及 API、Control；若使用现有 LiteLLM 转发路径，必须同步其模型配置并评估重启中断。无需重启 GPU 模型、Redis 或 Codex Adapter。
6. 以届时最新激活版本为基线，仅添加上述两条暗语，完成草稿验证、差异复核和激活；不预先假设下一版本一定为 r4。
7. 用两条暗语各完成一次真实 Router 请求，验证会话保持与恢复常规模式。离线模拟不可用故障，不主动断开生产网络。

## 真实请求证据

| 检查 | Router request ID | 结果 |
| --- | --- | --- |
| 天枢 → GLM 5.3 | `bc29404f8bec4e9599ee1cee215c1a18` | `DIRECTIVE_OK`，trace succeeded |
| GLM 会话保持 | `829d2080dfae401a9fbf0a54b14b584a` | `PIN_OK`，route_directive_affinity |
| 流光 → DeepSeek Flash | `c697d9072e934b5689a92cf12dfa3ec3` | `DIRECTIVE_OK`，trace succeeded |
| DeepSeek 会话保持 | `5d7d0c0bc44a46c89287aa63165ec35d` | `PIN_OK`，route_directive_affinity |
| Tailscale API 流光 | `84830aed43f04b1181887756e13c12d1` | `DIRECTIVE_OK` |
| GLM Responses 流式 | `78b1baf12d46445eba3066b5affca0fd` | adapter，response.completed，RESPONSES_OK |
| DeepSeek Responses 流式 | `6e0de225663743f5af4ffa445535dd14` | native，response.completed，RESPONSES_OK |
| GLM 工具调用生成 | `5824ce0f19b943928038c5e2162bda62` | report_result(status=OK) |
| 清除天枢定向 | `da6bbb7e436142a2bc2baeee85b3332a` | 原会话显式选 DeepSeek，RESET_OK，explicit_model |

真实浏览器读取控制台确认：`当前激活 r4 · 无草稿`；GLM 5.3 与 GLM 5.3 Flash 为两行；两个新增暗语及 Astra `357742` 均正确。四个 Router 容器的五个变更文件逐一核对 SHA-256 一致。

验收工件目录：`/home/ai/.local/state/ai-router-acceptance/20260909-directives/`。包含 `baseline.json`、精确补丁、Dockerfile、Compose 覆盖、原始请求响应、路由追踪、`deployed-hashes.json`、`ui-check.json` 和 `final-verification.json`。复部署应沿用 `compose-command.json` 中的完整覆盖链，避免遗漏此前已上线功能；正式状态来源仍为 `/opt/1panel/ai-router` 持久目录。

## 官方依据

- https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3
- https://docs.bigmodel.cn/cn/coding-plan/quick-start
- https://api-docs.deepseek.com/quick_start/pricing/
