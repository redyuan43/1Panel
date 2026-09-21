"""Recorded token evidence and bounded status refresh through the real management API."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from ai_router.auth import AuthManager
from ai_router.control import create_app
from ai_router.route_trace import DecisionTrace, RouteTraceStore
from ai_router.trace_summary import token_summary


def trace(request_id="request-a", started_at=100):
    value = DecisionTrace(request_id=request_id, client_id="synthetic", key_id="key", protocol="chat",
        requested_model="auto", excerpt={}, instance_id="router", boot_id="boot",
        settings_hash="settings", registry_hash="registry")
    value.set_request_context(conversation_id="conversation-a", prompt_tokens=386167,
                              output_reserve_tokens=65536)
    value.set_selection(attempt=1, selected_model="synthetic/model", endpoint_id="endpoint-a",
                        deployment_id="deployment-a", task="code", reason="selected", affinity="new",
                        context_required=253042)
    value.record(1, "candidate_scope", "passed", reason="candidates", evidence={"candidates": [
        {"endpoint_id": "endpoint-a", "safe_context_tokens": 262144},
        {"endpoint_id": "unselected", "safe_context_tokens": 8192},
    ]})
    value.record(1, "deployment_binding", "selected", reason="deployment_selected", evidence={
        "endpoint_id": "endpoint-a", "deployment_id": "deployment-a", "safe_context_tokens": 270000})
    value.payload["token_counting"] = {"selected": {"tokens": 187506, "exact": True, "source": "rendered"}}
    value.payload["started_at"] = started_at
    return value


def test_original_estimate_target_count_and_usage_are_distinct():
    value = trace()
    running = token_summary(value.payload)
    assert running == {
        "ingress_estimated_input_tokens": 386167, "target_input_tokens": 187506,
        "target_count_source": "rendered", "target_count_exact": True,
        "measured_input_tokens": None, "measured_output_tokens": None,
        "output_reserve_tokens": 65536, "required_context_tokens": 253042, "safe_context_tokens": 262144,
    }
    value.finish(attempt=1, status_code=200, evidence={"input_tokens": 187506, "output_tokens": 265,
        "backend_usage": {"state": "complete", "input_tokens": 187506, "cached_tokens": 0},
        "output_tokens_measured": True})
    final = token_summary(value.payload)
    assert final["measured_input_tokens"] == 187506
    assert final["measured_output_tokens"] == 265
    assert final["ingress_estimated_input_tokens"] == 386167


def test_window_matches_final_deployment_or_stays_unknown():
    value = trace()
    value.payload["attempts"][-1]["selection"]["deployment_id"] = "deployment-b"
    assert token_summary(value.payload)["safe_context_tokens"] is None
    value.record(1, "deployment_binding", "selected", reason="deployment_finalized", evidence={
        "endpoint_id": "endpoint-a", "deployment_id": "deployment-b", "safe_context_tokens": 131072})
    assert token_summary(value.payload)["safe_context_tokens"] == 131072
    value.record(1, "deployment_binding", "selected", reason="deployment_selected", evidence={
        "endpoint_id": "endpoint-a", "deployment_id": "wrong-deployment", "safe_context_tokens": 8192})
    assert token_summary(value.payload)["safe_context_tokens"] == 131072


def test_finalized_window_is_not_overridden_by_older_candidate_snapshot():
    value = trace()
    value.record(1, "candidate_scope", "passed", reason="candidates", evidence={"candidates": [
        {"endpoint_id": "endpoint-a", "safe_context_tokens": 8192}]})
    value.record(1, "deployment_binding", "selected", reason="deployment_finalized", evidence={
        "endpoint_id": "endpoint-a", "deployment_id": "deployment-a", "safe_context_tokens": 262144})
    assert token_summary(value.payload)["safe_context_tokens"] == 262144


def test_missing_or_invalid_usage_does_not_become_measured_zero():
    for usage in ({}, {"state": "invalid", "input_tokens": 0}):
        value = trace()
        value.finish(attempt=1, status_code=200, evidence={"input_tokens": 0, "output_tokens": 0,
            "backend_usage": usage, "output_tokens_measured": False})
        summary = token_summary(value.payload)
        assert summary["measured_input_tokens"] is None
        assert summary["measured_output_tokens"] is None
    value = trace()
    value.finish(attempt=1, status_code=200, evidence={"backend_usage": {"state": "missing", "input_tokens": 0},
        "output_tokens": 0, "output_tokens_measured": True})
    summary = token_summary(value.payload)
    assert summary["measured_input_tokens"] == 0
    assert summary["measured_output_tokens"] == 0


def test_historical_window_and_usage_are_never_borrowed_from_another_attempt():
    value = trace()
    value.record(1, "upstream_request", "error", reason="upstream_response", evidence={
        "backend_usage": {"state": "complete", "input_tokens": 100000},
        "output_tokens": 50, "output_tokens_measured": True})
    value.set_selection(attempt=2, selected_model="synthetic/other", endpoint_id="endpoint-b",
        deployment_id=None, task="code", reason="retry", affinity="new", context_required=66536)
    value.payload["token_counting"]["candidates"] = {"endpoint-b": {"tokens": 1000, "exact": False, "source": "estimated"}}
    summary = token_summary(value.payload)
    assert summary["target_input_tokens"] == 1000
    assert summary["target_count_exact"] is False
    assert summary["target_count_source"] == "estimated"
    assert summary["safe_context_tokens"] is None
    assert summary["measured_input_tokens"] is None
    assert summary["measured_output_tokens"] is None
    value.record(3, "candidate_scope", "error", reason="unavailable", evidence={})
    assert token_summary(value.payload)["target_input_tokens"] is None


def test_legacy_trace_without_evidence_remains_unknown():
    summary = token_summary({"request": {"prompt_tokens": 123}, "input_tokens": 0, "output_tokens": 0})
    assert summary["ingress_estimated_input_tokens"] == 123
    assert all(value is None for key, value in summary.items() if key != "ingress_estimated_input_tokens")


def test_stale_count_metadata_does_not_claim_exact_count_for_new_target():
    value = trace()
    value.payload["token_counting"]["selected"]["tokens"] = 100
    summary = token_summary(value.payload)
    assert summary["target_input_tokens"] == 187506
    assert summary["target_count_exact"] is None
    assert summary["target_count_source"] is None


def test_batch_refresh_includes_old_running_rows_and_respects_filters(tmp_path):
    store = RouteTraceStore(tmp_path / "traces.sqlite3")
    old = trace("old-running", 1)
    newer = trace("new-request", 2)
    asyncio.run(store.save(old))
    asyncio.run(store.save(newer))
    page = asyncio.run(store.list(request_mode="all", limit=1))
    assert [row["request_id"] for row in page["items"]] == ["new-request"]
    assert page["next_cursor"]
    old.finish(attempt=1, status_code=200)
    asyncio.run(store.save(old))
    batch = asyncio.run(store.list(request_mode="all", request_ids=("old-running", "missing", "old-running")))
    assert [row["request_id"] for row in batch["items"]] == ["old-running"]
    assert batch["items"][0]["status"] == "succeeded"
    assert batch["items"][0]["token_summary"]["safe_context_tokens"] == 262144
    assert batch["next_cursor"] is None
    assert asyncio.run(store.list(request_ids=("old-running",), client_id="different"))["items"] == []
    # SQL-like input is just a request ID.
    assert asyncio.run(store.list(request_ids=("' OR 1=1 --",)))["items"] == []
    for options in ({"request_ids": ()}, {"request_ids": ("x",) * 101},
                    {"request_ids": ("old-running",), "cursor": page["next_cursor"]}):
        with pytest.raises(ValueError):
            asyncio.run(store.list(**options))


def test_batch_api_requires_admin_and_has_bounded_input(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "synthetic-admin-key")
    store = RouteTraceStore(tmp_path / "traces.sqlite3")
    value = trace()
    asyncio.run(store.save(value))
    original = deepcopy(value.payload)
    runtime = SimpleNamespace(start=AsyncMock(), auth=AuthManager(None, None), route_traces=store,
        registry=SimpleNamespace(endpoints=()), settings=SimpleNamespace(section=lambda name: {}))
    with TestClient(create_app(runtime)) as client:
        auth = {"Authorization": "Bearer synthetic-admin-key"}
        query = [("request_ids", "request-a"), ("request_mode", "all")]
        assert client.get("/api/route-traces", params=query).status_code == 401
        response = client.get("/api/route-traces", headers=auth, params=query)
        assert response.status_code == 200
        row = response.json()["items"][0]
        assert row["prompt_tokens"] == 386167, "The existing API field is retained"
        assert row["token_summary"]["target_input_tokens"] == 187506
        detail = client.get("/api/route-traces/request-a", headers=auth)
        assert detail.status_code == 200
        assert detail.json()["trace"]["token_summary"] == row["token_summary"]
        for invalid in ([("request_ids", "x")] * 101, [("request_ids", "")],
                        [("request_ids", "x" * 129)], query + [("cursor", "stale")]):
            response = client.get("/api/route-traces", headers=auth, params=invalid)
            assert response.status_code == 400
        valid_max = client.get("/api/route-traces", headers=auth,
                               params=[("request_ids", f"id-{i}") for i in range(100)])
        assert valid_max.status_code == 200
    assert asyncio.run(store.get("request-a"))["request"] == original["request"]
