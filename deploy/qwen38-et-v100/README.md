# qwen38-et-v100 — Qwen3.8-27B-EfficientThink W4A16 + 原生 MTP2（V100-class TP4 生产部署）

2026-09-09 起的本机默认生产推理部署；2026-09-10 从 GPU4+5 TP2 切换为同一 NUMA1
内 GPU4-7 TP4。TP4 生产实测 24K 单流 decode 70.71 tok/s，KV 池 2,525,320
tokens；单独 A/B harness 的 4 并发聚合吞吐 147.29 tok/s。TP2 已退役，仅保留历史
基准与稳定的 systemd/container 命名以兼容 ai-router、监控和运维脚本。

## 模型

- 来源：`hf-mirror.com/nerkyor/Qwen3.8-27B-EfficientThink-Uncensored-K3-Opus5-Grok4.6-GPT5.6Sol-SFT-SimPO-MTP-NVFP4`（W4A16 子目录）
- 本地路径：`/home/ai/models/Qwen38-EfficientThink-W4A16`
- 量化：modelopt NVFP4（W4A16，激活 BF16）；架构 qwen3_5 hybrid（48 mamba + 16 full-attn + 5 draft SWA 层），自带原生 MTP 头（vision-mtp-bf16.safetensors）
- 完整性：18/18 文件 SHA256 校验通过（2026-09-09，含 DFlash2-FP8 子目录）

## 部署拓扑

```
systemd --user (Linger=yes，开机自启)
├── qwen38-v100-tp2-lmcache.service
│     └─ scripts/run-et-tp2-lmcache-container.sh ──► ai-router 通用 runner
│           └─ 容器 qwen38-v100-tp2-lmcache（DRAM L1，96G，NUMA1）
└── qwen38-v100-tp2-vllm.service  (Requires/BindsTo lmcache)
      ├─ ExecStartPre: ai-router check-qwen38-v100-tp2-lmcache.py --require-vllm-guards
      └─ scripts/run-et-tp2-container.sh ──► ai-router 通用 runner
            └─ 容器 qwen38-v100-tp2-vllm（GPU4-7 TP4，4×UUID 注入）
                  └─ vllm serve 127.0.0.1:18107  served: siyuan/qwen38-v100-196k
                        └─ ai-router 池成员 ai-qwen38-27b（defaults.yaml）
```

## 与 1Panel 项目的融合方式

**复用（未分叉，单一事实来源）**：
- `deploy/ai-router/scripts/run-qwen38-v100-tp2-container.sh`（通用容器 runner：
  LMCache 解析/健康门禁/补丁校验/GPU 校验/docker run 全流程，全参数化）
- `deploy/ai-router/scripts/run-qwen38-v100-tp2-lmcache-container.sh`（LMCache sidecar）
- `deploy/ai-router/scripts/run-qwen38-v100-tp2-vllm.sh`（内层 vllm serve：mamba align、
  prefix caching、chunked prefill、qwen3_coder 工具解析、API key 注入，全 env 驱动）
- `deploy/ai-router/scripts/check-qwen38-v100-tp2-lmcache.py`（ExecStartPre 门禁）
- LMCache 配置解析链（defaults.yaml + /opt/1panel/ai-router/settings.yaml，enabled=true）
- 单元名/容器名/端口 18107/served 名 `siyuan/qwen38-v100-196k` 全部不变 →
  ai-router、监控、运维习惯零改动

**新增（本目录）**：
- `env/qwen38-et-w4a16-mtp2.env` — 新模型生产参数（systemd EnvironmentFile 指向此处）
- `scripts/run-et-tp2-*.sh` — 两个薄包装（定位到 ai-router 通用 runner，路径相对仓库解析）
- `scripts/{start,stop,status}-qwen38-et.sh` — 运维便捷命令
- `benchmarks/` — A/B 数据与结论；`rollback/` — 切换前原状快照与回滚步骤

## 关键参数与依据

