from __future__ import annotations

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
        has_tools: bool,
        required_capabilities: RequestCapabilities | None = None,
        conversation: ConversationState | None,
        excluded_endpoint_ids: set[str] | None = None,
        excluded_deployment_ids: set[str] | None = None,
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
        if not modalities.issubset(set(endpoint.modalities)):
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
    ) -> None:
        endpoint = decision.endpoint
        if endpoint.backend_type not in {"ai_pool", "codex_pool"}:
            decision.deployment_id = endpoint.id
            if not endpoint.cloud:
                decision.upstream_api_base = endpoint.api_base
            return
        workers = []
        for item in status.detail.get("workers", []):
            worker_id = str(item.get("worker_id", ""))
            if (
                worker_id
                and worker_id not in excluded_deployment_ids
                and item.get("ready")
                and int(item.get("safe_context_tokens", 0)) >= required_context
                and not await self.health.in_cooldown(worker_id)
            ):
                workers.append(item)
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
                    if item.get("worker_id") == conversation.deployment_id
                    and item.get("state") == "available"
                ),
                None,
            )
        if selected is None:
            available = [
                item for item in workers if item.get("state") == "available"
            ]
            if not available:
                raise NoEligibleModelError(
                    "the local model pool has no available physical worker"
                )
            available.sort(
                key=lambda item: (
                    int(item.get("priority", 999)),
                    -int(item.get("safe_context_tokens", 0)),
                    str(item.get("worker_id", "")),
                )
            )
            selected = available[0]
            if conversation and conversation.endpoint_id == endpoint.id:
                decision.affinity = "physical-failover"
                decision.reason = "physical_worker_unavailable"
        candidates = [selected] if decision.affinity == "hit" else available
        decision.deployment_candidates = tuple(
            (
                str(item["worker_id"]),
                (
                    str(item["api_base"])
                    if item.get("api_base")
                    else f"http://127.0.0.1:{int(item['port'])}/v1"
                ),
            )
            for item in candidates
        )
        decision.deployment_id = str(selected["worker_id"])
        decision.upstream_api_base = decision.deployment_candidates[0][1]

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
