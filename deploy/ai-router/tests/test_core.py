from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from ai_router.api import (
    _acquire_internal_model,
    _acquire_route_capacity,
    _cache_metrics,
    _filter_restart_draining_deployments,
    _mirror_responses_format,
    _prefix_cache_delta,
    _prepare_routed_body,
    _usage_totals,
    create_app,
)
from ai_router.budget import CloudBudget
from ai_router.client_accounts import ClientAccountManager
from ai_router.compaction import CapsuleCipher
from ai_router.compaction import ContextCompactor, extract_messages
from ai_router.config import Registry, Settings, client_policies
from ai_router.control import create_app as create_control_app
from ai_router.errors import (
    AllLocalCapacityBusyError,
    AuthenticationError,
    CapacityBusyError,
    ConversationBusyError,
    InvalidToolHistoryError,
    NoEligibleModelError,
    QueueTimeoutError,
    RouterError,
)
from ai_router.evaluator import TaskEvaluator
from ai_router.health import HealthMonitor
from ai_router.history import (
    SSEAccumulator,
    assistant_items_from_response,
    history_identities,
)
from ai_router.policy import (
    ConversationRepository,
    RoutingPolicy,
    updated_conversation_state,
)
from ai_router.pilot import (
    anonymize_results,
    expand_case,
    finalize_verdict,
    oracle_result,
    validate_manifest,
    write_json,
)
from ai_router.protocol import normalize_request
from ai_router.runtime import build_runtime
from ai_router.scheduler import ClientLimiter, Scheduler
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import (
    HuggingFaceTokenCounter,
    SimpleTokenCounter,
    request_modalities,
)
from ai_router.training_archive import TrainingArchive
from ai_router.types import (
    ConversationState,
    EndpointCapabilities,
    EndpointStatus,
    Evaluation,
    RequestCapabilities,
    RouteDecision,
)
from ai_router.types import Endpoint


ROOT = Path(__file__).resolve().parents[1]


class FakeHealth:
    def __init__(
        self,
        statuses: dict[str, EndpointStatus],
        *,
        prefix_counters: dict[str, list[dict[str, float]]] | None = None,
    ) -> None:
        self._statuses = statuses
        self.failed: list[str] = []
        self.prefix_counters = prefix_counters or {}
        self.force_refreshes: list[str] = []

    async def statuses(self, endpoints, *, force_refresh: bool = False):
        if force_refresh:
            self.force_refreshes.extend(item.id for item in endpoints)
        return {item.id: self._statuses[item.id] for item in endpoints}

    async def status(self, endpoint, *, force_refresh: bool = False):
        if force_refresh:
            self.force_refreshes.append(endpoint.id)
        return self._statuses[endpoint.id]

    async def in_cooldown(self, _endpoint_id: str) -> bool:
        return False

    async def in_capability_cooldown(
        self,
        _deployment_id: str,
        _capability: str,
    ) -> bool:
        return False

    async def mark_capability_failure(
        self,
        deployment_id: str,
        capability: str,
        _cooldown_seconds: int,
    ) -> None:
        self.failed.append(f"{deployment_id}:{capability}")

    async def mark_failure(self, endpoint_id: str, _cooldown_seconds: int) -> None:
        self.failed.append(endpoint_id)

    async def prefix_cache_counters(self, endpoint):
        values = self.prefix_counters.get(endpoint.id, [])
        return values.pop(0) if values else None


def run(value):
    return asyncio.run(value)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )


def healthy(
    endpoint_id: str,
    *,
    context: int,
    workers: list[dict] | None = None,
) -> EndpointStatus:
    return EndpointStatus(
        endpoint_id=endpoint_id,
        healthy=True,
        checked_at=time.time(),
        load_headroom=1.0,
        latency_score=1.0,
        cache_generation="generation-1",
        eligible_context_tokens=context,
        detail={"workers": workers or []},
    )


def ai_workers() -> list[dict]:
    return [
        {
            "worker_id": "worker-priority-0",
            "api_base": "http://127.0.0.1:18110/v1",
            "profile_id": "v10032-qwen38-196k",
            "port": 18110,
            "tier": "v100_32_single",
            "priority": 0,
            "gpu_ids": ["3"],
            "gpu_uuids": ["GPU-v100"],
            "names": ["Tesla V100-PCIE-32GB"],
            "ready": True,
            "state": "available",
            "context_size": 196608,
            "safe_context_tokens": 196608,
            "cache_type_k": "f16",
            "cache_type_v": "f16",
            "modalities": ["text", "image"],
            "vision_status": "experimental",
            "max_images": 1,
            "runtime_fingerprint": "v100-runtime",
            "config_drift": [],
            "short_request_rank": 10,
        },
        {
            "worker_id": "worker-priority-1",
            "api_base": "http://127.0.0.1:18111/v1",
            "profile_id": "p40-qwen38-64k",
            "port": 18111,
            "tier": "p40_single",
            "priority": 2,
            "gpu_ids": ["0"],
            "gpu_uuids": ["GPU-p40"],
            "names": ["Tesla P40"],
            "ready": True,
            "state": "available",
            "context_size": 65536,
            "safe_context_tokens": 65536,
            "cache_type_k": "q8_0",
            "cache_type_v": "q8_0",
            "modalities": ["text", "image"],
            "vision_status": "experimental",
            "max_images": 1,
            "runtime_fingerprint": "p40-runtime",
            "config_drift": [],
            "short_request_rank": 0,
        },
    ]


def six_ai_workers() -> list[dict]:
    workers = [
        {
            "worker_id": f"worker-{index}",
            "api_base": f"http://127.0.0.1:{18112 + index}/v1",
            "profile_id": "p40-qwen38-64k",
            "port": 18112 + index,
            "tier": "p40_single",
            "priority": 2,
            "gpu_ids": [str(index)],
            "gpu_uuids": [f"GPU-p40-{index}"],
            "names": ["Tesla P40"],
            "ready": True,
            "state": "available",
            "context_size": 65536,
            "safe_context_tokens": 65536,
            "cache_type_k": "q8_0",
            "cache_type_v": "q8_0",
            "modalities": ["text", "image"],
            "vision_status": "experimental",
            "max_images": 1,
            "runtime_fingerprint": f"p40-runtime-{index}",
            "config_drift": [],
            "short_request_rank": 0,
        }
        for index in range(4)
    ]
    workers.append(
        {
            "worker_id": "worker-4",
            "api_base": "http://127.0.0.1:18111/v1",
            "profile_id": "v10016-p40-qwen38-262k",
            "port": 18111,
            "tier": "v100_16_p40_pair",
            "priority": 1,
            "gpu_ids": ["3", "0"],
            "gpu_uuids": ["GPU-v100-16", "GPU-p40-pair"],
            "names": ["Tesla V100-SXM2-16GB", "Tesla P40"],
            "ready": True,
            "state": "available",
            "context_size": 262144,
            "safe_context_tokens": 262144,
            "cache_type_k": "q8_0",
            "cache_type_v": "q8_0",
            "modalities": ["text"],
            "vision_status": "unverified",
            "max_images": None,
            "runtime_fingerprint": "v10016-p40-runtime",
            "config_drift": [],
            "short_request_rank": 20,
        }
    )
    workers.append(
        {
            "worker_id": "worker-5",
            "api_base": "http://127.0.0.1:18110/v1",
            "profile_id": "v10032-qwen38-196k",
            "port": 18110,
            "tier": "v100_32_single",
            "priority": 0,
            "gpu_ids": ["5"],
            "gpu_uuids": ["GPU-v100"],
            "names": ["Tesla V100-PCIE-32GB"],
            "ready": True,
            "state": "available",
            "context_size": 196608,
            "safe_context_tokens": 196608,
            "cache_type_k": "f16",
            "cache_type_v": "f16",
            "modalities": ["text", "image"],
            "vision_status": "experimental",
            "max_images": 1,
            "runtime_fingerprint": "v100-runtime",
            "config_drift": [],
            "short_request_rank": 10,
        }
    )
    return workers


def pilot_manifest() -> dict:
    cases = []
    for task, count in (("general", 6), ("code", 5), ("batch", 5)):
        for index in range(count):
            cases.append(
                {
                    "id": f"{task}-{index}",
                    "task": task,
                    "max_tokens": 64,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Return JSON with answer {task}-{index}.",
                        }
                    ],
                    "grading": {
                        "mode": "json_subset",
                        "expected": {"answer": f"{task}-{index}"},
                        "rubric": "The answer field must match exactly.",
                    },
                }
            )
    for index, target in enumerate((70000, 96000, 120000, 120000)):
        value = {
            "id": f"long-{index}",
            "task": "long-context",
            "max_tokens": 64,
            "grading": {
                "mode": "json_subset",
                "expected": {"answer": f"value-{index}"},
                "rubric": "Return the authoritative value.",
            },
            "long_context": {
                "target_tokens": target,
                "needle": {
                    "key": f"key-{index}",
                    "value": f"value-{index}",
                },
                "question": (
                    f"Return JSON with answer equal to key-{index}."
                ),
            },
        }
        if index == 3:
            value["long_context"]["follow_up"] = {
                "question": "Return the same value again as JSON.",
                "grading": {
                    "mode": "json_subset",
                    "expected": {"answer": "value-3"},
                    "rubric": "Recall the same authoritative value.",
                },
            }
        cases.append(value)
    return {
        "benchmark_version": 2,
        "run_type": "pilot",
        "generated_by": "gpt-5.6-luna",
        "seed": "seed-123",
        "cases": cases,
    }


def test_settings_and_registry_load(tmp_path: Path) -> None:
    value = settings(tmp_path)
    registry = Registry(ROOT / "config" / "registry.yaml")
    assert value.section("routing")["weights"]["quality"] == 0.50
    assert len(registry.endpoints) == 7
    assert all(
        item.max_concurrency == 1
        for item in registry.endpoints
        if not item.cloud
    )
    ivan = registry.by_id("ivan-qwen38-flash-128k")
    assert ivan is not None
    assert ivan.public_model == "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF"
    assert ivan.safe_context_tokens == 131072
    assert ivan.max_concurrency == 1
    assert ivan.auto_candidate is True
    ai = registry.by_id("ai-qwen38-27b")
    assert ai is not None
    assert ai.safe_context_tokens == 262144
    assert [item.id for item in ai.deployment_profiles] == [
        "p40-qwen38-64k",
        "v10032-qwen38-196k",
        "v10016-p40-qwen38-262k",
    ]
    assert ai.deployment_profiles[0].short_request_rank == 0
    assert ai.deployment_profiles[1].short_request_rank == 10
    assert ai.deployment_profiles[2].short_request_rank == 20
    amd = registry.by_id("amd-qwen38-rocmfpx-128k")
    assert amd is not None
    assert amd.public_model == (
        "Qwen/Qwen3.8-Flash-Next-ROCmFP4-FAST-imatrix-MTP"
    )
    assert amd.safe_context_tokens == 131072
    assert amd.max_concurrency == 1
    assert amd.auto_candidate is True
    assert registry.by_id("ivan-qwen38-rocmfpx-128k") is None
    assert registry.by_id("cloud-deepseek-v4-flash").max_concurrency == 8
    glm = registry.by_id("zhipu-glm-5.3-flash")
    assert glm is not None
    assert glm.public_model == "zhipu/glm-5.3-flash"
    assert glm.modalities == ("text", "image")
    assert glm.safe_context_tokens == 1000000
    assert glm.auto_candidate is False
    assert glm.quality["code"] > registry.by_id(
        "cloud-deepseek-v4-flash"
    ).quality["code"]
    assert glm.metadata["billing_mode"] == "subscription"
    assert glm.capabilities.tool_choice_modes == ("auto",)
    codex = registry.by_id("codex-pro-gpt-5.6-sol")
    assert codex is not None
    assert codex.public_model == "codex-pro/gpt-5.6-sol"
    assert codex.auto_candidate is True
    assert codex.metadata["billing_mode"] == "subscription"
    assert value.section("routing")["affinity_capacity_wait_seconds"] == 3
    assert value.section("routing")["new_request_capacity_wait_seconds"] == 0
    assert value.section("routing")["all_local_busy_policy"] == "cloud_or_429"
    assert value.section("routing")["provider_priority"] == "local_first"
    assert value.section("affinity")["ttl_seconds"] == 86400
    policies = {
        item.id: item
        for item in client_policies(value)
    }
    assert policies["check-boards"].models == ("auto",)
    assert policies["check-boards"].max_parallel_requests == 4
    assert all(
        endpoint.capabilities.chat
        and endpoint.capabilities.tools == "parallel"
        for endpoint in registry.endpoints
    )
    assert glm.capabilities.responses == "adapter"
    assert all(
        endpoint.capabilities.responses == "native"
        for endpoint in registry.endpoints
        if endpoint.id != glm.id
    )


