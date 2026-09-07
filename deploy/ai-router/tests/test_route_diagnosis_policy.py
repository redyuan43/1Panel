from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ai_router.api import _capacity_wait_seconds
from ai_router.config import Registry, Settings
from ai_router.conversation_control import ConversationControlManager
from ai_router.policy import RoutingPolicy, updated_conversation_state
from ai_router.policy_config import (
    PolicyConfigManager,
    PolicyConflictError,
)
from ai_router.route_diagnosis import diagnose_route
from ai_router.store import InMemoryStateStore
from ai_router.types import (
    ConversationState,
    Endpoint,
    EndpointStatus,
    Evaluation,
    RouteDecision,
)


ROOT = Path(__file__).resolve().parents[1]


def run(value):
    return asyncio.run(value)


class FakeSettings:
    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: int = 2,
        interval: float = 0,
        recovery_mode: str = "manual",
        preserve_tier: bool = True,
    ) -> None:
        self.sections = {
            "routing": {
                "strategy": "intelligent_v2",
                "provider_priority": "local_first",
                "conversation_stability": {
                    "enabled": enabled,
                    "health_failure_threshold": threshold,
                    "health_recheck_interval_seconds": interval,
                    "recovery_mode": recovery_mode,
                    "preserve_tier_after_migration": preserve_tier,
                },
                "remote_fallback_order": {},
                "weights": {
                    "quality": 0.5,
                    "load": 0.2,
                    "latency": 0.1,
                    "context": 0.1,
                    "cost": 0.05,
                    "locality": 0.05,
                },
            },
            "health": {
                "stale_after_seconds": 15,
            },
            "cloud": {
                "enabled": False,
                "auto_escalate": False,
                "monthly_budget": 0,
                "allowed_models": [],
                "allowed_providers": [],
            },
            "prefix_affinity": {},
        }

    def section(self, name: str):
        return self.sections.get(name, {}).copy()


class FakeRegistry:
    def __init__(self, endpoints: list[Endpoint]) -> None:
        self.endpoints = tuple(endpoints)
        self.tier_ranks = {"local-standard": 1, "local-premium": 2}

    def responders(self):
        return self.endpoints

    def by_id(self, endpoint_id: str):
        return next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.id == endpoint_id
            ),
            None,
        )

    def by_public_model(self, model: str):
        return tuple(
            endpoint
            for endpoint in self.endpoints
            if endpoint.public_model == model
        )


class SequencedHealth:
    def __init__(
        self,
        initial: dict[str, EndpointStatus],
        refreshes: dict[str, list[EndpointStatus]] | None = None,
    ) -> None:
        self.initial = initial
        self.refreshes = refreshes or {}
        self.refresh_calls: list[str] = []

    async def statuses(self, endpoints, *, force_refresh=False):
        return {
            endpoint.id: self.initial[endpoint.id]
            for endpoint in endpoints
        }

    async def status(self, endpoint, *, force_refresh=False):
        if force_refresh:
            self.refresh_calls.append(endpoint.id)
            values = self.refreshes.get(endpoint.id, [])
            if values:
                return values.pop(0)
        return self.initial[endpoint.id]

    async def in_cooldown(self, _endpoint_id):
        return False

    async def in_capability_cooldown(
        self,
        _deployment_id,
        _capability,
    ):
        return False


def endpoint(
    endpoint_id: str,
    *,
    tier_rank: int,
    safe_context: int,
    quality: float,
) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        public_model=f"model/{endpoint_id}",
        provider_model=endpoint_id,
        api_base=f"http://{endpoint_id}/v1",
        node=endpoint_id.split("-", 1)[0],
        role="responder",
        tier=(
            "local-standard"
            if tier_rank == 1
            else "local-premium"
        ),
        tier_rank=tier_rank,
        modalities=("text",),
        tasks=("*",),
        safe_context_tokens=safe_context,
        configured_context_tokens=safe_context,
        max_concurrency=1,
        backend_type="vllm",
        health_url=f"http://{endpoint_id}/health",
        quality={"general": quality},
    )


def status(
    endpoint_id: str,
    *,
    healthy: bool,
    headroom: float = 1.0,
) -> EndpointStatus:
    return EndpointStatus(
        endpoint_id=endpoint_id,
        healthy=healthy,
        checked_at=time.time(),
        load_headroom=headroom,
        latency_score=0.8,
    )


def conversation(endpoint_id: str, tier_rank: int) -> ConversationState:
    return ConversationState(
        conversation_id="lineage-test",
        public_model=f"model/{endpoint_id}",
        endpoint_id=endpoint_id,
        tier_rank=tier_rank,
        task="general",
        last_seen=time.time(),
    )


