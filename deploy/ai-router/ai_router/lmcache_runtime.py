from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import copy
import json

import yaml


LMCACHE_ENDPOINT_ID = "ai-qwen38-27b"
LMCACHE_CHUNK_SIZE = 1600
LMCACHE_NUMA_NODE = 1
LMCACHE_CONNECTOR_MODULE = (
    "lmcache.integration.vllm.lmcache_mp_connector"
)


def validate_lmcache_settings(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("lmcache must be an object")
    expected = {
        "enabled",
        "endpoint_id",
        "server_url",
        "http_url",
        "l1_size_gb",
        "memory_max_gb",
        "chunk_size",
        "separate_object_groups",
        "numa_node",
    }
    if set(value) != expected:
        raise ValueError(
            "lmcache must define exactly: " + ", ".join(sorted(expected))
        )
    if not isinstance(value["enabled"], bool):
        raise ValueError("lmcache.enabled must be a boolean")
    if str(value["endpoint_id"]) != LMCACHE_ENDPOINT_ID:
        raise ValueError(
            f"lmcache.endpoint_id must be {LMCACHE_ENDPOINT_ID}"
        )
    _validate_local_url(
        value["server_url"],
        schemes={"tcp"},
        field="lmcache.server_url",
    )
    _validate_local_url(
        value["http_url"],
        schemes={"http"},
        field="lmcache.http_url",
    )
    l1_size_gb = _integer(value["l1_size_gb"], "lmcache.l1_size_gb")
    memory_max_gb = _integer(
        value["memory_max_gb"],
        "lmcache.memory_max_gb",
    )
    if not 8 <= l1_size_gb <= 80:
        raise ValueError(
            "lmcache.l1_size_gb must be between 8 and 80"
        )
    if memory_max_gb != 96:
        raise ValueError("lmcache.memory_max_gb must be 96")
    if l1_size_gb >= memory_max_gb:
        raise ValueError(
            "lmcache.l1_size_gb must be smaller than memory_max_gb"
        )
    if _integer(value["chunk_size"], "lmcache.chunk_size") != LMCACHE_CHUNK_SIZE:
        raise ValueError(
            f"lmcache.chunk_size must be {LMCACHE_CHUNK_SIZE}"
        )
    if value["separate_object_groups"] is not True:
        raise ValueError(
            "lmcache.separate_object_groups must remain enabled"
        )
    if _integer(value["numa_node"], "lmcache.numa_node") != LMCACHE_NUMA_NODE:
        raise ValueError(
            f"lmcache.numa_node must be {LMCACHE_NUMA_NODE}"
        )


def load_lmcache_settings(
    defaults_path: Path,
    runtime_path: Path,
) -> dict[str, Any]:
    defaults = _load_yaml(defaults_path, required=True)
    runtime = _load_yaml(runtime_path, required=False)
    default_value = defaults.get("lmcache", {})
    runtime_value = runtime.get("lmcache", {})
    if not isinstance(default_value, dict) or not isinstance(
        runtime_value,
        dict,
    ):
        raise ValueError("lmcache settings must be an object")
    value = copy.deepcopy(default_value)
    value.update(copy.deepcopy(runtime_value))
    validate_lmcache_settings(value)
    return value


def build_kv_transfer_config(server_url: str) -> str:
    parsed = _validate_local_url(
        server_url,
        schemes={"tcp"},
        field="lmcache.server_url",
    )
    return json.dumps(
        {
            "kv_connector": "LMCacheMPConnector",
            "kv_role": "kv_both",
            "kv_connector_module_path": LMCACHE_CONNECTOR_MODULE,
            "kv_connector_extra_config": {
                "lmcache.mp.host": f"tcp://{parsed.hostname}",
                "lmcache.mp.port": parsed.port,
                "lmcache.mp.heartbeat_interval": 10,
                "lmcache.mp.mp_transfer_mode": "lmcache_driven",
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def server_host_port(server_url: str) -> tuple[str, int]:
    parsed = _validate_local_url(
        server_url,
        schemes={"tcp"},
        field="lmcache.server_url",
    )
    assert parsed.hostname is not None
    assert parsed.port is not None
    return parsed.hostname, parsed.port


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if integer != value:
        raise ValueError(f"{field} must be an integer")
    return integer


def _validate_local_url(
    value: Any,
    *,
    schemes: set[str],
    field: str,
):
    parsed = urlparse(str(value).strip())
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{field} must be a local {sorted(schemes)[0]} URL with a port"
        )
    return parsed


def _load_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value