| 参数 | 值 | 依据 |
|---|---|---|
| MODEL_PATH | /home/ai/models/Qwen38-EfficientThink-W4A16 | 新模型 |
| SPECULATIVE_CONFIG | {"method":"mtp","num_speculative_tokens":2} | A/B 最优；draft=Qwen3_5MTP 共享 embedding/lm_head，fork 自动套 1Cat SM70 MTP 默认 |
| GPU_MEMORY_UTILIZATION | 0.93 | 实测验证值；196K ctx 单请求需 <244K KV tokens 池 |
| MAX_MODEL_LEN | 196608 | 与旧生产一致（路由别名 196k） |
| MAX_NUM_BATCHED_TOKENS | 4096 | 沿用旧生产默认；> mamba align 块 1648 约束 |
| KV_CACHE_DTYPE | fp8_e5m2 | 沿用；FLASH_ATTN_V100 storage-only 路径 |
| NCCL_P2P_DISABLE=1 + UUID 注入 + --ipc host | 沿用 | 实锤：V100+PG500-216 混插卡不开必现 NCCL 无限死锁 |
| TENSOR_PARALLEL_SIZE | 4 | GPU4-7 同属 NUMA1；TP4 实测通过 |
| LMCACHE max-gpu-workers | 随 GPU_UUIDS 自动计数（当前 4） | 修复 TP2 硬编码只允许 2 rank 注册的问题 |
| MAX_NUM_SEQS | 8 | 从 4 提升；解除并发调度上限，单流性能无回归 |
| 其余（backend/KV 根目录等） | 沿用旧生产 | — |

## 与旧部署的差异

1. MODEL_PATH → 新模型；2. 新增 MTP2 投机解码（旧生产无投机）；3. util 0.90→0.93；
4. 批量 tokens 显式 4096。内层脚本的 TP2 Qwen3.8-NVFP4 对齐门禁
（`*Qwen3.8*NVFP4*` 路径匹配）对本目录路径不触发——它守护的是 CT-NVFP4
（sm70_turbomind）路径；新模型走 modelopt loader。2026-09-09 的历史 TP2 验证已完成
加载、数值和接受率检查；该 TP2 生产形态已于 2026-09-10 退役。

## 已知权衡

- TP4 生产 KV 池实测 **2,525,320 tokens**（util 0.93），是 TP2 930,611 的
  **2.71×**；196K 请求理论最大并发 **12.84×**。TP4 的首要收益是 KV 容量、
  单流吞吐和更高批处理上限；PCIe 无 NVLink 条件下并发扩展仍然是次线性的。
- LMCache(MP connector) × MTP2 已上线验证：投机接受率 83.3%（mean accept 2.67），
  prefix 复用有效（warm TTFT 2.07s→0.55s，API cached_tokens=1600）。
  已知问题：fork 的 Prometheus `vllm:prefix_cache_hits_total` 只覆盖部分复用路径
  （同 prompt 连发 +1600 正常；validator 变体 warm prompt 窗口 0 增量），API 口径
  cached_tokens 准确——仓库验证器 smoke/decode 的聚合门禁因此判 false
  （功能用例 9/9 全过）。属监控口径问题，非功能回归，建议后续在 fork 层修正。

## 上线验证（2026-09-09）

- 启动：权重 11.39 GiB/卡（加载 10.3s），KV 16.6 GiB → **930,611 tokens**，
  max_model_len=196608 接受，LMCache L1 80G 就绪，Application startup complete
- 冒烟（scripts/smoke-qwen38-et.py）：短输出 decode 52.5 tok/s；重复前缀
  prefill 7.3K-13K tok/s（prefix caching 复益）
- 仓库验证器 validate-qwen38-v100-tp2.py（--model-path 已指向新模型）：
  - smoke：short / prefix-cold / prefix-warm **3/3 passed**（warm TTFT 2.07→0.55s）
  - decode：**6/6 用例 passed**——24K 单流 decode **58.6 tok/s**（TTFT 2.22s，
    前缀命中 86.6%）；24K×3 并发聚合 68.2 tok/s；冷 prefill 952 tok/s @24K
  - 聚合门禁 passed=false：唯一原因即上述 Prometheus hit 口径问题
- 投机解码：mean acceptance length 2.67，逐位 0.917/0.750，平均 83.3%

## TP4 上线验证（2026-09-10）

- 硬件：NUMA1 内 GPU4-7；实际为 1×Tesla V100-PCIE-32GB + 3×Tesla PG500-216，
  均为 32 GiB、compute capability 7.0。GPU0-3 为 P40，不参与本部署。
- 启动：TP4/world_size=4，约 220s 完成；四卡稳态显存约 31.0-31.6 GiB/卡；
  KV 池 **2,525,320 tokens**，196,608 context 理论并发 **12.84×**。
