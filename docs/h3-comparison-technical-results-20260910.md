# H3 四组比较：技术执行结果（2026-09-10）

## 1. 范围与结论边界

- 证据主机：SSH `ivan`；只读证据根：`/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909`。
- 主要证据快照时间：**2026-09-10T09:18:16.307464+08:00**（Asia/Shanghai）；随后只读复核媒体、GPU身份及启动日志。
- 本次仅新增本文档；未提交生成、操作GPU、改生产配置、部署、重启、干预 D4-r2 或更新 memory。
- 本文汇总 R0、C0、A4-r3、A8、C1-r3、B8-r3 的实际执行与资源记录。六项均有成功 history、非缓存 sampler10 和对应视频，不能据此判定视觉或音频主观质量。
- R0/C0 的 report 状态仍为 `generated_pending_media_review`；其余四项为 `generated_pending_quality_review`。这些不是用户质量验收状态。
- 主代理已告知 B8-r3、C1-r3 发布完成；本文未另行核验发布端交付，只核对实验根下的实际视频。
- **不评画质、不选优、不将本表作为算法速度排名。D4-r2 单列为进行中，等待主代理验收。**

## 2. 统一项与必须披露的配方差异

六个成功 case 均为 **480×864、362帧、24fps**，视频帧时长为 `362/24 = 15.083333…s`，使用完整15秒规格，未用5秒替代。随机种子均为 **1565559107914140700**。

实际执行 GPU 均为 **NVIDIA GeForce RTX 4060 Ti 16GB**：
`GPU-0befdd20-6ea9-4e7e-3378-635e20f42536`，PCI `0000:04:00.0`。
R0/C0 由 report 的 fast 路由、metrics 的 PID1846391、同PID `CUDA_VISIBLE_DEVICES` 及 `/proc/driver/nvidia/gpus/*/information` 交叉确认；其余四项的 baseline.isolated 已保存该 UUID。
两张 RTX3060 的遥测记录不计入本表的执行GPU峰值。

| Case | 配方 | 步数 | video/audio shift | Worker / PID | reserve-vram（GiB） |
|---|---|---:|---|---|---:|
| R0 | 原始 T8 EMA4 | 4 | 12/3 | fast / 1846391 | 1（同PID当前配置复核） |
| C0 | 原始 T8 EMA4 + trigger | 4 | 12/3 | fast / 1846391 | 1（同PID当前配置复核） |
| A4-r3 | LightX2V 4step v1.2 | 4 | 6/3 | 隔离通用 / 2074565 | 1 |
| A8 | LightX2V 8step v1.0 | 8 | 6/3 | 隔离通用 / 2074565 | 1 |
| C1-r3 | EMA + People FP32组合工件 + trigger | 4 | 12/3 | 隔离通用 / 2074565 | 1 |
| B8-r3 | OpenVDN stage_dmd_8nfe | 8（配方NFE） | 12/3 | 隔离B / 2176657 | 6 |

共同底模为 `minimax_h3_fl2va_int8_convrot.safetensors`，文本编码器为 `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`，视频/音频VAE分别为 FP16/FP32。A/C 的 LoRA 文件精度不是底模量化切换。

具体适配工件：
- R0/C0：`t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors`。
- A4-r3：`minimax_h3_fl2v_turbo_4step_v1.2_768p_comfyui_bf16.safetensors`。
- A8：`minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors`。
- C1-r3：`c_realism_ema_people_fp32.safetensors`，单Bypass强度1；本次未重新加载其大张量做组合数学复验。
- B8-r3：VDN Composer5 → ExecutionPlan7 → Sampler10，`stage_dmd_8nfe`；report 中 `expected_steps=8` 是配方合同，不是逐步计数器观测。

### Prompt 一致性

逐字核对结果：R0/A4-r3/A8/B8-r3 使用共同 IR 原文；C0/C1-r3 恰为 `"r34l1sm\n" + 共同IR`，无其他文本差异。不能声称六组 prompt 字节完全相同。

