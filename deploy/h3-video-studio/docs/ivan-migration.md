# H3 原版工作室迁移：AI 控制面 + Ivan 三卡

## 边界与来源

- 工作分支：`codex/h3-studio-ivan-migration`，基于远端目标分支
  `codex/ai-router-media-h3-20260905` 的 `f8a4015338bfe6df4e41cfafb713a28fd0333e03`。
- 原开发工作树：`/home/ai/github/1Panel-worktrees/h3-studio-ivan`；源码现已迁入主仓
  `deploy/h3-video-studio`，该旧路径只保留迁移来源含义。
- UI 与后端来自 `redyuan43/h3-video-studio@c9af2768adf06298b014ca5d0fca551cc57df6f5`。
  十二份工作流来自 Edge 的 `/home/admin/github/minimax_h3/optimized_workflows`；文件摘要见 `SOURCE.json`。
- 保留原版六种素材模式、三种策略、音频选项、逐阶段确认、历史和定时批次。
  原版本来不支持的组合仍明确拒绝，不静默改模型或音频策略。
- 主工作树的 creative-studio、生产 media-adapter、Edge、模型和运行中的长任务均不修改。
  后续只在验收和 PR 合并后另行评估旧 UI 退役，不先拆除依赖共享签名函数的目录。

## 最小适配

```text
浏览器 → 独立 H3 Studio（AI，127.0.0.1）
       ├─ Context IR / 官方 768P / 2K → 原 MiniMax 客户端
       └─ preview / proof / local_768 → 私网认证 Fleet → Ivan 单 GPU worker
1Panel /h3-studio/ → 固定本机源的薄代理 → 同一套原版 Studio
```

本地阶段不再占用 AI 上的单卡锁，也不执行 Edge 的 GPU 独占检查。
Fleet 统一执行资源准入：短预览最多三路、短质量最多两路；长任务、参考、
Hybrid、音频锁定保守串行。三卡不是单个任务自动三卡并行。
保留原版 Turbo 四步模板；不能把历史六步验证说成四步迁移已实测。
耗时区间只是原 Edge 参考，不是 Ivan 的耗时承诺。

执行 ID 在提交前落盘。提交响应丢失只查询原 ID，禁止自动重投。
服务重启接续查询；观察窗口结束显示结果未知，可点击“继续对账”，不换 Seed。
对账未完成时禁止删除、重置提示词、重跑前置阶段和加入新批次。
素材采用项目唯一名称，禁止覆盖，worker 改名视为失败，避免悄悄使用旧素材。
取消只针对自己的 prompt；不调用全局 interrupt/free。批次持有 Fleet 独占窗口，
正常完成后释放；存在未结束任务时拒绝释放，暂停并保留对账路径。

## 独立交互体验（不调用 GPU 或付费 API）

独立状态目录：`/home/ai/.local/state/h3-studio-ivan-preview`。
`scripts/preview.py` 只生成 CPU 合成测试片，替换本地与云端客户端并禁用原 URL opener。
所有结果带 `simulated: true`；包括“官方 2K”阶段，它只是交互占位，**不是 2K 验证**。
没有从体验页面切换到真实模式的按钮；真实模式必须另起进程和状态目录。

候选 unit：`deploy/h3-studio-ivan-preview.service`，后端仅绑定 `127.0.0.1:14829`。
`deploy/preview.env.example` 中填写获准访问的 Tailnet 登录账号，放入
`~/.config/h3-studio-ivan/preview.env`；不要把实际身份配置或密钥加入 Git。
目录准备、依赖安装、启用 unit、设置 Serve 均需要部署范围确认。

确认后仅新增以下转发，不使用 `serve reset`，不修改已有端口，不开启 Funnel：

```bash
tailscale serve --bg --https=8444 http://127.0.0.1:14829
```

