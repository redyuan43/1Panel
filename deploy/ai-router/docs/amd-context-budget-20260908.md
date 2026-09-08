# AMD context budget — 2026-09-08

Decision: **retain 131,072 tokens (128 Ki tokens)**. Do not restart or infer to try a larger context in this round. 192K and 256K fail the required 1 GiB GPU reserve even before any increased compute allocation. 160K has not demonstrated an OOM; it fails the conservative budget needed to authorize maintenance validation. Keep batch/ubatch, model, quantization, checkpoint count and offload policy unchanged.

## Observed baseline

Read-only collection through SSH AI → AMD. User unit `qwen38-flash-next-amd-rocmfpx-128k.service`, PID 3275, started September 4. `/proc/3275/cmdline` confirms `-c 131072 -ctk q8_0 -ctv q8_0 -ngl 99 -ngld 99 -b 2048 -ub 512 -np 1 --ctx-checkpoints 4 --spec-type draft-mtp --spec-draft-n-max 3 --no-warmup`, mmap and the existing projector. No new CPU offload, swap, model or quantization is proposed.

- VRAM carve-out: 96.000 GiB. Sysfs used: 94.130032 GiB; free: 1.869968 GiB.
- `/proc/3275/fdinfo/4`: model process VRAM 93.650112 GiB; fd 5 is the same DRM client and must not be counted twice. About 0.479919 GiB belongs to other GPU allocations.
- GPU GTT allocation is already 0.719 GiB; it is separate host-memory pressure, not extra VRAM.
- System RAM: 30.981 GiB total, MemAvailable 6.465 GiB. Swap already used: 2.454 GiB; no budget credit is assigned to swap. Model RSS 9.796 GiB, historical RSS high-water 26.042 GiB; current RSS does not prove startup peak headroom.
- Main file: `/mnt/data853/AI-models/qwen3.8-flash-next-rocmfp4-imatrix/model/Qwen3.8-Flash-Next-ROCmFP4-FAST-v2-ple16.gguf`, 93,484,237,760 bytes. Draft: sibling `mtp/Qwen3.8-Flash-Next-MTP-ROCmFP4-FAST.gguf`, 2,444,519,296 bytes. Projector: `/home/ivan/model-sources/qwen3.8-flash-next-amd/vision/mmproj-F16.gguf`, 904,004,000 bytes.
- Build: `/home/ivan/github/qwen3.8_flash_next_amd/build-vulkan-rocmfpx-5e085d12`; Vulkan ON, HIP OFF. Source: sibling `vendor/llama.cpp-rocmfpx`, HEAD `5e085d123eead2e89b5c19f824fccb05727da6a2`.

Only GGUF metadata and tensor directory offsets were read; tensor payloads were not loaded for inference. The model metadata declares native context 262,144, which does not establish that this host can allocate it safely.

## Allocation derivation

GGUF: 48 trunk layers, 12 full-attention/QSA layers, 36 recurrent layers, key/value dimension 256, 2 KV heads; QSA indexer dimension 128, ratio 4, 4 query heads. One dense MTP layer. Q8_0 stores 34 bytes per 32 values.

| Persistent allocation | Bytes per context token | 128K GiB |
| --- | ---: | ---: |
| Main Q8 K+V: 12 × 2 × 512 × 34/32 | 13,056 | 1.593750 |
| Indexer Q8 K+V: 12 × (128+256) × 34/32 | 4,896 | 0.597656 |
| MTP F16 K+V: 2 × 512 × 2 | 2,048 | 0.250000 |
| Pooled F32 indexer: 12 × 128 × 4 / 4 | 1,536 | 0.187512 including two extra rows/layer |
| Total | **21,536** | **2.628918** |

The QSA ratio only compresses pooled scoring rows. It does not divide main K/V allocation by four. The generic indexer KV allocator allocates V storage even though the graph only writes indexer K. Draft KV stays default F16: target `-ctk/-ctv` does not change the draft defaults.

Source line references under `vendor/llama.cpp-rocmfpx`:

- `src/models/qwen4exp.cpp:98-104`: recurrent/full-attention layer selection.
- `src/llama-memory-hybrid-idx.cpp:53-62`: indexer overrides K dimension and head count, retains V dimension, passes target cache types. `:80-107`: pooled rows `ctx/4+2`, F32 storage.
- `src/llama-kv-cache.cpp:210-235`: both K and V allocation at full `kv_size` for non-MLA.
- `common/speculative.cpp:2607-2608` plus `common/common.h:341-342`: draft F16 cache defaults; `common/speculative.cpp:2658-2659`: draft takes target context length.
- `src/llama-model.cpp:2565-2566`: MTP-only dense layer filter. `:1481-1483`: token embeddings remain CPU as part of existing placement, not new offload.
- `common/common.h:397-402`, `src/llama-memory-recurrent.cpp:101-111`: MTP3 keeps four recurrent GPU rows. Their derived fixed size is 0.439728 GiB including the PLE convolution state.

