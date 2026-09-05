# NX3 Prefix Cache Validation

## Decision

The in-process, single-slot prefix cache is a Go for a dedicated text-only
WorkBuddy endpoint. The isolated Router path is also a Go. Disk slot restore
is a No-Go for this Qwen3.6 runtime. Production Router activation remains
pending.

## Runtime

- Model: `Qwen_Qwen3.6-35B-A3B-IQ2_S.gguf`
- Context: 57,344 tokens
- KV cache: `q4_0/q4_0`
- Batch: `2048`
- Micro-batch: `768`
- Slots: one
- Secondary RAM prompt cache: disabled
- Multimodal projector: not loaded
- Isolated endpoint: `nx3:18081`
- Authentication: `/home/nx/.config/prefix-cache-lab/api-key`, mode `0600`

## Real WorkBuddy Cases

All prompts were selected by the Terra validation agent from sanitized,
materialized WorkBuddy generation traces. Raw prompt content remains under the
ignored `private/` directory.

| Case | Prompt tokens | Cold TTFT | Warm TTFT median | TTFT reduction | Prefix reuse median |
|---|---:|---:|---:|---:|---:|
| `ea7d03ae60d94296` | 45,970 | 146.642 s | 2.419 s | 98.35% | 98.90% |
| `4607adc8c6eed8db` | 44,900 | 153.201 s | 2.595 s | 98.31% | 98.87% |
| `cd3c1184662a0db1` | 40,478 | 135.198 s | 2.403 s | 98.22% | 98.75% |

For the first case, changing only the final user request reused 44,963 of
45,979 expected prefix tokens. It re-profiled 1,029 tokens in 4.117 seconds,
with a 4.329-second TTFT. An identical request re-profiled four tokens and had
a 0.510-second TTFT.

No new OOM occurred in the isolated runtime. Resident memory remained about
12 GiB and available memory remained about 2.3-2.5 GiB.

The OpenAI-compatible Chat endpoint was also validated with a 1K fixed system
prompt. Cold TTFT was 3.881 seconds with zero cached tokens. The identical
second request had a 0.598-second TTFT and reported 1,036 cached tokens through
`prompt_tokens_details.cached_tokens`.

## Isolation

The former test on production port 8081 was invalidated by real Hermes traffic.
Two unrelated requests replaced the only active slot between the warmup and
the measured request.

The controlled interference test reproduced the behavior: after one unrelated
37-token request, the original 1,041-token request had zero cached tokens and
was fully evaluated again. A dedicated endpoint must therefore reject
unrelated workloads rather than relying only on conversation affinity.

## Restart Persistence

The slot endpoint successfully wrote and read a 1,040-token snapshot:

- Save: 71,884,224 bytes
- Restore: 71,884,224 bytes
- Saved and restored token count: 1,040

After restart, even the identical request reported zero cached tokens and
re-evaluated all 1,040 tokens. The file restores the sequence state and token
list, but this server build does not restore the server-level context
checkpoint metadata needed by the stateful Qwen3.6 context. A prefill-only
save ending exactly at the prompt boundary produced the same result.

The production design must treat model restart as a cold cache generation and
re-prime the fixed prefix in memory. It must not claim that the disk slot file
removes cold-start prefill.

## Random Size Validation

The final random validation used seed `348534234`. A refreshed metadata catalog
contained 1,755 traces, of which 745 successful non-empty generation inputs
remained after deduplication. Ninety-six candidates were sampled across eight
input-size quantiles; two were rejected because secret-like patterns remained
after sanitization.

The eight model-tokenized prompts were 17,678, 18,370, 32,221, 34,218, 36,968,
39,838, 47,186, and 50,603 tokens. Only one candidate was strictly in the
40K-50K band, so the second requested large case used the explicitly recorded
39,838-token nearest-safe fallback.

With `-ub 768`, direct validation produced:

- Cold TTFT median: 142.290 seconds
- Warm TTFT median: 1.948 seconds
- TTFT reduction: 98.63%
- Prefix reuse median: 99.249%
- Warm cache records passing 95%: 16 of 16
- Minimum warm reuse: 95.703%

