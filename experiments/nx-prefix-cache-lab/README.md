# NX Prefix Cache Lab

Independent command-line tooling for measuring WorkBuddy long-prefix reuse on
NX2-NX4 before integrating the behavior into AI Router.

The lab keeps real WorkBuddy data and generated outputs out of Git. It supports
two transports:

- `direct`: calls the physical llama.cpp process and records native timing
  fields.
- `router`: calls AI Router and verifies physical worker affinity and
  end-to-end latency.

AI Router does not make NX3 compute faster by itself. Its performance value is
keeping requests with the same stable prefix on the worker that already owns
the KV cache.

## Safety Boundary

- Windows data discovery is read-only over `ssh ivan-laptop`.
- Only generation inputs from selected trace files are copied.
- Credentials, memory, blobs, clipboard images, and shell snapshots are not
  scanned.
- Selected prompts are deterministically redacted and stored under `private/`.
- Service lifecycle commands are disabled for NX2 and NX4.
- NX3 lifecycle commands require the explicit `restart-test --apply` flag.
- No command deletes model files or invokes model-produced tool calls.

## Setup

Run from this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[router]"
```

The existing AI Router environment can also run the module directly when
`httpx` and `PyYAML` are installed:

```bash
python3 -m prefix_cache_lab.cli inspect
```

## WorkBuddy Data Preparation

Generate a metadata-only catalog:

```bash
python3 -m prefix_cache_lab.cli catalog-workbuddy
```

Select eight reproducible cases. Omit `--seed` to generate and record a random
seed:

```bash
python3 -m prefix_cache_lab.cli sample-workbuddy --seed 5603
```

Copy and redact only the selected generation inputs:

```bash
python3 -m prefix_cache_lab.cli materialize-workbuddy
```

Because WorkBuddy's recorded `totalTokens` may not match the tokenizer on NX3,
the authoritative long-context selection uses two stages:

```bash
python3 -m prefix_cache_lab.cli candidate-workbuddy \
  --seed 1575981708 \
  --count 48 \
  --min-input-chars 90000

python3 -m prefix_cache_lab.cli materialize-workbuddy \
  --selection private/candidate-selection.json \
  --output-dir private/candidate-cases

python3 -m prefix_cache_lab.cli profile-cases \
  --cases private/candidate-cases \
  --output private/candidate-profile.json

python3 -m prefix_cache_lab.cli select-profiled-cases
```

Private files are written below:

```text
private/catalog.json
private/selection.json
private/cases/
```

## Direct NX3 Validation

Preview the isolated text-only runtime:

```bash
python3 -m prefix_cache_lab.cli runtime-start --node nx3
```

Start it after reviewing the exact commands:

```bash
python3 -m prefix_cache_lab.cli runtime-start --node nx3 --apply
```

This temporarily replaces the production process without changing its systemd
unit. The text-only lab runtime listens on the isolated Tailscale test port
18081, removes the multimodal projector, uses a 57,344-token context, keeps the
known-good batch sizes, and disables the duplicate RAM prompt cache. The active
single slot remains the authoritative hot prefix without accepting Hermes
traffic from the production port 8081. Slot snapshots are stored under
`/home/nx/.local/state/prefix-cache-lab/slots`, never under `/tmp`.
The lab server reads its API key from
`/home/nx/.config/prefix-cache-lab/api-key`; the CLI retrieves it over SSH and
never writes the value to reports or Git.

For random validation across the full WorkBuddy size distribution:

```bash
python3 -m prefix_cache_lab.cli catalog-workbuddy \
  --output private/random-catalog.json
python3 -m prefix_cache_lab.cli random-validation-candidates \
  --catalog private/random-catalog.json \
  --output private/random-candidate-selection.json
python3 -m prefix_cache_lab.cli materialize-workbuddy \
  --selection private/random-candidate-selection.json \
  --output-dir private/random-candidate-cases
python3 -m prefix_cache_lab.cli profile-cases \
  --cases private/random-candidate-cases \
  --output private/random-candidate-profile.json
python3 -m prefix_cache_lab.cli select-random-validation \
  --profile private/random-candidate-profile.json \
  --output private/random-validation-selection.json
```

The final runtime uses `-b 2048 -ub 768`. The 768-token micro-batch is the
lowest cold-prefill penalty observed while keeping changed-suffix reuse above
95% for the approximately 18K-token validation cases.

Generate a model-tokenized 45K synthetic control:

```bash
python3 -m prefix_cache_lab.cli generate-synthetic
```

Record exact tokenizer counts for the selected real cases:

```bash
python3 -m prefix_cache_lab.cli profile-cases
```

```bash
python3 -m prefix_cache_lab.cli baseline --transport direct --node nx3
python3 -m prefix_cache_lab.cli verify --transport direct --node nx3 --runs 3
python3 -m prefix_cache_lab.cli interference-test --transport direct --node nx3
python3 -m prefix_cache_lab.cli output-isolation-test \
  --transport direct \
  --node nx3
```

Preview the approved NX3 stop/start operation:

```bash
python3 -m prefix_cache_lab.cli restart-test --transport direct --node nx3
```

Actually execute the restart test:

```bash
python3 -m prefix_cache_lab.cli restart-test \
  --transport direct \
  --node nx3 \
  --apply
```

The restart test warms one real prefix, repeats it with `n_predict=0` so the
saved slot ends exactly at the prompt boundary, saves slot 0, restarts only the
transient lab service, restores the slot snapshot, and then sends a request
with the same suffix followed by a different user suffix. The first request
proves the snapshot itself survived; the second proves whether a usable
context checkpoint survived. The report records saved/restored token and byte
counts plus save/restore timing.

Restore the original NX3 service:

```bash
python3 -m prefix_cache_lab.cli runtime-restore --node nx3 --apply
```

## AI Router Validation

Production Router mode requires:

- `AI_ROUTER_1PANEL_API_KEY`.
- The isolated model ID `prefix-lab/nx3-qwen36`.
- A physical deployment that resolves only to NX3 during the experiment.

```bash
python3 -m prefix_cache_lab.cli verify --transport router --node nx3 --runs 3
```

Router and direct results use the same case and suffix hashes, allowing the
report to estimate Router overhead without storing raw prompts.

Before production activation, run the repository Router code in an isolated
process:

```bash
python3 -m prefix_cache_lab.cli router-harness --port 14081
```

Then use the same benchmark CLI from another shell:

```bash
AI_ROUTER_1PANEL_API_KEY=prefix-cache-lab-key \
python3 -m prefix_cache_lab.cli verify \
  --transport router \
  --router-base-url http://127.0.0.1:14081 \
  --node nx3 \
  --reset-slot \
  --runs 2
```

The harness uses an in-memory Router state store, enables only the otherwise
disabled NX3 endpoint, and writes audit data under ignored `private/`.
`--reset-slot` is a lab-only control used to make the first request genuinely
cold.

## Report

```bash
python3 -m prefix_cache_lab.cli report \
  --output artifacts/summary.md
```

The report does not claim a pass unless native or Router usage metrics prove
the cached-token ratio.
