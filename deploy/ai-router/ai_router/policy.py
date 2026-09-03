from __future__ import annotations

import hashlib
import math
import time
from typing import Any
from uuid import uuid4

from .config import Registry, Settings
from .errors import (
    NoCompatibleModelError,
    NoEligibleModelError,
    RouterError,
)
from .health import HealthMonitor
from .route_trace import DecisionTrace
from .store import StateStore
from .types import (
    ConversationState,
    Endpoint,
    EndpointStatus,
    Evaluation,
    LineageContext,
    PhysicalDeployment,
    RequestCapabilities,
    RouteDecision,
)


INCOMPATIBLE_REJECTION_REASONS = frozenset(
    {
        "capability",
        "context",
        "deepseek_multimodal_unsupported",
        "modality",
        "task",
        "tier",
    }
)


class ConversationRepository:
    def __init__(self, store: StateStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def get(self, branch_id: str | None) -> ConversationState | None:
        if not branch_id:
            return None
        value = await self.store.get_json(
            f"router:conversation-branch:{branch_id}"
        )
        if not value:
            value = await self.store.get_json(
                f"router:conversation:{branch_id}"
            )
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
            f"router:conversation-branch:{state.branch_id or state.conversation_id}",
            state.to_dict(),
            ttl_seconds=ttl,
        )

    async def map_response(self, response_id: str, branch_id: str) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        await self.store.set_json(
            f"router:response-conversation:{response_id}",
            {"branch_id": branch_id},
            ttl_seconds=ttl,
        )

    async def map_lineage(
        self,
        client_id: str,
        lineage_id: str,
        branch_id: str,
    ) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        await self.store.set_json(
            f"router:lineage-conversation:{client_id}:{lineage_id}",
            {"branch_id": branch_id},
            ttl_seconds=ttl,
        )

    async def branch_for_lineage(
        self,
        client_id: str,
        lineage_id: str | None,
    ) -> str | None:
        if not lineage_id:
            return None
        value = await self.store.get_json(
            f"router:lineage-conversation:{client_id}:{lineage_id}"
        )
        if not value:
            return None
        branch_id = value.get("branch_id") or value.get("conversation_id")
        return str(branch_id) if branch_id else None

    async def map_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
        branch_id: str,
    ) -> None:
        ttl = int(self.settings.section("affinity").get("ttl_seconds", 86400))
        for identity in identities:
            await self.store.set_json(
                f"router:history-conversation:{client_id}:{identity}",
                {"branch_id": branch_id},
                ttl_seconds=ttl,
            )

    async def branch_for_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
    ) -> str | None:
        for identity in identities:
            value = await self.store.get_json(
                f"router:history-conversation:{client_id}:{identity}"
            )
            if value and (value.get("branch_id") or value.get("conversation_id")):
                return str(value.get("branch_id") or value["conversation_id"])
        return None

    async def conversation_for_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
    ) -> str | None:
        return await self.branch_for_history(client_id, identities)

    async def branch_for_response(self, response_id: str | None) -> str | None:
        if not response_id:
            return None
        value = await self.store.get_json(f"router:response-conversation:{response_id}")
        if not value:
            return None
        branch_id = value.get("branch_id") or value.get("conversation_id")
        return str(branch_id) if branch_id else None

    async def conversation_for_response(
        self,
        response_id: str | None,
    ) -> str | None:
        return await self.branch_for_response(response_id)

    async def lineage_context(
        self,
        *,
        client_id: str,
        identities: tuple[str, ...],
        explicit_lineage_id: str | None,
        previous_response_id: str | None,
        force_new: bool,
    ) -> LineageContext:
        branch_id = f"branch-{uuid4().hex}"
        if force_new:
            return LineageContext(
                lineage_id=explicit_lineage_id or f"lineage-{uuid4().hex}",
                branch_id=branch_id,
                parent_branch_id=None,
                mode="stateful" if explicit_lineage_id else "inferred",
                relation="compaction_reset",
            )
        parent_id = await self.branch_for_response(previous_response_id)
        if not parent_id:
            parent_id = await self.branch_for_lineage(
                client_id,
                explicit_lineage_id,
            )
        if not parent_id:
            parent_id = await self.branch_for_history(client_id, identities)
        parent = await self.get(parent_id)
        if parent:
            return LineageContext(
                lineage_id=explicit_lineage_id or parent.conversation_id,
                branch_id=branch_id,
                parent_branch_id=parent.branch_id or parent_id,
                mode="stateful" if explicit_lineage_id else "inferred",
                relation="continuation",
                parent=parent,
            )
        return LineageContext(
            lineage_id=explicit_lineage_id or f"lineage-{uuid4().hex}",
            branch_id=branch_id,
            parent_branch_id=None,
            mode="stateful" if explicit_lineage_id else "inferred",
            relation="new",
        )