def test_tail_control_tls_is_isolated_and_installable() -> None:
    compose = yaml.safe_load(
        (ROOT / "compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    tail_control = services["router-control-tail"]
    tls_mount = "/opt/1panel/ai-router-control-tls:/tls:ro"
    assert tls_mount in tail_control["volumes"]
    assert "/tls/" in tail_control["command"][-1]
    for service_id, service in services.items():
        if service_id == "router-control-tail":
            continue
        assert tls_mount not in service.get("volumes", [])

    installer = ROOT / "scripts" / "install-tail-control-tls.sh"
    renewal = ROOT / "scripts" / "renew-tail-control-cert.sh"
    assert installer.stat().st_mode & 0o111
    assert renewal.stat().st_mode & 0o111
    assert "AI_ROUTER_TLS_SKIP_RESTART=1" in installer.read_text(
        encoding="utf-8"
    )
    assert "/opt/1panel/ai-router/tls" in installer.read_text(
        encoding="utf-8"
    )
    renewal_text = renewal.read_text(encoding="utf-8")
    assert "certificate_public_key_fingerprint" in renewal_text
    assert "private_key_fingerprint" in renewal_text


def test_training_archive_encrypts_deduplicates_and_exports(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "training.key"
    key_path.write_bytes(Fernet.generate_key())
    database_path = tmp_path / "conversations.sqlite3"
    archive = TrainingArchive(str(database_path), str(key_path))
    token = run(
        archive.begin(
            request_id="request-1",
            conversation_id="conversation-1",
            conversation_mode="stateful",
            client_id="client-1",
            key_id="key-1",
            protocol="chat",
            received_body={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "USER_SECRET_PROMPT"}
                ],
            },
            instance_id="router-api-local",
            boot_id="boot-1",
        )
    )
    assert token
    run(
        archive.mark_routed(
            token,
            effective_body={
                "messages": [
                    {"role": "user", "content": "USER_SECRET_PROMPT"}
                ]
            },
            routed_body={
                "messages": [
                    {"role": "user", "content": "USER_SECRET_PROMPT"}
                ]
            },
            route={
                "attempt": 1,
                "selected_model": "local-model",
                "endpoint_id": "local-endpoint",
            },
        )
    )
    run(
        archive.complete(
            token,
            status_code=200,
            response_payload=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "ASSISTANT_SECRET_RESPONSE",
                            }
                        }
                    ]
                }
            ).encode(),
        )
    )
    duplicate = run(
        archive.begin(
            request_id="request-1",
            conversation_id="conversation-1",
            conversation_mode="stateful",
            client_id="client-1",
            key_id="key-1",
            protocol="chat",
            received_body={"messages": []},
            instance_id="router-api-tail",
            boot_id="boot-2",
        )
    )
    assert duplicate is None

    failed = run(
        archive.begin(
            request_id="request-2",
            conversation_id="conversation-2",
            conversation_mode="inferred",
            client_id="client-1",
            key_id="key-1",
            protocol="responses",
            received_body={"input": "FAILED_SECRET_PROMPT"},
            instance_id="router-api-tail",
            boot_id="boot-2",
        )
    )
    run(
        archive.fail(
            failed,
            status_code=503,
            error={"type": "test_failure"},
        )
    )

    state_key = Fernet.generate_key()
    state_cipher = Fernet(state_key)
    snapshot_messages = [
        {"role": "user", "content": "BACKFILL_SECRET_PROMPT"},
        {"role": "assistant", "content": "BACKFILL_SECRET_RESPONSE"},
    ]
    state = {
        "conversation_id": "legacy-conversation",
        "encrypted_capsule": state_cipher.encrypt(
            json.dumps(snapshot_messages).encode()
        ).decode(),
        "boundary_hash": "boundary",
        "last_seen": time.time(),
        "public_model": "legacy-model",
        "endpoint_id": "legacy-endpoint",
    }
    assert run(
        archive.backfill_conversation_snapshots(
            [state],
            state_key.decode(),
        )
    ) == 1
    assert run(
        archive.backfill_conversation_snapshots(
            [state],
            state_key.decode(),
        )
    ) == 0

    status = run(archive.status())
    assert status["records"] == 3
    assert status["trainable_records"] == 2
    assert status["incomplete_records"] == 0
    database_bytes = database_path.read_bytes()
    for secret in (
        b"USER_SECRET_PROMPT",
        b"ASSISTANT_SECRET_RESPONSE",
        b"FAILED_SECRET_PROMPT",
        b"BACKFILL_SECRET_PROMPT",
    ):
        assert secret not in database_bytes

    output_path = tmp_path / "export" / "training.jsonl"
    assert run(
        archive.export_jsonl(
            str(output_path),
            trainable_only=True,
        )
    ) == 2
    exported = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    request_record = next(
        item
        for item in exported
        if item["payload"]["record_type"] == "request"
    )
    assert request_record["payload"]["request"]["received_body"][
        "messages"
    ][0]["content"] == "USER_SECRET_PROMPT"
    assert request_record["payload"]["response"]["body"]["value"][
        "choices"
    ][0]["message"]["content"] == "ASSISTANT_SECRET_RESPONSE"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_legacy_five_weight_runtime_remains_loadable(
    tmp_path: Path,
) -> None:
    runtime_path = tmp_path / "settings.yaml"
    runtime_path.write_text(
        yaml.safe_dump(
            {
                "routing": {
                    "weights": {
                        "quality": 0.55,
                        "load": 0.20,
                        "latency": 0.10,
                        "context": 0.10,
                        "locality": 0.05,
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    value = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=runtime_path,
    )
    assert value.section("routing")["weights"]["cost"] == 0
    assert sum(value.section("routing")["weights"].values()) == 1


@pytest.mark.parametrize("ttl_seconds", [299, 86401])
def test_affinity_ttl_is_limited_to_cache_lease_range(
    tmp_path: Path,
    ttl_seconds: int,
) -> None:
    value = settings(tmp_path)
    with pytest.raises(ValueError, match="between 300 and 86400"):
        value.write_runtime(
            {"affinity": {"ttl_seconds": ttl_seconds}}
        )


def test_expired_conversation_is_ignored_even_if_store_key_remains(
    tmp_path: Path,
) -> None:
    value = settings(tmp_path)
    store = InMemoryStateStore()
    repository = ConversationRepository(store, value)
    state = ConversationState(
        conversation_id="expired-conversation",
        public_model="model",
        endpoint_id="endpoint",
        tier_rank=1,
        task="general",
        last_seen=time.time() - 86401,
    )
    run(
        store.set_json(
            "router:conversation:expired-conversation",
            state.to_dict(),
            ttl_seconds=172800,
        )
    )
    assert run(repository.get("expired-conversation")) is None


def test_same_conversation_is_rejected_while_active() -> None:
    store = InMemoryStateStore()
    scheduler = Scheduler(store)
    first = run(scheduler.begin_request("conversation-1"))
    with pytest.raises(ConversationBusyError):
        run(scheduler.begin_request("conversation-1"))
    run(first.release())
    second = run(scheduler.begin_request("conversation-1"))
    run(second.release())


def test_instance_restart_cleanup_preserves_other_router_leases() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        local = Scheduler(
            store,
            instance_id="router-api-local",
            boot_id="boot-old",
        )
        tail = Scheduler(
            store,
            instance_id="router-api-tail",
            boot_id="boot-tail",
        )
        limiter = ClientLimiter(store)

        local_lease = await local.begin_request("conversation-local")
        tail_lease = await tail.begin_request("conversation-tail")
        await local.acquire_deployment(
            local_lease,
            "worker-local",
            "request-local",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        await tail.acquire_deployment(
            tail_lease,
            "worker-tail",
            "request-tail",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        assert await limiter.acquire_parallel(
            "shared-client",
            local_lease.owner_token,
            2,
        )
        assert await limiter.acquire_parallel(
            "shared-client",
            tail_lease.owner_token,
            2,
        )
        await store.enqueue(
            "router:queue:test",
            f"{local_lease.owner_token}:queued",
            time.time(),
        )

        restarted = Scheduler(
            store,
            instance_id="router-api-local",
            boot_id="boot-new",
        )
        cleanup = await restarted.cleanup_previous_instance_leases()

        assert cleanup == {
            "deployment_members": 1,
            "client_members": 1,
            "queue_members": 1,
            "conversation_locks": 1,
            "deployments": ["worker-local"],
        }

        new_local = await restarted.begin_request("conversation-local")
        assert (
            await restarted.try_acquire_deployment_candidates(
                new_local,
                ("worker-local",),
            )
            == "worker-local"
        )
        assert await limiter.acquire_parallel(
            "shared-client",
            new_local.owner_token,
            2,
        )

        with pytest.raises(ConversationBusyError):
            await tail.begin_request("conversation-tail")
        blocked = await restarted.begin_request(None)
        assert (
            await restarted.try_acquire_deployment_candidates(
                blocked,
                ("worker-tail",),
            )
            is None
        )

        await new_local.release()
        await blocked.release()
        await tail_lease.release()
        await limiter.release_parallel(
            "shared-client",
            new_local.owner_token,
        )
        await limiter.release_parallel(
            "shared-client",
            tail_lease.owner_token,
        )

    run(scenario())


def test_deployment_capacity_allows_future_parallel_cloud_requests() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        scheduler = Scheduler(store)
        first = await scheduler.begin_request(None)
        second = await scheduler.begin_request(None)
        third = await scheduler.begin_request(None)
        await scheduler.acquire_deployment(
            first,
            "cloud-model",
            "request-1",
            timeout_seconds=0.2,
            affinity_priority=False,
            capacity=2,
        )
        await scheduler.acquire_deployment(
            second,
            "cloud-model",
            "request-2",
            timeout_seconds=0.2,
            affinity_priority=False,
            capacity=2,
        )
        with pytest.raises(QueueTimeoutError):
            await scheduler.acquire_deployment(
                third,
                "cloud-model",
                "request-3",
                timeout_seconds=0.05,
                affinity_priority=False,
                capacity=2,
            )
        await first.release()
        await second.release()
        await third.release()

    run(scenario())


def test_pool_candidates_use_distinct_single_capacity_workers() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        scheduler = Scheduler(store)
        first = await scheduler.begin_request(None)
        second = await scheduler.begin_request(None)
        candidates = ("worker-a", "worker-b")
        first_id = await scheduler.acquire_deployment_candidates(
            first,
            "local-pool",
            candidates,
            "request-1",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        second_id = await scheduler.acquire_deployment_candidates(
            second,
            "local-pool",
            candidates,
            "request-2",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        assert first_id == "worker-a"
        assert second_id == "worker-b"
        await first.release()
        await second.release()

    run(scenario())


def test_nonblocking_capacity_skips_busy_deployments() -> None:
    async def scenario() -> None:
        store = InMemoryStateStore()
        scheduler = Scheduler(store)
        first = await scheduler.begin_request(None)
        second = await scheduler.begin_request(None)
        third = await scheduler.begin_request(None)
        await scheduler.acquire_deployment(
            first,
            "worker-a",
            "request-1",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        selected = await scheduler.try_acquire_deployment_candidates(
            second,
            ("worker-a", "worker-b"),
        )
        assert selected == "worker-b"
        unavailable = await scheduler.try_acquire_deployment_candidates(
            third,
            ("worker-a", "worker-b"),
        )
        assert unavailable is None
        await first.release()
        await second.release()
        await third.release()

    run(scenario())


def test_nonblocking_capacity_uses_six_distinct_workers() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(InMemoryStateStore())
        workers = tuple(f"worker-{index}" for index in range(6))
        leases = []
        selected = []
        for index in range(6):
            lease = await scheduler.begin_request(None)
            leases.append(lease)
            selected.append(
                await scheduler.try_acquire_deployment_candidates(
                    lease,
                    workers,
                )
            )
        assert selected == list(workers)
        seventh = await scheduler.begin_request(None)
        assert (
            await scheduler.try_acquire_deployment_candidates(
                seventh,
                workers,
            )
            is None
        )
        for lease in leases:
            await lease.release()
        await seventh.release()

    run(scenario())


def test_runtime_start_cleans_previous_boot_and_marks_backend_draining(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
        monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv(
            "AI_ROUTER_AUDIT_PATH",
            str(tmp_path / "audit.jsonl"),
        )
        store = InMemoryStateStore()
        registry = Registry(ROOT / "config" / "registry.yaml")
        value = settings(tmp_path)
        first = build_runtime(
            settings=value,
            registry=registry,
            store=store,
            token_counter=SimpleTokenCounter(),
            instance_id="router-api-local",
            boot_id="boot-old",
        )
        await first.start()
        lease = await first.scheduler.begin_request("conversation-restart")
        await first.scheduler.acquire_deployment(
            lease,
            "ivan-qwen38-flash-128k",
            "request-restart",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
        assert await first.limiter.acquire_parallel(
            "1panel",
            lease.owner_token,
            8,
        )
        await first.track_request_started(
            lease.owner_token,
            "request-restart",
            "conversation-restart",
        )
        await first.track_request_routed(
            lease.owner_token,
            requested_model="auto",
            selected_model="model",
            endpoint_id="ivan-qwen38-flash-128k",
            deployment_id="ivan-qwen38-flash-128k",
            node="ivan",
            task="general",
            reason="local_priority",
            affinity="new",
            prompt_tokens=100,
            output_reserve_tokens=32,
        )

        second = build_runtime(
            settings=value,
            registry=registry,
            store=store,
            token_counter=SimpleTokenCounter(),
            instance_id="router-api-local",
            boot_id="boot-new",
        )
        await second.start()

        assert second.startup_cleanup["deployment_members"] == 1
        assert second.startup_cleanup["client_members"] == 1
        assert second.startup_cleanup["conversation_locks"] == 1
        marker = await second.draining_marker(
            "ivan-qwen38-flash-128k"
        )
        assert marker is not None
        assert marker["previous_boot_id"] == "boot-old"
        state = await store.get_json(
            "router:instance-state:router-api-local"
        )
        assert state is not None
        assert state["boot_id"] == "boot-new"
        assert state["startup_cleanup"]["deployment_members"] == 1
        events = second.audit.recent(20)
        interrupted = next(
            item
            for item in events
            if item["event"] == "request_interrupted_by_restart"
        )
        assert interrupted["request_id"] == "request-restart"
        assert interrupted["deployment_id"] == "ivan-qwen38-flash-128k"
        await second.close()

    run(scenario())


def test_restart_drain_guard_waits_for_busy_backend_then_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
        monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv(
            "AI_ROUTER_AUDIT_PATH",
            str(tmp_path / "audit.jsonl"),
        )
        store = InMemoryStateStore()
        registry = Registry(ROOT / "config" / "registry.yaml")
        endpoint = registry.by_id("ivan-qwen38-flash-128k")
        assert endpoint is not None
        runtime = build_runtime(
            settings=settings(tmp_path),
            registry=registry,
            store=store,
            token_counter=SimpleTokenCounter(),
        )
        fake_health = FakeHealth(
            {
                endpoint.id: EndpointStatus(
                    endpoint_id=endpoint.id,
                    healthy=True,
                    checked_at=time.time(),
                    load_headroom=0,
                    eligible_context_tokens=endpoint.safe_context_tokens,
                    detail={"processing": 1},
                )
            }
        )
        runtime.health = fake_health
        await store.set_json(
            f"router:draining-deployment:{endpoint.id}",
            {
                "deployment_id": endpoint.id,
                "instance_id": "router-api-local",
                "previous_boot_id": "boot-old",
                "cleared_at": time.time(),
                "last_busy_audit_at": 0,
            },
            ttl_seconds=7200,
        )
        decision = RouteDecision(
            endpoint=endpoint,
            requested_model="auto",
            task="general",
            prompt_tokens=100,
            output_reserve_tokens=32,
            reason="local_priority",
            affinity="new",
            score=1,
            deployment_id=endpoint.id,
            upstream_api_base=endpoint.api_base,
        )
        excluded_endpoints: set[str] = set()
        assert not await _filter_restart_draining_deployments(
            runtime,
            decision,
            excluded_endpoints,
            set(),
        )
        assert excluded_endpoints == {endpoint.id}
        assert fake_health.force_refreshes == [endpoint.id]
        assert await runtime.draining_marker(endpoint.id) is not None

        fake_health._statuses[endpoint.id] = EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=time.time(),
            load_headroom=1,
            eligible_context_tokens=endpoint.safe_context_tokens,
            detail={"processing": 0},
        )
        recovered = RouteDecision(
            endpoint=endpoint,
            requested_model="auto",
            task="general",
            prompt_tokens=100,
            output_reserve_tokens=32,
            reason="local_priority",
            affinity="new",
            score=1,
            deployment_id=endpoint.id,
            upstream_api_base=endpoint.api_base,
        )
        assert await _filter_restart_draining_deployments(
            runtime,
            recovered,
            set(),
            set(),
        )
        assert await runtime.draining_marker(endpoint.id) is None
        events = runtime.audit.recent(20)
        assert any(
            item["event"] == "backend_busy_after_restart"
            for item in events
        )
        assert any(
            item["event"] == "backend_available_after_drain"
            for item in events
        )

    run(scenario())


def test_router_drain_rejects_new_inference_but_keeps_status_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=Registry(ROOT / "config" / "registry.yaml"),
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
        instance_id="router-api-local",
        boot_id="boot-drain",
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        drained = client.post(
            "/internal/drain",
            headers={"Authorization": "Bearer admin-key"},
        )
        rejected = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        health = client.get("/health")
        status = client.get(
            "/internal/status",
            headers={"Authorization": "Bearer admin-key"},
        )

    assert drained.status_code == 200
    assert drained.json()["instance"]["draining"] is True
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "router_draining"
    assert health.status_code == 200
    assert health.json()["draining"] is True
    assert status.status_code == 200
    assert status.json()["instance"]["boot_id"] == "boot-drain"


def test_ai_pool_health_builds_physical_deployments_and_quarantines_drift(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    workers = six_ai_workers()
    workers[1]["cache_type_k"] = "f16"

    async def scenario() -> EndpointStatus:
        async def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "runtime_fingerprint": "runtime-a",
                    "workers": workers,
                },
            )

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        )
        monitor = HealthMonitor(
            InMemoryStateStore(),
            client=client,
        )
        try:
            return await monitor.status(
                endpoint,
                force_refresh=True,
            )
        finally:
            await client.aclose()

    status = run(scenario())
    by_id = {
        item["worker_id"]: item
        for item in status.detail["workers"]
    }
    assert status.healthy is True
    assert status.eligible_context_tokens == 262144
    assert status.detail["effective_modalities"] == ["image", "text"]
    assert status.detail["schedulable_workers"] == 5
    assert by_id["worker-0"]["profile_id"] == "p40-qwen38-64k"
    assert by_id["worker-0"]["max_images"] == 1
    assert by_id["worker-0"]["schedulable"] is True
    assert by_id["worker-1"]["config_drift"] == ("cache_type_k",)
    assert by_id["worker-1"]["schedulable"] is False
    assert (
        by_id["worker-4"]["profile_id"]
        == "v10016-p40-qwen38-262k"
    )
    assert by_id["worker-4"]["schedulable"] is True
    assert by_id["worker-5"]["profile_id"] == "v10032-qwen38-196k"


def test_ai_pool_pins_conversation_to_physical_worker(tmp_path: Path) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    status = healthy(
        endpoint.id,
        context=262144,
        workers=ai_workers(),
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
    )
    first = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert first.deployment_id == "worker-priority-1"
    assert first.upstream_api_base == "http://127.0.0.1:18111/v1"
    assert [item[0] for item in first.deployment_candidates] == [
        "worker-priority-1",
        "worker-priority-0",
    ]
    state = updated_conversation_state(
        None,
        conversation_id="conversation-1",
        decision=first,
        cache_generation="generation-1",
    )
    second = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=state,
        )
    )
    assert second.deployment_id == first.deployment_id
    assert second.affinity == "hit"
    assert second.deployment_candidates == (
        ("worker-priority-1", "http://127.0.0.1:18111/v1"),
    )


def test_short_requests_hash_across_p40s_and_reserve_v100(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth(
            {
                endpoint.id: healthy(
                    endpoint.id,
                    context=endpoint.safe_context_tokens,
                    workers=six_ai_workers(),
                )
            }
        ),
    )
    selected = {
        run(
            policy.choose(
                requested_model=endpoint.public_model,
                evaluation=Evaluation("general", None, 1.0, "test"),
                prompt_tokens=100,
                output_reserve_tokens=100,
                modalities={"text"},
                has_tools=False,
                conversation=None,
                routing_key=f"request-{index}",
            )
        ).deployment_id
        for index in range(100)
    }
    assert selected == {f"worker-{index}" for index in range(4)}
    v100 = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            excluded_deployment_ids={
                f"worker-{index}" for index in range(5)
            },
            routing_key="v100-fallback",
        )
    )
    assert v100.deployment_id == "worker-5"


def test_ai_pool_fails_over_when_affinity_worker_is_externally_leased(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    workers = ai_workers()
    status = healthy(endpoint.id, context=262144, workers=workers)
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
    )
    original = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    state = updated_conversation_state(
        None,
        conversation_id="conversation-1",
        decision=original,
        cache_generation="generation-1",
    )
    workers[1]["state"] = "leased"
    changed = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=state,
        )
    )
    assert changed.deployment_id == "worker-priority-0"
    assert changed.affinity == "physical-failover"
    assert changed.reason == "physical_worker_unavailable"


def test_ai_pool_failure_excludes_only_the_failed_worker(tmp_path: Path) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    status = healthy(
        endpoint.id,
        context=262144,
        workers=ai_workers(),
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
    )
    decision = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
            excluded_deployment_ids={"worker-priority-0"},
        )
    )
    assert decision.endpoint.id == endpoint.id
    assert decision.deployment_id == "worker-priority-1"


