from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .compaction import extract_messages, replace_messages
from .errors import (
    AllLocalCapacityBusyError,
    CapacityBusyError,
    CompactionUnavailableError,
    NoEligibleModelError,
    QueueTimeoutError,
    RouterError,
)
from .history import (
    SSEAccumulator,
    apply_stored_history,
    history_lookup_identities,
    persist_history,
)
from .policy import updated_conversation_state
from .protocol import normalize_request
from .runtime import RouterRuntime, build_runtime
from .token_counter import output_reserve_tokens, request_modalities
from .types import ConversationState, Endpoint, EndpointStatus, RouteDecision


RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def create_app(runtime: RouterRuntime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        app.state.runtime = runtime or build_runtime()
        await app.state.runtime.start()
        yield
        if owned:
            await app.state.runtime.close()

    app = FastAPI(
        title="1Panel AI Router",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(RouterError)
    async def router_error_handler(
        _request: Request,
        exc: RouterError,
    ) -> JSONResponse:
        return _error_response(exc)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        return {
            "ok": True,
            "state_store": await current.store.ping(),
            "registry_endpoints": len(current.registry.endpoints),
            "instance_id": current.instance_id,
            "boot_id": current.boot_id,
            "draining": current.draining,
        }

    @app.get("/internal/status")
    async def internal_status(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        current.auth.authenticate_admin(request.headers.get("authorization"))
        states = await current.instance_states()
        current_state = next(
            (
                item
                for item in states
                if item.get("instance_id") == current.instance_id
            ),
            {},
        )
        return {"instance": current_state, "instances": states}

    @app.post("/internal/drain")
    async def internal_drain(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        current.auth.authenticate_admin(request.headers.get("authorization"))
        await current.set_draining(True)
        states = await current.instance_states()
        current_state = next(
            (
                item
                for item in states
                if item.get("instance_id") == current.instance_id
            ),
            {},
        )
        return {"ok": True, "instance": current_state}

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        current = _runtime(request)
        current.reload_settings()
        client = current.auth.authenticate(request.headers.get("authorization"))
        values = []
        for model in ("auto", *current.registry.public_models()):
            if "*" not in client.policy.models and model not in client.policy.models:
                continue
            values.append(_model_descriptor(current, model))
        return JSONResponse({"object": "list", "data": values})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _proxy(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await _proxy(request, "responses")

    return app


def _model_descriptor(
    current: RouterRuntime,
    model: str,
) -> dict[str, Any]:
    endpoints = (
        tuple(
            endpoint
            for endpoint in current.registry.responders()
            if endpoint.enabled and endpoint.auto_candidate
        )
        if model == "auto"
        else current.registry.by_public_model(model)
    )
    modalities = sorted(
        {
            modality
            for endpoint in endpoints
            for modality in endpoint.modalities
        }
    )
    supports_images = "image" in modalities
    return {
        "id": model,
        "object": "model",
        "created": 0,
        "owned_by": "1panel-ai-router",
        "modalities": modalities,
        "input_modalities": modalities,
        "output_modalities": ["text"],
        "supportsImages": supports_images,
        "supportsToolCall": any(endpoint.tools for endpoint in endpoints),
        "capabilities": {
            "vision": supports_images,
            "chat": any(endpoint.capabilities.chat for endpoint in endpoints),
            "responses": any(
                endpoint.capabilities.responses != "none"
                for endpoint in endpoints
            ),
        },
    }


async def _proxy(request: Request, api_kind: str) -> Response:
    current = _runtime(request)
    current.reload_settings()
    request_id = request.headers.get("x-request-id") or uuid4().hex
    max_request_bytes = int(
        current.settings.section("limits").get(
            "max_request_bytes",
            32 * 1024 * 1024,
        )
    )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_request_bytes:
                raise _payload_too_large(max_request_bytes)
        except ValueError:
            pass
    try:
        raw_body = await request.body()
        if len(raw_body) > max_request_bytes:
            raise _payload_too_large(max_request_bytes)
        body = json.loads(raw_body)
    except RouterError:
        raise
    except Exception as exc:
        raise RouterError(
            "request body must be valid JSON",
            status_code=400,
            code="invalid_json",
        ) from exc
    if not isinstance(body, dict):
        raise RouterError(
            "request body must be a JSON object",
            status_code=400,
            code="invalid_request",
        )

    requested_model = str(body.get("model", "")).strip()
    if not requested_model:
        raise RouterError(
            "model is required",
            status_code=400,
            code="model_required",
    )
    authenticated = current.auth.authenticate(request.headers.get("authorization"))
    current.auth.ensure_model_access(authenticated, requested_model)
    if current.draining:
        raise RouterError(
            "router instance is draining",
            status_code=503,
            code="router_draining",
            details={"instance_id": current.instance_id},
        )
    normalized = normalize_request(
        body,
        api_kind,
        validate_history=not (
            api_kind == "responses"
            and bool(str(body.get("previous_response_id", "")).strip())
        ),
    )
    body = normalized.body
    tool_history_repairs = normalized.repairs
    conversation_id, conversation_mode = await _conversation_id(
        current,
        request,
        body,
        api_kind,
        authenticated.policy.id,
    )
    lease = await current.scheduler.begin_request(conversation_id)
    parallel_acquired = False
    stream_owned = False
    request_tracked = False

    try:
        await current.track_request_started(
            lease.owner_token,
            request_id,
            conversation_id,
        )
        request_tracked = True
        conversation = await current.conversations.get(conversation_id)
        effective_body = apply_stored_history(
            current.compactor,
            body,
            api_kind=api_kind,
            conversation=conversation,
        )
        normalized_effective = normalize_request(
            effective_body,
            api_kind,
        )
        effective_body = normalized_effective.body
        tool_history_repairs += normalized_effective.repairs
        required_capabilities = normalized_effective.required
        prompt_tokens = current.token_counter.count_request(
            effective_body,
            api_kind,
        )
        reserve_tokens = output_reserve_tokens(
            effective_body,
            api_kind,
            int(current.settings.section("routing").get("default_output_reserve_tokens", 4096)),
        )
        parallel_acquired = await current.limiter.acquire_parallel(
            authenticated.policy.id,
            lease.owner_token,
            authenticated.policy.max_parallel_requests,
        )
        if not parallel_acquired:
            raise RouterError(
                "client parallel request limit exceeded",
                status_code=429,
                code="parallel_limit_exceeded",
            )
        allowed, limit_code = await current.limiter.check_rate_limits(
            authenticated.policy.id,
            prompt_tokens=prompt_tokens,
            rpm_limit=authenticated.policy.rpm_limit,
            tpm_limit=authenticated.policy.tpm_limit,
        )
        if not allowed:
            raise RouterError(
                limit_code or "rate limit exceeded",
                status_code=429,
                code=limit_code or "rate_limit_exceeded",
            )

        header_values = {key.lower(): value for key, value in request.headers.items()}
        async def acquire_evaluator() -> None:
            await _acquire_internal_model(
                current,
                lease=lease,
                request_id=f"{request_id}:evaluator",
                model_id=str(
                    current.settings.section("evaluator").get("model_id", "")
                ),
                wait=False,
            )

        evaluation = await current.evaluator.evaluate(
            effective_body,
            headers=header_values,
            api_kind=api_kind,
            prompt_tokens=prompt_tokens,
            current_task=conversation.task if conversation else None,
            is_new_conversation=(
                conversation is None and requested_model == "auto"
            ),
            before_model_call=acquire_evaluator,
            after_model_call=lease.release_deployment,
        )
        modalities = request_modalities(effective_body, api_kind)
        has_tools = required_capabilities.tools
        excluded: set[str] = set()
        excluded_deployments: set[str] = set()
        max_attempts = int(current.settings.section("failover").get("max_attempts", 2))
        allow_retry = not bool(effective_body.get("stream")) and not has_tools
        attempts = max_attempts
        last_error: RouterError | None = None
        total_capacity_attempts = 0
        total_queue_wait_ms = 0.0

        for attempt in range(1, max(1, attempts) + 1):
            budget_reservation = None
            decision = None
            cache_snapshot = None
            try:
                (
                    decision,
                    routed_body,
                    capsule,
                    budget_reservation,
                    total_capacity_attempts,
                    total_queue_wait_ms,
                ) = await _acquire_route_capacity(
                    current,
                    request_id=request_id,
                    requested_model=requested_model,
                    evaluation=evaluation,
                    prompt_tokens=prompt_tokens,
                    output_reserve_tokens=reserve_tokens,
                    modalities=modalities,
                    has_tools=has_tools,
                    required_capabilities=required_capabilities,
                    conversation=conversation,
                    body=effective_body,
                    api_kind=api_kind,
                    lease=lease,
                    excluded_endpoints=excluded,
                    excluded_deployments=excluded_deployments,
                    capacity_attempts=total_capacity_attempts,
                    queue_wait_ms=total_queue_wait_ms,
                )
                decision.attempts = attempt
                decision.tool_history_repairs = tool_history_repairs
                _audit_started(
                    current,
                    request_id=request_id,
                    client_id=authenticated.policy.id,
                    conversation_id=conversation_id,
                    decision=decision,
                )
                await current.track_request_routed(
                    lease.owner_token,
                    requested_model=decision.requested_model,
                    selected_model=decision.endpoint.public_model,
                    endpoint_id=decision.endpoint.id,
                    deployment_id=(
                        decision.deployment_id or decision.endpoint.id
                    ),
                    node=decision.endpoint.node,
                    task=decision.task,
                    reason=decision.reason,
                    affinity=decision.affinity,
                    prompt_tokens=decision.prompt_tokens,
                    output_reserve_tokens=decision.output_reserve_tokens,
                )
                cache_snapshot = await _prefix_cache_snapshot(
                    current,
                    decision,
                )
                upstream = await _send_upstream(
                    current,
                    request,
                    routed_body,
                    api_kind=api_kind,
                    decision=decision,
                )
                subscription_fallback = bool(
                    requested_model == "auto"
                    and decision.endpoint.metadata.get("billing_mode")
                    == "subscription"
                    and upstream.status_code
                    in {401, 403, 429, 502, 503, 504}
                )
                if (
                    attempt < attempts
                    and (
                        (
                            upstream.status_code
                            in RETRYABLE_STATUS_CODES
                            and allow_retry
                        )
                        or subscription_fallback
                    )
                ):
                    await upstream.aclose()
                    await current.budget.release(budget_reservation)
                    budget_reservation = None
                    await _exclude_failed_decision(
                        current,
                        decision,
                        excluded,
                        excluded_deployments,
                    )
                    await lease.release_deployment()
                    continue

                headers = _response_headers(
                    upstream,
                    decision,
                    request_id,
                    conversation_id=conversation_id,
                    conversation_mode=conversation_mode,
                )
                if upstream.status_code >= 400:
                    payload = await upstream.aread()
                    await upstream.aclose()
                    await current.budget.release(budget_reservation)
                    budget_reservation = None
                    await _audit(
                        current,
                        request_id=request_id,
                        client_id=authenticated.policy.id,
                        conversation_id=conversation_id,
                        decision=decision,
                        status_code=upstream.status_code,
                        started_at=request.state.started_at,
                        cache_snapshot=cache_snapshot,
                    )
                    return Response(
                        content=payload,
                        status_code=upstream.status_code,
                        headers=headers,
                        media_type=upstream.headers.get("content-type"),
                    )

                await current.budget.commit(budget_reservation)
                budget_reservation = None
                state = await _save_conversation(
                    current,
                    conversation_id=conversation_id,
                    previous=conversation,
                    decision=decision,
                    capsule=capsule,
                )
                if bool(routed_body.get("stream")):
                    stream_owned = True
                    return StreamingResponse(
                        _stream_response(
                            current,
                            upstream,
                            lease=lease,
                            client_id=authenticated.policy.id,
                            request_id=request_id,
                            conversation_id=conversation_id,
                            decision=decision,
                            state=state,
                            body=routed_body,
                            api_kind=api_kind,
                            started_at=request.state.started_at,
                            cache_snapshot=cache_snapshot,
                        ),
                        status_code=upstream.status_code,
                        headers=headers,
                        media_type=upstream.headers.get("content-type"),
                    )

                payload = await upstream.aread()
                await upstream.aclose()
                await _map_response_id(current, payload, state)
                await persist_history(
                    current.compactor,
                    current.conversations,
                    state=state,
                    client_id=authenticated.policy.id,
                    body=routed_body,
                    api_kind=api_kind,
                    response_payload=payload,
                )
                await _audit(
                    current,
                    request_id=request_id,
                    client_id=authenticated.policy.id,
                    conversation_id=conversation_id,
                    decision=decision,
                    status_code=upstream.status_code,
                    started_at=request.state.started_at,
                    response_payload=payload,
                    cache_snapshot=cache_snapshot,
                )
                return Response(
                    content=payload,
                    status_code=upstream.status_code,
                    headers=headers,
                    media_type=upstream.headers.get("content-type"),
                )
            except (httpx.RequestError, NoEligibleModelError) as exc:
                await current.budget.release(budget_reservation)
                budget_reservation = None
                last_error = (
                    exc
                    if isinstance(exc, RouterError)
                    else RouterError(
                        f"internal model gateway request failed: {type(exc).__name__}",
                        status_code=503,
                        code="model_gateway_unavailable",
                    )
                )
                if decision is not None:
                    await _exclude_failed_decision(
                        current,
                        decision,
                        excluded,
                        excluded_deployments,
                    )
                await lease.release_deployment()
                subscription_fallback = bool(
                    requested_model == "auto"
                    and decision is not None
                    and decision.endpoint.metadata.get("billing_mode")
                    == "subscription"
                )
                if attempt >= attempts or not (
                    allow_retry or subscription_fallback
                ):
                    if decision is not None:
                        await _audit(
                            current,
                            request_id=request_id,
                            client_id=authenticated.policy.id,
                            conversation_id=conversation_id,
                            decision=decision,
                            status_code=last_error.status_code,
                            started_at=request.state.started_at,
                        )
                    raise last_error
            except Exception:
                await current.budget.release(budget_reservation)
                raise

        raise last_error or NoEligibleModelError()
    finally:
        if not stream_owned:
            await lease.release()
            if parallel_acquired:
                await current.limiter.release_parallel(
                    authenticated.policy.id,
                    lease.owner_token,
                )
            if request_tracked:
                await current.track_request_finished(lease.owner_token)


async def _acquire_route_capacity(
    current: RouterRuntime,
    *,
    request_id: str,
    requested_model: str,
    evaluation: Any,
    prompt_tokens: int,
    output_reserve_tokens: int,
    modalities: set[str],
    has_tools: bool,
    required_capabilities: Any,
    conversation: ConversationState | None,
    body: dict[str, Any],
    api_kind: str,
    lease: Any,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
    capacity_attempts: int,
    queue_wait_ms: float,
) -> tuple[RouteDecision, dict[str, Any], Any | None, Any | None, int, float]:
    capacity_busy_seen = False
    affinity_spilled = False
    routing = current.settings.section("routing")

    while True:
        try:
            decision = await current.policy.choose(
                requested_model=requested_model,
                evaluation=evaluation,
                prompt_tokens=prompt_tokens,
                output_reserve_tokens=output_reserve_tokens,
                modalities=modalities,
                has_tools=has_tools,
                required_capabilities=required_capabilities,
                conversation=conversation,
                excluded_endpoint_ids=excluded_endpoints,
                excluded_deployment_ids=excluded_deployments,
            )
        except NoEligibleModelError:
            if capacity_busy_seen:
                if requested_model == "auto":
                    raise AllLocalCapacityBusyError()
                raise CapacityBusyError()
            raise

        _apply_protocol_constraints(decision, api_kind)
        capacity_attempts += 1
        if not await _filter_restart_draining_deployments(
            current,
            decision,
            excluded_endpoints,
            excluded_deployments,
        ):
            capacity_busy_seen = True
            affinity_spilled = affinity_spilled or decision.affinity in {
                "hit",
                "logical-hit",
            }
            if requested_model != "auto":
                raise CapacityBusyError()
            continue
        initial_deployment = decision.deployment_id or decision.endpoint.id
        deployment_routes = dict(decision.deployment_candidates)
        deployment_ids = tuple(deployment_routes) or (initial_deployment,)

        if (
            decision.endpoint.cloud
            and capacity_busy_seen
            and routing.get("all_local_busy_policy") != "cloud_or_429"
        ):
            raise AllLocalCapacityBusyError()

        wait_seconds = _capacity_wait_seconds(
            routing,
            requested_model=requested_model,
            decision=decision,
        )
        wait_started = time.monotonic()
        try:
            if wait_seconds <= 0:
                selected_deployment = (
                    await current.scheduler.try_acquire_deployment_candidates(
                        lease,
                        deployment_ids,
                        capacity=decision.endpoint.max_concurrency,
                    )
                )
                if selected_deployment is None:
                    raise CapacityBusyError()
            else:
                try:
                    selected_deployment = (
                        await current.scheduler.acquire_deployment_candidates(
                            lease,
                            decision.endpoint.id,
                            deployment_ids,
                            request_id,
                            timeout_seconds=wait_seconds,
                            affinity_priority=decision.affinity
                            in {"hit", "logical-hit"},
                            capacity=decision.endpoint.max_concurrency,
                        )
                    )
                except QueueTimeoutError as exc:
                    raise CapacityBusyError() from exc
        except CapacityBusyError:
            queue_wait_ms += (time.monotonic() - wait_started) * 1000
            capacity_busy_seen = True
            affinity_spilled = affinity_spilled or decision.affinity in {
                "hit",
                "logical-hit",
            }
            _exclude_busy_decision(
                decision,
                excluded_endpoints,
                excluded_deployments,
            )
            await lease.release_deployment()
            if requested_model != "auto":
                raise
            continue

        queue_wait_ms += (time.monotonic() - wait_started) * 1000
        decision.deployment_id = selected_deployment
        if deployment_routes:
            decision.upstream_api_base = deployment_routes[
                selected_deployment
            ]

        budget_reservation = None
        try:
            routed_body, capsule = await _prepare_routed_body(
                current,
                body,
                api_kind=api_kind,
                decision=decision,
                request_id=request_id,
            )
        except RouterError:
            await lease.release_deployment()
            raise
        try:
            budget_reservation = await current.budget.reserve(
                decision.endpoint,
                request_id=request_id,
                prompt_tokens=decision.prompt_tokens,
                output_reserve_tokens=decision.output_reserve_tokens,
            )
        except RouterError:
            await current.budget.release(budget_reservation)
            await lease.release_deployment()
            if decision.endpoint.cloud and capacity_busy_seen:
                raise AllLocalCapacityBusyError()
            raise

        decision.capacity_attempts = capacity_attempts
        decision.queue_wait_ms = queue_wait_ms
        if capacity_busy_seen:
            if decision.endpoint.cloud:
                decision.reason = "cloud_capacity_fallback"
            elif affinity_spilled or conversation:
                decision.reason = "affinity_spillover"
            else:
                decision.reason = "capacity_spillover"
        elif selected_deployment != initial_deployment:
            decision.reason = "capacity_spillover"

        return (
            decision,
            routed_body,
            capsule,
            budget_reservation,
            capacity_attempts,
            queue_wait_ms,
        )


def _capacity_wait_seconds(
    routing: dict[str, Any],
    *,
    requested_model: str,
    decision: RouteDecision,
) -> float:
    if requested_model != "auto" or decision.affinity in {
        "hit",
        "logical-hit",
    }:
        return max(
            0.0,
            float(routing.get("affinity_capacity_wait_seconds", 3)),
        )
    return max(
        0.0,
        float(routing.get("new_request_capacity_wait_seconds", 0)),
    )


def _exclude_busy_decision(
    decision: RouteDecision,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
) -> None:
    if (
        decision.endpoint.backend_type in {"ai_pool", "codex_pool"}
        and decision.affinity in {"hit", "logical-hit"}
        and decision.deployment_id
    ):
        excluded_deployments.add(decision.deployment_id)
        return
    excluded_endpoints.add(decision.endpoint.id)


async def _filter_restart_draining_deployments(
    current: RouterRuntime,
    decision: RouteDecision,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
) -> bool:
    endpoint = decision.endpoint
    routes = dict(decision.deployment_candidates)
    deployment_ids = list(routes) or [
        decision.deployment_id or endpoint.id
    ]
    marker_ids = list(deployment_ids)
    if (
        endpoint.backend_type in {"ai_pool", "codex_pool"}
        and endpoint.id not in marker_ids
    ):
        marker_ids.append(endpoint.id)
    markers = {
        deployment_id: marker
        for deployment_id in marker_ids
        if (
            marker := await current.draining_marker(deployment_id)
        )
    }
    if not markers:
        return True

    status = await current.health.status(endpoint, force_refresh=True)
    blocked: set[str] = set()
    for deployment_id, marker in markers.items():
        if _deployment_available(endpoint, status, deployment_id):
            await current.clear_draining_marker(deployment_id)
            current.audit.write(
                "backend_available_after_drain",
                endpoint_id=endpoint.id,
                deployment_id=deployment_id,
                instance_id=current.instance_id,
                boot_id=current.boot_id,
                cleared_by_instance=marker.get("instance_id"),
                previous_boot_id=marker.get("previous_boot_id"),
            )
            continue
        await current.record_draining_busy(deployment_id, marker)
        if deployment_id == endpoint.id:
            blocked.update(deployment_ids)
        else:
            blocked.add(deployment_id)

    if not blocked:
        return True
    if routes:
        filtered = tuple(
            (deployment_id, url)
            for deployment_id, url in decision.deployment_candidates
            if deployment_id not in blocked
        )
        excluded_deployments.update(blocked)
        if not filtered:
            return False
        decision.deployment_candidates = filtered
        decision.deployment_id = filtered[0][0]
        decision.upstream_api_base = filtered[0][1]
        if decision.affinity in {"hit", "logical-hit"}:
            decision.affinity = "physical-failover"
            decision.reason = "physical_worker_unavailable"
        return True

    excluded_endpoints.add(endpoint.id)
    return False


def _deployment_available(
    endpoint: Endpoint,
    status: EndpointStatus,
    deployment_id: str,
) -> bool:
    if not status.healthy:
        return False
    if endpoint.backend_type in {"ai_pool", "codex_pool"}:
        workers = status.detail.get("workers", [])
        if deployment_id == endpoint.id:
            return any(
                item.get("ready")
                and item.get("state") == "available"
                for item in workers
            )
        return any(
            str(item.get("worker_id", "")) == deployment_id
            and item.get("ready")
            and item.get("state") == "available"
            for item in workers
        )
    return status.load_headroom > 0


async def _send_upstream(
    current: RouterRuntime,
    request: Request,
    body: dict[str, Any],
    *,
    api_kind: str,
    decision: RouteDecision,
) -> httpx.Response:
    payload = json.loads(json.dumps(body))
    direct = bool(decision.upstream_api_base)
    if decision.endpoint.metadata.get("thinking_via_extra_body"):
        thinking = payload.pop("thinking", None)
        if thinking is not None:
            extra_body = payload.setdefault("extra_body", {})
            if not isinstance(extra_body, dict):
                raise RouterError(
                    "extra_body must be an object",
                    status_code=400,
                    code="invalid_request",
                )
            extra_body.setdefault("thinking", thinking)
    if api_kind == "responses" and not decision.endpoint.cloud:
        _mirror_responses_format(payload)
    payload["model"] = (
        decision.endpoint.provider_model if direct else decision.endpoint.id
    )
    if api_kind == "responses":
        payload.pop("conversation", None)
        payload.pop("previous_response_id", None)
    base_url = (
        decision.upstream_api_base.rstrip("/")
        if decision.upstream_api_base
        else f"{current.internal_base_url}/v1"
    )
    url = f"{base_url}/{'chat/completions' if api_kind == 'chat' else 'responses'}"
    api_key = current.internal_api_key
    if direct:
        api_key = os.environ.get(
            decision.endpoint.backend_api_key_env,
            "",
        )
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request.headers.get("x-request-id") or uuid4().hex,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    conversation_id = (
        request.headers.get("x-1panel-conversation-id")
        or request.headers.get("x-litellm-session-id")
    )
    if conversation_id:
        headers["X-1Panel-Conversation-ID"] = conversation_id
    if request.headers.get("x-litellm-session-id"):
        headers["x-litellm-session-id"] = request.headers["x-litellm-session-id"]
    upstream_request = current.internal_client.build_request(
        "POST",
        url,
        headers=headers,
        json=payload,
    )
    return await current.internal_client.send(upstream_request, stream=True)


def _mirror_responses_format(payload: dict[str, Any]) -> None:
    if isinstance(payload.get("response_format"), dict):
        return
    text = payload.get("text")
    output_format = (
        text.get("format")
        if isinstance(text, dict)
        else None
    )
    if not isinstance(output_format, dict):
        return
    format_type = str(output_format.get("type", ""))
    if format_type == "json_object":
        payload["response_format"] = {"type": "json_object"}
        return
    if format_type != "json_schema":
        return
    payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            key: output_format[key]
            for key in ("name", "description", "schema", "strict")
            if key in output_format
        },
    }


def _apply_protocol_constraints(
    decision: RouteDecision,
    api_kind: str,
) -> None:
    decision.protocol = api_kind
    decision.native_or_adapter = (
        decision.endpoint.capabilities.protocol_mode(api_kind)
    )


async def _exclude_failed_decision(
    current: RouterRuntime,
    decision: RouteDecision,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
) -> None:
    cooldown = int(
        current.settings.section("failover").get("cooldown_seconds", 20)
    )
    if (
        decision.endpoint.backend_type == "codex_pool"
        and decision.upstream_api_base
        and decision.deployment_id
    ):
        if len(decision.deployment_candidates) > 1:
            excluded_deployments.add(decision.deployment_id)
        else:
            excluded_endpoints.add(decision.endpoint.id)
        await current.health.mark_failure(decision.deployment_id, cooldown)
        return
    if (
        decision.endpoint.backend_type == "ai_pool"
        and decision.upstream_api_base
        and decision.deployment_id
    ):
        excluded_deployments.add(decision.deployment_id)
        await current.health.mark_failure(decision.deployment_id, cooldown)
        return
    excluded_endpoints.add(decision.endpoint.id)
    await current.health.mark_failure(decision.endpoint.id, cooldown)


async def _prepare_routed_body(
    current: RouterRuntime,
    body: dict[str, Any],
    *,
    api_kind: str,
    decision: RouteDecision,
    request_id: str,
) -> tuple[dict[str, Any], Any | None]:
    if not decision.migration:
        routed = json.loads(json.dumps(body))
        if (
            api_kind == "responses"
            and decision.endpoint.backend_type == "codex_pool"
        ):
            routed.pop("conversation", None)
            routed.pop("previous_response_id", None)
        return routed, None
    if (
        api_kind == "chat"
        and decision.prompt_tokens + decision.output_reserve_tokens
        <= decision.endpoint.safe_context_tokens
    ):
        return body, None
    if not bool(current.settings.section("compaction").get("enabled", True)):
        raise RouterError(
            "cross-model upgrade requires context compaction",
            status_code=503,
            code="compaction_disabled",
        )
    compaction_lease = await current.scheduler.begin_request(None)
    try:
        await _acquire_internal_model(
            current,
            lease=compaction_lease,
            request_id=f"{request_id}:compactor",
            model_id=current.compactor.model_id,
        )
        capsule = await current.compactor.compact(
            body,
            api_kind=api_kind,
            target_context_tokens=decision.endpoint.safe_context_tokens,
        )
    except QueueTimeoutError as exc:
        raise CompactionUnavailableError(
            "the compaction model queue did not become available in time"
        ) from exc
    finally:
        await compaction_lease.release()
    compacted_messages = current.compactor.cipher.decrypt(
        capsule.encrypted_messages
    )
    routed = replace_messages(body, api_kind, compacted_messages)
    if (
        api_kind == "responses"
        and decision.endpoint.backend_type == "codex_pool"
    ):
        routed.pop("conversation", None)
        routed.pop("previous_response_id", None)
    decision.prompt_tokens = capsule.after_tokens
    return routed, capsule


async def _acquire_internal_model(
    current: RouterRuntime,
    *,
    lease: Any,
    request_id: str,
    model_id: str,
    wait: bool = True,
) -> None:
    endpoint = current.registry.by_id(model_id)
    if endpoint is None or not endpoint.enabled:
        raise RouterError(
            f"internal model is not registered or enabled: {model_id}",
            status_code=503,
            code="internal_model_unavailable",
        )
    if wait:
        await current.scheduler.acquire_deployment(
            lease,
            endpoint.id,
            request_id,
            timeout_seconds=float(
                current.settings.section("queue").get(
                    "timeout_seconds",
                    120,
                )
            ),
            affinity_priority=False,
            capacity=endpoint.max_concurrency,
        )
        return
    acquired = await current.scheduler.try_acquire_deployment_candidates(
        lease,
        (endpoint.id,),
        capacity=endpoint.max_concurrency,
    )
    if not acquired:
        raise QueueTimeoutError()


async def _save_conversation(
    current: RouterRuntime,
    *,
    conversation_id: str | None,
    previous: ConversationState | None,
    decision: RouteDecision,
    capsule: Any | None,
) -> ConversationState | None:
    if not conversation_id:
        return None
    status = await current.health.status(decision.endpoint)
    state = updated_conversation_state(
        previous,
        conversation_id=conversation_id,
        decision=decision,
        cache_generation=status.cache_generation,
        encrypted_capsule=capsule.encrypted_messages if capsule else None,
        boundary_hash=capsule.boundary_hash if capsule else None,
    )
    await current.conversations.save(state)
    return state


async def _conversation_id(
    current: RouterRuntime,
    request: Request,
    body: dict[str, Any],
    api_kind: str,
    client_id: str,
) -> tuple[str, str]:
    for name in ("x-1panel-conversation-id", "x-litellm-session-id"):
        value = request.headers.get(name, "").strip()
        if value:
            return value[:256], "stateful"
    if api_kind == "responses":
        conversation = body.get("conversation")
        if isinstance(conversation, str) and conversation.strip():
            return conversation.strip()[:256], "stateful"
        if isinstance(conversation, dict) and conversation.get("id"):
            return str(conversation["id"])[:256], "stateful"
        mapped = await current.conversations.conversation_for_response(
            str(body.get("previous_response_id", "")).strip() or None
        )
        if mapped and await current.conversations.get(mapped):
            return mapped, "inferred"
        return f"inferred-{uuid4().hex}", "inferred"

    identities = history_lookup_identities(extract_messages(body, api_kind))
    mapped = await current.conversations.conversation_for_history(
        client_id,
        identities,
    )
    if mapped and await current.conversations.get(mapped):
        return mapped, "inferred"
    return f"inferred-{uuid4().hex}", "inferred"


async def _map_response_id(
    current: RouterRuntime,
    payload: bytes,
    state: ConversationState | None,
) -> None:
    if not state:
        return
    try:
        response_id = str(json.loads(payload).get("id", "")).strip()
    except Exception:
        return
    if response_id:
        await current.conversations.map_response(
            response_id,
            state.conversation_id,
        )


async def _stream_response(
    current: RouterRuntime,
    upstream: httpx.Response,
    *,
    lease: Any,
    client_id: str,
    request_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
    state: ConversationState | None,
    body: dict[str, Any],
    api_kind: str,
    started_at: float,
    cache_snapshot: dict[str, float] | None,
) -> AsyncIterator[bytes]:
    accumulator = SSEAccumulator(api_kind)
    status_code = upstream.status_code
    completed = False
    try:
        async for chunk in upstream.aiter_bytes():
            accumulator.feed(chunk)
            yield chunk
            if accumulator.completed:
                completed = True
                break
        else:
            completed = True
    finally:
        accumulator.finish()
        try:
            await upstream.aclose()
            if accumulator.response_id and conversation_id:
                await current.conversations.map_response(
                    accumulator.response_id,
                    conversation_id,
                )
            if completed:
                await persist_history(
                    current.compactor,
                    current.conversations,
                    state=state,
                    client_id=client_id,
                    body=body,
                    api_kind=api_kind,
                    assistant_items=accumulator.assistant_items(),
                )
            await _audit(
                current,
                request_id=request_id,
                client_id=client_id,
                conversation_id=conversation_id,
                decision=decision,
                status_code=status_code,
                started_at=started_at,
                usage=accumulator.usage,
                cache_snapshot=cache_snapshot,
            )
        finally:
            await lease.release()
            await current.limiter.release_parallel(
                client_id,
                lease.owner_token,
            )
            await current.track_request_finished(lease.owner_token)


def _response_headers(
    upstream: httpx.Response,
    decision: RouteDecision,
    request_id: str,
    *,
    conversation_id: str,
    conversation_mode: str,
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in {"content-length", "content-encoding"}
    }
    headers.update(decision.response_headers(request_id))
    headers["X-1Panel-Conversation-ID"] = conversation_id
    headers["X-1Panel-Conversation-Mode"] = conversation_mode
    return headers


async def _audit(
    current: RouterRuntime,
    *,
    request_id: str,
    client_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
    status_code: int,
    started_at: float,
    response_payload: bytes | None = None,
    usage: dict[str, Any] | None = None,
    cache_snapshot: dict[str, float] | None = None,
) -> None:
    cached_prompt_tokens_fallback = await _prefix_cache_delta(
        current,
        decision,
        cache_snapshot,
    )
    cached_prompt_tokens, cache_hit_ratio = _cache_metrics(
        response_payload,
        usage,
        cached_prompt_tokens_fallback=cached_prompt_tokens_fallback,
        prompt_tokens_fallback=decision.prompt_tokens,
    )
    current.audit.write(
        "request_completed",
        request_id=request_id,
        client_id=client_id,
        conversation_id=conversation_id,
        requested_model=decision.requested_model,
        selected_model=decision.endpoint.public_model,
        endpoint_id=decision.endpoint.id,
        deployment_id=decision.deployment_id or decision.endpoint.id,
        node=decision.endpoint.node,
        task=decision.task,
        reason=decision.reason,
        affinity=decision.affinity,
        prompt_tokens=decision.prompt_tokens,
        output_reserve_tokens=decision.output_reserve_tokens,
        attempts=decision.attempts,
        capacity_attempts=decision.capacity_attempts,
        queue_wait_ms=round(decision.queue_wait_ms, 2),
        status_code=status_code,
        latency_ms=round((time.monotonic() - started_at) * 1000, 2),
        cached_prompt_tokens=cached_prompt_tokens,
        cache_hit_ratio=cache_hit_ratio,
        required_capabilities=list(decision.required_capabilities),
        tool_history_repairs=decision.tool_history_repairs,
        protocol=decision.protocol,
        native_or_adapter=decision.native_or_adapter,
        candidate_rejections=list(decision.candidate_rejections),
        instance_id=current.instance_id,
        boot_id=current.boot_id,
    )


def _cache_metrics(
    response_payload: bytes | None,
    usage: dict[str, Any] | None,
    *,
    cached_prompt_tokens_fallback: int | None = None,
    prompt_tokens_fallback: int = 0,
) -> tuple[int | None, float | None]:
    current = usage
    if current is None and response_payload:
        try:
            value = json.loads(response_payload)
        except Exception:
            value = {}
        if isinstance(value, dict):
            current = value.get("usage")
            if not isinstance(current, dict):
                response = value.get("response")
                current = (
                    response.get("usage")
                    if isinstance(response, dict)
                    else None
                )
    if not isinstance(current, dict) and cached_prompt_tokens_fallback is None:
        return None, None
    current = current if isinstance(current, dict) else {}
    prompt_tokens = int(
        current.get("prompt_tokens")
        or current.get("input_tokens")
        or prompt_tokens_fallback
    )
    details = current.get("prompt_tokens_details")
    cached_values = [
        current.get("prompt_cache_hit_tokens"),
        current.get("cache_read_input_tokens"),
    ]
    if isinstance(details, dict):
        cached_values.append(details.get("cached_tokens"))
    explicit_cached_values = [
        int(value)
        for value in cached_values
        if isinstance(value, (int, float))
    ]
    cached_prompt_tokens = (
        max(explicit_cached_values)
        if explicit_cached_values
        else int(cached_prompt_tokens_fallback or 0)
    )
    ratio = (
        round(cached_prompt_tokens / prompt_tokens, 6)
        if prompt_tokens > 0
        else None
    )
    return cached_prompt_tokens, ratio


async def _prefix_cache_snapshot(
    current: RouterRuntime,
    decision: RouteDecision,
) -> dict[str, float] | None:
    if decision.endpoint.backend_type != "vllm":
        return None
    reader = getattr(current.health, "prefix_cache_counters", None)
    if reader is None:
        return None
    return await reader(decision.endpoint)


async def _prefix_cache_delta(
    current: RouterRuntime,
    decision: RouteDecision,
    before: dict[str, float] | None,
) -> int | None:
    if before is None:
        return None
    after = await _prefix_cache_snapshot(current, decision)
    if after is None:
        return None
    query_delta = float(after.get("queries", 0)) - float(
        before.get("queries", 0)
    )
    hit_delta = float(after.get("hits", 0)) - float(before.get("hits", 0))
    if query_delta < 0 or hit_delta < 0:
        return None
    return int(round(min(query_delta, hit_delta)))


def _audit_started(
    current: RouterRuntime,
    *,
    request_id: str,
    client_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
) -> None:
    current.audit.write(
        "request_started",
        request_id=request_id,
        client_id=client_id,
        conversation_id=conversation_id,
        requested_model=decision.requested_model,
        selected_model=decision.endpoint.public_model,
        endpoint_id=decision.endpoint.id,
        deployment_id=decision.deployment_id or decision.endpoint.id,
        node=decision.endpoint.node,
        task=decision.task,
        reason=decision.reason,
        affinity=decision.affinity,
        prompt_tokens=decision.prompt_tokens,
        output_reserve_tokens=decision.output_reserve_tokens,
        attempts=decision.attempts,
        capacity_attempts=decision.capacity_attempts,
        queue_wait_ms=round(decision.queue_wait_ms, 2),
        required_capabilities=list(decision.required_capabilities),
        tool_history_repairs=decision.tool_history_repairs,
        protocol=decision.protocol,
        native_or_adapter=decision.native_or_adapter,
        candidate_rejections=list(decision.candidate_rejections),
        instance_id=current.instance_id,
        boot_id=current.boot_id,
    )


def _runtime(request: Request) -> RouterRuntime:
    if not hasattr(request.state, "started_at"):
        request.state.started_at = time.monotonic()
    return request.app.state.runtime


def _payload_too_large(max_request_bytes: int) -> RouterError:
    return RouterError(
        "request body exceeds the configured size limit",
        status_code=413,
        code="payload_too_large",
        details={"max_request_bytes": max_request_bytes},
    )


def _error_response(exc: RouterError) -> JSONResponse:
    error = {
        "message": str(exc),
        "type": "invalid_request_error"
        if exc.status_code < 500
        else "server_error",
        "code": exc.code,
    }
    if exc.details:
        error["details"] = exc.details
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={"error": error},
    )


app = create_app()
