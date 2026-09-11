# 原照片首帧成片恢复记录

## 根因及已执行恢复

- 旧生产 preview/main/fast worker 共持有约6.36GiB Swap，占当时H3 Swap约99.7%；实际首帧后端 single-a4 仅约19MiB。原任务触发8GiB运行期硬保护，不是证据已确认的显存OOM。
- 用户授权维护旧worker后，仅重启上述三个空闲生产worker；新PID分别为361051、361005、361018。single-a4、single-realism、single-b8未重启。主机Swap降至约2GiB，完成60秒稳定观察后恢复本次验收许可。72GiB准入、Swap硬线及其他门禁不变。
- 清理及恢复审计分别位于Ivan的 `/mnt/ivan-ext4-offload/h3-fleet-cutovers/20260911-idle-cache-recovery-r1` 和 `/mnt/ivan-ext4-offload/h3-fleet-cutovers/20260911-fullfilm-recovery-r1`。
- 并行Router发布遗漏H3配置、密钥挂载及schema，导致Studio页面提交前读取Control策略404。首次恢复又被后续model-aliases发布覆盖；第二次以最新实际镜像及完整Compose链为基础，追加H3-only配置。未回退镜像，未替换非H3环境，Router API未重建。
- 两个Control恢复记录位于本机持久目录 `deployment/control-config-recovery-20260911-r2`，其完整前缀为 `/home/ai/.local/state/h3-studio-ivan-production/`。15:00前复核两个容器身份与恢复回执一致，写入和生成均启用，两个实例健康。未来发布仍需保留该Compose覆盖链；本次恢复不等于所有其它部署工具已自动解决配置合并。

## 本次唯一重试

2026-09-11 15:00从真实Studio页面点击重试一次，HTTP 200。原提示词批准、素材及规格均保留；此前取消attempt不复活。

- 项目：`7732e9926474`
- 执行：`studio_7732e9926474_preview_a1bad5b894bc41a4b1d79314f1ea0631`
- Studio run：`run_527f57fc9f6f4977a403f1c40da409ce`
- Fleet：`98cbe07cd7e549e7bb1c8c15737c378f`
- 后端prompt：`ced67ca0-54af-482c-ad45-6859d275d535`
- 配置：`H3_I2V_QUALITY14`，480×864、362帧、24fps、原生音轨，14步完整模型。
- 首帧：`asset_1ffde8cf39a84559994d1c874df95370`，SHA256 `a80da8df3ab36cb5af5ce8d96792f69a045ae435a41febdc6efec8415232c63a`。
- 种子保留服务器整数 `2696045020911358409`；浏览器既有大整数显示精度问题未在本次运行中改变输入。

排队后自动通过原稳定观察要求，15:03进入后端。准入预测约52.42GiB，小于72GiB；包含24GiB静态候选预算及2GiB全局余量，clean inactive cache按0.5折扣，未降低预算。未匹配峰值历史，因此不能把本次预算称作历史实测预算。

原图节点20连接到图生视频节点5的first_frame，实际执行事件已经过节点20、5并到达采样节点10；仅进入节点不等同完成采样。

过程证据：本机 `deployment/fullfilm-recovery-20260911-r1/page-retry-firstframe.json` 及 `fullfilm-samples.jsonl`。此时仍在运行，完整成片、媒体解码与播放验收待补充；其他模式和并发不据此授予资格。

## 完整成片与媒体验收结果

2026-09-11 15:29:39 后端完成，Studio 15:29:41持久化成片并进入 `awaiting_approval`。全程同run和execution，14个真实非缓存采样步骤全部完成，没有重启或重复提交。实际后端执行1587.827秒（26分28秒），页面总耗时1741.049秒（29分01秒，含准入等待和回传）。

- 不可变产物：`out_f8cea126df6742fca9744f1e85d70bd5`。
- 文件：`/home/ai/.local/state/h3-studio-ivan-production/projects/projects/7732e9926474/router-outputs/out_f8cea126df6742fca9744f1e85d70bd5.mp4`。
- 362帧，480×864，24fps，视频15.083333秒；AAC双声道音轨15.075秒，文件2263695字节。
- 完整音视频解码通过；本机页面和用户Tailscale入口均实际播放通过，无播放器错误，`object-fit: contain`完整画幅。
- Tailscale视频Range返回206、1024字节；下载返回200，完整下载与本地产物SHA256一致：`dca288ef366326194062da6cdb4a1867094f4533e50a62ae2d52c106c46b919c`。
- 准入预测52.42GiB，采样到的实际cgroup峰值53.34GiB，最低主机可用70.23GiB；Swap峰值约1.97GiB，较基线峰值增长651264字节；PSI avg10峰值均0，OOM/Xid记录为空。
- pgsteal累计回收12263145472字节；仅表示cgroup累计页面回收，不冒称本任务净文件缓存释放量。
- 审核仍未通过，未自动开始高清、付费或后续任务，画质留给用户评价。

验收记录和页面截图：上述本机审计目录中的 `fullfilm-verification.json`、`fullfilm-playback.png`。

15:34后既有300秒空闲卸载实际生效，4060Ti显存由13794MiB降到642MiB、利用率0；未人工强制清理，single-a4 PID仍3218832。两张3060也无计算负载。成片期间两个Control容器身份与恢复回执一致，生成和写入开关仍启用，没有再次发生覆盖。`git diff --check`通过。

## 尚未包含的交付

本次证明原照片首帧完整生成与网页播放下载链路，不冒充其他五种模式、三卡并发或WorkBuddy本次重新调用均已验证。Fleet仍为指定项目验收gate，未宣称普通新项目已恢复接单；正式范围开放需将新证据绑定配置并单独核对资格。页面仍存在排队时长显示0秒、未知总进度显示1%、种子大整数展示精度等已知展示不足，本次不在健康采样过程中热改线上代码。
