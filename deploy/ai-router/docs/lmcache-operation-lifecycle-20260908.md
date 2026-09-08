# AI LMCache retained-request stall: 2026-09-08

## Root cause and evidence

LMCache GPU registrations were created at 2026-09-07 19:47:10 UTC and reaped at 20:17:27 UTC after 1,817 seconds without a first ping. The heartbeat started on the first transfer rather than registration. An unknown instance still received successful PING. A subsequent STORE raised for the missing GPU context, but the MQ callback logged the exception without returning a correlated response.

Thirty completed requests consequently remained in delayed-free bookkeeping; TP0 retained thirty STORE futures. The model had 600 physical KV blocks with only 43 free. An 87,004-token waiting request could not satisfy full-sequence allocation; the scheduler stopped at that queue head and also prevented a 1,229-token request from running. The observed state was running=0, waiting=4, KV usage about 92.82%, GPU utilization 0%. This was resource retention and head-of-line blocking, not an inference IPC deadlock. Hash block size 16 must not be confused with physical KV block capacity.

Local evidence (metadata only): `/home/ai/github/1Panel/experiments/workbuddy-immutable-history-20260908/ai-backend-stall-20260908.md` and associated scheduler/adapter/STORE error evidence. The new experiment directory is `/home/ai/github/1Panel/experiments/lmcache-stall-fix-20260908`.

## Changes

- Begin worker heartbeat immediately after registration; PING checks actual registration and triggers re-registration when it disappears.
- Return correlated terminal MQ errors. Only explicit rejection before GPU access is safe to release; timeout, malformed response and unknown execution outcome remain unsafe. Late/duplicate response IDs cannot finish another operation.
- Retain every STORE batch and its IPC event until all batches finish. Pending STORE/RETRIEVE operations prevent delayed-free completion, including cancellation and a new batch following an earlier completed batch.
- Protect registered GPU contexts from reaping/unregister while a handler or CUDA stream is active.
- Bound MQ and DeviceMessagingFuture.query waits (120 seconds by default). Unknown device outcome fails closed; it does not authorize block reuse. The legacy direct wait/result device synchronization API remains blocking; the active adapter queries successfully before obtaining a result.
- Router samples real progress counters. With zero running requests, positive waiting requests and unchanged complete counters for 30 seconds, it publishes `backend_no_progress` and makes the endpoint ineligible for new selection. A first suspicious sample starts a bounded background observer; no page refresh or new request is needed after that sample. This guard does not migrate an already executing answer or interrupt a healthy running prefill. Missing counters are not treated as zero.

## Validation and rollout

- Candidate runtime: 59 CPU tests passed, including 5 real loopback ZMQ tests; two of these are development builder checks. The shipped runtime suite contains 57 corresponding behavior tests and does not require the development builder.
- Router integration: 66 tests across history, public WorkBuddy normalization, Prefix Break and the 31 health guard tests passed; 22 cache contract and 2 metric tests also passed.
- Independent read-only review found no deployment-blocking defect; the direct device wait limitation is documented above.
- Installed production probe: unknown registration returns false for PING; STORE returned `not_started` in 0.00675 seconds, the future was terminal, and a following request worked. No GPU inference was submitted.
- Live idle registration-loss injection: one actual GPU registration was removed; heartbeat restored it in 8.06 seconds with both registrations present. Model process stayed unchanged and no inference was submitted.
- vLLM and LMCache were restarted serially during maintenance, with no active model request. LMCache DRAM contents were lost on restart; no cache files were removed. Router/Control rollout subsequently preserved native PIDs 3967906 / 3962394.
- Image: `1panel-ai-router:lmcache-lifecycle-20260908-r1`, SHA256 `b28992f608fb7f04fcfacd44d0aaf14d6553dbbd071edc98352a9157feeb6226`. Only health.py and runtime.py differ in executable application code from the previous image.
- AI endpoint disabled at revision 28 and re-enabled at revision 29. Post-rollout: enabled, healthy, registered_count=2, running=0, waiting=0; both Router instances accepting requests.

