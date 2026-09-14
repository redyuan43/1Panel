# 请求路由诊断

`scripts/diagnose-prompt-route.py` 根据 Router 请求 ID 解释选模、迁移和拒绝原因。
默认只以 SQLite `mode=ro` 读取路由审计，不调用模型、不修改策略，也不自动重试。

在 Router 容器内诊断已有请求：

```bash
python -B /app/scripts/diagnose-prompt-route.py \
  --request-id "完整请求ID" \
  --report "/data/diagnostics/route-完整请求ID.json"
```

报告只包含请求 ID、模型和端点、Token 数、候选资格、拒绝代码及生成的因果说明。
它不会包含提示词、消息正文、凭据、密钥 ID 或上游错误正文。报告以 `0600` 创建，且拒绝
覆盖已有文件。退出码 `0` 表示找到审计记录，`1` 表示未找到，`2` 表示参数或读取错误。

只有需要一次真实冒烟时才使用：

```bash
python -B /app/scripts/diagnose-prompt-route.py \
  --live-short \
  --base-url "http://127.0.0.1:4000" \
  --model "siyuan/auto" \
  --report "/data/diagnostics/route-live-时间戳.json"
```

`--live-short` 从 `AI_ROUTER_1PANEL_API_KEY` 读取现有凭据，只发送一次固定、无工具、
最多 32 Token 的短请求；HTTP 客户端不重试、不跟随重定向。之后最多轮询十次只读审计，
不会重发推理请求。只有 HTTP 200、固定答案匹配、审计状态成功且
`route_selected=true` 时，才报告 `inference_verified`。

工具应从固定版本的 Router 镜像或发布目录运行。报告中的
`source.route_diagnosis_sha256` 和可选的 `AI_ROUTER_RELEASE_REVISION` 用于核对实现版本。
