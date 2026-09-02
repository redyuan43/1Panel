from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from typing import Any

from .config import Registry, Settings
from .errors import NoEligibleModelError, RouterError
from .health import HealthMonitor
from .store import StateStore
from .types import (
    ConversationState,
    Endpoint,
    EndpointStatus,
    Evaluation,
    PhysicalDeployment,
    RequestCapabilities,
    RouteDecision,
)


class ConversationRepository:
    def __init__(self, store: StateStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def get(self, conversation_id: str | None) -> ConversationState | None:
        if not conversation_id:
            return None
        value = await self.store.get_json(f"router:conversation:{conversation_id}")
        if not value:
            return None
        state = ConversationState.from_dict(value)
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        if time.time() - state.last_seen >= ttl:
            return None
        return state

    async def save(self, state: ConversationState) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        state.last_seen = time.time()
        await self.store.set_json(
            f"router:conversation:{state.conversation_id}",
            state.to_dict(),
            ttl_seconds=ttl,
        )

    async def map_response(self, response_id: str, conversation_id: str) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        await self.store.set_json(
            f"router:response-conversation:{response_id}",
            {"conversation_id": conversation_id},
            ttl_seconds=ttl,
        )

    async def map_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
        conversation_id: str,
    ) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        for identity in identities:
            await self.store.set_json(
                f"router:history-conversation:{client_id}:{identity}",
                {"conversation_id": conversation_id},
                ttl_seconds=ttl,
            )

    async def conversation_for_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
    ) -> str | None:
        for identity in identities:
            value = await self.store.get_json(
                f"router:history-conversation:{client_id}:{identity}"
            )
            if value and value.get("conversation_id"):
                return str(value["conversation_id"])
        return None

    async def conversation_for_response(self, response_id: str | None) -> str | None:
        if not response_id:
            return None
        value = await self.store.get_json(f"router:response-conversation:{response_id}")
        return str(value["conversation_id"]) if value and value.get("conversation_id") else None


