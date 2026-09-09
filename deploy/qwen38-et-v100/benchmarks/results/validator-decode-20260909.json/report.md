# Qwen3.8 V100 Validation Report

- Generated: `2026-09-09T16:50:55+0800`
- Profile: `decode`
- Model: `siyuan/qwen38-v100-196k`
- Base URL: `http://127.0.0.1:18107/v1`
- Overall: **FAIL**

## Request Summary

| Case | Result | Prompt | Completion | TTFT (s) | Decode (tok/s) | Wall (s) |
|---|---:|---:|---:|---:|---:|---:|
| startup | PASS | - | - | - | - | - |
| decode-summary-24000 | FAIL | - | - | - | - | - |

## Decode Detail: `decode-summary-24000`

| Stream | Result | Prompt | Completion | TTFT (s) | Decode (tok/s) | Wall (s) |
|---|---:|---:|---:|---:|---:|---:|
| single | PASS | 24021 | 256 | 2.221 | 58.553 | 6.576 |
| decode-concurrent-0 | PASS | 24027 | 256 | 7.134 | 40.267 | 13.467 |
| decode-concurrent-1 | PASS | 24027 | 256 | 7.136 | 39.855 | 13.534 |
| decode-concurrent-2 | PASS | 24027 | 256 | 2.326 | 23.189 | 13.322 |

- Concurrent wall time: `13.549 s`
- Aggregate decode throughput: `68.246 tok/s`

| APC phase | Query tokens | Hit tokens | Hit ratio |
|---|---:|---:|---:|
| cache_prime | 24012.000 | 0.000 | 0.000 |
| cache_single | 24021.000 | 20800.000 | 0.866 |
| cache_concurrent | 72081.000 | 62400.000 | 0.866 |
