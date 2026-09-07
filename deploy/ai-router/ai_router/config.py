from __future__ import annotations

import copy
import math
from dataclasses import replace
import os
from pathlib import Path
import re
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
        self.model_aliases = _model_aliases_from_dict(
            raw.get("model_aliases", {}),
            self.endpoints,
        )
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
        value.model_aliases = copy.deepcopy(self.model_aliases)
        value.tier_ranks = dict(self.tier_ranks)
        return value

    def by_id(self, endpoint_id: str) -> Endpoint | None:
        return self._by_id.get(endpoint_id)

    def by_public_model(self, model: str) -> tuple[Endpoint, ...]:
        direct = self._by_public_model.get(model)
        if direct is not None:
            return tuple(direct)
        alias = self.model_aliases.get(model)
        if alias is None:
            return ()
        result = []
        for endpoint_id in alias["endpoint_ids"]:
            endpoint = self._by_id.get(endpoint_id)
            if endpoint is None:
                continue
            metadata = copy.deepcopy(endpoint.metadata)
            metadata["requested_model_alias"] = model
            if alias["deployment_profile_ids"]:
                metadata["allowed_deployment_profile_ids"] = list(
                    alias["deployment_profile_ids"]
                )
            if alias["max_input_tokens"] is not None:
                metadata["model_alias_max_input_tokens"] = alias[
                    "max_input_tokens"
                ]
            if alias["max_output_tokens"] is not None:
                metadata["model_alias_max_output_tokens"] = alias[
                    "max_output_tokens"
                ]
            result.append(
                replace(
                    endpoint,
                    public_model=model,
                    metadata=metadata,
                )
            )
        return tuple(result)

    def public_models(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self._by_public_model) | set(self.model_aliases))
        )

    def enabled_public_models(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    model
                    for model in self.public_models()
                    if any(
                        item.enabled
                        for item in self.by_public_model(model)
                    )
                }
            )
        )

    def responders(self) -> tuple[Endpoint, ...]:
        return tuple(item for item in self.endpoints if item.role == "responder")


def _model_aliases_from_dict(
    value: Any,
    endpoints: tuple[Endpoint, ...],
) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("registry model_aliases must be an object")
    by_id = {item.id: item for item in endpoints}
    public_models = {item.public_model for item in endpoints}
    aliases: dict[str, dict[str, Any]] = {}
    for raw_model, raw_constraints in value.items():
        model = str(raw_model).strip()
        if not model or model == "auto":
            raise ValueError("model alias must be a non-auto model ID")
        if model in public_models:
            raise ValueError(
                f"model alias conflicts with endpoint public_model: {model}"
            )
        if not isinstance(raw_constraints, dict):
            raise ValueError(f"model alias {model} must be an object")
        endpoint_ids = tuple(
            str(item)
            for item in raw_constraints.get("endpoint_ids", [])
        )
        if not endpoint_ids:
            raise ValueError(f"model alias {model} requires endpoint_ids")
        unknown_endpoints = sorted(set(endpoint_ids) - set(by_id))
        if unknown_endpoints:
            raise ValueError(
                f"model alias {model} has unknown endpoints: "
                + ", ".join(unknown_endpoints)
            )
        deployment_profile_ids = tuple(
            str(item)
            for item in raw_constraints.get(
                "deployment_profile_ids",
                [],
            )
        )
        available_profiles = {
            profile.id
            for endpoint_id in endpoint_ids
            for profile in by_id[endpoint_id].deployment_profiles
        }
        unknown_profiles = sorted(
            set(deployment_profile_ids) - available_profiles
        )
        if unknown_profiles:
            raise ValueError(
                f"model alias {model} has unknown deployment profiles: "
                + ", ".join(unknown_profiles)
            )
        max_input_tokens = raw_constraints.get("max_input_tokens")
        max_output_tokens = raw_constraints.get("max_output_tokens")
        if max_input_tokens is not None and int(max_input_tokens) <= 0:
            raise ValueError(
                f"model alias {model} max_input_tokens must be positive"
            )
        if max_output_tokens is not None and int(max_output_tokens) <= 0:
            raise ValueError(
                f"model alias {model} max_output_tokens must be positive"
            )
        aliases[model] = {
            "endpoint_ids": endpoint_ids,
            "deployment_profile_ids": deployment_profile_ids,
            "max_input_tokens": (
                int(max_input_tokens)
                if max_input_tokens is not None
                else None
            ),
            "max_output_tokens": (
                int(max_output_tokens)
                if max_output_tokens is not None
                else None
            ),
        }
    return aliases


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
    metadata_value = value.get("metadata", {}) or {}
    if not isinstance(metadata_value, dict):
        raise ValueError("endpoint metadata must be an object")
    read_timeout = metadata_value.get("upstream_read_timeout_seconds")
    if read_timeout is not None and not 1 <= float(read_timeout) <= 3600:
        raise ValueError("endpoint upstream_read_timeout_seconds must be between 1 and 3600")
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
            output_token_limit=bool(
                capabilities_value.get("output_token_limit", True)
            ),
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
        metadata=copy.deepcopy(metadata_value),
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
                allow_compaction=bool(
                    value.get("allow_compaction", False)
                ),
                disclosure_mode=str(
                    value.get("disclosure_mode", "internal")
                ),
            )
        )
    return tuple(result)


