# 加密训练对话归档

## 目标

AI Router 将所有通过鉴权的 Chat Completions 和 Responses 请求写入独立的
训练数据库。记录采用两阶段写入：

1. 收到请求后立即保存原始请求内容，并标记为 `received`。
2. 路由后补充完整有效上下文、实际模型输入和路由信息。
3. 模型完成后补充完整回答、工具调用、工具结果和 usage，并标记为
   `completed`。
4. 上游错误、客户端断流或 Router 中断的记录仍保留，但标记为不可训练。

## 加密与隔离

- 数据库：`/opt/1panel/ai-router-training/conversations.sqlite3`
- 专用密钥：`/opt/1panel/ai-router-training/training.key`
- 目录权限：`0700`
- 密钥和数据库权限：`0600`
- 对话 JSON 先使用 zlib 压缩，再使用专用 Fernet 密钥加密。
- SQLite 仅保存 HMAC 索引、模型等最少元数据和密文，不保存明文提示词或回答。
- 训练目录只挂载到 `router-api-local` 和 `router-api-tail`。
- 密钥不写入环境变量、Redis、日志、审计或 Git。

首次部署前执行：

```bash
cd "/home/ai/github/1Panel/deploy/ai-router"
sudo "./scripts/install-training-archive.sh"
```

## 保存范围

加密负载包括：

- 原始请求体
- 恢复历史后的完整有效上下文
- 压缩或迁移后实际发送给模型的内容
- 系统、用户和助手消息
- 工具定义、工具调用参数及工具结果
- 图片 URL、Base64 图片及其他多模态内容
- 完整非流式响应或重建后的流式助手输出
- 路由模型、deployment、Token、协议和任务元数据

数据不设置自动过期时间。请求 ID 使用 HMAC 唯一索引，重复请求不会产生重复
训练记录。

## 现有数据回填

首次启动启用了训练归档的 Router API 时，会将当前 Redis 中仍未过期的加密
会话胶囊回填为 `conversation_snapshot`。回填只代表当前仍可恢复的会话快照，
无法恢复已经过期的历史，也无法重新拆分此前每一轮独立请求。

## 状态与导出

查看状态：

```bash
curl --fail \
  -H "Authorization: Bearer <admin-key>" \
  "http://127.0.0.1:4000/internal/training/status"
```

导出可训练记录：

```bash
docker compose run --rm --no-deps router-api-local \
  python -m ai_router.training_export \
  --trainable-only \
  --output /training/exports/training-$(date +%Y%m%d).jsonl
```

导出的 JSONL 是解密后的明文训练数据，文件权限为 `0600`，不得放入 Git 或
通过不受控渠道传输。
