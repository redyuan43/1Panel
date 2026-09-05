from prefix_cache_lab.backend import CompletionResult
from prefix_cache_lab.benchmark import _request_record, summarize_records


def test_summary_separates_warm_and_cold_ttft() -> None:
    records = [
        {
            "phase": "warmup",
            "first_token_seconds": 100,
            "prefix_reuse_ratio": 0,
            "cache_passed": False,
        },
        {
            "phase": "warm-1",
            "first_token_seconds": 20,
            "prefix_reuse_ratio": 0.98,
            "cache_passed": True,
        },
        {
            "phase": "warm-2",
            "first_token_seconds": 10,
            "prefix_reuse_ratio": 0.99,
            "cache_passed": True,
        },
    ]
    summary = summarize_records(records)
    assert summary["cold_ttft_median_seconds"] == 100
    assert summary["warm_ttft_median_seconds"] == 15
    assert summary["ttft_reduction_ratio"] == 0.85
    assert summary["prefix_reuse_ratio_median"] == 0.985
    assert summary["cache_fail_count"] == 0
    assert summary["all_cache_records_passed"] is True


def test_summary_includes_post_restart() -> None:
    records = [
        {
            "phase": "warmup",
            "first_token_seconds": 120,
            "prefix_reuse_ratio": 0,
            "cache_passed": False,
        },
        {
            "phase": "post-restart-variant",
            "first_token_seconds": 5,
            "prefix_reuse_ratio": 0.97,
            "cache_passed": True,
        },
    ]
    summary = summarize_records(records)
    assert summary["warm_ttft_median_seconds"] == 5
    assert summary["prefix_reuse_ratio_median"] == 0.97
    assert summary["all_cache_records_passed"] is True


def test_request_record_does_not_store_output_excerpt() -> None:
    result = CompletionResult(
        transport="direct",
        first_token_seconds=1,
        total_seconds=2,
        prompt_tokens=100,
        cached_tokens=100,
        new_prefill_tokens=0,
        prefill_seconds=0,
        decode_tokens=1,
        decode_seconds=1,
        decode_tps=1,
        content="private model output",
        response_headers={},
        raw_usage=None,
    )
    record = _request_record(
        case={"case_id": "case", "prompt_sha256": "hash"},
        phase="warm-1",
        suffix="suffix",
        expected_prefix_tokens=100,
        result=result,
    )
    assert "content_excerpt" not in record
    assert record["content_sha256"]
    assert record["content_chars"] == len("private model output")


def test_request_record_proves_output_marker_without_storing_content() -> None:
    result = CompletionResult(
        transport="direct",
        first_token_seconds=1,
        total_seconds=2,
        prompt_tokens=100,
        cached_tokens=0,
        new_prefill_tokens=100,
        prefill_seconds=1,
        decode_tokens=2,
        decode_seconds=1,
        decode_tps=2,
        content="CASE_CURRENT",
        response_headers={},
        raw_usage=None,
    )
    record = _request_record(
        case={"case_id": "current", "prompt_sha256": "hash"},
        phase="output-isolation",
        suffix="suffix",
        expected_prefix_tokens=100,
        result=result,
        expected_output_marker="CASE_CURRENT",
        output_markers={
            "current": "CASE_CURRENT",
            "other": "CASE_OTHER",
        },
    )
    assert "content_excerpt" not in record
    assert record["output_sanity"]["expected_marker_present"] is True
    assert record["output_sanity"]["unexpected_marker_case_ids"] == []
    assert record["output_sanity"]["passed"] is True
