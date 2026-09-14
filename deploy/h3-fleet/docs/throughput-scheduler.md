# Four-recipe throughput scheduler

## Delivery boundary

This is an opt-in Fleet extension, not an automatic production promotion. The shipped
`config/recipe-scheduling.json` has `enabled=false`, no backends, and no qualified
combinations. Existing production capacity and systemd memory limits are unchanged.
Only A4, A4_C0, A4_C1 and B8 enter the new catalog. New portrait 15-second native-audio
T2V preview requests default to A4. Other stages retain the previous policy.
Old projects and media are not rewritten. Retired recipes require explicit reselection.

## Admission and scheduling

All values below are integer bytes. Memory collection brackets `memory.stat` with
two `memory.current` reads and uses their maximum; observations taking over one
second fail closed. All stat fields, host availability, swap counters, PSI and events
remain in the admission/reconciliation records.

```
reclaimable = max(0, min(file, inactive_file) - dirty - writeback - unevictable)
effective_reclaimable = floor(reclaimable * reclaim_factor)
effective_working_set = memory.current - effective_reclaimable
running_remaining = sum(max(0, expected_delta - observed_delta)
                        + max(0, observed_worker_peak - current_worker_memory))
candidate_budget = max(static_floor, previous_budget_floors,
                       measured_peak_delta + max(2 GiB, ceil(10% * measured_peak_delta)))
projected = effective_working_set + running_remaining + candidate_budget + global_margin
```

Default cache credit is 0.5, global margin at least 2 GiB, static candidate floor at
least 18 GiB. Matched source-verified peak history is reused. Observed job peaks also
raise persistent floors, including across runtime changes; missing history cannot
reduce a floor. Counter resets, missing measurements, increased swap-out or swap,
unstable PSI and stale samples block admission. Dirty/writeback/unevictable memory
receives no credit. Reclaim counters are cumulative aggregate activity, not causal
bytes freed by this task. No automatic calibration or cache reclaim operation exists.

The effective forecast and the independent **raw 72 GiB runtime guard** both remain.
Other limits are host available >=16 GiB, root free >=25 GiB, offload free after
reservations >=40 GiB. Generic residual-swap recovery remains exclusive/fast-only.
An explicitly isolated `resource_profile=residual_zram` instead reuses the existing
experimental `ProgressiveResourcePolicy` without changing production: complete zram
inventory, unused disk swap, stable 60s baseline, <=8 GiB absolute logical swap,
<=1 GiB growth, PSI and production-PID checks are all required. It is forbidden in
non-experimental policy. This is not a flag that accepts arbitrary residual swap.

At most the oldest 32 jobs, with three per recipe, enter assignment enumeration.
Rank: protected >=15-minute wait, scarce compatible GPUs, safe starts, estimated
finish, warm affinity, FIFO ties. No recipe substitution occurs. Each live dispatch
starts only one job, then waits for another continuously stable >=60s resource
window **and owned non-cached sampler progress** before another lane joins.
Unknown submission outcomes retain their GPU and memory reservation. Recovery uses
the persisted backend URL/PID/start ticks, not a newly reconfigured lane endpoint.

The Fleet write transaction re-reads active jobs and resource decisions before
reserving physical GPU ownership. Port ownership is checked using the pinned PID's
socket inodes, not merely a responding health endpoint. A changing PID, command line,
GPU environment, cgroup or weight file closes admission. Model files and relevant
recipe metadata are hashed before a backend is enabled.

## Runtime configuration

`H3_RECIPE_POLICY` points to a persistent operator-reviewed JSON file. Each backend
contains `id`, `lane_id`, `gpu_uuid`, loopback `url`, `pid`, `start_ticks`,
`cmdline_sha256`, protected child `cgroup_path`, `runtime_files` (absolute path/SHA256),
`runtime_version` (digest of the runtime file manifest), and `weight_files` (catalog
filename, absolute path and SHA256 for every weight and metadata file).