- LMCache：原 runner 硬编码 `CUDA_VISIBLE_DEVICES=0,1` 与 `--max-gpu-workers 2`，
  导致 TP2→TP4 后每轮仅 2 个 rank 注册成功、其余 2 个报
  `outcome_unknown_remote_error`。已改为按 `GPU_UUIDS` 自动生成可见设备列表和 GPU
  worker 数；4/4 rank 均成功注册 65 层 KV cache，sidecar 80 GiB L1 正常。
- 独立 TP4 benchmark（无 LMCache）：短输出 55.97-56.18 tok/s；8K / 24K TTFT
  8.70s / 19.51s；长上下文后 decode 72.27-72.32 tok/s；4 并发聚合
  **147.29 tok/s**，单流 42.18-43.47 tok/s。结果文件头残留旧 harness 的 TP2/DFlash2
  configuration 文案，但请求目标和运行容器已核实为 TP4 + native MTP2。
- 生产 validator：功能用例 smoke **3/3 passed**、decode **5/5 请求 passed**（另 1 条汇总记录）；24K
  单流 decode **70.71 tok/s**，24K×3 并发聚合 **61.52 tok/s**；单流/三并发前缀
  命中率 86.59% / 93.05%。聚合 `passed=false` 仍仅因已知 Prometheus prefix-hit
  口径问题；API `cached_tokens=1600` 明确证明 LMCache warm 命中。
- MTP2：近期大样本 mean acceptance length 2.95，逐位接受率 98.3% / 97.2%，
  average draft acceptance **97.7%**。
- 对比 TP2 生产基线：24K 单流 58.6→70.71 tok/s（**+20.7%**）；KV
  930,611→2,525,320（**2.71×**）。初次 24K×3 聚合仅 61.52 tok/s，随后确认
  主因是生产 `MAX_NUM_SEQS=4` 限制调度，不是 TP4 算力没有收益。

### 并发调优（MAX_NUM_SEQS=8）

将 `MAX_NUM_SEQS` 从 4 提升到 8 后，使用同一 24K prompt 和生产 validator 复测：

| 并发 | 聚合 decode | 单流 decode | 相对单流吞吐 | TTFT |
|---:|---:|---:|---:|---:|
| 1 | 71.24 tok/s | 71.24 tok/s | 1.00× | 0.51s |
| 3（旧 seqs=4） | 61.52 tok/s | 20.5-31.7 tok/s | 0.86× | 2.4-6.7s |
| 4 | **141.09 tok/s** | 36.2-46.4 tok/s（均值约 43.6） | **1.98×** | 0.68-2.35s |
| 8 | **183.49 tok/s** | 23.3-31.0 tok/s（均值约 29） | **2.58×** | 4.18s |

结论：

- `MAX_NUM_SEQS=8` 对单流无可测回归（70.71→71.24 tok/s），应保留为生产上限。
- 4 路是交互与吞吐平衡点：聚合接近单流 **2×**，单请求仍约 43.6 tok/s。
- 8 路可稳定运行，聚合提高到 **183.49 tok/s**；代价是单请求 decode 相对单流
  下降约 **59%**（71.24→约 29 tok/s），TTFT 提高到约 4.18s，适合吞吐优先任务。
- TP4 并发扩展未达到线性 4×/8×，限制来自无 NVLink 的 PCIe all-reduce、调度和
  每请求 KV/计算竞争；但相较旧 seqs=4 的 61.52 tok/s，8 路总吞吐已提高 **2.98×**。

## 运维

```bash
scripts/start-qwen38-et.sh    # 起链路并等 18107 就绪（最长 20 分钟）
scripts/stop-qwen38-et.sh
scripts/status-qwen38-et.sh   # 单元/容器/GPU/端点/接受率一览
journalctl --user -u qwen38-v100-tp2-vllm -e --no-pager
docker logs -f qwen38-v100-tp2-vllm
```

回滚：见 `rollback/README.md`（三条 cp + daemon-reload + start）。

## 变更记录

- 2026-09-10：生产从 GPU4+5 TP2 切换至 NUMA1 GPU4-7 TP4；LMCache GPU worker
  槽位改为随 GPU UUID 数自动配置。TP4 生产上线验证通过，TP2 退役（稳定单元名保留）。
- 2026-09-09：建立本目录；生产默认切换至 EfficientThink W4A16 + 原生 MTP2
  （systemd：qwen38-v100-tp2-{lmcache,vllm}.service → env/wrapper 指向本目录）。
  上线验证通过（见「上线验证」）；验证器聚合门禁因 fork prefix-hit 指标口径判
  false，功能用例 9/9 全过。
