from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from ai_router.config import Settings, load_yaml
from ai_router.control import create_app


def test_routing_only_update_preserves_identity_and_other_runtime_sections(
    tmp_path, monkeypatch
):
    settings = Settings(
        Path(__file__).resolve().parents[1] / "config/defaults.yaml",
        tmp_path / "settings.yaml",
    )
    settings.write_runtime(
        {
            "identity": {"enabled": True},
            "cloud": {"enabled": False},
            "routing": {"provider_priority": "balanced"},
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
    assert stored["identity"]["enabled"] is True
    assert stored["cloud"]["enabled"] is False
    assert stored["routing"]["provider_priority"] == "cloud_first"
    assert result.json()["settings"]["identity"]["enabled"] is True
    runtime.audit.write.assert_called_once()
    assert runtime.audit.write.call_args.kwargs["sections"] == ["routing"]
