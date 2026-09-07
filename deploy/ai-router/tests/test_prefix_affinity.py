from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import yaml

from ai_router.config import Registry, Settings
from ai_router.policy import RoutingPolicy
from ai_router.prefix_affinity import (
    PrefixAffinityLocation,
    PrefixAffinityRecord,
    PrefixAffinityRepository,
)
from ai_router.store import InMemoryStateStore
from ai_router.types import EndpointStatus, Evaluation


ROOT = Path(__file__).resolve().parents[1]


def run(value):
    return asyncio.run(value)


class FakeHealth:
    def __init__(self, statuses: dict[str, EndpointStatus]) -> None:
        self.status_values = statuses

    async def statuses(self, endpoints, *, force_refresh: bool = False):
        return {
            endpoint.id: self.status_values[endpoint.id]
            for endpoint in endpoints
        }

    async def status(self, endpoint, *, force_refresh: bool = False):
        return self.status_values[endpoint.id]

    async def in_cooldown(self, _endpoint_id: str) -> bool:
        return False

    async def in_capability_cooldown(
        self,
        _deployment_id: str,
        _capability: str,
    ) -> bool:
        return False


def settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    value = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    value.write_runtime(
        {
            "prefix_affinity": {
                "enabled": enabled,
                "ttl_seconds": 3600,
                "min_prompt_tokens": 2048,
            }
        }
    )
    return value


def test_prefix_affinity_enabled_requires_a_boolean(
    tmp_path: Path,
) -> None:
    value = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "invalid-settings.yaml",
    )
    with pytest.raises(
        ValueError,
        match="prefix_affinity.enabled must be a boolean",
    ):
        value.write_runtime(
            {"prefix_affinity": {"enabled": "false"}}
        )


def test_capture_template_keeps_only_stable_prefix(
    tmp_path: Path,
) -> None:
    value = settings(tmp_path)
    template_dir = tmp_path / "private" / "prefix-templates"
    value.write_runtime(
        {
            "prefix_affinity": {
                "capture_templates": True,
                "template_dir": str(template_dir),
                "template_min_tokens": 20000,
                "template_client_ids": ["workbuddy-qwen36-shared"],
            }
        }
    )
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        value,
        "test-secret",
    )
    body = {
        "model": "siyuan/qwen36-shared",
        "messages": [
            {"role": "system", "content": "fixed private prefix"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "dynamic secret question"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,DYNAMIC"
                        },
                    },
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "fixed_tool"},
            }
        ],
    }

    path = run(
        repository.capture_template(
            "a" * 64,
            body,
            "chat",
            client_id="workbuddy-qwen36-shared",
            requested_model="siyuan/qwen36-shared",
            prefix_tokens=40000,
        )
    )

    assert path is not None
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["request"]["messages"] == [
        {"role": "system", "content": "fixed private prefix"}
    ]
    assert saved["request"]["tools"] == body["tools"]
    serialized = path.read_text(encoding="utf-8")
    assert "dynamic secret question" not in serialized
    assert "DYNAMIC" not in serialized


def test_capture_template_ignores_unapproved_client(
    tmp_path: Path,
) -> None:
    value = settings(tmp_path)
    value.write_runtime(
        {
            "prefix_affinity": {
                "capture_templates": True,
                "template_dir": str(tmp_path / "templates"),
                "template_min_tokens": 20000,
                "template_client_ids": ["workbuddy-qwen36-shared"],
            }
        }
    )
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        value,
        "test-secret",
    )

    path = run(
        repository.capture_template(
            "b" * 64,
            {
                "messages": [
                    {"role": "system", "content": "fixed"},
                    {"role": "user", "content": "dynamic"},
                ]
            },
            "chat",
            client_id="other-client",
            requested_model="siyuan/qwen36-shared",
            prefix_tokens=40000,
        )
    )

    assert path is None