**Still pending:** joint acceptance with 8-10 subsequent real WorkBuddy rounds. Separately verify native completed-request release/no stalls, immutable history/common native token prefix, actual per-request cache counts and TTFT. Queue time must remain separate; missing/estimated counts are not measured hits. Cold first-request latency after restart is not a warm-cache result. No old conversations were replayed for inference and no fabricated prompts were used.

## Reproducible CPU check

The runtime tests require the patched LMCache 0.5.4 environment and its installed dependencies, but perform no GPU inference:

```sh
/home/ai/venvs/1cat-vllm-1.5.0-lmcache/bin/python -m pytest -o addopts= -q \
  /home/ai/github/1Panel/deploy/ai-router/scripts/patches/lmcache-operation-lifecycle-v1/tests
```

For a candidate overlay, set `SOURCE_ROOT` to its site-packages-shaped directory. Router guard tests are `tests/test_health_stall.py` and run using unittest or pytest.

## Rollback

Do not restore a stale complete registry/settings file. Disable only the affected AI endpoint using the authenticated Control action with its current revision; drain relevant Router ingress and wait for active requests to reach zero. Record the current image and runtime hashes first.

1. Restore Router/Control to `1panel-ai-router:workbuddy-immutable-history-20260908-r1` by tagging each `1panel-ai-router-{router-api-local,router-api-tail,router-control-local,router-control-tail}` image and recreating the corresponding service with compose `--no-deps --no-build --force-recreate --timeout 5`, only after draining. Do not recreate model/other adapter services as a side effect. Original image IDs are in `router-deployment.json`.
2. Stop AI vLLM, then independent LMCache, after idle confirmation. Run the guarded rollback below. It refuses runtime hashes that differ from this patch rather than overwriting another task's change.
3. Start LMCache and await health, then start vLLM and await native readiness. Verify registration/health before re-enabling the endpoint with the current revision. This runtime rollback reintroduces the documented stall defect; prefer the fixed runtime if only Router rollback is needed.

```sh
python3 /home/ai/github/1Panel/deploy/ai-router/scripts/patch-lmcache-operation-lifecycle.py rollback \
  --site-packages /home/ai/venvs/1cat-vllm-1.5.0-lmcache/lib/python3.12/site-packages \
  --backup /home/ai/github/1Panel/experiments/lmcache-stall-fix-20260908/maintenance-backup/runtime
```

The guarded installer checks exact before/after source hashes, compiles changed Python, backs up originals before mutation and writes files atomically. Service configuration backup is protected in `maintenance-backup`; do not print its secrets. No code was automatically committed or pushed.

## First real WorkBuddy validation after rollout

Real traffic started around 15:28 local. AI completed a cold 48,629-token request in 56.28 seconds to first output, saved 128 cache objects (6.35 GiB), and returned to running=0, waiting=0 and GPU KV usage=0. LMCache pending STORE tasks, in-flight tasks and read/write locks were all zero. Subsequent real requests reported exact backend cache counts: 48,000/50,305 (TTFT 4.88 seconds), 49,600/50,846 (3.58 seconds), 49,600/51,557 (4.26 seconds), and 51,200/52,140 (4.14 seconds). Each preserved the complete previous native token prefix.

A different continuation grew from 48,629 to 62,409 native tokens: cache reuse was 48,000 (98.71% of the previous prefix), while 14,409 input tokens remained uncached and TTFT was 26.84 seconds. This is not a complete cache miss; growing-tail compute must remain visible.

Edge real continuations preserved 59,138 and then 62,257 previous native tokens, with TTFT 9.39 and 7.97 seconds after a 66.04-second first request. Its cache counts remain global-difference estimates, not per-request measurements. No synthetic inference or old-context inference replay was used. Individual stable conversations still need the agreed 8-10-round observation; these results do not constitute full final acceptance. Request IDs, source labels and counters are retained in the experiment's first-real-joint-results.json and joint-acceptance-latest-requests.json.
