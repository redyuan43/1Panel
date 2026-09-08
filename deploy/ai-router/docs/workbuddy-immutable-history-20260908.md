# Immutable WorkBuddy history: 2026-09-08

## Problem
The raw input for b84ea1868624447483bd88deba45d176 -> 9fe5429653a14fa2beeaebda216c2435 preserved 27 prior messages and unchanged Tools, but latest-user relocation changed messages[1].content[0].text. Shared native input fell to 33,343 tokens, and the new inferred lineage hid this as a baseline. The latter request waited 75.33 seconds.

## Implementation
WorkBuddy keeps original caller input for lineage lookup in a separate `wb-raw-v1` identity namespace. This does not claim that a content match establishes the user's business-task identity. Explicit IDs and compaction/reset behavior remain in the existing lineage manager.

A shared SQLite `workbuddy_history` metadata index locates verified raw-history prefixes within the authenticated client and requested-model scope. Stable system content, message roles/content, images, function IDs/arguments, tool_call_id and name participate; known UI fields do not. Workspace/catalog dynamic sections are compared separately. Index entries reference existing encrypted archives; no new plaintext prompt store is created. Retention: 24 hours and at most 2,048 entries; lookup scans at most 256 scoped metadata candidates and decrypts at most three matching archives. Current tool definitions, schemas, tool_choice and availability always come from the current request.

The new `workbuddy_history_preserved` stage reuses the prior normalized message layout with an explicit raw-to-normalized position map. Identical dynamic snapshots are not reinserted. Updates attach to a newly appended user message or a separate late user context message after a tool continuation, preserving original tool results and function-call IDs. Position maps keep such additional context messages stable on subsequent requests. The previous dynamic snapshot must be present in the preserved archive. Historical model-visible content is compared before returning; an unexpected rewrite fails the check and sends the original caller input instead of relocating it.

If no verified prefix exists for an input already containing history, the historical reorder is bypassed. A fresh system/user input may still establish a first normalized snapshot. Missing/invalid archive/index state is reported explicitly and falls back to original caller content. True tool or stable-system changes can still require recomputation; this release does not conceal them or retain removed tools.

Prefix Break can compare the verified raw-prefix source request even across inferred lineage IDs. Unconfirmed associations are shown as uncertain rather than a first baseline. Token reconstruction remains distinct from actual cache restoration and backend usage. No additional inference or model-grading calls are used.

## Validation
- 121 focused CPU tests passed (history preservation, archive/index roundtrip with SQLite + Fernet, namespace isolation, dynamic synthetic messages, tool IDs/images, prefix diagnostics, existing content/cache/usage contracts).
- Ten existing isolated browser checks passed; UI resources remain in the existing audit page.
- Expanded test_core suite has the same 23 failures before and after the patch, involving current registry/physical-worker configuration expectations. These are not represented as passing and were not modified in this task.
- Original failing archive replay used native `/tokenize` only, retaining the actual downstream system scaffolding. Previous input 94,045; current input 96,612; fixed common prefix 94,045. No historical content/token-count reduction. This is CPU template evidence, not a measured TTFT benchmark.
- Existing encrypted archives seeded 91 valid index records and 182 namespaced raw aliases from a bounded scan of 256 records, with no errors.
- Independent review and tests were performed by the user-requested Luna verifier; the parent verified its test fixtures and the real native-token path.

## Deployment and rollback
Image: `1panel-ai-router:workbuddy-immutable-history-20260908-r1`.
Experiment: `/home/ai/github/1Panel/experiments/workbuddy-immutable-history-20260908`.
`deployment.json` records exact old/new images and service results. Baseline hashes guard against concurrent edits. Only this task's files are integrated; no automatic commit/push. Both APIs drain to zero before the two API and two Control services are recreated. AI vLLM, independent LMCache and Edge model start state are checked unchanged.

Rollback: drain both APIs and wait for zero in-flight requests. For each of the four services, tag its `old_images` SHA from `deployment.json` to `1panel-ai-router-<service>` and run existing compose `up -d --no-deps --no-build --force-recreate <service>`. Restore backed-up source only after comparing subsequent changes. The additive metadata table and `wb-raw-v1` Redis aliases are ignored by the previous image and expire; model processes and cache files need no restart or removal.

Real acceptance remains pending subsequent genuine WorkBuddy requests with a newly appended user message; deployment readiness and 94,045 common tokens do not prove a particular real first-output latency.


Deployment completion: all four services run the new image and both API instances report draining=false. Local and tail summary, request detail and updated audit JS returned HTTP 200. The live history reader successfully reconciled an encrypted archive through the seeded index. The first compose replacement timed out on lingering HTTP connections and collided with a temporary container during rollback; the never-started temporary container was removed and deployment completed using a bounded `--timeout 5` recreate after draining. Use `resume-deploy.py` and `deployment.json` for the recovery audit. All model process identities remained unchanged. No commit/push occurred; unrelated source baseline hashes were rechecked and preserved.
