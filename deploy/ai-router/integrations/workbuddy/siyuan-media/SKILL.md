---
name: siyuan-media
description: Generate images, edit supplied reference images, and create staged videos through the user's SIYUAN Router. Use for SIYUAN media creation, checking a media task, reviewing stage outputs, approving a version, starting an approved successor, or downloading results. Not for image analysis, unrelated local editing, or other providers.
---

# SIYUAN Media

For H3 Studio recipes A4, A4_C0, A4_C1 and B8, use the dedicated H3 Studio
MCP connector and its Skill. Do not submit those requests through the legacy
video CLI or creative workflow as a fallback. If the connector is unavailable,
report that boundary and retain the draft. Image and other media flows below
remain unchanged.

## Creative workflows (preferred)

When `options` publishes `creative_workflows.version=1`, use the server-owned
creative workflow. `siyuan/auto` itself handles natural-language planning,
selection and progress; do not reconstruct its stage machine in the client.

- Upload only user-selected photos with `upload --image PATH --role ROLE`.
  Preserve the returned asset IDs and the user's reference roles/order.
- If a local helper is needed to start a task, use `workflow-create
  --operation-id OP --kind video --prompt TEXT --asset-id ID --candidates 1`.
  It creates a proposal; it does not authorize all generation stages.
- Query with `status --job-id wf_...`. A chat reply ending does not mean the
  generation finished. Explain pending work and return the workflow ID.
- Natural-language confirmation/selection through `siyuan/auto` is preferred.
  The helper also supports `workflow-action --operation-id OP --workflow-id ID
  --request-file JSON`. Bind the current revision and exact output/review IDs.
  Submit only the operation the user actually authorized.
- Confirming a proposal authorizes its candidate samples. Choosing a sample
  and continuing authorizes one complete low-resolution preview and one local
  final, with zero automatic regenerations. This recorded scope allows the
  server to advance; do not ask again for each internal approved stage.
- Download a child image/video job using its existing job and artifact IDs.
  Keep this credential-protected helper for local upload/download; ordinary
  text requests do not require the helper.

The remaining commands describe low-level image and legacy-compatible manual
operations. Their per-stage confirmation instructions do not replace a newer
creative workflow's explicitly recorded execution scope.

Use the bundled local CLI. It holds the Router credential outside the skill and
returns sanitized JSON. Do not replace it with generated curl code or generic
HTTP tools: that would expose credentials or bypass its retry and origin checks.

Resolve `scripts/media.py` relative to this skill directory. Run it with a local
Python 3.10+ interpreter (`python` on the deployed Windows client; `python3` on
Linux/macOS). Commands below use `python <skill>/scripts/media.py`.

## Start Here

1. Run `doctor`. If unconfigured, ask the operator to configure the local
   credential store. Do not ask the user to paste a key into chat.
2. Check `options` for the account's grants and current media options.
3. For each new mutation, run `new-operation` once and retain its operation ID.
   Reuse that ID for retries of the same input; never regenerate it on timeout.
4. Supply the prompt directly or use a UTF-8 `--prompt-file` for multiline text.
   Upload only files the user selected. Never upload a whole workspace.

## Images

- Generate: `image --operation-id OP --prompt "..." --aspect-ratio square`.
- Edit: `edit --operation-id OP --prompt "..." --image "original.png"`.
  Repeat `--image` for 1-5 reference images, at most 10 MiB each.
- Capture the returned `img_...` ID. Poll using `wait --job-id ID --seconds 30`.
- On completion, use `download --job-id ID --output "chosen/output.png"`.
  Return the actual local artifact link and the task ID, not Base64 or raw URLs.

## Videos

Read [video workflow and commands](references/usage.md#video-workflow) when
creating or advancing a video.

1. Run `options`, then confirm the workflow, strategy, duration and creation
   cost boundary. The current Router publishes `quality_gate`, which executes
   directly on Ivan. Do not use `legacy_pipeline`: the Edge H3 route is retired.
   Only use `duration_ladder` when a future Router explicitly publishes it.
   A Router without workflow options is incompatible and must fail closed.
2. Only after confirmation use `video` with `--confirm-context-cost`. Supply
   `--workflow-mode`, `--creative-profile`, and `--aspect-ratio` only from the
   values published by `options`.
   Do not split one customer video into independent five-second H3 generations
   unless `duration_ladder` is explicitly published. Independent native audio
   and visible state changes at the joins are not accepted as a production
   continuity strategy.
3. Poll and show the current stage's text or downloaded video and its quality
   review to the user. Read the returned `stages`; never assume stage names.
4. Approval requires the user's explicit approval of that exact `output_id`.
   Use `approve`; it does not start the next stage.
5. Starting a stage requires a separate explicit user instruction. Use `start`
   with the approved predecessor's `output_id`. Warn before cloud stages.
6. A review may include scores, issue time ranges, and a revised prompt. It is
   advice only. Run `regenerate` only after the user explicitly accepts that
   suggestion, binding both the current `output_id` and `review_id`.
   Use `--apply-suggestion` only when the user explicitly chose the review's
   revised prompt. The `plan` stage has no quality `review_id`; regenerate it
   with its current `output_id` and an optional explicitly approved
   `--prompt-file`.
7. Stop after returning each stage's result. Do not interpret "make a video" as
   permission to approve unseen outputs or start every stage automatically.

Historical `legacy_pipeline` tasks are read-only. They may be inspected and
their archived outputs downloaded, but must never be approved, restarted,
regenerated or advanced. Do not try to skip a failed legacy stage.

`--confirmed` records the caller's assertion; it is not proof of a human click.
Never add it without actual user authorization. Router version checks do not
replace this client-side confirmation boundary.

## Recovery and Privacy

- For a timeout/unknown outcome, run `resume --operation-id OP`; the saved
  request uses the original idempotency key. Do not create a replacement task.
- `status`, `wait`, `outputs` and `download` do not advance a video.
- A SIYUAN model or technical review never approves, starts, or regenerates a stage.
- A download uses the delivery artifact `id`. Approval uses `output_id`; they
  are different for sanitized videos. Do not interchange them.
- Never read, echo, attach or summarize the credential/configuration files.
  Never place a key in tool arguments, environment configuration sent to a
  model, SKILL.md, task text or command output. Do not export credentials.
- Do not print complete HTTP responses: they can contain bearer download
  tickets or Base64 data. The CLI already filters those fields.
- Credentials remain on the client. Requests go only to the fixed SIYUAN
  Router origin, without environment proxies or HTTP redirects.
- Do not install or log into Codex/H3 on the user's client, access their server
  credentials, or enable the deferred Qwen fallback.

For additional CLI examples, file uploads, cancellation and recovery, read
[usage](references/usage.md). Missing credentials require operator setup, not
credential discovery by the agent.
