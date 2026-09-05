# Media API and Deployment

## Implementation and deployment boundary

The media source, host service unit, console and H3 contract extension are shipped
here. They are disabled by default. No import installs a service, changes an
existing Router database, restarts Codex/H3, or triggers generation.
The authorized September 4 rollout is tracked separately in
`media-live-acceptance-2026-09-04.md`; default source settings and current deployed
readiness are not the same claim.

Chat routing, privacy classification, text history and media workflows remain
separate. The latest local tool-schema normalization does not apply to media.

## Credentials and data

- Public routes use existing Client Keys, with explicit `media_models` grants.
  Existing clients receive no media permissions. Public grants allow only
  `siyuan-image` and `siyuan-video`; internal clients may select
  `qwen-image-3.0-pro` explicitly.
- `/api/media/*` uses the existing Admin Key. The console is at `/media`.
- Both Router API and control processes need `AI_ROUTER_MEDIA_INTERNAL_KEY`,
  matching the host daemon. The daemon listens only on `127.0.0.1:14020`.
- The host daemon also needs `AI_ROUTER_MEDIA_ROOT` (default
  `/opt/1panel/ai-router/media`), `AI_ROUTER_H3_KEY` and optionally
  `AI_ROUTER_DASHSCOPE_API_KEY`. The Qwen base URL must be the exact workspace
  origin in `AI_ROUTER_DASHSCOPE_BASE_URL`, not a guessed workspace or region.
- H3 requires `H3_ROUTER_KEY`, matching the daemon's H3 key.
- Put host credentials in a private service environment file, not Git. The
  example user service reads `~/.config/ai-router-media/service.env`.
  Install its isolated Python environment under
  `~/.local/share/ai-router-media/venv`; use `requirements-media.txt`.
  The service runs an immutable source snapshot through
  `~/.local/share/ai-router-media/current`, not the concurrently edited worktree.
- Create only the new media leaf directory as owner `ai`, mode `0700`. Do not
  change ownership of the existing Router data tree. SQLite and artifacts belong
  to the host daemon; API containers access them only via authenticated HTTP.

The Codex adapter connects to the existing app-server control socket. It checks
ChatGPT authentication and uses dedicated persistent threads with the imagegen
skill, disabled shell/connectors, and a narrow filesystem permission profile.
The proxy is a WebSocket byte relay; the adapter uses the `websockets` Sans-I/O
protocol and validates the effective permission profile returned by the server
before submitting a turn. It does not assume JSONL framing or that configuration
overrides alone establish isolation.
It never reads or exports the authentication file. Login, thread-schema and
sandbox compatibility must be validated before marking `codex_ready=true`.
Do not relax the profile just to make an old daemon accept it.

The daemon requires `ffmpeg` and `ffprobe` for metadata removal and stage output
validation. Its data directory has an exclusive worker lock. Do not run multiple
media workers against one directory.

## Public API

`GET /v1/media/options` returns account-scoped options and readiness. JSON image
generation and multipart edits use:

```json
{
  "model": "siyuan-image",
  "prompt": "A product photo",
  "use_case": "product",
  "aspect_ratio": "square",
  "background": "auto",
  "response_format": "b64_json",
  "n": 1
}
```

Endpoints:

- `POST /v1/images/generations`; `POST /v1/images/edits` with repeated `image`
  file fields. Editing permits 1-5 images, 10 MiB each; masks and local paths are
  rejected. Qwen fallback cannot take more than three references or guarantee
  transparency.
- `GET /v1/images`, `GET /v1/images/{id}`, `GET|HEAD /v1/images/{id}/content`.
- `POST /v1/images/{id}/cancel`: cancels queued work immediately. Running work
  becomes `cancelling` while its original upstream task is reconciled; a completed
  upstream image may still be archived. Explicit Codex cancellation interrupts
  only the owned original turn; timeout or daemon shutdown only detaches the
  relay. Qwen cancellation is advisory. This is not a refund guarantee.
- `POST /v1/videos` accepts H3 project fields and uploaded reference assets.
  Initial status is `queued`; creation starts only Context IR.
- `GET /v1/videos`, `GET /v1/videos/{id}`, `GET|HEAD /v1/videos/{id}/content`.
- `GET /v1/videos/{id}/stages`, `GET /v1/videos/{id}/stages/{stage}`,
  `GET|HEAD /v1/videos/{id}/stages/{stage}/content`.
- `POST /v1/videos/{id}/stages/{stage}/{start|approve|cancel}`.
- `GET /v1/images/{id}/outputs`, `GET /v1/videos/{id}/outputs`: immutable output
  history, including outputs from previous stage attempts.
- `DELETE /v1/videos/{id}` soft-deletes a terminal task. Image deletion is
  administrator-only. Files are not automatically removed.
- `POST /api/media/jobs/{id}/purge` is administrator-only and requires an already
  soft-deleted terminal task plus `{"confirm":"<exact task id>"}`. It removes that
  task's archived output files and stored request inputs, but preserves audit
  metadata, operation receipts and an idempotency tombstone. This is not secure
  erasure of backups, SQLite pages or upstream Codex/H3 history.

