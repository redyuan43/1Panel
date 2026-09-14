# Ivan / Ivan-u24 双节点迁移

## 本次交付边界

Studio 增加持久化节点归属与设备选择；各主机仍由自己的 Fleet 执行资源准入。
目标为 Ivan 两张 3060 各执行一个独立任务，Ivan-u24 的 4060 Ti 执行一个任务。
不合并显存，不使用 Ivan-u24 的 V100。双卡资格先只覆盖 A4+A4；其他配方组合未验证前保持单路。

模型复制、隔离环境和两机完整单路实验已完成；2026-09-14 Studio 已切换双节点版本，仅启用 Ivan-u24 的 A4 单路接单。Ivan 的独立双任务并发资格尚未完成；双 3060 同视频实验功能通过但更慢，不作为生产加速方案。
离线模板中的 `enabled: false`、空资格列表与 `inference_admission: wait` 是刻意保留的默认状态，不能直接视为线上资格；当前部署以逐机审计和下文现场记录为准。

## 代码与候选包

- `../../h3-video-studio/app/multifleet.py`：节点能力汇总、设备选择、跨主机任务分配。
- `../../h3-video-studio/app/fleet_routes.py`：独立 SQLite 记录执行、提交状态及批次租约。
- `../app/admission.py`：cgroup 准入上限取配置上限与物理内存减保留量的较小值，默认保留至少 16 GiB。
- `../scripts/prepare_multinode_release.py`：将本次相对基线的改动三方合并到真实线上源代码，保留首帧、多素材及连接器功能。冲突时停止，不覆盖线上。
- `../scripts/prepare_multinode_bundle.py`：从只读盘点生成模型清单、独立环境锁定文件和未启用服务模板。

本次持久化材料根目录为 `/home/ai/.local/state/h3-multinode-20260913`：

- `workspace-before/`：修改前工作区快照，不代表 Git HEAD。
- `ivan-inventory.json`、`ivan-u24-inventory.json`：现场只读盘点。
- `release-r6/h3-video-studio`、`release-r6/h3-fleet`：最终候选源代码；各自 `multinode-manifest.json` 记录源文件哈希与合并来源。
- `deployment-r2/`：15 个工件约 62.5 GiB 的复制清单、环境版本、节点模板与资源 dry-run。

仓库原先存在大量未提交内容。本次没有提交、推送、切换分支或覆盖这些内容。部署必须使用审核后的候选包，不得直接以较旧的工作区覆盖线上新版。

离线验证：工作区 Fleet 全量 1,214 项、Studio 全量 143 项通过；真实线上源码合并后的候选 Studio 143 项、Fleet 准入/调度/迁移材料 113 项、前端 15 项通过。
候选 Fleet 仅包含线上运行脚本及本次增量，没有打包历史实验脚本，因此候选只运行适用的 113 项；1,214 项全量结果属于工作区。
测试使用假后端和本地数据，未调用生产推理。候选 r6 相比已验证 r5 的运行时代码相同，仅前端测试补齐了线上已有事件的浏览器模拟。

## 执行与恢复约束

提交前先在 SQLite 中保留节点和执行编号，再以原子状态转换保证同一编号最多发起一次提交。
超时、连接中断及回执缺失保留原节点，重启后先对账；不向另一节点重发。
未进入提交阶段的取消可以释放预留。旧任务没有节点字段时只认原 Ivan；节点停用不会改变历史归属。
批次取得单节点租约，旧批次恢复也先检查原执行归属；另一节点仍可接收独立任务。

节点容量快照超过 15 秒、资格缺失、计数不完整、资源异常或提交结果未知时保守等待。
第二张 3060 由 Ivan 本地 Fleet 的精确运行时资格、配方对、采样进度及至少 60 秒稳定窗口决定。
Studio 的 `max_parallel: 2` 仅是上限，不能绕过 Fleet 或自动产生资格。

## 现场限制

