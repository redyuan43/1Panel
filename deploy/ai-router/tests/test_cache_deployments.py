from pathlib import Path
import time

import pytest

from ai_router.cache_deployments import (
    cache_deployment_view,
    load_cache_deployment_catalog,
)
from ai_router.control import create_app


ROOT = Path(__file__).resolve().parents[1]


def endpoint_value(
    endpoint_id,
    *,
    backend_type="vllm",
    enabled=True,
    auto_candidate=True,
    detail=None,
):
    return {
        "endpoint": {
            "id": endpoint_id,
            "public_model": endpoint_id,
            "backend_type": backend_type,
            "enabled": enabled,
            "auto_candidate": auto_candidate,
            "safe_context_tokens": 196608,
            "configured_context_tokens": 196608,
            "max_concurrency": 1,
            "modalities": ["text"],
        },
        "status": {
            "healthy": True,
            "checked_at": time.time(),
            "load_headroom": 1,
            "cache_generation": "generation",
            "detail": detail or {},
        },
        "management": {"revision": 9},
    }


def fleet_workers():
    return [
        {
            "worker_id": worker_id,
            "ready": True,
            "state": "available",
            "safe_context_tokens": 57344,
            "context_size": 57344,
            "cache_type_k": "q4_0",
            "cache_type_v": "q4_0",
            "context_checkpoints": 2,
            "modalities": ["text", "image"],
            "config_drift": [],
            "backend_api_key_env": "MUST_NOT_LEAK",
        }
        for worker_id in (
            "qwen36-nx3",
            "qwen36-nx4",
            "qwen36-agx",
        )
    ]


def test_catalog_loads_six_safe_deployments():
    catalog = load_cache_deployment_catalog()
    assert [item["id"] for item in catalog["deployments"]] == [
        "ai-v100-tp2",
        "edge-qwen38-flash",
        "amd-qwen38-rocmfpx",
        "nx3-qwen36",
        "nx4-qwen36",
        "agx-qwen36",
    ]
    assert all(
        not item["management"]["remote_restart"]
        and not item["management"]["cache_clear"]
        for item in catalog["deployments"]
    )


def test_control_registers_cache_deployment_api():
    app = create_app()
    assert "/api/cache/deployments" in {
        route.path for route in app.routes
    }