class RoutingPolicy:
    def __init__(
        self,
        registry: Registry,
        settings: Settings,
        health: HealthMonitor,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.health = health

    async def choose(
        self,
        *,
        requested_model: str,
        evaluation: Evaluation,
        prompt_tokens: int,
        output_reserve_tokens: int,
        modalities: set[str],
        image_count: int = 0,
        has_tools: bool,
        required_capabilities: RequestCapabilities | None = None,
        conversation: ConversationState | None,
        excluded_endpoint_ids: set[str] | None = None,
        excluded_deployment_ids: set[str] | None = None,
        routing_key: str = "",
    ) -> RouteDecision:
        excluded = excluded_endpoint_ids or set()
        excluded_deployments = excluded_deployment_ids or set()
        required = required_capabilities or RequestCapabilities(
            protocol="chat",
            tools=has_tools,
        )
        endpoints = (
            list(self.registry.responders())
            if requested_model == "auto"
            else list(self.registry.by_public_model(requested_model))
        )
        if not endpoints:
            raise RouterError(
                f"unknown model: {requested_model}",
                status_code=404,
                code="model_not_found",
            )
        statuses = await self.health.statuses(endpoints)
        candidates: list[Endpoint] = []
        rejections: list[str] = []
        for endpoint in endpoints:
            reason = (
                "excluded"
                if endpoint.id in excluded
                else await self._ineligible_reason(
                    endpoint,
                    statuses[endpoint.id],
                    evaluation=evaluation,
                    prompt_tokens=prompt_tokens,
                    output_reserve_tokens=output_reserve_tokens,
                    modalities=modalities,
                    required_capabilities=required,
                    conversation=conversation,
                    auto=requested_model == "auto",
                    excluded_deployment_ids=excluded_deployments,
                    image_count=image_count,
                )
            )
            if reason:
                rejections.append(f"{endpoint.id}:{reason}")
            else:
                candidates.append(endpoint)
        if not candidates:
            raise NoEligibleModelError(
                "no eligible model is available: " + ", ".join(rejections)
            )

        if conversation:
            pinned = next(
                (item for item in candidates if item.id == conversation.endpoint_id),
                None,
            )
            if pinned:
                decision = RouteDecision(
                    endpoint=pinned,
                    requested_model=requested_model,
                    task=evaluation.task,
                    prompt_tokens=prompt_tokens,
                    output_reserve_tokens=output_reserve_tokens,
                    reason="conversation_affinity",
                    affinity="hit",
                    score=1.0,
                    protocol=required.protocol,
                    native_or_adapter=pinned.capabilities.protocol_mode(
                        required.protocol
                    ),
                    required_capabilities=required.labels(),
                    candidate_rejections=tuple(rejections),
                )
                await self._bind_physical_deployment(
                    decision,
                    statuses[pinned.id],
                    conversation,
                    prompt_tokens + output_reserve_tokens,
                    excluded_deployments,
                    modalities,
                    image_count,
                    routing_key,
                )
                return decision

        if requested_model == "auto":
            candidates = self._apply_provider_priority(
                candidates,
                evaluation,
            )

        if requested_model != "auto":
            endpoint = max(
                candidates,
                key=lambda item: (
                    statuses[item.id].load_headroom,
                    statuses[item.id].latency_score,
                    item.id,
                ),
            )
            migration = bool(
                conversation and endpoint.id != conversation.endpoint_id
            )
            decision = RouteDecision(
                endpoint=endpoint,
                requested_model=requested_model,
                task=evaluation.task,
                prompt_tokens=prompt_tokens,
                output_reserve_tokens=output_reserve_tokens,
                reason="explicit_model_change" if migration else "explicit_model",
                affinity="migrated" if migration else "explicit",
                score=1.0,
                migration=migration,
                previous_endpoint_id=conversation.endpoint_id
                if migration and conversation
                else None,
                protocol=required.protocol,
                native_or_adapter=endpoint.capabilities.protocol_mode(
                    required.protocol
                ),
                required_capabilities=required.labels(),
                candidate_rejections=tuple(rejections),
            )
            await self._bind_physical_deployment(
                decision,
                statuses[endpoint.id],
                conversation,
                prompt_tokens + output_reserve_tokens,
                excluded_deployments,
                modalities,
                image_count,
                routing_key,
            )
            return decision

        scored = [
            (
                self._score(
                    endpoint,
                    statuses[endpoint.id],
                    evaluation.task,
                    prompt_tokens + output_reserve_tokens,
                ),
                endpoint,
            )
            for endpoint in candidates
        ]
        score, endpoint = max(scored, key=lambda item: (item[0], item[1].node == "ai", item[1].id))
        migration = bool(conversation and endpoint.id != conversation.endpoint_id)
        decision = RouteDecision(
            endpoint=endpoint,
            requested_model=requested_model,
            task=evaluation.task,
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=output_reserve_tokens,
            reason=(
                "monotonic_upgrade"
                if migration
                else self._priority_reason(endpoint, evaluation)
            ),
            affinity="migrated" if migration else ("miss" if conversation else "new"),
            score=score,
            migration=migration,
            previous_endpoint_id=conversation.endpoint_id if migration and conversation else None,
            protocol=required.protocol,
            native_or_adapter=endpoint.capabilities.protocol_mode(
                required.protocol
            ),
            required_capabilities=required.labels(),
            candidate_rejections=tuple(rejections),
        )
        await self._bind_physical_deployment(
            decision,
            statuses[endpoint.id],
            conversation,
            prompt_tokens + output_reserve_tokens,
            excluded_deployments,
            modalities,
            image_count,
            routing_key,
        )
        return decision

    async def _ineligible_reason(
        self,
        endpoint: Endpoint,
        status: EndpointStatus,
        *,
        evaluation: Evaluation,
        prompt_tokens: int,
        output_reserve_tokens: int,
        modalities: set[str],
        required_capabilities: RequestCapabilities,
        conversation: ConversationState | None,
        auto: bool,
        excluded_deployment_ids: set[str],
        image_count: int,
    ) -> str | None:
        if not endpoint.enabled:
            return "disabled"
        if auto and not endpoint.auto_candidate:
            return "auto_disabled"
        if await self.health.in_cooldown(endpoint.id):
            return "cooldown"
        stale_after = float(self.settings.section("health").get("stale_after_seconds", 15))
        if not status.is_fresh(time.time(), stale_after):
            return "unhealthy_or_stale"
        if endpoint.backend_type == "ai_pool":
            deployments = await self._eligible_physical_deployments(
                endpoint,
                status,
                required_context=prompt_tokens + output_reserve_tokens,
                modalities=modalities,
                image_count=image_count,
                excluded_deployment_ids=excluded_deployment_ids,
                require_available=False,
            )
            if not deployments:
                return "physical_deployment"
        elif not modalities.issubset(set(endpoint.modalities)):
            return "modality"
        if not endpoint.capabilities.supports(required_capabilities):
            return "capability"
        if endpoint.tasks and "*" not in endpoint.tasks and evaluation.task not in endpoint.tasks:
            return "task"
        required_context = prompt_tokens + output_reserve_tokens
        safe_context = min(
            endpoint.safe_context_tokens,
            status.eligible_context_tokens or endpoint.safe_context_tokens,
        )
        if required_context > safe_context:
            return "context"
        cloud = self.settings.section("cloud")
        if endpoint.cloud and not bool(cloud.get("enabled", False)):
            return "cloud_disabled"
        if auto and endpoint.cloud and not bool(cloud.get("auto_escalate", False)):
            return "cloud_auto_disabled"
        if endpoint.cloud:
            allowed_models = {
                str(item) for item in cloud.get("allowed_models", [])
            }
            allowed_providers = {
                str(item) for item in cloud.get("allowed_providers", [])
            }
            provider = str(endpoint.metadata.get("provider", ""))
            if endpoint.public_model not in allowed_models:
                return "cloud_model_not_allowed"
            if not provider or provider not in allowed_providers:
                return "cloud_provider_not_allowed"
            if endpoint.metadata.get("billing_mode") != "subscription":
                if float(cloud.get("monthly_budget", 0)) <= 0:
                    return "cloud_budget"
                if (
                    endpoint.metadata.get("input_cost_per_million_usd") is None
                    or endpoint.metadata.get("output_cost_per_million_usd") is None
                ):
                    return "cloud_pricing"
        if (
            auto
            and conversation
            and endpoint.tier_rank < conversation.tier_rank
            and not self._cloud_to_local_migration(conversation, endpoint)
        ):
            return "tier_downgrade"
        if evaluation.required_tier:
            required_rank = self.registry.tier_ranks.get(evaluation.required_tier)
            if required_rank is not None and endpoint.tier_rank < required_rank:
                return "tier"
        return None

    def _apply_provider_priority(
        self,
        candidates: list[Endpoint],
        evaluation: Evaluation,
    ) -> list[Endpoint]:
        if evaluation.preferred_tier:
            preferred_rank = self.registry.tier_ranks.get(
                evaluation.preferred_tier
            )
            preferred = [
                item
                for item in candidates
                if preferred_rank is not None
                and item.tier_rank >= preferred_rank
            ]
            if preferred:
                return preferred
        priority = str(
            self.settings.section("routing").get(
                "provider_priority",
                "local_first",
            )
        )
        local = [item for item in candidates if not item.cloud]
        cloud = [item for item in candidates if item.cloud]
        if priority == "local_first" and local:
            return local
        if priority == "cloud_first" and cloud:
            return cloud
        return candidates

    def _priority_reason(
        self,
        endpoint: Endpoint,
        evaluation: Evaluation,
    ) -> str:
        preferred_rank = (
            self.registry.tier_ranks.get(evaluation.preferred_tier)
            if evaluation.preferred_tier
            else None
        )
        if (
            preferred_rank is not None
            and endpoint.tier_rank >= preferred_rank
        ):
            return "preferred_tier"
        priority = str(
            self.settings.section("routing").get(
                "provider_priority",
                "local_first",
            )
        )
        if priority == "local_first" and not endpoint.cloud:
            return "local_priority"
        if priority == "cloud_first" and endpoint.cloud:
            return "cloud_priority"
        return "balanced_score"

    def _cloud_to_local_migration(
        self,
        conversation: ConversationState,
        endpoint: Endpoint,
    ) -> bool:
        previous = self.registry.by_id(conversation.endpoint_id)
        return bool(previous and previous.cloud and not endpoint.cloud)

    async def _bind_physical_deployment(
        self,
        decision: RouteDecision,
        status: EndpointStatus,
        conversation: ConversationState | None,
        required_context: int,
        excluded_deployment_ids: set[str],
        modalities: set[str],
        image_count: int,
        routing_key: str,
    ) -> None:
        endpoint = decision.endpoint
        if endpoint.backend_type not in {"ai_pool", "codex_pool"}:
            decision.deployment_id = endpoint.id
            if not endpoint.cloud:
                decision.upstream_api_base = endpoint.api_base
            return
        if endpoint.backend_type == "ai_pool":
            workers = await self._eligible_physical_deployments(
                endpoint,
                status,
                required_context=required_context,
                modalities=modalities,
                image_count=image_count,
                excluded_deployment_ids=excluded_deployment_ids,
                require_available=False,
            )
        else:
            workers = [
                PhysicalDeployment.from_dict(
                    {
                        "worker_id": str(item.get("worker_id", "")),
                        "api_base": str(item.get("api_base", "")),
                        "profile_id": str(
                            item.get("account_alias", "codex")
                        ),
                        "tier": "codex_account",
                        "priority": 0,
                        "gpu_ids": (),
                        "gpu_uuids": (),
                        "names": (),
                        "port": None,
                        "context_size": int(
                            item.get("safe_context_tokens", 0)
                        ),
                        "safe_context_tokens": int(
                            item.get("safe_context_tokens", 0)
                        ),
                        "cache_type_k": "",
                        "cache_type_v": "",
                        "modalities": endpoint.modalities,
                        "vision_status": str(
                            endpoint.metadata.get(
                                "vision_status",
                                "unverified",
                            )
                        ),
                        "max_images": endpoint.metadata.get(
                            "max_images"
                        ),
                        "runtime_fingerprint": "",
                        "ready": bool(item.get("ready")),
                        "state": str(item.get("state", "unknown")),
                        "config_drift": (),
                        "short_request_rank": 0,
                        "error_code": item.get("error_code"),
                        "cooldown_until": item.get("cooldown_until"),
                    }
                )
                for item in status.detail.get("workers", [])
                if (
                    item.get("worker_id")
                    and str(item["worker_id"])
                    not in excluded_deployment_ids
                    and item.get("ready")
                    and int(item.get("safe_context_tokens", 0))
                    >= required_context
                    and not await self.health.in_cooldown(
                        str(item["worker_id"])
                    )
                )
            ]
        if not workers:
            raise NoEligibleModelError(
                "the local model pool has no physical worker with sufficient context"
            )
        selected = None
        if conversation and conversation.endpoint_id == endpoint.id:
            selected = next(
                (
                    item
                    for item in workers
                    if item.worker_id == conversation.deployment_id
                    and item.state == "available"
                ),
                None,
            )
        if selected is None:
            available = [
                item for item in workers if item.state == "available"
            ]
            if not available:
                raise NoEligibleModelError(
                    "the local model pool has no available physical worker"
                )
            available.sort(
                key=lambda item: (
                    item.short_request_rank,
                    _deployment_hash(routing_key, item.worker_id),
                    item.worker_id,
                )
            )
            selected = available[0]
            if conversation and conversation.endpoint_id == endpoint.id:
                decision.affinity = "physical-failover"
                decision.reason = "physical_worker_unavailable"
        candidates = [selected] if decision.affinity == "hit" else available
        decision.deployment_candidates = tuple(
            (
                item.worker_id,
                item.api_base,
            )
            for item in candidates
        )
        decision.deployment_details = {
            item.worker_id: item.to_dict()
            for item in candidates
        }
        decision.deployment_id = selected.worker_id
        decision.deployment_profile_id = selected.profile_id
        decision.deployment_modalities = selected.modalities
        decision.deployment_vision_status = selected.vision_status
        decision.deployment_safe_context_tokens = (
            selected.safe_context_tokens
        )
        decision.deployment_max_images = selected.max_images
        decision.upstream_api_base = decision.deployment_candidates[0][1]

    async def _eligible_physical_deployments(
        self,
        endpoint: Endpoint,
        status: EndpointStatus,
        *,
        required_context: int,
        modalities: set[str],
        image_count: int,
        excluded_deployment_ids: set[str],
        require_available: bool,
    ) -> list[PhysicalDeployment]:
        result: list[PhysicalDeployment] = []
        for value in status.detail.get("workers", []):
            try:
                item = _physical_deployment_from_status(
                    endpoint,
                    value,
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not item.worker_id
                or item.worker_id in excluded_deployment_ids
                or not item.schedulable
                or item.safe_context_tokens < required_context
                or not item.supports_modalities(modalities)
                or not item.supports_image_count(image_count)
                or (
                    require_available
                    and item.state != "available"
                )
                or await self.health.in_cooldown(item.worker_id)
                or (
                    "image" in modalities
                    and await self.health.in_capability_cooldown(
                        item.worker_id,
                        "image",
                    )
                )
            ):
                continue
            result.append(item)
        return result

    def _score(
        self,
        endpoint: Endpoint,
        status: EndpointStatus,
        task: str,
        required_context: int,
    ) -> float:
        weights = self.settings.section("routing").get("weights", {})
        quality = float(endpoint.quality.get(task, 0)) / 100.0
        context_headroom = max(
            0.0,
            min(1.0, (endpoint.safe_context_tokens - required_context) / max(1, endpoint.safe_context_tokens)),
        )
        locality = 1.0 if endpoint.node == "ai" else 0.0
        cost = self._cost_score(endpoint)
        return (
            float(weights.get("quality", 0.50)) * quality
            + float(weights.get("load", 0.20)) * status.load_headroom
            + float(weights.get("latency", 0.10)) * status.latency_score
            + float(weights.get("context", 0.10)) * context_headroom
            + float(weights.get("cost", 0.05)) * cost
            + float(weights.get("locality", 0.05)) * locality
        )

    def _cost_score(self, endpoint: Endpoint) -> float:
        if not endpoint.cloud:
            return 1.0
        if endpoint.metadata.get("billing_mode") == "subscription":
            return 0.5
        input_cost = float(
            endpoint.metadata.get("input_cost_per_million_usd", 0)
        )
        output_cost = float(
            endpoint.metadata.get("output_cost_per_million_usd", 0)
        )
        return 1.0 / (1.0 + max(0.0, input_cost + output_cost))


def _deployment_hash(routing_key: str, deployment_id: str) -> str:
    return hashlib.sha256(
        f"{routing_key}\0{deployment_id}".encode("utf-8")
    ).hexdigest()


def _physical_deployment_from_status(
    endpoint: Endpoint,
    value: dict[str, Any],
) -> PhysicalDeployment:
    if "profile_id" in value and "modalities" in value:
        return PhysicalDeployment.from_dict(value)
    safe_context = int(value.get("safe_context_tokens", 0))
    tier = str(value.get("tier", ""))
    profile = next(
        (
            item
            for item in endpoint.deployment_profiles
            if (
                item.matches(tier)
                or (
                    not tier
                    and item.safe_context_tokens == safe_context
                )
            )
        ),
        None,
    )
    port_value = value.get("port")
    port = int(port_value) if port_value is not None else None
    api_base = str(value.get("api_base") or "").rstrip("/")
    if not api_base and port is not None:
        api_base = f"http://127.0.0.1:{port}/v1"
    return PhysicalDeployment(
        worker_id=str(value.get("worker_id", "")),
        api_base=api_base,
        profile_id=profile.id if profile else "legacy",
        tier=tier,
        priority=int(value.get("priority", 999)),
        gpu_ids=tuple(str(item) for item in value.get("gpu_ids", [])),
        gpu_uuids=tuple(
            str(item) for item in value.get("gpu_uuids", [])
        ),
        names=tuple(str(item) for item in value.get("names", [])),
        port=port,
        context_size=int(
            value.get("context_size")
            or (profile.context_size if profile else safe_context)
        ),
        safe_context_tokens=safe_context,
        cache_type_k=str(
            value.get("cache_type_k")
            or (profile.cache_type_k if profile else "")
        ),
        cache_type_v=str(
            value.get("cache_type_v")
            or (profile.cache_type_v if profile else "")
        ),
        modalities=(
            profile.modalities if profile else endpoint.modalities
        ),
        vision_status=(
            profile.vision_status if profile else "unverified"
        ),
        max_images=(
            profile.max_images if profile else None
        ),
        runtime_fingerprint=str(
            value.get("runtime_fingerprint", "")
        ),
        ready=bool(value.get("ready")),
        state=str(value.get("state", "unknown")),
        config_drift=tuple(value.get("config_drift", ())),
        short_request_rank=(
            profile.short_request_rank
            if profile
            else int(value.get("priority", 999))
        ),
        error_code=(
            str(value["error_code"])
            if value.get("error_code")
            else None
        ),
        cooldown_until=(
            float(value["cooldown_until"])
            if value.get("cooldown_until") is not None
            else None
        ),
    )


def updated_conversation_state(
    existing: ConversationState | None,
    *,
    conversation_id: str,
    decision: RouteDecision,
    cache_generation: str,
    encrypted_capsule: str | None = None,
    boundary_hash: str | None = None,
) -> ConversationState:
    if existing is None:
        return ConversationState(
            conversation_id=conversation_id,
            public_model=decision.endpoint.public_model,
            endpoint_id=decision.endpoint.id,
            tier_rank=decision.endpoint.tier_rank,
            task=decision.task,
            last_seen=time.time(),
            cache_generation=cache_generation,
            deployment_id=decision.deployment_id or decision.endpoint.id,
            upstream_api_base=decision.upstream_api_base,
            encrypted_capsule=encrypted_capsule,
            boundary_hash=boundary_hash,
            migration_count=1 if decision.migration else 0,
        )
    return replace(
        existing,
        public_model=decision.endpoint.public_model,
        endpoint_id=decision.endpoint.id,
        tier_rank=max(existing.tier_rank, decision.endpoint.tier_rank),
        task=decision.task,
        last_seen=time.time(),
        cache_generation=cache_generation,
        deployment_id=decision.deployment_id or decision.endpoint.id,
        upstream_api_base=decision.upstream_api_base,
        encrypted_capsule=encrypted_capsule or existing.encrypted_capsule,
        boundary_hash=boundary_hash or existing.boundary_hash,
        migration_count=existing.migration_count + (1 if decision.migration else 0),
    )
