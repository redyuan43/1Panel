# Studio full-duration validation, 2026-09-09

## Verified preview results

All jobs below used native-audio T2VA, Turbo4, 480x864, 362 frames at 24 fps
(15.083333 seconds). Every output passed ffprobe video/audio validation and has
an independent seed, artifact hash and successful non-cached sampler history.
Model and conditioning caches are allowed and recorded separately.

| Run / batch | 4060 Ti | Main 3060 | Preview 3060 | Entire batch |
| --- | ---: | ---: | ---: | ---: |
| Dual | 529.605 s | 763.704 s | — | 766.285 s |
| Triple 1 | 525.901 s | 760.474 s | 766.066 s | 767.290 s |
| Triple 2 | 529.444 s | 767.904 s | 766.647 s | 768.608 s |

The triple batches completed three outputs in approximately the same wall-clock
time as the dual batch completed two. This is throughput evidence, not a claim
that three GPUs accelerate one video or that cached and cold runs are identical.
The separate per-card 15-second staircase was intentionally omitted at the user's
request; an existing successful 4060 Ti full-duration preview was retained as
baseline evidence.

## Memory evidence

| Metric | Dual | Two triple batches |
| --- | ---: | ---: |
| Minimum host MemAvailable | 64.20 GiB | 61.12 GiB |
| Peak H3 aggregate cgroup usage | 28.42 GiB | 30.26 GiB |
| Swap growth / swap-out pages | 0 / 0 | 0 / 0 |
| New cgroup high/max/OOM events | 0 | 0 |
| New kernel OOM/Xid alerts | 0 | 0 |
| Host read+write bandwidth median | 0.39 GiB/s | 1.60 GiB/s |
| Host read+write bandwidth p95 | 4.20 GiB/s | 6.28 GiB/s |
| Host read+write bandwidth sampled maximum | 15.55 GiB/s | 15.46 GiB/s |
| Maximum GPU temperature | 86 C | 86 C |

Bandwidth comes from one-second Intel uncore IMC counter samples interleaved
with the safety monitor. It covers the whole host and does not measure GPU
VRAM bandwidth, nor does the sampled maximum prove the hardware's bandwidth
ceiling. A single swap-in page occurred in each run, without swap-out or growth.
The triple run's maximum host memory PSI avg10 was 0.54%; no cgroup limit event
occurred. These observations do not show a capacity or sustained host-memory
bandwidth bottleneck for this preview workload. They do not validate 768P memory
requirements. Physical free RAM alone is misleading because reclaimable file
cache contributes to MemAvailable.

## Production decision

The reviewed `studio_preview` rule enables at most three native-audio Studio
T2VA Turbo4 jobs, up to 362 frames and 480x864 or its landscape orientation.
The full-duration measurements above are portrait; the rotated shape uses the
same pixel/frame resource envelope. Other tasks, six-step long previews,
references, quality jobs and mixed profiles keep their previous restrictions.
RAM, disk, swap, FIFO ordering, unknown-execution reservations and per-lane
exclusive ownership still apply. Capacity display evaluates this same admission
policy instead of equating idle GPU count with available job slots.

The update is limited to the independent Ivan Fleet and AI Studio. Neither AI
Router nor GPU worker processes were restarted during the full-duration runs.
## Mixed quality and preview observation

Run `h3val_07d0d24bcf2b46ca` placed a 768x1344, 14-step, 362-frame quality job on
`fast` and a 480x864, four-step, 362-frame preview on `main`. The preview completed
in 760.507 seconds while quality remained running, with a valid 15.083333-second
AAC/video output. Its hash is
`beda669973f7983d5bcdf5aeec6fc0837f333ce8a7bb6fd9750d18b97c5b72ef`.

The observed minimum MemAvailable was 60.98 GiB, peak H3 cgroup usage 29.64 GiB,
and swap growth zero. No kernel OOM/Xid was observed. Quality's first reported
sampling step arrived about 478 seconds after submission. To avoid making an
hour-plus slow path a prerequisite for usable preview delivery, the owned quality
job was deliberately cancelled after preview completion. The lease was released.

This proves preview completion alongside an active quality sampler, **not** full
quality completion or mixed-workload stability through high-resolution decoding.
`report.json` correctly remains failed/interrupted; `partial-result.json` records
the deliberate latency-based stop and the verified preview separately. Production
mixed capacity remains closed. The three-way preview rule is based exclusively
on the completed dual/triple runs, not this interrupted experiment.

## Normal production UI acceptance

Three projects were created, manually approved and submitted through the actual
8445 frontend, without a validation lease. The observer measured three active
worker queues simultaneously. All three upstream histories completed successfully
with actual sampler execution, not cached sampler outputs.

| Project | Lane | Shape | Studio elapsed |
| --- | --- | --- | ---: |
| `ecd858a56f47` CCD Coser | fast | 480x864 | 579.1 s |
| `2d45e048187a` stage dance | main | 864x480 | 806.5 s |
| `bc03fc27efca` toy train | preview | 480x864 | 762.2 s |

These are frontend stage times, including orchestration and artifact delivery,
not the earlier table's isolated worker execution times. Every delivered MP4
contains 362 video frames, 15.083333 seconds and AAC audio, and passed full
FFmpeg decoding. Both portrait and landscape players passed read-only desktop
and mobile geometry checks with `object-fit: contain`. All projects remain at
preview approval; no high-resolution or paid cloud stage was started.

Open `https://ai-x10drg.taild500c8.ts.net:8445/?project=<id>&stage=preview`.
The original Coser/dance projects were not overwritten. Final capacity reports
zero active jobs, zero queued jobs and three admitted Studio preview slots.
Evidence copies are `production-observer/` and `production-ui/completion.json`
under the AI evidence root; the latter includes complete artifact SHA-256 hashes.

## Evidence and recovery

For subsequent real-video validation, the user selected the retro-pink fashion
commercial in `../../h3-video-studio/docs/validation-prompt-15s.txt`. Only the
requested duration changed from 25 to 15 seconds; preserve the remaining text.
Use it as the original Studio prompt through the existing prompt-processing path,
not the older toy-train/Coser fixtures for newly started rounds. Already-running
acceptance tasks keep their original graphs and prompts. A prompt mentioning 4K
does not change the measured preview output resolution or establish 4K support.

- Ivan root: `/mnt/ivan-ext4-offload/h3-fleet/evidence/studio-full15-20260909/`
- Dual: `h3val_306ff5b26d804d43/report.json`
- Two triple batches: `h3val_73e47812fca04434/report.json`
- AI copy: `/home/ai/.local/state/h3-studio-ivan-production/deployment/full15-validation/`
- Pre-promotion Fleet files: `/mnt/ivan-ext4-offload/h3-fleet/releases/full15-promotion-20260909/before/`

Reports include artifact names and SHA-256 hashes. Each run directory also
contains executed graphs, upstream histories, worker logs and `metrics.jsonl`.
The obsolete run `h3val_6c9dadca1aca4dca` is retained as failed evidence: its
second execution cached the sampler and is not part of these results.

Rollback requires an idle/drained Fleet: restore its backed-up `main.py`,
`admission.py` and `capacity.json`, then restart only `h3-fleet.service`.
Do not discard queued/reserved jobs or interrupt unrelated GPU work.
