from __future__ import annotations

from .workbuddy_history import WorkBuddyHistory
import copy

from .content_audit import ContentObservation, ArchiveReader
from .protocol import stabilize_workbuddy_tools
from .prefix_break import PrefixBreakCollector
from .cache_audit import TelemetryCollector, OutputClock
from .usage_evidence import UsageOnlyFilter, token_count, usage_dict, usage_measurement

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, AsyncIterator, Awaitable
from uuid import uuid4

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .compaction import extract_messages, replace_messages
from .errors import (
    AllLocalCapacityBusyError,
    AuthenticationError,
    CapacityBusyError,
    CompactionUnavailableError,
    HistoryMigrationRequiredError,
    NoCompatibleModelError,
    NoEligibleModelError,
    PublicIdentityUnavailableError,
    QueueTimeoutError,
    RouterError,
)
from .history import (
    SSEAccumulator,
    apply_stored_history,
    assistant_items_from_response,
    deepseek_history_requires_migration,
    history_lookup_identities,
    history_identities,
    normalize_history_for_provider,
    persist_history,
    provider_family,
)
from .identity import (
    IdentityProfile,
    IdentityStreamSanitizer,
    identity_disclosure_requires_model_protocol,
    internal_identifiers,
    is_identity_disclosure_request,
    sanitize_payload,
    sanitize_value,
)
from .media import inspect_image_inputs, normalize_ai_images
from .media_service.gateway import model_descriptors as media_model_descriptors, router as media_router
from .policy import updated_conversation_state
from .prefix_affinity import PrefixAffinityRecord, PrefixSignature
from .prompt_directives import (
    resolve_conversation_directive,
    sanitize_prompt_directives,
)
from .protocol import (
    move_workbuddy_dynamic_context,
    normalize_llama_tool_schemas,
    normalize_request,
)
from .public_protocol import private_history_items
from .privacy_view import review_view
from .responses_adapter import (
    chat_response_to_responses,
    chat_stream_to_responses,
    responses_request_to_chat,
)
from .route_trace import (
    DecisionTrace,
    registry_fingerprint,
    request_excerpt,
    settings_fingerprint,
)
from .runtime import RouterRuntime, build_runtime
from .token_counter import output_reserve_tokens, request_modalities
from .types import (
    ConversationState,
    Endpoint,
    EndpointStatus,
    Evaluation,
    LineageContext,
    ModelCallTarget,
    PhysicalDeployment,
    RequestCapabilities,
    RouteDecision,
)


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
    app.include_router(media_router())

    @app.exception_handler(RouterError)
    async def router_error_handler(
        request: Request,
        exc: RouterError,
    ) -> JSONResponse:
        await _finish_request_trace_error(request, exc)
        current = _runtime(request)
        profile = getattr(
            request.state,
            "identity_profile",
            IdentityProfile.from_settings(
                current.settings.section("identity")
            ),
        )
        return _error_response(
            exc,
            profile=profile,
            identifiers=internal_identifiers(current.registry),
            public=(
                getattr(
                    request.state,
                    "disclosure_mode",
                    "public",
                )
                == "public"
            ),
            request_id=getattr(
                request.state,
                "server_request_id",
                None,
            ),
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        return {"ok": bool(await current.store.ping())}

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

    @app.get("/internal/request-content/{request_id}")
    async def internal_request_content(request: Request, request_id: str, stage: str | None = None,
                                       offset: int = Query(default=0, ge=0), limit: int = Query(default=16384, ge=1, le=65536)):
        current = _runtime(request)
        current.auth.authenticate_admin(request.headers.get("authorization"))
        if not await current.route_traces.get(request_id):
            raise RouterError("request not found", status_code=404, code="route_trace_not_found")
        def read():
            return ArchiveReader(os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3"),
                                 os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key")).content(request_id, stage, offset, limit)
        try:
            value = await asyncio.to_thread(read)
        except KeyError:
            raise RouterError("stage not available", status_code=404, code="content_stage_not_found")
        except Exception:
            raise RouterError("encrypted archive is unavailable", status_code=503, code="content_archive_unavailable")
        if value is None:
            raise RouterError("content was not archived", status_code=404, code="content_not_archived")
        current.audit.write("admin_content_read_internal", request_id=request_id, stage=stage, offset=offset)
        return JSONResponse(value, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/internal/training/status")
    async def internal_training_status(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        current.auth.authenticate_admin(request.headers.get("authorization"))
        if current.training is None:
            return {"enabled": False}
        return await current.training.status()

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        current = _runtime(request)
        request.state.server_request_id = uuid4().hex
        request.state.disclosure_mode = "public"
        current.reload_settings()
        await current.reload_endpoint_config()
        client = await current.auth.authenticate(
            request.headers.get("authorization")
        )
        request.state.disclosure_mode = client.policy.disclosure_mode
        profile = IdentityProfile.from_settings(
            current.settings.section("identity")
        )
        if client.policy.disclosure_mode == "public":
            _ensure_public_identity(profile)
            values = await _identity_model_descriptors(
                current,
                client.policy.models,
                profile,
            )
        else:
            values = []
            for model in (
                "auto",
                *current.registry.enabled_public_models(),
            ):
                if (
                    "*" not in client.policy.models
                    and model not in client.policy.models
                ):
                    continue
                values.append(await _model_descriptor(current, model))
        values.extend(media_model_descriptors(client.policy))
        return JSONResponse(
            {"object": "list", "data": values},
            headers={
                "Cache-Control": "no-store",
                "Vary": "Authorization",
                "X-Request-ID": request.state.server_request_id,
            },
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _proxy(request, "chat")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await _proxy(request, "responses")

    return app


async def _model_descriptor(
    current: RouterRuntime,
    model: str,
    *,
    endpoints_override: tuple[Endpoint, ...] | None = None,
    owned_by: str = "1panel-ai-router",
    use_auto_limits: bool | None = None,
) -> dict[str, Any]:
    if use_auto_limits is None:
        use_auto_limits = model == "auto"
    endpoints = (
        endpoints_override
        if endpoints_override is not None
        else (
            tuple(
                endpoint
                for endpoint in current.registry.responders()
                if endpoint.enabled and endpoint.auto_candidate
            )
            if model == "auto"
            else tuple(
                endpoint
                for endpoint in current.registry.by_public_model(model)
                if endpoint.enabled
            )
        )
    )
    statuses = await current.health.statuses(endpoints)
    modalities = sorted(
        {
            modality
            for endpoint in endpoints
            for modality in (
                statuses[endpoint.id].detail.get(
                    "effective_modalities",
                    endpoint.modalities,
                )
                if endpoint.backend_type == "ai_pool"
                else endpoint.modalities
            )
        }
    )
    supports_images = "image" in modalities
    max_image_counts: list[int] = []
    image_count_unbounded = False
    for endpoint in endpoints:
        if endpoint.backend_type == "ai_pool":
            for worker in statuses[endpoint.id].detail.get("workers", []):
                if "image" not in worker.get("modalities", []):
                    continue
                max_images = worker.get("max_images")
                if max_images is None:
                    image_count_unbounded = True
                else:
                    max_image_counts.append(int(max_images))
        elif "image" in endpoint.modalities:
            max_images = endpoint.metadata.get("max_images")
            if max_images is None:
                image_count_unbounded = True
            else:
                max_image_counts.append(int(max_images))
    descriptor = {
        "id": model,
        "object": "model",
        "created": 0,
        "owned_by": owned_by,
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
            "tool_choice": any(
                endpoint.capabilities.tool_choice
                for endpoint in endpoints
            ),
            "tool_choice_modes": sorted(
                {
                    mode
                    for endpoint in endpoints
                    for mode in endpoint.capabilities.tool_choice_modes
                }
            ),
            "output_token_limit": any(
                endpoint.capabilities.output_token_limit
                for endpoint in endpoints
            ),
        },
    }
    max_context_tokens = max(
        (
            min(
                endpoint.safe_context_tokens,
                statuses[endpoint.id].eligible_context_tokens
                or endpoint.safe_context_tokens,
            )
            for endpoint in endpoints
        ),
        default=0,
    )
    configured_output_limits = [
        int(endpoint.metadata["max_output_tokens"])
        for endpoint in endpoints
        if endpoint.metadata.get("max_output_tokens") is not None
    ]
    alias = current.registry.model_aliases.get(model, {})
    alias_max_input_tokens = alias.get("max_input_tokens")
    alias_max_output_tokens = alias.get("max_output_tokens")
    if use_auto_limits:
        max_output_tokens = int(
            current.settings.section("routing").get(
                "auto_max_output_tokens",
                65536,
            )
        )
        declared_input_tokens = int(
            current.settings.section("routing").get(
                "auto_max_input_tokens",
                max_context_tokens,
            )
        )
        max_context_tokens = min(
            max_context_tokens,
            declared_input_tokens + max_output_tokens,
        )
    else:
        max_output_tokens = max(
            configured_output_limits,
            default=min(65536, max_context_tokens),
        )
        if alias_max_output_tokens is not None:
            max_output_tokens = min(
                max_output_tokens,
                int(alias_max_output_tokens),
            )
    max_output_tokens = min(
        max_output_tokens,
        max_context_tokens,
    )
    max_input_tokens = max(
        0,
        max_context_tokens - max_output_tokens,
    )
    if not use_auto_limits and alias_max_input_tokens is not None:
        max_input_tokens = min(
            max_input_tokens,
            int(alias_max_input_tokens),
        )
        max_context_tokens = min(
            max_context_tokens,
            max_input_tokens + max_output_tokens,
        )
    descriptor["maxInputTokens"] = max_input_tokens
    descriptor["maxOutputTokens"] = max_output_tokens
    descriptor["contextWindow"] = max_context_tokens
    if supports_images:
        descriptor["maxInputImages"] = (
            None
            if image_count_unbounded
            else max(max_image_counts, default=1)
        )
    return descriptor


async def _identity_model_descriptors(
    current: RouterRuntime,
    allowed_models: tuple[str, ...],
    profile: IdentityProfile,
) -> list[dict[str, Any]]:
    allowed = set(allowed_models)
    if allowed != {profile.public_model_id}:
        return []
    endpoints = tuple(
        endpoint
        for endpoint in current.registry.responders()
        if endpoint.enabled and endpoint.auto_candidate
    )
    if not endpoints:
        return []

    return [
        await _model_descriptor(
            current,
            profile.public_model_id,
            endpoints_override=endpoints,
            owned_by=profile.provider_name,
            use_auto_limits=True,
        )
    ]


def _identity_alias_target(
    current: RouterRuntime,
    allowed_models: tuple[str, ...],
    profile: IdentityProfile,
) -> str | None:
    allowed = set(allowed_models)
    if (
        "*"
        in allowed
        or "auto"
        in allowed
        or profile.public_model_id in allowed
    ):
        return "auto"
    explicit = sorted(
        model
        for model in allowed
        if current.registry.by_public_model(model)
    )
    return explicit[0] if len(explicit) == 1 else None


def _resolve_requested_model(
    current: RouterRuntime,
    allowed_models: tuple[str, ...],
    requested_model: str,
    profile: IdentityProfile,
    disclosure_mode: str = "internal",
) -> str:
    if disclosure_mode == "public":
        _ensure_public_identity(profile)
        if (
            requested_model not in {"auto", profile.public_model_id}
            or set(allowed_models) != {profile.public_model_id}
        ):
            raise RouterError(
                "the requested model is not available",
                status_code=404,
                code="model_not_found",
            )
        return "auto"
    if (
        not profile.enabled
        or requested_model != profile.public_model_id
    ):
        return requested_model
    alias_target = _identity_alias_target(
        current,
        allowed_models,
        profile,
    )
    if alias_target is not None:
        return alias_target
    raise AuthenticationError(
        "API key does not permit this public model alias"
    )


def _ensure_prompt_directive_access(
    current: RouterRuntime,
    authenticated: Any,
    directive: Any,
    *,
    requested_model: str,
) -> None:
    if directive is None:
        return
    if requested_model == "auto":
        return
    endpoint = current.registry.by_id(str(directive.endpoint_id or ""))
    if endpoint is None:
        return
    allowed_models = set(authenticated.policy.models)
    if "*" in allowed_models or endpoint.public_model in allowed_models:
        return
    if any(
        any(
            candidate.id == endpoint.id
            for candidate in current.registry.by_public_model(model)
        )
        for model in allowed_models
    ):
        return
    raise AuthenticationError(
        "API key does not permit the directed model"
    )


def _ensure_public_identity(profile: IdentityProfile) -> None:
    if not profile.enabled or not profile.complete:
        raise PublicIdentityUnavailableError()


def _identity_for_disclosure(
    profile: IdentityProfile,
    disclosure_mode: str,
) -> IdentityProfile:
    if disclosure_mode == "public":
        _ensure_public_identity(profile)
        return profile
    return replace(profile, enabled=False)


async def _proxy(request: Request, api_kind: str) -> Response:
    current = _runtime(request)
    current.reload_settings()
    await current.reload_endpoint_config()
    configured_identity = IdentityProfile.from_settings(
        current.settings.section("identity")
    )
    request.state.identity_profile = configured_identity
    request.state.disclosure_mode = "public"
    request_id = uuid4().hex
    client_request_id = (
        request.headers.get("x-request-id", "").strip()[:256]
        or None
    )
    request.state.server_request_id = request_id
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
    client_requested_model = str(body.get("model", "")).strip()
    if not client_requested_model:
        raise RouterError(
            "model is required",
            status_code=400,
            code="model_required",
    )
    authenticated = await current.auth.authenticate(
        request.headers.get("authorization")
    )
    request.state.disclosure_mode = authenticated.policy.disclosure_mode
    identity = _identity_for_disclosure(
        configured_identity,
        authenticated.policy.disclosure_mode,
    )
    request.state.identity_profile = identity
    observation = ContentObservation()
    observation.capture("received", body, archive_body=False)
    request.state.content_observation = observation
    prompt_directive_settings = current.settings.section("routing").get(
        "prompt_directives",
        {},
    )
    prompt_directive_result = sanitize_prompt_directives(
        body,
        api_kind,
        prompt_directive_settings,
    )
    body = prompt_directive_result.body
    observation.capture("after_directives", body)
    received_body = json.loads(json.dumps(body))
    trace = DecisionTrace(
        request_id=request_id,
        client_id=authenticated.policy.id,
        key_id=authenticated.key_id,
        protocol=api_kind,
        requested_model=client_requested_model,
        excerpt=request_excerpt(received_body, api_kind),
        instance_id=current.instance_id,
        boot_id=current.boot_id,
        settings_hash=settings_fingerprint(current.settings),
        registry_hash=registry_fingerprint(current.registry),
        client_models=authenticated.policy.models,
        client_request_id=client_request_id,
        disclosure_mode=authenticated.policy.disclosure_mode,
    )
    request.state.route_trace = trace
    await _save_request_trace(current, trace)
    requested_model = _resolve_requested_model(
        current,
        authenticated.policy.models,
        client_requested_model,
        configured_identity,
        authenticated.policy.disclosure_mode,
    )
    public_auto_request = (
        authenticated.policy.disclosure_mode == "public"
        and configured_identity.enabled
        and client_requested_model
        in {"auto", configured_identity.public_model_id}
    )
    if not public_auto_request and not (
        configured_identity.enabled
        and client_requested_model == configured_identity.public_model_id
    ):
        current.auth.ensure_model_access(
            authenticated,
            client_requested_model,
        )
    if current.draining:
        raise RouterError(
            "router instance is draining",
            status_code=503,
            code="router_draining",
            details={"instance_id": current.instance_id},
        )
    lineage_body = copy.deepcopy(body)
    dynamic_context_move = move_workbuddy_dynamic_context(
        body,
        api_kind,
        client_id=authenticated.policy.id,
    )
    before_reorder = body
    body = dynamic_context_move.body
    observation.check_workbuddy(before_reorder, body, dynamic_context_move)
    observation.capture("workbuddy_reordered", body)
    trace.payload["observation"] = {"content": observation.metadata()}
    if dynamic_context_move.skip_reason != "not_applicable":
        current.audit.write(
            "workbuddy_dynamic_context_moved" if dynamic_context_move.moved
            else "workbuddy_dynamic_context_skipped",
            request_id=request_id,
            client_id=authenticated.policy.id,
            moved=dynamic_context_move.moved,
            moved_chars=dynamic_context_move.moved_chars,
            mode=dynamic_context_move.mode,
            target_user_index=dynamic_context_move.target_user_index,
            stable_prefix_sha256=dynamic_context_move.stable_prefix_sha256,
            skip_reason=dynamic_context_move.skip_reason,
        )
    if api_kind == "chat" and authenticated.policy.id in {"workbuddy-public", "workbuddy-qwen36-shared"}:
        history_store = WorkBuddyHistory(current.route_traces.database_path)
        restored, history_report = await history_store.apply(
            lineage_body, authenticated.policy.id,
            reset=_truthy_header(request.headers.get("x-1panel-context-compacted", "")),
        )
        if restored is not None:
            body = restored
        elif history_report.get("association") == "unconfirmed" and (
            any(m.get("role") in {"assistant", "tool"} for m in lineage_body.get("messages", []) if isinstance(m, dict))
            or sum(m.get("role") == "user" for m in lineage_body.get("messages", []) if isinstance(m, dict)) > 1
        ):
            body = copy.deepcopy(lineage_body)
            history_report["reorder_bypassed"] = True
        history_report["raw_identities"] = ["wb-raw-v1:" + key for key in history_identities(extract_messages(lineage_body, api_kind))]
        history_report.setdefault("positions", list(range(len(lineage_body.get("messages", [])))))
        observation.checks.append({"check":"workbuddy_history", **history_report})
        observation.capture("workbuddy_history_preserved", body)
        current.audit.write("workbuddy_history", request_id=request_id,
            **{k:v for k,v in history_report.items() if k != "positions"})
    body, tool_stability = stabilize_workbuddy_tools(body, api_kind, client_id=authenticated.policy.id)
    observation.checks.append({"check": "tool_serialization_stability", **tool_stability})
    observation.capture("tools_stabilized", body)
    normalized = normalize_request(
        body,
        api_kind,
        validate_history=not (
            api_kind == "responses"
            and bool(str(body.get("previous_response_id", "")).strip())
        ),
    )
    body = normalized.body
    observation.capture("normalized", body)
    tool_history_repairs = normalized.repairs
    client_compacted = _truthy_header(
        request.headers.get("x-1panel-context-compacted", "")
    )
    lineage = await _lineage_context(
        current,
        request,
        lineage_body if api_kind == "chat" and authenticated.policy.id in {"workbuddy-public", "workbuddy-qwen36-shared"} else body,
        api_kind,
        authenticated.policy.id,
        force_new_inferred=client_compacted,
        raw_identity_namespace=api_kind == "chat" and authenticated.policy.id in {"workbuddy-public", "workbuddy-qwen36-shared"},
    )
    conversation_id = lineage.lineage_id
    conversation_mode = lineage.mode
    resolved_directive, clear_directive_affinity = (
        resolve_conversation_directive(
            prompt_directive_result.directive,
            lineage.parent,
            prompt_directive_settings,
        )
    )
    _ensure_prompt_directive_access(
        current,
        authenticated,
        resolved_directive,
        requested_model=requested_model,
    )
    trace.set_request_context(
        conversation_id=conversation_id,
        conversation_mode=conversation_mode,
        branch_id=lineage.branch_id,
        parent_branch_id=lineage.parent_branch_id,
        lineage_relation=lineage.relation,
        context_compacted=client_compacted,
        context_compaction_source=(
            "client" if client_compacted else None
        ),
    )
    if identity.enabled:
        identity_view = review_view(body, api_kind)
        trace.payload["privacy_input"] = {
            "source": identity_view.source,
            "certain": identity_view.certain,
        }
    await _save_request_trace(current, trace)
    if identity.enabled and is_identity_disclosure_request(
        body,
        api_kind,
        identity_context=bool(
            lineage.parent is not None
            and lineage.parent.identity_only
        ),
    ):
        return await _identity_intercept_response(
            current,
            body=body,
            api_kind=api_kind,
            request_id=request_id,
            client_id=authenticated.policy.id,
            key_id=authenticated.key_id,
            conversation_id=conversation_id,
            identity=identity,
            trace=trace,
            lineage=lineage,
            rpm_limit=authenticated.policy.rpm_limit,
            tpm_limit=authenticated.policy.tpm_limit,
            max_parallel_requests=(
                authenticated.policy.max_parallel_requests
            ),
        )
    training_token = None
    try:
        if current.training is not None:
            training_token = await current.training.begin(
                request_id=request_id,
                conversation_id=conversation_id,
                conversation_mode=conversation_mode,
                client_id=authenticated.policy.id,
                key_id=authenticated.key_id,
                protocol=api_kind,
                received_body=received_body,
                instance_id=current.instance_id,
                boot_id=current.boot_id,
            )
    except BaseException as exc:
        await _finish_trace_exception(current, trace, exc)
        raise
    try:
        lease = await current.scheduler.begin_request(None)
    except BaseException as exc:
        await _fail_training_record(
            current,
            training_token,
            exc,
        )
        await _finish_trace_exception(current, trace, exc)
        raise
    request.state.training_token = training_token
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
        stored_conversation = lineage.parent
        conversation_control = (
            await current.conversation_controls.consume_for_request(
                authenticated.policy.id,
                conversation_id,
            )
        )
        routing_conversation = (
            None
            if (
                client_compacted
                or clear_directive_affinity
                or conversation_control.get("reset")
                or (
                    stored_conversation is not None
                    and stored_conversation.identity_only
                )
            )
            else stored_conversation
        )
        if conversation_control.get("reset"):
            trace.record(
                1,
                "conversation_affinity",
                "evaluated",
                branch="admin_reset",
                reason="conversation_admin_reset",
                evidence={
                    "conversation_id": conversation_id,
                    "operator": conversation_control["reset"].get(
                        "operator"
                    ),
                    "reason": conversation_control["reset"].get("reason"),
                },
                path=False,
            )
        effective_body = json.loads(json.dumps(body))
        if (
            not client_compacted
            and api_kind == "responses"
            and stored_conversation is not None
        ):
            effective_body = await apply_stored_history(
                current.compactor,
                current.conversations,
                body,
                api_kind=api_kind,
                conversation=stored_conversation,
            )
        normalized_effective = normalize_request(
            effective_body,
            api_kind,
        )
        effective_body = normalized_effective.body
        observation.capture("effective", effective_body)
        trace.payload["observation"]["content"] = observation.metadata()
        tool_history_repairs += normalized_effective.repairs
        required_capabilities = normalized_effective.required
        if current.training is not None:
            await current.training.set_effective_context(
                training_token,
                effective_body=effective_body,
            )
        prompt_tokens = current.token_counter.count_request(
            identity.inject(effective_body, api_kind),
            api_kind,
        )
        requested_prompt_tokens = prompt_tokens
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

        if authenticated.policy.disclosure_mode == "public":
            current.review_privacy(
                effective_body, api_kind, request_id=request_id,
                client_id=authenticated.policy.id,
            )
        header_values = {key.lower(): value for key, value in request.headers.items()}
        async def acquire_evaluator() -> ModelCallTarget:
            return await _acquire_internal_model(
                current,
                lease=lease,
                request_id=f"{request_id}:evaluator",
                model_id=str(
                    current.settings.section("evaluator").get("model_id", "")
                ),
                wait=False,
                prompt_tokens=4096,
                output_reserve_tokens=256,
            )

        evaluation = await current.evaluator.evaluate(
            effective_body,
            headers=header_values,
            api_kind=api_kind,
            prompt_tokens=prompt_tokens,
            current_task=(
                routing_conversation.task
                if routing_conversation
                else None
            ),
            current_route_profile=(
                routing_conversation.route_profile
                if routing_conversation
                else None
            ),
            current_complexity=(
                routing_conversation.complexity
                if routing_conversation
                else None
            ),
            is_new_conversation=(
                routing_conversation is None
                and requested_model == "auto"
            ),
            before_model_call=acquire_evaluator,
            after_model_call=lease.release_deployment,
        )
        if resolved_directive is not None:
            evaluation.directive_id = resolved_directive.id
            evaluation.directive_generation = (
                resolved_directive.generation
            )
            evaluation.required_endpoint_id = (
                resolved_directive.endpoint_id
            )
            evaluation.evidence = {
                **evaluation.evidence,
                "route_directive": resolved_directive.id,
            }
        modalities = request_modalities(effective_body, api_kind)
        image_inputs = inspect_image_inputs(effective_body)
        has_tools = required_capabilities.tools
        allow_compaction = _compaction_allowed(
            current,
            authenticated.policy.allow_compaction,
            request.headers.get("x-1panel-allow-compaction", ""),
        )
        excluded: set[str] = {
            endpoint.id
            for endpoint in current.registry.responders()
            if (
                requested_model == "auto"
                and image_inputs.remote
                and endpoint.backend_type == "ai_pool"
            )
        }
        pre_route_capsule = None
        if allow_compaction:
            (
                effective_body,
                prompt_tokens,
                pre_route_capsule,
            ) = await _maybe_compact_for_route(
                current,
                effective_body,
                api_kind=api_kind,
                request_id=request_id,
                requested_model=requested_model,
                evaluation=evaluation,
                prompt_tokens=prompt_tokens,
                output_reserve_tokens=reserve_tokens,
                modalities=modalities,
                image_count=image_inputs.total,
                has_tools=has_tools,
                required_capabilities=required_capabilities,
                conversation=routing_conversation,
                excluded_endpoints=excluded,
                identity=identity,
            )
        trace.set_request_context(
            prompt_tokens=requested_prompt_tokens,
            output_reserve_tokens=reserve_tokens,
            modalities=modalities,
            required_capabilities=list(required_capabilities.labels()),
        )
        trace.set_evaluation(evaluation)
        if pre_route_capsule is not None:
            routing_conversation = None
            trace.record(
                1,
                "context_compaction",
                "passed",
                reason="explicit_compaction",
                evidence={
                    "before_prompt_tokens": requested_prompt_tokens,
                    "after_prompt_tokens": prompt_tokens,
                    "requested_output_tokens": reserve_tokens,
                    "requested_context_tokens": (
                        requested_prompt_tokens + reserve_tokens
                    ),
                    "routed_context_tokens": (
                        prompt_tokens + reserve_tokens
                    ),
                },
                path=False,
            )
            trace.set_request_context(
                context_compacted=True,
                context_compaction_source="router",
            )
        await _save_request_trace(current, trace)
        signature_body = json.loads(json.dumps(effective_body))
        prefix_endpoints = (
            current.registry.by_public_model(requested_model)
            if requested_model != "auto"
            else ()
        )
        if (
            isinstance(signature_body.get("tools"), list)
            and prefix_endpoints
            and all(
                item.backend_type in {"llama_cpp", "ai_pool"}
                and item.metadata.get("prefix_affinity_enabled") is True
                for item in prefix_endpoints
            )
        ):
            signature_body["tools"] = normalize_llama_tool_schemas(
                signature_body["tools"],
                api_kind,
            )
        rendered_signature_body = identity.inject(
            signature_body,
            api_kind,
        )
        prefix_signature: PrefixSignature | None = None
        try:
            prefix_token_reader = getattr(
                current.token_counter,
                "prefix_token_ids",
                None,
            )
            prefix_token_ids = (
                prefix_token_reader(rendered_signature_body, api_kind)
                if callable(prefix_token_reader)
                else ()
            )
            prefix_signature = current.prefix_affinity.signature(
                rendered_signature_body,
                api_kind,
                client_id=authenticated.policy.id,
                requested_model=requested_model,
                token_ids=prefix_token_ids,
                modalities=modalities,
                new_conversation=(
                    lineage.parent is None
                    and not client_compacted
                    and pre_route_capsule is None
                ),
                context_revision=(
                    identity.revision if identity.enabled else ""
                ),
                legacy_body=effective_body,
            )
        except Exception as exc:
            current.audit.write(
                "prefix_signature_failed",
                request_id=request_id,
                client_id=authenticated.policy.id,
                requested_model=requested_model,
                error=type(exc).__name__,
            )
        prefix_affinity_key = (
            prefix_signature.exact_key
            if prefix_signature is not None
            else None
        )
        reusable_prefix_tokens = (
            prefix_signature.prefix_tokens
            if prefix_signature is not None
            else 0
        )
        prefix_affinity = await current.prefix_affinity.match(
            prefix_signature
        )
        template_capture_attempted = False
        excluded_deployments: set[str] = set()
        max_attempts = int(current.settings.section("failover").get("max_attempts", 2))
        # At this point no response bytes or tool calls reached the client.
        allow_retry = resolved_directive is None
        attempts = (
            1
            if resolved_directive is not None
            else max(max_attempts, 3)
            if requested_model == "auto"
            and (
                "image" in modalities
                or str(
                    current.settings.section("routing").get(
                        "strategy",
                        "legacy_v1",
                    )
                )
                == "intelligent_v2"
            )
            else max_attempts
        )
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
                    client_id=authenticated.policy.id,
                    conversation_control=conversation_control,
                    evaluation=evaluation,
                    prompt_tokens=prompt_tokens,
                    output_reserve_tokens=reserve_tokens,
                    requested_context_tokens=(
                        requested_prompt_tokens + reserve_tokens
                    ),
                    modalities=modalities,
                    image_count=image_inputs.total,
                    has_tools=has_tools,
                    required_capabilities=required_capabilities,
                    conversation=routing_conversation,
                    history_conversation=stored_conversation,
                    body=effective_body,
                    api_kind=api_kind,
                    lease=lease,
                    excluded_endpoints=excluded,
                    excluded_deployments=excluded_deployments,
                    capacity_attempts=total_capacity_attempts,
                    queue_wait_ms=total_queue_wait_ms,
                    prefix_affinity=prefix_affinity,
                    prefix_affinity_key=prefix_affinity_key,
                    trace=trace,
                    route_attempt=attempt,
                    identity=identity,
                    allow_compaction=allow_compaction,
                    history_precompacted=(
                        pre_route_capsule is not None
                    ),
                )
                capsule = capsule or pre_route_capsule
                compaction_source = (
                    "client"
                    if client_compacted
                    else (
                        "router"
                        if capsule is not None
                        else None
                    )
                )
                decision.context_compacted = bool(compaction_source)
                decision.context_compaction_source = compaction_source
                decision.prefix_affinity_signature = prefix_signature
                decision.prefix_affinity_prefix_tokens = (
                    reusable_prefix_tokens
                )
                decision.conversation_mode = conversation_mode
                decision.branch_id = lineage.branch_id
                decision.parent_branch_id = lineage.parent_branch_id
                decision.lineage_relation = lineage.relation
                decision.attempts = attempt
                decision.tool_history_repairs = tool_history_repairs
                decision.identity_revision = (
                    identity.revision if identity.enabled else None
                )
                decision.legacy_model_alias_used = bool(
                    identity.enabled
                    and client_requested_model
                    not in {"auto", identity.public_model_id}
                )
                if (
                    not template_capture_attempted
                    and decision.endpoint.metadata.get(
                        "prefix_affinity_enabled"
                    )
                    is True
                ):
                    template_capture_attempted = True
                    try:
                        template_path = (
                            await current.prefix_affinity.capture_template(
                                prefix_affinity_key,
                                effective_body,
                                api_kind,
                                client_id=authenticated.policy.id,
                                requested_model=requested_model,
                                prefix_tokens=reusable_prefix_tokens,
                            )
                        )
                        if template_path is not None:
                            current.audit.write(
                                "prefix_template_captured",
                                request_id=request_id,
                                client_id=authenticated.policy.id,
                                requested_model=requested_model,
                                prefix_tokens=reusable_prefix_tokens,
                                prefix_key=prefix_affinity_key,
                            )
                    except Exception as exc:
                        current.audit.write(
                            "prefix_template_capture_failed",
                            request_id=request_id,
                            client_id=authenticated.policy.id,
                            requested_model=requested_model,
                            error=type(exc).__name__,
                        )
                if current.training is not None:
                    await current.training.mark_routed(
                        training_token,
                        effective_body=effective_body,
                        routed_body=routed_body,
                        route={
                            "attempt": attempt,
                            "requested_model": decision.requested_model,
                            "selected_model": (
                                decision.endpoint.public_model
                            ),
                            "endpoint_id": decision.endpoint.id,
                            "deployment_id": (
                                decision.deployment_id
                                or decision.endpoint.id
                            ),
                            "deployment_profile_id": (
                                decision.deployment_profile_id
                            ),
                            "deployment_vision_status": (
                                decision.deployment_vision_status
                            ),
                            "image_resizes": decision.image_resizes,
                            "node": decision.endpoint.node,
                            "task": decision.task,
                            "reason": decision.reason,
                            "affinity": decision.affinity,
                            "prompt_tokens": decision.prompt_tokens,
                            "output_reserve_tokens": (
                                decision.output_reserve_tokens
                            ),
                            "requested_output_tokens": (
                                decision.output_reserve_tokens
                            ),
                            "context_required": (
                                decision.context_required
                            ),
                            "strategy_version": (
                                decision.strategy_version
                            ),
                            "route_profile": decision.route_profile,
                            "complexity": decision.complexity,
                            "history_mode": decision.history_mode,
                            "context_compacted": (
                                decision.context_compacted
                            ),
                            "context_compaction_source": (
                                decision.context_compaction_source
                            ),
                            "remote_fallback_position": (
                                decision.remote_fallback_position
                            ),
                            "required_capabilities": list(
                                decision.required_capabilities
                            ),
                            "identity_revision": (
                                identity.revision
                                if identity.enabled
                                else None
                            ),
                        },
                    )
                _audit_started(
                    current,
                    request_id=request_id,
                    client_id=authenticated.policy.id,
                    key_id=authenticated.key_id,
                    conversation_id=conversation_id,
                    decision=decision,
                )
                trace.record(
                    attempt,
                    "upstream_request",
                    "running",
                    reason="upstream_request_started",
                    evidence={
                        "endpoint_id": decision.endpoint.id,
                        "deployment_id": (
                            decision.deployment_id
                            or decision.endpoint.id
                        ),
                        "selected_model": (
                            decision.endpoint.public_model
                        ),
                    },
                )
                await _save_request_trace(current, trace)
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
                    identity=identity,
                )
                if (
                    attempt < attempts
                    and decision.endpoint.backend_type == "ai_pool"
                    and "image" in modalities
                    and upstream.status_code >= 400
                ):
                    vision_error_payload = await upstream.aread()
                    if _is_ai_vision_workspace_failure(
                        decision,
                        modalities,
                        upstream.status_code,
                        vision_error_payload,
                    ):
                        await upstream.aclose()
                        await current.budget.release(budget_reservation)
                        budget_reservation = None
                        await _exclude_failed_vision_profile(
                            current,
                            decision,
                            excluded,
                            excluded_deployments,
                        )
                        _record_trace_retry(
                            trace,
                            attempt=attempt,
                            status_code=upstream.status_code,
                            reason="vision_workspace_failure",
                            allowed=True,
                        )
                        await _save_request_trace(current, trace)
                        await lease.release_deployment()
                        continue
                legacy_subscription_fallback = bool(
                    str(
                        current.settings.section("routing").get(
                            "strategy",
                            "legacy_v1",
                        )
                    )
                    == "legacy_v1"
                    and requested_model == "auto"
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
                        or legacy_subscription_fallback
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
                    _record_trace_retry(
                        trace,
                        attempt=attempt,
                        status_code=upstream.status_code,
                        reason=(
                            "legacy_subscription_fallback"
                            if legacy_subscription_fallback
                            else "retryable_upstream_status"
                        ),
                        allowed=True,
                    )
                    await _save_request_trace(current, trace)
                    await lease.release_deployment()
                    continue

                identifiers = internal_identifiers(
                    current.registry,
                    decision,
                )
                headers = _response_headers(
                    upstream,
                    decision,
                    request_id,
                    conversation_id=conversation_id,
                    conversation_mode=conversation_mode,
                    identity=identity,
                )
                if upstream.status_code >= 400:
                    payload = await upstream.aread()
                    await upstream.aclose()
                    await current.budget.release(budget_reservation)
                    budget_reservation = None
                    if identity.enabled:
                        public_payload = json.dumps(
                            {
                                "error": {
                                    "message": (
                                        "the model request could not be "
                                        "completed"
                                    ),
                                    "type": (
                                        "invalid_request_error"
                                        if upstream.status_code < 500
                                        else "server_error"
                                    ),
                                    "code": "model_request_failed",
                                }
                            },
                            separators=(",", ":"),
                        ).encode("utf-8")
                        redactions = 1
                    else:
                        public_payload = payload
                        redactions = 0
                    decision.response_redactions = redactions
                    await _audit(
                        current,
                        request_id=request_id,
                        client_id=authenticated.policy.id,
                        key_id=authenticated.key_id,
                        conversation_id=conversation_id,
                        decision=decision,
                        status_code=upstream.status_code,
                        started_at=request.state.started_at,
                        cache_snapshot=cache_snapshot,
                    )
                    if current.training is not None:
                        await current.training.fail(
                            training_token,
                            status_code=upstream.status_code,
                            error={
                                "type": "upstream_error",
                                "endpoint_id": decision.endpoint.id,
                            },
                            response_payload=payload,
                        )
                    return Response(
                        content=public_payload,
                        status_code=upstream.status_code,
                        headers=headers,
                        media_type=(
                            "application/json"
                            if identity.enabled
                            else upstream.headers.get("content-type")
                        ),
                    )

                current.prepare_overflow_prefix(
                    decision, routed_body, client_id=authenticated.policy.id,
                    request_id=request_id, api_kind=api_kind,
                )
                await current.budget.commit(budget_reservation)
                budget_reservation = None
                state = await _save_conversation(
                    current,
                    lineage=lineage,
                    previous=(
                        None
                        if decision.context_compacted
                        else stored_conversation
                    ),
                    decision=decision,
                    capsule=capsule,
                )
                if bool(routed_body.get("stream")):
                    resource_finalizer = _StreamResourceFinalizer(
                        current,
                        lease,
                        authenticated.policy.id,
                    )
                    response = _FinalizingStreamingResponse(
                        _stream_response(
                            current,
                            upstream,
                            resource_finalizer=resource_finalizer,
                            client_id=authenticated.policy.id,
                            key_id=authenticated.key_id,
                            request_id=request_id,
                            conversation_id=conversation_id,
                            decision=decision,
                            state=state,
                            body=routed_body,
                            api_kind=api_kind,
                            training_token=training_token,
                            started_at=request.state.started_at,
                            cache_snapshot=cache_snapshot,
                            identity=identity,
                            identifiers=identifiers,
                        ),
                        status_code=upstream.status_code,
                        headers=headers,
                        media_type=upstream.headers.get("content-type"),
                        background=BackgroundTask(
                            _finalize_abandoned_stream,
                            current,
                            resource_finalizer=resource_finalizer,
                            client_id=authenticated.policy.id,
                            key_id=authenticated.key_id,
                            request_id=request_id,
                            conversation_id=conversation_id,
                            decision=decision,
                            training_token=training_token,
                            started_at=request.state.started_at,
                            cache_snapshot=cache_snapshot,
                        ),
                    )
                    stream_owned = True
                    return response

                payload = await upstream.aread()
                await upstream.aclose()
                raw_usage = usage_dict(payload)
                if (
                    api_kind == "responses"
                    and decision.native_or_adapter == "adapter"
                ):
                    payload = chat_response_to_responses(
                        payload,
                        model=decision.endpoint.public_model,
                    )
                await _map_response_id(current, payload, state)
                public_payload, redactions = sanitize_payload(
                    payload,
                    identity,
                    identifiers,
                )
                decision.response_redactions = redactions
                await persist_history(
                    current.compactor,
                    current.conversations,
                    state=state,
                    client_id=authenticated.policy.id,
                    body=routed_body,
                    api_kind=api_kind,
                    assistant_items=private_history_items(
                        assistant_items_from_response(public_payload, api_kind),
                        assistant_items_from_response(payload, api_kind),
                    ),
                )
                if current.training is not None:
                    await current.training.complete(
                        training_token,
                        status_code=upstream.status_code,
                        response_payload=payload,
                    )
                await _audit(
                    current,
                    request_id=request_id,
                    client_id=authenticated.policy.id,
                    key_id=authenticated.key_id,
                    conversation_id=conversation_id,
                    decision=decision,
                    status_code=upstream.status_code,
                    started_at=request.state.started_at,
                    response_payload=payload,
                    usage=raw_usage,
                    cache_snapshot=cache_snapshot,
                )
                return Response(
                    content=public_payload,
                    status_code=upstream.status_code,
                    headers=headers,
                    media_type=upstream.headers.get("content-type"),
                )
            except (
                httpx.RequestError,
                HistoryMigrationRequiredError,
                NoCompatibleModelError,
                NoEligibleModelError,
            ) as exc:
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
                legacy_subscription_fallback = bool(
                    str(
                        current.settings.section("routing").get(
                            "strategy",
                            "legacy_v1",
                        )
                    )
                    == "legacy_v1"
                    and requested_model == "auto"
                    and decision is not None
                    and decision.endpoint.metadata.get("billing_mode")
                    == "subscription"
                )
                non_retryable_route_error = isinstance(
                    exc,
                    (
                        HistoryMigrationRequiredError,
                        NoCompatibleModelError,
                    ),
                )
                if (
                    attempt >= attempts
                    or non_retryable_route_error
                    or not (
                        allow_retry
                        or legacy_subscription_fallback
                    )
                ):
                    _record_trace_retry(
                        trace,
                        attempt=attempt,
                        status_code=last_error.status_code,
                        reason=last_error.code,
                        allowed=False,
                        upstream=isinstance(exc, httpx.RequestError),
                    )
                    await _save_request_trace(current, trace)
                    if decision is not None:
                        await _audit(
                            current,
                            request_id=request_id,
                            client_id=authenticated.policy.id,
                            key_id=authenticated.key_id,
                            conversation_id=conversation_id,
                            decision=decision,
                            status_code=last_error.status_code,
                            started_at=request.state.started_at,
                        )
                    raise last_error
                _record_trace_retry(
                    trace,
                    attempt=attempt,
                    status_code=last_error.status_code,
                    reason=last_error.code,
                    allowed=True,
                    upstream=isinstance(exc, httpx.RequestError),
                )
                await _save_request_trace(current, trace)
            except Exception:
                await current.budget.release(budget_reservation)
                raise

        raise last_error or NoEligibleModelError()
    except BaseException as exc:
        await _fail_training_record(
            current,
            training_token,
            exc,
        )
        await _finish_trace_exception(current, trace, exc)
        raise
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


async def _identity_intercept_response(
    current: RouterRuntime,
    *,
    body: dict[str, Any],
    api_kind: str,
    request_id: str,
    client_id: str,
    key_id: str,
    conversation_id: str,
    identity: IdentityProfile,
    trace: DecisionTrace,
    lineage: LineageContext,
    rpm_limit: int,
    tpm_limit: int,
    max_parallel_requests: int,
) -> Response:
    owner_token = f"identity:{request_id}"
    parallel_acquired = False
    request_tracked = False
    input_tokens = 0
    output_tokens = max(1, len(identity.identity_response) // 4)
    try:
        await current.track_request_started(
            owner_token,
            request_id,
            conversation_id,
        )
        request_tracked = True
        parallel_acquired = await current.limiter.acquire_parallel(
            client_id,
            owner_token,
            max_parallel_requests,
        )
        if not parallel_acquired:
            raise RouterError(
                "client parallel request limit exceeded",
                status_code=429,
                code="parallel_limit_exceeded",
            )

        effective_body = json.loads(json.dumps(body))
        if api_kind == "responses" and lineage.parent is not None:
            effective_body = await apply_stored_history(
                current.compactor,
                current.conversations,
                effective_body,
                api_kind=api_kind,
                conversation=lineage.parent,
            )
        effective_body = normalize_request(
            effective_body,
            api_kind,
        ).body
        input_tokens = current.token_counter.count_request(
            identity.inject(effective_body, api_kind),
            api_kind,
        )
        allowed, limit_code = await current.limiter.check_rate_limits(
            client_id,
            prompt_tokens=input_tokens,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
        )
        if not allowed:
            raise RouterError(
                limit_code or "rate limit exceeded",
                status_code=429,
                code=limit_code or "rate_limit_exceeded",
            )
        if identity_disclosure_requires_model_protocol(body, api_kind):
            raise RouterError(
                "the requested output format is not available",
                status_code=400,
                code="identity_disclosure_not_available",
            )

        if trace.payload.get("disclosure_mode") == "public":
            current.review_privacy(
                effective_body, api_kind, request_id=request_id, client_id=client_id,
            )
        headers = {
            "Cache-Control": "no-store",
            "X-Request-ID": request_id,
            "X-1Panel-Public-Model": identity.public_model_id,
            "X-1Panel-Conversation-ID": conversation_id,
        }
        payload = _identity_response_payload(
            api_kind,
            request_id=request_id,
            identity=identity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        response_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response: Response
        if not bool(body.get("stream")):
            response = JSONResponse(payload, headers=headers)
        else:
            response = StreamingResponse(
                _identity_stream(
                    api_kind,
                    payload,
                    identity.identity_response,
                ),
                headers=headers,
                media_type="text/event-stream",
            )
        state = (
            replace(
                lineage.parent,
                conversation_id=lineage.lineage_id,
                branch_id=lineage.branch_id,
                parent_branch_id=lineage.parent_branch_id,
                lineage_relation=lineage.relation,
                public_model=identity.public_model_id,
                task="identity",
                last_seen=time.time(),
                identity_only=True,
            )
            if lineage.parent is not None
            else ConversationState(
                conversation_id=lineage.lineage_id,
                branch_id=lineage.branch_id,
                parent_branch_id=lineage.parent_branch_id,
                lineage_relation=lineage.relation,
                public_model=identity.public_model_id,
                endpoint_id="",
                tier_rank=0,
                task="identity",
                last_seen=time.time(),
                identity_only=True,
            )
        )
        await persist_history(
            current.compactor,
            current.conversations,
            state=state,
            client_id=client_id,
            body=effective_body,
            api_kind=api_kind,
            response_payload=response_payload,
        )
        await _map_response_id(current, response_payload, state)
        current.audit.write(
            "identity_intercepted",
            request_id=request_id,
            client_request_id=trace.payload.get("client_request_id"),
            client_id=client_id,
            key_id=key_id,
            conversation_id=conversation_id,
            disclosure_mode="public",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            instance_id=current.instance_id,
            boot_id=current.boot_id,
        )
        trace.finish_identity_intercept(
            status_code=200,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        await _save_request_trace(current, trace)
        await _record_client_usage(
            current,
            client_id=client_id,
            key_id=key_id,
            request_id=request_id,
            status_code=200,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return response
    except BaseException as exc:
        await _finish_trace_exception(current, trace, exc)
        await _record_client_usage(
            current,
            client_id=client_id,
            key_id=key_id,
            request_id=request_id,
            status_code=(
                499
                if isinstance(exc, asyncio.CancelledError)
                else int(getattr(exc, "status_code", 500))
            ),
            input_tokens=input_tokens,
            output_tokens=0,
        )
        raise
    finally:
        if parallel_acquired:
            await current.limiter.release_parallel(
                client_id,
                owner_token,
            )
        if request_tracked:
            await current.track_request_finished(owner_token)


def _identity_response_payload(
    api_kind: str,
    *,
    request_id: str,
    identity: IdentityProfile,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    created = int(time.time())
    if api_kind == "chat":
        return {
            "id": f"chatcmpl-{request_id}",
            "object": "chat.completion",
            "created": created,
            "model": identity.public_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": identity.identity_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
    response_id = f"resp_{request_id}"
    message = {
        "id": f"msg_{request_id}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": identity.identity_response,
                "annotations": [],
            }
        ],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": identity.public_model_id,
        "output": [message],
        "output_text": identity.identity_response,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


async def _identity_stream(
    api_kind: str,
    payload: dict[str, Any],
    text: str,
) -> AsyncIterator[bytes]:
    if api_kind == "chat":
        base = {
            "id": payload["id"],
            "object": "chat.completion.chunk",
            "created": payload["created"],
            "model": payload["model"],
        }
        chunks = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
    else:
        response = {
            key: value
            for key, value in payload.items()
            if key not in {"output", "output_text"}
        }
        output = payload["output"][0]
        part = output["content"][0]
        chunks = [
            {"type": "response.created", "response": response},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    **output,
                    "status": "in_progress",
                    "content": [],
                },
            },
            {
                "type": "response.content_part.added",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {**part, "text": ""},
            },
            {
                "type": "response.output_text.delta",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
            {
                "type": "response.output_text.done",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": output,
            },
            {"type": "response.completed", "response": payload},
        ]
    for chunk in chunks:
        yield (
            "data: "
            + json.dumps(
                chunk,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n"
        ).encode("utf-8")
    yield b"data: [DONE]\n\n"


async def _acquire_route_capacity(
    current: RouterRuntime,
    *,
    request_id: str,
    requested_model: str,
    evaluation: Any,
    prompt_tokens: int,
    output_reserve_tokens: int,
    requested_context_tokens: int | None = None,
    modalities: set[str],
    image_count: int = 0,
    has_tools: bool,
    required_capabilities: Any,
    conversation: ConversationState | None,
    history_conversation: ConversationState | None = None,
    body: dict[str, Any],
    api_kind: str,
    lease: Any,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
    capacity_attempts: int,
    queue_wait_ms: float,
    prefix_affinity: PrefixAffinityRecord | None = None,
    prefix_affinity_key: str | None = None,
    trace: DecisionTrace | None = None,
    route_attempt: int = 1,
    identity: IdentityProfile | None = None,
    allow_compaction: bool = False,
    history_precompacted: bool = False,
    client_id: str = "",
    conversation_control: dict[str, Any] | None = None,
) -> tuple[RouteDecision, dict[str, Any], Any | None, Any | None, int, float]:
    identity = identity or IdentityProfile.from_settings(
        current.settings.section("identity")
    )
    if history_conversation is None:
        history_conversation = conversation
    capacity_busy_seen = False
    affinity_spilled = False
    history_incompatible_seen = False
    carried_capsule = None
    routing = current.settings.section("routing")
    pool_wait_deadlines = {}
    pool = getattr(getattr(current, "policy", None), "local_pool", None)

    while True:
        if pool:
            await pool.release(request_id, trace)
        try:
            decision = await current.policy.choose(
                requested_model=requested_model,
                evaluation=evaluation,
                prompt_tokens=prompt_tokens,
                output_reserve_tokens=output_reserve_tokens,
                requested_context_tokens=requested_context_tokens,
                modalities=modalities,
                image_count=image_count,
                has_tools=has_tools,
                required_capabilities=required_capabilities,
                conversation=conversation,
                excluded_endpoint_ids=excluded_endpoints,
                excluded_deployment_ids=excluded_deployments,
                prefix_affinity=prefix_affinity,
                prefix_affinity_key=prefix_affinity_key,
                routing_key=prefix_affinity_key or request_id,
                client_id=client_id,
                conversation_control=conversation_control,
                trace=trace,
                trace_attempt=route_attempt,
            )
        except (NoCompatibleModelError, NoEligibleModelError):
            if trace:
                await _save_request_trace(current, trace)
            if history_incompatible_seen:
                raise HistoryMigrationRequiredError()
            if capacity_busy_seen:
                if requested_model == "auto":
                    raise AllLocalCapacityBusyError()
                raise CapacityBusyError()
            raise
        if (
            prefix_affinity_key
            and decision.endpoint.metadata.get(
                "prefix_affinity_enabled"
            )
            is True
        ):
            decision.prefix_affinity_key = prefix_affinity_key

        _apply_protocol_constraints(decision, api_kind)
        capacity_attempts += 1
        if trace:
            trace.record(
                route_attempt,
                "capacity_check",
                "running",
                reason="capacity_check",
                evidence={
                    "endpoint_id": decision.endpoint.id,
                    "deployment_id": (
                        decision.deployment_id
                        or decision.endpoint.id
                    ),
                },
            )
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
                "prefix-hit",
                "prefix-replica",
            }
            if trace:
                trace.record(
                    route_attempt,
                    "capacity_check",
                    "failed",
                    reason="deployment_draining_after_restart",
                    evidence={
                        "endpoint_id": decision.endpoint.id,
                    },
                )
                trace.record(
                    route_attempt,
                    "retry_decision",
                    (
                        "passed"
                        if requested_model == "auto"
                        else "failed"
                    ),
                    reason="deployment_draining_after_restart",
                    evidence={
                        "allowed": requested_model == "auto",
                    },
                )
                await _save_request_trace(current, trace)
            if requested_model != "auto":
                raise CapacityBusyError()
            continue
        initial_deployment = decision.deployment_id or decision.endpoint.id
        deployment_routes = dict(decision.deployment_candidates)
        deployment_ids = tuple(deployment_routes) or (initial_deployment,)
        if trace:
            trace.record(
                route_attempt,
                "capacity_check",
                "running",
                reason="capacity_check",
                evidence={
                    "endpoint_id": decision.endpoint.id,
                    "deployment_ids": list(deployment_ids),
                    "wait_seconds": _capacity_wait_seconds(
                        routing,
                        requested_model=requested_model,
                        decision=decision,
                    ),
                },
            )

        if (
            decision.endpoint.cloud
            and capacity_busy_seen
            and routing.get("all_local_busy_policy") != "cloud_or_429"
        ):
            if trace:
                trace.record(
                    route_attempt,
                    "capacity_check",
                    "failed",
                    reason="cloud_fallback_disabled",
                    evidence={"endpoint_id": decision.endpoint.id},
                )
                trace.record(
                    route_attempt,
                    "retry_decision",
                    "failed",
                    reason="all_local_busy_policy",
                    evidence={"allowed": False},
                )
                await _save_request_trace(current, trace)
            raise AllLocalCapacityBusyError()

        wait_seconds = _capacity_wait_seconds(
            routing,
            requested_model=requested_model,
            decision=decision,
        )
        pool = getattr(getattr(current, "policy", None), "local_pool", None)
        adaptive_wait = bool(pool and pool.member(decision.endpoint) and requested_model == "auto"
                             and not evaluation.required_endpoint_id and decision.affinity != "admin-pin"
                             and conversation is not None and wait_seconds > 0)
        if adaptive_wait:
            deadline = pool_wait_deadlines.setdefault("request", time.monotonic() + wait_seconds)
            wait_seconds = min(float(pool.config.get("recheck_seconds", 5)), max(0, deadline - time.monotonic()))
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
                            in {
                                "hit",
                                "logical-hit",
                                "prefix-hit",
                                "prefix-replica",
                                "admin-pin",
                            },
                            capacity=decision.endpoint.max_concurrency,
                        )
                    )
                except QueueTimeoutError as exc:
                    if adaptive_wait and time.monotonic() < pool_wait_deadlines["request"]:
                        queue_wait_ms += (time.monotonic() - wait_started) * 1000
                        if trace:
                            trace.payload.setdefault("local_pool", {})["waited_ms"] = round(queue_wait_ms, 2)
                            await _save_request_trace(current, trace)
                        continue
                    raise CapacityBusyError() from exc
            backend_wait_seconds = max(
                0.0,
                wait_seconds - (time.monotonic() - wait_started),
            )
            if not await _wait_for_selected_deployment(
                current,
                decision,
                selected_deployment,
                timeout_seconds=backend_wait_seconds,
            ):
                raise CapacityBusyError()
        except CapacityBusyError:
            if pool and pool.member(decision.endpoint):
                await pool.release(request_id, trace)
            queue_wait_ms += (time.monotonic() - wait_started) * 1000
            if adaptive_wait and time.monotonic() < pool_wait_deadlines["request"]:
                await lease.release_deployment()
                if pool:
                    await pool.release(request_id, trace)
                continue
            if adaptive_wait and trace:
                trace.payload.setdefault("local_pool", {}).update(
                    selection="capacity_timeout_cold_fallback", wait_budget_seconds=float(routing.get("affinity_capacity_wait_seconds", 120)))
            capacity_busy_seen = True
            affinity_spilled = affinity_spilled or decision.affinity in {
                "hit",
                "logical-hit",
                "prefix-hit",
                "prefix-replica",
            }
            _exclude_busy_decision(
                decision,
                excluded_endpoints,
                excluded_deployments,
            )
            if trace:
                trace.record(
                    route_attempt,
                    "capacity_check",
                    "failed",
                    reason="capacity_busy",
                    evidence={
                        "endpoint_id": decision.endpoint.id,
                        "deployment_ids": list(deployment_ids),
                        "queue_wait_ms": round(queue_wait_ms, 2),
                    },
                )
                trace.record(
                    route_attempt,
                    "retry_decision",
                    (
                        "passed"
                        if (
                            requested_model == "auto"
                            and decision.affinity != "admin-pin"
                        )
                        else "failed"
                    ),
                    reason="capacity_spillover",
                    evidence={
                        "allowed": (
                            requested_model == "auto"
                            and decision.affinity != "admin-pin"
                        ),
                        "excluded_endpoint_ids": sorted(
                            excluded_endpoints
                        ),
                        "excluded_deployment_ids": sorted(
                            excluded_deployments
                        ),
                    },
                )
                await _save_request_trace(current, trace)
            await lease.release_deployment()
            if pool:
                await pool.release(request_id, trace)
            if decision.affinity == "admin-pin":
                raise
            if requested_model != "auto":
                raise
            continue

        queue_wait_ms += (time.monotonic() - wait_started) * 1000
        if trace:
            trace.record(
                route_attempt,
                "capacity_check",
                "selected",
                reason="capacity_acquired",
                evidence={
                    "endpoint_id": decision.endpoint.id,
                    "deployment_id": selected_deployment,
                    "queue_wait_ms": round(queue_wait_ms, 2),
                },
            )
        decision.deployment_id = selected_deployment
        if deployment_routes:
            decision.upstream_api_base = deployment_routes[
                selected_deployment
            ]
        _apply_selected_deployment(decision, selected_deployment)
        if pool and pool.member(decision.endpoint) and trace:
            status = await current.health.status(decision.endpoint)
            trace.payload.setdefault("local_pool", {})["generation"] = status.cache_generation
            await pool.start(decision, trace, conversation)

        budget_reservation = None
        if trace:
            trace.record(
                route_attempt,
                "history_preflight",
                "running",
                reason="history_preflight",
                evidence={
                    "endpoint_id": decision.endpoint.id,
                    "provider": provider_family(decision.endpoint),
                    "allow_compaction": allow_compaction,
                },
            )
            trace.record(
                route_attempt,
                "request_prepare",
                "running",
                reason="request_prepare",
                evidence={
                    "protocol": api_kind,
                    "endpoint_id": decision.endpoint.id,
                },
            )
        try:
            routed_body, capsule = await _prepare_routed_body(
                current,
                body,
                api_kind=api_kind,
                decision=decision,
                request_id=request_id,
                identity=identity,
                conversation=history_conversation,
                allow_compaction=allow_compaction,
                history_precompacted=history_precompacted,
            )
        except HistoryMigrationRequiredError as exc:
            if trace:
                trace.record(
                    route_attempt,
                    "history_preflight",
                    "failed",
                    reason=exc.code,
                    evidence={
                        "endpoint_id": decision.endpoint.id,
                        "provider": provider_family(decision.endpoint),
                        "allow_compaction": allow_compaction,
                    },
                )
                trace.record(
                    route_attempt,
                    "retry_decision",
                    (
                        "passed"
                        if requested_model == "auto"
                        else "failed"
                    ),
                    reason=exc.code,
                    evidence={"allowed": requested_model == "auto"},
                )
                await _save_request_trace(current, trace)
            await lease.release_deployment()
            if pool:
                await pool.release(request_id, trace)
            if requested_model != "auto":
                raise
            history_incompatible_seen = True
            excluded_endpoints.add(decision.endpoint.id)
            continue
        except RouterError as exc:
            if trace:
                trace.record(
                    route_attempt,
                    "request_prepare",
                    "error",
                    reason=exc.code,
                    evidence={"message": str(exc)[:500]},
                )
                await _save_request_trace(current, trace)
            await lease.release_deployment()
            if pool:
                await pool.release(request_id, trace)
            raise
        if (
            capsule is not None
            and conversation is not None
            and not history_precompacted
        ):
            carried_capsule = capsule
            prompt_tokens = decision.prompt_tokens
            if "tools" in body and routed_body.get("tools") != body["tools"]:
                # Re-selection must start with backend-neutral tool definitions.
                routed_body = {**routed_body, "tools": body["tools"]}
                prompt_tokens = current.token_counter.count_request(
                    identity.inject(routed_body, api_kind),
                    api_kind,
                )
            body = routed_body
            requested_context_tokens = prompt_tokens + output_reserve_tokens
            conversation = None
            history_conversation = None
            history_precompacted = True
            prefix_affinity = None
            prefix_affinity_key = None
            await lease.release_deployment()
            if pool:
                await pool.release(request_id, trace)
            continue
        capsule = capsule or carried_capsule
        if trace:
            trace.record(
                route_attempt,
                "history_preflight",
                "passed",
                reason=decision.history_mode,
                evidence={
                    "history_mode": decision.history_mode,
                    "provider": provider_family(decision.endpoint),
                },
                path=False,
            )
            trace.record(
                route_attempt,
                "request_prepare",
                "passed",
                reason="request_prepared",
                evidence={
                    "protocol": api_kind,
                    "image_resizes": decision.image_resizes,
                    "compacted": capsule is not None,
                    "history_mode": decision.history_mode,
                },
            )
            trace.record(
                route_attempt,
                "budget_check",
                "running",
                reason="budget_check",
                evidence={
                    "cloud": decision.endpoint.cloud,
                    "endpoint_id": decision.endpoint.id,
                },
            )
        try:
            budget_reservation = await current.budget.reserve(
                decision.endpoint,
                request_id=request_id,
                prompt_tokens=decision.prompt_tokens,
                output_reserve_tokens=decision.output_reserve_tokens,
            )
        except RouterError as exc:
            if trace:
                trace.record(
                    route_attempt,
                    "budget_check",
                    "failed",
                    reason=exc.code,
                    evidence={"message": str(exc)[:500]},
                )
                await _save_request_trace(current, trace)
            await current.budget.release(budget_reservation)
            await lease.release_deployment()
            if pool:
                await pool.release(request_id, trace)
            if decision.endpoint.cloud and capacity_busy_seen:
                raise AllLocalCapacityBusyError()
            raise
        if trace:
            trace.record(
                route_attempt,
                "budget_check",
                "passed",
                reason=(
                    "cloud_budget_reserved"
                    if decision.endpoint.cloud
                    else "local_no_budget_required"
                ),
                evidence={
                    "cloud": decision.endpoint.cloud,
                    "reservation": budget_reservation is not None,
                },
            )

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
        if trace:
            trace.set_selection(
                attempt=route_attempt,
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
            trace.confirm_selection(attempt=route_attempt)
            await _save_request_trace(current, trace)

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
    pin = decision.endpoint.metadata.get("client_deployment_pin")
    if pin is not None:
        return float(pin["capacity_wait_seconds"])
    if requested_model != "auto" or decision.reason == "local_pool_faster_first_output" or decision.affinity in {
        "hit",
        "logical-hit",
        "prefix-hit",
        "prefix-replica",
        "admin-pin",
    }:
        return max(
            0.0,
            float(routing.get("affinity_capacity_wait_seconds", 3)),
        )
    return max(
        0.0,
        float(routing.get("new_request_capacity_wait_seconds", 0)),
    )


async def _wait_for_selected_deployment(
    current: RouterRuntime,
    decision: RouteDecision,
    deployment_id: str,
    *,
    timeout_seconds: float,
) -> bool:
    pool = getattr(getattr(current, "policy", None), "local_pool", None)
    direct_pool = bool(pool and pool.member(decision.endpoint))
    if decision.endpoint.backend_type != "ai_pool" and not direct_pool:
        return True
    deadline = time.monotonic() + timeout_seconds
    while True:
        probe = current.health.status(decision.endpoint, force_refresh=True)
        try:
            status = (await asyncio.wait_for(probe, max(0, deadline - time.monotonic()))
                      if timeout_seconds > 0 else await probe)
        except asyncio.TimeoutError:
            return False
        worker = next(
            (
                item
                for item in status.detail.get("workers", [])
                if item.get("worker_id") == deployment_id
            ),
            None,
        )
        if direct_pool:
            if status.healthy and status.load_headroom > 0:
                return True
        elif worker and worker.get("state") == "available":
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(1.0, remaining))


def _exclude_busy_decision(
    decision: RouteDecision,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
) -> None:
    if (
        decision.endpoint.backend_type in {"ai_pool", "codex_pool"}
        and decision.affinity
        in {
            "hit",
            "logical-hit",
            "prefix-hit",
            "prefix-replica",
            "prefix-reset",
            "prefix-miss",
            "physical-failover",
        }
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
        _apply_selected_deployment(decision, filtered[0][0])
        if decision.affinity in {
            "hit",
            "logical-hit",
            "prefix-hit",
            "prefix-replica",
            "prefix-reset",
        }:
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


def _apply_selected_deployment(
    decision: RouteDecision,
    deployment_id: str,
) -> None:
    value = decision.deployment_details.get(deployment_id)
    if not value:
        return
    try:
        deployment = PhysicalDeployment.from_dict(value)
    except (KeyError, TypeError, ValueError):
        return
    decision.deployment_profile_id = deployment.profile_id
    decision.deployment_modalities = deployment.modalities
    decision.deployment_vision_status = deployment.vision_status
    decision.deployment_safe_context_tokens = (
        deployment.safe_context_tokens
    )
    decision.deployment_max_images = deployment.max_images


async def _send_upstream(
    current: RouterRuntime,
    request: Request,
    body: dict[str, Any],
    *,
    api_kind: str,
    decision: RouteDecision,
    identity: IdentityProfile,
) -> httpx.Response:
    payload = identity.inject(body, api_kind)
    responses_adapter = (
        api_kind == "responses"
        and decision.native_or_adapter == "adapter"
    )
    if responses_adapter:
        payload = responses_request_to_chat(payload)
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
    if (
        api_kind == "responses"
        and not responses_adapter
        and not decision.endpoint.cloud
    ):
        _mirror_responses_format(payload)
    payload["model"] = (
        decision.endpoint.provider_model if direct else decision.endpoint.id
    )
    if api_kind == "responses" and not responses_adapter:
        payload.pop("conversation", None)
        payload.pop("previous_response_id", None)
    base_url = (
        decision.upstream_api_base.rstrip("/")
        if decision.upstream_api_base
        else f"{current.internal_base_url}/v1"
    )
    upstream_api_kind = "chat" if responses_adapter else api_kind
    url = (
        f"{base_url}/"
        f"{'chat/completions' if upstream_api_kind == 'chat' else 'responses'}"
    )
    api_key = current.internal_api_key
    if direct:
        deployment = getattr(
            decision,
            "deployment_details",
            {},
        ).get(
            getattr(decision, "deployment_id", "") or "",
            {},
        )
        key_env = str(
            deployment.get("backend_api_key_env")
            or decision.endpoint.backend_api_key_env
        )
        api_key = os.environ.get(
            key_env,
            "",
        )
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": getattr(
            request.state,
            "server_request_id",
            uuid4().hex,
        ),
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
    timeout = current.internal_client.timeout
    read_timeout = decision.endpoint.metadata.get("upstream_read_timeout_seconds")
    if read_timeout is not None:
        timeout = httpx.Timeout(
            connect=timeout.connect,
            read=float(read_timeout),
            write=timeout.write,
            pool=timeout.pool,
        )
    operation_id = uuid4().hex
    attempt = int(getattr(decision, "attempts", 1) or 1)
    headers["X-1Panel-Operation-ID"] = operation_id
    headers["X-1Panel-Attempt"] = str(attempt)
    headers["X-1Panel-Operation-Kind"] = "foreground"
    internal_usage = False
    if (decision.endpoint.metadata.get("cache_usage") == "per_request"
            and payload.get("stream") and (api_kind == "chat" or responses_adapter)):
        options = payload.get("stream_options")
        if options is None or isinstance(options, dict):
            internal_usage = api_kind == "chat" and not bool((options or {}).get("include_usage"))
            payload["stream_options"] = {**(options or {}), "include_usage": True}
    observation = getattr(request.state, "content_observation", None)
    if observation:
        observation.capture("forwarded_" + str(attempt), payload)
        if decision.trace:
            decision.trace.payload.setdefault("observation", {})["content"] = observation.metadata()
            decision.trace.payload["observation"]["queue_wait_ms"] = decision.queue_wait_ms
            await _save_request_trace(current, decision.trace)
        if current.training:
            await current.training.record_pipeline(getattr(request.state, "training_token", None), observation.archive())
    upstream_request = current.internal_client.build_request(
        "POST",
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    response = await current.internal_client.send(upstream_request, stream=True)
    response.extensions["internal_cache_usage"] = internal_usage
    if direct and response.headers.get("X-Prefix-Telemetry") == "1":
        if not getattr(current, "cache_collector", None):
            current.cache_collector = TelemetryCollector(current)
        current.cache_collector.collect(base_url=base_url, key=api_key,
            request_id=headers["X-Request-ID"], operation_id=operation_id,
            attempt=attempt, deployment_id=decision.deployment_id or decision.endpoint.id)
    return response


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


def _is_ai_vision_workspace_failure(
    decision: RouteDecision,
    modalities: set[str],
    status_code: int,
    payload: bytes,
) -> bool:
    if (
        decision.endpoint.backend_type != "ai_pool"
        or "image" not in modalities
        or status_code < 400
    ):
        return False
    lowered = payload.lower()
    return any(
        marker in lowered
        for marker in (
            b"failed to find a memory slot",
            b"mtmd",
            b"decode workspace",
            b"batch of size",
        )
    )


async def _exclude_failed_vision_profile(
    current: RouterRuntime,
    decision: RouteDecision,
    excluded_endpoints: set[str],
    excluded_deployments: set[str],
) -> None:
    deployment_id = decision.deployment_id
    profile_id = decision.deployment_profile_id
    if not deployment_id:
        excluded_endpoints.add(decision.endpoint.id)
        return
    cooldown = int(
        current.settings.section("failover").get("cooldown_seconds", 20)
    )
    await current.health.mark_capability_failure(
        deployment_id,
        "image",
        cooldown,
    )
    status = await current.health.status(
        decision.endpoint,
        force_refresh=True,
    )
    deployments: list[PhysicalDeployment] = []
    for value in status.detail.get("workers", []):
        try:
            deployments.append(PhysicalDeployment.from_dict(value))
        except (KeyError, TypeError, ValueError):
            continue
    matching = {
        item.worker_id
        for item in deployments
        if item.profile_id == profile_id
    }
    excluded_deployments.update(matching or {deployment_id})
    remaining = [
        item
        for item in deployments
        if (
            item.schedulable
            and item.worker_id not in excluded_deployments
            and "image" in item.modalities
        )
    ]
    if not remaining:
        excluded_endpoints.add(decision.endpoint.id)
    current.audit.write(
        "vision_deployment_failed",
        endpoint_id=decision.endpoint.id,
        deployment_id=deployment_id,
        deployment_profile_id=profile_id,
        excluded_profile_deployments=sorted(matching),
        fallback_available=bool(remaining),
    )


async def _maybe_compact_for_route(
    current: RouterRuntime,
    body: dict[str, Any],
    *,
    api_kind: str,
    request_id: str,
    requested_model: str,
    evaluation: Evaluation,
    prompt_tokens: int,
    output_reserve_tokens: int,
    modalities: set[str],
    image_count: int,
    has_tools: bool,
    required_capabilities: RequestCapabilities,
    conversation: ConversationState | None,
    excluded_endpoints: set[str],
    identity: IdentityProfile,
) -> tuple[dict[str, Any], int, Any | None]:
    try:
        await current.policy.choose(
            requested_model=requested_model,
            evaluation=evaluation,
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=output_reserve_tokens,
            modalities=modalities,
            image_count=image_count,
            has_tools=has_tools,
            required_capabilities=required_capabilities,
            conversation=conversation,
            excluded_endpoint_ids=excluded_endpoints,
            routing_key=f"{request_id}:preflight",
        )
        return body, prompt_tokens, None
    except (NoCompatibleModelError, NoEligibleModelError) as original:
        try:
            target = await current.policy.choose(
                requested_model=requested_model,
                evaluation=evaluation,
                prompt_tokens=1,
                output_reserve_tokens=output_reserve_tokens,
                modalities=modalities,
                image_count=image_count,
                has_tools=has_tools,
                required_capabilities=required_capabilities,
                conversation=conversation,
                excluded_endpoint_ids=excluded_endpoints,
                routing_key=f"{request_id}:compaction-target",
            )
        except (NoCompatibleModelError, NoEligibleModelError):
            raise original

    target_context = (
        target.deployment_safe_context_tokens
        or target.endpoint.safe_context_tokens
    )
    capsule, routed, compacted_prompt_tokens = (
        await _compact_body_for_target(
            current,
            body,
            api_kind=api_kind,
            request_id=request_id,
            target_context=target_context,
            identity=identity,
        )
    )
    if compacted_prompt_tokens + output_reserve_tokens > target_context:
        raise NoCompatibleModelError(
            "the compacted request still exceeds the selected model context"
        )
    return routed, compacted_prompt_tokens, capsule


async def _prepare_routed_body(
    current: RouterRuntime,
    body: dict[str, Any],
    *,
    api_kind: str,
    decision: RouteDecision,
    request_id: str,
    identity: IdentityProfile | None = None,
    conversation: ConversationState | None = None,
    allow_compaction: bool = False,
    history_precompacted: bool = False,
) -> tuple[dict[str, Any], Any | None]:
    identity = identity or IdentityProfile.from_settings(
        current.settings.section("identity")
    )
    target_context = (
        decision.deployment_safe_context_tokens
        or decision.endpoint.safe_context_tokens
    )
    target_provider = provider_family(decision.endpoint)
    previous_endpoint = (
        current.registry.by_id(conversation.endpoint_id)
        if conversation
        else None
    )
    source_provider = (
        conversation.provider_family
        if conversation and conversation.provider_family
        else provider_family(previous_endpoint)
    )
    cross_provider = bool(
        source_provider
        and target_provider
        and source_provider != target_provider
    )
    deepseek_incompatible = (
        target_provider == "deepseek"
        and deepseek_history_requires_migration(body, api_kind)
    )
    routed = (
        normalize_history_for_provider(body, api_kind)
        if cross_provider
        else json.loads(json.dumps(body))
    )
    if (
        not decision.endpoint.cloud
        and decision.endpoint.backend_type in {"llama_cpp", "ai_pool"}
        and isinstance(routed.get("tools"), list)
    ):
        routed["tools"] = normalize_llama_tool_schemas(routed["tools"], api_kind)
    decision.history_mode = (
        "normalized"
        if cross_provider
        else "native"
    )
    if deepseek_incompatible and not allow_compaction:
        raise HistoryMigrationRequiredError(
            "DeepSeek history is missing reasoning_content required for "
            "the preceding tool transaction"
        )

    routed_prompt_tokens = current.token_counter.count_request(
        identity.inject(routed, api_kind),
        api_kind,
    )
    needs_context_compaction = (
        routed_prompt_tokens + decision.output_reserve_tokens
        > target_context
    )
    force_compaction = deepseek_incompatible
    capsule = None
    if needs_context_compaction and not allow_compaction:
        raise NoCompatibleModelError(
            "the request exceeds the selected model context and compaction "
            "was not explicitly allowed"
        )
    if force_compaction or needs_context_compaction:
        compaction = current.settings.section("compaction")
        if (
            not bool(compaction.get("enabled", True))
            or compaction.get("mode", "explicit_only") == "disabled"
        ):
            raise CompactionUnavailableError(
                "explicitly requested compaction is disabled"
            )
        capsule, routed, decision.prompt_tokens = (
            await _compact_body_for_target(
                current,
                routed,
                api_kind=api_kind,
                request_id=request_id,
                target_context=target_context,
                identity=identity,
            )
        )
        decision.history_mode = "capsule"
        if (
            decision.prompt_tokens + decision.output_reserve_tokens
            > target_context
        ):
            raise NoCompatibleModelError(
                "the compacted request still exceeds the selected model "
                "context"
            )
        if deepseek_incompatible and deepseek_history_requires_migration(
            routed,
            api_kind,
        ):
            raise HistoryMigrationRequiredError(
                "DeepSeek history is still missing reasoning_content "
                "required for the preceding tool transaction after "
                "compaction"
            )
    else:
        decision.prompt_tokens = routed_prompt_tokens

    if (
        decision.endpoint.backend_type == "ai_pool"
        and "image" in request_modalities(routed, api_kind)
    ):
        image_inputs = inspect_image_inputs(routed)
        if image_inputs.remote:
            raise RouterError(
                "AI physical deployments require embedded image data",
                status_code=400,
                code="ai_image_requires_embedded_data",
                details={"remote_images": image_inputs.remote},
            )
        vision = current.settings.section("vision")
        routed, decision.image_resizes = normalize_ai_images(
            routed,
            max_dimension=int(
                vision.get("ai_max_dimension", 1024)
            ),
            max_source_pixels=int(
                vision.get("max_source_pixels", 40_000_000)
            ),
        )
    if (
        api_kind == "responses"
        and decision.endpoint.backend_type == "codex_pool"
    ):
        routed.pop("conversation", None)
        routed.pop("previous_response_id", None)
    if history_precompacted:
        decision.history_mode = "capsule"
    return routed, capsule


async def _compact_body_for_target(
    current: RouterRuntime,
    body: dict[str, Any],
    *,
    api_kind: str,
    request_id: str,
    target_context: int,
    identity: IdentityProfile,
) -> tuple[Any, dict[str, Any], int]:
    compaction_lease = await current.scheduler.begin_request(None)
    try:
        compaction_target = await _acquire_internal_model(
            current,
            lease=compaction_lease,
            request_id=f"{request_id}:compactor",
            model_id=current.compactor.model_id,
            prompt_tokens=current.token_counter.count_request(
                body,
                api_kind,
            ),
            output_reserve_tokens=2048,
        )
        capsule = await current.compactor.compact(
            body,
            api_kind=api_kind,
            target_context_tokens=target_context,
            target=compaction_target,
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
    routed = replace_messages(
        body,
        api_kind,
        compacted_messages,
    )
    prompt_tokens = current.token_counter.count_request(
        identity.inject(routed, api_kind),
        api_kind,
    )
    return capsule, routed, prompt_tokens


def _compaction_allowed(
    current: RouterRuntime,
    client_allows: bool,
    header_value: str,
) -> bool:
    compaction = current.settings.section("compaction")
    if (
        not bool(compaction.get("enabled", True))
        or compaction.get("mode", "explicit_only") == "disabled"
    ):
        return False
    if compaction.get("mode", "explicit_only") == "automatic":
        return True
    return client_allows or _truthy_header(header_value)


def _truthy_header(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


async def _acquire_internal_model(
    current: RouterRuntime,
    *,
    lease: Any,
    request_id: str,
    model_id: str,
    wait: bool = True,
    prompt_tokens: int = 4096,
    output_reserve_tokens: int = 2048,
) -> ModelCallTarget:
    endpoint = current.registry.by_id(model_id)
    if endpoint is None or not endpoint.enabled:
        raise RouterError(
            f"internal model is not registered or enabled: {model_id}",
            status_code=503,
            code="internal_model_unavailable",
        )
    if endpoint.backend_type in {"ai_pool", "codex_pool"}:
        decision = await current.policy.choose(
            requested_model=endpoint.public_model,
            evaluation=Evaluation(
                "general",
                None,
                1.0,
                "internal_model",
            ),
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=output_reserve_tokens,
            modalities={"text"},
            has_tools=False,
            required_capabilities=RequestCapabilities(
                protocol="chat",
            ),
            conversation=None,
            routing_key=request_id,
        )
        if decision.endpoint.id != endpoint.id:
            raise RouterError(
                "internal model resolved to an unexpected endpoint",
                status_code=503,
                code="internal_model_unavailable",
            )
        candidates = tuple(
            deployment_id
            for deployment_id, _url in decision.deployment_candidates
        ) or (decision.deployment_id or endpoint.id,)
        if wait:
            selected = await current.scheduler.acquire_deployment_candidates(
                lease,
                endpoint.id,
                candidates,
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
        else:
            selected = (
                await current.scheduler.try_acquire_deployment_candidates(
                    lease,
                    candidates,
                    capacity=endpoint.max_concurrency,
                )
            )
            if not selected:
                raise QueueTimeoutError()
        routes = dict(decision.deployment_candidates)
        decision.deployment_id = selected
        decision.upstream_api_base = routes.get(
            selected,
            decision.upstream_api_base,
        )
        _apply_selected_deployment(decision, selected)
        return ModelCallTarget(
            base_url=decision.upstream_api_base or endpoint.api_base,
            model=endpoint.provider_model,
            api_key=os.environ.get(
                str(
                    decision.deployment_details.get(
                        selected,
                        {},
                    ).get("backend_api_key_env")
                    or endpoint.backend_api_key_env
                ),
                "",
            ),
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
    else:
        acquired = await current.scheduler.try_acquire_deployment_candidates(
            lease,
            (endpoint.id,),
            capacity=endpoint.max_concurrency,
        )
        if not acquired:
            raise QueueTimeoutError()
    return ModelCallTarget(
        base_url=endpoint.api_base,
        model=endpoint.provider_model,
        api_key=os.environ.get(endpoint.backend_api_key_env, ""),
    )


async def _save_conversation(
    current: RouterRuntime,
    *,
    lineage: LineageContext,
    previous: ConversationState | None,
    decision: RouteDecision,
    capsule: Any | None,
) -> ConversationState | None:
    status = await current.health.status(decision.endpoint)
    cache_generation = status.cache_generation
    if decision.deployment_id:
        details = decision.deployment_details.get(
            decision.deployment_id,
            {},
        )
        cache_generation = str(
            details.get("cache_generation") or cache_generation
        )
    return updated_conversation_state(
        previous,
        conversation_id=lineage.lineage_id,
        branch_id=lineage.branch_id,
        parent_branch_id=lineage.parent_branch_id,
        lineage_relation=lineage.relation,
        decision=decision,
        cache_generation=cache_generation,
        encrypted_capsule=capsule.encrypted_messages if capsule else None,
        boundary_hash=capsule.boundary_hash if capsule else None,
    )


async def _lineage_context(
    current: RouterRuntime,
    request: Request,
    body: dict[str, Any],
    api_kind: str,
    client_id: str,
    *,
    force_new_inferred: bool = False,
    raw_identity_namespace: bool = False,
) -> LineageContext:
    explicit_lineage_id = None
    for name in ("x-1panel-conversation-id", "x-litellm-session-id"):
        value = request.headers.get(name, "").strip()
        if value:
            explicit_lineage_id = value[:256]
            break
    if api_kind == "responses":
        conversation = body.get("conversation")
        if explicit_lineage_id is None:
            if isinstance(conversation, str) and conversation.strip():
                explicit_lineage_id = conversation.strip()[:256]
            elif isinstance(conversation, dict) and conversation.get("id"):
                explicit_lineage_id = str(conversation["id"])[:256]
        return await current.conversations.lineage_context(
            client_id=client_id,
            identities=history_lookup_identities(
                extract_messages(body, api_kind)
            ),
            explicit_lineage_id=explicit_lineage_id,
            previous_response_id=(
                str(body.get("previous_response_id", "")).strip()
                or None
            ),
            force_new=force_new_inferred,
        )
    return await current.conversations.lineage_context(
        client_id=client_id,
        identities=tuple(("wb-raw-v1:" if raw_identity_namespace else "") + key
                         for key in history_lookup_identities(extract_messages(body, api_kind))),
        explicit_lineage_id=explicit_lineage_id,
        previous_response_id=None,
        force_new=force_new_inferred,
    )


class _FinalizingStreamingResponse(StreamingResponse):
    """Run the background finalizer even when ASGI send disconnects."""

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        background = self.background
        self.background = None
        try:
            await super().__call__(scope, receive, send)
        finally:
            if background is not None:
                await background()


class _StreamResourceFinalizer:
    """Release stream-owned leases exactly once, including disconnects."""

    def __init__(
        self,
        current: RouterRuntime,
        lease: Any,
        client_id: str,
    ) -> None:
        self.current = current
        self.lease = lease
        self.client_id = client_id
        self._lock = asyncio.Lock()
        self._stream_finalization_lock = asyncio.Lock()
        self._released = False

    async def begin_stream(self) -> None:
        await self._stream_finalization_lock.acquire()

    async def wait_for_stream_finalization(self) -> None:
        async with self._stream_finalization_lock:
            pass

    def finish_stream(self) -> None:
        if self._stream_finalization_lock.locked():
            self._stream_finalization_lock.release()

    async def __call__(self) -> None:
        async with self._lock:
            if self._released:
                return
            first_error: Exception | None = None
            for operation in (
                self.lease.release,
                lambda: self.current.limiter.release_parallel(
                    self.client_id,
                    self.lease.owner_token,
                ),
                lambda: self.current.track_request_finished(
                    self.lease.owner_token
                ),
            ):
                try:
                    await operation()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
            self._released = True


async def _run_stream_resource_finalizer(
    finalizer: _StreamResourceFinalizer,
) -> None:
    cleanup = asyncio.create_task(finalizer())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(cleanup)
        finally:
            raise


async def _run_stream_finalization(finalization: Awaitable[None]) -> None:
    """Finish stream bookkeeping before propagating client cancellation."""
    task = asyncio.create_task(finalization)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        finally:
            raise


async def _finalize_abandoned_stream(
    current: RouterRuntime,
    *,
    resource_finalizer: _StreamResourceFinalizer,
    client_id: str,
    key_id: str,
    request_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
    training_token: str | None,
    started_at: float,
    cache_snapshot: dict[str, float] | None,
) -> None:
    try:
        await resource_finalizer.wait_for_stream_finalization()
        if decision.trace is not None and not decision.trace.terminal:
            if current.training is not None:
                await asyncio.shield(
                    current.training.fail(
                        training_token,
                        status_code=499,
                        error={
                            "type": "stream_interrupted",
                            "message": (
                                "stream response ended before finalization"
                            ),
                        },
                        interrupted=True,
                    )
                )
            await _audit(
                current,
                request_id=request_id,
                client_id=client_id,
                key_id=key_id,
                conversation_id=conversation_id,
                decision=decision,
                status_code=499,
                started_at=started_at,
                cache_snapshot=cache_snapshot,
            )
    finally:
        await _run_stream_resource_finalizer(resource_finalizer)


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
            state.branch_id or state.conversation_id,
        )


async def _stream_response(
    current: RouterRuntime,
    upstream: httpx.Response,
    *,
    resource_finalizer: _StreamResourceFinalizer,
    client_id: str,
    key_id: str,
    request_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
    state: ConversationState | None,
    body: dict[str, Any],
    api_kind: str,
    training_token: str | None,
    started_at: float,
    cache_snapshot: dict[str, float] | None,
    identity: IdentityProfile,
    identifiers: tuple[str, ...],
) -> AsyncIterator[bytes]:
    await resource_finalizer.begin_stream()
    accumulator = SSEAccumulator(api_kind)
    private_accumulator = SSEAccumulator(api_kind)
    output_clock = OutputClock(api_kind, started_at)
    usage_filter = UsageOnlyFilter() if upstream.extensions.get("internal_cache_usage") and api_kind == "chat" else None
    adapter_usage = {}
    def capture_adapter_usage(value):
        adapter_usage.clear()
        adapter_usage.update(value)
    sanitizer = IdentityStreamSanitizer(
        api_kind,
        identity,
        identifiers,
    )
    status_code = upstream.status_code
    completed = False
    try:
        source = (
            chat_stream_to_responses(
                upstream,
                model=decision.endpoint.public_model,
                usage_observer=capture_adapter_usage,
            )
            if (
                api_kind == "responses"
                and decision.native_or_adapter == "adapter"
            )
            else upstream.aiter_bytes()
        )
        async for chunk in source:
            private_accumulator.feed(chunk)
            batch_completed = False
            visible = b"".join(usage_filter.feed(chunk)) if usage_filter else chunk
            for public_chunk in sanitizer.feed(visible):
                accumulator.feed(public_chunk)
                output_clock.feed(public_chunk)
                if decision.trace:
                    decision.trace.payload.setdefault("observation", {}).update(output_clock.values)
                yield public_chunk
                if accumulator.completed:
                    batch_completed = True
            if batch_completed:
                completed = True
                break
        else:
            trailing = b"".join(usage_filter.finish()) if usage_filter else b""
            for public_chunk in sanitizer.feed(trailing) + sanitizer.finish():
                accumulator.feed(public_chunk)
                output_clock.feed(public_chunk)
                if decision.trace:
                    decision.trace.payload.setdefault("observation", {}).update(output_clock.values)
                yield public_chunk
            # A clean EOF still needs a protocol terminal marker. Cancellation
            # or transport failure must never promote partial usage to success.
            completed = bool(accumulator.terminal or private_accumulator.terminal)
    finally:
        accumulator.finish()
        private_accumulator.finish()

        async def finalize_stream() -> None:
            try:
                await upstream.aclose()
                if completed:
                    await persist_history(
                        current.compactor,
                        current.conversations,
                        state=state,
                        client_id=client_id,
                        body=body,
                        api_kind=api_kind,
                        assistant_items=private_history_items(
                            accumulator.assistant_items(),
                            private_accumulator.assistant_items(),
                        ),
                    )
                    if accumulator.response_id and state:
                        await current.conversations.map_response(
                            accumulator.response_id,
                            state.branch_id or state.conversation_id,
                        )
                    if current.training is not None:
                        await asyncio.shield(
                            current.training.complete(
                                training_token,
                                status_code=status_code,
                                assistant_items=(
                                    private_accumulator.assistant_items()
                                ),
                                usage=private_accumulator.usage,
                            )
                        )
                elif current.training is not None:
                    await asyncio.shield(
                        current.training.fail(
                            training_token,
                            status_code=499,
                            error={
                                "type": "stream_interrupted",
                                "message": (
                                    "stream ended before a complete response"
                                ),
                            },
                            interrupted=True,
                        )
                    )
                if not completed and decision.trace:
                    decision.trace.record(
                        decision.attempts,
                        "upstream_request",
                        "error",
                        reason="stream_interrupted",
                        evidence={"status_code": 499},
                    )
                    decision.trace.fail(
                        status_code=499,
                        code="stream_interrupted",
                        message=(
                            "stream ended before a complete response"
                        ),
                        interrupted=True,
                        attempt=decision.attempts,
                    )
                    await _save_request_trace(
                        current,
                        decision.trace,
                    )
                decision.response_redactions = sanitizer.redactions
                await _audit(
                    current,
                    request_id=request_id,
                    client_id=client_id,
                    key_id=key_id,
                    conversation_id=conversation_id,
                    decision=decision,
                    status_code=status_code if completed else 499,
                    started_at=started_at,
                    usage=adapter_usage if api_kind == "responses" and decision.native_or_adapter == "adapter" else private_accumulator.usage,
                    usage_complete=not private_accumulator.usage_incomplete,
                    cache_snapshot=cache_snapshot,
                )
            finally:
                try:
                    await _run_stream_resource_finalizer(resource_finalizer)
                finally:
                    resource_finalizer.finish_stream()

        await _run_stream_finalization(finalize_stream())


async def _fail_training_record(
    current: RouterRuntime,
    token: str | None,
    exc: BaseException,
) -> None:
    if current.training is None or not token:
        return
    status_code = int(getattr(exc, "status_code", 500))
    interrupted = isinstance(exc, asyncio.CancelledError)
    if interrupted:
        status_code = 499
    error = {
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
    }
    if isinstance(exc, RouterError):
        error["code"] = exc.code
        error["details"] = exc.details
    await asyncio.shield(
        current.training.fail(
            token,
            status_code=status_code,
            error=error,
            interrupted=interrupted,
        )
    )


def _response_headers(
    upstream: httpx.Response,
    decision: RouteDecision,
    request_id: str,
    *,
    conversation_id: str,
    conversation_mode: str,
    identity: IdentityProfile,
) -> dict[str, str]:
    if identity.enabled:
        headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in {"cache-control", "retry-after"}
        }
        headers.update(
            {
                "X-Request-ID": request_id,
                "X-1Panel-Public-Model": identity.public_model_id,
                "X-1Panel-Conversation-ID": conversation_id,
            }
        )
        return headers
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in {"content-length", "content-encoding"}
    }
    headers.update(decision.response_headers(request_id))
    headers["X-Request-ID"] = request_id
    headers["X-1Panel-Conversation-ID"] = conversation_id
    headers["X-1Panel-Conversation-Mode"] = conversation_mode
    return headers


async def _audit(
    current: RouterRuntime,
    *,
    request_id: str,
    client_id: str,
    key_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
    status_code: int,
    started_at: float,
    response_payload: bytes | None = None,
    usage: dict[str, Any] | None = None,
    usage_complete: bool = True,
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
    explicit_cached, _ = _cache_metrics(response_payload, usage, prompt_tokens_fallback=decision.prompt_tokens)
    backend_usage = usage_measurement(response_payload, usage)
    if response_payload:
        try:
            response_value = json.loads(response_payload)
            if isinstance(response_value, dict) and response_value.get("status") in {"incomplete", "failed", "cancelled", "in_progress", "queued"}:
                usage_complete = False
        except (ValueError, TypeError):
            pass
    if not usage_complete:
        backend_usage = {**backend_usage, "state": "incomplete", "cached_tokens": None}
    cache_measurement_source = "upstream_usage" if explicit_cached is not None else "backend_counter_delta" if cached_prompt_tokens_fallback is not None else "unavailable"
    decision.actual_cached_tokens = cached_prompt_tokens
    if decision.trace:
        decision.trace.payload.setdefault("observation", {})["queue_wait_ms"] = decision.queue_wait_ms
    prefix_prediction_error_tokens = (
        abs(
            decision.predicted_cached_tokens
            - decision.actual_cached_tokens
        )
        if decision.actual_cached_tokens is not None
        else None
    )
    input_tokens, output_tokens = _usage_totals(
        response_payload,
        usage,
        prompt_tokens_fallback=decision.prompt_tokens,
    )
    if decision.trace and not decision.trace.terminal:
        decision.trace.payload["response_redactions"] = int(
            decision.response_redactions
        )
        decision.trace.finish(
            attempt=decision.attempts,
            status_code=status_code,
            evidence={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_prompt_tokens": cached_prompt_tokens,
                "cache_measurement_source": cache_measurement_source,
                "backend_usage": backend_usage,
                "cache_hit_ratio": cache_hit_ratio,
                "prefix_match_type": decision.prefix_match_type,
                "predicted_cached_tokens": (
                    decision.predicted_cached_tokens
                ),
                "matched_checkpoint_tokens": (
                    decision.matched_checkpoint_tokens
                ),
            },
        )
        await _save_request_trace(current, decision.trace)
    if client_id in {"workbuddy-public", "workbuddy-qwen36-shared"} and decision.trace and decision.trace.terminal:
        if decision.trace.payload.get("status") == "succeeded":
            try:
                await asyncio.to_thread(WorkBuddyHistory(current.route_traces.database_path).record, decision.trace.payload)
                history = next((c for c in decision.trace.payload.get("observation", {}).get("content", {}).get("checks", []) if c.get("check") == "workbuddy_history"), {})
                # Only metadata aliases are stored in Redis; raw content remains encrypted.
                aliases = history.get("raw_identities", [])
                if aliases and decision.trace.payload.get("branch_id"):
                    await current.conversations.map_history(client_id, tuple(aliases), decision.trace.payload["branch_id"])
            except Exception as error:
                current.audit.write("workbuddy_history_index_unavailable", request_id=request_id, error_type=type(error).__name__)
        if not getattr(current, "prefix_break_collector", None):
            current.prefix_break_collector = PrefixBreakCollector(current)
        current.prefix_break_collector.submit(decision.trace.payload, decision)
    current.audit.write(
        "request_completed",
        request_id=request_id,
        client_request_id=(
            decision.trace.payload.get("client_request_id")
            if decision.trace
            else None
        ),
        client_id=client_id,
        key_id=key_id,
        conversation_id=conversation_id,
        requested_model=decision.requested_model,
        selected_model=decision.endpoint.public_model,
        endpoint_id=decision.endpoint.id,
        deployment_id=decision.deployment_id or decision.endpoint.id,
        deployment_profile_id=decision.deployment_profile_id,
        deployment_vision_status=decision.deployment_vision_status,
        image_resizes=decision.image_resizes,
        node=decision.endpoint.node,
        task=decision.task,
        reason=decision.reason,
        affinity=decision.affinity,
        prompt_tokens=decision.prompt_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        output_reserve_tokens=decision.output_reserve_tokens,
        requested_output_tokens=decision.output_reserve_tokens,
        context_required=decision.context_required,
        strategy_version=decision.strategy_version,
        route_profile=decision.route_profile,
        complexity=decision.complexity,
        history_mode=decision.history_mode,
        conversation_mode=decision.conversation_mode,
        branch_id=decision.branch_id,
        parent_branch_id=decision.parent_branch_id,
        lineage_relation=decision.lineage_relation,
        context_compacted=decision.context_compacted,
        context_compaction_source=decision.context_compaction_source,
        remote_fallback_position=decision.remote_fallback_position,
        attempts=decision.attempts,
        capacity_attempts=decision.capacity_attempts,
        queue_wait_ms=round(decision.queue_wait_ms, 2),
        status_code=status_code,
        latency_ms=round((time.monotonic() - started_at) * 1000, 2),
        cached_prompt_tokens=cached_prompt_tokens,
        cache_measurement_source=cache_measurement_source,
        backend_usage=backend_usage,
        cache_hit_ratio=cache_hit_ratio,
        prefix_match_type=decision.prefix_match_type,
        predicted_cached_tokens=decision.predicted_cached_tokens,
        actual_cached_tokens=decision.actual_cached_tokens,
        matched_checkpoint_tokens=decision.matched_checkpoint_tokens,
        prefix_prediction_error_tokens=(
            prefix_prediction_error_tokens
        ),
        required_capabilities=list(decision.required_capabilities),
        tool_history_repairs=decision.tool_history_repairs,
        protocol=decision.protocol,
        native_or_adapter=decision.native_or_adapter,
        candidate_rejections=list(decision.candidate_rejections),
        identity_revision=decision.identity_revision,
        legacy_model_alias_used=decision.legacy_model_alias_used,
        response_redactions=decision.response_redactions,
        disclosure_mode=(
            decision.trace.payload.get("disclosure_mode", "internal")
            if decision.trace
            else "internal"
        ),
        instance_id=current.instance_id,
        boot_id=current.boot_id,
    )
    if status_code < 400:
        try:
            await current.policy.mark_deployment_recent(
                decision,
                conversation_id,
            )
        except Exception as exc:
            current.audit.write(
                "deployment_recent_marker_failed",
                request_id=request_id,
                deployment_id=(
                    decision.deployment_id or decision.endpoint.id
                ),
                error=type(exc).__name__,
            )
    if (
        decision.endpoint.metadata.get("prefix_affinity_enabled")
        is True
    ):
        try:
            deployment_id = (
                decision.deployment_id or decision.endpoint.id
            )
            details = decision.deployment_details.get(
                deployment_id,
                {},
            )
            cache_generation = str(
                details.get("cache_generation") or ""
            )
            if not cache_generation:
                endpoint_status = await current.health.status(
                    decision.endpoint
                )
                cache_generation = endpoint_status.cache_generation
            if (
                status_code < 400
                and decision.prefix_affinity_signature is not None
            ):
                await current.prefix_affinity.record_worker(
                    decision.prefix_affinity_signature,
                    endpoint_id=decision.endpoint.id,
                    deployment_id=deployment_id,
                    cache_generation=cache_generation,
                )
            else:
                await current.prefix_affinity.invalidate_worker(
                    deployment_id
                )
        except Exception as exc:
            current.audit.write(
                "prefix_affinity_marker_failed",
                request_id=request_id,
                endpoint_id=decision.endpoint.id,
                deployment_id=(
                    decision.deployment_id
                    or decision.endpoint.id
                ),
                error=type(exc).__name__,
            )
    await _record_client_usage(
        current,
        client_id=client_id,
        key_id=key_id,
        request_id=request_id,
        status_code=status_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _record_client_usage(
    current: RouterRuntime,
    *,
    client_id: str,
    key_id: str,
    request_id: str,
    status_code: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    try:
        await current.clients.record_usage(
            client_id=client_id,
            key_id=key_id,
            status_code=status_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception as exc:
        current.audit.write(
            "client_usage_record_failed",
            client_id=client_id,
            key_id=key_id,
            request_id=request_id,
            error=type(exc).__name__,
        )


def _usage_totals(
    response_payload: bytes | None,
    usage: dict[str, Any] | None,
    *,
    prompt_tokens_fallback: int,
) -> tuple[int, int]:
    current = usage_dict(response_payload, usage)
    measured = usage_measurement(usage=current)
    input_tokens = measured["input_tokens"]
    if input_tokens is None:
        input_tokens = token_count(prompt_tokens_fallback) or 0
    output_tokens = token_count(current.get("completion_tokens", current.get("output_tokens")))
    return input_tokens, output_tokens if output_tokens is not None else 0


def _cache_metrics(
    response_payload: bytes | None,
    usage: dict[str, Any] | None,
    *,
    cached_prompt_tokens_fallback: int | None = None,
    prompt_tokens_fallback: int = 0,
) -> tuple[int | None, float | None]:
    measured = usage_measurement(response_payload, usage)
    if measured["state"] == "invalid":
        return None, None
    cached = measured["cached_tokens"]
    if cached is None:
        cached = token_count(cached_prompt_tokens_fallback)
    if cached is None:
        return None, None
    total = measured["input_tokens"]
    if total is None:
        total = token_count(prompt_tokens_fallback)
    if total is not None and cached > total:
        return None, None
    return cached, round(cached / total, 6) if total else None


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
    key_id: str,
    conversation_id: str | None,
    decision: RouteDecision,
) -> None:
    current.audit.write(
        "request_started",
        request_id=request_id,
        client_request_id=(
            decision.trace.payload.get("client_request_id")
            if decision.trace
            else None
        ),
        client_id=client_id,
        key_id=key_id,
        conversation_id=conversation_id,
        requested_model=decision.requested_model,
        selected_model=decision.endpoint.public_model,
        endpoint_id=decision.endpoint.id,
        deployment_id=decision.deployment_id or decision.endpoint.id,
        deployment_profile_id=decision.deployment_profile_id,
        deployment_vision_status=decision.deployment_vision_status,
        image_resizes=decision.image_resizes,
        node=decision.endpoint.node,
        task=decision.task,
        reason=decision.reason,
        affinity=decision.affinity,
        prompt_tokens=decision.prompt_tokens,
        output_reserve_tokens=decision.output_reserve_tokens,
        requested_output_tokens=decision.output_reserve_tokens,
        context_required=decision.context_required,
        strategy_version=decision.strategy_version,
        route_profile=decision.route_profile,
        complexity=decision.complexity,
        history_mode=decision.history_mode,
        conversation_mode=decision.conversation_mode,
        branch_id=decision.branch_id,
        parent_branch_id=decision.parent_branch_id,
        lineage_relation=decision.lineage_relation,
        context_compacted=decision.context_compacted,
        context_compaction_source=decision.context_compaction_source,
        remote_fallback_position=decision.remote_fallback_position,
        attempts=decision.attempts,
        capacity_attempts=decision.capacity_attempts,
        queue_wait_ms=round(decision.queue_wait_ms, 2),
        required_capabilities=list(decision.required_capabilities),
        tool_history_repairs=decision.tool_history_repairs,
        protocol=decision.protocol,
        native_or_adapter=decision.native_or_adapter,
        candidate_rejections=list(decision.candidate_rejections),
        identity_revision=decision.identity_revision,
        legacy_model_alias_used=decision.legacy_model_alias_used,
        disclosure_mode=(
            decision.trace.payload.get("disclosure_mode", "internal")
            if decision.trace
            else "internal"
        ),
        instance_id=current.instance_id,
        boot_id=current.boot_id,
    )


async def _save_request_trace(
    current: RouterRuntime,
    trace: DecisionTrace | None,
) -> None:
    if trace is None:
        return
    if trace.terminal:
        pool = getattr(getattr(current, "policy", None), "local_pool", None)
        if pool:
            try:
                await asyncio.shield(pool.finish(trace))
            except Exception as exc:
                current.audit.write("local_pool_observation_failed", request_id=trace.request_id,
                                    error=type(exc).__name__)
    try:
        await asyncio.shield(current.route_traces.save(trace))
    except Exception as exc:
        current.audit.write(
            "route_trace_write_failed",
            request_id=trace.request_id,
            error=type(exc).__name__,
            instance_id=current.instance_id,
            boot_id=current.boot_id,
        )


async def _finish_request_trace_error(
    request: Request,
    exc: RouterError,
) -> None:
    trace = getattr(request.state, "route_trace", None)
    if trace is None or trace.terminal:
        return
    current = _runtime(request)
    trace.fail(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
    )
    await _save_request_trace(current, trace)


async def _finish_trace_exception(
    current: RouterRuntime,
    trace: DecisionTrace | None,
    exc: BaseException,
) -> None:
    if trace is None or trace.terminal:
        return
    interrupted = isinstance(exc, asyncio.CancelledError)
    trace.fail(
        status_code=(
            499
            if interrupted
            else int(getattr(exc, "status_code", 500))
        ),
        code=(
            "request_interrupted"
            if interrupted
            else str(
                getattr(
                    exc,
                    "code",
                    "internal_router_error",
                )
            )
        ),
        message=(
            "request interrupted before completion"
            if interrupted
            else str(exc) or type(exc).__name__
        ),
        interrupted=interrupted,
    )
    await _save_request_trace(current, trace)


def _record_trace_retry(
    trace: DecisionTrace | None,
    *,
    attempt: int,
    status_code: int,
    reason: str,
    allowed: bool,
    upstream: bool = True,
) -> None:
    if trace is None or trace.terminal:
        return
    if upstream:
        trace.record(
            attempt,
            "upstream_request",
            "error",
            reason=reason,
            evidence={"status_code": status_code},
        )
    trace.record(
        attempt,
        "retry_decision",
        "passed" if allowed else "failed",
        reason=reason,
        evidence={
            "allowed": allowed,
            "status_code": status_code,
        },
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


def _error_response(
    exc: RouterError,
    *,
    profile: IdentityProfile | None = None,
    identifiers: tuple[str, ...] = (),
    public: bool = True,
    request_id: str | None = None,
) -> JSONResponse:
    message = str(exc)
    if public:
        message = _public_error_message(exc)
    error = {
        "message": message,
        "type": "invalid_request_error"
        if exc.status_code < 500
        else "server_error",
        "code": exc.code,
    }
    if exc.details and not public:
        error["details"] = exc.details
    if public and profile and profile.enabled:
        error, _redactions = sanitize_value(
            error,
            profile,
            identifiers,
        )
    headers = dict(exc.headers)
    if request_id:
        headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={"error": error},
    )


def _public_error_message(exc: RouterError) -> str:
    messages = {
        "invalid_api_key": "invalid API key",
        "model_not_found": "the requested model is not available",
        "public_identity_unavailable": (
            "public model identity is unavailable"
        ),
        "invalid_json": "request body must be valid JSON",
        "invalid_request": "the request is invalid",
        "model_required": "model is required",
        "payload_too_large": "request body is too large",
        "invalid_tool_history": "tool history is invalid",
        "conversation_busy": (
            "another request is already running for this conversation"
        ),
        "conversation_state_conflict": (
            "conversation history does not match the stored state"
        ),
        "parallel_limit_exceeded": (
            "client parallel request limit exceeded"
        ),
        "rate_limit_exceeded": "rate limit exceeded",
        "model_capacity_busy": "model capacity is busy",
        "all_local_capacity_busy": "model capacity is busy",
        "model_queue_timeout": "model capacity is busy",
        "no_eligible_model": "no eligible model is available",
        "no_compatible_model": (
            "no model can satisfy the request constraints"
        ),
        "router_draining": "service is restarting",
    }
    return messages.get(
        exc.code,
        (
            "the request could not be completed"
            if exc.status_code < 500
            else "the model service is temporarily unavailable"
        ),
    )


app = create_app()
