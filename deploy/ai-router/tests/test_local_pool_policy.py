"""Integration coverage for LocalPool as used by RoutingPolicy.choose."""
import asyncio
import time
from pathlib import Path
from dataclasses import replace

import pytest

from ai_router.config import Registry
from ai_router.policy import RoutingPolicy
from ai_router.route_trace import DecisionTrace
from ai_router.store import InMemoryStateStore
from ai_router.types import ConversationState, EndpointStatus, Evaluation, RequestCapabilities, EndpointCapabilities

ROOT = Path(__file__).resolve().parents[1]
IDS = ("ai-qwen38-27b", "edge-qwen38-flash", "amd-qwen38-rocmfpx-128k")
CTX = dict(zip(IDS, (196608, 500000, 131072)))


class FakeHealth:
    def __init__(self, statuses):
        self.status_map = statuses

    async def statuses(self, endpoints, **_):
        return {e.id: self.status_map[e.id] for e in endpoints}

    async def in_cooldown(self, *_):
        return False

    async def in_capability_cooldown(self, *_):
        return False

    async def mark_failure(self, *_):
        return None

    async def mark_capability_failure(self, *_):
        return None


class Settings:
    def section(self, name):
        if name == "routing":
            return {"strategy": "intelligent_v2", "local_pool": {"enabled": True, "members": list(IDS)}}
        return {}


def trace(rid, conversation_id=None):
    return DecisionTrace(
        request_id=rid, client_id="workbuddy-public", key_id="test",
        protocol="chat", requested_model="auto",
        excerpt={"conversation_id": conversation_id or rid},
        instance_id="test-instance", boot_id="test-boot",
        settings_hash="settings", registry_hash="registry",
    )


def make_policy():
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoints = []
    for i in IDS:
        e = replace(registry.by_id(i), enabled=True, backend_type=("llama_cpp" if i.startswith("amd-") else "vllm"), auto_candidate=True)
        if i.startswith("amd-"):
            e = replace(e, capabilities=replace(e.capabilities, tools="none"))
        endpoints.append(e)
    registry = registry.with_endpoints(endpoints)
    statuses = {
        e.id: EndpointStatus(endpoint_id=e.id, healthy=True, checked_at=time.time(),
            load_headroom=1.0, latency_score=1.0, cache_generation="generation-1",
            eligible_context_tokens=CTX[e.id], detail={"workers": []})
        for e in endpoints
    }
    return RoutingPolicy(registry, Settings(), FakeHealth(statuses), InMemoryStateStore()), endpoints


def choose(policy, **kwargs):
    args = dict(requested_model="auto", evaluation=Evaluation("general", None, 1.0, "test"),
        prompt_tokens=100, output_reserve_tokens=100, modalities={"text"}, image_count=0,
        has_tools=False, conversation=None, client_id="workbuddy-public", trace=trace("request"))
    args.update(kwargs)
    return policy.choose(**args)


def test_choose_spreads_three_new_sessions_across_local_pool():
    async def case():
        policy, endpoints = make_policy()
        choices = [await choose(policy, trace=trace(f"new-{i}")) for i in range(3)]
        assert {d.endpoint.id for d in choices} == {e.id for e in endpoints}
    asyncio.run(case())


def test_choose_preserves_existing_conversation_affinity_on_continuation():
    async def case():
        policy, _ = make_policy()
        first = await choose(policy, trace=trace("first", "conv-1"))
        conversation = ConversationState(conversation_id="conv-1", public_model=first.endpoint.public_model,
            endpoint_id=first.endpoint.id, deployment_id=first.deployment_id,
            tier_rank=first.endpoint.tier_rank, task="general", last_seen=time.time())
        second = await choose(policy, conversation=conversation, trace=trace("second", "conv-1"))
        assert second.endpoint.id == first.endpoint.id
        assert second.affinity in {"hit", "bound", "stable"}
    asyncio.run(case())


def test_choose_context_filter_selects_edge_when_other_local_context_is_insufficient():
    async def case():
        policy, _ = make_policy()
        decision = await choose(policy, prompt_tokens=490000, output_reserve_tokens=5000, trace=trace("long"))
        assert decision.endpoint.id == "edge-qwen38-flash"
        assert "ai-qwen38-27b:context" in decision.candidate_rejections
        assert "amd-qwen38-rocmfpx-128k:context" in decision.candidate_rejections
    asyncio.run(case())


def test_choose_image_and_tools_constraints_are_applied_to_candidates():
    async def case():
        policy, _ = make_policy()
        image = await choose(policy, modalities={"text", "image"}, image_count=1, trace=trace("image"))
        assert image.endpoint.id in {"ai-qwen38-27b", "amd-qwen38-rocmfpx-128k"}
        assert any("edge-qwen38-flash:" in x for x in image.candidate_rejections)
        tools = await choose(policy, has_tools=True,
            required_capabilities=RequestCapabilities(protocol="chat", tools=True), trace=trace("tools"))
        assert tools.endpoint.id in {"ai-qwen38-27b", "edge-qwen38-flash"}
        assert any("amd-qwen38-rocmfpx-128k:capability" in x for x in tools.candidate_rejections)
    asyncio.run(case())


def test_choose_explicit_endpoint_directive_is_not_overridden_by_local_pool():
    async def case():
        policy, _ = make_policy()
        decision = await choose(policy, evaluation=Evaluation("general", None, 1.0, "test", required_endpoint_id="edge-qwen38-flash"),
            trace=trace("directive"))
        assert decision.endpoint.id == "edge-qwen38-flash"
        explicit = await choose(policy, requested_model="siyuan/qwen38-v100-196k", trace=trace("model"))
        assert explicit.endpoint.id == "ai-qwen38-27b"
    asyncio.run(case())


def test_choose_keeps_busy_edge_even_when_old_cost_samples_predict_faster_ai():
    async def case():
        policy, endpoints = make_policy()
        now = time.time()
        await policy.store.set_json("router:local-pool:v1:claim:busy",
            {"request_id": "busy", "endpoint_id": "edge-qwen38-flash", "owner": "other",
             "phase": "running", "started_at": now - 10, "assigned_at": now - 10,
             "prompt_bucket": 65536, "output_bucket": 16384}, 3600)
        for endpoint_id, first_s, duration in (("edge-qwen38-flash", 5, 100), ("ai-qwen38-27b", 30, 60)):
            await policy.store.set_json("router:local-pool:v1:samples:" + endpoint_id,
                {"items": [{"request_id": f"{endpoint_id}-{i}", "at": now, "generation": "generation-1",
                    "prompt_bucket": 65536, "output_bucket": 16384, "cache_state": "cold",
                    "first_s": first_s, "duration_s": duration} for i in range(5)]}, 86400)
        policy.health.status_map["edge-qwen38-flash"] = replace(policy.health.status_map["edge-qwen38-flash"], load_headroom=0.0, detail={"running": 1})
        edge = next(e for e in endpoints if e.id == "edge-qwen38-flash")
        conversation = ConversationState(conversation_id="migrate", public_model=edge.public_model,
            endpoint_id=edge.id, deployment_id=None,
            tier_rank=edge.tier_rank, task="general", last_seen=now)
        decision = await choose(policy, conversation=conversation, prompt_tokens=40000, output_reserve_tokens=16000, trace=trace("migrate", "migrate"))
        assert decision.endpoint.id == "edge-qwen38-flash"
        assert decision.migration is False
    asyncio.run(case())
