from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from ai_router.config import Registry, Settings
from ai_router.health import HealthMonitor
from ai_router.lmcache_runtime import (
    build_kv_transfer_config,
    load_lmcache_settings,
    validate_lmcache_settings,
)
from ai_router.store import InMemoryStateStore


ROOT = Path(__file__).resolve().parents[1]


def test_lmcache_settings_are_persisted_and_strict(tmp_path: Path) -> None:
    runtime_path = tmp_path / "settings.yaml"
    runtime_path.write_text(
        yaml.safe_dump(
            {
                "lmcache": {
                    "enabled": True,
                    "l1_size_gb": 64,
                }
            }
        ),
        encoding="utf-8",
    )
    value = load_lmcache_settings(
        ROOT / "config/defaults.yaml",
        runtime_path,
    )
    assert value["enabled"] is True
    assert value["l1_size_gb"] == 64
    assert value["memory_max_gb"] == 96
    assert value["chunk_size"] == 1600

    settings = Settings(
        defaults_path=ROOT / "config/defaults.yaml",
        runtime_path=runtime_path,
    )
    assert settings.section("lmcache") == value

    invalid = dict(value)
    invalid["server_url"] = "tcp://example.com:6555"
    with pytest.raises(ValueError, match="local tcp URL"):
        validate_lmcache_settings(invalid)

    invalid = dict(value)
    invalid["memory_max_gb"] = 128
    with pytest.raises(ValueError, match="must be 96"):
        validate_lmcache_settings(invalid)


def test_lmcache_kv_transfer_config_uses_external_connector() -> None:
    value = json.loads(
        build_kv_transfer_config("tcp://127.0.0.1:6555")
    )
    assert value == {
        "kv_connector": "LMCacheMPConnector",
        "kv_connector_extra_config": {
            "lmcache.mp.heartbeat_interval": 10,
            "lmcache.mp.host": "tcp://127.0.0.1",
            "lmcache.mp.mp_transfer_mode": "lmcache_driven",
            "lmcache.mp.port": 6555,
        },
        "kv_connector_module_path": (
            "lmcache.integration.vllm.lmcache_mp_connector"
        ),
        "kv_role": "kv_both",
    }


def test_vllm_health_exposes_gpu_and_lmcache_metrics() -> None:
    endpoint = Registry(ROOT / "config/registry.yaml").by_id(
        "ai-qwen38-27b"
    )
    assert endpoint is not None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 18107 and request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.port == 18107 and request.url.path == "/metrics":
            return httpx.Response(
                200,
                text="\n".join(
                    (
                        'vllm:num_requests_running{engine="0"} 1',
                        'vllm:num_requests_waiting{engine="0"} 0',
                        'vllm:kv_cache_usage_perc{engine="0"} 0.25',
                        'vllm:prefix_cache_queries_total{engine="0"} 50000',
                        'vllm:prefix_cache_hits_total{engine="0"} 40000',
                        'vllm:external_prefix_cache_queries_total{engine="0"} 30000',
                        'vllm:external_prefix_cache_hits_total{engine="0"} 28000',
                        'vllm:prompt_tokens_cached_total{engine="0"} 68000',
                        'vllm:prompt_tokens_by_source_total{engine="0",source="local_compute"} 2000',
                        'vllm:prompt_tokens_by_source_total{engine="0",source="local_cache_hit"} 40000',
                        'vllm:prompt_tokens_by_source_total{engine="0",source="external_kv_transfer"} 28000',
                        "process_start_time_seconds 100",
                    )
                ),
            )
        if request.url.port == 18084 and request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "is_healthy": True,
                    "instance_id": "lmcache-test",
                    "engine_type": "MPCacheServer",
                    "chunk_size": 1600,
                    "registered_gpu_ids": [0, 1],
                    "active_sessions": 2,
                    "cache_context_meta": {"0": {}, "1": {}},
                    "storage_manager": {
                        "l1_manager": {
                            "memory_used_bytes": 32 << 30,
                            "memory_total_bytes": 80 << 30,
                            "memory_usage_ratio": 0.4,
                        },
                        "num_l2_adapters": 0,
                    },
                },
            )
        if request.url.port == 18084 and request.url.path == "/metrics":
            return httpx.Response(
                200,
                text="\n".join(
                    (
                        'lmcache_mp_lookup_requested_tokens_total{model_name="qwen"} 30000',
                        'lmcache_mp_lookup_hit_tokens_total{model_name="qwen"} 28000',
                        'lmcache_mp_l1_read_chunks_total{cache_salt=""} 18',
                        'lmcache_mp_l1_write_chunks_total{cache_salt=""} 32',
                        "lmcache_mp_l1_memory_usage_bytes 34359738368",
                        "lmcache_mp_l1_usage_ratio 0.4",
                        "process_start_time_seconds 1788750000",
                    )
                ),
            )
        return httpx.Response(404)

    async def run_probe():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        monitor = HealthMonitor(
            InMemoryStateStore(),
            client=client,
        )
        try:
            return await monitor.status(endpoint, force_refresh=True)
        finally:
            await client.aclose()

    status = asyncio.run(run_probe())
    assert status.healthy is True
    assert status.detail["prefix_cache_hits"] == 40000
    assert status.detail["prompt_tokens_external_transfer"] == 28000
    lmcache = status.detail["lmcache"]
    assert lmcache["healthy"] is True
    assert lmcache["connector_active"] is True
    assert lmcache["registered_count"] == 2
    assert lmcache["expected_registrations"] == 2
    assert lmcache["chunk_size"] == 1600
    assert lmcache["memory_total_bytes"] == 80 << 30
    assert lmcache["lookup_hit_tokens"] == 28000
    assert lmcache["process_start_time_seconds"] == 1788750000


