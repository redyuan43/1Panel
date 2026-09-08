# WorkBuddy prefix normalization and break diagnostics (2026-09-08)

## Change and scope

Authenticated `workbuddy-public` Chat requests now use the existing guarded WorkBuddy transformation for all permitted aliases, including `siyuan/auto`. The legacy shared-client model guard remains. The latest user message receives the workspace and Agent/Skill/ToolSearch dynamic descriptions; assistant/tool continuations, tool call IDs/arguments, images and schemas remain intact. Function tools are sorted by name, with recursively canonical JSON object keys; array order, values, actual tool availability and `tool_choice` remain unchanged. Unsupported or duplicate tool definitions are skipped.

Native Edge `/model/tokenizer_config.json` renders the entire tools list BEFORE system/history, in array order via `tojson`. Its XML function-call contract is unchanged. Removing or adding a real tool can still invalidate the downstream prefix. Keeping unavailable tools is deliberately unsupported. This release does not claim to eliminate latency spikes caused by genuine tool-set changes, queueing, reasoning or cache eviction.

## Prefix Break

Completed WorkBuddy audit traces enqueue bounded background diagnostics (two outstanding tasks per Router process, one inspector at a time, 30-second inspector deadline, 8-second tokenize HTTP timeout). Diagnostics read the existing encrypted archive, compare the prior successful nonoverlapping request on the same model/deployment, and use the final archived execution attempt. All template tokenization calls use `/tokenize`; there are no extra completion/model-evaluation calls. Requests without native tokenization retain structural comparison only. Missing/corrupt archives, incompatible requests, network errors and invalid counts stay unavailable.

The audit's Content and Model Execution nodes show zero-based first-different-token position, shared prefix length, changed stage/field paths and tool add/remove counts. A Tools-only counterfactual that moves the break supplies stronger attribution; otherwise field attribution is explicitly a structural candidate. Reconstructed template tokens are NOT historical inference counts, cache restore boundaries, or proof of cache hits. A template/model revision can invalidate comparability; the UI states this limit. Diagnostics become visible on a later refresh and never load prompt originals in polling responses. Original prompt viewing remains the authenticated on-demand existing interface.

Metadata lives in `prefix_breaks` in the AI audit SQLite database, keyed by `(request_id, attempt)` with a created-time index and 30-day deletion. No new cache snapshots or model memory allocations are introduced. No NX3, AGX or Edge disk budget is changed.

## Evidence

- 105 focused CPU tests passed: Prefix Break, WorkBuddy public and legacy transformations, content audit, cache audit and usage contracts.
- 10 existing isolated browser checks passed (routing graph, audit review/content, summary links, refresh behavior).
- Additional isolated browser checks passed for token index zero, untrusted field escaping, Prefix Break disclosure refresh preservation and content/execution linkage.
- Independent Luna 5.6 review found no substantiated production regression or cross-request/attempt mixup.
- Three original archived requests were rendered using native CPU `/tokenize` only. No old conversations were replayed for inference.
- Incident original round 6 -> 7: common prefix 2,143 tokens. Replacing only current tools with previous tools gives 66,361 common tokens, confirming Tools attribution. Round 7 -> 8 preserves all 71,206 prior reconstructed tokens.
- With the new transformation, round 6 -> 7 shares 6,085 tokens (real tool deletion remains); round 7 -> 8 preserves all 70,813 previous reconstructed tokens. All six content-preservation checks passed for each original request.
- Historical tokenizer reconstruction differs from recorded inference usage; do not substitute these values for historical backend counters.

## Deployment and rollback

Image: `1panel-ai-router:workbuddy-prefix-break-20260908-r1`.
Only the two Router API and two Control services are updated. Both APIs drain to zero active requests first. AI vLLM PID 2222966, independent LMCache PID 237071 and Edge container start time are checked unchanged. Source integration compares baseline hashes before copying only this task's files; existing unrelated edits and Git HEAD are preserved. No commit or push is performed.

