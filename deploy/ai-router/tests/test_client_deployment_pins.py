from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import _capacity_wait_seconds, create_app
from ai_router.config import Registry, Settings
from ai_router.errors import NoCompatibleModelError, NoEligibleModelError, QueueTimeoutError
from ai_router.policy import RoutingPolicy
from ai_router.prefix_affinity import PrefixAffinityLocation, PrefixAffinityRecord
from ai_router.runtime import build_runtime
from ai_router.scheduler import Scheduler
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import ConversationState, EndpointStatus, Evaluation

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "workbuddy-qwen36-shared"
MODEL = "siyuan/qwen36-shared"
POOL = "qwen36-shared-fleet"
NX3 = "qwen36-nx3"
NX4 = "qwen36-nx4"


class FakeHealth:
    def __init__(self, workers):
        self.value = EndpointStatus(
            endpoint_id=POOL, healthy=True, checked_at=time.time(),
            eligible_context_tokens=255000, detail={"workers": workers},
        )

    async def statuses(self, endpoints, **kwargs):
        return {item.id: self.value for item in endpoints}

    async def status(self, endpoint, **kwargs):
        return self.value

    async def in_cooldown(self, deployment_id):
        return False

    async def in_capability_cooldown(self, deployment_id, capability):
        return False

    async def mark_failure(self, deployment_id, seconds):
        pass

    async def prefix_cache_counters(self, endpoint):
        return None


def worker(worker_id, state="available"):
    return {
        "worker_id": worker_id, "api_base": f"http://{worker_id}/v1",
        "profile_id": "nx-q4-vision", "tier": "edge-small", "priority": 10, "port": 8081,
        "context_size": 57344, "safe_context_tokens": 57344,
        "cache_type_k": "q4_0", "cache_type_v": "q4_0",
        "modalities": ["text", "image"], "vision_status": "validated",
        "max_images": 1, "runtime_fingerprint": "generation-1",
        "cache_generation": "generation-1", "ready": True,
        "state": state, "config_drift": [], "short_request_rank": 0,
    }


@pytest.fixture
def environment(tmp_path):
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "settings.yaml")
    registry = Registry(ROOT / "config/registry.yaml")
    health = FakeHealth([worker(NX3), worker(NX4)])
    store = InMemoryStateStore()
    policy = RoutingPolicy(registry, settings, health, store=store)
    return settings, registry, health, store, policy


async def choose(policy, **overrides):
    args = dict(
        client_id=CLIENT, requested_model=MODEL,
        evaluation=Evaluation("general", None, 1.0, "test"),
        prompt_tokens=46125, output_reserve_tokens=4096,
        modalities={"text"}, has_tools=False, conversation=None,
    )
    args.update(overrides)
    return await policy.choose(**args)


@pytest.mark.parametrize("state", ["available", "busy", "leased"])
def test_pin_without_prefix_record_keeps_only_nx3(environment, state):
    settings, registry, health, _, policy = environment
    health.value.detail["workers"][0]["state"] = state
    before = copy.deepcopy(registry.by_id(POOL).metadata)
    decision = asyncio.run(choose(policy))
    assert decision.deployment_candidates == ((NX3, "http://qwen36-nx3/v1"),)
    assert decision.affinity == "client-pinned"
    assert _capacity_wait_seconds(settings.section("routing"), requested_model=MODEL, decision=decision) == 900
    assert registry.by_id(POOL).metadata == before
    assert "client_deployment_pin" not in registry.by_id(POOL).metadata


@pytest.mark.parametrize("warm_worker", [NX3, NX4])
def test_pin_overrides_prefix_replica_and_old_nx4_conversation(environment, warm_worker):
    _, _, health, _, policy = environment
    health.value.detail["workers"][0]["state"] = "busy"
    affinity = PrefixAffinityRecord(locations=(PrefixAffinityLocation(
        endpoint_id=POOL, deployment_id=warm_worker, cache_generation="generation-1",
        warmed_at=time.time(), last_hit_at=time.time(), matched_tokens=33028,
        prefix_tokens=33028, match_type="exact",
    ),), updated_at=time.time())
    for conversation in [None, ConversationState(
        conversation_id="old-nx4", public_model=MODEL, endpoint_id=POOL,
        tier_rank=20, task="general", last_seen=time.time(), deployment_id=NX4,
    )]:
        decision = asyncio.run(choose(policy, conversation=conversation, prefix_affinity=affinity))
        assert tuple(dict(decision.deployment_candidates)) == (NX3,)
        assert decision.deployment_id == NX3


