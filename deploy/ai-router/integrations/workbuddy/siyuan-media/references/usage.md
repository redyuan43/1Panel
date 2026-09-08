# Media CLI

Replace `<cli>` with the installed skill's `scripts/media.py`. On the deployed
Windows machine Python is `C:\Python312\python.exe`. The skill folder is
`C:\Users\Ivan\.workbuddy\skills\siyuan-media`. Other users should resolve their
own skill folder; do not copy Ivan's credentials.

## Read-Only Commands

```text
python <cli> doctor
python <cli> options
python <cli> list --kind image
python <cli> list --kind video
python <cli> status --job-id img_...
python <cli> wait --job-id vid_... --seconds 30
python <cli> outputs --job-id vid_...
python <cli> download --job-id vid_... --stage preview --output "preview.mp4"
python <cli> download --job-id vid_... --artifact-id out_... --output "older-version.mp4"
```

Downloads verify the published size and SHA-256 before publishing the local
file. Existing files are never overwritten. A video may be awaiting approval
even though its parent task remains `in_progress`; read the stage statuses.

## Image Workflow

```text
python <cli> new-operation
python <cli> image --operation-id OP --prompt "A blue ceramic mug" --use-case product --aspect-ratio square
python <cli> wait --job-id img_... --seconds 30
python <cli> download --job-id img_... --output "blue-mug.png"
```

For an edit, get a new operation ID and provide the selected local reference:

```text
python <cli> edit --operation-id OP --image "blue-mug.png" --prompt "Change the mug to green"
```

Optional image settings:

- `--use-case`: photo, product, ui, infographic, illustration, logo.
- `--aspect-ratio`: auto, square, landscape, portrait.
- `--background`: auto, transparent, opaque.

One image is generated per operation. Mask editing is not supported.

## Video Workflow

After the user confirms the initial Context IR cost:

```text
python <cli> options
python <cli> video --operation-id OP --prompt "A red cube rotates on a white table" --workflow-mode quality_gate --creative-profile product --aspect-ratio 9:16 --strategy fast --duration 4 --confirm-context-cost
python <cli> wait --job-id vid_... --seconds 30
```

`quality_gate` is the default on a Router that publishes modern workflow
options and generates each customer video as one continuous H3 execution.
`duration_ladder` is currently shelved and must only be used if a future Router
explicitly publishes it. `legacy_pipeline` preserves the original Context IR and H3 stage sequence.
If an old Router omits `workflow_mode`, the CLI omits all three new fields and
uses the legacy behavior. An explicitly requested modern mode is rejected
instead of silently changing it.

The returned stages define the actual pipeline; do not hard-code stage names.
Display the current stage's `output.text` or download its video using
`download --stage STAGE`. Also show `review.scores`, issue time ranges and the
suggested prompt when present. Preserve the exact `output_id` and `review_id`.

After the user approves exactly that output:

```text
python <cli> approve --operation-id NEW_OP --job-id vid_... --stage context_ir --output-id out_REVIEWED --confirmed
```

Only after a separate instruction to start the next stage:

```text
python <cli> start --operation-id ANOTHER_OP --job-id vid_... --stage preview --output-id out_APPROVED_PREDECESSOR --confirmed
```

An edited legacy Context IR may be approved with `--prompt-file "reviewed.txt"`.
A managed `plan` is an immutable package of prompt, storyboard and anchors; to
change it, explicitly regenerate the plan rather than changing its text during
approval:

```text
python <cli> regenerate --operation-id NEW_OP --job-id vid_... --stage plan --output-id out_REVIEWED --prompt-file "revised.txt" --confirmed
```

Quality review is advisory and never causes an automatic retry. After showing
the review, obtain an explicit instruction before requesting regeneration:

```text
python <cli> regenerate --operation-id NEW_OP --job-id vid_... --stage preview --output-id out_REVIEWED --review-id rev_REVIEWED --apply-suggestion --confirmed
```

This creates a new attempt for the same stage. It does not approve the current
version and does not start its successor.

Strategies: `fast`, `safe`, `cloud`; duration 4-15 seconds.
Modes: `t2v`, `i2v`, `l2v`, `fl2v`, `reference`, `hybrid`.
Workflow modes: `quality_gate`, `duration_ladder`, `legacy_pipeline`.
Creative profiles and video aspect ratios are server-published values; query
`options` before using them.
For non-text-only modes, first query `options` and match `required_assets`.
Upload each required field with `--asset "FIELD=path/to/file"`.
Audio settings: `--audio-policy native|reference|lock_source`;
`--use-embedded-video-audio` and `--watermark` are optional flags.

## Recovery and Cancellation

Every mutation uses its own operation ID. Local receipts persist the exact
request without its Authorization header. `resume` reuses that original request
and key, or queries the already accepted job:

```text
python <cli> resume --operation-id ORIGINAL_OP
```

A 409 conflict means the reviewed output, review, or request changed. Fetch
current state and ask the user; never silently approve or regenerate a
replacement version.

Only cancel when requested:

```text
python <cli> cancel --operation-id NEW_OP --job-id img_... --confirmed
python <cli> cancel --operation-id NEW_OP --job-id vid_... --stage preview --confirmed
```

Cancellation may remain pending while the original provider task is reconciled.
It does not guarantee a refund. Do not assume a stopped client process cancelled
the remote task.

## Operator-Only Setup

The distributable ZIP contains no key. `scripts/install.py` copies only the
four public skill files. An operator can supply the credential JSON through
local standard input using `--credentials-stdin`; there is no key command-line
argument. Do not have the language model construct that input.

Windows stores a CurrentUser-DPAPI encrypted blob under
`%LOCALAPPDATA%\SIYUAN\Media`, outside WorkBuddy's skill/task directories, with
an ACL restricted to the current user and SYSTEM. Linux/macOS use a separate
mode-0600 file under `~/.config/siyuan-media`; this is not encrypted at rest.
No credential is written to WorkBuddy model settings or uploaded with a skill.
DPAPI/0600 are not isolation from arbitrary code running as the same OS user.

The default API origin is `http://ai-x10drg.taild500c8.ts.net:4000`; the machine
must have access to that Tailscale network. `http://127.0.0.1:4000` is allowed
only for testing on the Router host. Other origins, proxies and redirects are
rejected. Do not change the helper to send the credential elsewhere.
