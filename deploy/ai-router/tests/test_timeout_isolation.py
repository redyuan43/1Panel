"""A slow request must not put unrelated conversations into endpoint cooldown."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import _exclude_failed_decision, create_app
from ai_router.config import Registry
from ai_router.policy import RoutingPolicy
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import RouteDecision
from test_core import FakeHealth, ROOT, healthy, settings


@pytest.mark.parametrize("error_type,cooled", [
    (httpx.ReadTimeout, False),
    (httpx.ConnectTimeout, True),
    (httpx.ConnectError, True),
])
def test_timeout_does_not_block_another_conversation(tmp_path, monkeypatch, error_type, cooled):
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AI_ROUTER_ROUTE_TRACE_DB_PATH", str(tmp_path / "traces.sqlite3"))
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    registry = Registry(ROOT / "config/registry.yaml")
    endpoint = replace(registry.by_id("ai-qwen38-27b"), metadata={})
    registry = registry.with_endpoints([endpoint])
    runtime = build_runtime(settings=settings(tmp_path), registry=registry,
                            store=InMemoryStateStore(), token_counter=SimpleTokenCounter())

    class CooldownHealth(FakeHealth):
        async def in_cooldown(self, endpoint_id):
            return endpoint_id in self.failed

    health = CooldownHealth({endpoint.id: healthy(endpoint.id, context=endpoint.safe_context_tokens)})
    runtime.health = health
    runtime.policy = RoutingPolicy(registry, runtime.settings, health)
    calls = []

    async def upstream(request):
        calls.append(request)
        if len(calls) == 1:
            raise error_type("synthetic failure", request=request)
        return httpx.Response(200, json={
            "id": "chatcmpl-other-conversation", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
        })

    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(runtime)) as client:
        def send(conversation):
            return client.post("/v1/chat/completions", headers={
                "Authorization": "Bearer client-key", "x-1panel-conversation-id": conversation,
            }, json={"model": endpoint.public_model,
                     "messages": [{"role": "user", "content": conversation}], "max_tokens": 16})
        first = send("slow-conversation")
        assert first.status_code == 503
        # The failed target remains excluded for this request: no blind retry.
        assert len(calls) == 1
        assert bool(health.failed) is cooled
        second = send("independent-conversation")
        assert second.status_code == (503 if cooled else 200)
        assert len(calls) == (1 if cooled else 2)
    asyncio.run(runtime.internal_client.aclose())


@pytest.mark.parametrize("backend,pool_size", [("vllm", 1), ("ai_pool", 2),
                                                ("codex_pool", 1), ("codex_pool", 2)])
@pytest.mark.parametrize("apply_cooldown", [False, True])
def test_request_exclusion_is_preserved_for_worker_pools(backend, pool_size, apply_cooldown):
    endpoint = replace(Registry(ROOT / "config/registry.yaml").by_id("ai-qwen38-27b"),
                       backend_type=backend)
    decision = RouteDecision(endpoint=endpoint, requested_model=endpoint.public_model,
                             task="general", prompt_tokens=8, output_reserve_tokens=16,
                             reason="test", affinity="explicit", score=1, native_or_adapter="native",
                             deployment_id="worker-0", upstream_api_base="http://worker/v1",
                             deployment_candidates=tuple((f"worker-{i}", f"http://worker-{i}/v1")
                                                         for i in range(pool_size)))
    current = SimpleNamespace(settings=SimpleNamespace(section=lambda _: {"cooldown_seconds": 20}),
                              health=SimpleNamespace(mark_failure=AsyncMock()))
    endpoints, deployments = set(), set()
    asyncio.run(_exclude_failed_decision(current, decision, endpoints, deployments,
                                        apply_cooldown=apply_cooldown))
    worker_only = backend == "ai_pool" or (backend == "codex_pool" and pool_size > 1)
    assert endpoints == (set() if worker_only else {endpoint.id})
    assert deployments == ({"worker-0"} if worker_only else set())
    if apply_cooldown:
        current.health.mark_failure.assert_awaited_once_with(
            "worker-0" if backend in {"ai_pool", "codex_pool"} else endpoint.id, 20)
    else:
        current.health.mark_failure.assert_not_awaited()
