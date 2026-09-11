# H3 capacity evidence and operational boundary

The following raw reports were read on Ivan on 2026-09-09. Paths are relative
to `/mnt/ivan-ext4-offload/h3-deploy/evidence/20260907-233628`. That historical
report inspection did not run generation; fresh validation is recorded below.

| Report | Workload | Results and wall time | SHA-256 of report.json |
| --- | --- | --- | --- |
| `gate3-triple-6step` | preview, 864×480, 124 frames, 6 steps, three lanes | all three outputs passed media checks; 288.08 s | `e44d44c0d7a5b9913ae6cd3a31ed0b24d219577fa16980e9520672072463acde` |
| `custom-subway-triple-5s-turbo6` | preview, 480×864, 124 frames, three lanes | all three outputs passed media checks; 278.40 s | `a784a8ab03c1a687687293b56c082992a087f3ea669925aec5200287fc10ed11` |
| `gate3-dual-14step-768-124` | quality, 1344×768, 124 frames, 14 steps, fast/main | both outputs passed media checks; 1650.18 s | `ad6e9467bf3662546fabd2611ff42bf1e030ed07733b7e9907bf7846c5c99736` |
| `gate3-dual-14step-768-243` | quality, 1344×768, 243 frames, two lanes | report has no output or media-validation result; 3442.44 s observation is not a pass | `418af7c9d73836f42364a98300a42f95ac172cea5e14e0eeba01c4fc5596f055` |
| `custom-subway-winner-15s-turbo6` | preview, 480×864, 362 frames, fast only | output passed media checks; 802.96 s | `990b2981baf211378923e9f35c239eda1e3c529663ca1c4aa226fec4b2396ac5` |

The three-lane 124-frame landscape run recorded at least 71,614,226,432 bytes
of host available RAM, peak swap of 417,792 bytes, and minimum offload free
space of 48,082,071,552 bytes. Those historical measurements do not guarantee
today's capacity. Their telemetry predates the new cgroup-event and worker
restart monitoring and must not be described as a new validation-staircase pass.

## New 2026-09-09 controlled short-preview validation

Run `h3val_7f034cc4963242ce` completed on Ivan using the authenticated managed
execution contract and an owned validation lease. All 12 outputs passed the
124-frame, 24-fps, 864×480 and native-audio media checks.

| Actual simultaneous running lanes | Batch 1 | Batch 2 |
| --- | --- | --- |
| 1 | 198.24 s | 180.04 s |
| 2 | 264.91 s | 227.50 s |
| 3 | 240.88 s | 228.68 s |

The complete staircase took 1340.42 seconds. Minimum available RAM was
65.73 GiB; aggregate cgroup peak 23.33 GiB; peak host swap 1,011,712 bytes;
minimum root/offload free space 27.56/44.71 GiB; highest GPU temperature 85°C.
No OOM/Xid evidence or GPU worker restart occurred. The lease was released
after all owned tasks reached confirmed terminal states. This run does not
promote long-task or mixed-profile capacity.

Raw report:
`/mnt/ivan-ext4-offload/h3-deploy/evidence/20260909-capacity/h3val_7f034cc4963242ce/report.json`.
SHA-256: `e84dc8ac47d36b497cad7bfac989c6504c19b83f349682b4bee4da96b8b11256`.
Sibling `metrics.jsonl` and `deployment-provenance.json` preserve telemetry and
the deployed code/configuration digests. The 15-second staircase is a separate
experiment; it is not covered by this result.

## New 2026-09-09 long-preview attempt and residual-swap recovery

Run `h3val_7c46055f5a174d9d` produced two validated fast-lane 362-frame,
864×480, 24-fps, native-audio clips in 761.55 and 734.45 seconds. Its first
two-lane batch was stopped after about 234 seconds by the original absolute
1 GiB swap guard. Neither dual-lane task produced a validated output. Both
owned executions were confirmed cancelled and the validation lease released.
This is a failed parallel-capacity attempt, not evidence for opening two lanes.
No worker restart, OOM/Xid, or cgroup high/max/OOM event was observed.

Raw report (preserved unchanged):
`/mnt/ivan-ext4-offload/h3-deploy/evidence/20260909-capacity/h3val_7c46055f5a174d9d/report.json`.
SHA-256: `9110081f7c79df8bce7c564dbb9b295de0b90fee05279d9766d8ce0f8cb0b81c`.
The two single-lane batches changed host swap by +399,597,568 and -198,610,944
bytes; the attempted dual batch added 896,192,512 bytes to an existing
201,998,336-byte balance. At the stop there was 67.33 GiB available RAM and
24.32 GiB aggregate cgroup memory. Subsequent idle samples at least 193 seconds
apart had identical pswpin/pswpout and memory-PSI totals, zero pressure averages,
and about 1.023 GiB residual host swap. These observations distinguish inactive
swapped pages from ongoing pressure; they do not validate dual-lane operation.
Sibling `swap-analysis-initial.json` and `swap-recovery-samples.jsonl` retain
the phase deltas, raw counters, queue states and headroom observations.

The reviewed recovery rule observes at least 60 seconds with empty raw queues,
no busy/unknown fleet reservations, unchanged swap I/O/PSI/event counters and
zero current memory pressure. It then allows only one known-shape fast-lane
execution, retaining all RAM/cgroup/disk budgets. The idle baseline remains
frozen during that round; more than 1 GiB new swap blocks subsequent admission,
and 8 GiB total host or cgroup swap is a hard admission stop. Activity invalidates
readiness immediately; a subsequent round requires another complete quiet
window. A fleet restart starts recovery closed. Recovery never cancels running
production jobs or frees swapped pages; execution owners retain cancellation
control. Long parallelism remains one. The standalone stress validator retains
its original absolute guard, so no failed report is reclassified by this change.