def test_fingerprint_reuses_only_the_stable_text_prefix(
    tmp_path: Path,
) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "model": "lab/model",
        "messages": [
            {"role": "system", "content": "fixed system context"},
            {"role": "developer", "content": "fixed tools context"},
            {"role": "user", "content": "first question"},
        ],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    first = repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
    )
    changed_question = {
        **body,
        "messages": [
            *body["messages"][:-1],
            {"role": "user", "content": "different question"},
        ],
    }
    second = repository.fingerprint(
        changed_question,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
    )
    assert first
    assert second == first
    assert repository.fingerprint(
        body,
        "chat",
        client_id="hermes",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
    ) != first
    assert repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="other/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
    ) != first
    vision_body = {
        **body,
        "messages": [
            *body["messages"][:-1],
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AA=="},
                    },
                    {"type": "text", "text": "describe this image"},
                ],
            },
        ],
    }
    assert repository.fingerprint(
        vision_body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text", "image"},
        new_conversation=True,
    ) == first
    media_prefix = {
        **vision_body,
        "messages": [
            vision_body["messages"][-1],
            {"role": "user", "content": "next question"},
        ],
    }
    assert repository.fingerprint(
        media_prefix,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text", "image"},
        new_conversation=True,
    ) is None
    assert repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="auto",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
    ) is None
    assert repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
        context_revision="identity-v2",
    ) != repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=40000,
        modalities={"text"},
        new_conversation=True,
        context_revision="identity-v1",
    )
    assert repository.fingerprint(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        prefix_tokens=100,
        modalities={"text"},
        new_conversation=True,
    ) is None


def test_repository_round_trip(tmp_path: Path) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    run(
        repository.save(
            "prefix-key",
            endpoint_id="pool",
            deployment_id="worker-b",
            cache_generation="generation-1",
        )
    )
    value = run(repository.get("prefix-key"))
    assert value is not None
    assert value.locations == (
        PrefixAffinityLocation(
            endpoint_id="pool",
            deployment_id="worker-b",
            cache_generation="generation-1",
            warmed_at=value.locations[0].warmed_at,
            last_hit_at=value.locations[0].last_hit_at,
        ),
    )


