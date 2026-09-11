# AI Router 局域网 HTTPS

服务地址：`https://192.168.2.66:4000/v1`（nano3 已安装信任证书）。
原域名入口 `https://ai-x10drg.taild500c8.ts.net:4000/v1` 同时保留。
独立 Nginx 仅监听 `192.168.2.66:4000`，转发到现有 `127.0.0.1:4000`。
本机和 Tailscale 入口保持不变。API Key 仍由 Router 验证。

## 客户端

nano3 可直接使用 IP 地址，无需修改 hosts。其系统信任库已安装
`/usr/local/share/ca-certificates/ai-router-lan-ca.crt`，并执行了 `update-ca-certificates`。
其他客户端若使用 IP，需另行安装同一 CA 的公钥证书；不可关闭 TLS 校验。

如果选择域名入口，局域网 DNS 或客户端 hosts 需要包含：

```text
192.168.2.66 ai-x10drg.taild500c8.ts.net
```

Windows hosts 位置：`C:\Windows\System32\drivers\etc\hosts`，修改需要管理员权限。
这条映射影响该客户端对此域名的全部访问；仅在局域网内使用。
离开局域网后应移除映射以恢复原有域名解析。也可在局域网 DNS 中配置分区解析，避免固定客户端 hosts。

应用模型填 `siyuan/auto`，使用现有 Key。域名使用 Let's Encrypt 证书；
IP 使用独立的本地 CA 证书，SAN 包含 IP:192.168.2.66。

## 服务端

正式部署文件：`/opt/1panel/ai-router-lan-https/compose.yaml` 和同目录 `nginx.conf`。
证书只读复用 `/opt/1panel/ai-router-control-tls`，私钥不进入仓库。
容器 `ai-router-lan-https` 使用固定镜像摘要，重启策略为 `unless-stopped`。
现有域名证书续期服务保持不变；`ai-router-lan-tls-reload.timer` 每日检查代理是否运行，
IP 证书剩余有效期不足 30 天时由 `renew-ip-cert.sh` 自动续签一年，校验配置后平滑加载证书。
CA 有效期十年。私钥保存在 AI 的 `ip-tls` 目录，root 专用；代理仅挂载 `ip-tls/live` 中的服务器证书和密钥，不能读取 CA 私钥。

入口仅转发 `/v1/` 和 `/health`；内部管理路径返回 404。
流式响应关闭代理缓冲，请求体上限 32 MiB，上游读写超时 1800 秒。
访问日志不包含 Authorization 或请求正文。

## 验收（2026-09-10）

在 AI 上强制连接局域网 IP，同时使用证书域名进行 TLS 验证：

- TLS 1.3，系统默认信任库验证成功；未跳过证书验证。
- `/health`：200。
- `/v1/models` 无 Key：401。
- `/v1/models` 使用用户提供的 Key：200，返回 `siyuan/auto`。
- `/internal/status`：404。
- 三个地址分别监听 4000，容器运行正常，RestartCount=0。

后续已从 SSH nano3 完成到 AI 局域网 IP 的 HTTPS 验证：curl、Python urllib、requests、httpx 和系统 Node HTTPS 均在默认 TLS 验证下返回健康检查 200。
用户 Key 从 nano3 获取模型列表返回 200 / siyuan/auto，不带 Key 返回 401；域名入口也复验正常。
尚未定位用户应用的独立容器或虚拟环境；上述运行时验证使用 nano3 的系统环境。
没有发送模型推理请求，也没有重启现有 Router 或 GPU 服务。

## 停用

停用专用代理容器及 `ai-router-lan-tls-reload.timer` 即可关闭局域网 HTTPS；不影响原有两个 Router 入口。

## 控制台自助连接页面

控制台导航“局域网 HTTPS”打开 `/lan-https`；账号创建及 API Key 展示窗口也提供入口。
页面提供地址复制、CA 公钥下载、Windows/Linux/macOS 安装指引、证书有效期和续签检查状态。
新账号复用现有 CA；新设备安装一次信任证书。浏览器不能自动修改操作系统信任库。

公开下载仅开放固定地址 `/api/lan-https/ca.crt`，返回解析后重新编码的 CA 公钥证书。
状态与服务端检查需要管理密钥。服务端检查仅连接配置的局域网地址，验证 TLS，
可输入 API Key 检查权限；只有主动勾选才发送短推理请求。检查成功不代表客户端已信任证书。

`publish-public-certificates.sh` 在续签检查和平滑加载成功后，将公钥与状态发布到
`/opt/1panel/ai-router/lan-https`，控制台通过既有 /data 挂载读取。
私钥始终留在原 root 专用目录，不提供下载接口。
