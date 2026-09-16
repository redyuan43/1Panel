from __future__ import annotations

import asyncio
from pathlib import Path
import time

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import create_app as create_api_app
from ai_router.config import Registry
from ai_router.config import Settings
from ai_router.control import create_app as create_control_app
from ai_router.endpoint_config import EndpointConfigManager
from ai_router.errors import RouterError
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.types import EndpointStatus
from tests.test_core import SimpleTokenCounter


ROOT = Path(__file__).resolve().parents[1]


def run(value):
    return asyncio.run(value)


def healthy(endpoint_id: str) -> EndpointStatus:
    return EndpointStatus(
        endpoint_id=endpoint_id,
        healthy=True,
        checked_at=1,
    )


def test_endpoint_draft_requires_validation_before_activation() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    manager = EndpointConfigManager(InMemoryStateStore(), registry)
    endpoint_id = "edge-qwen38-flash"

    draft = run(
        manager.save_draft(
            endpoint_id,
            {
                "safe_context_tokens": 262144,
                "modalities": ["text"],
            },
            expected_revision=0,
            source="test",
        )
    )
    assert draft["revision"] == 1
    with pytest.raises(RouterError) as exc:
        run(
            manager.activate(
                endpoint_id,
                expected_revision=1,
                source="test",
            )
        )
    assert exc.value.code == "endpoint_not_validated"

    validated = run(
        manager.validate_draft(
            endpoint_id,
            expected_revision=1,
            status=healthy(endpoint_id),
            source="test",
        )
    )
    assert validated["validation"]["status"] == "passed"
    active = run(
        manager.activate(
            endpoint_id,
            expected_revision=2,
            source="test",
        )
    )
    assert active["revision"] == 3
    effective = run(manager.effective_registry()).by_id(endpoint_id)
    assert effective is not None
    assert effective.safe_context_tokens == 262144
    assert effective.modalities == ("text",)
    assert (
        effective.capabilities.validation_status
        == "live-validated-chat-responses-tools-structured"
    )


def test_endpoint_configuration_cannot_exceed_baseline() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    manager = EndpointConfigManager(InMemoryStateStore(), registry)
    endpoint_id = "edge-qwen38-flash"

    with pytest.raises(RouterError) as context_error:
        run(
            manager.save_draft(
                endpoint_id,
                {"safe_context_tokens": 500001},
                expected_revision=0,
                source="test",
            )
        )
    assert context_error.value.code == "invalid_endpoint_config"

    with pytest.raises(RouterError) as modality_error:
        run(
            manager.save_draft(
                endpoint_id,
                {"modalities": ["text", "audio"]},
                expected_revision=0,
                source="test",
            )
        )
    assert modality_error.value.code == "invalid_endpoint_config"


def test_endpoint_actions_are_immediate_and_revision_guarded() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    manager = EndpointConfigManager(InMemoryStateStore(), registry)
    endpoint_id = "ivan-qwen38-flash-128k"

    disabled = run(
        manager.action(
            endpoint_id,
            "disable",
            expected_revision=0,
            source="test",
        )
    )
    assert disabled["revision"] == 1
    effective = run(manager.effective_registry()).by_id(endpoint_id)
    assert effective is not None and effective.enabled is False

    with pytest.raises(RouterError) as conflict:
        run(
            manager.action(
                endpoint_id,
                "enable",
                expected_revision=0,
                source="test",
            )
        )
    assert conflict.value.code == "endpoint_revision_conflict"

    enabled = run(
        manager.action(
            endpoint_id,
            "enable",
            expected_revision=1,
            source="test",
        )
    )
    assert enabled["revision"] == 2
    restored = run(manager.effective_registry()).by_id(endpoint_id)
    assert restored is not None and restored.enabled is True


def test_unverified_endpoint_cannot_join_auto_without_validation(
    tmp_path: Path,
) -> None:
    registry_value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(encoding="utf-8")
    )
    for endpoint in registry_value["endpoints"]:
        if endpoint["id"] == "zhipu-glm-5.3-flash":
            endpoint["auto_candidate"] = False
            endpoint["capabilities"]["validation_status"] = (
                "official-documented-unverified"
            )
            endpoint["capabilities"]["validated_at"] = ""
    registry_path = tmp_path / "unverified-registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            registry_value,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_path)
    manager = EndpointConfigManager(InMemoryStateStore(), registry)

    with pytest.raises(RouterError) as exc:
        run(
            manager.action(
                "zhipu-glm-5.3-flash",
                "auto-enable",
                expected_revision=0,
                source="test",
            )
        )
    assert exc.value.code == "endpoint_not_validated"


