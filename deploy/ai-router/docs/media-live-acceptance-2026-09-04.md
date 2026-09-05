# Media Live Acceptance: September 4, 2026

## Authorization and scope

The user authorized service reloads and multiple agents running real functional
tests, with the longest video test scheduled last. No Git commit or push was
performed. Existing worktree changes outside media were not included in this
deployment.

Router images were constructed from the existing API and control images,
overlaid with media handlers, account grants and the media console. The
pre-existing chat routing implementation, registry and text model services were
preserved. Redis, LiteLLM and the chat Codex adapter were not restarted.

## Deployment

- Host `media-adapter.service` is installed and enabled on `127.0.0.1:14020`.
- Host Python dependencies are isolated in `~/.local/share/ai-router-media/venv`.
- Source release: `~/.local/share/ai-router-media/releases/20260904-media-6`.
  The deployment manifest records the authoritative current release and hashes.
- API local/tail and control local/tail were reloaded. API instances were drained
  and observed without active requests before recreation.
- H3 was backed up with SQLite's online backup API, patched and reloaded on Edge.
  Existing projects and neighboring services were preserved.
- Shared Codex App Server was not restarted. Protocol checks used ChatGPT
  authentication and a dedicated effective `router_media` permissions profile.

The initial H3 startup exposed a runtime API difference: its FastAPI version no
longer exposes `app.add_event_handler`. The extension now registers through
`app.router.add_event_handler`. H3 recovered and authenticated contract calls pass.

## Evidence and current gates

Authoritative evidence root:
`~/.local/state/ai-router-acceptance/20260904-media-deploy/`.

| Gate | State | Evidence |
| --- | --- | --- |
| Deployed source scope and hashes | Recorded | `deployment-manifest.json` |
| Real chat regression | Passed, semantic response `MEDIA_ROUTER_OK` | `chat-regression.json` |
| Codex account/thread/effective permissions | Passed without generation | `codex-protocol-*.json` |
| H3 auth, create/replay, stale approval/start, legacy guard | Passed against real H3 | `h3-deploy.json` |
| Console login, settings, grant PATCH/readback, responsive layout | Passed against real console | `browser/live-*/report.json` |
| Public API auth, model disclosure and negative parameters | Passed without provider submission | `api-negative-tests.json` |
| Real image generation/editing and artifact validation | Passed | `images/generate.json`, `images/edit.json` |
| Context IR, preview and local 768 stages | Passed, including public downloads and real browser playback | `video-live/`, `browser/live-*/report.json` |
| Approval and successor-start separation | Passed; browser sent one approval, no start, successor remained pending | `browser/live-2026-09-04T14-00-33-490Z/report.json` |
| Video in-flight adapter reload | Passed with unchanged project, run, prompt and start-operation identities | `video-live/adapter-reload-*.json` |
| Public video metadata removal | Passed on preview and local 768; old raw URLs return 410 | `video-privacy/report.json`, `browser/clean-media-binary-scan.json` |
| H3 clean cloud-upload source | Passed without cloud submission; original sources and AV content unchanged | `video-live/upload-privacy.json`, `h3-upload-deploy.json` |
| Image in-flight adapter reload | Passed: one POST, one original turn and one completed image | `images/restart-recovery.json` |
| Fast pipeline through 2K | Passed: all stages approved, public download and real browser playback | `video-live/manifest.json`, `video-live/fast/`, browser 2K reports |
| Additional cloud 768 and proof coverage | Passed; bounded samples stop after their unique stage | `video-live/manifest.json`, `video-live/cloud/`, `video-live/safe/` |
| Populated video layout on local and TLS console | Passed at 1440, 1101, 1100, 768 and 390 pixels after tablet fix | `browser/live-2026-09-04T15-21-44-{549,610}Z/report.json` |
| Real Qwen paid image/fallback | Deferred by the user; excluded from this acceptance closeout | No DashScope key or workspace origin configured; fallback remains disabled |

Final H3 verification is `video-live/acceptance-completed.json`; actual cloud
submission privacy is `video-live/actual-cloud-upload.json`. The latter verifies
the upload hook's real provider-bound copy, not just a standalone sanitizer test.
Final browser evidence is `browser/2k-final-report.json`.

The real image generation task is `img_6ac7c887260b47d8ab6cfead736232cc`,
request `70c425382beb43cc8f2b3cec6e7c8c52`, with a 34.9-second job duration.
The edit is `img_4eb4b011b4b749ac9984ea0f6ebe9f34`,
request `0c0f33fcd0ff4656aeb65c37ce4a399b`, with a 45.8-second job duration.
Both are 1254x1254 PNGs. Visual inspection confirmed the blue mug generation and
green mug edit. Base64, Router URL and archived SHA-256 agree; HEAD, Range,
cross-client denial and replay of the same job/thread/hash passed. Each dedicated
Codex thread contains one turn and one completed image-generation item.
`account/read` reports `type=chatgpt`, `planType=pro`.