def test_repository_tracks_multiple_locations_and_single_slot_eviction(
    tmp_path: Path,
) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    run(
        repository.save(
            "prefix-a",
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )
    run(
        repository.save(
            "prefix-a",
            endpoint_id="pool",
            deployment_id="worker-b",
            cache_generation="generation-1",
        )
    )
    first = run(repository.get("prefix-a"))
    assert first is not None
    assert {
        item.deployment_id for item in first.locations
    } == {"worker-a", "worker-b"}

    run(
        repository.save(
            "prefix-b",
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )
    remaining = run(repository.get("prefix-a"))
    replacement = run(repository.get("prefix-b"))
    assert remaining is not None
    assert [item.deployment_id for item in remaining.locations] == [
        "worker-b"
    ]
    assert replacement is not None
    assert [item.deployment_id for item in replacement.locations] == [
        "worker-a"
    ]


def test_repository_treats_a_malformed_record_as_a_miss(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    repository = PrefixAffinityRepository(
        store,
        settings(tmp_path),
        "test-secret",
    )
    run(
        store.set_json(
            "router:prefix-affinity:v1:broken",
            {
                "endpoint_id": "pool",
                "deployment_id": "worker-a",
                "updated_at": "not-a-number",
            },
        )
    )
    assert run(repository.get("broken")) is None


def test_v3_matches_the_longest_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    repository = PrefixAffinityRepository(
        store,
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    shared = tuple(range(19839))
    first = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=(*shared, *range(30000, 36000)),
        modalities={"text"},
        new_conversation=True,
    )
    second = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=(*shared, *range(40000, 46000)),
        modalities={"text"},
        new_conversation=True,
    )
    assert first is not None
    assert second is not None
    run(
        repository.record_worker(
            first,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )

    matched = run(repository.match(second))

    assert matched is not None
    assert matched.locations[0].match_type == "partial"
    assert matched.locations[0].matched_tokens == 18432


def test_v3_exact_match_reports_the_full_prefix(
    tmp_path: Path,
) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    signature = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=range(10000),
        modalities={"text"},
        new_conversation=True,
    )
    assert signature is not None
    run(
        repository.record_worker(
            signature,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )

    matched = run(repository.match(signature))

    assert matched is not None
    assert matched.locations[0].match_type == "exact"
    assert matched.locations[0].matched_tokens == 10000


def test_v3_ignores_a_partial_match_below_the_minimum(
    tmp_path: Path,
) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    first = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=(*range(7000), *range(30000, 35000)),
        modalities={"text"},
        new_conversation=True,
    )
    second = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=(*range(7000), *range(40000, 45000)),
        modalities={"text"},
        new_conversation=True,
    )
    assert first is not None
    assert second is not None
    run(
        repository.record_worker(
            first,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )

    assert run(repository.match(second)) is None


def test_v3_worker_state_is_replaced_by_a_new_single_slot_prefix(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    repository = PrefixAffinityRepository(
        store,
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    first = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=range(10000),
        modalities={"text"},
        new_conversation=True,
    )
    second = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=range(20000, 30000),
        modalities={"text"},
        new_conversation=True,
    )
    assert first is not None
    assert second is not None
    run(
        repository.record_worker(
            first,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )
    run(
        repository.record_worker(
            second,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )

    assert run(repository.match(first)) is None
    replacement = run(repository.match(second))
    assert replacement is not None
    assert replacement.locations[0].deployment_id == "worker-a"
    worker_states = run(
        store.list_json_items("router:prefix-affinity-worker:v3:")
    )
    assert len(worker_states) == 1


def test_v3_tombstone_blocks_stale_v2_fallback_during_lock_contention(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    repository = PrefixAffinityRepository(
        store,
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    signature = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=range(10000),
        modalities={"text"},
        new_conversation=True,
    )
    assert signature is not None
    assert signature.legacy_key is not None
    run(
        repository.save(
            signature.legacy_key,
            endpoint_id="pool",
            deployment_id="worker-a",
            cache_generation="generation-1",
        )
    )
    run(
        store.acquire_lock(
            "router:prefix-affinity:v2:mutation-lock",
            "contender",
            ttl_seconds=30,
        )
    )

    run(repository.invalidate_worker("worker-a"))

    assert run(repository.match(signature)) is None


def test_v3_malformed_worker_state_is_a_miss(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    repository = PrefixAffinityRepository(
        store,
        settings(tmp_path),
        "test-secret",
    )
    body = {
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "dynamic"},
        ]
    }
    signature = repository.signature(
        body,
        "chat",
        client_id="workbuddy",
        requested_model="lab/model",
        token_ids=range(10000),
        modalities={"text"},
        new_conversation=True,
    )
    assert signature is not None
    run(
        store.set_json(
            "router:prefix-affinity-worker:v3:worker-a",
            {
                "version": 3,
                "deployment_id": "worker-a",
                "prefix_tokens": "broken",
                "checkpoints": [
                    {"tokens": "broken", "digest": "broken"}
                ],
            },
        )
    )

    assert run(repository.match(signature)) is None


def test_reusable_prefix_rejects_conversation_history(
    tmp_path: Path,
) -> None:
    repository = PrefixAffinityRepository(
        InMemoryStateStore(),
        settings(tmp_path),
        "test-secret",
    )
    assert repository.reusable_prefix_body(
        {
            "messages": [
                {"role": "system", "content": "stable"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "new question"},
            ]
        },
        "chat",
    ) is None


def test_policy_selects_the_recorded_pool_worker(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        load_headroom=1.0,
        latency_score=1.0,
        cache_generation="pool-generation",
        eligible_context_tokens=57344,
        detail={
            "workers": [
                _worker("worker-a", "generation-1"),
                _worker("worker-b", "generation-1"),
            ]
        },
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )
    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation(
                "general",
                None,
                1.0,
                "test",
            ),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_affinity(
                ("worker-b", "generation-1"),
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )
    assert decision.affinity == "prefix-hit"
    assert decision.reason == "prefix_affinity"
    assert decision.deployment_id == "worker-b"
    assert decision.deployment_candidates == (
        ("worker-b", "http://worker-b/v1"),
    )


def test_first_requests_with_the_same_prefix_choose_the_same_worker(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        load_headroom=1.0,
        latency_score=1.0,
        cache_generation="pool-generation",
        eligible_context_tokens=57344,
        detail={
            "workers": [
                _worker("worker-a", "generation-1"),
                _worker("worker-b", "generation-1"),
            ]
        },
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )

    def choose():
        return run(
            policy.choose(
                requested_model="lab/model",
                evaluation=Evaluation(
                    "general",
                    None,
                    1.0,
                    "test",
                ),
                prompt_tokens=40000,
                output_reserve_tokens=8,
                modalities={"text"},
                has_tools=False,
                conversation=None,
                prefix_affinity_key="prefix-key",
                routing_key="prefix-key",
            )
        )

    first = choose()
    second = choose()
    assert first.deployment_id == second.deployment_id
    assert first.prefix_affinity_key == "prefix-key"
    assert second.prefix_affinity_key == "prefix-key"


def test_policy_uses_worker_priority_within_the_same_profile(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    slower = _worker("worker-slower", "generation-1")
    slower["priority"] = 30
    faster = _worker("worker-faster", "generation-1")
    faster["priority"] = 10
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        detail={"workers": [slower, faster]},
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )

    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity_key="new-prefix",
            routing_key="new-prefix",
        )
    )

    assert decision.deployment_id == "worker-faster"
    assert [
        worker_id
        for worker_id, _api_base in decision.deployment_candidates
    ] == ["worker-faster", "worker-slower"]


def test_policy_marks_generation_change_as_cache_reset(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        load_headroom=1.0,
        latency_score=1.0,
        cache_generation="pool-generation",
        eligible_context_tokens=57344,
        detail={
            "workers": [
                _worker("worker-a", "generation-1"),
                _worker("worker-b", "generation-2"),
            ]
        },
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )
    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation(
                "general",
                None,
                1.0,
                "test",
            ),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_affinity(
                ("worker-b", "generation-1"),
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )
    assert decision.affinity == "prefix-reset"
    assert decision.reason == "prefix_cache_generation_changed"
    assert decision.deployment_id in {"worker-a", "worker-b"}


def test_policy_falls_back_when_the_recorded_worker_is_unavailable(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        load_headroom=1.0,
        latency_score=1.0,
        cache_generation="pool-generation",
        eligible_context_tokens=57344,
        detail={"workers": [_worker("worker-a", "generation-1")]},
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )
    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation(
                "general",
                None,
                1.0,
                "test",
            ),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_affinity(
                ("worker-b", "generation-1"),
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )
    assert decision.affinity == "prefix-miss"
    assert decision.reason == "prefix_worker_unavailable"
    assert decision.deployment_id == "worker-a"


def test_policy_offloads_a_long_request_when_warm_worker_is_busy(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    busy = _worker("worker-a", "generation-1")
    busy["state"] = "busy"
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        detail={
            "workers": [
                busy,
                _worker("worker-b", "generation-1"),
            ]
        },
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )
    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_affinity(
                ("worker-a", "generation-1"),
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )
    assert decision.affinity == "prefix-replica"
    assert decision.reason == "prefix_replica_on_busy"
    assert decision.deployment_id == "worker-b"


def test_policy_can_prefer_a_faster_cold_worker_over_a_partial_hit(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    partial = _worker("worker-nx", "generation-1")
    partial["prefill_tokens_per_second"] = 180
    cold = _worker("worker-agx", "generation-1")
    cold["prefill_tokens_per_second"] = 600
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        detail={"workers": [partial, cold]},
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )

    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_matched_affinity(
                "worker-nx",
                "generation-1",
                matched_tokens=8192,
                match_type="partial",
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )

    assert decision.deployment_id == "worker-agx"
    assert decision.affinity == "prefix-bypass"
    assert decision.reason == "prefix_estimated_ttft"


def test_policy_ignores_unverified_partial_prefix_hits(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, partial_prefix_enabled=False)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        detail={
            "workers": [
                _worker("worker-nx", "generation-1"),
                _worker("worker-agx", "generation-1"),
            ]
        },
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )

    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_matched_affinity(
                "worker-nx",
                "generation-1",
                matched_tokens=8192,
                match_type="partial",
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )

    assert decision.affinity != "prefix-hit"
    assert decision.prefix_match_type == "none"
    assert decision.predicted_cached_tokens == 0


def test_policy_prefers_an_exact_hit_over_a_faster_cold_worker(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, partial_prefix_enabled=False)
    endpoint = registry.by_id("pool")
    assert endpoint is not None
    exact = _worker("worker-nx", "generation-1")
    exact["prefill_tokens_per_second"] = 180
    cold = _worker("worker-agx", "generation-1")
    cold["prefill_tokens_per_second"] = 600
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        detail={"workers": [exact, cold]},
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
        store=InMemoryStateStore(),
    )

    decision = run(
        policy.choose(
            requested_model="lab/model",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=40000,
            output_reserve_tokens=8,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            prefix_affinity=_matched_affinity(
                "worker-nx",
                "generation-1",
                matched_tokens=38912,
                match_type="exact",
            ),
            prefix_affinity_key="prefix-key",
            routing_key="prefix-key",
        )
    )

    assert decision.deployment_id == "worker-nx"
    assert decision.affinity == "prefix-hit"
    assert decision.predicted_cached_tokens == 38912


