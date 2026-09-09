# qwen38-et-v100 — Qwen3.8-27B-EfficientThink W4A16 + 原生 MTP2（V100 TP2 生产部署）

2026-09-09 起的本机默认生产推理部署。同条件 A/B 实测并发聚合吞吐 143.0 tok/s，
较前一代 QUASAR-QAT+MTP2（135.7）+5.4%，投机接受率同档（69-77%）；见
`benchmarks/20260909-tp2-ab-summary.md`。

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
            └─ 容器 qwen38-v100-tp2-vllm（GPU4+GPU5 TP2，UUID 注入）
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
| 其余（TP2/seqs4/backend/KV 根目录等） | 沿用旧生产 | — |

## 与旧部署的差异

1. MODEL_PATH → 新模型；2. 新增 MTP2 投机解码（旧生产无投机）；3. util 0.90→0.93；
4. 批量 tokens 显式 4096。内层脚本的 TP2 Qwen3.8-NVFP4 对齐门禁
（`*Qwen3.8*NVFP4*` 路径匹配）对本目录路径不触发——它守护的是 CT-NVFP4
（sm70_turbomind）路径；新模型走 modelopt loader，已于 2026-09-09 在同机
TP2 单独实测验证（加载、数值、接受率均正常）。

## 已知权衡

- KV 池实测 930,611 tokens（util 0.93，16.6 GiB）——高于 QUASAR 时期的 632K；
  单个 196K 长请求仅占池 ~21%，长文并发余量反而更充裕。
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

- 2026-09-09：建立本目录；生产默认切换至 EfficientThink W4A16 + 原生 MTP2
  （systemd：qwen38-v100-tp2-{lmcache,vllm}.service → env/wrapper 指向本目录）。
  上线验证通过（见「上线验证」）；验证器聚合门禁因 fork prefix-hit 指标口径判
  false，功能用例 9/9 全过。
