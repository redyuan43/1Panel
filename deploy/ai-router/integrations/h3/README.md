# H3 Router Contract

Prepared against H3 `be1edf4`. Router startup and tests never install these files
on Edge automatically. Deployment is an explicit operator action with an online
SQLite backup.

1. Review `router_contract.py` and `main.patch`.
2. Copy the module to H3's `app/router_contract.py`.
3. Run `git apply --check main.patch` against the H3 checkout, then apply it.
4. Set `H3_ROUTER_KEY` through the service's private environment file. Configure
   the matching `AI_ROUTER_H3_KEY` in the media daemon; never commit either key.
5. Restart H3 only in the approved deployment window and test `/api/router/options`.

`deploy_edge.py` performs a read-only inventory by default. Its explicit
`--deploy` mode requires a clean, matching H3 checkout and a private local
`~/.config/ai-router-media/h3-key` (0600). The key travels over SSH standard input,
not process arguments or output. It writes only the H3 extension, entry patch,
private H3 environment file and the dedicated user-service drop-in.

Before stopping the target service, the helper backs up SQLite with the online
backup API into `~/.local/state/h3-router-deploy/`, checks integrity, and holds a
database write lock while rechecking active projects and batch tasks. Since H3
persists queued state before dispatch, this prevents a newly accepted task from
slipping between the idle check and stop. Busy deployments are deferred. No
ComfyUI, model or ASR service is restarted.

The extension must also import successfully in the actual H3 virtualenv before
service stop. Startup registration uses `app.router.add_event_handler` because
the Edge FastAPI 0.141.1 runtime no longer exposes it on the application object.
An unavailable target after startup triggers an idle-only restoration of the
backed-up entry file. Reports preserve the backup and before/after service and
source evidence.

`--verify` reuses the original report and runs real, non-billable API requests
without reloading: authentication, options, persistent project creation,
idempotent replay/conflict, invalid and stale stage actions, legacy write
protection (including JSON-escaped project IDs), and unchanged old projects.
The acceptance project remains pending. Context IR and video generation are
separate, explicitly scheduled acceptance steps.

Existing projects and generation workflows are unchanged. Router-managed
projects are readable in the existing H3 UI, but writes and legacy batch
enrollment must use the versioned Router API. This is intentional: the legacy
page does not know which immutable output the Router user reviewed.

The extension wraps the existing single-process ProjectStore with SQLite
transactions. Run one H3 application process, as before. Dispatch callbacks carry
run identity; old callbacks cannot change newer project state. Stage outputs are
copied to immutable version paths before approval becomes available.
Dispatch is deferred until the operation receipt commits. Rolled-back operations
cannot start a worker, and local GPU reservations are released on rollback.

A crash between committing a queued stage and starting its thread may leave the
stage interrupted under H3's existing restart recovery. Retrying the same
operation returns its receipt and does not silently repeat a paid generation.
After reviewing the interrupted state, use a **new** operation key for a retry.

## Cloud Upload Privacy

`Contract.regenerate_2k` wraps only the existing MiniMax 2K upload boundary.
Immediately before submission it checks the current approved 768P predecessor,
source artifact, run and output version, then prepares a private MP4 copy with
`sanitize_upload`. Original immutable outputs remain untouched.

The copy uses stream copy, not re-encoding. Container/stream metadata, chapters,
subtitles, attached pictures and data tracks are removed. Packet payload hashes,
normalized packet timestamps, codec parameters and decoded audio/video frame
hashes must match the original. Validation failure never falls back to uploading
raw bytes. Approval and cancellation are checked again after preparation; private
upload evidence is stored on the H3 stage and excluded from Router public JSON.

`deploy_upload.py --execute --report <private-report>` upgrades the reviewed,
already-installed extension. It guards the expected HEAD and source hashes,
backs up live SQLite, imports the actual Edge application before stopping H3,
and checks both H3 activity and the Comfy queue. Only H3 is reloaded. It verifies
unchanged projects, raw outputs and neighboring services. The original extension
is restored on an idle deployment failure.

`verify_upload.py --project <existing-project> --report <private-report>` invokes
the installed sanitizer on the existing preview and local 768P sources. This
creates only private verification copies: no provider calls, approvals or GPU
generation. Credential checks emit booleans only, never credential values.
With `--existing-report <prior-report>`, it checks the previously prepared copies
without another remux or any Edge artifact write. Literal matching scans the
entire original MP4, including chunk boundaries, not just ffprobe metadata.

The Router acceptance script's `--phase delivery` also makes no generation or
approval call for the owning client. It verifies the existing stages, using
`output_id` as the stable H3 approval version and `id` as the clean delivery
identity. H3 raw bytes must match the private source archive; served bytes must
match the clean archive. Both must preserve AV packet and decoded frame content.
Old raw delivery IDs must return 410 and disappear from public history; public
JSON must contain no `source_*` fields. Cross-client checks use an independent
real client and an invalid-version mutation probe, which must return 404.
Completion leaves local 768P awaiting approval and 2K pending until the
coordinator explicitly releases the next phase.
