# 2026-09-09 TP2 同条件 A/B：四种配置实测

条件：ai-X10DRG，GPU4(V100-PCIE-32GB)+GPU5(PG500-216)，TP=2，ctx 32768，
util 0.93，seqs 4，batched 2048，KV fp8_e5m2，FLASH_ATTN_V100，mamba align，
同一 bench 脚本（短输出 / 8K / 24K prefill / 4 路并发），temperature=0。

| 配置 | 单流 decode tok/s | prefill@24K tok/s | 4路并发聚合 tok/s | 投机接受率 |
|---|---|---|---|---|
| QUASAR-QAT NVFP4 + MTP2 | 54.5 | 1,287 | 135.7 | 70-81% |
| EfficientThink W4A16 + DFlash2(8)（作者打包 draft） | 47.7 | 1,485 | 65.6 | 25-44% |
| **EfficientThink W4A16 + 原生 MTP2（采用）** | 51-53 | 1,271 | **143.0** | 69-77% |
| EfficientThink W4A16 + DFlash2(4)（调优） | 40.4 | 1,378 | 61.6 | 48.7% |

结论：新模型瓶颈在 DFlash2 draft 而非模型本身；原生 MTP2 反超 QUASAR 基线
（并发 +5.4%），DFlash2 路线在本机（SM70 + 5 层 draft 每步前向过贵）画句号。

原始数据：results/*.json（quasar_baseline / tp2_dflash2 / new_mtp2 / new_dflash4）。
注：切换前生产 env（qwen38-v100-tp2.env）未启用投机解码，上表 QUASAR 基线
为「QUASAR + MTP2」最优配置；即便如此，新模型 MTP2 仍胜出。
