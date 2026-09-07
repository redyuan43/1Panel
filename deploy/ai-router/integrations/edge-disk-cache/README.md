# Edge Qwen3.8 Disk Prefix Cache

This deployment keeps the Edge model, 500K context, MTP3, APC, and Qwen3.8
runtime patches. It adds only two safety fixes to the vLLM native disk offload
path:

1. exclude the non-prefix-cacheable QSA `CircularBufferSpec` group;
2. order both GPU-to-disk stores and disk-to-GPU loads after the active compute
   stream.

The first candidate deliberately uses scheduler-block-aligned hits. It does not
include the larger, still-open fine-grained hybrid-prefix patch series.

## Build

Run on Edge from this directory:

```bash
sudo docker build \
  --build-arg BASE_IMAGE=qwen38-flash-dgx:e655b7d \
  --tag qwen38-flash-dgx:e655b7d-diskcache-v1 \
  .
```

The base image must already exist locally. The build fails if its pinned vLLM
sources no longer match the reviewed patch anchors.

## Runtime Configuration

`serve_flash_disk_cache.sh` keeps the reviewed production launch arguments and
adds a dedicated cache mount plus this vLLM configuration:

```json
{
    "kv_connector": "SimpleCPUOffloadConnector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {
        "cpu_bytes_to_use": 1073741824,
        "lazy_offload": true,
        "kv_offload_backend": "disk",
        "disk_path": "/kv-cache/qwen38-prefix",
        "disk_capacity_bytes": 34359738368,
        "disk_buffer_slots": 2,
        "use_page_cache": false
    }
}
```

Use a bind mount such as:

```text
/home/admin/.cache/qwen38-disk-kv:/kv-cache
```

The initial capacity is 32 GiB. Lazy offload copies only hashed blocks near GPU
eviction; it avoids the positional eager-store path used by Mamba `align`.
`use_page_cache=false` keeps the file on the O_DIRECT path so it does not
consume the GB10 unified host/device memory pool.
Copy `edge-disk-cache.env.example` to a persistent, host-local configuration
file and select it with `QWEN38_FLASH_CONFIG`.

For an isolated candidate on port 18301:

```bash
QWEN38_FLASH_CONFIG=/path/to/candidate.env \
  ./serve_flash_disk_cache.sh
```

Production uses `edge-production.env`, container `qwen38-flash-next`, and port
`18300`. The user systemd unit is enabled so the service returns after a host
restart, but the disk cache itself is currently guaranteed only within one vLLM
process lifetime.

## Scope

- Same-process GPU eviction and disk reload only.
- No LMCache.
- No cross-restart persistence claim.
- No Router or multimodal change.
- Router continues to manage the existing `edge-qwen38-flash` endpoint.

## Acceptance

Use a controlled Edge maintenance window and a candidate port. The benchmark
stores both JSON and Markdown evidence and refuses port 18300 unless
`--allow-production` is explicit:

```bash
sudo -n python3 benchmark_edge_disk_cache.py \
  --base-url http://100.101.54.115:18301 \
  --container qwen38-flash-next-diskcache \
  --disk-path /home/admin/.cache/qwen38-disk-kv \
  --output-dir /home/admin/.local/state/edge-disk-cache/acceptance
```

The acceptance sequence is:

1. 20K cold request, GPU eviction, then exact disk-backed repeat.
2. 40K cold request, GPU eviction, then exact disk-backed repeat.
3. 50K cold request, GPU eviction, then exact disk-backed repeat.
4. Every target and filler request uses a unique marker with no response
   contamination.
5. Confirm disk read/write activity and an external cached-token increase.
6. Require at least 50% TTFT reduction for every repeated target.
7. Confirm no garbling, mixed scripts, repetition collapse, or tool corruption.

The first full candidate run passed all three targets:

| Target | Cold TTFT | Disk reload TTFT | External hit | Reduction |
| ---: | ---: | ---: | ---: | ---: |
| 20K | 13.21s | 2.01s | 17,600 tokens | 84.76% |
| 40K | 26.83s | 2.67s | 36,800 tokens | 90.05% |
| 50K | 32.86s | 1.95s | 48,000 tokens | 94.07% |

Any semantic failure restores the unmodified `qwen38-flash-dgx:e655b7d`
runtime. Cross-restart persistence is a separate second phase after this gate.