The four-band Router subset produced:

- Cold TTFT median: 149.723 seconds
- Warm TTFT median: 2.209 seconds
- TTFT reduction: 98.52%
- Prefix reuse median: 99.225%
- Warm cache records passing 95%: 8 of 8
- Minimum warm reuse: 95.634%

The final reports are:

- `artifacts/random-validation-ub768/20260906-014415-verify-direct/report.json`
- `artifacts/random-router-validation-ub768/20260906-020341-verify-router/report.json`
- `artifacts/random-interference-validation/20260906-012949-interference-direct/report.json`
- `artifacts/random-output-isolation-chat-direct/20260906-025746-isolation-direct/report.json`
- `artifacts/random-output-isolation-router/20260906-031625-isolation-router/report.json`

Reports retain output hashes and lengths but no output excerpts. The
interference case proved that an unrelated request evicts the single slot:
prefix reuse returned to zero and TTFT returned to the cold range.

Output isolation used a unique non-sensitive marker for each selected case with
thinking disabled. The direct path passed all eight cases and the Router path
passed one case from each of the four size bands. Every response contained its
own marker and no marker belonging to another case. The Router request IDs were:

- Small: `6d032845a7e74c5c9e49e6d1661dd63d`
- Medium: `e18e20ecbeec4bc89571baec54e53040`
- Large: `c1a8647e8de743f4b6e272c123fdf521`
- Near-limit: `608bc7fcb42a4efbb4a5b5e55a143b24`

Unauthenticated requests to `/completion` and `/slots` returned HTTP 401 while
authenticated requests succeeded. After testing, the production service was
active, the lab unit was inactive, port 18081 was unavailable, and a production
chat request returned `NX3_PRODUCTION_OK`.

## Router Gate

## Isolated Router

The harness runs the repository Router code on localhost with an in-memory
state store and only the NX3 endpoint enabled. It does not change the
production Router.

The 45,970-token case produced:

| Phase | Router TTFT | Cached tokens | Prefix reuse | Affinity |
|---|---:|---:|---:|---|
| Cold | 146.836 s | 0 | 0% | explicit |
| Identical | 0.701 s | 45,993 | 99.99% | hit |
| Changed suffix | 4.622 s | 44,969 | 97.76% | hit |

The warm TTFT median was 2.661 seconds, 98.19% below cold. Router request
latency exceeded the matched NX3 upstream total by about 484 ms, 383 ms, and
359 ms respectively.

Full request IDs:

- Cold: `d954730c69f6453a934a9dc09ce0bdd7`
- Identical: `617975459da94474a5cda1989700ac68`
- Changed suffix: `2b66d297e02c47d48e20eca55eefb5c8`

The harness uses `SimpleTokenCounter`, so its `X-1Panel-Prompt-Tokens` header is
not an acceptance value. The production tokenizer JSON encoded the fixed
system prompt as exactly 45,970 tokens, matching the backend tokenizer. The
production Router continues to use `HuggingFaceTokenCounter`.

After restarting only the NX3 lab process, the next request in the same
logical conversation returned `affinity=cache-reset` and
`reason=cache_generation_changed`, with zero cached tokens:

- Before restart: `1e389253ad1644b6ac73f197546ea346`
- After restart: `3014e8a9ca894a72a1fad4d1257ae2f5`

## Production Gate

The static endpoint `nx3-qwen36-prefix-lab` is intentionally:

- disabled;
- excluded from `auto`;
- text-only;
- limited to one concurrent request;
- capped at 57,344 configured and safe context tokens.

The isolated harness proved:

1. explicit requests reach only `nx3:18081`;
2. the same logical WorkBuddy session keeps endpoint affinity;
3. cached-token metrics survive Router streaming unchanged;
4. a changed cache generation is reported after runtime restart.

Production activation must still prove access control prevents unrelated
clients or models from occupying the NX3 slot. The endpoint must remain
disabled and excluded from `auto` until the matching Router backend key is
installed in the production Router environment and a production deployment
check is approved.
