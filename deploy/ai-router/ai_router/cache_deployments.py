from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
import math
from typing import Any

import yaml


DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "cache-deployments.yaml"
)
_REQUIRED_DEPLOYMENT_FIELDS = {
    "id",
    "title",
    "node",
    "strategy",
    "flow_label",
    "target",
    "lifecycle",
    "layers",
    "services",
    "paths",
    "validation",
    "telemetry",
    "management",
    "limitations",
}
_FORBIDDEN_KEY_PARTS = {
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
}


def load_cache_deployment_catalog(
    path: str | Path = DEFAULT_CATALOG_PATH,
) -> dict[str, Any]:
    source = Path(path)
    value = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("cache deployment catalog must be an object")
    if int(value.get("version", 0)) != 1:
        raise ValueError("cache deployment catalog version must be 1")
    deployments = value.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        raise ValueError(
            "cache deployment catalog requires deployments"
        )
    identifiers: set[str] = set()
    for index, deployment in enumerate(deployments):
        _validate_deployment(deployment, index)
        identifier = str(deployment["id"])
        if identifier in identifiers:
            raise ValueError(
                f"duplicate cache deployment id: {identifier}"
            )
        identifiers.add(identifier)
    _reject_secrets(value)
    return value


def cache_deployment_view(
    catalog: dict[str, Any],
    endpoint_values: list[dict[str, Any]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    endpoint_index = {
        str(item.get("endpoint", {}).get("id", "")): item
        for item in endpoint_values
    }
    deployments = [
        _deployment_view(item, endpoint_index, settings)
        for item in catalog["deployments"]
    ]
    return {
        "version": int(catalog["version"]),
        "updated_at": str(catalog.get("updated_at", "")),
        "generated_at": time.time(),
        "summary": {
            "total": len(deployments),
            "deployed": sum(
                item["declared"]["lifecycle"] == "deployed"
                for item in deployments
            ),
            "healthy": sum(
                item["declared"]["lifecycle"] == "deployed"
                and item["state"]["code"] == "healthy"
                for item in deployments
            ),
            "drifted": sum(
                any(
                    drift["severity"] in {"warning", "critical"}
                    and drift["code"] != "planned_not_deployed"
                    for drift in item["drift"]
                )
                for item in deployments
            ),
            "planned": sum(
                item["declared"]["lifecycle"] == "planned"
                for item in deployments
            ),
            "strategies": len(
                {item["declared"]["strategy"] for item in deployments}
            ),
        },
        "deployments": deployments,
    }


def _validate_deployment(value: Any, index: int) -> None:
    if not isinstance(value, dict):
        raise ValueError(
            f"cache deployment {index} must be an object"
        )
    missing = sorted(_REQUIRED_DEPLOYMENT_FIELDS - set(value))
    if missing:
        raise ValueError(
            f"cache deployment {index} is missing: "
            + ", ".join(missing)
        )
    target = value["target"]
    if (
        not isinstance(target, dict)
        or not str(target.get("endpoint_id", "")).strip()
    ):
        raise ValueError(
            f"cache deployment {index} requires target.endpoint_id"
        )
    layers = value["layers"]
    if not isinstance(layers, list) or not layers:
        raise ValueError(
            f"cache deployment {index} requires layers"
        )
    for layer_index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise ValueError(
                f"cache deployment {index} layer "
                f"{layer_index} must be an object"
            )
        required = {
            "id",
            "label",
            "medium",
            "backend",
            "capacity",
            "persistence",
            "description",
        }
        missing_layer = sorted(required - set(layer))
        if missing_layer:
            raise ValueError(
                f"cache deployment {index} layer {layer_index} "
                "is missing: "
                + ", ".join(missing_layer)
            )
    management = value["management"]
    if not isinstance(management, dict):
        raise ValueError(
            f"cache deployment {index} management must be an object"
        )
    for field in (
        "endpoint_actions",
        "lmcache_settings",
        "remote_restart",
        "cache_clear",
    ):
        if not isinstance(management.get(field), bool):
            raise ValueError(
                f"cache deployment {index} management.{field} "
                "must be boolean"
            )
    if management["remote_restart"] or management["cache_clear"]:
        raise ValueError(
            "cache deployment console cannot expose remote restart "
            "or cache clear"
        )


def _reject_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError(
                    "cache deployment catalog contains forbidden field: "
                    + ".".join((*path, str(key)))
                )
            _reject_secrets(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, (*path, str(index)))


def _deployment_view(
    item: dict[str, Any],
    endpoint_index: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    target = item["target"]
    endpoint_value = endpoint_index.get(str(target["endpoint_id"]))
    endpoint = endpoint_value.get("endpoint", {}) if endpoint_value else {}
    status = endpoint_value.get("status", {}) if endpoint_value else {}
    endpoint_management = (
        endpoint_value.get("management", {}) if endpoint_value else {}
    )
    worker_id = target.get("worker_id")
    worker = _target_worker(status, worker_id)
    observed = _observed(
        endpoint,
        status,
        worker,
        worker_required=bool(worker_id),
    )
    observed["cache_health"] = _cache_health(item, status, observed)
    desired = _desired(item, endpoint, settings)
    drift = _drift(item, endpoint, observed, desired, worker)
    return {
        "id": str(item["id"]),
        "title": str(item["title"]),
        "node": str(item["node"]),
        "target": deepcopy(target),
        "declared": {
            key: deepcopy(item[key])
            for key in (
                "strategy",
                "flow_label",
                "lifecycle",
                "layers",
                "services",
                "paths",
                "telemetry",
                "limitations",
            )
        },
        "desired": desired,
        "validated": deepcopy(item["validation"]),
        "observed": observed,
        "drift": drift,
        "state": _deployment_state(item, observed, drift),
        "management": _management(
            item,
            endpoint,
            endpoint_management,
        ),
    }


def _target_worker(
    status: dict[str, Any],
    worker_id: Any,
) -> dict[str, Any] | None:
    if not worker_id:
        return None
    workers = status.get("detail", {}).get("workers", [])
    if not isinstance(workers, list):
        return None
    return next(
        (
            worker
            for worker in workers
            if str(worker.get("worker_id")) == str(worker_id)
        ),
        None,
    )


def _observed(
    endpoint: dict[str, Any],
    status: dict[str, Any],
    worker: dict[str, Any] | None,
    *,
    worker_required: bool,
) -> dict[str, Any]:
    detail = status.get("detail", {})
    lmcache = detail.get("lmcache", {})
    endpoint_found = bool(endpoint)
    runtime_healthy = bool(status.get("healthy")) if endpoint_found else False
    if worker_required:
        runtime_healthy = runtime_healthy and bool(
            worker and worker.get("ready")
        )
    return {
        "target_found": endpoint_found and (
            not worker_required or worker is not None
        ),
        "runtime_healthy": runtime_healthy,
        "endpoint": (
            {
                "id": endpoint.get("id"),
                "model": endpoint.get("public_model"),
                "backend_type": endpoint.get("backend_type"),
                "enabled": bool(endpoint.get("enabled")),
                "auto_candidate": bool(endpoint.get("auto_candidate")),
                "safe_context_tokens": endpoint.get(
                    "safe_context_tokens"
                ),
                "configured_context_tokens": endpoint.get(
                    "configured_context_tokens"
                ),
                "max_concurrency": endpoint.get("max_concurrency"),
                "modalities": list(endpoint.get("modalities", [])),
            }
            if endpoint_found
            else None
        ),
        "status": {
            "healthy": bool(status.get("healthy")),
            "checked_at": status.get("checked_at"),
            "load_headroom": status.get("load_headroom"),
            "cache_generation": status.get("cache_generation"),
        },
        "worker": _worker_observation(worker),
        "cache": {
            "gpu_apc": {
                "queries": _number(detail.get("prefix_cache_queries")),
                "hit_tokens": _number(detail.get("prefix_cache_hits")),
            },
            "external": {
                "queries": _number(
                    detail.get("external_prefix_cache_queries")
                ),
                "hit_tokens": _number(
                    detail.get("external_prefix_cache_hits")
                ),
                "transferred_tokens": _number(
                    detail.get("prompt_tokens_external_transfer")
                ),
            },
            "lmcache": {
                key: deepcopy(lmcache.get(key))
                for key in (
                    "supported",
                    "healthy",
                    "registered",
                    "registered_count",
                    "expected_registrations",
                    "connector_active",
                    "generation",
                    "chunk_size",
                    "memory_used_bytes",
                    "memory_total_bytes",
                    "lookup_requested_tokens",
                    "lookup_hit_tokens",
                    "l1_read_chunks",
                    "l1_write_chunks",
                    "error",
                )
                if key in lmcache
            },
        },
    }


def _cache_health(item: dict[str, Any], status: dict[str, Any],
                  observed: dict[str, Any]) -> dict[str, Any]:
    # Model readiness and historical hit counters cannot establish cache readiness.
    result = {"healthy": None, "source": None, "checked_at": status.get("checked_at"),
              "reason": "cache_probe_unavailable"}
    checked = _number(status.get("checked_at"))
    if checked is None or not 0 <= time.time() - checked <= 120:
        result["reason"] = "cache_evidence_stale"
        return result
    if item["strategy"] != "lmcache_dram":
        return result
    cache = observed["cache"]["lmcache"]
    if cache.get("supported") is not True:
        return result
    result["source"] = "lmcache_probe"
    if cache.get("healthy") is False:
        result.update(healthy=False, reason="cache_service_unhealthy")
    elif cache.get("healthy") is True and cache.get("connector_active") is True:
        result.update(healthy=True, reason="service_and_connector_ready")
    elif cache.get("connector_active") is False:
        result.update(healthy=False, reason="cache_connector_inactive")
    return result


def _desired(
    item: dict[str, Any],
    endpoint: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    value = {
        "endpoint_enabled": (
            bool(endpoint.get("enabled")) if endpoint else None
        ),
        "auto_candidate": (
            bool(endpoint.get("auto_candidate")) if endpoint else None
        ),
    }
    if item["management"].get("lmcache_settings"):
        value["lmcache"] = deepcopy(settings.get("lmcache", {}))
    return value


def _worker_observation(
    worker: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if worker is None:
        return None
    allowed = (
        "worker_id",
        "state",
        "ready",
        "routing_enabled",
        "safe_context_tokens",
        "context_size",
        "cache_type_k",
        "cache_type_v",
        "context_checkpoints",
        "prefill_tokens_per_second",
        "modalities",
        "vision_status",
        "max_images",
        "runtime_fingerprint",
        "cache_generation",
        "artifact_verification",
        "config_drift",
    )
    return {
        key: deepcopy(worker.get(key))
        for key in allowed
        if key in worker
    }


def _drift(
    item: dict[str, Any],
    endpoint: dict[str, Any],
    observed: dict[str, Any],
    desired: dict[str, Any],
    worker: dict[str, Any] | None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not endpoint:
        return [
            _drift_item(
                "target_missing",
                "critical",
                "Registry target is unavailable.",
            )
        ]
    if item["lifecycle"] == "planned":
        result.append(
            _drift_item(
                "planned_not_deployed",
                "warning",
                "The persistent cache deployment is planned but not active.",
            )
        )
    if not observed["status"]["healthy"]:
        result.append(
            _drift_item(
                "endpoint_unhealthy",
                "critical",
                "Endpoint health check is failing.",
            )
        )
    if item["target"].get("worker_id"):
        if worker is None:
            result.append(
                _drift_item(
                    "worker_missing",
                    "critical",
                    "Fleet worker is absent from the live probe.",
                )
            )
        elif not worker.get("ready"):
            result.append(
                _drift_item(
                    "worker_not_ready",
                    "critical",
                    "Fleet worker is not ready for scheduling.",
                )
            )
        for code in worker.get("config_drift", []) if worker else []:
            result.append(
                _drift_item(
                    f"worker_{code}",
                    "warning",
                    f"Fleet worker reports configuration drift: {code}.",
                )
            )
    if item["management"].get("lmcache_settings"):
        lmcache = observed["cache"]["lmcache"]
        configured = desired.get("lmcache", {})
        expected = configured.get("enabled")
        active = lmcache.get("connector_active")
        evidence = observed["cache_health"]
        if evidence["reason"] == "cache_service_unhealthy":
            result.append(_drift_item(
                "lmcache_unhealthy", "critical",
                "LMCache service probe is failing."))
        elif (evidence["source"] == "lmcache_probe"
              and isinstance(expected, bool) and isinstance(active, bool)
              and expected != active):
            result.append(_drift_item(
                "lmcache_restart_required", "warning",
                "LMCache desired state differs from the attached connector."))
    if (
        item.get("telemetry", {}).get("status") == "partial"
        and item["lifecycle"] == "deployed"
    ):
        result.append(
            _drift_item(
                "aggregate_telemetry_unavailable",
                "info",
                "Only request-level cache evidence is available.",
            )
        )
    return result


def _deployment_state(
    item: dict[str, Any],
    observed: dict[str, Any],
    drift: list[dict[str, str]],
) -> dict[str, str]:
    if any(entry["severity"] == "critical" for entry in drift):
        return {
            "code": "unavailable",
            "label": "Unavailable",
            "tone": "danger",
        }
    if any(entry["severity"] == "warning" for entry in drift):
        return {
            "code": "warning",
            "label": (
                "Planned"
                if item["lifecycle"] == "planned"
                else "Configuration drift"
            ),
            "tone": "warning",
        }
    if observed["runtime_healthy"] and observed["cache_health"]["healthy"] is True:
        return {
            "code": "healthy",
            "label": "Healthy",
            "tone": "success",
        }
    return {"code": "unknown", "label": "Unknown", "tone": "neutral"}


def _management(
    item: dict[str, Any],
    endpoint: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    capabilities = item["management"]
    actions: list[dict[str, str]] = []
    if capabilities.get("lmcache_settings"):
        actions.append(
            {
                "id": "lmcache-settings",
                "label": "Configure LMCache",
                "kind": "navigate",
                "target": "settings:lmcache",
            }
        )
    if capabilities.get("endpoint_actions") and endpoint:
        actions.extend(
            [
                {
                    "id": (
                        "disable"
                        if endpoint.get("enabled")
                        else "enable"
                    ),
                    "label": (
                        "Disable"
                        if endpoint.get("enabled")
                        else "Enable"
                    ),
                    "kind": "endpoint",
                },
                {
                    "id": (
                        "auto-disable"
                        if endpoint.get("auto_candidate")
                        else "auto-enable"
                    ),
                    "label": (
                        "Leave automatic routing"
                        if endpoint.get("auto_candidate")
                        else "Join automatic routing"
                    ),
                    "kind": "endpoint",
                },
            ]
        )
    return {
        "endpoint_id": endpoint.get("id") if endpoint else None,
        "revision": record.get("revision"),
        "actions": actions,
        "remote_restart": False,
        "cache_clear": False,
        "read_only": not bool(actions),
    }


def _drift_item(
    code: str,
    severity: str,
    message: str,
) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
    }


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value