Send `Idempotency-Key` for generation requests and always for stage actions.
Reusing a key with different input returns 409. After a transport timeout, repeat
the same key or query the task; do not invent a fresh key to retry unknown work.
Image wait timeout returns 504 with a task ID when available. A lost client
connection does not cancel the job. Ambiguous upstream states are `reconciling`.

Clients that cannot hold a long image request may send `Prefer: respond-async`.
Generation and editing then return HTTP 202 with the durable task ID and
`Preference-Applied: respond-async`; poll `GET /v1/images/{id}` and download the
archived output after completion. Default synchronous behavior is unchanged.
The same opt-in works on the administrator image endpoints.

The WorkBuddy integration is in `../integrations/workbuddy/`. Its public Skill
ZIP contains no credentials. The local CLI keeps credentials outside WorkBuddy's
skill directory and uses authenticated artifact downloads, not bearer URLs in
model-visible output. See `workbuddy-media.md` for installation and boundaries.

Approve a stage with `{"output_id":"out_..."}`. Context IR additionally accepts an
edited `prompt`. Start the next stage with the approved predecessor's `output_id`.
Neither approval nor artifact retrieval starts the next stage. A stale version
returns 409. Failed or cancelled stages can be explicitly retried with a new key.

Creation may incur Context IR API costs even on a local generation strategy.
Cloud stages require explicit starts. Paid image fallback reserves a slot against
the separate daily quota (20 by default, UTC calendar day). Ambiguous attempts
retain their reservation; this is a conservative admission counter, not a claim
about actual provider billing.

## Outputs, privacy and configuration

Image responses retain `created` and `data` with Base64 or a Router URL; additional
task metadata is explicit. Video stages expose status, progress, output identity,
and archived text/video. Provider and host details remain in the admin view.
Video API stage actions are H3 extensions, not a claim of full OpenAI video API
compatibility.

Output URLs use revocable, output-scoped five-minute tickets. Ticket expiry does
not delete a file; query the task to get a new URL. Content supports Range and
HEAD. Disable URL query logging on all reverse proxies: ticket-bearing URLs are
temporary credentials. Never place a Client Key or Admin Key into a URL.
The media gateway removes output query strings from Uvicorn access records while
retaining path, status and structured request audit records.
Returned URLs are Router-relative; clients resolve them against the Router origin.
A missing or truncated archive returns 409 and schedules recovery from the
original upstream task or immutable output, not a fresh generation. Recovery
must match the original SHA-256 before replacing a file.

Video outputs have two distinct identifiers: `output_id` is the immutable H3
version used for approval and successor starts; `id` identifies the sanitized
delivery artifact used in download URLs. Never substitute one for the other.
Public MP4s are stream-copied without private workflow/model metadata, chapters
or non-audio/video tracks. Their published hashes describe the delivered clean
bytes. Original source bytes and hashes remain private under the media data
directory. Retired raw download IDs return 410 and are excluded from public
output history; recovery republishes a clean artifact without regenerating.
The H3 cloud-upload boundary independently prepares a clean copy of the approved
768P version and verifies unchanged audio/video packets, timing and decoded
frames before sending it to the cloud provider. Sanitization failure never
falls back to uploading the raw original.

`GET|PUT /api/media/settings` is separate from `/api/settings`; chat settings
cannot erase media configuration. Only enable `codex_ready` and `h3_ready` after
the corresponding real acceptance checks. Generation remains disabled initially.
Uploaded inputs, generated outputs, operation receipts and approval text remain
in the independent media directory, not the 30-day route-trace store.

## H3 installation and recovery

See `integrations/h3/README.md`. The prepared patch targets `be1edf4` and was
read-only checked against that checkout. Applying it, its SQLite migration and
restarting H3 require the approved deployment window.

Router-managed projects are read-only in H3's legacy UI and cannot enter legacy
batch schedules. Use the Router media console for version-bound approval. This
keeps older unversioned buttons from approving a newer unseen output while
preserving existing unmanaged H3 projects.

Back up SQLite using its online backup mechanism, including the existing H3
database, before upgrading. Revert by disabling new media submissions first;
retain the daemon and artifact APIs for in-flight reconciliation and downloads.
Do not remove new tables or media files as part of rollback.

## Acceptance

Run `pytest -p no:cacheprovider`, `node --check ai_router/static/media.js`, and the
isolated browser suite:

```sh
python3 tests/media_preview.py --state-dir "$HOME/.local/state/ai-router-media-preview" --port 14822
```

Use an existing Playwright installation via `PLAYWRIGHT_MODULE`, with
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` if necessary, to run `node tests/browser_media.cjs`.
The runner verifies a fixture marker before any writes. The preview has only fake
providers and synthetic artifacts; passing it is not real image/video acceptance.

Real acceptance requires explicit authorization for account usage and cloud fees:
one Codex generation, one edit, Qwen fallback, and serial short H3 samples covering
all advertised stages. Each output must be downloaded through the client-facing
Router API and each stage must be approved separately. Do not declare the
integration deployed or the included image allowance verified from unit tests.