def test_ai_pool_filters_workers_by_required_context(tmp_path: Path) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    status = healthy(
        endpoint.id,
        context=262144,
        workers=ai_workers(),
    )
    policy = RoutingPolicy(
        registry,
        settings(tmp_path),
        FakeHealth({endpoint.id: status}),
    )
    decision = run(
        policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("long-context", None, 1.0, "test"),
            prompt_tokens=110000,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.deployment_candidates == (
        ("worker-priority-0", "http://127.0.0.1:18110/v1"),
    )
    with pytest.raises(NoEligibleModelError):
        run(
            policy.choose(
                requested_model=endpoint.public_model,
                evaluation=Evaluation("long-context", None, 1.0, "test"),
                prompt_tokens=196600,
                output_reserve_tokens=100,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )


def test_explicit_model_change_is_marked_for_compaction(tmp_path: Path) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    ai = registry.by_id("ai-qwen38-27b")
    edge = registry.by_id("edge-qwen38-flash")
    assert ai is not None and edge is not None
    statuses = {
        ai.id: healthy(ai.id, context=262144, workers=ai_workers()),
        edge.id: healthy(edge.id, context=262144),
    }
    policy = RoutingPolicy(registry, settings(tmp_path), FakeHealth(statuses))
    original = run(
        policy.choose(
            requested_model=ai.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    state = updated_conversation_state(
        None,
        conversation_id="conversation-1",
        decision=original,
        cache_generation="generation-1",
    )
    changed = run(
        policy.choose(
            requested_model=edge.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=state,
        )
    )
    assert changed.migration is True
    assert changed.reason == "explicit_model_change"


def test_auto_keeps_models_eligible_without_verified_quality(
    tmp_path: Path,
) -> None:
    registry_value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(encoding="utf-8")
    )
    for endpoint in registry_value["endpoints"]:
        endpoint["quality"] = {}
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(registry_value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    registry = Registry(registry_path)
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers() if item.backend_type == "ai_pool" else None,
        )
        for item in registry.endpoints
    }
    policy = RoutingPolicy(registry, settings(tmp_path), FakeHealth(statuses))
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.cloud is False


def test_auto_uses_context_and_capacity_when_task_quality_is_missing(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers() if item.backend_type == "ai_pool" else None,
        )
        for item in registry.endpoints
    }
    policy = RoutingPolicy(registry, settings(tmp_path), FakeHealth(statuses))
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("long-context", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.cloud is False


def test_auto_prefers_local_unless_cloud_tier_is_required(
    tmp_path: Path,
) -> None:
    registry_value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(encoding="utf-8")
    )
    for endpoint in registry_value["endpoints"]:
        if endpoint["id"] == "cloud-deepseek-v4-flash":
            endpoint["quality"] = {"general": 100}
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(registry_value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    registry = Registry(registry_path)
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["deepseek"],
                "allowed_models": ["deepseek/deepseek-v4-flash"],
            }
        }
    )
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers() if item.backend_type == "ai_pool" else None,
        )
        for item in registry.endpoints
    }
    policy = RoutingPolicy(registry, value, FakeHealth(statuses))
    local = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    cloud = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "general",
                "cloud-frontier",
                1.0,
                "test",
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert local.endpoint.id == "edge-qwen38-flash"
    assert cloud.endpoint.id == "cloud-deepseek-v4-flash"


@pytest.mark.parametrize(
    ("provider_priority", "expected_node"),
    [
        ("local_first", "edge"),
        ("balanced", "edge"),
        ("cloud_first", "cloud"),
    ],
)
def test_provider_priority_modes_are_hot_configurable(
    tmp_path: Path,
    provider_priority: str,
    expected_node: str,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["deepseek"],
                "allowed_models": ["deepseek/deepseek-v4-flash"],
            },
            "routing": {"provider_priority": provider_priority},
        }
    )
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers()
            if item.backend_type == "ai_pool"
            else None,
        )
        for item in registry.endpoints
    }
    decision = run(
        RoutingPolicy(
            registry,
            value,
            FakeHealth(statuses),
        ).choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="responses",
                tools=True,
                parallel_tools=True,
            ),
            conversation=None,
        )
    )
    assert decision.endpoint.node == expected_node


def test_cloud_first_uses_local_when_cloud_is_disabled(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {"routing": {"provider_priority": "cloud_first"}}
    )
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers()
            if item.backend_type == "ai_pool"
            else None,
        )
        for item in registry.endpoints
    }
    decision = run(
        RoutingPolicy(
            registry,
            value,
            FakeHealth(statuses),
        ).choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
            ),
            conversation=None,
        )
    )
    assert decision.endpoint.cloud is False


def test_subscription_frontier_is_a_soft_auto_preference(
    tmp_path: Path,
) -> None:
    registry_value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(encoding="utf-8")
    )
    for endpoint in registry_value["endpoints"]:
        if endpoint["id"] == "codex-pro-gpt-5.6-sol":
            endpoint["auto_candidate"] = True
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            registry_value,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["openai-codex", "deepseek"],
                "allowed_models": [
                    "codex-pro/gpt-5.6-sol",
                    "deepseek/deepseek-v4-flash",
                ],
            }
        }
    )
    statuses = {}
    for endpoint in registry.endpoints:
        workers = None
        if endpoint.backend_type == "ai_pool":
            workers = ai_workers()
        elif endpoint.backend_type == "codex_pool":
            workers = [
                {
                    "worker_id": "codex-primary",
                    "ready": True,
                    "state": "available",
                    "safe_context_tokens": 131072,
                    "api_base": (
                        "http://127.0.0.1:14010"
                        "/v1/accounts/primary"
                    ),
                }
            ]
        statuses[endpoint.id] = healthy(
            endpoint.id,
            context=endpoint.safe_context_tokens,
            workers=workers,
        )
    policy = RoutingPolicy(registry, value, FakeHealth(statuses))
    preferred = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code",
                None,
                0.95,
                "complex_code",
                "subscription-frontier",
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
            ),
            conversation=None,
        )
    )
    fallback = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code",
                None,
                0.95,
                "complex_code",
                "subscription-frontier",
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
            ),
            conversation=None,
            excluded_endpoint_ids={"codex-pro-gpt-5.6-sol"},
        )
    )
    normal = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert preferred.endpoint.id == "codex-pro-gpt-5.6-sol"
    assert preferred.deployment_id == "codex-primary"
    assert preferred.reason == "preferred_tier"
    assert fallback.endpoint.cloud is False
    assert normal.endpoint.cloud is False


