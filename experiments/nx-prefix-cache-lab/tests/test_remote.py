import pytest

from prefix_cache_lab.config import NodeConfig
from prefix_cache_lab.remote import (
    lab_api_key_setup_command,
    service_command,
    start_lab_runtime,
)


def test_lifecycle_requires_allowlist() -> None:
    node = NodeConfig(
        name="nx2",
        ssh_host="nx2",
        base_url="http://nx2",
        service_unit="model.service",
    )
    with pytest.raises(ValueError, match="disabled"):
        service_command(node, "stop", apply=False)


def test_lifecycle_dry_run_is_exact() -> None:
    node = NodeConfig(
        name="nx3",
        ssh_host="nx3",
        base_url="http://nx3",
        service_unit="model.service",
        use_sudo=True,
        allow_lifecycle=True,
    )
    assert service_command(node, "restart", apply=False) == [
        "sudo",
        "-n",
        "systemctl",
        "restart",
        "model.service",
    ]


def test_lab_runtime_supports_multiple_prestart_commands() -> None:
    node = NodeConfig(
        name="nx3",
        ssh_host="nx3",
        base_url="http://nx3",
        service_unit="model.service",
        use_sudo=True,
        allow_lifecycle=True,
        lab_unit="lab.service",
        lab_prestart=(("mkdir", "-p", "/state/slots"),),
        lab_command=("llama-server", "--port", "18081"),
        lab_api_key_file="/state/api-key",
        backend_api_key_env="LAB_BACKEND_KEY",
    )
    commands = start_lab_runtime(node, apply=False)
    assert ["mkdir", "-p", "/state/slots"] in commands
    assert lab_api_key_setup_command(node) == commands[0]
    assert "secrets.token_urlsafe" in commands[0][-1]
    assert "api-key-value" not in commands[0][-1]
