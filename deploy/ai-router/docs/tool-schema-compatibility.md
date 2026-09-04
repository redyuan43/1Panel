# Local Tool Schema Compatibility

Date: 2026-09-04. Status: deployed to both Router API instances and live-verified.

## Behavior

For non-cloud `llama_cpp` and `ai_pool` endpoints, the Router converts
`additionalProperties: {}` to `additionalProperties: true` within function
parameter schemas. These constraints accept the same JSON values, but the
inspected local llama.cpp converter incorrectly interprets the empty schema as
object-only.

`protocol.normalize_llama_tool_schemas` copies the tool definitions and walks
schema positions only. It does not inspect tool names or parameter names,
rewrite examples/defaults/business data, fetch references, or repair generated
arguments. Boolean schemas and nonempty constraints remain in effect.

The integration in `api._prepare_routed_body` runs on the outgoing request copy
before final token accounting and explicit compaction. It supports Chat,
Responses, and the existing Responses adapter's nested function form.
Cloud, OpenAI, vLLM and Codex endpoints do not receive this compatibility rewrite.
Structured output schemas outside function tools are deliberately out of scope.

Original request bodies, messages, history boundaries, lineage IDs and routing
policy are unchanged. Existing archive handling can retain the received and
routed forms separately. Changes to tool-definition text can still invalidate
part of an existing prompt cache on the first adapted request; zero prefill is
not guaranteed.

## Offline Verification

Run from `deploy/ai-router`:

```bash
python3 -m pytest tests/test_tool_schema_compat.py -o addopts='' -q
python3 -m pytest -o addopts='' -q
git diff --check
```

The compatibility tests passed: 99 cases covering semantic equivalence across
JSON value types, existing rejections, schema nesting, local references,
literal-data preservation, backend isolation, final token accounting, explicit
compaction, cloud retry isolation, outgoing JSON/SSE preservation, and Chat and
Responses history persistence/restoration.

The implementation-stage full suite result was 384 passed and 1 failed in 58.50 seconds.
The independent failure is in
`test_validated_vision_endpoints_are_registered_for_images`: the concurrently
updated registry includes `agx-qwen36-cerebellum-256k` as an image endpoint,
while that test's expected set does not. Replacing the new conversion function
with an identity function reproduces the same failure. Neither the registry
nor that test was changed for this fix; the full suite is not green.

Python compilation and `git diff --check` passed.

## Actual Converter Check

The installed source at
`/home/ai/github/llama.cpp-dflash2/examples/json_schema_to_grammar.py`
was imported without modifying it or loading a model. With an object-valued
`params` property whose additional properties use the empty schema:

```text
Original:   params-additional-value ::= object
Normalized: params ::= object
            value ::= object | array | string | number | boolean | null
```

The normalized generic object rule uses `value` for its property values,
allowing the string that the original grammar excluded. This demonstrates the
converter workaround, not model-level task success.

## Live A/B Acceptance

The user authorized the following synthetic deferred-tool tests. No generated
tools were executed:

- Clean history, one validation error, and five repeated validation errors.
- Two paired A/B repetitions per scenario, at most 36 requests total.
- A uses the original schema; B applies the new function before submission.
- Keep paired inputs and sampling settings identical; vary tool/parameter names
  in the second repetition to avoid a name-specific test.
- Send explicit local-model requests through the existing Router for capacity
  coordination, one request at a time per host, only when idle; no cloud fallback.
- Limit each output to 1024 tokens and each inference request to 180 seconds.
- Record actual endpoint/deployment, request ID, arguments, finish reason, usage,
  cache information and elapsed time; never record credentials.
- Grade exact target arguments and successful stream termination, not HTTP 200.
- Check service identity and health before and after testing. Do not reload or
  restart models to make a test pass.

The completed run sent 36 requests: 35 reached a model and one AMD A request was
rejected before inference with `503 no_eligible_model` / `unhealthy_or_stale`.
That rejected request was not repeated.

| Model | Original A: correct/generated | Converted B: correct/generated |
| --- | --- | --- |
| AI Qwen3.8-27B Q4 DFlash2 | 0/6 | 4/6 |
| AMD Qwen3.8-Flash-Next ROCmFP4 | 0/5, plus one health rejection | 5/6 |
| AGX Cerebellum Q3_K_M | 0/6 | 6/6 |

All 12 B cases with clean history or one prior error passed. Three of six B
cases with five prior errors still produced the incorrect object-valued argument.
Removing a grammar restriction permits the correct answer; it does not
guarantee that a model corrects mistakes repeated in its history.

The AI pool selected multiple GPU deployments, so these results are not a
same-GPU performance benchmark. Both Router boot IDs and model process
identities stayed unchanged during A/B testing.

Raw reports are under
`/home/ai/.local/state/ai-router-acceptance/20260904-tool-schema-ab/`:
`7b1bf3009625.json` records the initial run and `525b74b48f64.json` contains
the combined results after resuming without repeating attempted cases.

## Production Deployment

The separately authorized rollout completed on 2026-09-04 at 20:08 and 20:09
(UTC+8). Only `router-api-local` and `router-api-tail` were drained and recreated.
GPU model services, control containers, Redis, LiteLLM and Codex Adapter were
not restarted.

To exclude concurrent workspace changes, the release used the existing
production image and replaced only `ai_router/api.py` and `ai_router/protocol.py`.
The complete 58-file runtime manifest was checked before and after deployment.
Image preflight caught restrictive copied-file permissions; `COPY --chmod=644`
corrected them before either production container was replaced.

- Image: `sha256:bb05ac80f9fc55086e353238f6012363d12aa2d459f502bfbe33786f488b148e`.
- Local API boot: `69cdea4492214a5d94b66c4d847497e0`.
- Tail API boot: `de0ea0e5ade4498ab5a5a85d653cb374`.

Both entries passed real Chat and Responses streaming checks using the original
empty Schema, without client-side normalization. Four acceptance cases passed,
with five HTTP requests in total: one AGX Responses attempt was rejected by the
health gate before inference, then the remaining cases used the idle AI pool.
Read-only archive checks confirmed `{}` on receipt and `true` in the routed
tool definition for all four successful requests.

| Entry | Protocol | Successful request ID |
| --- | --- | --- |
| Local | Chat | `1eed7acb57444b2dba4ffa4e08a7f238` |
| Local | Responses | `a993eba8c6cf4ff3896375ebe71865ca` |
| Tail | Chat | `ac5c54da21b54173996841ff3236d17f` |
| Tail | Responses | `c8ae1fe8651248c3a3f1669f2f5e1cd8` |

Deployment evidence is under
`/home/ai/.local/state/ai-router-acceptance/20260904-schema-deploy/schema-67e1d0758d36/`.
The independent AGX health rejection is `cf1bbab46b214d16a2775de01f289b9a`;
the underlying intermittent health-probe disconnect was not changed here.

After testing, the user disabled the AGX registry endpoint for separate
validation work. Do not reactivate, test, stop or restart AGX until the user
explicitly releases that restriction. Future inference tests require approval.
