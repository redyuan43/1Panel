"""Full Router HTTP tests. All upstreams are synthetic MockTransport servers."""
import asyncio
from dataclasses import replace
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_router.api import create_app
from ai_router.config import Registry
from ai_router.errors import CapacityBusyError
from ai_router.policy import RoutingPolicy
from ai_router.prefill_admission import AdmissionPolicy, AttemptWindow, CAPABILITY
from test_client_route_binding import _runtime, ROOT, TARGET_ENDPOINT, TARGET_MODEL, route_binding_rules
from test_core import FakeHealth, healthy


def setup(tmp_path, monkeypatch, *, local_only=False, busy_idle=False, version=CAPABILITY,
          late_error=False, cloud=True):
    runtime, secrets, _ = _runtime(tmp_path, monkeypatch)
    first = runtime.registry.by_id(TARGET_ENDPOINT)
    idle = replace(first, id="aa-idle", public_model="test/idle", node="ivan",
                   api_base="http://idle/v1")
    remote = replace(Registry(ROOT / "config/registry.yaml").by_id("cloud-deepseek-v4-flash"),
                     enabled=True, api_base="http://cloud/v1")
    registry = runtime.registry.with_endpoints([first, idle, remote])
    runtime.registry = runtime.base_registry = runtime.endpoint_configs.base_registry = registry
    runtime.health = FakeHealth({e.id: healthy(e.id, context=e.safe_context_tokens)
                                 for e in registry.endpoints})
    runtime.settings.write_runtime({"identity": {"enabled": False},
        "routing": {"strategy": "legacy_v1", "client_route_bindings": route_binding_rules(),
                    "new_request_capacity_wait_seconds": 0,
                    "affinity_capacity_wait_seconds": 0, "all_local_busy_policy": "cloud_or_429"},
        "cloud": {"enabled": cloud, "auto_escalate": cloud, "monthly_budget": 5,
                  "allowed_providers": ["deepseek"], "allowed_models": [remote.public_model]}})
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health, store=runtime.store)
    original_choose = runtime.policy.choose
    async def choose(**kwargs):
        # Fix only the initial preference, retaining the complete real policy checks.
        if first.id not in kwargs.get("excluded_deployment_ids", set()):
            kwargs["excluded_endpoint_ids"] = set(kwargs.get("excluded_endpoint_ids", ())) | {idle.id, remote.id}
        return await original_choose(**kwargs)
    runtime.policy.choose = choose
    runtime.scheduler.admission = AdmissionPolicy({"enabled": True, "groups": {
        "v100": {"mode": "cache", "capacity": 6, "deployments": [first.id]},
        "idle": {"mode": "idle", "capacity": 1, "deployments": [idle.id]}}})
    models = {"auto", first.public_model, idle.public_model, remote.public_model}
    asyncio.run(runtime.clients.create_account({"id": "admission", "name": "admission",
        "enabled": True, "models": sorted(models), "rpm_limit": 120, "tpm_limit": 1000000,
        "max_parallel_requests": 8, "disclosure_mode": "internal", "local_only": local_only},
        allowed_models=models))
    _, secrets["admission"] = asyncio.run(runtime.clients.create_key("admission", "test"))
    requests = []
    successful = runtime.internal_client._transport
    async def upstream(request):
        if request.url.path.endswith("/_prefill_admission"):
            return httpx.Response(200, json={"capability": version, "enabled": True,
                "service_group": "v100", "max_concurrency": 6, "short_prefill_tokens": 1600,
                "lookup_protocol": "lookup-admission-v1", "cleanup_healthy": True})
        if request.url.host == "litellm":
            assert json.loads(request.content)["model"] == remote.id
            requests.append("cloud")
        else:
            requests.append(request.url.host)
        if request.url.host == "upstream":
            if late_error:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                    'data: {"choices":[{"index":0,"delta":{"content":"synthetic"},"finish_reason":null}]}\n\n'
                    'data: {"error":{"code":"prefill_admission_busy"}}\n\n')
            return httpx.Response(409, headers={"X-Prefill-Admission": CAPABILITY},
                                  json={"error": {"code": "prefill_admission_busy"}})
        return await successful.handle_async_request(request)
    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), timeout=5)
    holder = None
    if busy_idle:
        holder = asyncio.run(runtime.scheduler.begin_request(None))
        asyncio.run(runtime.scheduler.try_acquire_deployment_candidates(holder, (idle.id,), capacity=6))
    return runtime, secrets, requests, holder


