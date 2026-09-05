import os

from prefix_cache_lab import router_harness


def test_harness_overrides_production_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "production-client-key")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", "/production/audit.jsonl")
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        "/production/route-traces.sqlite3",
    )
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv("AI_ROUTER_TRAINING_DB_PATH", "/production/training.db")
    monkeypatch.setenv("AI_ROUTER_TRAINING_KEY_PATH", "/production/training.key")

    router_harness._configure_environment(tmp_path)

    assert os.environ["AI_ROUTER_1PANEL_API_KEY"] == router_harness.CLIENT_KEY
    assert os.environ["AI_ROUTER_AUDIT_PATH"] == str(tmp_path / "audit.jsonl")
    assert os.environ["AI_ROUTER_ROUTE_TRACE_DB_PATH"] == str(
        tmp_path / "route-traces.sqlite3"
    )
    assert os.environ["AI_ROUTER_TRAINING_ENABLED"] == "false"
    assert "AI_ROUTER_TRAINING_DB_PATH" not in os.environ
    assert "AI_ROUTER_TRAINING_KEY_PATH" not in os.environ