def test_catalog_rejects_secret_shaped_fields(tmp_path):
    source = (
        ROOT / "config" / "cache-deployments.yaml"
    ).read_text(encoding="utf-8")
    path = tmp_path / "catalog.yaml"
    path.write_text(
        source.replace(
            "node: ai",
            "node: ai\n    api_key: forbidden",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbidden field"):
        load_cache_deployment_catalog(path)


def test_view_separates_lmcache_health_from_connector_attachment():
    catalog = load_cache_deployment_catalog()
    endpoints = [
        endpoint_value(
            "ai-qwen38-27b",
            detail={
                "prefix_cache_queries": 100,
                "prefix_cache_hits": 80,
                "lmcache": {
                    "supported": True,
                    "healthy": True,
                    "registered": False,
                    "registered_count": 0,
                    "expected_registrations": 2,
                    "connector_active": False,
                    "memory_used_bytes": 64 * 1024**3,
                    "memory_total_bytes": 80 * 1024**3,
                },
            },
        ),
        endpoint_value("amd-qwen38-rocmfpx-128k", backend_type="llama_cpp"),
        endpoint_value(
            "edge-qwen38-flash",
            detail={
                "external_prefix_cache_queries": 10,
                "external_prefix_cache_hits": 8,
            },
        ),
        endpoint_value(
            "qwen36-shared-fleet",
            backend_type="ai_pool",
            detail={"workers": fleet_workers()},
        ),
    ]
    payload = cache_deployment_view(
        catalog,
        endpoints,
        {"lmcache": {"enabled": True, "l1_size_gb": 80}},
    )
    ai = payload["deployments"][0]
    assert ai["observed"]["cache"]["lmcache"]["healthy"] is True
    assert ai["observed"]["cache"]["lmcache"]["connector_active"] is False
    assert {item["code"] for item in ai["drift"]} == {
        "lmcache_restart_required"
    }
    assert ai["state"]["tone"] == "warning"
    assert payload["summary"]["drifted"] == 1


def test_view_resolves_fleet_workers_and_keeps_nx4_planned():
    catalog = load_cache_deployment_catalog()
    endpoints = [
        endpoint_value("ai-qwen38-27b"),
        endpoint_value("edge-qwen38-flash"),
        endpoint_value("amd-qwen38-rocmfpx-128k", backend_type="llama_cpp"),
        endpoint_value(
            "qwen36-shared-fleet",
            backend_type="ai_pool",
            detail={"workers": fleet_workers()},
        ),
    ]
    payload = cache_deployment_view(catalog, endpoints, {})
    values = {item["id"]: item for item in payload["deployments"]}
    assert values["nx3-qwen36"]["observed"]["worker"]["ready"] is True
    assert values["agx-qwen36"]["observed"]["worker"]["ready"] is True
    assert "backend_api_key_env" not in values[
        "nx3-qwen36"
    ]["observed"]["worker"]
    assert values["nx4-qwen36"]["state"] == {
        "code": "warning",
        "label": "Planned",
        "tone": "warning",
    }
    assert {
        item["code"] for item in values["nx4-qwen36"]["drift"]
    } == {"planned_not_deployed"}


def test_management_only_exposes_existing_safe_actions():
    catalog = load_cache_deployment_catalog()
    endpoints = [
        endpoint_value("ai-qwen38-27b"),
        endpoint_value("amd-qwen38-rocmfpx-128k", backend_type="llama_cpp"),
        endpoint_value(
            "edge-qwen38-flash",
            enabled=False,
            auto_candidate=False,
        ),
        endpoint_value(
            "qwen36-shared-fleet",
            backend_type="ai_pool",
            detail={"workers": fleet_workers()},
        ),
    ]
    payload = cache_deployment_view(catalog, endpoints, {})
    values = {item["id"]: item for item in payload["deployments"]}
    assert [
        action["id"]
        for action in values["ai-v100-tp2"]["management"]["actions"]
    ] == ["lmcache-settings", "disable", "auto-disable"]
    assert [
        action["id"]
        for action in values["edge-qwen38-flash"]["management"]["actions"]
    ] == ["enable", "auto-enable"]
    for device in ("nx3-qwen36", "nx4-qwen36", "agx-qwen36"):
        assert values[device]["management"]["read_only"] is True
        assert values[device]["management"]["actions"] == []


def test_amd_memory_declaration_does_not_imply_disk_or_measured_hits():
    catalog = load_cache_deployment_catalog()
    payload = cache_deployment_view(catalog, [endpoint_value("amd-qwen38-rocmfpx-128k", backend_type="llama_cpp")], {})
    amd = next(x for x in payload["deployments"] if x["node"] == "amd")
    assert amd["declared"]["strategy"] == "native_memory"
    assert {x["medium"] for x in amd["declared"]["layers"]} == {"process", "compute"}
    assert amd["validated"]["status"] == "partial"
    assert amd["observed"]["cache"]["gpu_apc"]["hit_tokens"] is None
    assert amd["observed"]["cache"]["lmcache"] == {}
    assert amd["management"]["read_only"] is True


@pytest.mark.parametrize("target", ["edge-qwen38-flash", "amd-qwen38-rocmfpx-128k", "qwen36-shared-fleet"])
def test_model_health_without_cache_probe_is_not_cache_health(target):
    endpoints = [endpoint_value(target, detail={"workers": fleet_workers(),
                 "external_prefix_cache_hits": 9000})]
    payload = cache_deployment_view(load_cache_deployment_catalog(), endpoints, {})
    for item in payload["deployments"]:
        if item["target"]["endpoint_id"] == target and item["declared"]["lifecycle"] == "deployed":
            assert item["observed"]["runtime_healthy"] is True
            assert item["state"]["code"] == "unknown"
            assert item["observed"]["cache_health"]["healthy"] is None
    assert payload["summary"]["healthy"] == 0


@pytest.mark.parametrize("age", [None, 121, -10])
def test_missing_or_stale_cache_probe_cannot_claim_health(age):
    endpoint = endpoint_value("ai-qwen38-27b", detail={"lmcache": {
        "supported": True, "healthy": True, "connector_active": True}})
    endpoint["status"]["checked_at"] = None if age is None else time.time() - age
    payload = cache_deployment_view(load_cache_deployment_catalog(), [endpoint], {"lmcache": {"enabled": True}})
    ai = payload["deployments"][0]
    assert ai["state"]["code"] == "unknown"
    assert ai["observed"]["cache_health"]["healthy"] is None
    assert payload["summary"]["healthy"] == 0


def test_fresh_cache_service_failure_is_not_hidden_by_inactive_connector():
    endpoint = endpoint_value("ai-qwen38-27b", detail={"lmcache": {
        "supported": True, "healthy": False, "connector_active": False}})
    payload = cache_deployment_view(load_cache_deployment_catalog(), [endpoint], {"lmcache": {"enabled": True}})
    assert payload["deployments"][0]["state"]["code"] == "unavailable"
    assert payload["summary"]["healthy"] == 0
    endpoint["status"]["detail"]["lmcache"].update(healthy=True, connector_active=True)
    payload = cache_deployment_view(load_cache_deployment_catalog(), [endpoint], {"lmcache": {"enabled": True}})
    assert payload["deployments"][0]["state"]["code"] == "healthy"
    assert payload["summary"]["healthy"] == 1


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_unknown_or_invalid_counters_are_not_zero(value):
    endpoint = endpoint_value("edge-qwen38-flash", detail={"prefix_cache_hits": value})
    payload = cache_deployment_view(load_cache_deployment_catalog(), [endpoint], {})
    edge = payload["deployments"][1]
    assert edge["observed"]["cache"]["gpu_apc"]["hit_tokens"] is None
