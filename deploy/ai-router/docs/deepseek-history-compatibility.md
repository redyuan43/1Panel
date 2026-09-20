# DeepSeek 工具续接兼容

## 行为

- 接收本地 vLLM 的 `reasoning` 和 DeepSeek 的 `reasoning_content`，在私有历史中统一保存为 `reasoning_content`。若两个字段同时存在，优先使用非空的标准字段，不拼接重复内容。
- 跨模型归一化保留 DeepSeek 可接收的真实思考内容；不改正文、工具名、参数或工具结果。
- DeepSeek Chat 工具请求在最终发送前补齐 assistant 的思考字段：有真实内容就保留；历史中确实未提供内容时发送空字符串，表示没有记录。不会生成虚构思考，不会关闭思考模式。
- 同样覆盖 Responses 转 Chat 的适配路径；其他供应商不补齐空字段。
- 原生 Responses 是不同协议：真实 `reasoning` item 的 `content` 会保留，但不会补充未经该接口接受的空思考项，也不会偷偷转换协议或关闭思考。现场验证该接口缺少历史 `reasoning_text` 时仍可能返回 400，此限制不属于已验证的 WorkBuddy Chat 修复范围。
- 审计记录 `deepseek_history_compatibility` 的别名转换和缺失字段数量，不记录思考原文。
- 若出现流式空输出或中断，已有加密归档会保存最多 64 KiB 的上游 SSE 尾部及截断标识，仅供排障，不作为成功响应或训练样本，不写入普通日志。

## 2026-09-19 验证

- 已安装的 vLLM 实际启动参数为 `--default-chat-template-kwargs {"enable_thinking":false}`，故本地历史没有思考记录可以是正常行为。字段漏读是独立的已复现兼容缺陷，不应把所有缺失都归因于它。
- 用户授权后，通过 Router 发送合成 DeepSeek 工具续接测试：省略思考字段返回 400；空字符串返回 200、正确答案 `7`，同时产生新的思考内容（44 字符），证明无需关闭思考模式。成功请求 `6d419dda3c4a4043bb131190212084e3`。
- 本地原请求复现 `37acf799b701414f91f05d79463e72af`：72.99 秒、HTTP 200、正常工具调用，未执行任何返回工具。
- 原空输出请求 `de235eb41edf496f8e8df8ab58dde98c` 的原始 SSE 未留存。复现未重现空输出，不能声称该历史事件的模型/解析器根因已经确定。
- 57 项兼容和流式回归通过。扩大回归在补充留证测试前为 261 通过、1 失败：`test_settings_and_registry_load` 写死 14 个端点，当前注册表有 15 个；本次未修改该注册表或断言。
- 未部署、未重启、未提交。工作区中其他审核相关改动不属于本次修复。

## 部署前审查补充

- 修复了 Responses 项归一化漏掉 `reasoning` 别名，以及 DeepSeek 原生 `reasoning.content` 被丢弃的问题。
- 失败留证增加 `stream_format`：原生流和 Responses 适配后的流明确区分，避免把转换后内容误认作上游原始流。
- 最终部署快照的针对性回归为 60 项通过；部署记录位于 `outputs/reasoning-compat-deploy-20260919/`。是否已上线以该目录验收记录及运行镜像为准。

参考：<https://api-docs.deepseek.com/guides/thinking_mode/>