### 2026-09-14 完整实验与 Studio 修复

- Edge 新完整环境任务 `2488a108658f` 已经真实 GUI 提交并由 worker PID 3088799 执行，Fleet `ec4f95227c4246acb064edab8735e65e`、upstream `6d79188d-1792-4b4b-b21e-e4d0850f01c5`。模型加载阶段出现新增 NVRM `NV_ERR_NO_MEMORY`，约 7.86 秒后被 Fleet 保护性取消，采样为 0；Comfy 仅有 execution_interrupted、没有 CUDA OOM traceback，不能等同于模型 CUDA OOM。96 GiB cgroup 未命中限额，也未找到可证实的配置根因。已停止新增 GPU 重试，Edge 保留未开放状态和失败记录；本轮没有成片或生产资格。随后 controller 已回到 idle，原 Comfy/Studio/Qwen 三服务健康，Qwen 原模型、500K 上下文、MTP3 与全部配置哈希恢复一致；受管 H3 worker PID0、无活动任务。完整差异审计见 `/home/ai/.local/state/h3-edge-fleet-candidate-20260914/runtime-copy-audit/nv-memory-comparison/REPORT.md`，不因此放宽保护或系统限制。
- Studio `20260914-multinode-r7` 与两项 Router Control 已同步 `target_node=edge` 接口定义，schema SHA256 `4ee4425769acd675025f31d151cfd291838338032f21e90001037916939cc468`。两项 Control 保持现有镜像和全部环境变量不变，仅更新 schema 挂载；浏览器新任务接口已实际返回 200。首次受管 Edge 任务 `56a550214d0b` 因源码复制错误缺四个嵌套目录 Python 文件而在 worker 启动阶段失败，无 Comfy upstream、无采样；失败记录保留。补齐后成功环境与隔离环境 1015 个源码及资源逐个 SHA 一致，完整 CPU Comfy HTTP 启动及所需节点检查通过；失败后原 Qwen 与另外两项旧服务已实际恢复，后续新项目 `2488a108658f` 单独验收。
- Hybrid 改用原生 ReferenceToVideo 与首尾 AddGuide 后完成真实单条：项目 `6af89b2547df`，execution `studio_6af89b2547df_preview_9943bd2395d14131bc143f5964bda05a`，原生 prompt `6f760aaa-aee8-40ef-beff-9f22806b32ff`。14 步、480×864、362 帧、24 fps，Studio 耗时 1695.371 秒（28 分 15 秒）；整文件解码与真实浏览器播放通过。视频 SHA256 `bf9762a79167a9984420b923731359e6042371c9cb5ba0d1d0bcbc24b6e788c1`，证据 `/home/ai/.local/state/h3-six-modes-20260914/hybrid-native14-final-*`。该修复保留旧 T8 拼接兼容保护，不修改 Comfy runtime 或降低步数。 正式资格已晋级，测试 gate 已关闭；既有 300 秒空闲卸载完成后七个配方均回读 available_slots=1，队列为空，worker PID 108029 全程不变。用户画面质量批准仍待用户操作。
- Studio 已更新至不可变版本 `20260914-multinode-r6`：显式选择 Edge 时允许预约受控模型切换，真实 GPU 可用槽仍为 0；自动选卡不会触发关闭 Qwen。215 项 Python 与 28 项前端测试通过，107 份发布文件 SHA256 校验通过。运行中的 Hybrid 任务在仅重启 Studio 后保持原 execution、Fleet prompt、run、节点及 started_at，继续等待同一 GPU 任务，未重复提交；真实浏览器无脚本错误。部署证据位于 `/home/ai/.local/state/h3-studio-multinode-validation-20260914/deployment/r6-activation/`。Edge 此时仍未开放，须完成受管 worker 实际推理和 Qwen 自动恢复验收。
- 4060 Ti 后续原生 14 步完整验收已完成：首帧 `0afd1557f75a` 为 1681.183 秒，尾帧 `80f646c5ebf9` 为 1682.227 秒，首尾帧 `ff04ffb85ab2` 为 1718.515 秒，参考图 `5511f7eb98e5` 为 1628.309 秒。均为 480×864、362 帧、24 fps，完整音视频解码、浏览器播放、各自精确运行资格与资源监控通过。四项资格已与 A4 文生/A4 首帧一起开放单槽；Hybrid 旧 T8 路径因关键帧拼接顺序兼容保护失败，不计为通过。
- Edge GB10 与 4060 Ti 同首帧 A4 四步比较完成：完整提示词、图片、种子、五份权重及工作流一致（仅输出前缀不同），Comfy 同口径分别 425.633 秒和 717.761 秒，本次 Edge 耗时减少 40.70%。运行时核心实现不同，输出并非逐帧相同，不证明质量完全等同或纯硬件差异。Edge 原 Qwen 模型、500K 上下文、MTP3 与三项原服务已恢复并核验；恢复耗时 794.309 秒，单独记录，不算入视频生成时间。完整证据位于 `/home/ai/.local/state/h3-edge-same-benchmark-20260914/comparison.json`。
- 后续 Studio 已更新至不可变版本 `20260914-multinode-r4`：修复同项目修改生成模式后残留配方选择，提供明确的首帧四步配方选择，并允许未获运行资格的模式先保存和确认输入。指定验收任务仅在精确节点、项目、profile/version 匹配且资源正常、队列空闲时取得单槽预留；发布验证期间全局容量仍为 0，Fleet 原始准入与执行前缀检查保留。178 项 Studio、218 项连接器、28 项前端测试通过。
- 用户首帧图片及完整提示词的独立 A4 首帧项目 `15d2951c3abb` 经真实 GUI 创建、确认、提交后完成，耗时 745.419 秒。实际 Comfy prompt 为 `803db72e-f4ef-471a-a168-bd2347fc9578`，Fleet 编号为 `41817a9c66ba4c8d8eea97ff92bc62ff`。成片 480×864、362 帧、24 fps，完整音视频解码及浏览器播放通过；监控最高采样温度 89℃，无 Swap 增长、PSI 或 OOM 告警。证据位于 `/home/ai/.local/state/h3-six-modes-20260914/a4-final-receipt.json`。原项目 `0afd1557f75a` 保持用户选择的 14 步设置；此四步成功不代表其余模式均已通过。
- REF2VA 官方固定版本 `3f57e8291d2ef846f9a074b1b76d2767db434abe` 的 34,038,894,550 字节工件已在新机完整校验，SHA256 为 `9eef934046a0671bc8a5daf87100705e1478419c574cfde70c50fbe6885f76a9`。AI 位于 `192.168.2.66`，不能直接访问新机 `192.168.100.137:22`；Tailscale 点对点复制实测约 9.6 MiB/s。改由新机直接下载固定官方 CDN 工件，持续约 71.1 MiB/s，接收及完整校验合计 521.7 秒。仅工件落盘不代表 Reference/Hybrid 已完成推理验收。
- 同一 A4 配方、480×864、362 帧、24 fps、4 步，单 3060 完整实验耗时 1110.626 秒；双 3060 共同生成同一视频耗时 1458.140 秒，增加 31.29%，未达到提速目标。双 GPU 同 PID、attention 和 VAE helper 执行证据通过独立验收。两份 MP4 的全部 362 帧解码 RGB 及音频采样逐值一致；不外推其他种子、配方或未保存的 latent。
- 双卡解码保存从 294.49 秒缩短至 137.70 秒，但提交到解码开始由 814.28 秒增至 1320.32 秒，抵消了解码收益。结果位于 `/home/ai/.local/state/h3-single-task-dual-20260913/full-r1/result.md`；该实验没有注册生产资格。
- Ivan-u24 的 4060 Ti 完整 A4 样例耗时 585.915 秒。新机 Fleet 已依据本机完整执行记录、源码与模型哈希、媒体校验和实时 PID/GPU/socket 绑定开放 A4 单路；其他配方未获资格。
- 真实 Studio 地址为 `https://ai-x10drg.taild500c8.ts.net:8445/`；8444 是模拟页面。真实浏览器复现发现草稿创建报 503，根因为当前 Router Control 部署遗漏 H3 管理配置，依赖的管理接口返回 404。已在保留当前镜像及非 H3 配置的情况下恢复两个 Control 实例；聊天 API 容器未重建。
- Studio 已切换至不可变版本 `20260914-multinode-r1`，修复设备字段 schema、未知提交的原节点保持及禁用节点污染容量汇总。145 项 Studio、217 项连接器/浏览器/多素材、15 项前端测试通过。节点配置保留 Ivan 历史访问，仅启用 Ivan-u24 接收新任务。
- 用户完整 1684 字符提示词经真实页面创建、确认、指定 Ivan-u24、提交后生成成功，项目 `0afd1557f75a`，总耗时 597.771 秒。实际 Comfy prompt 为 `01a69b8d-6b54-49b3-b185-375e114e0c38`；Studio/Fleet 代理编号为 `480ce3b389434ec69cdc840372de9696`，两者不可混称。成片 480×864、362 帧、24 fps、约 15.08 秒，含 32kHz 双声道 AAC；整文件解码和真实浏览器播放通过。提示词中的“4K”不改变本轮预览规格，未宣称原生 4K。验收材料位于 `/home/ai/.local/state/h3-studio-ui-20260914/`。
- 成片完成且无在途任务后，Studio 更新至 `20260914-multinode-r2`：以绑定实际 upstream prompt 的已确认采样事件显示步数和阶段，移除运行中固定 1%，修正实际节点及旧显卡展示，并区分本地健康与云端配置。155 项 Studio、217 项连接器相关、21 项前端测试通过；本次真实成片回执正确提取 4/4 步，更新后浏览器再次播放成功。未重新生成视频，也未重启 GPU worker。

