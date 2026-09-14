# 隔离迁移验收记录（2026-09-09）

状态：候选代码和交互体验通过；独立 Tailscale 体验入口已获确认并部署。
未提交、未推送、未部署真实生成模式，生产模型与 Router 未切换。

## 已完成

- 原版 Studio 测试及适配回归：29 passed。
- Fleet 全套隔离测试：111 passed。
- 薄代理及原控制台审计契约回归：8 passed。
- 合计 148 项；Studio 有 4 条原有 FastAPI `on_event` 弃用警告。
- 真实 Chromium 浏览器：桌面与 390px 手机布局、六个模式按钮、建项目、
  Context IR 确认、预览、本地 768P、模拟 2K、历史、队列、视频读取通过；pageerror 为空。
- HTTP 完整流程覆盖十组配置，包括首/尾/首尾帧、参考图、图音频、参考视频内置音频、
  Hybrid、音频锁定，以及 safe/cloud 策略。
- 双项目定时批次的暂停、恢复、立即执行与串行起止时间检查通过。
- 回归覆盖未知提交不重投、重启接续、重复点击、批次对账前禁止删除/换项目、
  跨来源拒绝、Tailnet 身份白名单、视频 Range 和 multipart 透传。
- 修复参考视频缩放得到奇数高度导致 libx264 拒绝编码的问题；1344×768 合成片上传通过。
- JavaScript 语法、两份候选 systemd unit 静态校验通过。
- 十二份工作流摘要匹配；新文件无测试数据库、运行环境、模型或凭据；
  凭据模式扫描无命中，tracked/untracked whitespace 检查通过。

最新浏览器证据位于 AI 本机持久目录：
`/home/ai/.local/state/h3-studio-ivan-evidence/smoke-05/`。
包括 `report.json`、桌面/手机截图、测试进程日志。该轮批次 ID：`e8d46117def0`。
所有片段都是合成素材，真实 GPU 调用数 0、MiniMax 付费调用数 0。

主工作树的 `git status --porcelain=v1` 在开始和结束时摘要一致：
`71c99ea6f6d94c874459ecdc9e1134f0e15e7291b8a12494d80d95ddb15526d6`。
没有拆除或覆盖原工作树的未提交内容，没有重启模型或生产 Router。

## 私网体验部署验收

- 入口：`https://ai-x10drg.taild500c8.ts.net:8444/`。
- 用户级 `h3-studio-ivan-preview.service` 已 enabled/active，后端仅监听 `127.0.0.1:14829`。
- 只新增 Serve HTTPS 8444 → localhost 14829；其他 Serve/Funnel 配置逐项对比未变化，
  新端口未开启 Funnel。身份白名单配置保存在 Git 外的私有目录。
- Ivan-laptop 通过正常 HTTPS 校验、无手工认证头访问页面/API，创建项目、
  Context IR 确认、预览、本地 768P 和模拟 2K 均完成；三个合成视频返回 200，
  每个 2,694,262 字节，均带 `simulated: true`。
- 验证项目 ID：`38adafd8065d`；跨来源写入返回 403，localhost 无身份请求返回 401。
- 真实 HTTPS Chromium 页面、历史、队列、桌面和手机布局检查通过，pageerror 为空。
- 证据：`/home/ai/.local/state/h3-studio-ivan-preview/deployment/verification.json`，
  同目录包含 Serve 原配置和 HTTPS 截图。
- 用户级 systemd 的 `PrivateDevices=true` 导致 `218/CAPABILITIES`；仅对体验 unit
  移除此不兼容配置，保留 NoNewPrivileges、PrivateTmp、CPU/内存限制。
  体验程序仍只使用 CPU 合成素材和模拟客户端，不调用 GPU/付费 API。
- 部署期间主工作树出现外部并发状态变化；本次未向主工作树写入，未清理或覆盖那些变化。

如需撤销，仅执行 `sudo tailscale serve --https=8444 off` 和
`systemctl --user disable --now h3-studio-ivan-preview.service`，保留体验数据和其他转发。

## 仍需完成

1. 真实模式需要 Ivan 候选 Fleet 发布、缺失 Ref2VA 模型、AI MiniMax 凭据和现场端点验收。
   这与界面模拟验收分开，不用新增长渲染阻塞框架交付。
2. 单独确认提交/推送范围后建立 PR；不合入本地其他 ahead 历史，不自动合并。

部署边界见 `ivan-migration.md`。已可访问的入口仍是模拟交互体验，不代表真实生成验收。
