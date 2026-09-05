import pytest

from prefix_cache_lab import benchmark, cli
from prefix_cache_lab.config import NodeConfig


def _node() -> NodeConfig:
    return NodeConfig(
        name="nx3",
        ssh_host="nx3",
        base_url="http://nx3:18081",
        production_base_url="http://nx3:8081",
        service_unit="production.service",
        allow_lifecycle=True,
        lab_unit="lab.service",
        lab_command=("llama-server",),
        lab_api_key_file="/state/api-key",
        backend_api_key_env="LAB_BACKEND_KEY",
    )


def test_start_health_failure_restores_and_verifies_production(monkeypatch) -> None:
    restored = []
    health_urls = []

    monkeypatch.setattr(cli, "start_lab_runtime", lambda _node, apply: [["start"]])
    monkeypatch.setattr(cli, "load_lab_api_key", lambda _node: "secret")
    monkeypatch.setattr(
        cli,
        "restore_production_runtime",
        lambda _node, apply: restored.append(apply),
    )
    monkeypatch.setattr(cli, "service_state", lambda _node: "active")
    monkeypatch.setattr(cli, "user_service_state", lambda _node: "inactive")

    def wait_health(url, **_kwargs):
        health_urls.append(url)
        if url.endswith(":18081/health"):
            raise TimeoutError("lab did not become ready")

    monkeypatch.setattr(benchmark, "_wait_health", wait_health)

    with pytest.raises(RuntimeError, match="production was restored"):
        cli._start_runtime_checked(_node(), apply=True)

    assert restored == [True]
    assert health_urls == [
        "http://nx3:18081/health",
        "http://nx3:8081/health",
    ]


def test_start_failure_preserves_primary_and_rollback_errors(monkeypatch) -> None:
    def fail_start(_node, *, apply):
        raise RuntimeError("primary-start-error")

    def fail_restore(_node):
        raise RuntimeError("rollback-error")

    monkeypatch.setattr(cli, "start_lab_runtime", fail_start)
    monkeypatch.setattr(cli, "_restore_and_verify", fail_restore)

    with pytest.raises(RuntimeError) as error:
        cli._start_runtime_checked(_node(), apply=True)

    assert "primary-start-error" in str(error.value)
    assert "rollback-error" in str(error.value)
