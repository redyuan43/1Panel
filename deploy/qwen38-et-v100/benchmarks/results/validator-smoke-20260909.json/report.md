# Qwen3.8 V100 Validation Report

- Generated: `2026-09-09T16:44:30+0800`
- Profile: `smoke`
- Model: `siyuan/qwen38-v100-196k`
- Base URL: `http://127.0.0.1:18107/v1`
- Overall: **FAIL**

## Request Summary

| Case | Result | Prompt | Completion | TTFT (s) | Decode (tok/s) | Wall (s) |
|---|---:|---:|---:|---:|---:|---:|
| startup | PASS | - | - | - | - | - |
| short-smoke | PASS | 35 | 3 | 0.211 | - | 0.258 |
| prefix-2000-cold | PASS | 2008 | 3 | 2.067 | - | 2.112 |
| prefix-2000-warm | PASS | 2008 | 3 | 0.550 | - | 0.596 |
| prefix-2000-summary | FAIL | - | - | - | - | - |
