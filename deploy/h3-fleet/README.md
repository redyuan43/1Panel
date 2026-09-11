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
Three simultaneous quality jobs remain prohibited. Production long-duration
jobs remain serial; explicit owned validation leases can test long preview concurrency.

## Measured concurrency

- Three concurrent 6-step jobs at 864x480, 124 frames: passed.
- Two concurrent 14-step jobs at 1344x768, 124 frames: passed.
- Jobs over 124 frames run exclusively on `fast`, with one active execution
  across the entire fleet. They cannot overlap short jobs or other long jobs.
- Short preview and short quality jobs do not overlap across profiles. The
  configured resource guards can reduce concurrency below these historical
  maxima. Missing resource telemetry keeps new work queued.
- A successful 15-second preview on `fast` does not validate 15-second quality
  or multiple long jobs. See [capacity evidence](docs/capacity-evidence.md).

## Router contract

The private contract is authenticated with `H3_ROUTER_KEY` and exposes:

- `GET /api/router/options`
- `POST /api/router/executions`
- `GET /api/router/executions/{execution_id}`
- `POST /api/router/executions/{execution_id}/cancel`
- `GET /api/router/executions/{execution_id}/output`

Preview workflows use 6 steps and quality workflows use 14 steps. Only workloads
within the 124-frame short-job boundary can use the corresponding three/two-lane
capacity. `execution_profiles.max_parallel_frame_count` and `long_max_parallel`
make that boundary explicit without changing the version-2 execution contract.

`GET /api/router/capacity` reports the effective policy, current resource gates,
active execution IDs/statuses, and per-lane queue counts. It requires Router
authentication and never returns customer prompts or uploaded assets.

Admission uses a SQLite `BEGIN IMMEDIATE` transaction to reserve a lane and
check global workload/resource capacity together. The service also acquires an
OS lock beside its database, rejecting a second process that uses that database.
Jobs whose submission outcome is unknown retain their lane reservation and
inputs. Queue/history metadata is reconciled using a persisted fleet operation
ID; no automatic resubmission or age-based release occurs after a possible POST.
If upstream evidence was deleted, operator reconciliation is required.

The reviewed policy lives in `config/capacity.json`; `H3_CAPACITY_POLICY` can
select a restrictive policy. Increasing long-job concurrency is rejected by
code. Resource budgets are conservative admission settings, not measured peak
usage or a performance guarantee.

Inactive residual swap above 1 GiB can recover only after a 60-second quiet
idle observation, then admits one known-shape execution on the fast GPU lane.
This includes preview or quality under their existing resource budgets. The
baseline stays fixed through busy/unknown outcomes and is retained until the
next quiet window; new swap above 1 GiB or total swap at 8 GiB blocks further
admission. Active swap I/O/PSI/event changes invalidate readiness. Inspect
`resources.swap_recovery` from the capacity endpoint for readiness and evidence.
The standalone stress validator still uses the original absolute 1 GiB stop.

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

The load-validation clients use the same private Router credential. The key
file must be a regular `0600` file and is never printed or copied into the
report:

```bash
python3 scripts/validate_capacity.py \
  --fleet-url http://100.96.79.21:8789 \
  --key-file ~/.config/ai-router-media/h3-key \
  --output-dir ~/.local/state/h3-validation \
  --profile preview --duration 5 --max-parallel 3 --timeout 7200
```

Run the capacity validator on Ivan. The command above only inspects current
queues, policy and host resources. Add `--execute` only in the coordinated idle
window. It obtains a durable validation lease, rejects existing or untracked
production work, and runs 1→2→3 with two batches at each level. It confirms
simultaneous upstream running queues, validates downloaded audio/video, and
records resource samples, media hashes and owned operation IDs in a private
persistent evidence directory. A threshold, timeout, signal, OOM, Xid or worker
restart stops further submissions and cancels only the recorded IDs. Unknown
outcomes keep their reservations and require reconciliation. It never promotes
capacity automatically. The old `validate_dual.py` remains for historical
report reproduction; use `validate_capacity.py` for new capacity evidence.

When no separate key file is installed, the same local `ivan` user can replace
`--key-file ...` with `--key-from-service`. It reads the running fleet process's
private credential in memory, without printing or copying it to another file.

An explicitly coordinated long-concurrency experiment additionally uses
`--experimental-long-concurrency`, for example `--profile preview --duration 15
--max-parallel 3 --experimental-long-concurrency --execute`. The lease fixes
profile, actual frame count and maximum parallelism for that run only. Preview
experiments can use three physical lanes and quality experiments two. Resource
thresholds, media checks, monitoring and owned-ID cleanup remain enforced. No
production capacity setting is raised, and expiry stops further dispatch while
retaining active reservations until reconciliation.

An expensive staircase can pause between levels. To resume, pass
`--previous-report /persistent/path/report.json` with the next desired
`--max-parallel`. The validator requires the same workload and GPU topology,
two fully passed batches at every prior level, confirmed lease release and
unchanged output hashes. It then starts at the next level with a fresh idle
check and validation lease. This does not reuse a mere submitted/timeout report.

The authenticated validation lease is created/renewed with
`POST /api/router/validation-lease` (`owner: h3val_<16 hex>`, `ttl_seconds: 120`).
An optional immutable `experiment` object has `profile`, `frame_count` and
`max_parallel`; omitting it uses the production capacity policy.
During this window only operation IDs prefixed with `<owner>_` are admitted.
`DELETE` with the same owner releases the window only after all tracked work
has a confirmed terminal state. Expiry allows normal work only if no jobs are
still active; an unknown test outcome cannot silently open production admission.

For a scheduler-only update, `scripts/deploy.py --scheduler-only --execute`
uses the existing idle/drain, backup and rollback gates and restarts only
`h3-fleet.service`. Existing ComfyUI GPU workers remain running. Deployment
still needs its coordinated maintenance window.

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
