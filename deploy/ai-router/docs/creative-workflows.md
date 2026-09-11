# SIYUAN Creative Studio v1

The canonical workflow owner is the AI media daemon. Ivan H3 fleet executes GPU
jobs. Both `/media` and `siyuan/auto` use the same durable workflow and operation
records. Existing media jobs, outputs, reviews and immutable artifact downloads
remain available; the previous console is retained at `/media/legacy`.

## Source provenance

The Studio presentation reuses H3 Video Studio on Edge, commit
`be1edf4d1eeed8762b98aba691bfe0d445db5964`: the original stylesheet is retained as
`studio-base.css`, the local Lucide bundle as `studio-icons.js`, and pipeline,
player and inspector presentation are adapted to workflow/candidate records.
Edge's only outstanding main.py patch installs its Router contract; it does not
change that frontend. No Edge database or credentials are migrated.

AI source baseline: `608ae1bef1b954f89ec933744d55414253ba5011`, including the
pre-existing uncommitted media, review, routing and WorkBuddy changes. The full
baseline patch and file hashes are retained in the operator's private state
directory `siyuan-studio-20260909/baseline`. The original user-reported WorkBuddy
failure has not been proven; regression coverage reproduces the concrete
capability mismatch and prevents silent fallback to the retired workflow.

## Public interface

- `POST /v1/media/assets`: JSON uploaded image data or multipart `file` plus
  `role`; returns owner-bound immutable `asset_id`. Maximum 10MiB per image.
  Roles: subject, product, style, first_frame, last_frame, reference.
- `POST /v1/media/workflows`: kind (image/video), prompt, asset_ids (0–5), duration
  (4–15), aspect_ratio (9:16/16:9), candidate_count (1–3), direction_images.
  Defaults are 15 seconds, portrait, one candidate. Image `start:true` means
  explicit authorization to generate one image immediately.
- `GET /v1/media/workflows`, `GET /v1/media/workflows/{id}`.
- `POST /v1/media/workflows/{id}/actions`: exact `revision`, action and its
  parameters. Actions: confirm, revise, approve_frames, select, rerun,
  add_direction, resume, cancel. Every mutation requires Idempotency-Key.
- `POST /v1/media/workflows/{id}/messages`: revision, message and optional spec.
- `GET /v1/media/workflows/{id}/events`: authenticated SSE, Last-Event-ID or
  `after` cursor. Reconnect when the bounded stream ends.

Administrator equivalents are `/api/media/assets` and `/api/media/workflows`.
Public media grants remain mandatory. Returned jobs include the existing
authenticated delivery APIs; only the browser receives short-lived preview
tickets. The WorkBuddy helper filters tickets and downloads by artifact ID.

## Conversation adapter

Set `AI_ROUTER_CREATIVE_CHAT_ENABLED=1` on Router API instances only after
acceptance. Chat Completions and Responses retain `model:siyuan/auto`, with an
optional `media:{workflow_id,revision,asset_ids}` extension. Continuations can
also carry the preceding assistant's workflow/version marker. No global
"last active job" is used. Initial replay keys are derived from the account and
request body unless an explicit Idempotency-Key is supplied.

The response's `media` metadata distinguishes generation completion from the
end of the chat response. A queued task remains durable after disconnect; ask
for progress or poll its status URL. Discussion, examples, analysis and prompt
writing pass through ordinary text routing without creating render work.

Planner calls use the existing internal `siyuan/auto` review client, accompanied
by an HMAC-signed read-only context validated with the media internal secret.
User text cannot establish this context. The planner has no tool execution;
malformed/unavailable planning returns an explicitly labelled editable initial
proposal, never a render. Raw reference Skills are not executable dependencies.

## Execution and recovery

Plan confirmation authorizes only the specified five-second samples (and
optional direction images). Subject/product/style references require explicit
conversion into a direction frame. Those frames require exact output-version
confirmation before sampling. Pure text mode uses no hidden image anchors.

Selecting an exact sample authorizes one target-duration preview and one local
768P final, with zero automatic regenerations. Review failures stop progression.
A user can explicitly accept an archived uncertain review with `resume`, binding
both output_id and review_id. The original review remains unchanged. Internal
stage advances record `approval_source:workflow_policy` and the user execution
scope; they are not represented as human clicks.

Transitions and operation receipts commit together. Child submission keys are
deterministic and reconciled after restart. Revisions retain previous plans and
outputs. Adding or rerunning one candidate preserves other completed candidates.
Cancellation targets only child jobs belonging to this workflow.

First version excludes cloud 768P/2K, arbitrary remote URL downloads,
reference-video/audio generation, reverse prompt reconstruction and scheduled
batches. GPU capacity defaults and validation evidence belong to `h3-fleet`.

## Isolated validation

Run focused pytest suites for creative workflows, existing media workflows,
the client and both public protocols. `tests/creative_preview.py` provides a
strictly synthetic Studio and protocol fixture on loopback with no GPU/model
access. Its marker is `/__studio_preview__`; fixture key is `studio-preview-only`.
Real GPU and desktop acceptance are recorded separately from synthetic tests.