@pytest.mark.parametrize("failure", ["unready", "drift", "excluded", "context", "missing", "unhealthy"])
def test_pin_never_falls_back_to_healthy_nx4(environment, failure):
    _, _, health, _, policy = environment
    target = health.value.detail["workers"][0]
    overrides = {}
    if failure == "unready":
        target["ready"] = False
    elif failure == "drift":
        target["config_drift"] = ["cache_type"]
    elif failure == "excluded":
        overrides["excluded_deployment_ids"] = {NX3}
    elif failure == "context":
        target["safe_context_tokens"] = 32000
    elif failure == "missing":
        health.value.detail["workers"].pop(0)
    else:
        health.value.healthy = False
    with pytest.raises(NoEligibleModelError):
        asyncio.run(choose(policy, **overrides))


@pytest.mark.parametrize("overrides", [{"client_id": "hermes-qwen36-shared"}, {"requested_model": "internal/qwen36-shared-fleet"}])
def test_other_clients_and_models_keep_available_worker_routing(environment, overrides):
    settings, _, health, _, policy = environment
    health.value.detail["workers"][0]["state"] = "busy"
    decision = asyncio.run(choose(policy, **overrides))
    assert decision.deployment_id == NX4
    assert "client_deployment_pin" not in decision.endpoint.metadata
    assert _capacity_wait_seconds(settings.section("routing"), requested_model=decision.requested_model, decision=decision) == 120


def test_two_instances_queue_second_cold_request_on_nx3_and_clean_cancel(environment):
    _, _, _, store, policy = environment

    async def scenario():
        local = Scheduler(store, instance_id="local")
        tail = Scheduler(store, instance_id="tail")
        first = await local.begin_request(None)
        second = await tail.begin_request(None)
        decision = await choose(policy)
        candidates = tuple(dict(decision.deployment_candidates))
        assert await local.try_acquire_deployment_candidates(first, candidates) == NX3
        waiting = asyncio.create_task(tail.acquire_deployment_candidates(
            second, POOL, candidates, "second", timeout_seconds=2,
            affinity_priority=False,
        ))
        await asyncio.sleep(0.15)
        assert not waiting.done()
        await first.release()
        assert await waiting == NX3
        third = await local.begin_request(None)
        cancelled = asyncio.create_task(local.acquire_deployment_candidates(
            third, POOL, candidates, "cancelled", timeout_seconds=2,
            affinity_priority=False,
        ))
        await asyncio.sleep(0.15)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await second.release()
        assert await local.try_acquire_deployment_candidates(third, candidates) == NX3
        await third.release()
    asyncio.run(scenario())


def test_busy_timeout_does_not_acquire_nx4(environment):
    _, _, _, store, policy = environment

    async def scenario():
        scheduler = Scheduler(store)
        holder = await scheduler.begin_request(None)
        waiter = await scheduler.begin_request(None)
        decision = await choose(policy)
        candidates = tuple(dict(decision.deployment_candidates))
        await scheduler.try_acquire_deployment_candidates(holder, candidates)
        with pytest.raises(QueueTimeoutError):
            await scheduler.acquire_deployment_candidates(
                waiter, POOL, candidates, "waiter", timeout_seconds=0.01,
                affinity_priority=False,
            )
        assert waiter.deployment_key is None
        await holder.release()
        assert await scheduler.try_acquire_deployment_candidates(waiter, candidates) == NX3
        await waiter.release()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [None, {}, [{"client_id": CLIENT}], "wrong"])
def test_invalid_pin_configuration_is_rejected(environment, value):
    settings = environment[0]
    with pytest.raises(ValueError, match="client_deployment_pins"):
        settings.write_runtime({"routing": {"client_deployment_pins": value}})


@pytest.mark.parametrize("wait", [0, -1, True, "900", float("nan"), float("inf"), 3601])
def test_invalid_pin_wait_is_rejected(environment, wait):
    settings = environment[0]
    pins = settings.section("routing")["client_deployment_pins"]
    pins[0]["capacity_wait_seconds"] = wait
    with pytest.raises(ValueError, match="client_deployment_pins"):
        settings.write_runtime({"routing": {"client_deployment_pins": pins}})


def test_duplicate_scope_rejected_and_empty_config_disables_pin(environment):
    settings, _, health, _, policy = environment
    pins = settings.section("routing")["client_deployment_pins"]
    with pytest.raises(ValueError, match="scopes must be unique"):
        settings.write_runtime({"routing": {"client_deployment_pins": pins * 2}})
    settings.write_runtime({"routing": {"client_deployment_pins": []}})
    health.value.detail["workers"][0]["state"] = "busy"
    assert asyncio.run(choose(policy)).deployment_id == NX4


