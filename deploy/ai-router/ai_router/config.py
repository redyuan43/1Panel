from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from .types import (
    ClientPolicy,
    DeploymentProfile,
    Endpoint,
    EndpointCapabilities,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


class Settings:
    def __init__(
        self,
        defaults_path: Path | None = None,
        runtime_path: Path | None = None,
    ) -> None:
        self.defaults_path = defaults_path or Path(
            os.environ.get("AI_ROUTER_DEFAULTS_PATH", DEFAULT_CONFIG_DIR / "defaults.yaml")
        )
        self.runtime_path = runtime_path or Path(
            os.environ.get("AI_ROUTER_RUNTIME_SETTINGS_PATH", "/data/settings.yaml")
        )
        self._value: dict[str, Any] = {}
        self.reload()

    def reload(self) -> dict[str, Any]:
        defaults = load_yaml(self.defaults_path)
        runtime = load_yaml(self.runtime_path, required=False)
        self._value = deep_merge(defaults, runtime)
        _preserve_legacy_weight_total(runtime, self._value)
        validate_settings(self._value)
        return self._value

    @property
    def value(self) -> dict[str, Any]:
        return copy.deepcopy(self._value)

    def section(self, name: str) -> dict[str, Any]:
        value = self._value.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def write_runtime(self, value: dict[str, Any]) -> None:
        merged = deep_merge(load_yaml(self.defaults_path), value)
        _preserve_legacy_weight_total(value, merged)
        validate_settings(merged)
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_path.with_suffix(self.runtime_path.suffix + ".new")
        with temporary.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.runtime_path)
        self.reload()


def _preserve_legacy_weight_total(
    override: dict[str, Any],
    merged: dict[str, Any],
) -> None:
    override_weights = (
        override.get("routing", {}).get("weights", {})
        if isinstance(override.get("routing"), dict)
        else {}
    )
    if (
        not isinstance(override_weights, dict)
        or "cost" in override_weights
        or not override_weights
    ):
        return
    try:
        legacy_total = sum(float(item) for item in override_weights.values())
    except (TypeError, ValueError):
        return
    if abs(legacy_total - 1.0) > 0.001:
        return
    weights = merged.get("routing", {}).get("weights", {})
    if isinstance(weights, dict):
        weights["cost"] = 0.0


class Registry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("AI_ROUTER_REGISTRY_PATH", DEFAULT_CONFIG_DIR / "registry.yaml"))
        raw = load_yaml(self.path)
        endpoint_values = raw.get("endpoints", [])
        if not isinstance(endpoint_values, list):
            raise ValueError("registry endpoints must be a list")
        self.endpoints = tuple(endpoint_from_dict(item) for item in endpoint_values)
        self._by_id = {item.id: item for item in self.endpoints}
        self._by_public_model: dict[str, list[Endpoint]] = {}
        for endpoint in self.endpoints:
            self._by_public_model.setdefault(endpoint.public_model, []).append(endpoint)
        self.tier_ranks = {
            str(key): int(value)
            for key, value in (raw.get("tier_ranks", {}) or {}).items()
        }

    def with_endpoints(
        self,
        endpoints: list[Endpoint] | tuple[Endpoint, ...],
    ) -> "Registry":
        value = object.__new__(Registry)
        value.path = self.path
        value.endpoints = tuple(endpoints)
        value._by_id = {item.id: item for item in value.endpoints}
        value._by_public_model = {}
        for endpoint in value.endpoints:
            value._by_public_model.setdefault(
                endpoint.public_model,
                [],
            ).append(endpoint)
        value.tier_ranks = dict(self.tier_ranks)
        return value

    def by_id(self, endpoint_id: str) -> Endpoint | None:
        return self._by_id.get(endpoint_id)

    def by_public_model(self, model: str) -> tuple[Endpoint, ...]:
        return tuple(self._by_public_model.get(model, ()))

    def public_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_public_model))

    def enabled_public_models(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.public_model
                    for item in self.endpoints
                    if item.enabled
                }
            )
        )

    def responders(self) -> tuple[Endpoint, ...]:
        return tuple(item for item in self.endpoints if item.role == "responder")