预期入口：`https://ai-x10drg.taild500c8.ts.net:8444/`。
**预期地址不代表已经部署。** 发布后必须从另一台 Tailnet 设备确认页面、身份认证、
创建项目、播放产物以及服务持续运行，再对外提供“可访问”链接。
只信任 localhost 代理注入的 `Tailscale-User-Login`，应用禁用 Uvicorn proxy headers。
Tailscale Serve 的身份头和 localhost 限制见
[官方说明](https://tailscale.com/docs/features/tailscale-serve)。
回滚只撤销 `8444` 对应 Serve 规则并停止本候选 unit，保留状态数据和其他转发。

## 真实模式：下一道独立验收

### 2026-09-09：首条真实竖版任务已启动

用户确认后，独立真实服务 `h3-studio-ivan.service` 已部署至 AI 的
`127.0.0.1:14830`，Tailnet 入口为 `https://ai-x10drg.taild500c8.ts.net:8445/`。
原8444模拟体验页及其数据保留不动。真实数据位于
`~/.local/state/h3-studio-ivan-production/projects`，私有配置位于
`~/.config/h3-studio-ivan/production.env`；未配置付费云端凭据。

页面新增画面方向和提示词处理选择。竖版预览为480×864，本地768P为768×1344；
旧项目默认横版，竖版官方768P策略明确拒绝。选择“直接使用原文”时不调用
Context IR，不改写原文，仍需在页面确认后手动启动视频。

空闲检查确认三路无活动/未跟踪任务和验证租约后，只替换 Ivan Fleet 的
`app/main.py`、`app/admission.py` 并重启 `h3-fleet.service`，未改变容量策略，
未重启 Router 或三个 GPU worker。回滚备份位于 Ivan
`/mnt/ivan-ext4-offload/h3-fleet/releases/studio-20260909/before/`。
三个 worker PID 保持1091767、1091770、1091773。

真实浏览器完成填原文、选择竖版/15秒/原文直通、确认和点击低清预览，仅提交一次：

- 项目：`e5364e7e9e14`，名称“雨后十字路口 CCD Coser · 真实竖版15秒”。
- 执行：`studio_e5364e7e9e14_preview_f20a7eeed093473e935037e318170ea6`。
- Fleet 任务：`a107456bb29b44a78261e020acc35f76`。
- ComfyUI 任务：`86128938-bda3-4d1f-810c-8e2588b71cb1`。
- 通过60秒空闲保护后进入 `fast` 的 RTX 4060 Ti；现场队列为 running，GPU利用率100%。
- 实际工作流480×864、362帧、24fps，原15秒设置按H3帧网格得到约15.083秒。
- 原文与GPU工作流提示词SHA-256一致：`6dba7d361fa44d952c47626b92f7d1b24731e502d52d3bf1ccc8c558a3b70e23`。

Studio 62项与Fleet 111项测试通过。页面直达
`/?project=e5364e7e9e14&stage=preview`，桌面/手机只读恢复未新增任务。
证据位于 AI `~/.local/state/h3-studio-ivan-production/deployment/`。
本次验收到真实任务开始执行，不代表视频已经完成或画面质量通过；未启动768P、2K或其他任务。

以下为此前候选设计和其余未完成模式的边界，不应覆盖上述已部署事实。

历史切换修复（2026-09-09）：统一直达链接和历史入口的项目加载，进入工作室自动启动轮询；
使用视图版本忽略旧项目的迟到响应，防止快速切换后串项目。输出规格按项目方向显示。
`node --test tests/project_navigation.test.cjs` 四项回归通过；线上只读浏览器连续三次历史切换，
视频URL与文件摘要不变；用首个running响应夹具验证随后自动刷新真实完成状态，未提交任何写请求。
当前预览已完成，实际480×864、视频15.083333秒，含音轨；画面质量仍需用户确认。
证据：`~/.local/state/h3-studio-ivan-production/deployment/history-fix/verification.json`。

计时与容量候选（2026-09-09）：阶段总耗时使用提交至结束的时间戳，包含排队与传输，
结束后固定显示，历史列表按已结束阶段展示；不冒充纯GPU计算耗时。
新增认证接口 `/api/capacity`，只读获取 Fleet health/capacity，缓存10秒；页面每15秒刷新，
逐卡显示空闲、占用或未知及可用显存。并发策略上限与空闲卡数分开，不计算虚假的剩余算力百分比。
读取失败清空旧空闲数；模拟模式不伪造真实三卡容量。未改变Fleet准入策略或并发限制。
Studio 63项、Node 5项测试通过；桌面/手机用真实只读快照注入验证，见
`~/.local/state/h3-studio-ivan-production/deployment/capacity-ui/report.json`。
此轮尚未重启后端：现场本地768P执行中、另一预览排队，因此容量API上线待空闲窗口与用户确认。

候选 `deploy/h3-studio-ivan.service` 使用 `scripts/serve.py`，绑定 `127.0.0.1:14830`。
它要求配置 Fleet URL、Fleet 密钥文件、Studio 密钥文件和独立持久数据目录；
缺配置拒绝启动，不能误连 AI 上原有 ComfyUI。示例见 `deploy/production.env.example`。
Fleet URL 走 Ivan Tailscale DNS；私网请求不使用机器全局 HTTP 代理。

1Panel 集成使用 `AI_ROUTER_H3_STUDIO_URL=http://127.0.0.1:14830` 与
`AI_ROUTER_H3_STUDIO_KEY_FILE`（同一个 Studio 原始密钥文件）。代理仅允许固定本机上游，
原管理密钥换取 HttpOnly 短会话，不向浏览器发送 Fleet/MiniMax 密钥，视频支持 Range。
独立 Tailnet 页面和 1Panel 页签可以分别部署，不必重启生产 Router 才体验界面。

当前不能宣称全功能真实生成就绪：

1. Ivan 需要部署本候选 Fleet 端点与准入策略；先核对活动任务，不能覆盖运行中的长任务。
2. 完整 Ref2VA 模型（34,038,894,550 字节）尚未在 Ivan 已检查模型目录中发现。
   Edge 有源工件，迁移需要单独确认存储位置、传输和摘要；不占用有 40GiB 保护线的 offload 分区。
3. AI 上 MiniMax 凭据尚未配置；不得复制凭据到源码或把模拟响应当实际调用成功。
4. 模型、custom nodes、三路素材一致性和原版四步/参考模板的实际运行仍需部署后验收。

已有实际成片证据可以复用，不等待新的长渲染：
Ivan `/mnt/ivan-ext4-offload/h3-deploy/evidence/20260907-233628/gate3-dual-14step-768-124/report.json`，
摘要 `ad6e9467bf3662546fabd2611ff42bf1e030ed07733b7e9907bf7846c5c99736`。
该历史验证是双路 14 步、1344×768、124 帧、24fps、5.166667 秒及 AAC；
不是本迁移所有模式、15 秒质量、云端 2K 的验证。

## 隔离验证

```bash
H3_STUDIO_DATA="$HOME/.local/state/h3-studio-tests" python3 -m pytest -q
.venv/bin/python tests/preview_smoke.py \
  --evidence-dir "$HOME/.local/state/h3-studio-evidence/run-unique" \
  --chromium "/absolute/path/to/chrome"
```

浏览器测试需要单独测试环境中的 Playwright，不是运行依赖。测试启动一个随机 localhost
端口的短生命期预览进程，结束后停止；覆盖原版 UI、手机布局、十种有效配置、视频读取、
逐阶段确认和批次串行。报告、截图、测试数据库、模型、凭据均不进入 PR。
Fleet 在 `../h3-fleet` 单独运行 `python3 -m pytest -q`；薄代理在
`../ai-router` 运行 `PYTHONPATH=. python3 -m pytest -q tests/test_studio_proxy.py`。

## 2026-09-11：工作树收敛

Studio 与 Fleet 正式源码已迁入主仓。运行时 Python 环境使用
`~/.local/state/h3-studio-ivan-runtime/venv`，comparison 计划和发布结果使用
`~/.local/state/h3-studio-ivan-production/comparison`。只读状态发布器的正式 unit 为
`deploy/h3-fleet/systemd/h3-working-set-status.service`。
运行环境按 `requirements.txt` 的已验证精确版本重建；其中 Pillow 是 production
connector 素材校验的运行依赖，不能只安装基础 Studio 的四个 Web 依赖。

旧工作树只能在 production、preview 和状态发布器全部切换到上述路径、服务复验通过，
并确认没有进程持有旧目录后删除。切换涉及两个 Studio 服务的短暂重启，必须单独确认。
