# Terra anonymous pilot judge contract

Read `manifest.json` and `anonymous-results.json`. Do not request or infer the
identity of candidates A, B, C, D, or E. Return one JSON object only.

First reject or flag cases that are ambiguous, leak their expected answer, have
an invalid rubric, or cannot be judged from the supplied material. Deterministic
`oracle_passed` values are evidence, but inspect the answer for evaluator errors.

Required output:

```json
{
  "judge_model": "gpt-5.6-terra",
  "manifest_hash": "copied from the inputs",
  "confidence": 0.0,
  "case_reviews": [
    {
      "case_id": "case-id",
      "candidate": "A",
      "score": 0,
      "valid_case": true,
      "reason": "short reason"
    }
  ],
  "candidate_scores": {
    "A": {"general": 0, "code": 0, "batch": 0, "long-context": 0},
    "B": {"general": 0, "code": 0, "batch": 0, "long-context": 0},
    "C": {"general": 0, "code": 0, "batch": 0, "long-context": 0},
    "D": {"general": 0, "code": 0, "batch": 0, "long-context": 0},
    "E": {"general": 0, "code": 0, "batch": 0, "long-context": 0}
  },
  "routing_recommendation": {
    "general": ["A", "B", "C", "D", "E"],
    "code": ["A", "B", "C", "D", "E"],
    "batch": ["A", "B", "C", "D", "E"],
    "long-context": ["A", "B", "C", "D", "E"]
  },
  "judge_notes": []
}
```

Scores are provisional because this is a 20-case pilot. Do not recommend writing
them directly into production routing configuration.
