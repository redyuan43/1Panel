# Qwen3.8 V100 TP2 LMCache

## Scope

This deployment keeps the existing `ai-qwen38-27b` endpoint and its tested
runtime:

- GPU4/GPU5, TP2 and PP1
- 196608-token context, four concurrent sequences
- FP8 E5M2 KV cache
- MTP with two speculative tokens
- vLLM Automatic Prefix Caching
- NCCL P2P with vLLM custom AllReduce disabled

LMCache adds an optional CPU DRAM cache below GPU APC. It never enables disk,
object-store, remote, P40, TP1, or AGX resources.

The V100/PG500 pair passes a standalone NCCL P2P `all_reduce`, but vLLM's
custom IPC AllReduce can deadlock during communicator initialization after an
LMCache lifecycle change. Keep `DISABLE_CUSTOM_ALL_REDUCE=1`; this disables
only vLLM's custom transport while retaining NCCL P2P.

If a previous CUDA-IPC launch has already left peer access unhealthy, set
`NCCL_P2P_DISABLE=1` until the next host reboot. This uses NCCL host staging
and avoids a disruptive GPU/Xorg reset. After reboot, run the standalone check
with P2P enabled before removing the setting.

## Fixed Runtime

- LMCache source: `v0.5.4`
- Source commit: `3e11b8ed191631e6f098b8038235823f1a410b24`
- Build: CUDA 12.8, `TORCH_CUDA_ARCH_LIST=7.0`
- Isolated environment: `/home/ai/venvs/1cat-vllm-1.5.0-lmcache`
- Message endpoint: `tcp://127.0.0.1:6555`
- HTTP status and metrics: `http://127.0.0.1:18084`
- L1: 80 GiB CPU DRAM, hard cgroup limit 96 GiB
- NUMA: node 1
- Chunk size: 1600
- Hybrid layout: `--separate-object-groups`
- Transfer: CUDA IPC `lmcache_driven`, with CPU DRAM as the storage tier

The official LMCache 0.5.4 CUDA 13 wheel does not include `sm_70`. The setup
script clones the pinned source and builds the native extension with the same
CUDA 12.8 and C++ ABI as the working 1Cat vLLM environment.

LMCache 0.5.4 declares older OpenTelemetry and Prometheus upper bounds than
1Cat-vLLM 1.5.0 permits. The isolated environment deliberately keeps the
versions required by the tested vLLM runtime and installs LMCache from source
with `--no-deps`; live status, metrics, connector import, and semantic
acceptance are therefore release gates rather than relying on package metadata
alone.

## Prepare

Preparation does not restart the current TP2 service:

```bash
deploy/ai-router/scripts/setup-qwen38-v100-tp2-lmcache.sh
```

The script refuses to overwrite an existing target environment and verifies
that the built `cuda_ops` binary contains `sm_70`.

Install these user units only during the authorized deployment step:

```bash
install -m 0644 \
  deploy/ai-router/systemd/qwen38-v100-tp2-lmcache.service \
  "$HOME/.config/systemd/user/qwen38-v100-tp2-lmcache.service"
install -m 0644 \
  deploy/ai-router/systemd/qwen38-v100-tp2-vllm.service \
  "$HOME/.config/systemd/user/qwen38-v100-tp2-vllm.service"
systemctl --user daemon-reload
systemctl --user enable qwen38-v100-tp2-lmcache.service
```

The LMCache unit remains active as a lightweight dependency while
`lmcache.enabled` is false. It allocates the configured DRAM only when the
setting is enabled.

The LMCache server uses CUDA IPC only to move KV pages and keeps the cache
payload in CPU DRAM. Qwen3.8 uses hybrid KV cache groups, which LMCache 0.5.4
does not support in `engine_driven` mode; keep `lmcache_driven` and
`--separate-object-groups`.

## Control Console

The policy page writes `lmcache` into
`/opt/1panel/ai-router/settings.yaml`. The same file is read by both model
service launchers. Changing the toggle or DRAM budget therefore changes the
next real service start, not only the browser form.

