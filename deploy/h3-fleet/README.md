# H3 Fleet

Private ComfyUI-compatible scheduler for the Ivan three-GPU MiniMax H3 host.

The service exposes the ComfyUI endpoints used by H3 Video Studio and assigns
each prompt to one single-GPU ComfyUI lane. The third lane is installed as a
preview-only short-job lane. It was enabled only after the measured memory gate
and a real three-job validation passed.

Public Router APIs remain unchanged. AI Router calls this service directly
through its authenticated execution contract. Edge H3 Video Studio is not part
of the normal managed-workflow path and remains only for legacy compatibility.

All three ComfyUI workers run inside `h3-compute.slice`. The aggregate guard
uses `MemoryHigh=72G`, `MemoryMax=80G`, and `MemorySwapMax=8G`, in addition to
each lane's own limit. This preserves host headroom even if multiple workers
simultaneously enter a bad offload path.

## Ivan topology

| Lane | GPU | UUID | Port | Default |
| --- | --- | --- | --- | --- |
| `fast` | RTX 4060 Ti 16 GB | `GPU-0befdd20-6ea9-4e7e-3378-635e20f42536` | `8188` | enabled |
| `main` | RTX 3060 12 GB | `GPU-08c21842-c266-7f7d-6e5d-d494d4c20c4f` | `8189` | enabled |
| `preview` | RTX 3060 12 GB | `GPU-b9ca94d5-6180-2d81-bb33-5ad04722f492` | `8190` | short jobs only |

ComfyUI lanes bind to loopback. The fleet scheduler binds to the Ivan
Tailscale address at `100.96.79.21:8789`.

The third lane is preview-only. The measured two-lane peak left more than
68 GiB `MemAvailable` with less than 1 MiB swap in use, and the three-lane
6-step validation completed without SSH loss, OOM, Xid, or service restart.
Three simultaneous quality or long-duration jobs remain prohibited.

## Measured concurrency

- Three concurrent 6-step jobs at 864x480, 124 frames: passed.
- Two concurrent 14-step jobs at 1344x768, 124 frames: passed.
- The `quality` profile excludes the `preview` lane, so long jobs remain capped
  at two concurrent lanes.

## Router contract

The private contract is authenticated with `H3_ROUTER_KEY` and exposes:

- `GET /api/router/options`
- `POST /api/router/executions`
- `GET /api/router/executions/{execution_id}`
- `POST /api/router/executions/{execution_id}/cancel`
- `GET /api/router/executions/{execution_id}/output`

Preview workflows use 6 steps and can occupy all three lanes. Quality workflows
use 14 steps and can occupy only `fast` and `main`.

## Verified artifacts

- Plugin commit: `5ff46c253192e9d8cae185280fd34f4b4add063b`
- Turbo source SHA-256:
  `5f3a626cd72c93a8b9318d6760c510bc5092d2ab13aaba1f932c5bab07a416d3`
- Converted Turbo LoRA SHA-256:
  `b07ab477437c6a525dfdaf11107722aad609975ac172f3b577a7a87b228ff7b3`
- Full FL2VA INT8 ConvRot SHA-256:
  `7ad4c73e6e378b822ffd1629f27f632d3787d95f5e468e3af958f98c58df96a5`

Large model files live under `/media/ivan/55FF-1534/h3-models` and are linked
into `/mnt/ivan-ext4-offload/ComfyUI/models`. Outputs, temporary files,
databases, and evidence stay on the ext4 offload image.

## Operations

```bash
sudo systemctl status \
  comfyui-h3@fast.service \
  comfyui-h3@main.service \
  comfyui-h3@preview.service \
  h3-fleet.service

curl http://100.96.79.21:8789/api/health
```

`gpustack-worker` uses Docker and must remain stopped with restart policy
`no`. `ollama.service` must remain disabled while Ivan is dedicated to H3.

To restore the previous GPU services:

```bash
sudo docker update --restart=unless-stopped gpustack-worker
sudo docker start gpustack-worker
sudo systemctl enable --now ollama
```

## Router integration

The AI media daemon uses:

```ini
AI_ROUTER_H3_EXECUTOR_URL=http://100.96.79.21:8789
```

The matching `AI_ROUTER_H3_KEY` on AI and `H3_ROUTER_KEY` on Ivan stay in
private `0600` environment files and are never committed.