| 适用case | UTF-8 prompt SHA256 |
|---|---|
| R0、A4-r3、A8、B8-r3 | `f973b1d03e2127dd0887d64b6c3a3992effd9061c6db776dc1fb58864655d5b4` |
| C0、C1-r3 | `449bb1358e47e898141ee0b77bf308ba7c87040c357b3cca9a8bc2b120163a3a` |

## 3. 成功执行时长与媒体规格

“执行耗时”取 `report.execution.execution_seconds`，对应 Comfy history 的 execution_start→execution_success，包含该图执行期间的加载、条件编码、采样、解码/保存等，不是单纯DiT或采样内核耗时。
“任务墙钟”取 `report.finished_at - report.started_at`，还包含等待稳定窗口、预检、资源协调、收集及清理等；未从中反推纯加载时间。
以下按case固定顺序排列，不按快慢排序。

| Case | 执行开始（北京时间） | 执行耗时（s） | 任务墙钟（s） | 分辨率 / 帧 / fps | 本次成功attempt有OOM或取消 |
|---|---|---:|---:|---|---|
| R0 | 2026-09-09 22:10:17 | 556.215 | 558.882 | 480×864 / 362 / 24 | 否 |
| C0 | 2026-09-09 22:22:28 | 541.772 | 543.755 | 480×864 / 362 / 24 | 否 |
| A4-r3 | 2026-09-09 23:16:27 | 571.900 | 697.256 | 480×864 / 362 / 24 | 否 |
| A8 | 2026-09-09 23:27:54 | 988.069 | 1016.335 | 480×864 / 362 / 24 | 否 |
| C1-r3 | 2026-09-10 00:09:38 | 578.413 | 653.024 | 480×864 / 362 / 24 | 否 |
| B8-r3 | 2026-09-10 00:21:55 | 789.079 | 940.392 | 480×864 / 362 / 24 | 否 |

媒体证据：
- A4-r3/A8/C1-r3/B8-r3 的 `report.media.ok=true`；`media-probe.json` 中视频 `nb_read_frames=362`。
- R0/C0 原 report 没有 media 字段，不能伪造为已存在。本次对两份 `video.mp4` 只读运行CPU `ffprobe -count_frames`，均为 H.264、480×864、24/1、362帧；容器时长15.083008s。
- 四份隔离结果 report 的媒体时长为15.083333s；上述微小容器时长差异不改变362帧规格。
- 六份视频均含 AAC、32000Hz、双声道音轨；这不证明对白正确、同步良好或听感合格。
- 六份视频实际 SHA256 均与各自 report 的 `artifact_sha256` 一致；六项 `lease_released=true`。

## 4. 资源观测与Swap基线

统计范围为各case的 **report.baseline + 全部有效 metrics.jsonl 样本**，包括已记录的预检/执行/收尾片段，不包含没有遥测的全部等待时间。
GPU数字是指定UUID整卡 `memory_used_mib` 的**采样最大值**，不是进程独占量、PyTorch allocated峰值或连续采样硬峰值。
内存最低值是host MemAvailable，不是isolated cgroup剩余空间。

Swap基线：存在 `swap_recovery_baseline` 时使用其中 `host_bytes/cgroup_bytes`，否则使用 `report.baseline` 对应字段；优先使用runner自身冻结值，不混用 fleet_evidence 的相近时点值。
增量定义为 `max(0, max(已记录Swap用量) - 冻结基线)`，是峰值净占用增长，不是累计pswpout流量。GiB=2^30 B；MiB=2^20 B。