## Declared policy versus validated capability

- Preview ≤124 frames: historical maximum three lanes; current admission also
  requires matching 864×480/480×864 and six steps, plus current resource headroom.
- Quality ≤124 frames: historical maximum two lanes at 1344×768 and 14 steps.
  The portrait orientation uses the same pixel budget as a conservative policy
  choice; its exact two-lane configuration is not established by the listed report.
- >124 frames: a conservative exclusive policy, one fast-lane job at a time.
  The listed completed evidence covers 362-frame preview only. Quality 362-frame
  output and any 15-second parallel configuration remain unvalidated.
- Unknown legacy workflow shape: exclusive conservative admission. An H3 graph
  with missing/ambiguous or inconsistent dimensions is rejected. Actual graph
  frame count overrides duration labels; contradictory resource metadata fails.
- New short/long or preview/quality mixtures require separate evidence. They
  are not automatically enabled by a successful isolated workload.

The runtime budgets (8 GiB per short preview, 12 GiB per short quality, 12 GiB
per known long preview, 24 GiB per long quality/unknown legacy shape, and 1 GiB
output reservation per job) are conservative capacity reservations, not measured peak memory.
They are added to current aggregate cgroup memory and checked against 72 GiB;
host free RAM must retain 16 GiB. Parallel admission requires host/cgroup swap
below 1 GiB; higher inactive residual swap uses only the guarded single-lane
recovery rule above, with a 1 GiB increment limit and 8 GiB total hard stop;
root/offload free-space floors are 25/40 GiB, with additional output reservation.

## New validation protocol

1. Run on Ivan using its authenticated fleet execution boundary and private
   key file. Inspect all managed and raw upstream queues and verify local GPU
   UUIDs match the target fleet before any submission.
2. Claim an idle validation lease. Persist test operation IDs before sending
   them. The lease prevents production submissions entering the test window.
3. Within reviewed capacity, run 1→2→3 to the proposed maximum, two batches at
   each level. Use maximum intended profile/frame count/resolution. Observe
   simultaneous upstream running queues; admission or serial completion alone
   does not establish parallel capacity.
4. Sample RAM, cgroup memory/swap/events, disks, GPU utilization/temperature,
   OOM/Xid logs, process identity and restart count. Stop on resource thresholds,
   a worker restart, a fatal log, total wall-clock deadline or termination signal.
5. Validate every downloaded video's frames, duration, resolution, frame rate
   and audio; hash artifacts and record execution/lane/GPU IDs and elapsed time.
6. Cancel only recorded owned IDs on failure. A lost submit/cancel response
   requires operation-ID reconciliation; do not resubmit or globally interrupt.
7. Release the lease only after confirmed terminal work. Keep incomplete
   evidence as failed/needs-reconciliation and perform no automatic promotion.

A coordinated 15-second test can use the explicit
`--experimental-long-concurrency` flag. Its durable lease fixes profile,
frame count and maximum parallelism and accepts only that run's owned IDs.
Preview experiments are bounded to three physical lanes; quality to two.
Resource thresholds are unchanged. An expired lease stops further queued
dispatch, while unknown active outcomes keep the window closed to production.
The override disappears when the lease is released and never changes the
production policy. A short run's success never opens long concurrency implicitly.

## Deployment and pending verification

The change is deterministic scheduler code. It adds no agent role and does not
select a model outside AI Router's managed execution contract. The GPU workers
remain on Ivan; AI only manages Router and deployment code.

Use the deployment script's read-only mode first, then a coordinated
`--scheduler-only --execute` window. The script copies the new admission module
and capacity policy, preserves backups and checks active/queued/reconciling jobs.
Database columns and the control table are additive; the old job data is retained.
Rollback restores code/configuration and the older service can ignore new columns.

Two initial coordinated scheduler-only deployments completed on 2026-09-09; all three
GPU worker process IDs and restart counts remained unchanged. The live version-2
execution/options/capacity contract was checked. The follow-up included a legacy
unknown-outcome recovery fix and verified continuation of expensive validation
staircases; 95 local tests passed. Subsequent recovery patches passed 108 tests,
including late validation-ID rejection, quiet-window invalidation, frozen busy
baselines, retention until the next quiet window and exclusive fast-lane admission.
Four scheduler-only deployments completed without restarting any GPU worker.
The workflow's real five-second preview subsequently completed under recovery;
its host/cgroup telemetry is preserved in the sibling
`20260909-capacity/workflow-wf_6e88049545d041afb1b042147c39813e` directory.
Its full portrait preview also completed: 480×864, 362 frames, 24 fps,
15.083333 seconds with AAC 32-kHz stereo; actual submission-to-observed-completion
time was 742.80 seconds, excluding 30.37 seconds waiting for admission.
Media SHA-256: `c6bebc99db4976a185a9b17a7addeb7d8cf1270be0403ea46e99b9470e22e344`.
The 147 samples covering execution recorded minimum RAM 65.88 GiB, peak cgroup
25.84 GiB, maximum host swap 1.485 GiB and baseline increment 0.467 GiB,
minimum root/offload free space 27.55/44.69 GiB, maximum temperature 85°C,
and no resource/worker/kernel alert. Raw results and stream metadata are in
`observed-preview-completions.json`, with compact summaries in
`preview-media-summary.json` and `full-preview-resource-summary.json`.
These technical checks do not replace the workflow's semantic acceptance.
Long-quality acceptance remains
separately tracked until its actual output is validated.
Unit and fake-upstream tests establish code behavior; they do not establish new
GPU capacity.