def test_reset_removes_active_and_draft_overrides() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    manager = EndpointConfigManager(InMemoryStateStore(), registry)
    endpoint_id = "edge-qwen38-flash"
    run(
        manager.action(
            endpoint_id,
            "disable",
            expected_revision=0,
            source="test",
        )
    )
    run(
        manager.save_draft(
            endpoint_id,
            {"safe_context_tokens": 262144},
            expected_revision=1,
            source="test",
        )
    )
    revision = run(
        manager.reset(
            endpoint_id,
            expected_revision=2,
        )
    )
    assert revision == 3
    record = run(manager.records())[endpoint_id]
    assert record["has_override"] is False
    assert record["draft"] is None
    effective = run(manager.effective_registry()).by_id(endpoint_id)
    assert effective is not None
    assert effective.enabled is False
    assert effective.safe_context_tokens == 500000


class StaticHealth:
    async def statuses(self, endpoints, **_kwargs):
        return {
            item.id: EndpointStatus(
                endpoint_id=item.id,
                healthy=True,
                checked_at=time.time(),
                eligible_context_tokens=item.safe_context_tokens,
                detail=self._detail(item),
            )
            for item in endpoints
        }

    async def status(self, endpoint, **_kwargs):
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=time.time(),
            eligible_context_tokens=endpoint.safe_context_tokens,
            detail=self._detail(endpoint),
        )

    @staticmethod
    def _detail(endpoint):
        if not endpoint.metadata.get("lmcache_http_url"):
            return {}
        return {
            "lmcache": {
                "connector_active": True,
                "registered_count": 4,
                "expected_registrations": 4,
            }
        }

    async def in_cooldown(self, _endpoint_id):
        return False

    async def in_capability_cooldown(
        self,
        _deployment_id,
        _capability,
    ):
        return False


def test_control_actions_sync_two_runtimes_and_hide_disabled_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = InMemoryStateStore()
    registry = Registry(ROOT / "config" / "registry.yaml")
    runtime_settings = Settings(
        ROOT / "config" / "defaults.yaml",
        tmp_path / "settings.yaml",
    )
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    first = build_runtime(
        settings=runtime_settings,
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    second = build_runtime(
        settings=runtime_settings,
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    first.health = StaticHealth()
    first.policy.health = first.health
    second.health = StaticHealth()
    second.policy.health = second.health
    endpoint_id = "edge-qwen38-flash"
    public_model = registry.by_id(endpoint_id).public_model

    with TestClient(create_control_app(first)) as control:
        response = control.post(
            f"/api/endpoints/{endpoint_id}/actions/disable",
            headers={"Authorization": "Bearer admin-key"},
            json={"expected_revision": 0},
        )
        assert response.status_code == 200
        assert response.json()["active"]["revision"] == 1

    run(second.reload_endpoint_config())
    assert second.registry.by_id(endpoint_id).enabled is False

    with TestClient(create_api_app(second)) as api:
        models = api.get(
            "/v1/models",
            headers={"Authorization": "Bearer client-key"},
        )
        explicit = api.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": public_model,
                "messages": [
                    {"role": "user", "content": "hello"}
                ],
            },
        )
    assert models.status_code == 200
    model_ids = {item["id"] for item in models.json()["data"]}
    assert public_model not in model_ids
    assert "auto" in model_ids
    assert explicit.status_code == 503
    assert explicit.json()["error"]["code"] == "endpoint_disabled"
    run(first.close())
    run(second.close())