def test_lmcache_ui_and_service_contracts_are_present() -> None:
    html = (ROOT / "ai_router/static/index.html").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "ai_router/static/app.js").read_text(encoding="utf-8")
    vllm_unit = (
        ROOT / "systemd/qwen38-v100-tp2-vllm.service"
    ).read_text(encoding="utf-8")
    lmcache_unit = (
        ROOT / "systemd/qwen38-v100-tp2-lmcache.service"
    ).read_text(encoding="utf-8")
    lmcache_runner = (
        ROOT / "scripts/run-qwen38-v100-tp2-lmcache-container.sh"
    ).read_text(encoding="utf-8")

    assert 'id="lmcache-enabled"' in html
    assert 'id="lmcache-l1-size"' in html
    assert "需要受控重启" in html
    assert "function endpointCacheStatus" in app
    assert "function lmcacheRestartRequired" in app
    assert "Requires=qwen38-v100-tp2-lmcache.service" in vllm_unit
    assert "BindsTo=qwen38-v100-tp2-lmcache.service" in vllm_unit
    assert "PartOf=qwen38-v100-tp2-vllm.service" not in lmcache_unit
    assert "MemoryMax=96G" in lmcache_unit
    assert "NUMAMask=1" in lmcache_unit
    assert "--eviction-policy LRU" in lmcache_runner
    assert "--separate-object-groups" in lmcache_runner
    assert "--supported-transfer-mode lmcache_driven" in lmcache_runner
    assert "--worker-registration-grace-seconds 1800" in lmcache_runner
    assert "--runtime nvidia" in lmcache_runner
    assert "-e CUDA_VISIBLE_DEVICES=0,1" in lmcache_runner
    assert '--cpuset-cpus "$numa_cpu_list"' in lmcache_runner
    assert '--cpuset-mems "$numa_node"' in lmcache_runner
    assert 'NUMBA_CACHE_DIR="$NUMBA_CACHE_DIR"' in lmcache_runner
    vllm_runner = (
        ROOT / "scripts/run-qwen38-v100-tp2-container.sh"
    ).read_text(encoding="utf-8")
    preflight = (
        ROOT / "scripts/check-qwen38-v100-tp2-lmcache.py"
    ).read_text(encoding="utf-8")
    assert 'NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-}"' in vllm_runner
    assert "--require-vllm-guards" in vllm_unit
    assert "--query-gpu=memory.free,memory.total" in preflight
    assert 'os.environ.get("GPU_MEMORY_UTILIZATION", "0.90")' in preflight
    assert "RequestType.UNREGISTER_KV_CACHE" in preflight
