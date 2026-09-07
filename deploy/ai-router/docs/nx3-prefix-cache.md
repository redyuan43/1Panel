# NX3 WorkBuddy prefix cache

Production path: WorkBuddy `siyuan/qwen36-shared` → Router authenticated client
`workbuddy-qwen36-shared` → `qwen36-nx3` → port 8081 Python gateway →
loopback port 18081 patched llama.cpp. Other clients keep existing routing.
The pin overrides earlier NX4 affinity and waits up to 900 seconds for NX3 capacity.
An unavailable or incompatible NX3 returns an explicit error; it does not silently
move this client to another node with a cold cache.

## State and request handling

`qwen36_prefix_gateway.py` forwards the original request bytes. For unambiguous
text requests, it obtains the rendered template and real tokens from the backend,
and caches the prefix immediately before the last user message. Dynamic content
is still evaluated. Ambiguous, nontext, or short prefixes bypass snapshots.
The first request computes the prefix, saves it, then evaluates the original
request. This initial request is a cold miss even if its final native call reports
cached tokens; gateway `prime_tokens` must be included in cold accounting.

Later same-prefix requests use the retained in-memory checkpoint. Following a
backend restart or an unrelated prefix, the gateway validates and restores the
local snapshot before forwarding the original request. No manual prewarm or
restore call is needed from WorkBuddy. Snapshots contain target state, MTP state,
and checkpoint metadata. SHA-256 manifests bind them to model, libraries, actual
process arguments and relevant environment. Incompatible or corrupt files cause
an explicit logged miss and recomputation. The native checkpoint budget stays 2;
disk storage is bounded to 3 snapshots and 2 GiB.

## Installed files

- `/home/nx/llama/prefix-cache-production/`: gateway, configuration, launcher,
  isolated patched shared library, and original unit backup.
- `/home/nx/.local/share/qwen36-prefix-cache/`: persistent private snapshot data,
  atomic manifests and artifact hash cache.
- `/etc/systemd/system/nx3-qwen36.service.d/20-prefix-cache.conf`: native override.
- `/etc/systemd/system/nx3-qwen36-cache-gateway.service`: public port 8081 entry.

The original executable and `/home/nx/llama/lib` remain available for rollback.
The backend unit starts the gateway; restarting the backend also restarts the
gateway. Never run the experimental model service alongside production on NX3.

## Evidence and operations

Inspect both journals: `journalctl -u nx3-qwen36.service` for real restored and
evaluated token counts; `journalctl -u nx3-qwen36-cache-gateway.service` for
`miss_saved`, `hot`, `disk`, preparation duration and request hashes. Prefix labels
alone do not establish a hit. Compare cached tokens, total cold computation and
first-token latency. Logs must not contain prompt content or credentials.
Completed SSE usage/timings are authoritative for evaluated-token totals. For an
interrupted response with missing native timings, a zero aggregate in the gateway
log does not establish zero computation; treat the final token cost as unknown.
During the production test one real client stream ended with Router 499 and its
disconnect reached native cancellation about 41 seconds later. The archived reason
was stream_interrupted; whether the client canceled or timed out was not recorded.

Configuration is in `config/qwen36-prefix-cache-nx3.json`; service templates are in
`systemd/`; the native patch is `patches/llama-qwen36-prefix-checkpoints.patch`.
`scripts/build-qwen36-prefix-backend.py` builds against the audited source baseline
without overwriting it. A rebuilt binary needs its hash and runtime acceptance
checked before changing the expected production hash.

Before a maintenance restart, reserve NX3 capacity and wait for all active
requests to finish. Release only the maintenance reservation after health returns.
For Router updates, drain and replace API instances serially, waiting for each
instance's active-request count to reach zero; the control containers need no
change for this feature.

## Rollback

After draining NX3, stop the gateway and backend, disable the gateway, move only
`20-prefix-cache.conf` out of the native drop-in directory, run
`systemctl daemon-reload`, then start `nx3-qwen36.service` and verify port 8081.
Retain snapshots and production artifacts for diagnosis. The original API image
is `sha256:f858100bf8990b8465e96dbc064626287a2c4684a009fcc43b1446bfdefbf2fb`;
rolling back API containers to it removes client pinning. Do not overwrite the
runtime settings, other service drop-ins or unrelated uncommitted changes.

Full original-request evidence is retained under
`experiments/nx-prefix-cache-fix-20260907/reports/`. Private requests, responses and
credentials stay outside public deliverables.

## Change record

See [change summary, production evidence and known limitations](nx3-prefix-cache-release.md).