class RoutingPolicy:
    def __init__(
        self,
        registry: Registry,
        settings: Settings,
        health: HealthMonitor,
        store: StateStore | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.health = health
        self.store = store

    async def choose(
        self,
        *,
        requested_model: str,
        evaluation: Evaluation,
        prompt_tokens: int,
        output_reserve_tokens: int,
        requested_context_tokens: int | None = None,
        modalities: set[str],
        image_count: int = 0,
        has_tools: bool,
        required_capabilities: RequestCapabilities | None = None,
        conversation: ConversationState | None,
        excluded_endpoint_ids: set[str] | None = None,
        excluded_deployment_ids: set[str] | None = None,
        routing_key: str = "",
        trace: DecisionTrace | None = None,
        trace_attempt: int = 1,
    ) -> RouteDecision:
        strategy = str(
            self.settings.section("routing").get(
                "strategy",
                "legacy_v1",
            )
        )
        excluded = excluded_endpoint_ids or set()
        excluded_deployments = excluded_deployment_ids or set()
        required = required_capabilities or RequestCapabilities(
            protocol="chat",
            tools=has_tools,
        )
        context_required = (
            int(requested_context_tokens)
            if requested_context_tokens is not None
            else prompt_tokens + output_reserve_tokens
        )
        endpoints = (
            list(self.registry.responders())
            if requested_model == "auto"
            else list(self.registry.by_public_model(requested_model))
        )
        if not endpoints:
            if trace:
                trace.record_candidate_round(
                    trace_attempt,
                    [],
                    mode=(
                        "auto"
                        if requested_model == "auto"
                        else "explicit"
                    ),
                )
            raise RouterError(
                f"unknown model: {requested_model}",
                status_code=404,
                code="model_not_found",
            )
        if (
            requested_model != "auto"
            and not any(item.enabled for item in endpoints)
        ):
            raise RouterError(
                f"endpoint is disabled: {requested_model}",
                status_code=503,
                code="endpoint_disabled",
            )
        statuses = await self.health.statuses(endpoints)
        candidates: list[Endpoint] = []
        rejections: list[str] = []
        rejection_reasons: list[str] = []
        rejection_by_endpoint: dict[str, str] = {}
        trace_candidates: list[dict[str, Any]] = []
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
                rejection_reasons.append(reason)
                rejection_by_endpoint[endpoint.id] = reason
            else:
                candidates.append(endpoint)
            if trace:
                trace_candidates.append(
                    self._trace_candidate(
                        endpoint,
                        statuses[endpoint.id],
                        evaluation=evaluation,
                        prompt_tokens=prompt_tokens,
                        output_reserve_tokens=output_reserve_tokens,
                        required_capabilities=required,
                        rejection_reason=reason,
                    )
                )
        if trace:
            trace.record_candidate_round(
                trace_attempt,
                trace_candidates,
                mode=(
                    "auto"
                    if requested_model == "auto"
                    else "explicit"
                ),
            )
        if requested_model == "auto" and conversation:
            previous_endpoint = self.registry.by_id(
                conversation.endpoint_id
            )
            bound_rejection = rejection_by_endpoint.get(
                conversation.endpoint_id
            )
            if (
                previous_endpoint
                and previous_endpoint.cloud
                and bound_rejection
                and bound_rejection not in INCOMPATIBLE_REJECTION_REASONS
            ):
                raise NoEligibleModelError(
                    "the bound cloud conversation model is temporarily "
                    "unavailable: "
                    f"{conversation.endpoint_id}:{bound_rejection}"
                )
        if not candidates:
            message = "no eligible model is available: " + ", ".join(
                rejections
            )
            inactive_rejections = {
                "auto_disabled",
                "cloud_auto_disabled",
                "cloud_disabled",
                "disabled",
            }
            if (
                requested_model == "auto"
                and strategy == "intelligent_v2"
                and rejection_reasons
                and any(
                    reason in INCOMPATIBLE_REJECTION_REASONS
                    for reason in rejection_reasons
                )
                and all(
                    reason in INCOMPATIBLE_REJECTION_REASONS
                    or reason in inactive_rejections
                    for reason in rejection_reasons
                )
            ):
                raise NoCompatibleModelError(message)
            raise NoEligibleModelError(message)

        if conversation:
            pinned = next(
                (item for item in candidates if item.id == conversation.endpoint_id),
                None,
            )
            if pinned:
                cache_reset = bool(
                    conversation.cache_generation
                    and pinned.backend_type != "ai_pool"
                    and statuses[pinned.id].cache_generation
                    and conversation.cache_generation
                    != statuses[pinned.id].cache_generation
                )
                if trace:
                    trace.record(
                        trace_attempt,
                        "conversation_affinity",
                        "selected",
                        branch="hit",
                        reason=(
                            "cache_generation_changed"
                            if cache_reset
                            else "conversation_affinity"
                        ),
                        evidence={
                            "conversation_id": conversation.conversation_id,
                            "endpoint_id": pinned.id,
                            "deployment_id": conversation.deployment_id,
                        },
                    )
                decision = RouteDecision(
                    endpoint=pinned,
                    requested_model=requested_model,
                    task=evaluation.task,
                    prompt_tokens=prompt_tokens,
                    output_reserve_tokens=output_reserve_tokens,
                    reason=(
                        "cache_generation_changed"
                        if cache_reset
                        else "conversation_affinity"
                    ),
                    affinity="cache-reset" if cache_reset else "hit",
                    score=1.0,
                    protocol=required.protocol,
                    native_or_adapter=pinned.capabilities.protocol_mode(
                        required.protocol
                    ),
                    required_capabilities=required.labels(),
                    candidate_rejections=tuple(rejections),
                    strategy_version=strategy,
                    route_profile=evaluation.route_profile,
                    complexity=evaluation.complexity,
                    context_required=(
                        context_required
                    ),
                    trace=trace,
                )
                await self._bind_traced_deployment(
                    decision,
                    statuses[pinned.id],
                    conversation,
                    prompt_tokens + output_reserve_tokens,
                    excluded_deployments,
                    modalities,
                    image_count,
                    routing_key,
                    trace,
                    trace_attempt,
                )
                return decision
        if trace:
            trace.record(
                trace_attempt,
                "conversation_affinity",
                "evaluated",
                branch="miss",
                reason=(
                    "conversation_endpoint_ineligible"
                    if conversation
                    else "new_conversation"
                ),
                evidence={
                    "conversation_id": (
                        conversation.conversation_id
                        if conversation
                        else None
                    ),
                    "previous_endpoint_id": (
                        conversation.endpoint_id
                        if conversation
                        else None
                    ),
                },
            )

        remote_fallback_position = None
        selection_reason = ""
        if requested_model == "auto" and conversation:
            before_priority = [item.id for item in candidates]
            (
                candidates,
                selection_reason,
                remote_fallback_position,
            ) = self._conversation_fallback_candidates(
                candidates,
                conversation,
                evaluation,
            )
            if trace:
                trace.record(
                    trace_attempt,
                    "provider_priority",
                    "passed",
                    branch="conversation_fallback",
                    reason=selection_reason,
                    evidence={
                        "before_endpoint_ids": before_priority,
                        "after_endpoint_ids": [
                            item.id for item in candidates
                        ],
                        "previous_endpoint_id": conversation.endpoint_id,
                        "previous_tier_rank": conversation.tier_rank,
                        "remote_fallback_position": (
                            remote_fallback_position
                        ),
                    },
                    path=False,
                )
        elif requested_model == "auto" and strategy == "intelligent_v2":
            before_priority = [item.id for item in candidates]
            (
                candidates,
                selection_reason,
                remote_fallback_position,
            ) = self._apply_intelligent_v2_stage(
                candidates,
                evaluation,
                context_required=context_required,
                trace=trace,
                trace_attempt=trace_attempt,
            )
            if trace:
                trace.record(
                    trace_attempt,
                    "provider_priority",
                    "passed",
                    branch="auto",
                    reason=selection_reason,
                    evidence={
                        "strategy": strategy,
                        "before_endpoint_ids": before_priority,
                        "after_endpoint_ids": [
                            item.id for item in candidates
                        ],
                        "route_profile": evaluation.route_profile,
                        "complexity": evaluation.complexity,
                        "remote_fallback_position": (
                            remote_fallback_position
                        ),
                    },
                    path=False,
                )
        elif requested_model == "auto":
            before_priority = [item.id for item in candidates]
            candidates = self._apply_provider_priority(
                candidates,
                evaluation,
            )
            if trace:
                trace.record(
                    trace_attempt,
                    "provider_priority",
                    "passed",
                    branch="auto",
                    reason=self._provider_priority_reason(evaluation),
                    evidence={
                        "before_endpoint_ids": before_priority,
                        "after_endpoint_ids": [
                            item.id for item in candidates
                        ],
                        "preferred_tier": evaluation.preferred_tier,
                    },
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
            if trace:
                trace.record(
                    trace_attempt,
                    "explicit_selection",
                    "selected",
                    branch="explicit",
                    reason="explicit_model",
                    evidence={
                        "endpoint_id": endpoint.id,
                        "selected_model": endpoint.public_model,
                        "load_headroom": statuses[
                            endpoint.id
                        ].load_headroom,
                        "latency_score": statuses[
                            endpoint.id
                        ].latency_score,
                    },
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
                    strategy_version=strategy,
                    route_profile=evaluation.route_profile,
                    complexity=evaluation.complexity,
                    context_required=(
                        context_required
                    ),
                    trace=trace,
                )
            await self._bind_traced_deployment(
                decision,
                statuses[endpoint.id],
                conversation,
                prompt_tokens + output_reserve_tokens,
                excluded_deployments,
                modalities,
                image_count,
                routing_key,
                trace,
                trace_attempt,
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
        if trace:
            trace.record(
                trace_attempt,
                "score_candidates",
                "passed",
                branch="auto",
                reason="candidate_scoring",
                evidence={
                    "scores": [
                        {
                            "endpoint_id": candidate.id,
                            "score": round(candidate_score, 6),
                            "quality_score": float(
                                candidate.quality.get(
                                    evaluation.task,
                                    0,
                                )
                            ),
                            "load_headroom": statuses[
                                candidate.id
                            ].load_headroom,
                            "latency_score": statuses[
                                candidate.id
                            ].latency_score,
                        }
                        for candidate_score, candidate in sorted(
                            scored,
                            key=lambda item: item[0],
                            reverse=True,
                        )
                    ]
                },
            )
        score, endpoint = max(scored, key=lambda item: (item[0], item[1].node == "ai", item[1].id))
        migration = bool(conversation and endpoint.id != conversation.endpoint_id)
        decision = RouteDecision(
            endpoint=endpoint,
            requested_model=requested_model,
            task=evaluation.task,
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=output_reserve_tokens,
            reason=(
                selection_reason
                or (
                    "monotonic_upgrade"
                    if migration
                    else self._priority_reason(endpoint, evaluation)
                )
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
            strategy_version=strategy,
            route_profile=evaluation.route_profile,
            complexity=evaluation.complexity,
            context_required=context_required,
            remote_fallback_position=remote_fallback_position,
            trace=trace,
        )
        await self._bind_traced_deployment(
            decision,
            statuses[endpoint.id],
            conversation,
            prompt_tokens + output_reserve_tokens,
            excluded_deployments,
            modalities,
            image_count,
            routing_key,
            trace,
            trace_attempt,
        )
        return decision

    def _trace_candidate(
        self,
        endpoint: Endpoint,
        status: EndpointStatus,
        *,
        evaluation: Evaluation,
        prompt_tokens: int,
        output_reserve_tokens: int,
        required_capabilities: RequestCapabilities,
        rejection_reason: str | None,
    ) -> dict[str, Any]:
        workers = status.detail.get("workers", [])
        safe_context = min(
            endpoint.safe_context_tokens,
            status.eligible_context_tokens
            or endpoint.safe_context_tokens,
        )
        stale_after = float(
            self.settings.section("health").get(
                "stale_after_seconds",
                15,
            )
        )
        return {
            "endpoint_id": endpoint.id,
            "model": endpoint.public_model,
            "node": endpoint.node,
            "cloud": endpoint.cloud,
            "enabled": endpoint.enabled,
            "auto_candidate": endpoint.auto_candidate,
            "tier": endpoint.tier,
            "modalities": list(endpoint.modalities),
            "tasks": list(endpoint.tasks),
            "required_capabilities": list(
                required_capabilities.labels()
            ),
            "capability_validation": (
                endpoint.capabilities.validation_status
            ),
            "healthy": status.healthy,
            "fresh": status.is_fresh(time.time(), stale_after),
            "load_headroom": status.load_headroom,
            "latency_score": status.latency_score,
            "required_context_tokens": (
                prompt_tokens + output_reserve_tokens
            ),
            "safe_context_tokens": safe_context,
            "quality_score": float(
                endpoint.quality.get(evaluation.task, 0)
            ),
            "quality_status": endpoint.metadata.get(
                "quality_status",
                "unverified",
            ),
            "route_profile": evaluation.route_profile,
            "complexity": evaluation.complexity,
            "physical_deployments": {
                "total": len(workers),
                "ready": sum(
                    bool(item.get("ready"))
                    for item in workers
                ),
                "available": sum(
                    bool(
                        item.get("ready")
                        and item.get("schedulable", True)
                        and item.get("state") == "available"
                    )
                    for item in workers
                ),
            },
            "rejection_reason": rejection_reason,
        }

    def _provider_priority_reason(
        self,
        evaluation: Evaluation,
    ) -> str:
        if evaluation.preferred_tier:
            return "preferred_tier"
        return str(
            self.settings.section("routing").get(
                "provider_priority",
                "local_first",
            )
        )

    async def _bind_traced_deployment(
        self,
        decision: RouteDecision,
        status: EndpointStatus,
        conversation: ConversationState | None,
        required_context: int,
        excluded_deployment_ids: set[str],
        modalities: set[str],
        image_count: int,
        routing_key: str,
        trace: DecisionTrace | None,
        trace_attempt: int,
    ) -> None:
        if trace:
            trace.record(
                trace_attempt,
                "deployment_binding",
                "running",
                reason="deployment_binding",
                evidence={"endpoint_id": decision.endpoint.id},
            )
        try:
            await self._bind_physical_deployment(
                decision,
                status,
                conversation,
                required_context,
                excluded_deployment_ids,
                modalities,
                image_count,
                routing_key,
            )
        except Exception as exc:
            if trace:
                trace.record(
                    trace_attempt,
                    "deployment_binding",
                    "error",
                    reason=type(exc).__name__,
                    evidence={
                        "endpoint_id": decision.endpoint.id,
                        "message": str(exc)[:500],
                    },
                )
            raise
        if trace:
            trace.record(
                trace_attempt,
                "deployment_binding",
                "selected",
                reason="deployment_selected",
                evidence={
                    "endpoint_id": decision.endpoint.id,
                    "deployment_id": (
                        decision.deployment_id
                        or decision.endpoint.id
                    ),
                    "deployment_profile_id": (
                        decision.deployment_profile_id
                    ),
                    "safe_context_tokens": (
                        decision.deployment_safe_context_tokens
                        or decision.endpoint.safe_context_tokens
                    ),
                },
            )
            trace.set_selection(
                attempt=trace_attempt,
                selected_model=decision.endpoint.public_model,
                endpoint_id=decision.endpoint.id,
                deployment_id=decision.deployment_id,
                task=decision.task,
                reason=decision.reason,
                affinity=decision.affinity,
                strategy_version=decision.strategy_version,
                route_profile=decision.route_profile,
                complexity=decision.complexity,
                context_required=decision.context_required,
                history_mode=decision.history_mode,
                remote_fallback_position=(
                    decision.remote_fallback_position
                ),
            )

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
                return (
                    self._physical_incompatibility_reason(
                        endpoint,
                        status,
                        required_context=(
                            prompt_tokens + output_reserve_tokens
                        ),
                        modalities=modalities,
                        image_count=image_count,
                    )
                    or "physical_deployment"
                )
        if (
            "image" in modalities
            and str(endpoint.metadata.get("provider", "")) == "deepseek"
        ):
            return "deepseek_multimodal_unsupported"
        if (
            endpoint.backend_type != "ai_pool"
            and not modalities.issubset(set(endpoint.modalities))
        ):
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
            and str(
                self.settings.section("routing").get(
                    "strategy",
                    "legacy_v1",
                )
            )
            == "legacy_v1"
        ):
            return "tier_downgrade"
        if evaluation.required_tier:
            required_rank = self.registry.tier_ranks.get(evaluation.required_tier)
            if required_rank is not None and endpoint.tier_rank < required_rank:
                return "tier"
        return None

    def _apply_intelligent_v2_stage(
        self,
        candidates: list[Endpoint],
        evaluation: Evaluation,
        *,
        context_required: int,
        trace: DecisionTrace | None,
        trace_attempt: int,
    ) -> tuple[list[Endpoint], str, int | None]:
        local = [item for item in candidates if not item.cloud]
        if local:
            if trace:
                trace.record(
                    trace_attempt,
                    "local_sufficiency",
                    "selected",
                    branch="sufficient",
                    reason="local_constraints_satisfied",
                    evidence={
                        "endpoint_ids": [item.id for item in local],
                        "required_context_tokens": context_required,
                    },
                )
            return local, "local_sufficient", None

        profile_key = self._remote_profile_key(evaluation)
        order = [
            str(item)
            for item in self.settings.section("routing")
            .get("remote_fallback_order", {})
            .get(profile_key, [])
        ]
        if trace:
            trace.record(
                trace_attempt,
                "local_sufficiency",
                "evaluated",
                branch="insufficient",
                reason="no_eligible_local_candidate",
                evidence={
                    "route_profile": evaluation.route_profile,
                    "complexity": evaluation.complexity,
                    "required_context_tokens": context_required,
                },
            )
        for index, configured in enumerate(order, start=1):
            matched = [
                endpoint
                for endpoint in candidates
                if endpoint.cloud
                and configured
                in {
                    endpoint.id,
                    endpoint.public_model,
                    str(endpoint.metadata.get("provider", "")),
                }
            ]
            if not matched:
                continue
            if trace:
                trace.record(
                    trace_attempt,
                    "remote_expert_dispatch",
                    "selected",
                    branch=profile_key,
                    reason="configured_remote_order",
                    evidence={
                        "profile_key": profile_key,
                        "configured_order": order,
                        "position": index,
                        "endpoint_ids": [
                            endpoint.id for endpoint in matched
                        ],
                    },
                )
            return matched, "remote_profile_fallback", index
        raise NoCompatibleModelError(
            "no configured remote fallback can satisfy profile "
            f"{profile_key}"
        )

    def _conversation_fallback_candidates(
        self,
        candidates: list[Endpoint],
        conversation: ConversationState,
        evaluation: Evaluation,
    ) -> tuple[list[Endpoint], str, int | None]:
        eligible = [
            endpoint
            for endpoint in candidates
            if endpoint.tier_rank >= conversation.tier_rank
        ]
        if not eligible:
            raise NoCompatibleModelError(
                "no conversation fallback can preserve the current tier"
            )
        previous = self.registry.by_id(conversation.endpoint_id)
        if previous and previous.cloud:
            profile_key = self._remote_profile_key(evaluation)
            order = [
                str(item)
                for item in self.settings.section("routing")
                .get("remote_fallback_order", {})
                .get(profile_key, [])
            ]
            for index, configured in enumerate(order, start=1):
                matched = [
                    endpoint
                    for endpoint in eligible
                    if endpoint.cloud
                    and configured
                    in {
                        endpoint.id,
                        endpoint.public_model,
                        str(endpoint.metadata.get("provider", "")),
                    }
                ]
                if matched:
                    return matched, "remote_profile_fallback", index
            raise NoCompatibleModelError(
                "no configured remote fallback can preserve the current tier "
                f"for profile {profile_key}"
            )
        same_tier = [
            endpoint
            for endpoint in eligible
            if endpoint.tier_rank == conversation.tier_rank
        ]
        if same_tier:
            return same_tier, "affinity_same_tier_fallback", None
        next_rank = min(endpoint.tier_rank for endpoint in eligible)
        return (
            [
                endpoint
                for endpoint in eligible
                if endpoint.tier_rank == next_rank
            ],
            "monotonic_upgrade",
            None,
        )

    def _remote_profile_key(
        self,
        evaluation: Evaluation,
    ) -> str:
        if evaluation.route_profile == "multimodal":
            return (
                "multimodal_complex_code"
                if evaluation.complexity == "complex"
                else "multimodal"
            )
        if (
            evaluation.route_profile == "code"
            and evaluation.complexity == "complex"
        ):
            return "complex_code"
        return evaluation.route_profile

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
                    and item.state in {"available", "busy", "leased"}
                ),
                None,
            )
            if (
                selected
                and conversation.cache_generation
                and selected.cache_generation
                and conversation.cache_generation
                != selected.cache_generation
            ):
                decision.affinity = "cache-reset"
                decision.reason = "cache_generation_changed"
        available: list[PhysicalDeployment] = []
        if selected is None:
            available = [
                item for item in workers if item.state == "available"
            ]
            if not available:
                raise NoEligibleModelError(
                    "the local model pool has no available physical worker"
                )
            available = await self._order_available_deployments(
                endpoint,
                available,
                routing_key=routing_key,
                protect_recent=conversation is None,
            )
            selected = available[0]
            if conversation and conversation.endpoint_id == endpoint.id:
                decision.affinity = "physical-failover"
                decision.reason = "physical_worker_unavailable"
        candidates = (
            [selected]
            if decision.affinity in {"hit", "cache-reset"}
            else available or [selected]
        )
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

    async def _order_available_deployments(
        self,
        endpoint: Endpoint,
        deployments: list[PhysicalDeployment],
        *,
        routing_key: str,
        protect_recent: bool,
    ) -> list[PhysicalDeployment]:
        def base_key(item: PhysicalDeployment) -> tuple[int, str, str]:
            return (
                item.short_request_rank,
                _deployment_hash(routing_key, item.worker_id),
                item.worker_id,
            )

        if (
            not protect_recent
            or endpoint.backend_type != "ai_pool"
            or self.store is None
        ):
            return sorted(deployments, key=base_key)
        recent = await self._recent_deployment_uses(
            tuple(item.worker_id for item in deployments)
        )
        return sorted(
            deployments,
            key=lambda item: (
                item.worker_id in recent,
                recent.get(item.worker_id, 0.0),
                *base_key(item),
            ),
        )

    async def _recent_deployment_uses(
        self,
        deployment_ids: tuple[str, ...],
    ) -> dict[str, float]:
        if self.store is None:
            return {}
        result: dict[str, float] = {}
        for deployment_id in deployment_ids:
            value = await self.store.get_json(
                f"router:deployment-recent:{deployment_id}"
            )
            if value and value.get("used_at") is not None:
                result[deployment_id] = float(value["used_at"])
        return result

    async def mark_deployment_recent(
        self,
        decision: RouteDecision,
        conversation_id: str | None,
    ) -> None:
        if (
            self.store is None
            or decision.endpoint.backend_type != "ai_pool"
            or not decision.deployment_id
        ):
            return
        ttl = max(
            1,
            math.ceil(
                float(
                    self.settings.section("routing").get(
                        "affinity_capacity_wait_seconds",
                        120,
                    )
                )
            ),
        )
        await self.store.set_json(
            f"router:deployment-recent:{decision.deployment_id}",
            {
                "used_at": time.time(),
                "conversation_id": conversation_id,
            },
            ttl_seconds=ttl,
        )

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

    def _physical_incompatibility_reason(
        self,
        endpoint: Endpoint,
        status: EndpointStatus,
        *,
        required_context: int,
        modalities: set[str],
        image_count: int,
    ) -> str | None:
        reasons: set[str] = set()
        found = False
        for value in status.detail.get("workers", []):
            try:
                item = _physical_deployment_from_status(
                    endpoint,
                    value,
                )
            except (KeyError, TypeError, ValueError):
                continue
            found = True
            item_reasons: set[str] = set()
            if item.safe_context_tokens < required_context:
                item_reasons.add("context")
            if not item.supports_modalities(modalities):
                item_reasons.add("modality")
            if not item.supports_image_count(image_count):
                item_reasons.add("capability")
            if not item_reasons:
                return None
            reasons.update(item_reasons)
        if not found:
            return None
        return next(iter(reasons)) if len(reasons) == 1 else "capability"

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
        cache_generation=str(value.get("cache_generation", "")),
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
    branch_id: str | None = None,
    parent_branch_id: str | None = None,
    lineage_relation: str = "legacy",
    encrypted_capsule: str | None = None,
    boundary_hash: str | None = None,
) -> ConversationState:
    return ConversationState(
        conversation_id=conversation_id,
        branch_id=branch_id or conversation_id,
        parent_branch_id=parent_branch_id,
        lineage_relation=lineage_relation,
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
        migration_count=(
            (existing.migration_count if existing else 0)
            + (1 if decision.migration else 0)
        ),
        route_profile=decision.route_profile,
        complexity=decision.complexity,
        provider_family=str(
            decision.endpoint.metadata.get("provider", "")
            or decision.endpoint.backend_type
        ),
        history_mode=decision.history_mode,
    )