def test_unvalidated_glm_requires_an_explicit_model(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["zhipu-coding", "deepseek"],
                "allowed_models": [
                    "zhipu/glm-5.3-flash",
                    "deepseek/deepseek-v4-flash",
                ],
            }
        }
    )
    statuses = {
        item.id: healthy(
            item.id,
            context=item.safe_context_tokens,
            workers=ai_workers()
            if item.backend_type == "ai_pool"
            else None,
        )
        for item in registry.endpoints
    }
    automatic = run(
        RoutingPolicy(
            registry,
            value,
            FakeHealth(statuses),
        ).choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code",
                None,
                0.95,
                "complex_code",
                "subscription-frontier",
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
            ),
            conversation=None,
            excluded_endpoint_ids={"codex-pro-gpt-5.6-sol"},
        )
    )
    assert automatic.endpoint.cloud is False
    assert "zhipu-glm-5.3-flash:auto_disabled" in (
        automatic.candidate_rejections
    )

    explicit = run(
        RoutingPolicy(
            registry,
            value,
            FakeHealth(statuses),
        ).choose(
            requested_model="zhipu/glm-5.3-flash",
            evaluation=Evaluation(
                "code",
                None,
                0.95,
                "explicit_validation",
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
                tool_choice=True,
                tool_choice_mode="auto",
            ),
            conversation=None,
        )
    )
    assert explicit.endpoint.id == "zhipu-glm-5.3-flash"
    assert explicit.reason == "explicit_model"
    with pytest.raises(NoEligibleModelError):
        run(
            RoutingPolicy(
                registry,
                value,
                FakeHealth(statuses),
            ).choose(
                requested_model="zhipu/glm-5.3-flash",
                evaluation=Evaluation(
                    "code",
                    None,
                    0.95,
                    "unsupported_tool_choice",
                ),
                prompt_tokens=100,
                output_reserve_tokens=100,
                modalities={"text"},
                has_tools=True,
                required_capabilities=RequestCapabilities(
                    protocol="chat",
                    tools=True,
                    tool_choice=True,
                    tool_choice_mode="required",
                ),
                conversation=None,
            )
        )


def test_subscription_endpoint_does_not_reserve_usd_budget(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("codex-pro-gpt-5.6-sol")
    assert endpoint is not None
    value = settings(tmp_path)
    budget = CloudBudget(InMemoryStateStore(), value)
    reservation = run(
        budget.reserve(
            endpoint,
            request_id="subscription-request",
            prompt_tokens=100000,
            output_reserve_tokens=10000,
        )
    )
    assert reservation is None


def test_auto_tool_request_falls_back_local_when_sol_is_rate_limited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry_value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(encoding="utf-8")
    )
    for endpoint in registry_value["endpoints"]:
        if endpoint["id"] == "codex-pro-gpt-5.6-sol":
            endpoint["auto_candidate"] = True
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            registry_value,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["openai-codex", "deepseek"],
                "allowed_models": [
                    "codex-pro/gpt-5.6-sol",
                    "deepseek/deepseek-v4-flash",
                ],
            }
        }
    )
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv(
        "AI_ROUTER_LITELLM_MASTER_KEY",
        "internal-key",
    )
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "subscription-fallback.jsonl"),
    )
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    statuses = {}
    for endpoint in registry.endpoints:
        workers = None
        if endpoint.backend_type == "ai_pool":
            workers = ai_workers()
        elif endpoint.backend_type == "codex_pool":
            workers = [
                {
                    "worker_id": "codex-primary",
                    "ready": True,
                    "state": "available",
                    "safe_context_tokens": 131072,
                    "api_base": (
                        "http://127.0.0.1:14010"
                        "/v1/accounts/primary"
                    ),
                }
            ]
        statuses[endpoint.id] = healthy(
            endpoint.id,
            context=endpoint.safe_context_tokens,
            workers=workers,
        )
    fake_health = FakeHealth(statuses)
    runtime.health = fake_health
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    calls = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.port == 14010:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "quota",
                        "code": "codex_rate_limited",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "LOCAL_FALLBACK_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "Inspect this repository"}
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "git.status",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "LOCAL_FALLBACK_OK"
    )
    assert response.headers["X-1Panel-Route-Node"] != "codex-pro"
    assert len(calls) == 2
    assert fake_health.failed == ["codex-primary"]
    run(runtime.internal_client.aclose())


def test_ai_responses_route_binds_a_physical_worker(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    status = healthy(
        endpoint.id,
        context=endpoint.safe_context_tokens,
        workers=ai_workers(),
    )
    decision = run(
        RoutingPolicy(
            registry,
            settings(tmp_path),
            FakeHealth({endpoint.id: status}),
        ).choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("general", None, 1.0, "test"),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="responses",
                tools=True,
            ),
            conversation=None,
        )
    )
    assert decision.deployment_id == "worker-priority-1"
    assert decision.upstream_api_base == "http://127.0.0.1:18111/v1"
    assert decision.native_or_adapter == "native"


def test_ai_large_context_excludes_64k_physical_worker(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    workers = [
        {
            "worker_id": "worker-64k",
            "port": 18110,
            "priority": 0,
            "ready": True,
            "state": "available",
            "safe_context_tokens": 65536,
        },
        {
            "worker_id": "worker-256k",
            "port": 18111,
            "priority": 1,
            "ready": True,
            "state": "available",
            "safe_context_tokens": 262144,
        },
    ]
    decision = run(
        RoutingPolicy(
            registry,
            settings(tmp_path),
            FakeHealth(
                {
                    endpoint.id: healthy(
                        endpoint.id,
                        context=262144,
                        workers=workers,
                    )
                }
            ),
        ).choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation("long-context", None, 1.0, "test"),
            prompt_tokens=110000,
            output_reserve_tokens=1024,
            modalities={"text"},
            has_tools=True,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                tools=True,
            ),
            conversation=None,
        )
    )
    assert decision.deployment_candidates == (
        ("worker-256k", "http://127.0.0.1:18111/v1"),
    )


def test_existing_conversation_does_not_call_evaluator_each_turn() -> None:
    async def fail_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("evaluator should not be called")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_request))
    evaluator = TaskEvaluator(
        {
            "enabled": True,
            "model_id": "small-router",
            "evaluate_task_changes": True,
        },
        internal_base_url="http://litellm",
        internal_api_key="internal",
        client=client,
    )
    result = run(
        evaluator.evaluate(
            {"messages": [{"role": "user", "content": "continue"}]},
            headers={},
            api_kind="chat",
            prompt_tokens=10,
            current_task="code",
            is_new_conversation=False,
        )
    )
    assert result.task == "code"
    assert result.reason == "conversation_task"
    run(client.aclose())


def test_complex_code_heuristic_prefers_subscription_frontier() -> None:
    async def fail_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("strong complex-code signals should not call the model")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_request))
    evaluator = TaskEvaluator(
        {
            "enabled": True,
            "model_id": "small-router",
            "prefer_frontier_for_complex_code": True,
        },
        internal_base_url="http://litellm",
        internal_api_key="internal",
        client=client,
    )
    result = run(
        evaluator.evaluate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Review a complex multi-file security-sensitive "
                            "distributed system change."
                        ),
                    }
                ]
            },
            headers={},
            api_kind="chat",
            prompt_tokens=30,
            current_task=None,
            is_new_conversation=True,
        )
    )
    assert result.task == "code"
    assert result.preferred_tier == "subscription-frontier"
    assert result.reason == "complex_code_heuristic"
    run(client.aclose())


def test_code_tool_mapping_precedes_long_context_classification() -> None:
    evaluator = TaskEvaluator(
        {
            "enabled": True,
            "model_id": "small-router",
            "long_context_threshold_tokens": 65536,
            "prefer_frontier_for_code_tools": True,
            "tool_task_mappings": {"code": ["git"]},
        },
        internal_base_url="http://litellm",
        internal_api_key="internal",
    )
    result = run(
        evaluator.evaluate(
            {
                "messages": [{"role": "user", "content": "Inspect the repository."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "git.status", "parameters": {}},
                    }
                ],
            },
            headers={},
            api_kind="chat",
            prompt_tokens=100000,
            current_task=None,
            is_new_conversation=True,
        )
    )
    assert result.task == "code"
    assert result.preferred_tier == "subscription-frontier"
    assert result.reason == "tool_mapping"
    run(evaluator.client.aclose())


def test_explicit_route_tier_is_validated() -> None:
    evaluator = TaskEvaluator(
        {"enabled": False},
        internal_base_url="http://litellm",
        internal_api_key="internal",
    )
    result = run(
        evaluator.evaluate(
            {"messages": [{"role": "user", "content": "hello"}]},
            headers={
                "x-1panel-route-task": "general",
                "x-1panel-route-tier": "cloud-frontier",
            },
            api_kind="chat",
            prompt_tokens=10,
            current_task=None,
            is_new_conversation=True,
        )
    )
    assert result.required_tier == "cloud-frontier"
    with pytest.raises(Exception):
        run(
            evaluator.evaluate(
                {"messages": [{"role": "user", "content": "hello"}]},
                headers={"x-1panel-route-tier": "invalid"},
                api_kind="chat",
                prompt_tokens=10,
                current_task=None,
                is_new_conversation=True,
            )
        )
    run(evaluator.client.aclose())


def test_request_modalities_detects_chat_and_responses_images() -> None:
    chat = request_modalities(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AA=="},
                        },
                    ],
                }
            ]
        },
        "chat",
    )
    responses = request_modalities(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                }
            ]
        },
        "responses",
    )
    assert chat == {"text", "image"}
    assert responses == {"text", "image"}


class CapturingTokenizer:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def apply_chat_template(
        self,
        messages,
        *,
        tools,
        tokenize,
        add_generation_prompt,
    ):
        self.messages = messages
        assert tools is None
        assert tokenize is True
        assert add_generation_prompt is True
        return [1] * 12


def test_multimodal_token_counter_does_not_tokenize_base64() -> None:
    tokenizer = CapturingTokenizer()
    counter = HuggingFaceTokenCounter(
        ROOT / "missing-tokenizer",
        image_token_estimate=1024,
    )
    counter._tokenizer = tokenizer
    encoded = "A" * 4_000_000
    tokens = counter.count_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ]
        },
        "chat",
    )
    rendered = json.dumps(tokenizer.messages)
    assert encoded not in rendered
    assert "<image>" in rendered
    assert tokens == 1036


def test_simple_token_counter_bounds_large_image_payload() -> None:
    counter = SimpleTokenCounter(image_token_estimate=1024)
    small = counter.count_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,AA=="
                            },
                        },
                    ],
                }
            ]
        },
        "chat",
    )
    large = counter.count_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/png;base64,"
                                    + "A" * 4_000_000
                                )
                            },
                        },
                    ],
                }
            ]
        },
        "chat",
    )
    assert large == small
    assert large < 2048


def test_multimodal_token_counter_redacts_nested_source_data() -> None:
    tokenizer = CapturingTokenizer()
    counter = HuggingFaceTokenCounter(
        ROOT / "missing-tokenizer",
        image_token_estimate=1024,
    )
    counter._tokenizer = tokenizer
    encoded = "B" * 1_000_000
    tokens = counter.count_request(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        }
                    ],
                }
            ]
        },
        "chat",
    )
    rendered = json.dumps(tokenizer.messages)
    assert encoded not in rendered
    assert "<image>" in rendered
    assert tokens == 1036


def test_evaluator_does_not_forward_base64_media() -> None:
    encoded = "C" * 4_000_000
    captured: dict = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "task": "general",
                                    "required_tier": None,
                                    "preferred_tier": None,
                                    "confidence": 1,
                                    "reason": "visual request",
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    evaluator = TaskEvaluator(
        {
            "enabled": True,
            "model_id": "small-router",
            "confidence_threshold": 0.9,
            "evaluate_task_changes": True,
        },
        internal_base_url="http://litellm",
        internal_api_key="internal",
        client=client,
    )
    result = run(
        evaluator.evaluate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        "data:image/png;base64,"
                                        + encoded
                                    )
                                },
                            },
                        ],
                    }
                ]
            },
            headers={},
            api_kind="chat",
            prompt_tokens=1024,
            current_task=None,
            is_new_conversation=True,
        )
    )
    evaluator_prompt = captured["messages"][1]["content"]
    assert encoded not in evaluator_prompt
    assert "<image>" in evaluator_prompt
    assert len(evaluator_prompt) < 2000
    assert result.task == "general"
    run(client.aclose())


def test_evaluator_capacity_acquisition_does_not_wait(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "evaluator-capacity-audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=Registry(ROOT / "config" / "registry.yaml"),
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    endpoint = runtime.registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers(),
            )
        }
    )
    runtime.policy = RoutingPolicy(
        runtime.registry,
        runtime.settings,
        runtime.health,
    )
    held = []
    for index, worker in enumerate(ai_workers()):
        lease = run(runtime.scheduler.begin_request(None))
        run(
            runtime.scheduler.acquire_deployment(
                lease,
                worker["worker_id"],
                f"held-request-{index}",
                timeout_seconds=0,
                affinity_priority=False,
                capacity=1,
            )
        )
        held.append(lease)
    evaluator = run(runtime.scheduler.begin_request(None))
    started = time.monotonic()
    with pytest.raises(QueueTimeoutError):
        run(
            _acquire_internal_model(
                runtime,
                lease=evaluator,
                request_id="evaluator-request",
                model_id="ai-qwen38-27b",
                wait=False,
            )
        )
    assert time.monotonic() - started < 0.5
    for lease in held:
        run(lease.release())
    run(evaluator.release())
    run(runtime.close())