@pytest.mark.parametrize("upstream_status", [200, 503])
def test_api_passes_authenticated_client_scope_without_changing_body(environment, tmp_path, monkeypatch, upstream_status):
    settings, registry, health, store, _ = environment
    health.value.detail["workers"][0]["short_request_rank"] = 10
    settings.write_runtime({
        "identity": {"enabled": False},
        "clients": {"policies": [{
            "id": CLIENT, "key_env": "TEST_WORKBUDDY_KEY", "models": [MODEL],
            "rpm_limit": 60, "tpm_limit": 6000000, "max_parallel_requests": 2,
        }]},
    })
    for key, value in {
        "TEST_WORKBUDDY_KEY": "test-client-key",
        "AI_ROUTER_LITELLM_MASTER_KEY": "test-internal-key",
        "AI_ROUTER_STATE_KEY": Fernet.generate_key().decode(),
        "AI_ROUTER_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "AI_ROUTER_TRAINING_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    runtime = build_runtime(settings=settings, registry=registry, store=store, token_counter=SimpleTokenCounter())
    runtime.health = health
    runtime.policy = RoutingPolicy(registry, settings, health, store=store)
    sent = []

    def respond(request):
        sent.append((request.url.host, json.loads(request.content)))
        return httpx.Response(upstream_status, json={
            "id": "test-output", "object": "chat.completion", "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        })
    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    body = {"model": MODEL, "messages": [{"role": "user", "content": "unit test"}], "temperature": 0.7, "max_tokens": 4096, "stream": False}
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer test-client-key"}, json=body)
    assert response.status_code == upstream_status, response.text
    assert sent == [(NX3, body)]


def agx_worker(state="available"):
    value = worker("qwen36-agx", state)
    value.update(profile_id="large-q8-vision", tier="local-large",
                 context_size=262144, safe_context_tokens=255000,
                 cache_type_k="q8_0", cache_type_v="q8_0")
    return value


@pytest.mark.parametrize("prompt,expected", [(53248, NX3), (53249, "qwen36-agx"), (55619, "qwen36-agx")])
def test_context_overflow_respects_output_reserve_and_keeps_busy_primary(environment, prompt, expected):
    settings, _, health, _, policy = environment
    health.value.detail["workers"][0]["state"] = "busy"
    health.value.detail["workers"].append(agx_worker("busy"))
    decision = asyncio.run(choose(policy, prompt_tokens=prompt, output_reserve_tokens=4096))
    assert decision.deployment_id == expected
    assert decision.prompt_tokens == prompt and decision.output_reserve_tokens == 4096
    assert decision.deployment_candidates == ((expected, f"http://{expected}/v1"),)
    assert decision.reason == ("client_deployment_pin" if expected == NX3 else "client_context_overflow")
    assert _capacity_wait_seconds(settings.section("routing"), requested_model=MODEL, decision=decision) == 900


def test_unready_primary_is_not_a_context_overflow(environment):
    _, _, health, _, policy = environment
    health.value.detail["workers"][0]["ready"] = False
    health.value.detail["workers"].append(agx_worker())
    with pytest.raises(NoEligibleModelError):
        asyncio.run(choose(policy, prompt_tokens=55619))


def test_unready_overflow_never_falls_back_to_another_worker(environment):
    _, _, health, _, policy = environment
    value = agx_worker(); value["ready"] = False
    health.value.detail["workers"].append(value)
    with pytest.raises(NoEligibleModelError):
        asyncio.run(choose(policy, prompt_tokens=55619))


def test_disabled_overflow_reports_context_incompatibility_without_transient_503(environment):
    settings, _, health, _, policy = environment
    rules = copy.deepcopy(settings.section("routing")["client_deployment_pins"])
    rules[0].pop("context_overflow_deployment_id")
    rules[0].pop("prewarm_min_prompt_tokens", None)
    settings.write_runtime({"routing": {"client_deployment_pins": rules}})
    health.value.detail["workers"].append(agx_worker())
    with pytest.raises(NoCompatibleModelError, match="59715") as result:
        asyncio.run(choose(policy, prompt_tokens=55619, output_reserve_tokens=4096))
    assert result.value.status_code == 422


@pytest.mark.parametrize("overflow", [None, "", " qwen36-agx", NX3, 123])
def test_invalid_overflow_configuration_is_rejected(environment, overflow):
    settings = environment[0]
    rules = copy.deepcopy(settings.section("routing")["client_deployment_pins"])
    rules[0]["context_overflow_deployment_id"] = overflow
    with pytest.raises(ValueError, match="overflow deployment"):
        settings.write_runtime({"routing": {"client_deployment_pins": rules}})
