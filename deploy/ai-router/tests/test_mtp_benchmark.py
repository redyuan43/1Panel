from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import time

import httpx
import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark-cerebellum-mtp.py"
SPEC = importlib.util.spec_from_file_location("mtp_benchmark", PATH)
assert SPEC and SPEC.loader
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def timings():
    return {"stop": True, "tokens_cached": 99999, "timings": {
        "cache_n": 4092, "prompt_n": 4, "prompt_ms": 200,
        "predicted_n": 256, "predicted_ms": 10000,
        "draft_n": 200, "draft_n_accepted": 100,
    }}


def test_native_cache_counter_is_not_total_cached_sequence_length():
    value = bench.summarize_timings(timings(), 4096, 4090, 2)
    assert value["cached_tokens"] == 4092
    assert value["new_prefill_tokens"] == 4
    assert value["decode_tps"] == 25.6
    assert value["acceptance_rate"] == 0.5
    assert value["cache_passed"]


@pytest.mark.parametrize("field,value", [
    ("prompt_n", 1000), ("predicted_n", 3), ("predicted_ms", 0),
    ("predicted_ms", float("nan")), ("prompt_ms", float("inf")),
    ("draft_n", -1), ("draft_n_accepted", 201),
])
def test_invalid_or_incomparable_timings_fail(field, value):
    final = timings()
    final["timings"][field] = value
    with pytest.raises(ValueError):
        bench.summarize_timings(final, 4096, 4090, 2)


def test_missing_timings_and_unverified_mtp_are_rejected():
    with pytest.raises(ValueError, match="missing"):
        bench.summarize_timings({}, 4096, 4090, 2)
    final = timings()
    final["timings"].pop("draft_n")
    final["timings"].pop("draft_n_accepted")
    with pytest.raises(ValueError, match="no speculative"):
        bench.summarize_timings(final, 4096, 4090, 2)
    with pytest.raises(ValueError, match="unexpectedly"):
        bench.summarize_timings(timings(), 4096, 4090, 0)


def test_budget_and_url_guards():
    bench.check_budget(260000)
    with pytest.raises(ValueError):
        bench.check_budget(262000)
    assert bench.validate_url("http://127.0.0.1:18081/") == "http://127.0.0.1:18081"
    for url in ("https://example.com", "http://user:secret@localhost:4000", "http://localhost/v1", "http://localhost/?key=x"):
        with pytest.raises(ValueError):
            bench.validate_url(url)


def test_prefix_accounting_and_cold_vs_warm_requests():
    assert bench.common_prefix([1, 2, 3], [1, 2, 7]) == 2
    assert bench.common_prefix([], [1]) == 0
    assert not bench.completion_body("hello", 3, 1)["cache_prompt"]
    assert bench.completion_body("hello again", 3, 2)["cache_prompt"]
    assert bench.completion_body("hello", 3, 1)["ignore_eos"]


def paired_reports():
    baseline, candidate = [], []
    for group in range(3):
        base = {
            "passed": True, "mode": "record", "spec_depth": 0,
            "fixture_sha256": f"fixture-{group}",
            "turns": [{
                "request_sha256": f"request-{group}-{turn}", "passed": True,
                "input_tokens": 260000, "decode_tps": 10, "total_seconds": 30,
            } for turn in range(4)],
        }
        test = copy.deepcopy(base)
        test.update({"mode": "replay", "spec_depth": 2})
        for turn in test["turns"]:
            turn.update({"decode_tps": 13, "total_seconds": 24})
        baseline.append(base)
        candidate.append(test)
    return baseline, candidate


def test_performance_gate_does_not_approve_deployment():
    result = bench.compare(*paired_reports())
    assert result["performance_gate_passed"]
    assert result["median_decode_speedup"] == 1.3
    assert result["median_total_time_ratio"] == 0.8
    assert not result["deployment_approved"]


def test_depth_one_uses_the_same_comparison_gates():
    base, candidate = paired_reports()
    for report in candidate:
        report["spec_depth"] = 1
    result = bench.compare(base, candidate)
    assert result["valid"]
    assert result["performance_gate_passed"]
    assert not result["deployment_approved"]


def test_non_speculative_parameter_replays_use_the_same_comparison_gates():
    base, candidate = paired_reports()
    for report in candidate:
        report["spec_depth"] = 0
    result = bench.compare(base, candidate)
    assert result["valid"]
    assert not result["deployment_approved"]


def test_incomplete_or_different_inputs_cannot_claim_success():
    base, test = paired_reports()
    assert not bench.compare(base[:1], test[:1])["performance_gate_passed"]
    test[0]["turns"][1]["request_sha256"] = "changed"
    with pytest.raises(ValueError, match="not identical"):
        bench.compare(base, test)
    base, test = paired_reports()
    test[0]["mode"] = "natural"
    with pytest.raises(ValueError):
        bench.compare(base, test)
    base, test = paired_reports()
    test[0]["passed"] = False
    assert not bench.compare(base, test)["performance_gate_passed"]


def test_stream_reads_native_final_event_and_records_request_id(tmp_path):
    async def scenario():
        async def handler(request):
            assert request.headers["x-request-id"] == "full-test-request-id"
            text = 'data: {"content":"hello","stop":false}\n\n'
            text += "data: " + json.dumps(timings()) + "\n\n"
            return httpx.Response(200, text=text, headers={"x-request-id": "server-id"})

        async with httpx.AsyncClient(
            base_url="http://localhost", transport=httpx.MockTransport(handler),
        ) as client:
            result = await bench.stream_completion(
                client, {}, tmp_path / "response.sse", "full-test-request-id",
            )
        assert result["content"] == "hello"
        assert result["response_request_id"] == "server-id"
        assert result["final"]["stop"]

    asyncio.run(scenario())


def test_first_token_deadline_also_covers_response_headers(tmp_path):
    async def scenario():
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200)

        async with httpx.AsyncClient(
            base_url="http://localhost", transport=httpx.MockTransport(handler),
        ) as client:
            started = time.monotonic()
            with pytest.raises(asyncio.TimeoutError):
                await bench.stream_completion(
                    client, {}, tmp_path / "response.sse", "deadline-test",
                    first_deadline=0.01, total_deadline=2,
                )
            assert time.monotonic() - started < 0.5

    asyncio.run(scenario())


def test_backend_error_is_not_a_successful_http_response(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://localhost",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='data: {"error":"failed"}\n\n')
            ),
        ) as client:
            with pytest.raises(ValueError, match="backend error"):
                await bench.stream_completion(client, {}, tmp_path / "error.sse", "error-test")

    asyncio.run(scenario())
