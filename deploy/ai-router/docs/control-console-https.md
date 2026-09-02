# AI Router 管理台 HTTPS

## 访问地址

- Tailnet 管理台：`https://ai-x10drg.taild500c8.ts.net:4001`
- AI 主机本地管理台：`http://127.0.0.1:4001`

Tailnet 地址直接由 Router Control 进程在原端口 `4001` 提供 TLS，不经过额外反向代理。端口 `4000` 的 OpenAI 兼容 API 地址保持不变。

## 证书

- 证书：`/opt/1panel/ai-router-control-tls/ai-x10drg.taild500c8.ts.net.crt`
- 私钥：`/opt/1panel/ai-router-control-tls/ai-x10drg.taild500c8.ts.net.key`
- 私钥权限：`0600`
- 证书和私钥归属：Router 容器用户 `10001:10001`

不要将证书私钥、管理密钥或客户端 API Key 写入 Git。
TLS 目录位于 Router 共享数据目录之外，并且只以只读方式挂载到
`router-control-tail`，其他 Router、LiteLLM 和 Codex 容器不可读取私钥。

## 初始化与自动续期

首次部署或迁移到新主机时，在启动 Compose 服务前执行：

```bash
cd "/home/ai/github/1Panel/deploy/ai-router"
sudo "./scripts/install-tail-control-tls.sh"
docker compose up -d
```

安装脚本会签发并校验证书、安装续期脚本和以下 systemd 单元，并启用定时器：
如果检测到旧版 `/opt/1panel/ai-router/tls` 证书，安装脚本会先将其迁移到
隔离目录，避免旧私钥继续暴露给共享 `/data` 的其他容器。

- `ai-router-tail-cert-renew.timer`
- `ai-router-tail-cert-renew.service`

定时器每周检查一次。只有证书内容发生变化时，才重启 `router-control-tail` 以加载新证书；Router API 和 GPU 模型服务不受影响。

```bash
sudo systemctl status ai-router-tail-cert-renew.timer --no-pager
sudo systemctl list-timers ai-router-tail-cert-renew.timer --no-pager
sudo systemctl start ai-router-tail-cert-renew.service
```

## 健康检查

```bash
curl --noproxy '*' \
  --fail \
  https://ai-x10drg.taild500c8.ts.net:4001/health
```

管理接口继续使用现有管理密钥鉴权。客户端账号和 API Key 只能在管理台或 `/api/clients` 管理接口中创建、停用和撤销，完整 Key 只在创建时显示一次。