`recipes` maps recipe ID to frozen `recipe_version`, full `vram_budget_bytes`, optional
source-verified `peak_history`, and qualification evidence. A `single_completed`
qualification reopens pinned workflow, actual history, full-decode media probe and
video file; catalog digest, hardware, runtime and non-cached sampler must agree.
An experimental `trial` qualification is allowed only for explicit `validation_cases`.
It is not a single-card success and never promotes production.

Mixing requires `qualified_combinations` with exact recipe/GPU/runtime members and
source-pinned task evidence containing real overlapping sampler intervals. Isolated
`validation_combinations` may explicitly authorize a trial instead, and remain labeled
unvalidated. Single-card qualification never automatically grants mixed concurrency.

An isolated Fleet also needs `production_fence_url`. It verifies an owned, unexpired
lease and empty queues on production Fleet before dispatch and again before reserve.
Ivan's existing service is bound to its Tailscale address, not localhost: use
`http://ivan-ms-7b17.taild500c8.ts.net:8789`. No production restart is involved.

## Lifecycle and failure

Healthy jobs are not cancelled because a forecast rejects another lane. Actual hard
limit/OOM/Xid violations are audited first, then only owned tasks on identity-verified
backends may be cancelled. Unconfirmed cancellation remains reserved/reconciling.
A sticky `recipe_hard_stop` requires operator reconciliation before new admission.
CUDA OOM quarantines the exact recipe/GPU/runtime/command-line profile.

Finished jobs store prediction/actual reconciliation, worker peak, lowest host
availability, PSI peaks, swap and reclaim counters, CUDA failures and kernel alerts.
`recipe_audit` records admission, waits, task completion and separate lifecycle events.
The default background interval is five seconds; no page polling is needed to dispatch.

Warm models are reused only under the same verified runtime and recipe. A measured
full VRAM budget may credit that idle worker's own GPU allocation, never a foreign
process. Otherwise the independent lifecycle path unloads and verifies <=1 GiB device
residency before switching. Idle models without same-recipe queued work may unload
after 300 seconds. No production restart, drop_caches, swapoff, memory.reclaim, cgroup
limit increase, attention change or model/quality substitution is performed.

## Validation and rollback

`scripts/validate_recipe_batch.py` defaults to a CPU-only dry run. Explicit execution
obtains owned production/experimental leases, submits through Fleet, and saves full
15-second artifacts, decoder checks and resource/sampler reports. Batches are:
`single` (A4_C0), `b8` (B8+A4_C0+A4), `people` (A4_C1+A4+A4_C0).
Serial completion reports `peak_parallel=1, parallel_validated=false`; synthetic
replay performance is not a real GPU speedup. All quality decisions remain human.

The latest user acceptance scope is a bounded startup smoke, not full-video generation.
Pass `--smoke-seconds 60` explicitly: the original 15-second workflow still loads and
performs real non-cached sampling, but its owned task is cancelled after a continuously
observed 60-second sampler window with current telemetry and no observed OOM/Xid.
This writes `startup_smoke_passed`, `full_video_validated=false`, and
`parallel_validated=false`; it never creates a single-completed qualification receipt.
Decoder/audio and full-run peaks remain unverified. Cleanup reconciles only owned
tasks, checks backend process identity and empty queues, unloads idle experimental
models, and confirms VRAM release before releasing the production validation fence.
Unknown submissions or failed cleanup retain the fence for reconciliation.

The operator setup helper `scripts/prepare_recipe_smoke.py` creates only separate
experimental units, private environment files, pinned source/weight manifests and a
separate database. A reused weight manifest is not a verification bypass: Fleet always
performs its full hash verification before accepting work. It does not deploy Studio,
restart production, raise memory limits or submit inference by itself.

Do not enable the mixed trials before 3060 adaptation succeeds. A failed adaptation
keeps that exact profile closed. Production installation still requires the explicit
maintenance/drain/rollback window from the approved plan. Rollback disables new
recipe admission; it does not change already-submitted identities or stored artifacts.