Tensor-directory storage estimates for fixed GPU weights: main 86.568054 GiB, MTP 1.780782 GiB, projector 0.841919 GiB. Each main/draft input embedding is 521,472,000 bytes on CPU and is excluded. These are source-placement/storage estimates, not per-buffer allocator measurements. Subtracting those, persistent KV and recurrent rows from process VRAM leaves **1.390711 GiB** of compute/driver/allocation residual. Startup logs at verbosity 3 do not expose individual compute buffer sizes, so this residual cannot honestly be described as a measured compute-only buffer.

## Candidate budget

K means 1,024 tokens; limits include output tokens. Resident lower bound keeps the currently observed compute/other allocations fixed and adds only mandatory context KV growth. Planning peak also scales the unresolved 1.390711 GiB residual linearly with context and adds a 64 MiB allocation margin. The latter is a conservative planning scenario, not a measured peak or a proven strict allocator upper bound.

| Context | Persistent KV GiB | Resident GPU lower bound GiB | Planning GPU peak GiB | Planning GPU free GiB | Four CPU checkpoints + one speculative snapshot GiB |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 131,072 | 2.628918 | 94.130032 | 94.192532 | 1.807468 | 1.814326 |
| 163,840 | 3.286144 | 94.787258 | 95.197436 | 0.802564 | 2.130488 |
| 196,608 | 3.943371 | 95.444485 | 96.202340 | -0.202340 | 2.446650 |
| 262,144 | 5.257824 | 96.758938 | 98.212149 | -2.212149 | 3.078975 |

192K already reaches 95.444485 GiB in the resident-only lower bound, leaving 0.555515 GiB, below the mandatory 1 GiB. 256K reaches 96.758938 GiB before compute growth. Both are ruled out under the unchanged configuration.

160K reaches 94.787258 GiB before compute growth. To preserve 1 GiB it permits at most **0.212742 GiB (217.848 MiB)** of all additional compute, allocator and other growth. After the 64 MiB planning margin, the compute allowance is only 153.848 MiB. The planning scenario adds 356.022 MiB compute/driver residual and gives 95.197436 GiB total, only 0.802564 GiB free. Therefore 160K is **not budget-approved**, rather than “tested and failed.” QSA graph scores and masks genuinely scale with context at fixed ubatch: `src/models/qwen4exp.cpp:943-974` and `:1022-1048`. Assuming zero compute growth would be unjustified.

## CPU checkpoint and RAM constraints

Checkpoint buffers are CPU vectors (`common/common.cpp:2292-2324`). Four retained prompt checkpoints serialize partial target/draft state (`tools/server/server-context.cpp:2319-2341`); an additional speculative rollback snapshot exists (`:3059`). Existing journal metadata for this exact PID gives, for example, 38,608 tokens / 188.861 MiB and 67,098 tokens / 245.158 MiB. Their fitted serialized size is approximately **112.574 MiB + 2,072 bytes/token per checkpoint**, consistent with recurrent state plus F16 draft history. Main attention/indexer full history is excluded by PARTIAL_ONLY (`src/llama-memory-hybrid-idx.cpp:285-293`). The fit is a planning estimate from observed lower-context snapshots, not a measurement of a 192K snapshot.

For a conservative RAM envelope, assign five full candidate-sized snapshots without crediting reclamation of any checkpoints already included in MemAvailable, plus 256 MiB for metadata/staging growth. This double-counts existing checkpoint occupancy intentionally because current retained-vector capacity is not directly exposed. Estimated available RAM becomes roughly 4.085 GiB at 160K, 3.769 GiB at 192K and 3.136 GiB at 256K. 160K narrowly meets that CPU-only envelope; it still fails GPU proof. 192K/256K do not meet the 4 GiB system-available condition under this conservative envelope. Startup/fragmentation/high-water behavior remains unverified; swap cannot fill the gap.

## Result and limitations

No enlarged context is authorized by this budget. Retain 128K, Q8 target KV, F16 draft KV, MTP3, checkpoint4, np1, b2048/ub512. Do not reduce b/ub or modify any production setting as part of this report. 192K/256K are excluded independently of the uncertain compute residual; 160K may or may not allocate, but the required reserve has not been proven. No larger-context load, restart, GPU allocation probe, inference, previous-request replay or CPU offload change was performed.

A future budget reassessment would need exact buffer-allocation accounting for the same graph/build or an explicitly authorized configuration change; current source/static estimates are not a substitute for the missing peak evidence. Existing 128K operation is observed, not reaccepted against a new worst-case startup or multimodal workload in this round.

Evidence: `/home/ai/github/1Panel/experiments/local-pool-20260908/amd-context-budget-metadata.json`. No prompt content or credentials are included.

Runtime hashes:

- llama-server: `1d8029a4c3e595f720cd32226698c07d4b0b8bd80935c403d3081d75789e7696`
- libllama.so: `04a59b4f20eb62649cfbb1e405848b8d7d504e4d6c13532575cdc37de61b3902`
