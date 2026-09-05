from prefix_cache_lab.report import render_markdown


def test_report_renders_acceptance_metrics() -> None:
    text = render_markdown(
        [
            {
                "mode": "verify",
                "transport": "direct",
                "case_count": 1,
                "summary": {
                    "cold_ttft_median_seconds": 100,
                    "warm_ttft_median_seconds": 10,
                    "ttft_reduction_ratio": 0.9,
                    "prefix_reuse_ratio_median": 0.98,
                },
                "records": [
                    {
                        "transport": "direct",
                        "prompt_sha256": "a",
                        "suffix_sha256": "b",
                        "first_token_seconds": 10,
                        "cache_passed": True,
                    }
                ],
            }
        ]
    )
    assert "90.0%" in text
    assert "98.0%" in text
    assert "Cache pass records: 1" in text