def test_validated_vision_endpoints_are_registered_for_images() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    expected = {
        "ai-qwen38-27b",
        "ivan-qwen38-flash-128k",
        "amd-qwen38-rocmfpx-128k",
        "codex-pro-gpt-5.6-sol",
        "zhipu-glm-5.3-flash",
    }
    actual = {
        endpoint.id
        for endpoint in registry.endpoints
        if "image" in endpoint.modalities
    }
    assert actual == expected


def test_models_endpoint_reports_vision_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=Registry(ROOT / "config" / "registry.yaml"),
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers()
                if endpoint.backend_type == "ai_pool"
                else None,
            )
            for endpoint in runtime.registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        runtime.registry,
        runtime.settings,
        runtime.health,
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer client-key"},
        )
    assert response.status_code == 200
    models = {
        item["id"]: item
        for item in response.json()["data"]
    }
    assert models["auto"]["supportsImages"] is True
    assert "image" in models["auto"]["input_modalities"]
    assert models[
        "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF"
    ]["supportsImages"] is True
    assert models[
        "huihui/Qwen3.8-27B-Q4-DFlash2"
    ]["supportsImages"] is True
    assert models[
        "RadixArk/Qwen3.8-Flash-Next-NVFP4"
    ]["supportsImages"] is False
    assert models["zhipu/glm-5.3-flash"]["supportsImages"] is True
    assert models["zhipu/glm-5.3-flash"]["capabilities"][
        "tool_choice_modes"
    ] == ["auto"]
    run(runtime.close())


def test_large_base64_image_bypasses_text_tpm_and_routes_to_vision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "vision-audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(image_token_estimate=1024),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers()
                if endpoint.backend_type == "ai_pool"
                else [
                    {
                        "worker_id": "codex-primary",
                        "ready": True,
                        "state": "available",
                        "safe_context_tokens": 131072,
                        "api_base": (
                            "http://127.0.0.1:14010"
                            "/v1/accounts/primary"
                        ),
                    }
                ]
                if endpoint.backend_type == "codex_pool"
                else None,
            )
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    image_buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(
        image_buffer,
        format="BMP",
    )
    encoded = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    local_vision_models = {
        "huihui/Qwen3.8-27B-Q4-DFlash2",
        "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF",
        "Qwen/Qwen3.8-Flash-Next-ROCmFP4-FAST-imatrix-MTP",
    }

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] in local_vision_models
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        assert image_url.endswith(encoded)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-vision",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "VISION_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        "data:image/png;base64,"
                                        + encoded
                                    )
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "VISION_OK"
    assert response.headers["x-1panel-route-node"] in {
        "ai",
        "ivan",
        "amd",
    }
    run(runtime.internal_client.aclose())


def test_ai_image_is_resized_for_inference_and_original_is_archived(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "vision-training-audit.jsonl"),
    )
    training_key = tmp_path / "training.key"
    training_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_DB_PATH",
        str(tmp_path / "training.sqlite3"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_KEY_PATH",
        str(training_key),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(image_token_estimate=1024),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers(),
            )
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    original_buffer = io.BytesIO()
    Image.new("RGB", (2048, 1024), "red").save(
        original_buffer,
        format="PNG",
    )
    original_url = (
        "data:image/png;base64,"
        + base64.b64encode(original_buffer.getvalue()).decode("ascii")
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == endpoint.provider_model
        routed_url = payload["messages"][0]["content"][1][
            "image_url"
        ]["url"]
        image_bytes = base64.b64decode(routed_url.split(",", 1)[1])
        with Image.open(io.BytesIO(image_bytes)) as routed_image:
            assert routed_image.size == (1024, 512)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-ai-vision",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "VISION_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {"url": original_url},
                            },
                        ],
                    }
                ],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.headers["x-1panel-image-resized"] == "1"
    assert response.headers[
        "x-1panel-route-deployment-profile"
    ] == "p40-qwen38-64k"
    assert response.headers["x-1panel-vision-status"] == "experimental"
    assert runtime.training is not None
    output_path = tmp_path / "vision-training-export.jsonl"
    assert run(runtime.training.export_jsonl(str(output_path))) == 1
    record = json.loads(
        output_path.read_text(encoding="utf-8").strip()
    )["payload"]
    received_url = record["request"]["received_body"]["messages"][0][
        "content"
    ][1]["image_url"]["url"]
    routed_url = record["routing_attempts"][0]["routed_body"][
        "messages"
    ][0]["content"][1]["image_url"]["url"]
    assert received_url == original_url
    assert routed_url != original_url
    run(runtime.internal_client.aclose())


def test_explicit_ai_rejects_remote_image_without_fetching_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "remote-image-audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers(),
            )
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )

    async def upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("remote image must not reach AI worker")

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://example.invalid/image.png"
                                },
                            },
                        ],
                    }
                ],
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == (
        "ai_image_requires_embedded_data"
    )
    run(runtime.internal_client.aclose())


def test_auto_vision_workspace_failure_tries_p40_then_v100_then_falls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    ai = registry.by_id("ai-qwen38-27b")
    assert ai is not None
    ai.quality["general"] = 1000
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    audit_path = tmp_path / "vision-fallback-audit.jsonl"
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(audit_path))
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers()
                if endpoint.backend_type == "ai_pool"
                else None,
            )
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.port in {18110, 18111}:
            return httpx.Response(
                500,
                headers={"content-type": "application/json"},
                json={
                    "error": {
                        "message": (
                            "failed to find a memory slot "
                            "for batch of size 920"
                        )
                    }
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-vision-fallback",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "FALLBACK_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    image_buffer = io.BytesIO()
    Image.new("RGB", (128, 128), "red").save(
        image_buffer,
        format="PNG",
    )
    image_url = (
        "data:image/png;base64,"
        + base64.b64encode(image_buffer.getvalue()).decode("ascii")
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    }
                ],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "FALLBACK_OK"
    )
    assert response.headers["x-1panel-route-node"] in {"ivan", "amd"}
    assert [httpx.URL(item).port for item in calls[:2]] == [18111, 18110]
    assert runtime.health.failed == [
        "worker-priority-1:image",
        "worker-priority-0:image",
    ]
    assert audit_path.read_text(encoding="utf-8").count(
        "vision_deployment_failed"
    ) == 2
    run(runtime.internal_client.aclose())


def test_request_body_size_limit_is_independent_from_tpm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    value = settings(tmp_path)
    value.write_runtime({"limits": {"max_request_bytes": 256}})
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "size-limit-audit.jsonl"),
    )
    runtime = build_runtime(
        settings=value,
        registry=Registry(ROOT / "config" / "registry.yaml"),
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "A" * 512}
                ],
            },
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.json()["error"]["details"] == {
        "max_request_bytes": 256
    }
    run(runtime.close())


def test_sse_accumulator_handles_split_events() -> None:
    accumulator = SSEAccumulator()
    accumulator.feed(b'data: {"id":"chat-1","choices":[{"delta":{"content":"hel')
    accumulator.feed(b'lo"}}]}\n\n')
    accumulator.feed(
        b'data: {"id":"chat-1","choices":[{"delta":{"content":" world"}}]}\n\n'
    )
    accumulator.finish()
    assert accumulator.response_id == "chat-1"
    assert accumulator.assistant_message() == {
        "role": "assistant",
        "content": "hello world",
    }


def test_chat_tool_history_repairs_unique_missing_id() -> None:
    normalized = normalize_request(
        {
            "tools": [{"type": "function", "function": {"name": "search"}}],
            "messages": [
                {"role": "user", "content": "find it"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-search",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "search",
                    "content": "found",
                },
            ],
        },
        "chat",
    )
    assert normalized.repairs == 1
    assert normalized.body["messages"][2]["tool_call_id"] == "call-search"
    assert normalized.required.tools is True


def test_chat_tool_history_uses_function_name_before_global_match() -> None:
    normalized = normalize_request(
        {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-a",
                            "type": "function",
                            "function": {"name": "alpha", "arguments": "{}"},
                        },
                        {
                            "id": "call-b",
                            "type": "function",
                            "function": {"name": "beta", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "name": "beta", "content": "b"},
                {
                    "role": "tool",
                    "tool_call_id": "call-a",
                    "content": "a",
                },
            ]
        },
        "chat",
    )
    assert normalized.repairs == 1
    assert normalized.body["messages"][1]["tool_call_id"] == "call-b"


def test_chat_tool_history_rejects_parallel_ambiguity() -> None:
    with pytest.raises(InvalidToolHistoryError) as captured:
        normalize_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": "{\"secret\":\"a\"}",
                                },
                            },
                            {
                                "id": "call-b",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": "{\"secret\":\"b\"}",
                                },
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "search",
                        "content": "result",
                    },
                ]
            },
            "chat",
        )
    assert captured.value.details == {
        "item_index": 1,
        "candidate_count": 2,
        "reason": "ambiguous_missing_tool_call_id",
    }
    assert "secret" not in str(captured.value.details)


def test_chat_tool_history_rejects_duplicate_result() -> None:
    with pytest.raises(InvalidToolHistoryError) as captured:
        normalize_request(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-a",
                        "content": "first",
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-a",
                        "content": "duplicate",
                    },
                ]
            },
            "chat",
        )
    assert captured.value.details["reason"] == (
        "unknown_or_duplicate_tool_result"
    )


def test_responses_tool_history_repairs_unique_call_id() -> None:
    normalized = normalize_request(
        {
            "input": [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "output": "done",
                },
            ]
        },
        "responses",
    )
    assert normalized.repairs == 1
    assert normalized.body["input"][1]["call_id"] == "call-1"


def test_sse_accumulator_rebuilds_chat_tool_calls() -> None:
    accumulator = SSEAccumulator("chat")
    accumulator.feed(
        (
            'data: {"id":"chat-tools","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"id":"call-1","type":"function","function":'
            '{"name":"lookup","arguments":"{\\\"city\\\":"}}]}}]}\n\n'
        ).encode()
    )
    accumulator.feed(
        (
            'data: {"id":"chat-tools","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"function":{"arguments":"\\\"Paris\\\"}"}}]}}]}\n\n'
        ).encode()
    )
    accumulator.finish()
    assert accumulator.assistant_items() == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": "{\"city\":\"Paris\"}",
                    },
                }
            ],
        }
    ]


def test_sse_accumulator_rebuilds_responses_function_call() -> None:
    accumulator = SSEAccumulator("responses")
    accumulator.feed(
        (
            'data: {"type":"response.output_item.added","output_index":0,'
            '"item":{"type":"function_call","id":"fc-1","call_id":"call-1",'
            '"name":"lookup","arguments":""}}\n\n'
        ).encode()
    )
    accumulator.feed(
        (
            'data: {"type":"response.function_call_arguments.delta",'
            '"item_id":"fc-1","output_index":0,"delta":"{\\\"city\\\":"}\n\n'
        ).encode()
    )
    accumulator.feed(
        (
            'data: {"type":"response.function_call_arguments.delta",'
            '"item_id":"fc-1","output_index":0,"delta":"\\\"Paris\\\"}"}\n\n'
        ).encode()
    )
    accumulator.finish()
    assert accumulator.assistant_items() == [
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{\"city\":\"Paris\"}",
        }
    ]


def test_sse_accumulator_detects_protocol_completion_events() -> None:
    chat = SSEAccumulator("chat")
    chat.feed(b"data: [DONE]\n\n")
    assert chat.completed is True

    responses = SSEAccumulator("responses")
    responses.feed(
        b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
    )
    assert responses.completed is True


def test_response_history_preserves_native_function_call_items() -> None:
    payload = json.dumps(
        {
            "output": [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ]
        }
    ).encode()
    assert assistant_items_from_response(payload, "responses") == [
        {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{}",
        }
    ]


def test_history_identity_includes_tool_call_ids() -> None:
    first = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        }
    ]
    second = json.loads(json.dumps(first))
    second[0]["tool_calls"][0]["id"] = "call-b"
    assert history_identities(first) != history_identities(second)


def test_compaction_recent_window_keeps_tool_transaction_atomic() -> None:
    compactor = ContextCompactor(
        SimpleTokenCounter(),
        CapsuleCipher(Fernet.generate_key().decode()),
        internal_base_url="http://litellm",
        internal_api_key="internal",
        model_id="compactor",
    )
    messages = [
        {"role": "user", "content": "x" * 6000}
        for _index in range(5)
    ]
    messages.extend(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {"name": "a", "arguments": "{}"},
                    },
                    {
                        "id": "call-b",
                        "type": "function",
                        "function": {"name": "b", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "content": "a",
            },
            {
                "role": "tool",
                "tool_call_id": "call-b",
                "content": "b",
            },
            {"role": "user", "content": "continue"},
        ]
    )
    older, recent = compactor._partition_recent(messages, 2048, "chat")
    assert [item.get("role") for item in recent[-4:]] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert not any(item.get("role") == "assistant" for item in older)
    run(compactor.client.aclose())