The restart-recovery image is `img_2327de80b60b41539f916dc500e870f8`,
request `3cafa9897baa4e57a56464b2a6b551ee`. The adapter was restarted once while
the original turn was `inProgress`; the same job, thread and turn completed
without another POST or turn. Its 1254x1254 PNG was decoded and visually reviewed,
and public Base64, URL, Range and archived hashes match. Job duration was 65.0
seconds. The adapter PID changed from 3685324 to 3702567; shared Codex PID 1674,
its start time and configuration hashes were unchanged.
That real restart test ran on release5. Release6 preserves the successful-image
recovery path and additionally requires a confirmed terminal failed Codex turn
before quota-triggered paid fallback can proceed. Ambiguous interruption remains
reconciling instead of starting overlapping paid work.

The real video is `vid_f76b81b2cdc142ed8c9d8353ea236d31`, H3 project
`b729a4668c15`, request `16dff9de793146e18e2ba1144842f413`. Its actual fast
pipeline is Context IR, preview, local 768 and 2K regeneration. All four stages
are now approved. Final H3 output version is
`out_518161741d6542d5be729957271f466f`; its clean delivery artifact is
`out_d0ff60e402c9e78dc6efe0321ed67407ab705493fcdc58b1b089c2d7277c962a`.
The real final MP4 was downloaded through Router, decoded and inspected using
sampled frames. It is 2560x1440, 24 fps, 4.459 seconds, with H.264 video and AAC
audio. Real browser playback showed advancing presented/decoded frames and
nonblack pixels; the browser download hash matches the published clean hash
`daeca163992213be163a750bc19ef1b9d57756eea2d0a8930842d65c44339317`.

The cloud coverage job is `vid_9fcc90b8704646bfa542f282283ec725`, H3 project
`4d355a4c967e`, request `bf80d1fc59954e1c8c2e3ddf185d6c7c`. It stops after the
approved `cloud_768` output `out_2ca42745672840a2a3aaa0beabdea3a1`.
The safe coverage job is `vid_60da8573795649eaa53e29ec5d0b068b`, H3 project
`d441e7440262`, request `7f9e10769bed4cf9bb7245ca9145266d`. It stops after the
approved `proof` output `out_f7051bf14a38412ebc2cc9d771c3f147`.
Their remaining stages intentionally stay pending, not failed or secretly
running. These are minimum unique-stage coverage samples, not two additional
complete 2K pipelines.

A populated video exposed a tablet layout defect missed by the earlier empty
layout check: at 768 pixels the output column was only 95 pixels wide. The
workspace now becomes one column at 1100 pixels, while the existing compact
stage layout remains below 760 pixels. The real 768-pixel output is now 514
pixels wide. Both console instances passed populated-video layout checks,
including the 1100/1101 breakpoint; no API or media backend was reloaded for this
CSS-only fix.

The generated preview and local 768 MP4s contained embedded Comfy workflow/model
metadata. They are retained only as private sources. Public delivery now uses
metadata-free stream copies with distinct delivery IDs and hashes while
preserving the original H3 approval version. Real public downloads and browser
playback passed; packet payloads are unchanged. A boolean-only scan found no
actual MiniMax key or nonempty credential fields in either original MP4.
The separate H3 upload hook verifies codec parameters, packet timing and decoded
frames as well as payload hashes. It never modifies the approved original or
uploads raw bytes after a sanitization failure.

Paid fallback is disabled while DashScope credentials are absent. Existing
accounts were not granted media access; dedicated acceptance accounts were
created. Credentials are stored only in private environment files.
On September 4 the user explicitly deferred Qwen testing. The remaining media
implementation, deployment and real acceptance scope is complete; no Qwen
generation or successful quota fallback is claimed. To resume that deferred
test, supply `AI_ROUTER_DASHSCOPE_API_KEY` and the exact
`AI_ROUTER_DASHSCOPE_BASE_URL` through the private service environment and record
the real result separately.

At final verification both Router API instances were running and not draining,
both control health endpoints passed, H3/Comfy queues were empty, and the shared
Codex PID was still 1674. The isolated fake preview on port 14822 was stopped.
No observer process remains running; formal services and real artifacts remain.

## Known verification boundaries

Early Codex probes caused the App Server to add trust entries for three dedicated
probe directories. Subsequent probes explicitly set untrusted project context
and verified the global configuration hash stayed unchanged. Existing user
configuration was not silently reverted.

An effective server-reported permission profile is not an OS-level negative
filesystem/network test. No exact billing bucket is inferred solely from
ChatGPT authentication or the absence of an API key.

The prior full worktree test run had unrelated GGUF API and AGX registry
expectation failures. Those concurrent changes remain outside this deployment.
The focused media, Codex protocol, H3 contract, upload privacy and deployment
suite passed 219 tests after the final cancellation/recovery changes.
