from pathlib import Path

import pytest

from prefix_cache_lab.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_repository_config_loads() -> None:
    config = load_config(ROOT / "config" / "nx2-nx4.yaml")
    assert config.node("nx3").allow_lifecycle
    assert not config.node("nx2").allow_lifecycle
    assert config.workbuddy.ssh_host == "ivan-laptop"
    assert config.router.model == "prefix-lab/nx3-qwen36"
    assert "--cache-ram" in config.node("nx3").lab_command
    assert "--slot-save-path" in config.node("nx3").lab_command
    assert "--api-key-file" in config.node("nx3").lab_command
    assert config.node("nx3").lab_unit == "prefix-cache-lab-nx3.service"
    assert config.node("nx3").lab_api_key_file.endswith("/api-key")
    assert config.node("nx3").backend_api_key_env == "AI_ROUTER_NX3_BACKEND_KEY"
    assert config.node("nx3").provider_model == "Qwen_Qwen3.6-35B-A3B-IQ2_S.gguf"
    assert config.node("nx3").production_base_url.endswith(":8081")
    assert len(config.node("nx3").lab_prestart) == 2


def test_unknown_node_is_rejected() -> None:
    config = load_config(ROOT / "config" / "nx2-nx4.yaml")
    with pytest.raises(ValueError, match="unknown node"):
        config.node("nx9")
