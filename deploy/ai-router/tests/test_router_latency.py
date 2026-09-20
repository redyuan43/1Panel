"""Critical-path regressions; synthetic HTTP transports and temporary archives only."""
import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router import api, content_audit
from ai_router.config import Registry
from ai_router.content_audit import ArchiveReader, ContentObservation
from ai_router.phase_timing import PhaseTimingMiddleware, current_timings, phase
from ai_router.policy import RoutingPolicy
from ai_router.route_trace import DecisionTrace
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from tests.test_core import FakeHealth, SimpleTokenCounter, healthy, settings

ROOT = Path(__file__).resolve().parents[1]


def test_identical_snapshots_parse_once_and_preserve_history():
    body = {"messages": [{"role": "user", "content": "snapshot" * 10000}]}
    observer = ContentObservation()
    with patch.object(content_audit.json, "loads", wraps=json.loads) as loads:
        observer.capture("received", body, archive_body=False)
        for stage in ("normalized", "effective", "forwarded"):
            observer.capture(stage, body)
        assert loads.call_count == 1
    body["messages"][0]["content"] = "changed"
    assert len(observer.stages) == 4
    assert next(iter(observer.bodies.values()))["messages"][0]["content"] == "snapshot" * 10000


@pytest.mark.parametrize("per_request", [True, False])
def test_snapshot_skips_network_for_per_request_endpoints(per_request):
    endpoint = Registry(ROOT / "config/registry.yaml").by_id("ai-qwen38-27b")
    endpoint = replace(endpoint, metadata={**endpoint.metadata, "cache_usage": "per_request" if per_request else "legacy"})
    reader = AsyncMock(return_value={"queries": 10, "hits": 2})
    result = asyncio.run(api._prefix_cache_snapshot(SimpleNamespace(health=SimpleNamespace(prefix_cache_counters=reader)), SimpleNamespace(endpoint=endpoint)))
    assert reader.await_count == (0 if per_request else 1)
    assert result == (None if per_request else {"queries": 10, "hits": 2})


@pytest.mark.parametrize("per_request,cached,complete,expected_calls", [
    (True, 0, True, 0), (True, 75, True, 0), (True, None, True, 0),
    (False, 0, True, 0), (False, None, True, 1),
    (False, 101, True, 0), (False, None, False, 0),
])
def test_audit_network_fallback_is_only_for_missing_legacy_usage(per_request, cached, complete, expected_calls):
    endpoint = Registry(ROOT / "config/registry.yaml").by_id("ai-qwen38-27b")
    endpoint = replace(endpoint, metadata={**endpoint.metadata, "cache_usage": "per_request" if per_request else "legacy"})
    decision = api.RouteDecision(endpoint=endpoint, requested_model=endpoint.public_model, task="general",
        prompt_tokens=100, output_reserve_tokens=16, reason="explicit_model", affinity="explicit", score=1)
    decision.trace = DecisionTrace(request_id="synthetic", client_id="synthetic", key_id="synthetic",
        protocol="chat", requested_model="synthetic", excerpt={}, instance_id="test", boot_id="test",
        settings_hash="test", registry_hash="test")
    current = SimpleNamespace(instance_id="test", boot_id="test", settings=SimpleNamespace(section=lambda _: {}),
        audit=SimpleNamespace(write=Mock()), policy=SimpleNamespace(mark_deployment_recent=AsyncMock()),
        clients=SimpleNamespace(record_usage=AsyncMock()),
        health=SimpleNamespace(status=AsyncMock(return_value=SimpleNamespace(cache_generation="test"))),
        prefix_affinity=SimpleNamespace(invalidate_worker=AsyncMock(), record_worker=AsyncMock()))
    usage = {"prompt_tokens": 100, "completion_tokens": 10}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    with patch.object(api, "_prefix_cache_delta", new=AsyncMock(return_value=64)) as fallback, patch.object(api, "_save_request_trace", new=AsyncMock()):
        asyncio.run(api._audit(current, request_id="synthetic", client_id="synthetic", key_id="synthetic",
            conversation_id=None, decision=decision, status_code=200, started_at=time.monotonic(),
            usage=usage, usage_complete=complete, cache_snapshot={"queries": 0, "hits": 0}))
    assert fallback.await_count == expected_calls
    if complete and cached == 0:
        assert decision.actual_cached_tokens == 0


def test_phase_context_isolated_for_concurrent_requests_and_exceptions():
    async def run():
        captured = []
        async def app(scope, receive, send):
            with phase(scope["name"]):
                await asyncio.sleep(0)
            captured.append(current_timings())
            if scope["name"] == "failed":
                raise ValueError("original")
        middleware = PhaseTimingMiddleware(app)
        results = await asyncio.gather(*(middleware({"type": "http", "path": "/v1/chat/completions", "name": name}, None, None) for name in ("first", "failed")), return_exceptions=True)
        assert isinstance(results[1], ValueError)
        assert [set(x["stages"]) for x in captured] == [{"first"}, {"failed"}]
        assert current_timings() is None
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_reusable_context_durable_before_dispatch_with_one_less_archive_update(tmp_path, monkeypatch, stream):
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    key = tmp_path / "training.key"
    key.write_bytes(Fernet.generate_key())
    database = tmp_path / "training.sqlite3"
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv("AI_ROUTER_TRAINING_DB_PATH", str(database))
    monkeypatch.setenv("AI_ROUTER_TRAINING_KEY_PATH", str(key))
    registry = Registry(ROOT / "config/registry.yaml")
    endpoint = registry.by_id("ivan-qwen38-flash-128k")
    runtime = build_runtime(settings=settings(tmp_path), registry=registry, store=InMemoryStateStore(), token_counter=SimpleTokenCounter())
    runtime.health = FakeHealth({endpoint.id: healthy(endpoint.id, context=endpoint.safe_context_tokens)})
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)
    reader = ArchiveReader(database, key)
    seen = []
    def persistence(_current, body, **kwargs):
        value = copy.deepcopy(body)
        value["messages"] = [{"role": "user", "content": "reusable-context"}]
        return value
    async def upstream(request):
        archived = reader.read(request.headers["x-request-id"])
        assert archived["request"]["effective_body"]["messages"][0]["content"] == "reusable-context"
        assert len(archived["routing_attempts"]) == 1
        assert archived["pipeline"]["stages"][-1]["stage"] == "forwarded_1"
        seen.append(request.headers["x-request-id"])
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "answer"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with patch.object(api, "_history_body_for_persistence", side_effect=persistence), patch.object(runtime.training, "set_effective_context", wraps=runtime.training.set_effective_context) as prepared:
        with TestClient(api.create_app(runtime)) as client:
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer client-key"},
                json={"model": endpoint.public_model, "messages": [{"role": "user", "content": "original-input"}], "stream": stream, "max_tokens": 16})
        assert response.status_code == 200, response.text
        assert prepared.call_count == 1
    archived = reader.read(seen[0])
    assert archived["state"] == "completed"
    assert archived["request"]["effective_body"]["messages"][0]["content"] == "reusable-context"
    trace = asyncio.run(runtime.route_traces.get(seen[0]))
    stages = trace["observation"]["router_phase_timings"]["stages"]
    assert stages["archive_update"]["calls"] == 4
    assert stages["upstream_headers_wait"]["calls"] == 1
    assert stages["history_persist"]["calls"] == 1