def _worker(worker_id: str, generation: str) -> dict:
    return {
        "worker_id": worker_id,
        "api_base": f"http://{worker_id}/v1",
        "profile_id": "nx-text",
        "tier": "edge-small",
        "priority": 0,
        "gpu_ids": [],
        "gpu_uuids": [],
        "names": ["Jetson Orin NX"],
        "port": 8081,
        "context_size": 57344,
        "safe_context_tokens": 57344,
        "cache_type_k": "q4_0",
        "cache_type_v": "q4_0",
        "modalities": ["text"],
        "vision_status": "disabled",
        "max_images": None,
        "runtime_fingerprint": generation,
        "cache_generation": generation,
        "ready": True,
        "state": "available",
        "config_drift": [],
        "short_request_rank": 0,
    }


def _affinity(
    *locations: tuple[str, str],
) -> PrefixAffinityRecord:
    now = time.time()
    return PrefixAffinityRecord(
        locations=tuple(
            PrefixAffinityLocation(
                endpoint_id="pool",
                deployment_id=deployment_id,
                cache_generation=generation,
                warmed_at=now,
                last_hit_at=now,
            )
            for deployment_id, generation in locations
        ),
        updated_at=now,
    )


def _matched_affinity(
    deployment_id: str,
    generation: str,
    *,
    matched_tokens: int,
    match_type: str,
) -> PrefixAffinityRecord:
    now = time.time()
    return PrefixAffinityRecord(
        locations=(
            PrefixAffinityLocation(
                endpoint_id="pool",
                deployment_id=deployment_id,
                cache_generation=generation,
                warmed_at=now,
                last_hit_at=now,
                matched_tokens=matched_tokens,
                prefix_tokens=matched_tokens,
                match_type=match_type,
            ),
        ),
        updated_at=now,
    )


def _registry(
    tmp_path: Path,
    *,
    partial_prefix_enabled: bool = True,
) -> Registry:
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tiers": {
                    "edge-small": {
                        "rank": 10,
                        "description": "test",
                    }
                },
                "endpoints": [
                    {
                        "id": "pool",
                        "public_model": "lab/model",
                        "provider_model": "model.gguf",
                        "api_base": "http://pool/v1",
                        "node": "pool",
                        "role": "responder",
                        "tier": "edge-small",
                        "tier_rank": 10,
                        "modalities": ["text"],
                        "tasks": ["general"],
                        "safe_context_tokens": 57344,
                        "configured_context_tokens": 57344,
                        "max_concurrency": 2,
                        "backend_type": "ai_pool",
                        "health_url": "http://pool/health",
                        "enabled": True,
                        "auto_candidate": False,
                        "capabilities": {
                            "chat": True,
                            "responses": "none",
                            "tools": "none",
                            "streaming": True,
                            "validation_status": "test",
                        },
                        "metadata": {
                            "prefix_affinity_enabled": True,
                            "prefix_partial_affinity_enabled": (
                                partial_prefix_enabled
                            ),
                        },
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return Registry(path)