def test_control_manual_drain_is_idempotent_and_blocks_new_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = InMemoryStateStore()
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=Settings(
            ROOT / "config" / "defaults.yaml",
            tmp_path / "settings.yaml",
        ),
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = StaticHealth()
    runtime.policy.health = runtime.health
    endpoint_id = "ai-qwen38-27b"
    public_model = registry.by_id(endpoint_id).public_model
    run(
        store.set_json(
            "router:instance-state:test-router",
            {
                "instance_id": "test-router",
                "status": "running",
                "updated_at": time.time(),
                "active_requests": [
                    {
                        "request_id": "active-request",
                        "deployment_id": endpoint_id,
                        "started_at": time.time(),
                    }
                ],
            },
        )
    )
    headers = {"Authorization": "Bearer admin-key"}

    with TestClient(create_control_app(runtime)) as control:
        first = control.post(
            f"/api/endpoints/{endpoint_id}/actions/drain",
            headers=headers,
            json={},
        )
        second = control.post(
            f"/api/endpoints/{endpoint_id}/actions/drain",
            headers=headers,
            json={},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["maintenance"]["active_request_count"] == 1
        assert second.json()["maintenance"]["draining"] is True

    with TestClient(create_api_app(runtime)) as api:
        blocked = api.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": public_model,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "endpoint_draining"

    with TestClient(create_control_app(runtime)) as control:
        resumed = control.post(
            f"/api/endpoints/{endpoint_id}/actions/resume",
            headers=headers,
            json={},
        )
        repeated = control.post(
            f"/api/endpoints/{endpoint_id}/actions/resume",
            headers=headers,
            json={},
        )
        assert resumed.status_code == 200
        assert resumed.json()["maintenance"]["draining"] is False
        assert repeated.status_code == 200
    run(runtime.close())


def test_manual_drain_race_is_rechecked_before_upstream_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = InMemoryStateStore()
    registry = Registry(ROOT / "config/registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "settings.yaml",
        ),
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = StaticHealth()
    runtime.policy.health = runtime.health
    endpoint_id = "ai-qwen38-27b"
    endpoint = registry.by_id(endpoint_id)
    dispatched = False

    async def upstream(_request):
        nonlocal dispatched
        dispatched = True
        return httpx.Response(200, json={})

    run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    original_track = runtime.track_request_routed

    async def track_and_drain(*args, **kwargs):
        await original_track(*args, **kwargs)
        await runtime.set_manual_deployment_drain(
            endpoint_id,
            source="race-test",
        )

    runtime.track_request_routed = track_and_drain
    with TestClient(create_api_app(runtime)) as api:
        response = api.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": endpoint.public_model,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "endpoint_draining"
    assert dispatched is False
    run(runtime.close())


def test_control_resume_requires_healthy_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnhealthyHealth(StaticHealth):
        async def status(self, endpoint, **_kwargs):
            return EndpointStatus(
                endpoint_id=endpoint.id,
                healthy=False,
                checked_at=time.time(),
                eligible_context_tokens=0,
                detail={"reason": "test-unhealthy"},
            )

    store = InMemoryStateStore()
    registry = Registry(ROOT / "config/registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "settings.yaml",
        ),
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = UnhealthyHealth()
    runtime.policy.health = runtime.health
    endpoint_id = "ai-qwen38-27b"
    headers = {"Authorization": "Bearer admin-key"}
    with TestClient(create_control_app(runtime)) as control:
        drained = control.post(
            f"/api/endpoints/{endpoint_id}/actions/drain",
            headers=headers,
            json={},
        )
        resumed = control.post(
            f"/api/endpoints/{endpoint_id}/actions/resume",
            headers=headers,
            json={},
        )
    assert drained.status_code == 200
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "endpoint_resume_unhealthy"
    marker = run(runtime.draining_marker(endpoint_id))
    assert marker is not None
    assert marker["mode"] == "manual"
    run(runtime.close())


def test_control_resume_requires_all_lmcache_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class PartialLMCacheHealth(StaticHealth):
        @staticmethod
        def _detail(_endpoint):
            return {
                "lmcache": {
                    "connector_active": False,
                    "registered_count": 2,
                    "expected_registrations": 4,
                }
            }

    store = InMemoryStateStore()
    registry = Registry(ROOT / "config/registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "settings.yaml",
        ),
        registry=registry,
        store=store,
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = PartialLMCacheHealth()
    runtime.policy.health = runtime.health
    endpoint_id = "ai-qwen38-27b"
    headers = {"Authorization": "Bearer admin-key"}
    with TestClient(create_control_app(runtime)) as control:
        drained = control.post(
            f"/api/endpoints/{endpoint_id}/actions/drain",
            headers=headers,
            json={},
        )
        resumed = control.post(
            f"/api/endpoints/{endpoint_id}/actions/resume",
            headers=headers,
            json={},
        )
    assert drained.status_code == 200
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "endpoint_resume_unhealthy"
    assert resumed.json()["error"]["details"] == {
        "endpoint_healthy": True,
        "lmcache_required": True,
        "lmcache_ready": False,
        "lmcache_registered_count": 2,
        "lmcache_expected_registrations": 4,
    }
    marker = run(runtime.draining_marker(endpoint_id))
    assert marker is not None
    run(runtime.close())


def test_control_draft_validate_and_activate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=Settings(
            ROOT / "config" / "defaults.yaml",
            tmp_path / "settings.yaml",
        ),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = StaticHealth()
    runtime.policy.health = runtime.health
    endpoint_id = "edge-qwen38-flash"
    headers = {"Authorization": "Bearer admin-key"}

    with TestClient(create_control_app(runtime)) as control:
        saved = control.patch(
            f"/api/endpoints/{endpoint_id}",
            headers=headers,
            json={
                "expected_revision": 0,
                "changes": {"safe_context_tokens": 262144},
            },
        )
        assert saved.status_code == 200
        assert (
            runtime.registry.by_id(endpoint_id).safe_context_tokens
            == 500000
        )
        validated = control.post(
            f"/api/endpoints/{endpoint_id}/validate",
            headers=headers,
            json={"expected_revision": 1},
        )
        assert validated.status_code == 200
        assert (
            validated.json()["draft"]["validation"]["status"]
            == "passed"
        )
        activated = control.post(
            f"/api/endpoints/{endpoint_id}/activate",
            headers=headers,
            json={"expected_revision": 2},
        )
        assert activated.status_code == 200
        assert (
            runtime.registry.by_id(endpoint_id).safe_context_tokens
            == 262144
        )
        conflict = control.post(
            f"/api/endpoints/{endpoint_id}/actions/disable",
            headers=headers,
            json={"expected_revision": 2},
        )
        assert conflict.status_code == 409
        assert (
            conflict.json()["error"]["code"]
            == "endpoint_revision_conflict"
        )
        for schedule_key in (
            "work_flash_order",
            "off_hours_flash_order",
        ):
            invalid_schedule = control.patch(
                "/api/policy/draft",
                headers=headers,
                json={
                    "changes": {
                        "routing": {
                            "objectives": {
                                "schedule": {
                                    schedule_key: {
                                        "general": ["missing-endpoint"]
                                    }
                                }
                            }
                        }
                    }
                },
            )
            assert invalid_schedule.status_code == 400
            assert (
                invalid_schedule.json()["error"]["code"]
                == "invalid_policy_draft"
            )
    run(runtime.close())

@pytest.mark.parametrize("modalities", [[], ["video"], ["text", "unknown"]])
def test_input_modality_invalid_values_rejected(modalities):
    manager = EndpointConfigManager(InMemoryStateStore(), Registry(ROOT / "config" / "registry.yaml"))
    with pytest.raises(RouterError):
        run(manager.save_draft("edge-qwen38-flash", {"modalities": modalities}, expected_revision=0, source="test"))


def test_image_configuration_persists_and_syncs_runtimes():
    registry = Registry(ROOT / "config" / "registry.yaml")
    store = InMemoryStateStore()
    manager = EndpointConfigManager(store, registry)
    endpoint_id = "edge-qwen38-flash"
    record = run(manager.records())[endpoint_id]
    assert record["configurable_modalities"] == ["image", "text"]
    assert record["baseline"]["modalities"] == ["text"]
    draft = run(manager.save_draft(endpoint_id, {"modalities": ["text", "image"]}, expected_revision=0, source="test"))
    validated = run(manager.validate_draft(endpoint_id, expected_revision=draft["revision"], status=healthy(endpoint_id), source="test"))
    active = run(manager.activate(endpoint_id, expected_revision=validated["revision"], source="test"))
    reloaded = EndpointConfigManager(store, registry)
    effective = run(reloaded.effective_registry()).by_id(endpoint_id)
    assert effective.modalities == ("text", "image")
    assert effective.enabled == registry.by_id(endpoint_id).enabled
    draft = run(reloaded.save_draft(endpoint_id, {"modalities": ["text"]}, expected_revision=active["revision"], source="test"))
    validated = run(reloaded.validate_draft(endpoint_id, expected_revision=draft["revision"], status=healthy(endpoint_id), source="test"))
    run(reloaded.activate(endpoint_id, expected_revision=validated["revision"], source="test"))
    assert run(manager.effective_registry()).by_id(endpoint_id).modalities == ("text",)