def validate_settings(value: dict[str, Any]) -> None:
    from .lmcache_runtime import validate_lmcache_settings
    from .prompt_directives import validate_prompt_directives
    from .privacy_review import validate_review_settings

    validate_review_settings(value.get("identity", {}).get("review", {}))
    validate_lmcache_settings(value.get("lmcache", {}))
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

    identity = value.get("identity", {})
    identity_fields = (
        "public_model_id",
        "display_name_zh",
        "display_name_en",
        "provider_name",
        "description",
        "identity_response",
    )
    if bool(identity.get("enabled", False)) and any(
        not str(identity.get(field, "")).strip()
        for field in identity_fields
    ):
        raise ValueError(
            "identity fields must be present before identity masking is enabled"
        )
    public_model_id = str(
        identity.get("public_model_id", "")
    ).strip()
    if public_model_id and (
        len(public_model_id) > 128
        or public_model_id == "auto"
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]*",
            public_model_id,
        )
    ):
        raise ValueError(
            "identity.public_model_id must be a valid non-auto model ID"
        )
    for field in (
        "display_name_zh",
        "display_name_en",
        "provider_name",
    ):
        if len(str(identity.get(field, ""))) > 120:
            raise ValueError(f"identity.{field} must not exceed 120 characters")
    for field in ("description", "identity_response"):
        if len(str(identity.get(field, ""))) > 2000:
            raise ValueError(f"identity.{field} must not exceed 2000 characters")

    affinity = value.get("affinity", {})
    affinity_ttl = int(affinity.get("ttl_seconds", 0))
    if not 300 <= affinity_ttl <= 86400:
        raise ValueError(
            "affinity.ttl_seconds must be between 300 and 86400"
        )
    if int(affinity.get("max_priority_burst", 0)) <= 0:
        raise ValueError("affinity.max_priority_burst must be positive")

    prefix_affinity = value.get("prefix_affinity", {})
    if not isinstance(prefix_affinity, dict):
        raise ValueError("prefix_affinity must be an object")
    if not isinstance(prefix_affinity.get("enabled"), bool):
        raise ValueError("prefix_affinity.enabled must be a boolean")
    prefix_ttl = int(prefix_affinity.get("ttl_seconds", 0))
    if not 300 <= prefix_ttl <= 604800:
        raise ValueError(
            "prefix_affinity.ttl_seconds must be between 300 and 604800"
        )
    if int(prefix_affinity.get("min_prompt_tokens", 0)) <= 0:
        raise ValueError(
            "prefix_affinity.min_prompt_tokens must be positive"
        )
    if int(prefix_affinity.get("revision", 0)) <= 0:
        raise ValueError("prefix_affinity.revision must be positive")
    if int(prefix_affinity.get("legacy_revision", 0)) <= 0:
        raise ValueError(
            "prefix_affinity.legacy_revision must be positive"
        )
    checkpoint_tokens = int(
        prefix_affinity.get("checkpoint_tokens", 0)
    )
    if checkpoint_tokens <= 0:
        raise ValueError(
            "prefix_affinity.checkpoint_tokens must be positive"
        )
    partial_min_tokens = int(
        prefix_affinity.get("partial_min_tokens", 0)
    )
    if partial_min_tokens < checkpoint_tokens:
        raise ValueError(
            "prefix_affinity.partial_min_tokens must be at least one "
            "checkpoint"
        )
    if (
        int(
            prefix_affinity.get(
                "replica_on_busy_min_prompt_tokens",
                0,
            )
        )
        <= 0
    ):
        raise ValueError(
            "prefix_affinity.replica_on_busy_min_prompt_tokens "
            "must be positive"
        )
    if not isinstance(
        prefix_affinity.get("capture_templates"),
        bool,
    ):
        raise ValueError(
            "prefix_affinity.capture_templates must be a boolean"
        )
    template_dir = str(
        prefix_affinity.get("template_dir", "")
    ).strip()
    if not template_dir or not Path(template_dir).is_absolute():
        raise ValueError(
            "prefix_affinity.template_dir must be an absolute path"
        )
    if int(prefix_affinity.get("template_min_tokens", 0)) <= 0:
        raise ValueError(
            "prefix_affinity.template_min_tokens must be positive"
        )
    template_client_ids = prefix_affinity.get(
        "template_client_ids",
        [],
    )
    if (
        not isinstance(template_client_ids, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in template_client_ids
        )
        or len(template_client_ids)
        != len(set(template_client_ids))
    ):
        raise ValueError(
            "prefix_affinity.template_client_ids must be a unique "
            "string array"
        )

    evaluator = value.get("evaluator", {})
    confidence = float(evaluator.get("confidence_threshold", 0))
    if not 0 <= confidence <= 1:
        raise ValueError("evaluator.confidence_threshold must be between 0 and 1")

    routing = value.get("routing", {})
    validate_prompt_directives(routing.get("prompt_directives", {}))
    stability = routing.get("conversation_stability", {})
    if not isinstance(stability, dict):
        raise ValueError(
            "routing.conversation_stability must be an object"
        )
    if not isinstance(stability.get("enabled"), bool):
        raise ValueError(
            "routing.conversation_stability.enabled must be a boolean"
        )
    failure_threshold = int(
        stability.get("health_failure_threshold", 0)
    )
    if not 1 <= failure_threshold <= 5:
        raise ValueError(
            "routing.conversation_stability.health_failure_threshold "
            "must be between 1 and 5"
        )
    recheck_interval = float(
        stability.get("health_recheck_interval_seconds", 0)
    )
    if not 0 <= recheck_interval <= 60:
        raise ValueError(
            "routing.conversation_stability."
            "health_recheck_interval_seconds must be between 0 and 60"
        )
    if stability.get("recovery_mode") not in {
        "manual",
        "next_turn",
        "when_idle",
    }:
        raise ValueError(
            "routing.conversation_stability.recovery_mode must be "
            "manual, next_turn, or when_idle"
        )
    if not isinstance(
        stability.get("preserve_tier_after_migration"),
        bool,
    ):
        raise ValueError(
            "routing.conversation_stability."
            "preserve_tier_after_migration must be a boolean"
        )
    pins = routing.get("client_deployment_pins", [])
    if not isinstance(pins, list):
        raise ValueError("routing.client_deployment_pins must be an array")
    pin_scopes: set[tuple[str, str]] = set()
    pin_fields = {
        "client_id", "model", "endpoint_id", "deployment_id",
        "capacity_wait_seconds",
    }
    for pin in pins:
        if (
            not isinstance(pin, dict)
            or not pin_fields.issubset(pin)
            or set(pin) - pin_fields - {"context_overflow_deployment_id", "prewarm_min_prompt_tokens"}
        ):
            raise ValueError("routing.client_deployment_pins has invalid fields")
        for field in pin_fields - {"capacity_wait_seconds"}:
            text = pin[field]
            if not isinstance(text, str) or not text.strip() or text != text.strip():
                raise ValueError(
                    "routing.client_deployment_pins requires nonempty strings"
                )
        overflow = pin.get("context_overflow_deployment_id")
        if "context_overflow_deployment_id" in pin and (
            not isinstance(overflow, str) or not overflow.strip()
            or overflow != overflow.strip() or overflow == pin["deployment_id"]
        ):
            raise ValueError("routing.client_deployment_pins has invalid context overflow deployment")
        if "prewarm_min_prompt_tokens" in pin:
            threshold = pin["prewarm_min_prompt_tokens"]
            if (not overflow or isinstance(threshold, bool)
                or not isinstance(threshold, int) or not 4096 <= threshold <= 250000):
                raise ValueError("routing.client_deployment_pins has invalid prewarm threshold")
        if pin["model"] == "auto":
            raise ValueError("routing.client_deployment_pins requires an explicit model")
        scope = (pin["client_id"], pin["model"])
        if scope in pin_scopes:
            raise ValueError("routing.client_deployment_pins scopes must be unique")
        pin_scopes.add(scope)
        wait = pin["capacity_wait_seconds"]
        if (
            isinstance(wait, bool)
            or not isinstance(wait, (int, float))
            or not math.isfinite(wait)
            or not 0 < wait <= 3600
        ):
            raise ValueError(
                "routing.client_deployment_pins wait must be between 0 and 3600 seconds"
            )
    strategy = str(routing.get("strategy", "legacy_v1"))
    if strategy not in {"legacy_v1", "intelligent_v2"}:
        raise ValueError(
            "routing.strategy must be legacy_v1 or intelligent_v2"
        )
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
    if int(routing.get("auto_max_input_tokens", 0)) <= 0:
        raise ValueError(
            "routing.auto_max_input_tokens must be positive"
        )
    if int(routing.get("auto_max_output_tokens", 0)) <= 0:
        raise ValueError(
            "routing.auto_max_output_tokens must be positive"
        )
    remote_orders = routing.get("remote_fallback_order", {})
    expected_profiles = {
        "general",
        "agent_text",
        "code",
        "complex_code",
        "multimodal",
        "multimodal_complex_code",
    }
    if not isinstance(remote_orders, dict):
        raise ValueError(
            "routing.remote_fallback_order must be an object"
        )
    if strategy == "intelligent_v2" and set(remote_orders) != expected_profiles:
        raise ValueError(
            "routing.remote_fallback_order must define general, agent_text, "
            "code, complex_code, multimodal, and multimodal_complex_code"
        )
    for profile, order in remote_orders.items():
        if profile not in expected_profiles:
            raise ValueError(
                f"unsupported remote fallback profile: {profile}"
            )
        if (
            not isinstance(order, list)
            or not order
            or any(not str(item).strip() for item in order)
            or len(order) != len(set(str(item) for item in order))
        ):
            raise ValueError(
                f"routing.remote_fallback_order.{profile} must be a "
                "non-empty unique list"
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

    health = value.get("health", {})
    refresh_seconds = float(health.get("refresh_seconds", 0))
    stale_after_seconds = float(
        health.get("stale_after_seconds", 0)
    )
    probe_timeout_seconds = float(
        health.get("probe_timeout_seconds", 0)
    )
    if not 1 <= refresh_seconds <= 60:
        raise ValueError(
            "health.refresh_seconds must be between 1 and 60"
        )
    if not refresh_seconds <= stale_after_seconds <= 300:
        raise ValueError(
            "health.stale_after_seconds must be between "
            "health.refresh_seconds and 300"
        )
    if not 1 <= probe_timeout_seconds <= 60:
        raise ValueError(
            "health.probe_timeout_seconds must be between 1 and 60"
        )

    failover = value.get("failover", {})
    attempts = int(failover.get("max_attempts", 0))
    if attempts < 1 or attempts > 3:
        raise ValueError("failover.max_attempts must be between 1 and 3")

    cloud = value.get("cloud", {})
    if float(cloud.get("monthly_budget", 0)) < 0:
        raise ValueError("cloud.monthly_budget must not be negative")

    compaction = value.get("compaction", {})
    if compaction.get("mode", "explicit_only") not in {
        "explicit_only",
        "automatic",
        "disabled",
    }:
        raise ValueError(
            "compaction.mode must be explicit_only, automatic, or disabled"
        )

    clients = value.get("clients", {}).get("policies", [])
    ids = [str(item.get("id", "")) for item in clients]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("client policy IDs must be present and unique")
    for client in clients:
        if client.get("disclosure_mode", "internal") not in {
            "public",
            "internal",
        }:
            raise ValueError(
                "client disclosure_mode must be public or internal"
            )
        if (
            int(client.get("rpm_limit", 0)) <= 0
            or int(client.get("tpm_limit", 0)) <= 0
            or int(client.get("max_parallel_requests", 0)) <= 0
        ):
            raise ValueError("client policy limits must be positive")
