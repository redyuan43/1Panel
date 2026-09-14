"""Per-endpoint context strategy; never mutate shared registry or health state."""
from __future__ import annotations

import copy
from dataclasses import replace

from .types import Endpoint, EndpointStatus


def validate_target(policy, registry):
    if not isinstance(policy, dict):
        raise ValueError("context_policy must be an object")
    if policy.get("mode", "legacy") == "legacy":
        return
    endpoint = registry.by_id(policy.get("endpoint_id", "codex-pro-gpt-6-astra"))
    if endpoint is None or endpoint.backend_type != "codex_pool":
        raise ValueError("context_policy.endpoint_id must identify a registered Codex endpoint")


def strategy_for(settings, endpoint: Endpoint | None) -> str:
    policy = settings.section("context_policy")
    if endpoint is None or endpoint.backend_type != "codex_pool":
        return "legacy"
    if endpoint.id != policy.get("endpoint_id", "codex-pro-gpt-6-astra"):
        return "legacy"
    return policy.get("mode", "legacy")


def request_strategy(settings, registry, requested_model, evaluation) -> str:
    if evaluation.required_endpoint_id:
        endpoint = registry.by_id(evaluation.required_endpoint_id)
    else:
        endpoints = registry.by_public_model(requested_model)
        endpoint = endpoints[0] if len(endpoints) == 1 else None
    return strategy_for(settings, endpoint)


def apply_context_policy(settings, endpoint: Endpoint, status: EndpointStatus):
    if strategy_for(settings, endpoint) != "extended":
        return endpoint, status
    requested = settings.section("context_policy").get("extended_context_tokens", 500000)
    ceiling = min(requested, endpoint.configured_context_tokens)
    detail = copy.deepcopy(status.detail)
    for worker in detail.get("workers", []):
        # Old adapters and missing per-model evidence cannot unlock a window.
        model_limits = worker.get("model_context_limits", {})
        limits = model_limits.get(endpoint.provider_model, {}) if isinstance(model_limits, dict) else {}
        limits = limits if isinstance(limits, dict) else {}
        maximum = limits.get("max_context_tokens")
        if type(maximum) is not int or maximum <= 0:
            maximum = int(worker.get("safe_context_tokens", 0))
        worker["safe_context_tokens"] = min(ceiling, maximum)
    available_ids = set(detail.get("available_worker_ids", []))
    eligible = max((worker["safe_context_tokens"] for worker in detail.get("workers", [])
                    if worker.get("ready") and worker.get("worker_id") in available_ids), default=0)
    # A configured limit is not a health or entitlement signal.
    return replace(endpoint, safe_context_tokens=min(ceiling, eligible)), replace(
        status, detail=detail, eligible_context_tokens=eligible,
    )