Deployment manifest, immutable image IDs, source backups, sanitized evidence and browser artifacts: `/home/ai/github/1Panel/experiments/workbuddy-prefix-break-20260908/`. `deployment.json` is authoritative for completion and rollback images.

To roll back, drain the two APIs and wait for zero in-flight requests, then tag each service's `old_images` SHA from `deployment.json` to `1panel-ai-router-<service>` and recreate only that service using existing compose with `up -d --no-deps --no-build --force-recreate`. Restore files from `deployment-backup` only after checking for subsequent edits. Do not drop the additive table or remove cache files. Model processes and independent LMCache need no restart.

## Pending real acceptance

The user chose a NEW genuine WorkBuddy task. Observe 8-10 consecutive completed requests after rollout; do not fabricate prompts, invoke model grading or replay old conversations. Record request/conversation ID, deployment, backend usage source, input/cache counts, TTFT, queue time, Prefix Break and actual tool-set mutations. Separate the first cold baseline and source/template changes. A normal growing-history sequence should retain the stable prefix without unexplained 70-80-second TTFT spikes. This real acceptance remains pending until that traffic exists; CPU and browser tests are not latency acceptance.


## Real traffic snapshot, 2026-09-08 13:08 UTC+8

User reported two real tasks. Main lineage 493148ea5b434c9cbc4e45077f0cad5b: 13 successful requests, all 12 continuation native reconstructions preserve the previous input prefix. Main lineage d9a868b2052b497e9f9683530aca1bbe: 9 successful requests, all 8 continuations preserve the previous input prefix. All observed transformation conservation checks passed. Tools stayed at 24 without definition changes in these continuations.

After subtracting measured Router queue time (this remainder is NOT pure prefill), median first-output wait was 6.64s / 5.21s; ranges 3.82-10.69s / 3.52-7.37s. Main first requests waited 81.99s / 62.79s. A 66.26s continuation included 58.82s Router queue; another 55.53s continuation included 50.65s queue. Edge reuse estimates span 91.87-97.40% / 93.31-97.11% of TOTAL input, and remain counter-delta estimates, not per-request usage or fixed-prefix pass rates.

Extra one-request lineages exist in the same time window. Some spilled from busy Edge to AI (`capacity_spillover`), and request 9fe5429653a14fa2beeaebda216c2435 on Edge waited 75.33s with estimated reuse 24,000/96,612 tokens. Their relationship to the two tasks is under investigation. Overall latency acceptance is PARTIAL: normal main-history preservation is evidenced; cold/branch/spillover latency remains unresolved. No synthetic inference or model grading calls were sent.

Sanitized machine-readable snapshot: experiment `real-acceptance.json`.


### Acceptance failure found: new user turn rewrites history

Direct native archive comparison establishes a relationship that lineage metadata missed: `b84ea1868624447483bd88deba45d176` -> `9fe5429653a14fa2beeaebda216c2435` has 27 -> 30 messages at `after_directives`, with ALL 27 previous rendered messages preserved and identical Tools. At `workbuddy_reordered`, only the first message remains identical; the first changed field is `messages[1].content[0].text`. The transformation relocates dynamic content from the former latest user message to the newly appended user message, thereby rewriting historical model input. Native CPU tokenization yields 94,045 -> 96,612 input tokens but only 33,343 common tokens. The latter request waits 75.33s, with estimated 24,000 cached tokens. This is not a true Tools availability mutation.

Reordering precedes `_lineage_context`, so the rewritten history also becomes a new inferred lineage and Prefix Break reports a baseline. New-conversation labels therefore cannot establish independence from the main task. Overall acceptance is FAILED for newly appended user turns, despite all 20 tool/history continuations preserving their prior prefixes. A fix must keep prior normalized history immutable and use pre-reorder client history for lineage/diagnostic association; simply moving the current dynamic block to a different fixed location cannot preserve past dynamic values. No further production changes were made during this read-only check.