### 2026-09-13 实施进展

- Ivan-u24 的 15 个模型工件全部通过 SHA256 校验，三个独立运行时与两套隔离 Python 环境已完成安装和依赖检查。迁移凭据不保存在仓库。
- 经用户确认，Ivan-u24 的 ZFS ARC 长期上限设为 16 GiB，持久配置为 `/etc/modprobe.d/h3-zfs-arc.conf`；当前内核与可用回退内核的 initramfs 均已更新。运行时上限和实际缓存已核验，尚未通过重启验证。
- Ivan 的闲置 Gradle 缓存、禁用 Snap 版本及对应下载缓存，先归档到 AMD 外接 RAID、逐文件校验后再清理；根分区空闲恢复至约 26.2 GiB。归档为 AMD 的 `/media/ivan/1.42.6-25426/h3-archives/ivan-20260913/cache-and-disabled-snaps.tar.gz`。25 GiB 准入门槛保持不变，空闲空间需持续现场检查。
- Ivan Fleet 已仅禁用迁出的 4060 Ti 旧 lane，保留原发布控制状态；两张 3060 的原 worker 未重启。Studio 尚未切换到双节点候选。
- 4060 Ti 与 3060 分别完成 A4 短派生工作流（107 帧、480×864、24 fps、含音轨）；结果仅证明短单路预览，不代表 362 帧或并发资格。
- Ivan 首次 A4+A4 的第一路在 362 帧采样时 CUDA OOM，第二路未提交。后续低显存实验使用独立运行时副本、固定来源的 LoRA 分块适配器及更多 CPU 权重卸载；保留原时长、分辨率、步数和全部资源门槛，结果另存实验报告。

