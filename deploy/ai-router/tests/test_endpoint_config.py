from __future__ import annotations

import asyncio
from pathlib import Path

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
                {"modalities": ["text", "image"]},
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
                checked_at=1,
                eligible_context_tokens=item.safe_context_tokens,
                detail={},
            )
            for item in endpoints
        }

    async def status(self, endpoint, **_kwargs):
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=1,
            eligible_context_tokens=endpoint.safe_context_tokens,
            detail={},
        )

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
    run(runtime.close())