def test_endpoint_capability_matrix_is_protocol_aware() -> None:
    capabilities = EndpointCapabilities(
        chat=True,
        responses="native",
        tools="parallel",
        tool_choice=True,
        tool_choice_modes=("auto",),
        structured_output=("json_object", "json_schema"),
        streaming=True,
    )
    assert capabilities.supports(
        RequestCapabilities(
            protocol="responses",
            tools=True,
            parallel_tools=True,
            tool_choice=True,
            tool_choice_mode="auto",
            structured_output="json_schema",
            streaming=True,
        )
    )
    assert not capabilities.supports(
        RequestCapabilities(
            protocol="chat",
            tools=True,
            tool_choice=True,
            tool_choice_mode="required",
        )
    )
    assert not EndpointCapabilities(
        chat=True,
        responses="none",
    ).supports(RequestCapabilities(protocol="responses"))


def test_request_capabilities_preserve_tool_choice_mode() -> None:
    automatic = normalize_request(
        {"messages": [], "tool_choice": "auto"},
        "chat",
        validate_history=False,
    )
    required = normalize_request(
        {"messages": [], "tool_choice": "required"},
        "chat",
        validate_history=False,
    )
    function = normalize_request(
        {
            "messages": [],
            "tool_choice": {
                "type": "function",
                "function": {"name": "lookup"},
            },
        },
        "chat",
        validate_history=False,
    )
    assert automatic.required.tool_choice_mode == "auto"
    assert required.required.tool_choice_mode == "required"
    assert function.required.tool_choice_mode == "function"


def test_local_responses_format_is_mirrored_for_backend_compatibility() -> None:
    payload = {
        "text": {
            "format": {
                "type": "json_schema",
                "name": "result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                    },
                },
            }
        }
    }
    _mirror_responses_format(payload)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "result",
            "schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                },
            },
            "strict": True,
        },
    }


def test_capsule_cipher_rejects_wrong_key() -> None:
    first = CapsuleCipher(Fernet.generate_key().decode())
    second = CapsuleCipher(Fernet.generate_key().decode())
    encrypted = first.encrypt([{"role": "user", "content": "state"}])
    with pytest.raises(Exception):
        second.decrypt(encrypted)


def test_cloud_budget_reserves_commits_and_caps(tmp_path: Path) -> None:
    value = settings(tmp_path)
    runtime_override = value.value
    runtime_override["cloud"] = {
        "enabled": True,
        "auto_escalate": True,
        "monthly_budget": 0.001,
        "allowed_providers": ["test"],
        "allowed_models": ["cloud/test"],
    }
    value.write_runtime(runtime_override)
    store = InMemoryStateStore()
    budget = CloudBudget(store, value)
    endpoint = Endpoint(
        id="cloud-test",
        public_model="cloud/test",
        provider_model="cloud/test",
        api_base="https://example.invalid/v1",
        node="cloud",
        role="responder",
        tier="cloud-frontier",
        tier_rank=40,
        modalities=("text",),
        tasks=("general",),
        safe_context_tokens=1000000,
        configured_context_tokens=1000000,
        max_concurrency=10,
        backend_type="openai",
        health_url="https://example.invalid/health",
        cloud=True,
        quality={"general": 100},
        metadata={
            "provider": "test",
            "input_cost_per_million_usd": 1,
            "output_cost_per_million_usd": 1,
        },
    )
    reservation = run(
        budget.reserve(
            endpoint,
            request_id="request-1",
            prompt_tokens=400,
            output_reserve_tokens=400,
        )
    )
    run(budget.commit(reservation))
    with pytest.raises(Exception):
        run(
            budget.reserve(
                endpoint,
                request_id="request-2",
                prompt_tokens=400,
                output_reserve_tokens=400,
            )
        )


def test_public_api_explicit_model_proxy(tmp_path: Path, monkeypatch) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    training_key = tmp_path / "training.key"
    training_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_DB_PATH",
        str(tmp_path / "training.sqlite3"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_KEY_PATH",
        str(training_key),
    )

    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        }
    )
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == endpoint.provider_model
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    assert response.headers["x-1panel-route-node"] == "edge"
    assert response.headers["x-1panel-conversation-mode"] == "inferred"
    assert response.headers["x-1panel-conversation-id"].startswith(
        "inferred-"
    )
    assert runtime.training is not None
    training_status = run(runtime.training.status())
    assert training_status["records"] == 1
    assert training_status["trainable_records"] == 1
    output_path = tmp_path / "training-export.jsonl"
    assert run(runtime.training.export_jsonl(str(output_path))) == 1
    training_record = json.loads(
        output_path.read_text(encoding="utf-8").strip()
    )["payload"]
    assert training_record["request"]["received_body"]["messages"] == [
        {"role": "user", "content": "hello"}
    ]
    assert training_record["request"]["effective_body"]["messages"] == [
        {"role": "user", "content": "hello"}
    ]
    assert training_record["routing_attempts"][0]["routed_body"][
        "messages"
    ] == [{"role": "user", "content": "hello"}]
    assert training_record["response"]["body"]["value"]["choices"][0][
        "message"
    ]["content"] == "ok"
    assert b"hello" not in (tmp_path / "training.sqlite3").read_bytes()
    run(runtime.internal_client.aclose())


def test_streaming_response_is_written_to_training_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "stream-audit.jsonl"),
    )
    training_key = tmp_path / "training.key"
    training_key.write_bytes(Fernet.generate_key())
    database_path = tmp_path / "training.sqlite3"
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_DB_PATH",
        str(database_path),
    )
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_KEY_PATH",
        str(training_key),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        }
    )
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)

    async def upstream(_request: httpx.Request) -> httpx.Response:
        content = (
            'data: {"id":"chatcmpl-stream","choices":[{"delta":'
            '{"role":"assistant"}}]}\n\n'
            'data: {"id":"chatcmpl-stream","choices":[{"delta":'
            '{"content":"STREAM_SECRET_RESPONSE"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [
                    {"role": "user", "content": "STREAM_SECRET_PROMPT"}
                ],
                "stream": True,
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert "STREAM_SECRET_RESPONSE" in response.text
    assert runtime.training is not None
    status = run(runtime.training.status())
    assert status["records"] == 1
    assert status["trainable_records"] == 1
    output_path = tmp_path / "stream-export.jsonl"
    assert run(runtime.training.export_jsonl(str(output_path))) == 1
    payload = json.loads(
        output_path.read_text(encoding="utf-8").strip()
    )["payload"]
    assert payload["response"]["assistant_items"] == [
        {
            "role": "assistant",
            "content": "STREAM_SECRET_RESPONSE",
        }
    ]
    database_bytes = database_path.read_bytes()
    assert b"STREAM_SECRET_PROMPT" not in database_bytes
    assert b"STREAM_SECRET_RESPONSE" not in database_bytes
    run(runtime.internal_client.aclose())


def test_workbuddy_missing_tool_call_id_is_repaired_and_stays_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers()
                if endpoint.backend_type == "ai_pool"
                else None,
            )
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == (
            "RadixArk/Qwen3.8-Flash-Next-NVFP4"
        )
        assert payload["messages"][2]["tool_call_id"] == "call-lookup"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-workbuddy",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "LOCAL_TOOL_CHAIN_OK",
                        }
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
                "messages": [
                    {"role": "user", "content": "run lookup"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-lookup",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "lookup",
                        "content": "{\"ok\":true}",
                    },
                ],
            },
        )
    assert response.status_code == 200
    assert response.headers["x-1panel-route-node"] == "edge"
    assert response.headers["x-1panel-tool-history-repaired"] == "1"
    assert response.headers["x-1panel-protocol"] == "chat"
    run(runtime.internal_client.aclose())


def test_workbuddy_ambiguous_tool_history_is_rejected_before_upstream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    called = False

    async def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("ambiguous history must not reach upstream")

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-a",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            },
                            {
                                "id": "call-b",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "lookup",
                        "content": "result",
                    },
                ],
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_tool_history"
    assert response.json()["error"]["details"]["candidate_count"] == 2
    assert called is False
    run(runtime.internal_client.aclose())


def test_auto_spills_busy_edge_to_next_local_without_cooldown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "routing": {
                "new_request_capacity_wait_seconds": 0,
            }
        }
    )
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    statuses = {
        endpoint.id: healthy(
            endpoint.id,
            context=endpoint.safe_context_tokens,
            workers=ai_workers()
            if endpoint.backend_type == "ai_pool"
            else None,
        )
        for endpoint in registry.endpoints
    }
    fake_health = FakeHealth(statuses)
    runtime.health = fake_health
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)
    edge = registry.by_id("edge-qwen38-flash")
    assert edge is not None
    holder = run(runtime.scheduler.begin_request(None))
    run(
        runtime.scheduler.acquire_deployment(
            holder,
            edge.id,
            "edge-holder",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == (
            "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF"
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-spill",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "SPILLED_TO_AI",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.headers["x-1panel-route-node"] == "ivan"
    assert response.headers["x-1panel-route-reason"] == "capacity_spillover"
    assert response.headers["x-1panel-capacity-attempts"] == "2"
    assert float(response.headers["x-1panel-queue-wait-ms"]) < 1000
    assert fake_health.failed == []
    run(holder.release())
    run(runtime.internal_client.aclose())


def test_explicit_busy_model_returns_429_without_cooldown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "routing": {
                "affinity_capacity_wait_seconds": 0.01,
            }
        }
    )
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    training_key = tmp_path / "training.key"
    training_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_DB_PATH",
        str(tmp_path / "training.sqlite3"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_KEY_PATH",
        str(training_key),
    )
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    edge = registry.by_id("edge-qwen38-flash")
    assert edge is not None
    fake_health = FakeHealth(
        {
            edge.id: healthy(
                edge.id,
                context=edge.safe_context_tokens,
            )
        }
    )
    runtime.health = fake_health
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)
    holder = run(runtime.scheduler.begin_request(None))
    run(
        runtime.scheduler.acquire_deployment(
            holder,
            edge.id,
            "edge-holder",
            timeout_seconds=0.2,
            affinity_priority=False,
        )
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": edge.public_model,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "model_capacity_busy"
    assert response.headers["retry-after"] == "1"
    assert fake_health.failed == []
    assert runtime.training is not None
    training_status = run(runtime.training.status())
    assert training_status["records"] == 1
    assert training_status["trainable_records"] == 0
    assert training_status["incomplete_records"] == 0
    run(holder.release())
    run(runtime.internal_client.aclose())


def test_auto_uses_cloud_after_all_local_capacity_is_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["deepseek"],
                "allowed_models": ["deepseek/deepseek-v4-flash"],
            },
            "routing": {
                "new_request_capacity_wait_seconds": 0,
                "all_local_busy_policy": "cloud_or_429",
            },
        }
    )
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    statuses = {
        endpoint.id: healthy(
            endpoint.id,
            context=endpoint.safe_context_tokens,
            workers=ai_workers()
            if endpoint.backend_type == "ai_pool"
            else None,
        )
        for endpoint in registry.endpoints
    }
    fake_health = FakeHealth(statuses)
    runtime.health = fake_health
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)
    edge = registry.by_id("edge-qwen38-flash")
    assert edge is not None
    holders = []
    for deployment_id in (
        edge.id,
        "ivan-qwen38-flash-128k",
        "amd-qwen38-rocmfpx-128k",
        "worker-priority-0",
        "worker-priority-1",
    ):
        holder = run(runtime.scheduler.begin_request(None))
        run(
            runtime.scheduler.acquire_deployment(
                holder,
                deployment_id,
                f"holder-{deployment_id}",
                timeout_seconds=0.2,
                affinity_priority=False,
            )
        )
        holders.append(holder)

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "cloud-deepseek-v4-flash"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-cloud-fallback",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "CLOUD_CAPACITY_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.headers["x-1panel-route-node"] == "cloud"
    assert (
        response.headers["x-1panel-route-reason"]
        == "cloud_capacity_fallback"
    )
    assert response.headers["x-1panel-capacity-attempts"] == "5"
    assert fake_health.failed == []
    for holder in holders:
        run(holder.release())
    run(runtime.internal_client.aclose())


def test_auto_returns_429_when_all_local_busy_and_cloud_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        registry = Registry(ROOT / "config" / "registry.yaml")
        value = settings(tmp_path)
        monkeypatch.setenv(
            "AI_ROUTER_LITELLM_MASTER_KEY",
            "internal-key",
        )
        monkeypatch.setenv(
            "AI_ROUTER_STATE_KEY",
            Fernet.generate_key().decode(),
        )
        monkeypatch.setenv(
            "AI_ROUTER_AUDIT_PATH",
            str(tmp_path / "local-busy-audit.jsonl"),
        )
        runtime = build_runtime(
            settings=value,
            registry=registry,
            store=InMemoryStateStore(),
            token_counter=SimpleTokenCounter(),
        )
        runtime.health = FakeHealth(
            {
                endpoint.id: healthy(
                    endpoint.id,
                    context=endpoint.safe_context_tokens,
                    workers=ai_workers()
                    if endpoint.backend_type == "ai_pool"
                    else None,
                )
                for endpoint in registry.endpoints
            }
        )
        runtime.policy = RoutingPolicy(
            registry,
            runtime.settings,
            runtime.health,
        )
        holders = []
        for deployment_id in (
            "edge-qwen38-flash",
            "ivan-qwen38-flash-128k",
            "amd-qwen38-rocmfpx-128k",
            "worker-priority-0",
            "worker-priority-1",
        ):
            holder = await runtime.scheduler.begin_request(None)
            await runtime.scheduler.acquire_deployment(
                holder,
                deployment_id,
                f"holder-{deployment_id}",
                timeout_seconds=0.2,
                affinity_priority=False,
            )
            holders.append(holder)
        lease = await runtime.scheduler.begin_request(None)
        try:
            with pytest.raises(AllLocalCapacityBusyError):
                await _acquire_route_capacity(
                    runtime,
                    request_id="all-local-busy",
                    requested_model="auto",
                    evaluation=Evaluation("general", None, 1, "test"),
                    prompt_tokens=100,
                    output_reserve_tokens=16,
                    modalities={"text"},
                    has_tools=True,
                    required_capabilities=RequestCapabilities(
                        protocol="chat",
                        tools=True,
                    ),
                    conversation=None,
                    body={
                        "model": "auto",
                        "messages": [
                            {"role": "user", "content": "hello"}
                        ],
                    },
                    api_kind="chat",
                    lease=lease,
                    excluded_endpoints=set(),
                    excluded_deployments=set(),
                    capacity_attempts=0,
                    queue_wait_ms=0,
                )
        finally:
            await lease.release()
            for holder in holders:
                await holder.release()
            await runtime.internal_client.aclose()

    run(scenario())