def choose(policy, state, *, prompt_tokens=100):
    return run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=10,
            modalities={"text"},
            has_tools=False,
            conversation=state,
        )
    )


def test_affinity_health_recheck_recovers_without_migration() -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=1000,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=False),
            amd.id: status(amd.id, healthy=True),
        },
        {ai.id: [status(ai.id, healthy=True)]},
    )
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(),
        health,
    )

    decision = choose(policy, conversation(ai.id, 1))

    assert decision.endpoint.id == ai.id
    assert decision.affinity == "hit"
    assert health.refresh_calls == [ai.id]


def test_affinity_migrates_after_two_real_health_failures(
    monkeypatch,
) -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=1000,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=False),
            amd.id: status(amd.id, healthy=True),
        },
        {
            ai.id: [
                status(ai.id, healthy=False),
                status(ai.id, healthy=False),
            ]
        },
    )
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(threshold=2, interval=10),
        health,
    )
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr("ai_router.policy.asyncio.sleep", record_wait)

    decision = choose(policy, conversation(ai.id, 1))

    assert decision.endpoint.id == amd.id
    assert decision.affinity == "migrated"
    assert health.refresh_calls == [ai.id, ai.id]
    assert waits == [10]


def test_hard_context_constraint_migrates_without_health_wait() -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=50,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=True),
            amd.id: status(amd.id, healthy=True),
        }
    )
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(),
        health,
    )

    decision = choose(
        policy,
        conversation(ai.id, 1),
        prompt_tokens=100,
    )

    assert decision.endpoint.id == amd.id
    assert health.refresh_calls == []


def test_next_turn_recovery_can_return_to_original_endpoint() -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=1000,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=True),
            amd.id: status(amd.id, healthy=True),
        }
    )
    state = conversation(amd.id, 2)
    state.recovery_endpoint_id = ai.id
    state.recovery_tier_rank = 1
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(recovery_mode="next_turn"),
        health,
    )

    decision = choose(policy, state)
    updated = updated_conversation_state(
        state,
        conversation_id=state.conversation_id,
        decision=decision,
        cache_generation="",
    )

    assert decision.endpoint.id == ai.id
    assert decision.affinity == "recovered"
    assert updated.recovery_endpoint_id is None


def test_manual_recovery_stays_on_migrated_endpoint_and_keeps_wait() -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=1000,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=True),
            amd.id: status(amd.id, healthy=True),
        }
    )
    state = conversation(amd.id, 2)
    state.recovery_endpoint_id = ai.id
    state.recovery_tier_rank = 1
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(recovery_mode="manual"),
        health,
    )

    decision = choose(policy, state)
    pinned = RouteDecision(
        endpoint=ai,
        requested_model="auto",
        task="general",
        prompt_tokens=100,
        output_reserve_tokens=10,
        reason="conversation_admin_pin",
        affinity="admin-pin",
        score=1,
    )

    assert decision.endpoint.id == amd.id
    assert decision.affinity == "hit"
    assert _capacity_wait_seconds(
        {
            "affinity_capacity_wait_seconds": 120,
            "new_request_capacity_wait_seconds": 0,
        },
        requested_model="auto",
        decision=pinned,
    ) == 120


def test_admin_pin_bypasses_tier_lock_but_not_context_constraint() -> None:
    ai = endpoint(
        "ai-primary",
        tier_rank=1,
        safe_context=1000,
        quality=0.8,
    )
    amd = endpoint(
        "amd-next",
        tier_rank=2,
        safe_context=1000,
        quality=0.9,
    )
    health = SequencedHealth(
        {
            ai.id: status(ai.id, healthy=True),
            amd.id: status(amd.id, healthy=True),
        }
    )
    state = conversation(amd.id, 2)
    policy = RoutingPolicy(
        FakeRegistry([ai, amd]),
        FakeSettings(recovery_mode="manual"),
        health,
    )
    control = {
        "pin": {
            "endpoint_id": ai.id,
            "expires_at": time.time() + 3600,
        }
    }

    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=10,
            modalities={"text"},
            has_tools=False,
            conversation=state,
            conversation_control=control,
        )
    )

    assert decision.endpoint.id == ai.id
    assert decision.affinity == "admin-pin"

    with pytest.raises(Exception) as exc:
        run(
            policy.choose(
                requested_model="auto",
                evaluation=Evaluation("general", None, 1.0, "test"),
                prompt_tokens=995,
                output_reserve_tokens=10,
                modalities={"text"},
                has_tools=False,
                conversation=state,
                conversation_control=control,
            )
        )
    assert getattr(exc.value, "code", None) == "conversation_pin_incompatible"