| Case | metrics样本数 | 执行GPU采样峰值（MiB） | 最低MemAvailable（GiB） | host Swap基线（GiB） | host峰值增量（MiB） | h3父cgroup Swap基线（GiB） | h3峰值增量（MiB） |
|---|---:|---:|---:|---:|---:|---:|---:|
| R0 | 89 | 14771 | 61.821 | 0.933 | 3.809 | 0.797 | 0.000 |
| C0 | 86 | 14771 | 63.159 | 0.937 | 0.000 | 0.794 | 0.000 |
| A4-r3 | 96 | 15192 | 59.919 | 2.170 | 0.000 | 1.248 | 0.000 |
| A8 | 162 | 15180 | 60.093 | 2.130 | 0.000 | 1.208 | 0.000 |
| C1-r3 | 97 | 15084 | 63.196 | 3.403 | 869.504 | 2.134 | 439.414 |
| B8-r3 | 130 | 14075 | 59.382 | 4.249 | 812.762 | 2.563 | 396.871 |

六个成功attempt的 h3 父cgroup `high/max/oom/oom_kill/oom_group_kill` 采样计数均为0。
但不能写成“所有events全零”：C1-r3 的 `sock_throttled` 增加1，B8-r3 基线已为1且无增量。
CUDA OOM 与host/cgroup OOM是不同证据层；失败的 B8-r2 见下一节。

保留的安全边界：host/cgroup Swap硬上限8GiB、相对冻结基线增长上限1GiB、最少可用RAM16GiB及既有内存预算/cgroup/OOM门。
C1-r3和B8-r3虽成功，仍观测到host Swap净增长约869.504/812.762MiB，不能表述为“无交换”。

## 5. 冷加载、预留与环境差异：不能直接排名

- R0/C0 走生产fast worker；history分别缓存节点 `[1,2,3,4,5]` 和 `[1,2,3,4,5,8]`。节点8是RandomNoise，节点10没有缓存，因此不是复用既有采样结果。
- A4-r3/A8/C1-r3/B8-r3 的 history.cached_nodes 均为空，但**Comfy节点无缓存不等于操作系统文件缓存全冷**，也不证明模型传输和内存驻留状态一致。
- A4-r3/A8/C1-r3 使用 `h3-comparison-runtime-20260909.service`、PID2074565、18188、reserve1；同一进程跨case使用，非每case全新环境。
- B8-r3 使用独立 `h3-comparison-runtime-B-20260910.service`、PID2176657、18190、reserve6。00:19:27启动日志明确包含 `--reserve-vram 6 --disable-pinned-memory`。
- B8-r3 的00:22:16日志记录 VDNBranchModel：`0.00 MB loaded, 4080.38 MB offloaded, 220.50 MB buffer reserved`。这是本次成功时的分支卸载状态；不得沿用 B8-r2 的“分支约4080MiB全驻留GPU”描述。
- 底模/文本编码器采用动态VRAM加载；日志中的 `Staged` 不是实际GPU驻留显存。更大的reserve改变驻留与传输开销，不等于算法本身变快或变慢。
- 独立A/C与B worker的读取配置均为 MemoryHigh42GiB、MemoryMax50GiB，父h3-compute聚合上限另行生效；不把worker限额相加当作额外物理内存。
- `a-runtime-live-evidence.json` 记录 ComfyUI0.34.0/core `250b2e9551a7bc7a8ebb5beb07e0fecd2983e04a`、T8 v1.3.3/`5ff46c253192e9d8cae185280fd34f4b4add063b`。B另加此前钉住的VDN最小插件；B启动日志记录torch2.12.1+cu130。本文不声称重新全量审计运行树。
- `admission-swapin-deployment.json` 记录后续恢复性swapin窗口修正已部署；前后attempt并非完全相同的准入版本。修正不构成抹去失败或放宽1GiB增长/8GiB硬门的理由。
- R0/C0的reserve1由本次同PID配置复核支持，未发现每case独立保存的启动参数快照；不把当前配置证据夸大为完整历史运行清单。

因此本报告只列真实耗时，不计算算法加速比、不将重试前后的值混为一次运行、不按耗时或显存选优。

## 6. 可解析的内存控制器采样

来源：`metrics.jsonl → memory_diagnostics.memory_bandwidth.perf_stat`。
仅纳入returncode=0且 `uncore_imc/data_reads/`、`data_writes/` 为数值MiB的记录。
下表是每次约1秒perf采样窗口的传输量范围（MiB/窗口），不是整段生成的平均带宽或瞬时峰值；不将读写各自最大值相加冒充同一时刻峰值。