def test_eight_parallel_auto_capacity_selections_stay_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        registry = Registry(ROOT / "config" / "registry.yaml")
        value = settings(tmp_path)
        value.write_runtime(
            {
                "cloud": {
                    "enabled": True,
                    "auto_escalate": True,
                    "monthly_budget": 5,
                    "allowed_providers": ["deepseek"],
                    "allowed_models": ["deepseek/deepseek-v4-flash"],
                },
                "routing": {
                    "new_request_capacity_wait_seconds": 0,
                    "all_local_busy_policy": "cloud_or_429",
                },
            }
        )
        monkeypatch.setenv(
            "AI_ROUTER_LITELLM_MASTER_KEY",
            "internal-key",
        )
        monkeypatch.setenv(
            "AI_ROUTER_STATE_KEY",
            Fernet.generate_key().decode(),
        )
        monkeypatch.setenv(
            "AI_ROUTER_AUDIT_PATH",
            str(tmp_path / "parallel-audit.jsonl"),
        )
        runtime = build_runtime(
            settings=value,
            registry=registry,
            store=InMemoryStateStore(),
            token_counter=SimpleTokenCounter(),
        )
        statuses = {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=six_ai_workers()
                if endpoint.backend_type == "ai_pool"
                else None,
            )
            for endpoint in registry.endpoints
        }
        runtime.health = FakeHealth(statuses)
        runtime.policy = RoutingPolicy(
            registry,
            runtime.settings,
            runtime.health,
        )

        async def select(index: int):
            lease = await runtime.scheduler.begin_request(None)
            result = await _acquire_route_capacity(
                runtime,
                request_id=f"parallel-{index}",
                requested_model="auto",
                evaluation=Evaluation("general", None, 1, "test"),
                prompt_tokens=100,
                output_reserve_tokens=16,
                modalities={"text"},
                has_tools=False,
                required_capabilities=RequestCapabilities(
                    protocol="chat",
                ),
                conversation=None,
                body={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": f"request {index}"}
                    ],
                },
                api_kind="chat",
                lease=lease,
                excluded_endpoints=set(),
                excluded_deployments=set(),
                capacity_attempts=0,
                queue_wait_ms=0,
            )
            return lease, result

        selections = await asyncio.gather(
            *(select(index) for index in range(8))
        )
        nodes = Counter(
            result[0].endpoint.node
            for _lease, result in selections
        )
        assert nodes == Counter({"ai": 6, "edge": 1, "ivan": 1})
        ai_deployments = {
            result[0].deployment_id
            for _lease, result in selections
            if result[0].endpoint.node == "ai"
        }
        assert ai_deployments == {
            f"worker-{index}" for index in range(6)
        }
        for lease, result in selections:
            await runtime.budget.release(result[3])
            await lease.release()
        await runtime.internal_client.aclose()

    run(scenario())


def test_cloud_thinking_parameter_is_forwarded_via_extra_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("cloud-deepseek-v4-flash")
    assert endpoint is not None
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["deepseek"],
                "allowed_models": [endpoint.public_model],
            }
        }
    )
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        }
    )
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "thinking" not in payload
        assert payload["extra_body"]["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-cloud",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "CLOUD_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "disabled"},
                "max_tokens": 16,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "CLOUD_OK"
    run(runtime.internal_client.aclose())


def test_chat_model_migration_preserves_full_history_without_compaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    body = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ]
    }
    decision = RouteDecision(
        endpoint=endpoint,
        requested_model="auto",
        task="general",
        prompt_tokens=100,
        output_reserve_tokens=16,
        reason="affinity_spillover",
        affinity="migrated",
        score=1,
        migration=True,
    )
    routed, capsule = run(
        _prepare_routed_body(
            runtime,
            body,
            api_kind="chat",
            decision=decision,
            request_id="migration-test",
        )
    )
    assert routed == body
    assert capsule is None
    run(runtime.internal_client.aclose())


def test_chat_history_infers_same_conversation_and_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(audit_path))
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
                workers=ai_workers()
                if endpoint.backend_type == "ai_pool"
                else None,
            )
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["model"] == (
            "RadixArk/Qwen3.8-Flash-Next-NVFP4"
        )
        if calls == 1:
            assert len(payload["messages"]) == 1
            answer = "CACHE-ANCHOR"
            cached_tokens = 0
        else:
            assert len(payload["messages"]) == 3
            answer = "SECOND"
            cached_tokens = 60
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": f"chatcmpl-{calls}",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": answer,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {
                        "cached_tokens": cached_tokens,
                    },
                },
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "first"}],
                "max_tokens": 16,
            },
        )
        assert first.status_code == 200
        conversation_id = first.headers["x-1panel-conversation-id"]
        second = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "first"},
                    {
                        "role": "assistant",
                        "content": "CACHE-ANCHOR",
                    },
                    {"role": "user", "content": "second"},
                ],
                "max_tokens": 16,
            },
        )
    assert second.status_code == 200
    assert second.headers["x-1panel-conversation-id"] == conversation_id
    assert second.headers["x-1panel-conversation-mode"] == "inferred"
    assert second.headers["x-1panel-affinity"] == "hit"
    assert second.headers["x-1panel-route-deployment"] == (
        first.headers["x-1panel-route-deployment"]
    )
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    completed = [
        event
        for event in events
        if event["event"] == "request_completed"
    ]
    assert completed[-1]["cached_prompt_tokens"] == 60
    assert completed[-1]["cache_hit_ratio"] == 0.6
    run(runtime.internal_client.aclose())


def test_cache_metrics_accepts_common_usage_shapes() -> None:
    assert _cache_metrics(
        json.dumps(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 75},
                }
            }
        ).encode(),
        None,
    ) == (75, 0.75)
    assert _cache_metrics(
        None,
        {
            "input_tokens": 200,
            "cache_read_input_tokens": 120,
        },
    ) == (120, 0.6)
    assert _cache_metrics(
        json.dumps({"usage": {"prompt_tokens": 100}}).encode(),
        None,
        cached_prompt_tokens_fallback=64,
    ) == (64, 0.64)


def test_vllm_prefix_cache_delta_uses_native_counters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    fake_health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        },
        prefix_counters={
            endpoint.id: [
                {"queries": 1080, "hits": 564},
            ]
        },
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = fake_health
    decision = RouteDecision(
        endpoint=endpoint,
        requested_model=endpoint.public_model,
        task="general",
        prompt_tokens=100,
        output_reserve_tokens=16,
        reason="explicit_model",
        affinity="explicit",
        score=1,
    )
    assert run(
        _prefix_cache_delta(
            runtime,
            decision,
            {"queries": 1000, "hits": 500},
        )
    ) == 64
    run(runtime.internal_client.aclose())


def test_responses_previous_id_rebuilds_encrypted_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        }
    )
    runtime.policy = RoutingPolicy(registry, runtime.settings, runtime.health)
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert "previous_response_id" not in payload
        if calls == 1:
            assert payload["input"] == "first question"
            response_id = "resp-first"
            answer = "first answer"
        else:
            assert len(payload["input"]) == 3
            assert payload["input"][0]["role"] == "user"
            assert payload["input"][1]["role"] == "assistant"
            assert payload["input"][2]["role"] == "user"
            response_id = "resp-second"
            answer = "second answer"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": response_id,
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": answer}
                        ],
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        first = client.post(
            "/v1/responses",
            headers={
                "Authorization": "Bearer client-key",
                "X-1Panel-Conversation-ID": "conversation-1",
            },
            json={
                "model": endpoint.public_model,
                "input": "first question",
                "max_output_tokens": 16,
            },
        )
        assert first.status_code == 200
        second = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "previous_response_id": "resp-first",
                "input": "second question",
                "max_output_tokens": 16,
            },
        )
    assert second.status_code == 200
    assert calls == 2
    run(runtime.internal_client.aclose())


def test_responses_previous_id_repairs_function_call_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "responses-tools-audit.jsonl"),
    )
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: healthy(
                endpoint.id,
                context=endpoint.safe_context_tokens,
            )
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            assert payload["input"] == "lookup weather"
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "resp-tool-first",
                    "object": "response",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc-weather",
                            "call_id": "call-weather",
                            "name": "weather",
                            "arguments": "{\"city\":\"Paris\"}",
                        }
                    ],
                },
            )
        assert "previous_response_id" not in payload
        assert [item["type"] for item in payload["input"]] == [
            "message",
            "function_call",
            "function_call_output",
        ]
        assert payload["input"][2]["call_id"] == "call-weather"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "resp-tool-second",
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Weather is clear.",
                            }
                        ],
                    }
                ],
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    tools = [
        {
            "type": "function",
            "name": "weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                },
            },
        }
    ]
    app = create_app(runtime)
    with TestClient(app) as client:
        first = client.post(
            "/v1/responses",
            headers={
                "Authorization": "Bearer client-key",
                "X-1Panel-Conversation-ID": "responses-tool-chain",
            },
            json={
                "model": endpoint.public_model,
                "input": "lookup weather",
                "tools": tools,
            },
        )
        assert first.status_code == 200
        second = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "previous_response_id": "resp-tool-first",
                "input": [
                    {
                        "type": "function_call_output",
                        "output": "{\"temperature\":20}",
                    }
                ],
                "tools": tools,
            },
        )
    assert second.status_code == 200
    assert second.headers["x-1panel-tool-history-repaired"] == "1"
    assert second.headers["x-1panel-protocol"] == "responses"
    assert second.headers["x-1panel-protocol-mode"] == "native"
    assert calls == 2
    run(runtime.internal_client.aclose())


def test_control_api_rejects_invalid_runtime_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    app = create_control_app(runtime)
    with TestClient(app) as client:
        unauthorized = client.get("/api/settings")
        assert unauthorized.status_code == 401
        current = client.get(
            "/api/settings",
            headers={"Authorization": "Bearer admin-key"},
        )
        assert current.status_code == 200
        payload = current.json()["settings"]
        payload["routing"]["provider_priority"] = "balanced"
        valid = client.put(
            "/api/settings",
            headers={"Authorization": "Bearer admin-key"},
            json=payload,
        )
        assert valid.status_code == 200
        assert (
            valid.json()["settings"]["routing"]["provider_priority"]
            == "balanced"
        )
        payload = valid.json()["settings"]
        payload["routing"]["weights"]["quality"] = 1
        invalid = client.put(
            "/api/settings",
            headers={"Authorization": "Bearer admin-key"},
            json=payload,
        )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_settings"


def test_legacy_client_key_import_and_revocation_survive_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = InMemoryStateStore()
    value = settings(tmp_path)
    state_key = Fernet.generate_key().decode()
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "legacy-client-key")
    manager = ClientAccountManager(store, value, state_key)

    imported = run(manager.bootstrap_legacy())
    assert any(item["client_id"] == "1panel" for item in imported)
    policy, key_id = run(manager.authenticate("legacy-client-key"))
    assert policy.id == "1panel"
    assert key_id.startswith("legacy-")

    revoked = run(manager.revoke_key("1panel", key_id))
    assert revoked["status"] == "revoked"
    with pytest.raises(AuthenticationError):
        run(manager.authenticate("legacy-client-key"))

    restarted = ClientAccountManager(store, value, state_key)
    assert run(restarted.bootstrap_legacy()) == []
    with pytest.raises(AuthenticationError):
        run(restarted.authenticate("legacy-client-key"))