The endpoint page reports:

- GPU APC query and hit tokens
- vLLM external KV transfer tokens
- LMCache lookup and hit tokens
- L1 memory usage and capacity
- LMCache health, generation, and whether the connector is active

The page shows `需要重启模型服务` until desired and observed states agree.

## Controlled Activation

Activation interrupts the current model endpoint while LMCache and vLLM are
restarted in dependency order:

```bash
systemctl --user stop qwen38-v100-tp2-vllm.service
systemctl --user restart qwen38-v100-tp2-lmcache.service
systemctl --user start qwen38-v100-tp2-vllm.service
```

`qwen38-v100-tp2-vllm.service` is bound to the LMCache unit. Its preflight
fails closed unless LMCache reports:

- healthy state
- chunk size 1600
- exact configured DRAM capacity
- zero L2 adapters

The same preflight waits until both selected GPUs have released enough memory
for `GPU_MEMORY_UTILIZATION`. This prevents a restart from racing delayed CUDA
context cleanup. If the previous vLLM container has exited but LMCache still
holds its GPU registrations, the preflight unregisters those stale contexts
through the LMCache message protocol while preserving the CPU L1 objects.

If TP2 stops at NCCL initialization, stop the model service and run the
standalone two-rank check before changing model or LMCache settings:

```bash
docker run --rm --runtime nvidia --network host --ipc host \
  --user 1000:1000 \
  -e NVIDIA_VISIBLE_DEVICES="$GPU_UUIDS" \
  -e CUDA_VISIBLE_DEVICES=0,1 \
  -e CUDA_DEVICE_ORDER=PCI_BUS_ID \
  -v /home/ai/runtimes/python-3.12.14-20260901:/home/ai/runtimes/python-3.12.14-20260901:ro \
  -v /home/ai/venvs/1cat-vllm-1.5.0:/home/ai/venvs/1cat-vllm-1.5.0:ro \
  -v "$PWD/deploy/ai-router/scripts/check-v100-tp2-nccl.py:/check-v100-tp2-nccl.py:ro" \
  docker.io/nvidia/cuda@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66 \
  timeout -s KILL 25s \
  /home/ai/venvs/1cat-vllm-1.5.0/bin/python -m torch.distributed.run \
  --standalone --nproc-per-node=2 /check-v100-tp2-nccl.py
```

## Acceptance

Prime six unique 2K to 50K prefixes before restarting vLLM:

```bash
deploy/ai-router/scripts/validate-qwen38-v100-tp2-lmcache.py \
  --phase prime \
  --api-key-file "$HOME/.config/1cat-vllm/api-key" \
  --output "$HOME/.local/state/qwen38-v100-tp2-lmcache"
```

Keep LMCache running and restart only vLLM in the controlled window:

```bash
systemctl --user restart qwen38-v100-tp2-vllm.service
```

Then validate restored prefixes:

```bash
deploy/ai-router/scripts/validate-qwen38-v100-tp2-lmcache.py \
  --phase resume \
  --api-key-file "$HOME/.config/1cat-vllm/api-key" \
  --output "$HOME/.local/state/qwen38-v100-tp2-lmcache"
```

Add `--router-key-file`, `--require-workbuddy`, `--training-db`, and
`--training-key` to make the same run require WorkBuddy-style complete
history, `继续`, a closed tool transaction, and the encrypted database copy.

The run fails on any foreign marker, unhealthy cache, missing external hit,
40K to 50K TTFT reduction below 50 percent, malformed history, replacement
characters, or pathological identical-token runs.

## Rollback

Preview the rollback:

```bash
deploy/ai-router/scripts/rollback-qwen38-v100-tp2-lmcache.sh
```

During an authorized restart window:

```bash
deploy/ai-router/scripts/rollback-qwen38-v100-tp2-lmcache.sh --apply
```

This creates a timestamped settings backup, sets `lmcache.enabled=false`, and
restarts the same TP2/MTP2/APC service with the original vLLM environment.
