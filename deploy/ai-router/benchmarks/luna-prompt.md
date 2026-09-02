# Luna pilot manifest contract

Generate one JSON object only. Do not call any model endpoint and do not include
model names or model outputs.

Required top-level fields:

- `benchmark_version`: `2`
- `run_type`: `"pilot"`
- `generated_by`: `"gpt-5.6-luna"`
- `seed`: the seed supplied by the coordinator
- `cases`: exactly 20 unique cases

Required task distribution:

- 6 `general`
- 5 `code`
- 5 `batch`
- 4 `long-context`

Each case requires `id`, `task`, `max_tokens`, and `grading`. Grading is either:

```json
{
  "mode": "json_subset",
  "expected": {"answer": "value"},
  "rubric": "What constitutes a correct answer."
}
```

or:

```json
{
  "mode": "terra",
  "rubric": "A self-contained semantic scoring rubric."
}
```

Normal cases require a `messages` array. Long-context cases must not contain the
expanded document. They require:

```json
{
  "long_context": {
    "target_tokens": 70000,
    "needle": {"key": "random-key", "value": "random-value"},
    "question": "Return JSON containing the authoritative value.",
    "follow_up": {
      "question": "Optional cache follow-up question.",
      "grading": {
        "mode": "json_subset",
        "expected": {"answer": "random-value"},
        "rubric": "Correctly recalls the same authoritative value."
      }
    }
  }
}
```

Use targets near 70000, 96000, and 120000 tokens. Exactly one long-context case
must include `follow_up`. Avoid trivia that depends on current events, ambiguous
prompts, copyrighted passages, unsafe content, duplicated answers, or clues that
identify any tested model.