def test_managed_client_multiple_keys_share_policy_and_usage(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    manager = ClientAccountManager(
        store,
        settings(tmp_path),
        Fernet.generate_key().decode(),
    )
    account = run(
        manager.create_account(
            {
                "id": "home-assistant",
                "name": "Home Assistant",
                "enabled": True,
                "models": ["auto"],
                "rpm_limit": 30,
                "tpm_limit": 200000,
                "max_parallel_requests": 2,
            },
            allowed_models={"auto"},
        )
    )
    assert account["id"] == "home-assistant"
    first, first_secret = run(
        manager.create_key("home-assistant", "front-door")
    )
    second, second_secret = run(
        manager.create_key("home-assistant", "delivery")
    )
    assert first_secret.startswith("sk-1panel-")
    assert second_secret.startswith("sk-1panel-")
    assert first_secret != second_secret

    first_policy, first_key_id = run(manager.authenticate(first_secret))
    second_policy, second_key_id = run(manager.authenticate(second_secret))
    assert first_policy == second_policy
    assert first_policy.max_parallel_requests == 2
    assert first_key_id != second_key_id
    limiter = ClientLimiter(store)
    assert run(
        limiter.check_rate_limits(
            first_policy.id,
            prompt_tokens=100,
            rpm_limit=1,
            tpm_limit=1000,
        )
    ) == (True, None)
    assert run(
        limiter.check_rate_limits(
            second_policy.id,
            prompt_tokens=100,
            rpm_limit=1,
            tpm_limit=1000,
        )
    ) == (False, "rpm_limit_exceeded")

    run(
        manager.record_usage(
            client_id="home-assistant",
            key_id=first_key_id,
            status_code=200,
            input_tokens=120,
            output_tokens=30,
        )
    )
    run(
        manager.record_usage(
            client_id="home-assistant",
            key_id=second_key_id,
            status_code=429,
            input_tokens=80,
            output_tokens=0,
        )
    )
    listed = run(manager.list_accounts())
    current = next(item for item in listed if item["id"] == "home-assistant")
    assert current["usage_24h"] == {
        "requests": 2,
        "input_tokens": 200,
        "output_tokens": 30,
        "errors": 1,
    }
    assert all(item["last_used_at"] for item in current["keys"])

    stored = json.dumps(
        [item.value for item in store._values.values()],
        ensure_ascii=False,
    )
    assert first_secret not in stored
    assert second_secret not in stored
    assert "digest" not in json.dumps(listed)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {
                "id": "INVALID",
                "name": "Invalid",
                "models": ["auto"],
                "rpm_limit": 1,
                "tpm_limit": 1,
                "max_parallel_requests": 1,
            },
            "invalid_client_id",
        ),
        (
            {
                "id": "unknown-model",
                "name": "Unknown model",
                "models": ["missing"],
                "rpm_limit": 1,
                "tpm_limit": 1,
                "max_parallel_requests": 1,
            },
            "invalid_client_models",
        ),
        (
            {
                "id": "invalid-limit",
                "name": "Invalid limit",
                "models": ["auto"],
                "rpm_limit": 0,
                "tpm_limit": 1,
                "max_parallel_requests": 1,
            },
            "invalid_client_limits",
        ),
    ],
)
def test_client_account_validation(
    tmp_path: Path,
    payload: dict,
    code: str,
) -> None:
    manager = ClientAccountManager(
        InMemoryStateStore(),
        settings(tmp_path),
        Fernet.generate_key().decode(),
    )
    with pytest.raises(RouterError) as error:
        run(
            manager.create_account(
                payload,
                allowed_models={"auto"},
            )
        )
    assert error.value.code == code


def test_disabled_client_and_revoked_key_are_rejected_immediately(
    tmp_path: Path,
) -> None:
    manager = ClientAccountManager(
        InMemoryStateStore(),
        settings(tmp_path),
        Fernet.generate_key().decode(),
    )
    value = {
        "id": "shared-client",
        "name": "Shared Client",
        "enabled": True,
        "models": ["auto"],
        "rpm_limit": 10,
        "tpm_limit": 10000,
        "max_parallel_requests": 1,
    }
    run(
        manager.create_account(
            value,
            allowed_models={"auto"},
        )
    )
    key, secret = run(manager.create_key("shared-client", "primary"))
    run(manager.authenticate(secret))
    run(
        manager.update_account(
            "shared-client",
            {**value, "enabled": False},
            allowed_models={"auto"},
        )
    )
    with pytest.raises(AuthenticationError):
        run(manager.authenticate(secret))
    run(
        manager.update_account(
            "shared-client",
            {**value, "enabled": True},
            allowed_models={"auto"},
        )
    )
    run(manager.revoke_key("shared-client", key["key_id"]))
    with pytest.raises(AuthenticationError):
        run(manager.authenticate(secret))


def test_key_revocation_is_shared_by_independent_router_managers(
    tmp_path: Path,
) -> None:
    store = InMemoryStateStore()
    state_key = Fernet.generate_key().decode()
    first = ClientAccountManager(store, settings(tmp_path), state_key)
    second = ClientAccountManager(store, settings(tmp_path), state_key)
    value = {
        "id": "shared-router-client",
        "name": "Shared Router Client",
        "enabled": True,
        "models": ["auto"],
        "rpm_limit": 10,
        "tpm_limit": 10000,
        "max_parallel_requests": 1,
    }
    run(first.create_account(value, allowed_models={"auto"}))
    key, secret = run(first.create_key(value["id"], "primary"))
    assert run(second.authenticate(secret))[0].id == value["id"]
    run(first.revoke_key(value["id"], key["key_id"]))
    with pytest.raises(AuthenticationError):
        run(second.authenticate(secret))


def test_client_authentication_fails_closed_when_store_is_unavailable(
    tmp_path: Path,
) -> None:
    class FailingStore(InMemoryStateStore):
        async def get_json(self, key: str):
            raise OSError(f"unavailable: {key}")

    manager = ClientAccountManager(
        FailingStore(),
        settings(tmp_path),
        Fernet.generate_key().decode(),
    )
    with pytest.raises(RouterError) as error:
        run(manager.authenticate("any-key"))
    assert error.value.status_code == 503
    assert error.value.code == "auth_store_unavailable"


def test_usage_totals_normalize_chat_and_responses_usage() -> None:
    assert _usage_totals(
        json.dumps(
            {
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                }
            }
        ).encode(),
        None,
        prompt_tokens_fallback=1,
    ) == (120, 30)
    assert _usage_totals(
        None,
        {"input_tokens": 90, "output_tokens": 20},
        prompt_tokens_fallback=1,
    ) == (90, 20)


def test_control_client_management_api_returns_secret_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "legacy-client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    runtime = build_runtime(
        settings=settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    control = create_control_app(runtime)
    headers = {"Authorization": "Bearer admin-key"}
    with TestClient(control) as client:
        legacy = client.get("/api/clients", headers=headers)
        assert legacy.status_code == 200
        assert any(
            item["id"] == "1panel"
            for item in legacy.json()["clients"]
        )
        created = client.post(
            "/api/clients",
            headers=headers,
            json={
                "id": "ha-door",
                "name": "HA Door",
                "enabled": True,
                "models": ["auto"],
                "rpm_limit": 60,
                "tpm_limit": 500000,
                "max_parallel_requests": 2,
            },
        )
        assert created.status_code == 201
        generated = client.post(
            "/api/clients/ha-door/keys",
            headers=headers,
            json={"label": "nx4"},
        )
        assert generated.status_code == 201
        assert generated.headers["cache-control"] == "no-store"
        secret = generated.json()["api_key"]
        key_id = generated.json()["key"]["key_id"]
        listed = client.get("/api/clients", headers=headers)
        assert secret not in listed.text
        assert "digest" not in listed.text

    router = create_app(runtime)
    with TestClient(router) as client:
        accepted = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert accepted.status_code == 200

    with TestClient(control) as client:
        revoked = client.post(
            f"/api/clients/ha-door/keys/{key_id}/revoke",
            headers=headers,
        )
        assert revoked.status_code == 200
        repeated = client.post(
            f"/api/clients/ha-door/keys/{key_id}/revoke",
            headers=headers,
        )
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "client_key_revoked"

    with TestClient(router) as client:
        rejected = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert rejected.status_code == 401
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert secret not in audit_text
    assert "client_key_created" in audit_text
    assert "client_key_revoked" in audit_text


def test_control_dashboard_aggregates_runtime_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    value = settings(tmp_path)
    value.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": True,
                "monthly_budget": 5,
                "allowed_providers": ["deepseek"],
                "allowed_models": ["deepseek/deepseek-v4-flash"],
            }
        }
    )
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    store = InMemoryStateStore()
    runtime = build_runtime(
        settings=value,
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    statuses = {}
    for endpoint in registry.endpoints:
        statuses[endpoint.id] = healthy(
            endpoint.id,
            context=endpoint.safe_context_tokens,
            workers=ai_workers() if endpoint.node == "ai" else None,
        )
    runtime.health = FakeHealth(statuses)
    runtime.audit.write(
        "request_started",
        request_id="running-request",
        client_id="1panel",
        requested_model="auto",
        selected_model="model-a",
        endpoint_id="endpoint-a",
        deployment_id="worker-a",
        node="ai",
        task="general",
        reason="quality_score",
        affinity="new",
        prompt_tokens=100,
        output_reserve_tokens=32,
        attempts=1,
    )
    runtime.audit.write(
        "request_completed",
        request_id="completed-request",
        client_id="1panel",
        requested_model="auto",
        selected_model="model-b",
        endpoint_id="endpoint-b",
        deployment_id="worker-b",
        node="edge",
        task="code",
        reason="quality_score",
        affinity="hit",
        prompt_tokens=200,
        output_reserve_tokens=64,
        attempts=1,
        capacity_attempts=2,
        queue_wait_ms=12.5,
        status_code=200,
        latency_ms=250.5,
    )
    run(
        store.set_json(
            f"router:cloud-budget:{datetime.now(timezone.utc):%Y-%m}",
            {"spent_usd": 1.25, "reservations": {}},
        )
    )
    run(
        store.set_json(
            "router:instance-state:router-api-local",
            {
                "instance_id": "router-api-local",
                "boot_id": "boot-local",
                "status": "running",
                "draining": False,
                "started_at": time.time() - 60,
                "updated_at": time.time(),
                "active_request_count": 1,
                "active_requests": [{"request_id": "running-request"}],
                "startup_cleanup": {"deployment_members": 2},
            },
        )
    )

    app = create_control_app(runtime)
    with TestClient(app) as client:
        unauthorized = client.get("/api/dashboard")
        assert unauthorized.status_code == 401
        response = client.get(
            "/api/dashboard",
            headers={"Authorization": "Bearer admin-key"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["healthy_endpoints"] == 7
    assert payload["summary"]["ready_workers"] == 2
    assert payload["summary"]["active_requests"] == 1
    assert payload["summary"]["success_rate"] == 1
    assert payload["cloud_budget"]["spent_usd"] == 1.25
    assert payload["router_instances"][0]["boot_id"] == "boot-local"
    assert payload["router_instances"][0]["startup_cleanup"][
        "deployment_members"
    ] == 2
    assert payload["requests"][0]["request_id"] == "completed-request"
    assert payload["requests"][0]["capacity_attempts"] == 2
    assert payload["requests"][0]["queue_wait_ms"] == 12.5
    assert payload["requests"][1]["status"] == "running"


class CharacterTokenCounter:
    def count_request(self, body: dict, _api_kind: str) -> int:
        return len(
            json.dumps(body.get("messages", []), ensure_ascii=False)
        )


def test_pilot_manifest_is_reproducible_and_expands_long_context() -> None:
    first = validate_manifest(pilot_manifest())
    second = validate_manifest(pilot_manifest())
    assert first["manifest_hash"] == second["manifest_hash"]
    assert len(first["cases"]) == 20
    long_case = next(
        item for item in first["cases"]
        if item["task"] == "long-context"
    )
    counter = CharacterTokenCounter()
    first_body, _ = expand_case(
        long_case,
        seed=first["seed"],
        token_counter=counter,
    )
    second_body, _ = expand_case(
        long_case,
        seed=first["seed"],
        token_counter=counter,
    )
    assert first_body == second_body
    assert counter.count_request(first_body, "chat") <= 70000
    assert "authoritative-record:" in first_body["messages"][0]["content"]


def test_pilot_oracle_accepts_json_code_fences() -> None:
    case = {
        "grading": {
            "mode": "json_subset",
            "expected": {"answer": 42},
            "rubric": "Exact integer answer.",
        }
    }
    assert oracle_result(case, "```json\n{\"answer\": 42}\n```") is True


def test_pilot_anonymization_and_terra_finalize(tmp_path: Path) -> None:
    manifest = validate_manifest(pilot_manifest())
    run_dir = tmp_path / "pilot-run"
    run_dir.mkdir()
    raw = {
        "benchmark_version": 2,
        "run_type": "pilot",
        "run_id": run_dir.name,
        "manifest_hash": manifest["manifest_hash"],
        "seed": manifest["seed"],
        "models": ["model-a", "model-b"],
        "anonymization_map": {"A": "model-b", "B": "model-a"},
        "projected_cloud_cost_usd": 0,
        "results": [
            {
                "case_id": "general-0",
                "task": "general",
                "turn": 1,
                "model": "model-a",
                "status_code": 200,
                "content": "{\"answer\":\"ok\"}",
                "error": None,
                "oracle_passed": True,
            },
            {
                "case_id": "general-0",
                "task": "general",
                "turn": 1,
                "model": "model-b",
                "status_code": 200,
                "content": "{\"answer\":\"ok\"}",
                "error": None,
                "oracle_passed": True,
            },
        ],
    }
    write_json(run_dir / "raw-results.json", raw)
    anonymous = anonymize_results(raw)
    write_json(run_dir / "anonymous-results.json", anonymous)
    assert "model-a" not in json.dumps(anonymous)
    verdict = {
        "judge_model": "gpt-5.6-terra",
        "manifest_hash": manifest["manifest_hash"],
        "confidence": 0.6,
        "case_reviews": [
            {
                "case_id": "general-0",
                "candidate": "A",
                "score": 80,
                "valid_case": True,
                "reason": "valid",
            }
        ],
        "candidate_scores": {
            "A": {
                "general": 80,
                "code": 70,
                "batch": 75,
                "long-context": 85,
            },
            "B": {
                "general": 90,
                "code": 85,
                "batch": 80,
                "long-context": 88,
            },
        },
        "routing_recommendation": {
            task: ["B", "A"]
            for task in ("general", "code", "batch", "long-context")
        },
        "judge_notes": ["pilot only"],
    }
    recommendation = finalize_verdict(
        run_dir=run_dir,
        verdict=verdict,
    )
    assert recommendation["apply_to_production"] is False
    assert recommendation["status"] == "provisional"
    assert recommendation["routing_recommendation"]["general"] == [
        "model-a",
        "model-b",
    ]