| Case | 有效读/写样本数 | 读观测范围（MiB/约1s窗口） | 写观测范围（MiB/约1s窗口） |
|---|---:|---:|---:|
| R0 | 89/89 | 39.91–4003.69 | 9.08–1823.70 |
| C0 | 86/86 | 43.13–4587.21 | 9.26–1947.32 |
| A4-r3 | 96/96 | 37.38–5056.18 | 8.42–2848.53 |
| A8 | 162/162 | 32.26–5525.64 | 7.27–2646.30 |
| C1-r3 | 97/97 | 46.57–5501.13 | 9.77–2656.81 |
| B8-r3 | 130/130 | 89.24–5024.55 | 16.02–3073.67 |

perf为host级观测，含其他进程活动，且窗口间存在间隔；没有足够的同步PCIe/GPU内核/文件IO归因证据，**不能据此证明内存带宽、PCIe或存储是瓶颈**。

## 7. 失败尝试：不计入成功结果

| Attempt | 是否提交 | 原始失败依据 | 分类 |
|---|---|---|---|
| A4 | 否 | `ConnectError: [Errno 111] Connection refused` | 提交前连接失败，不是算法失败或OOM |
| A4-r2 | 否 | `RuntimeError: swap limit crossed` | 提交前资源门拒绝，不算推理取消 |
| C1 | 否 | `no stable idle swap window within 180 seconds; no submission` | 稳定窗口超时 |
| C1-r2 | 是 | report `swap limit crossed`；history node10 `execution_interrupted` | 安全取消；不是CUDA OOM |
| B8 | 否 | `no stable idle swap window within 180 seconds; no submission` | 稳定窗口超时 |
| B8-r2 | 是 | history node10 `torch.OutOfMemoryError`；已分配13.95GiB、请求367.50MiB、CUDA余量7.94MiB | CUDA OOM；不算成功 |
| D4 | 是 | report `swap limit crossed`；history node10 `execution_interrupted` | 先前D尝试安全取消，不算通过 |

B8-r2 的runner顶层错误为 `worker history does not prove successful execution`，根因应以history中的CUDA OOM为准。
C1-r2、B8-r2、D4记录已释放lease；提交前失败中的 `lease_released=false` 不等于已经获取且遗留lease，必须连同lease_attempted解释。
不把失败attempt的等待、部分采样或OOM前时长计入成功case的算法速度。

## 8. D4-r2：进行中，不纳入验收

本次09:17–09:18只读快照中：
- `D4-r2/report.json` 为 `status=running`、`submit_acknowledged=true`；尚无终态history或视频工件。
- prompt_id：`9e89177d-59cd-49d0-891a-282d47aa2046`。
- 独立worker：`h3-comparison-runtime-D-fast-20260910.service`，PID2655155，`127.0.0.1:18191`，同一4060Ti UUID。
- 配方合同为480×864、362帧、24fps、4步。主代理告知当前在执行原生VSA；本报告不替代其内核路径与终态验收。
- 后续即使执行状态变化，本快照也不能自动升级为通过。**等待主代理验收，未干预其运行。**

## 9. 可追溯索引

以下路径均相对上述Ivan证据根。所有六项均读取 `<case>/report.json`、`metrics.jsonl`、`history.json`、`workflow.json`；视频哈希本次重新核对。R0/C0媒体由本次只读CPU探针补证，未写回其report。

