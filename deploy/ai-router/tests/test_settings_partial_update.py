from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from ai_router.config import Settings, load_yaml
from ai_router.client_route_binding import resolve_client_route
from ai_router.control import create_app


def test_routing_only_update_preserves_identity_and_other_runtime_sections(
    tmp_path, monkeypatch
):
    settings = Settings(
        Path(__file__).resolve().parents[1] / "config/defaults.yaml",
        tmp_path / "settings.yaml",
    )
    bindings = [
        {
            "client_id": client_id,
            "requested_models": ["auto"],
            "target_endpoint_id": "nx1-ornith-35b-a3b-96k",
            "ignored_route_tiers": [],
        }
        for client_id in ("home-assistant", "check-boards")
    ]
    settings.write_runtime(
        {
            "identity": {"enabled": True},
            "cloud": {"enabled": False},
            "routing": {
                "provider_priority": "balanced",
                "client_route_bindings": bindings,
            },
        }
    )
    runtime = SimpleNamespace(
        settings=settings,
        registry=Mock(),
        reload_settings=settings.reload,
        policy_config=SimpleNamespace(
            record_external_activation=AsyncMock(return_value={"revision": 2})
        ),
        prompt_directives=SimpleNamespace(
            sync_active=Mock(), record_changes=Mock()
        ),
        audit=SimpleNamespace(write=Mock()),
    )
    monkeypatch.setattr("ai_router.control._authorized_runtime", lambda request: runtime)
    monkeypatch.setattr("ai_router.control.validate_context_target", lambda *args: None)
    monkeypatch.setattr("ai_router.control.validate_background_settings", lambda *args: None)

    client = TestClient(create_app(runtime))
    try:
        result = client.put(
            "/api/settings",
            json={"routing": {"provider_priority": "cloud_first"}},
        )
    finally:
        client.close()

    assert result.status_code == 200, result.json()
    stored = load_yaml(settings.runtime_path)
    assert set(stored) == {"identity", "cloud", "routing"}
    assert stored["identity"]["enabled"] is True
    assert stored["cloud"]["enabled"] is False
    assert stored["routing"]["provider_priority"] == "cloud_first"
    assert stored["routing"]["client_route_bindings"] == bindings
    assert result.json()["settings"]["identity"]["enabled"] is True
    endpoint = SimpleNamespace(
        id="nx1-ornith-35b-a3b-96k",
        role="responder",
        public_model="siyuan/ornith-nx1-96k",
    )
    registry = SimpleNamespace(by_id=lambda endpoint_id: endpoint)
    for client_id in ("home-assistant", "check-boards"):
        resolved = resolve_client_route(
            settings.section("routing"),
            registry,
            client_id=client_id,
            requested_model="auto",
            disclosure_mode="internal",
        )
        assert resolved is not None
        assert resolved.target_endpoint_id == endpoint.id
    assert resolve_client_route(
        settings.section("routing"),
        registry,
        client_id="workbuddy-public",
        requested_model="auto",
        disclosure_mode="public",
    ) is None
    runtime.audit.write.assert_called_once()
    assert runtime.audit.write.call_args.kwargs["sections"] == ["routing"]