证据位于上述持久材料根目录的 `migration-receipt.md`、`u24-preview/`、`ivan-preview/`、`ivan-pair-r1/` 和 `lowmem-audit/`。下文盘点限制是实施前快照，不能代替实时准入。

### 实施前盘点

2026-09-13 盘点中 Ivan 内核可用物理总量约 62.7 GiB（用户扩容至 64 GB），聚合上限相应约 46.7 GiB。
根分区空闲约 20.8 GiB，低于现有 25 GiB 门槛，dry-run 返回 `root_disk_headroom`。
不得自动删除文件、降低门槛、清缓存、关闭 swap 或通过重启生产来争取空间。
Ivan-u24 最近一次内核报告约 78.4 GiB，模板按实际报告计算，不固定假设两机内存相同。

新机当前 Python 包与源机不同。ComfyUI 的 100 个依赖与 Fleet 的 24 个环境包分别记录，不能复制虚拟环境目录或安装到现有用户环境。
版本锁定不是安装成功证明；后续需检查包来源、必要 wheel 可用性、`pip check` 及运行时 CUDA 兼容性。

## 分阶段操作与验收

1. 经确认后，仅向 Ivan-u24 新建的 `/home/ivan/h3` 同步数据。`copy-to-ivan-u24.sh` 在 Ivan 执行，使用 LAN rsync，无 `--delete`。事先核验源到目标的 SSH 主机身份与登录权限；不得禁用主机验证。脚本没有密码。
2. 在目标 `models` 目录运行 `sha256sum --check`，使用材料中的 `models.sha256`。清单哈希取自已有固定策略，尚未现场重算或确认目标哈希。所有工件校验完成后才能进入下一阶段。
3. 在目标分别创建 `comfy-venv` 与 `fleet-venv`，按各自锁定文件安装并核验。模型路径模板只应用于新机复制出的运行时；不要覆盖 Ivan 旧运行时的 `extra_model_paths.yaml`。
4. 复制候选源代码至独立版本目录，准备私有认证文件及数据目录。服务模板尚未安装；`fleet-auth.env` 需按当前 Fleet 认证契约注入独立密钥，Studio 节点清单只引用绝对密钥路径。
5. 审核后再逐主机启动单个新 worker。依据 `worker-specs.json` 和实际 PID、argv、GPU UUID、代码及权重哈希生成新的运行时绑定。不得直接继承 4060 Ti 旧主机资格，不得同时启动同卡的 A4/realism/B8 三个 worker。
6. 保留线上发布闸门与 hard-stop 状态，逐项调查解除条件；资格注册及闸门变更需单独记录。先按主机串行验收短单路预览，再测目标参数；确认资源与作品质量后才做 Ivan A4+A4，最后做跨主机三任务验收。
7. 切换 Studio 的 `H3_FLEET_NODES_FILE`，逐节点启用。四配方以及首帧、多素材需分别核验能力目录、输入上传、结果下载和执行绑定。未验证项不宣告支持。旧通用 turbo/quality 模板没有在新机获得资格，`legacy_profiles` 默认空；需要保留旧 768 批次入口时应另行复制并固定相应工作流与资格。
8. 浏览器连接器的设备选择已并入候选。Router MCP 若使用外置静态 schema，需要同步候选 `app/connector_schema.json` 并审核对应重载；否则 MCP 不宣告新参数。

验收同时记录峰值内存、显存、swap 增长、PSI、OOM/Xid、真实采样进度、产物和原生产服务状态。仅健康检查通过不等于推理通过。

## 切换与回退

服务切换涉及 AI 的 Studio、Ivan 的 Fleet/worker、Ivan-u24 新服务以及可选的 Router MCP schema 重载。
切换前保存实际 unit/env 引用、数据库备份和未完成任务归属，暂停新任务入口并等待已知任务终态。
回退先停止接收新任务、对账在途任务，再恢复原 unit 与环境引用；保留节点 SQLite，不删除新节点结果或未知提交记录。
仅当无新节点在途任务时才回到原单节点 Studio，避免旧代码失去新节点任务的恢复能力。