def post(client, key, protocol, stream, model="auto"):
    body = {"model": model, "stream": stream, "max_output_tokens" if protocol == "responses" else "max_tokens": 16}
    body.update({"input": "synthetic admission test"} if protocol == "responses" else
                {"messages": [{"role": "user", "content": "synthetic admission test"}]})
    return client.post("/v1/" + ("responses" if protocol == "responses" else "chat/completions"),
                       headers={"Authorization": "Bearer " + key}, json=body)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_refusal_reselects_local_before_cloud(tmp_path, monkeypatch, protocol, stream):
    runtime, keys, requests, _ = setup(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = post(client, keys["admission"], protocol, stream)
        assert response.status_code == 200, response.text
        assert requests == ["upstream", "idle"]
        assert "prefill_admission_busy" not in response.text
        assert "x-prefill-admission" not in response.headers
    assert runtime.health.failed == []
    assert not any(runtime.store._semaphores.values())


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_fixed_target_never_spills(tmp_path, monkeypatch, protocol, stream, bound):
    runtime, keys, requests, _ = setup(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = post(client, keys["home-assistant" if bound else "admission"], protocol, stream,
                        model="auto" if bound else TARGET_MODEL)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "model_capacity_busy"
    assert requests == ["upstream"]
    assert runtime.health.failed == []
    assert not any(runtime.store._semaphores.values())


@pytest.mark.parametrize("local_only", [False, True])
def test_busy_local_cloud_fallback_respects_privacy(tmp_path, monkeypatch, local_only):
    runtime, keys, requests, holder = setup(tmp_path, monkeypatch, busy_idle=True, local_only=local_only)
    try:
        with TestClient(create_app(runtime)) as client:
            response = post(client, keys["admission"], "chat", False)
        assert response.status_code == (429 if local_only else 200), response.text
        assert requests == (["upstream"] if local_only else ["upstream", "cloud"])
        assert runtime.health.failed == []
    finally:
        asyncio.run(holder.release())
    assert not any(runtime.store._semaphores.values())


def test_mismatched_backend_cannot_enable_router(tmp_path, monkeypatch):
    runtime, _, requests, _ = setup(tmp_path, monkeypatch, version="old")
    with pytest.raises(ValueError, match="capability mismatch"):
        with TestClient(create_app(runtime)):
            pass
    assert not requests
    asyncio.run(runtime.close())


def test_no_switch_after_output(tmp_path, monkeypatch):
    runtime, keys, requests, _ = setup(tmp_path, monkeypatch, late_error=True)
    with TestClient(create_app(runtime)) as client:
        response = post(client, keys["admission"], "chat", True)
    assert response.status_code == 200
    assert "synthetic" in response.text
    assert requests == ["upstream"]


def test_attempt_window_is_shared_and_aliases_are_attempted_once():
    policy = AdmissionPolicy({"enabled": True, "groups": {
        "gpu": {"mode": "cache", "capacity": 6, "deployments": ["a", "b"]}}})
    window = AttemptWindow(policy, 1)
    excluded = set()
    window.dispatch("a", excluded)
    assert excluded == {"a", "b"}
    with pytest.raises(CapacityBusyError):
        window.dispatch("b", excluded)
    window.deadline = time.monotonic() - 1
    with pytest.raises(CapacityBusyError):
        window.remaining()
    async def check():
        window.deadline = time.monotonic() + .01
        with pytest.raises(CapacityBusyError):
            await window.run(asyncio.sleep(1))
    asyncio.run(check())
