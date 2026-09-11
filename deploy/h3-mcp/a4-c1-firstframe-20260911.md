# A4_C1 首帧四步验证

用户指定先测试原照片首帧＋A4加速＋C1人物写实增强，不降低480×864、15秒、24fps和原生音轨。

## 已完成

- 现场确认single-realism已有首帧输入端口和 `a4v12_people10_fp32.safetensors`，无需重启GPU worker或改LoRA强度。
- 新配置 `H3_I2V_A4_C1_TURBO4` 从固定A4_C1工作流派生，只增加首帧加载并将T2VA改为I2VA。每次校验重新和原配方逐项比较，拒绝改变采样步数、权重、音轨、尺寸或素材连接。
- 原始提示词不覆写；r34l1sm只加入执行提示词。大整数种子保持原值。
- 专项测试12项通过；此前相关组合回归125项通过（包含前11项专项测试及原Connector/多模态测试）。
- 最终组合回归144项通过，覆盖12项专项、原Connector、多模态工作流、资格和生命周期；只有既有依赖弃用警告，`git diff --check`通过。
- 新建独立草稿 `e00cd1dd056a`，原项目 `7732e9926474` 和已成片产物未修改。
- Fleet已发布 `/mnt/ivan-ext4-offload/h3-fleet-releases/20260911-a4-c1-i2v-r1`，所有GPU worker身份未变；只给新项目指定验收许可，正式资格仍为false。72GiB、Swap、PSI和独占保护未改变。
- Fleet备份及切换审计：`/mnt/ivan-ext4-offload/h3-fleet-cutovers/20260911-a4-c1-i2v-r1`。对应配置版本 `b1d3a49b7773843bee889faa6ea9084c7ec305b9840c3fdd872136f84ee60c9b`。

## 已解除的发布阻断

Studio切换最初被安全审批拒绝，随后用户明确回复“继续”。已切换至不可变候选 `20260911-a4-c1-i2v-r1` 并仅重启Studio一次，运行PID为1856889。Control、Router API及GPU worker未重启。原部署脚本使用无认证 `/health` 导致探测误报；通过有认证的实际任务API确认服务可用、核对候选文件哈希并补齐部署回执，没有再次重启。

已将新草稿显式改成C1首帧四步，确认复用的原提示词并启动一次。素材SHA256仍为 `a80da8df3ab36cb5af5ce8d96792f69a045ae435a41febdc6efec8415232c63a`。

## 本次实际执行

- 项目：`e00cd1dd056a`。
- Studio run：`run_f2b2b7cecd094bf0964d70f182f1429e`。
- 执行：`studio_e00cd1dd056a_preview_afa99609934a4d5fbee979eaf5b1d48b`。
- Fleet任务：`09be5bee81194ae7a797e2bf1198c9ba`。
- ComfyUI任务：`08916c8f-e204-4ce0-b682-cb3e6ba92581`。
- 实际后端：single-realism，4060 Ti；节点20加载原照片，节点10已报告非缓存采样4/4步并进入解码、保存及execution_success。
- 部署/操作回执和30秒监控记录：`/home/ai/.local/state/h3-studio-ivan-production/deployment/a4-c1-firstframe-20260911-r1`。
- 已完整成片，等待人工评价；未自动通过画质审核或启动后续步骤。

## 成片验收结果

- 新产物：`out_5f0bec8676f54efc92f526f0f0a63be2`；SHA256 `3dcd50b6d567b6c7abfe2b8dc72d969d05e5a6ceb15cdcdd37ce9fd7318d1a80`。
- 实际执行583.480秒（9分43秒），Studio端总耗时586.344秒（9分46秒）；原14步执行1587.827秒（26分28秒）。本次单样本执行耗时约为原来的36.7%，约2.72倍速度，不代表稳定均值或同等画质。
- 保持480×864、362帧、24fps、15.083秒及音轨；完整ffmpeg解码通过。
- 从实际Tailscale页面验证播放成功、画幅contain完整显示、Range返回206、下载返回200且与本地产物SHA一致。
- 原项目视频SHA仍为 `dca288ef366326194062da6cdb4a1867094f4533e50a62ae2d52c106c46b919c`，未覆盖。
- Cgroup采样峰值66.279GiB，低于72GiB；峰值增量15.549GiB，未降低原24GiB候选fallback预算。
- PSI avg10峰值均为0，Swap存量减少73728字节，无OOM/Xid；少量swap IO仍由原生产稳定窗口约束，不因此放宽新任务准入。
- 注意：准入预测55.125GiB是折扣缓存后的有效工作集投影，实际66.279GiB是原始cgroup采样峰；不能将两者当作同口径准确预测。pgsteal统计回收约6.98MiB，不意味着折扣的21.61GiB缓存实际已释放；未自动校准reclaim_factor或减少预算。
- Fleet已completed，Studio awaiting_approval，reconciliation_required=false；现场active任务为空、三个lane队列均为空。模型热驻留与任务释放分开记录，空闲卸载仍由原300秒策略处理。
- 页面：`https://ai-x10drg.taild500c8.ts.net:8445/?project=e00cd1dd056a&stage=preview`。

该分支本次完整成片通过，画质仍待用户评价；只具备指定验收证据，尚未自动授予正式首帧配方资格或并发资格。Control/WorkBuddy的新配方选择及通用页面选择入口未在本次扩展。取消288×512草案；768P及自动排队恢复不是本次已交付内容。