def endpoint_from_dict(value: dict[str, Any]) -> Endpoint:
    if not isinstance(value, dict):
        raise ValueError("registry endpoint must be an object")
    required = {
        "id",
        "public_model",
        "provider_model",
        "api_base",
        "node",
        "role",
        "tier",
        "tier_rank",
        "modalities",
        "tasks",
        "safe_context_tokens",
        "configured_context_tokens",
        "max_concurrency",
        "backend_type",
        "health_url",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"registry endpoint is missing: {', '.join(missing)}")
    capabilities_value = value.get("capabilities", {}) or {}
    if not isinstance(capabilities_value, dict):
        raise ValueError("endpoint capabilities must be an object")
    responses_mode = str(
        capabilities_value.get("responses", "none")
    )
    tools_mode = str(
        capabilities_value.get(
            "tools",
            "single" if value.get("tools", False) else "none",
        )
    )
    if responses_mode not in {"none", "native", "adapter"}:
        raise ValueError(
            "endpoint capabilities.responses must be none, native, or adapter"
        )
    if tools_mode not in {"none", "single", "parallel"}:
        raise ValueError(
            "endpoint capabilities.tools must be none, single, or parallel"
        )
    tool_choice_modes = tuple(
        str(item)
        for item in capabilities_value.get("tool_choice_modes", [])
    )
    if not set(tool_choice_modes).issubset(
        {"auto", "none", "required", "function"}
    ):
        raise ValueError(
            "endpoint tool_choice_modes contains an unsupported mode"
        )
    tool_choice_supported = bool(
        capabilities_value.get("tool_choice", tools_mode != "none")
    )
    if tool_choice_modes and not tool_choice_supported:
        raise ValueError(
            "endpoint tool_choice_modes requires tool_choice support"
        )
    structured_output = tuple(
        str(item)
        for item in capabilities_value.get("structured_output", [])
    )
    if not set(structured_output).issubset({"json_object", "json_schema"}):
        raise ValueError(
            "endpoint structured_output supports only json_object and json_schema"
        )
    profile_values = value.get("deployment_profiles", []) or []
    if not isinstance(profile_values, list):
        raise ValueError("endpoint deployment_profiles must be a list")
    deployment_profiles = tuple(
        deployment_profile_from_dict(item)
        for item in profile_values
    )
    profile_ids = [item.id for item in deployment_profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ValueError("endpoint deployment profile IDs must be unique")
    return Endpoint(
        id=str(value["id"]),
        public_model=str(value["public_model"]),
        provider_model=str(value["provider_model"]),
        api_base=str(value["api_base"]).rstrip("/"),
        node=str(value["node"]),
        role=str(value["role"]),
        tier=str(value["tier"]),
        tier_rank=int(value["tier_rank"]),
        modalities=tuple(str(item) for item in value["modalities"]),
        tasks=tuple(str(item) for item in value["tasks"]),
        safe_context_tokens=int(value["safe_context_tokens"]),
        configured_context_tokens=int(value["configured_context_tokens"]),
        max_concurrency=int(value["max_concurrency"]),
        backend_type=str(value["backend_type"]),
        health_url=str(value["health_url"]),
        load_url=str(value["load_url"]) if value.get("load_url") else None,
        backend_api_key_env=str(value.get("backend_api_key_env", "AI_ROUTER_BACKEND_API_KEY")),
        enabled=bool(value.get("enabled", True)),
        auto_candidate=bool(value.get("auto_candidate", True)),
        cloud=bool(value.get("cloud", False)),
        capabilities=EndpointCapabilities(
            chat=bool(capabilities_value.get("chat", True)),
            responses=responses_mode,
            tools=tools_mode,
            tool_choice=tool_choice_supported,
            tool_choice_modes=tool_choice_modes,
            structured_output=structured_output,
            streaming=bool(capabilities_value.get("streaming", True)),
            validation_status=str(
                capabilities_value.get(
                    "validation_status",
                    "unverified",
                )
            ),
            validated_at=str(
                capabilities_value.get("validated_at", "")
            ),
        ),
        deployment_profiles=deployment_profiles,
        quality={str(key): float(score) for key, score in (value.get("quality", {}) or {}).items()},
        metadata=copy.deepcopy(value.get("metadata", {}) or {}),
    )


def deployment_profile_from_dict(
    value: dict[str, Any],
) -> DeploymentProfile:
    if not isinstance(value, dict):
        raise ValueError("deployment profile must be an object")
    required = {
        "id",
        "tiers",
        "modalities",
        "context_size",
        "safe_context_tokens",
        "cache_type_k",
        "cache_type_v",
        "short_request_rank",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(
            "deployment profile is missing: " + ", ".join(missing)
        )
    tiers = tuple(str(item) for item in value["tiers"])
    modalities = tuple(str(item) for item in value["modalities"])
    context_size = int(value["context_size"])
    safe_context_tokens = int(value["safe_context_tokens"])
    if not tiers:
        raise ValueError("deployment profile tiers must not be empty")
    if not modalities:
        raise ValueError(
            "deployment profile modalities must not be empty"
        )
    if context_size <= 0 or safe_context_tokens <= 0:
        raise ValueError(
            "deployment profile context limits must be positive"
        )
    if safe_context_tokens > context_size:
        raise ValueError(
            "deployment profile safe context cannot exceed context size"
        )
    max_images_value = value.get("max_images")
    max_images = (
        int(max_images_value)
        if max_images_value is not None
        else None
    )
    if max_images is not None and max_images <= 0:
        raise ValueError(
            "deployment profile max_images must be positive"
        )
    return DeploymentProfile(
        id=str(value["id"]),
        tiers=tiers,
        modalities=modalities,
        context_size=context_size,
        safe_context_tokens=safe_context_tokens,
        cache_type_k=str(value["cache_type_k"]),
        cache_type_v=str(value["cache_type_v"]),
        short_request_rank=int(value["short_request_rank"]),
        vision_status=str(value.get("vision_status", "unverified")),
        max_images=max_images,
    )


def client_policies(settings: Settings) -> tuple[ClientPolicy, ...]:
    values = settings.section("clients").get("policies", [])
    result: list[ClientPolicy] = []
    for value in values:
        result.append(
            ClientPolicy(
                id=str(value["id"]),
                key_env=str(value["key_env"]),
                models=tuple(str(item) for item in value.get("models", ["*"])),
                rpm_limit=int(value["rpm_limit"]),
                tpm_limit=int(value["tpm_limit"]),
                max_parallel_requests=int(value["max_parallel_requests"]),
            )
        )
    return tuple(result)


def validate_settings(value: dict[str, Any]) -> None:
    limits = value.get("limits", {})
    if int(limits.get("max_request_bytes", 0)) <= 0:
        raise ValueError("limits.max_request_bytes must be positive")
    if int(limits.get("image_token_estimate", 0)) <= 0:
        raise ValueError("limits.image_token_estimate must be positive")
    if int(limits.get("audio_token_estimate", 0)) <= 0:
        raise ValueError("limits.audio_token_estimate must be positive")

    vision = value.get("vision", {})
    if int(vision.get("ai_max_dimension", 0)) <= 0:
        raise ValueError("vision.ai_max_dimension must be positive")
    if int(vision.get("max_source_pixels", 0)) <= 0:
        raise ValueError("vision.max_source_pixels must be positive")

    affinity = value.get("affinity", {})
    affinity_ttl = int(affinity.get("ttl_seconds", 0))
    if not 300 <= affinity_ttl <= 86400:
        raise ValueError(
            "affinity.ttl_seconds must be between 300 and 86400"
        )
    if int(affinity.get("max_priority_burst", 0)) <= 0:
        raise ValueError("affinity.max_priority_burst must be positive")

    evaluator = value.get("evaluator", {})
    confidence = float(evaluator.get("confidence_threshold", 0))
    if not 0 <= confidence <= 1:
        raise ValueError("evaluator.confidence_threshold must be between 0 and 1")

    routing = value.get("routing", {})
    if routing.get("provider_priority") not in {
        "local_first",
        "balanced",
        "cloud_first",
    }:
        raise ValueError(
            "routing.provider_priority must be local_first, balanced, or cloud_first"
        )
    affinity_wait = float(
        routing.get("affinity_capacity_wait_seconds", 0)
    )
    new_request_wait = float(
        routing.get("new_request_capacity_wait_seconds", 0)
    )
    if affinity_wait < 0 or new_request_wait < 0:
        raise ValueError("routing capacity wait values must not be negative")
    if routing.get("all_local_busy_policy") not in {
        "cloud_or_429",
        "return_429",
    }:
        raise ValueError(
            "routing.all_local_busy_policy must be cloud_or_429 or return_429"
        )

    weights = routing.get("weights", {})
    expected_weights = {
        "quality",
        "load",
        "latency",
        "context",
        "cost",
        "locality",
    }
    if set(weights) != expected_weights:
        raise ValueError(
            "routing.weights must contain quality, load, latency, context, "
            "cost, and locality"
        )
    total = sum(float(item) for item in weights.values())
    if abs(total - 1.0) > 0.001:
        raise ValueError("routing.weights must add up to 1.0")

    queue = value.get("queue", {})
    if float(queue.get("timeout_seconds", 0)) <= 0:
        raise ValueError("queue.timeout_seconds must be positive")
    if int(queue.get("lock_ttl_seconds", 0)) <= 0:
        raise ValueError("queue.lock_ttl_seconds must be positive")

    failover = value.get("failover", {})
    attempts = int(failover.get("max_attempts", 0))
    if attempts < 1 or attempts > 3:
        raise ValueError("failover.max_attempts must be between 1 and 3")

    cloud = value.get("cloud", {})
    if float(cloud.get("monthly_budget", 0)) < 0:
        raise ValueError("cloud.monthly_budget must not be negative")

    clients = value.get("clients", {}).get("policies", [])
    ids = [str(item.get("id", "")) for item in clients]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("client policy IDs must be present and unique")
    for client in clients:
        if (
            int(client.get("rpm_limit", 0)) <= 0
            or int(client.get("tpm_limit", 0)) <= 0
            or int(client.get("max_parallel_requests", 0)) <= 0
        ):
            raise ValueError("client policy limits must be positive")
