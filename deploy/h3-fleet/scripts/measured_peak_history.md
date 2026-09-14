# 离线实测峰值预算

仅 Python 标准库、本地文件读取/历史写入；不请求 report 中的 URL，不运行 GPU、SSH、ffprobe 或服务操作。

## 父协调器接口

```python
history = load_history(history_path)
result = resolve_budget(case, static_budget_bytes, history, profile_key,
                        margin_bytes=2 * GIB, margin_ratio=.1)
budget = max(static_budget_bytes, result["candidate_budget_bytes"])
```

返回 `candidate_budget_bytes`（同 `budget_bytes`）、`profile_key`、`provenance`、`fallback`、`reason`。
`resolve_budget` 是无 I/O 纯函数；只接受 `load_history` / `collect_peak_run` 返回的 `VerifiedHistory`，不信任普通 JSON 的 verified 声明。
无可验证对应历史时保留**调用方原 manifest 的正值预算**；18GiB 仅为独立 CLI 默认，不覆盖父传入的 4GiB 等值。
缺失来源时不降低同 key 已保存的历史 floor。父仍执行静态下限和所有既有 admission/lease/PSI/swap/OOM 保护。

`profile_key = sha256(json.dumps(profile_binding, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()`。
binding 必须包含 `case, workflow_sha256, validator_sha256, gpu_uuid, unit, endpoint, shape, runtime_profile_id`。
`shape` 含 `width,height,length,fps`，其余字段也参与 exact hash。
workflow 是冻结的**输入图原始字节**（不是 runner 改过 prefix 的提交图）；图 SHA 锁住 precision、权重/强度及 trigger，不推导相似图、不跨 A4_C05→C1 别名。
同一 GPU 名字不构成匹配。父负责检查活进程 boot/PID/start_ticks，变化后更换显式 `runtime_profile_id` 并人工建档。

## 从新 runner report 自动采集

```bash
python3 -B scripts/measured_peak_history.py collect \
  --from-report /absolute/report.json --case A4_C05 \
  --metrics /absolute/metrics.jsonl \
  --workflow /absolute/frozen-input-workflow.json \
  --media /absolute/video.mp4 --history /absolute/peak-history.json
```

纯接口：`build_source_manifest(report_path, *, case, metrics_path, workflow_path, media_path)` 返回 descriptor；
`collect_peak_run(descriptor, *, margin_bytes=2*GIB, margin_ratio=.1)` 验证并计算；`save_history(path, collected)` 追加。
也可 `collect --from-local-files /absolute/source-manifest.json --history ...` 读取已审核 descriptor。
`resolve --history ... --case ... --profile-key /absolute/profile-key.json` 输出预算；profile-key 文件内容是 JSON SHA 字符串。

新 batch 的 `report.tasks` 必须保存 profile_binding/key；baseline 取本 task 的
`resource_reconciliation.baseline_sample` 或 `admission_baseline`，两者存在须相等，绝不使用旧 batch baseline。
resource_reconciliation 必须 finished、case/boot/started_at 一致且无 observation_error/failure_samples。
原始 JSONL 接受 `{sample,identities}` 或直接 sample；筛选本 task baseline 之后至执行完成的覆盖样本。
既有 batch 前序/后序样本不归本 case；窗口内要求严格递增且相邻不超过 15 秒，包含完成后的覆盖点。
baseline 距提交不超过 15 秒；证据不足拒绝而不猜值。

多 task 只用 `sample.perworker_subtree[case] = {case,prompt_id,memory_current_bytes,identity}`。
identity 使用 runner workers 身份，包含 Id/MainPID/start_ticks（兼容 process_start_ticks）、isolated_url/gpu_uuid；每个有效样本身份必须相同。
单 worker 才允许 `single_worker_aggregate`；其他为 `perworker_subtree`，scope 写入 provenance。
`peak_delta = max(执行窗口内该 scope 的 memory.current) - 同 scope baseline`；不是 RSS、压缩 zram 或工作集折扣，也不是连续硬件 peak 计数器。

必须验证 report 成功、lease 释放、模型卸载、terminal reconciliation、非缓存 sampler、完整帧数/时长/音频、视频 SHA、执行/清理时间绑定。
新 batch 的成功标志为 generated_pending_quality_review_drained + both_unloaded；离线仅复核 runner 已有 media 成功证明，不重新解码。
来源缺失/哈希不符/未知格式返回有 reason 的 fallback（直接 collect 拒绝且不写历史）。

历史格式：`{"schema_version":1,"sources":[descriptor,...]}`；descriptor 含 case/run_id/prompt_id/profile_key/profile_binding/worker_count/scope/identity，
以及 `files.{report,metrics,workflow,media}.{path,sha256}`，可选单独 pinned baseline 文件。
自动 builder 从已固定哈希的 report 提取 task baseline，不必另造 baseline 文件。
保存 floor：`历史最大 peak_delta + max(2GiB, ceil(10%*peak_delta), 显式 margin, 显式 ratio*peak_delta)`；保留全部旧条目，floor 只升不降。
预算公式使用额外的 `budget_peak_delta_bytes`：取执行窗口峰值、首个完成后样本 delta、同 worker accounting 峰值的最大值；原 `peak_delta_bytes` 仍仅表示执行窗口，三种证据分别标明 scope/时间范围，绝不借用 aggregate 或其他 worker 的峰。
成功 report 内 `task.peak_budget.candidate_budget_bytes` 也是经源哈希/任务验证的 floor，保存及重载不得降低它。
提高 margin 后应以相同参数采集保存；纯 resolver 不修改历史。单写入者，父负责并发锁与发布 history SHA。
哈希防止错配/篡改未更新的来源，不是数字签名：报告/manifest 的采集渠道及 runtime-profile 人工审核仍是信任边界。
结果标记 experimental=true、hardware_certified=false；不宣称硬件认证或双视频验收。
