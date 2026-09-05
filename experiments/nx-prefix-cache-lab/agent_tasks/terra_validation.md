# Terra 5.6 Independent Validation

Use model `gpt-5.6-terra` with `reasoning_effort=high`.

## Inputs

- Read only this experiment directory.
- Read only sanitized cases referenced by the supplied selection manifest.
- Use the CLI and NX3/AI Router endpoints configured in
  `config/nx2-nx4.yaml`.
- Never open WorkBuddy credential, memory, blob, clipboard, or shell snapshot
  directories.

## Procedure

1. Record the supplied random seed and selected case hashes.
2. Run direct NX3 baseline and warm-cache verification.
3. Run the interference test.
4. Run the same selected cases through AI Router when the lab model is
   registered.
5. Compare direct and Router worker identity, cached tokens, TTFT, and output
   sanity.
6. Do not execute model-produced tool calls or write to external services.
7. Treat a missing metric as unverified, not as a pass.

## Acceptance

- Stable-prefix reuse is at least 95%.
- Median warm TTFT is at least 70% lower than cold TTFT.
- Direct and Router requests use the intended NX3 model process.
- No output contains content from another sampled case.
- Restart recovery is accepted only after a second request proves a cache hit.

Return a concise verdict with the report paths, random seed, failed cases, and
the first authoritative failure evidence.