| Case | 实际upstream prompt_id | video.mp4 SHA256 |
|---|---|---|
| R0 | `17a7b97e-90a1-4df8-9fda-fbfef25adf6e` | `0eff3c5bc4a053f35cbf8e90d6507a3f6834536dbb0e01eba989defe719022d1` |
| C0 | `4b509812-c59f-4f97-8177-f67d4fb4703b` | `d2700faf4c60fada6d153418bf1a3a189b0ba02169b008990390d136eec304d0` |
| A4-r3 | `0fee52ca-9feb-427c-b5d7-8a32150588f2` | `cf884a83c5c8b6c6bf010f020ae3590ebe850b1379963a1ae510983337ca7b89` |
| A8 | `6a57f187-7782-4c32-870d-236cf0c56e6a` | `9f0c051d29a85bcef3bb8077d2704710b48e0715d08559db69366da99b84b04e` |
| C1-r3 | `5a2aa245-81e3-4693-86d0-a144dc33a9d3` | `c3cbc6e818c442cbb2210a313e78b0410bc63face9e11e1daacbd2704d1a1be9` |
| B8-r3 | `d50c1e4b-ae9f-4463-ac46-4d869ad5530f` | `aafa318836c47f64e4ec2018518f8216502ff057e097ec1e8dfa8df8df5c1d0a` |

### 本次读取的证据文件SHA256

- `R0/report.json`：`493faf0b7071529c91be191472e962dc350170ececc5a212480eb6fe7d8a808a`
- `R0/metrics.jsonl`：`1ee4021b189734a033043bdde4cc70d7931447e383bd1556297efdb86fecd3c2`
- `R0/workflow.json`：`d0f1172aef4f719834e299cf496bd46cf8e1f7db483b0ef1ed1be5f053601cba`
- `C0/report.json`：`e172b7d11428651f13da359b0ae45eb304a502051214392d87756271044ee50d`
- `C0/metrics.jsonl`：`c69159006a36a0ec9e5513fdda5313ebd731eb8da58b37800f6fda8f709e2847`
- `C0/workflow.json`：`bf42ce66082b414175c7767f75f6e6ceda188d755f7e0e7bff8a5be2f17de6a5`
- `A4-r3/report.json`：`c225a9aee7df5eca47111992c332507f74db2e75f1241f7514ace2fa7e4bb9ad`
- `A4-r3/metrics.jsonl`：`d28e02d9f8c4e931e9adc3a4afa4197a8d2de6a523bfe01ab3226a7ae5d74284`
- `A4-r3/workflow.json`：`20b53db849fc05c633ce50e4e8c7d5e615eebc933c97c672249fdc91227501e0`
- `A8/report.json`：`bd42e869b6a97ec795126d6693b5e4f2b0a8e228a42487476266a54015d52018`
- `A8/metrics.jsonl`：`093f960e04abe8ee846e350706ef8dc68480c8a71924a38f657f1d3698f13cb0`
- `A8/workflow.json`：`f84e9e0f013ba7cc97296c76ebff248d9fb0ac4d6637afdf7d099f4960b16577`
- `C1-r3/report.json`：`11dace06b02d757e1a32b4054bc844d732236e7f880c92900be875bb57cfcabe`
- `C1-r3/metrics.jsonl`：`002953639101319de74b77d6d9d30d6578cf025f44d93b47632c80d55478c441`
- `C1-r3/workflow.json`：`732873e55398d4450302efa422548be3973f0db74082f61b107a8931fdf95c24`
- `B8-r3/report.json`：`a093d9bade21a03a9e1c1f0db29aa75ed99d25554e952544233999573fd4e8f7`
- `B8-r3/metrics.jsonl`：`97d047ba2a8e57861b76a9f5011c11078d32b0ef6b35fc03b5a079643d5ee0da`
- `B8-r3/workflow.json`：`f4c8e156c1b100db7d65ebef4df6a95c94a9dd4654cf65e2116b2f09c2ba2623`

补充只读来源：`a-runtime-live-evidence.json`、`admission-swapin-deployment.json`、B独立worker历史journal、systemd启动参数、fast进程GPU环境与NVIDIA驱动GPU身份文件。空文件 `a-runtime-evidence.json` 未作为有效证据。

## 10. 人工确认

技术执行完成、资源门通过和媒体规格成立，不代表画质、人物一致性、动作、对白或艺术效果获得认可。**最终效果与是否接受各组结果，待用户人工确认；本报告不作质量判断或选优。**