def test_conversation_control_reset_pin_and_expiry(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(
        "ai_router.conversation_control.time.time",
        lambda: now,
    )
    monkeypatch.setattr("ai_router.store.time.time", lambda: now)
    manager = ConversationControlManager(InMemoryStateStore())

    pin = run(
        manager.pin(
            client_id="client",
            conversation_id="lineage",
            endpoint_id="ai-primary",
            ttl_seconds=900,
            operator="test",
            reason="diagnosis",
        )
    )
    assert pin["expires_at"] == 1900
    assert run(manager.get("client", "lineage"))["pin"] is not None

    reset = run(
        manager.request_reset(
            client_id="client",
            conversation_id="lineage",
            operator="test",
            reason="recover",
        )
    )
    consumed = run(manager.consume_for_request("client", "lineage"))
    assert consumed["reset"] == reset
    assert run(manager.get("client", "lineage"))["reset_pending"] is False

    now = 1901.0
    assert run(manager.get("client", "lineage"))["pin"] is None


def test_policy_draft_validate_activate_conflict_and_rollback(
    tmp_path: Path,
) -> None:
    settings = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    manager = PolicyConfigManager(
        tmp_path / "policy.sqlite3",
        settings,
    )
    active = run(manager.snapshot())["active"]
    draft = run(
        manager.patch_draft(
            {
                "routing": {
                    "conversation_stability": {
                        "enabled": True,
                    }
                }
            },
            expected_revision=active["revision"],
            expected_fingerprint=active["settings_fingerprint"],
            source="test",
        )
    )
    with pytest.raises(PolicyConflictError):
        run(
            manager.patch_draft(
                {"health": {"refresh_seconds": 6}},
                expected_revision=draft["revision"],
                expected_fingerprint=None,
                source="test",
            )
        )
    with pytest.raises(PolicyConflictError):
        run(
            manager.patch_draft(
                {"health": {"refresh_seconds": 6}},
                expected_revision=draft["revision"] + 1,
                expected_fingerprint=draft["settings_fingerprint"],
                source="test",
            )
        )
    validated = run(
        manager.validate_draft(
            [],
            expected_revision=draft["revision"],
            expected_fingerprint=draft["settings_fingerprint"],
            source="test",
        )
    )
    assert validated["validation"]["impact"]["offline_only"] is True
    activated = run(
        manager.activate(
            expected_revision=validated["revision"],
            expected_fingerprint=validated["settings_fingerprint"],
            source="test",
        )
    )
    assert activated["status"] == "active"
    assert (
        settings.section("routing")["conversation_stability"]["enabled"]
        is True
    )
    rollback = run(
        manager.rollback(
            active["revision"],
            expected_active_revision=activated["revision"],
            expected_active_fingerprint=activated[
                "settings_fingerprint"
            ],
            source="test",
        )
    )
    assert rollback["status"] == "draft"
    assert rollback["base_revision"] == activated["revision"]
    settings.write_runtime(
        {
            "health": {
                "refresh_seconds": 6,
                "stale_after_seconds": 18,
                "probe_timeout_seconds": 4,
            }
        }
    )
    external = run(
        manager.record_external_activation(
            settings.value,
            source="legacy-api",
        )
    )
    snapshot = run(manager.snapshot())
    assert external["revision"] > activated["revision"]
    assert snapshot["draft"] is None
    assert snapshot["active"]["revision"] == external["revision"]


def test_lineage_fixture_diagnoses_health_not_context_and_tier_lock() -> None:
    fixture = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "lineage-e34e5fa424014588a80d735758a39c1a.json"
        ).read_text(encoding="utf-8")
    )
    settings = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=ROOT / "tests" / "fixtures" / "missing.yaml",
    )
    registry = Registry(ROOT / "config" / "registry.yaml")
    trace = fixture["traces"][1]

    diagnosis = diagnose_route(
        trace,
        fixture["traces"],
        settings,
        registry,
    )

    assert "健康状态异常或过期" in diagnosis["verdict"]
    assert {
        item["evidence"] for item in diagnosis["non_causes"]
    } >= {"115,158 < 196,608"}
    edge = next(
        item
        for item in diagnosis["alternatives"]
        if item["endpoint_id"] == "edge-qwen38-flash"
    )
    assert edge["load_headroom"] == 0
    assert [item["request_count"] for item in diagnosis["conversation_phases"]] == [
        1,
        2,
    ]
    assert "tier_lock" in diagnosis["conversation_phases"][1]["flags"]
