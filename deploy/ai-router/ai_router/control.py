from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import os
import sqlite3
from urllib.parse import quote
from .instance_status import classify_instances
from .cache_audit import CacheAudit
from .content_audit import ArchiveReader
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .cache_deployments import (
    cache_deployment_view,
    load_cache_deployment_catalog,
)
from .errors import RouterError
from .identity import IdentityProfile
from .media_service.gateway import router as media_router
from .h3_mcp import install_h3_mcp
from .prompt_directives import (
    configured_phrases,
    prepare_prompt_directive_update,
)
from .policy_config import PolicyConflictError
from .route_diagnosis import diagnose_route
from .route_trace import (
    graph_document,
    registry_fingerprint,
    validate_review,
)
from .runtime import RouterRuntime, build_runtime


from .lan_https import router as lan_https_router


STATIC_DIR = Path(__file__).resolve().parent / "static"
EDITABLE_SECTIONS = {
    "affinity",
    "cloud",
    "compaction",
    "evaluator",
    "failover",
    "health",
    "identity",
    "lmcache",
    "queue",
    "routing",
    "vision",
}


def create_app(runtime: RouterRuntime | None = None) -> FastAPI:
    cache_catalog = load_cache_deployment_catalog()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = runtime is None
        app.state.runtime = runtime or build_runtime()
        await app.state.runtime.start()
        yield
        if owned:
            await app.state.runtime.close()

    app = FastAPI(
        title="1Panel AI Router Control",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.include_router(media_router(admin=True))
    app.include_router(lan_https_router)
    install_h3_mcp(app)

    @app.get("/media")
    async def media_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "studio.html")

    @app.get("/media/legacy")
    async def legacy_media_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "media.html")

    @app.exception_handler(RouterError)
    async def router_error_handler(
        _request: Request,
        exc: RouterError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": str(exc),
                    "code": exc.code,
                    **({"details": exc.details} if exc.details else {}),
                }
            },
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        current = _runtime(request)
        return {"ok": True, "state_store": await current.store.ping()}

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        return {
            "settings": _editable(current.settings.value),
            "runtime_path": str(current.settings.runtime_path),
        }

    @app.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        try:
            value = await request.json()
        except Exception as exc:
            raise RouterError(
                "settings must be valid JSON",
                status_code=400,
                code="invalid_settings",
            ) from exc
        if not isinstance(value, dict):
            raise RouterError(
                "settings must be a JSON object",
                status_code=400,
                code="invalid_settings",
            )
        override = _editable(value)
        current_prompt = current.settings.section("routing").get(
            "prompt_directives",
            {},
        )
        proposed_prompt = (
            override.get("routing", {}).get("prompt_directives")
            if isinstance(override.get("routing"), dict)
            else None
        )
        prompt_changes: list[dict[str, str]] = []
        if proposed_prompt is not None:
            prepared_prompt, prompt_changes = (
                prepare_prompt_directive_update(
                    current_prompt,
                    proposed_prompt,
                )
            )
            override["routing"]["prompt_directives"] = prepared_prompt
        try:
            current.settings.write_runtime(override)
        except (TypeError, ValueError) as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_settings",
            ) from exc
        current.reload_settings()
        active_policy = await current.policy_config.record_external_activation(
            current.settings.value,
            source=request.client.host if request.client else "unknown",
        )
        updated_prompt = current.settings.section("routing").get(
            "prompt_directives",
            {},
        )
        current.prompt_directives.sync_active(
            configured_phrases(updated_prompt)
        )
        current.prompt_directives.record_changes(
            int(updated_prompt.get("revision", 1)),
            prompt_changes,
            previous=current_prompt,
            current=updated_prompt,
            source=request.client.host if request.client else "unknown",
        )
        current.audit.write(
            "settings_updated",
            sections=sorted(override),
            prompt_directive_ids=[
                item["directive_id"] for item in prompt_changes
            ],
            source=request.client.host if request.client else "unknown",
            policy_revision=active_policy["revision"],
        )
        return {
            "ok": True,
            "settings": _editable(current.settings.value),
            "policy_revision": active_policy["revision"],
        }

    @app.get("/api/policy")
    async def get_policy(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        return {
            "policy": await current.policy_config.snapshot(),
            "effective_settings": _editable(current.settings.value),
            "runtime_path": str(current.settings.runtime_path),
        }

    @app.patch("/api/policy/draft")
    async def patch_policy_draft(
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        value = await _json_body(request)
        changes = value.get("changes")
        if not isinstance(changes, dict):
            raise RouterError(
                "changes must be a JSON object",
                status_code=400,
                code="invalid_policy_draft",
            )
        objectives = changes.get("routing", {}).get("objectives", {}) if isinstance(changes.get("routing"), dict) else {}
        if isinstance(objectives, dict):
            for key in ("flash_order", "quality_order"):
                order = objectives.get(key)
                if isinstance(order, dict):
                    for values in order.values():
                        if isinstance(values, list) and any(not isinstance(eid, str) or current.registry.by_id(eid) is None for eid in values):
                            raise RouterError("模型顺序中包含未注册的端点", status_code=400, code="invalid_policy_draft")
        snapshot = await current.policy_config.snapshot()
        draft_record = snapshot.get("draft")
        base_settings = (
            draft_record.get("settings", {})
            if draft_record
            else snapshot["active"].get("settings", {})
        )
        proposed_prompt = (
            changes.get("routing", {}).get("prompt_directives")
            if isinstance(changes.get("routing"), dict)
            else None
        )
        if proposed_prompt is not None:
            base_prompt = (
                base_settings.get("routing", {}).get(
                    "prompt_directives"
                )
                or current.settings.section("routing").get(
                    "prompt_directives",
                    {},
                )
            )
            prepared_prompt, _changes = prepare_prompt_directive_update(
                base_prompt,
                proposed_prompt,
            )
            changes = {
                **changes,
                "routing": {
                    **changes["routing"],
                    "prompt_directives": prepared_prompt,
                },
            }
        source = request.client.host if request.client else "unknown"
        try:
            draft = await current.policy_config.patch_draft(
                _editable(changes),
                expected_revision=_optional_int(
                    value.get("expected_revision")
                ),
                expected_fingerprint=_optional_text(
                    value.get("expected_fingerprint"),
                    128,
                ),
                source=source,
            )
        except PolicyConflictError as exc:
            raise RouterError(
                str(exc),
                status_code=409,
                code="policy_revision_conflict",
            ) from exc
        except ValueError as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_policy_draft",
            ) from exc
        current.audit.write(
            "policy_draft_updated",
            revision=draft["revision"],
            settings_fingerprint=draft["settings_fingerprint"],
            source=source,
        )
        return {"draft": draft}

    @app.post("/api/policy/draft/validate")
    async def validate_policy_draft(
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        limit = max(1, min(500, int(value.get("limit", 100))))
        traces = await current.route_traces.recent_auto(
            limit=limit,
            auto_models=(
                "auto",
                _public_model_id(current),
            ),
        )
        source = request.client.host if request.client else "unknown"
        try:
            draft = await current.policy_config.validate_draft(
                traces,
                expected_revision=_optional_int(
                    value.get("expected_revision")
                ),
                expected_fingerprint=_optional_text(
                    value.get("expected_fingerprint"),
                    128,
                ),
                source=source,
            )
        except PolicyConflictError as exc:
            raise RouterError(
                str(exc),
                status_code=409,
                code="policy_revision_conflict",
            ) from exc
        except ValueError as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_policy_draft",
            ) from exc
        current.audit.write(
            "policy_draft_validated",
            revision=draft["revision"],
            impact=draft["validation"]["impact"],
            source=source,
        )
        return {"draft": draft}

    @app.post("/api/policy/draft/activate")
    async def activate_policy_draft(
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        source = request.client.host if request.client else "unknown"
        current_prompt = current.settings.section("routing").get(
            "prompt_directives",
            {},
        )
        try:
            active = await current.policy_config.activate(
                expected_revision=_optional_int(
                    value.get("expected_revision")
                ),
                expected_fingerprint=_optional_text(
                    value.get("expected_fingerprint"),
                    128,
                ),
                source=source,
            )
        except PolicyConflictError as exc:
            raise RouterError(
                str(exc),
                status_code=409,
                code="policy_revision_conflict",
            ) from exc
        except ValueError as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_policy_activation",
            ) from exc
        current.reload_settings()
        updated_prompt = current.settings.section("routing").get(
            "prompt_directives",
            {},
        )
        prompt_changes = _prompt_change_records(
            current_prompt,
            updated_prompt,
        )
        current.prompt_directives.sync_active(
            configured_phrases(updated_prompt)
        )
        current.prompt_directives.record_changes(
            int(updated_prompt.get("revision", 1)),
            prompt_changes,
            previous=current_prompt,
            current=updated_prompt,
            source=source,
        )
        current.audit.write(
            "policy_activated",
            revision=active["revision"],
            settings_fingerprint=active["settings_fingerprint"],
            source=source,
        )
        return {
            "active": active,
            "settings": _editable(current.settings.value),
        }

    @app.post("/api/policy/revisions/{revision}/rollback")
    async def rollback_policy_revision(
        revision: int,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        source = request.client.host if request.client else "unknown"
        try:
            draft = await current.policy_config.rollback(
                revision,
                expected_active_revision=_optional_int(
                    value.get("expected_active_revision")
                ),
                expected_active_fingerprint=_optional_text(
                    value.get("expected_active_fingerprint"),
                    128,
                ),
                source=source,
            )
        except PolicyConflictError as exc:
            raise RouterError(
                str(exc),
                status_code=409,
                code="policy_revision_conflict",
            ) from exc
        except ValueError as exc:
            raise RouterError(
                str(exc),
                status_code=404,
                code="policy_revision_not_found",
            ) from exc
        current.audit.write(
            "policy_rollback_draft_created",
            source_revision=revision,
            draft_revision=draft["revision"],
            source=source,
        )
        return {"draft": draft}

    @app.get("/api/prompt-directives/pool")
    async def prompt_directive_pool(
        request: Request,
    ) -> JSONResponse:
        current = _authorized_runtime(request)
        current.reload_settings()
        return JSONResponse(
            {"pool": current.prompt_directives.stats()},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/prompt-directives/suggest")
    async def suggest_prompt_directives(
        request: Request,
    ) -> JSONResponse:
        current = _authorized_runtime(request)
        current.reload_settings()
        value = await _json_body(request)
        directive_ids = value.get("directive_ids")
        if not isinstance(directive_ids, list):
            raise RouterError(
                "directive_ids must be an array",
                status_code=400,
                code="invalid_route_directive",
            )
        prompt_settings = current.settings.section("routing").get(
            "prompt_directives",
            {},
        )
        valid_ids = {
            *prompt_settings.get("routes", {}),
            "reset",
        }
        policy_snapshot = await current.policy_config.snapshot()
        draft_settings = (policy_snapshot.get("draft") or {}).get("settings", {})
        valid_ids.update(
            draft_settings.get("routing", {}).get("prompt_directives", {}).get("routes", {})
        )
        ids = [str(item) for item in directive_ids]
        if any(item not in valid_ids for item in ids):
            raise RouterError(
                "directive_ids contains an unknown directive",
                status_code=400,
                code="invalid_route_directive",
            )
        suggestions = current.prompt_directives.suggest(
            ids,
            excluded=set(configured_phrases(prompt_settings)),
        )
        current.audit.write(
            "prompt_directives_suggested",
            directive_ids=ids,
            source=request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {
                "suggestions": suggestions,
                "pool": current.prompt_directives.stats(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/endpoints")
    async def endpoints(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        await current.reload_endpoint_config()
        return {"endpoints": await _endpoint_values(current, cache_catalog)}

    @app.get("/api/cache/deployments")
    async def cache_deployments(request: Request) -> JSONResponse:
        current = _authorized_runtime(request)
        current.reload_settings()
        await current.reload_endpoint_config()
        payload = cache_deployment_view(
            cache_catalog,
            await _endpoint_values(current, cache_catalog),
            current.settings.value,
        )
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store"},
        )

    @app.patch("/api/endpoints/{endpoint_id}")
    async def update_endpoint_draft(
        endpoint_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        changes = value.get("changes", {})
        if not isinstance(changes, dict):
            raise RouterError(
                "endpoint changes must be a JSON object",
                status_code=400,
                code="invalid_endpoint_config",
            )
        source = request.client.host if request.client else "unknown"
        draft = await current.endpoint_configs.save_draft(
            endpoint_id,
            changes,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
            source=source,
        )
        current.audit.write(
            "endpoint_draft_updated",
            endpoint_id=endpoint_id,
            revision=draft["revision"],
            fields=sorted(changes),
            source=source,
        )
        return {"draft": draft}

    @app.post("/api/endpoints/{endpoint_id}/validate")
    async def validate_endpoint_draft(
        endpoint_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        await current.reload_endpoint_config()
        endpoint = current.registry.by_id(endpoint_id)
        if endpoint is None:
            raise RouterError(
                f"unknown endpoint: {endpoint_id}",
                status_code=404,
                code="endpoint_not_found",
            )
        source = request.client.host if request.client else "unknown"
        current.audit.write(
            "endpoint_validation_started",
            endpoint_id=endpoint_id,
            source=source,
        )
        status = await current.health.status(
            endpoint,
            force_refresh=True,
        )
        draft = await current.endpoint_configs.validate_draft(
            endpoint_id,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
            status=status,
            source=source,
        )
        current.audit.write(
            "endpoint_validation_completed",
            endpoint_id=endpoint_id,
            revision=draft["revision"],
            status=draft["validation"]["status"],
            errors=draft["validation"]["errors"],
            source=source,
        )
        return {"draft": draft}

    @app.post("/api/endpoints/{endpoint_id}/activate")
    async def activate_endpoint_draft(
        endpoint_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        source = request.client.host if request.client else "unknown"
        active = await current.endpoint_configs.activate(
            endpoint_id,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
            source=source,
        )
        await current.reload_endpoint_config(force=True)
        current.audit.write(
            "endpoint_activated",
            endpoint_id=endpoint_id,
            revision=active["revision"],
            source=source,
        )
        return {"active": active}

    @app.delete("/api/endpoints/{endpoint_id}/draft")
    async def discard_endpoint_draft(
        endpoint_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        revision = await current.endpoint_configs.discard_draft(
            endpoint_id,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
        )
        current.audit.write(
            "endpoint_draft_discarded",
            endpoint_id=endpoint_id,
            revision=revision,
            source=request.client.host
            if request.client
            else "unknown",
        )
        return {"ok": True, "revision": revision}

    @app.post("/api/endpoints/{endpoint_id}/actions/{action}")
    async def endpoint_action(
        endpoint_id: str,
        action: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        source = request.client.host if request.client else "unknown"
        active = await current.endpoint_configs.action(
            endpoint_id,
            action,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
            source=source,
        )
        await current.reload_endpoint_config(force=True)
        event = {
            "enable": "endpoint_enabled",
            "disable": "endpoint_disabled",
            "auto-enable": "endpoint_auto_enabled",
            "auto-disable": "endpoint_auto_disabled",
        }.get(action, "endpoint_updated")
        current.audit.write(
            event,
            endpoint_id=endpoint_id,
            revision=active["revision"],
            source=source,
        )
        return {"active": active}

    @app.post("/api/endpoints/{endpoint_id}/reset")
    async def reset_endpoint(
        endpoint_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        revision = await current.endpoint_configs.reset(
            endpoint_id,
            expected_revision=_optional_int(
                value.get("expected_revision")
            ),
        )
        await current.reload_endpoint_config(force=True)
        current.audit.write(
            "endpoint_reset",
            endpoint_id=endpoint_id,
            revision=revision,
            source=request.client.host
            if request.client
            else "unknown",
        )
        return {"ok": True, "revision": revision}

    @app.get("/api/clients")
    async def clients(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        return {"clients": await current.clients.list_accounts()}

    @app.post("/api/clients")
    async def create_client(request: Request) -> JSONResponse:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        account = await current.clients.create_account(
            value,
            allowed_models=_allowed_client_models(current),
            public_model_id=_public_model_id(current),
        )
        current.audit.write(
            "client_created",
            client_id=account["id"],
            disclosure_mode=account["disclosure_mode"],
            source=request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {"client": account},
            status_code=201,
        )

    @app.patch("/api/clients/{client_id}")
    async def update_client(
        client_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        account = await current.clients.update_account(
            client_id,
            value,
            allowed_models=_allowed_client_models(current),
            public_model_id=_public_model_id(current),
        )
        current.audit.write(
            "client_updated",
            client_id=client_id,
            enabled=account["enabled"],
            models=account["models"],
            rpm_limit=account["rpm_limit"],
            tpm_limit=account["tpm_limit"],
            max_parallel_requests=account["max_parallel_requests"],
            disclosure_mode=account["disclosure_mode"],
            source=request.client.host if request.client else "unknown",
        )
        if not account["enabled"]:
            current.audit.write(
                "client_disabled",
                client_id=client_id,
                source=(
                    request.client.host
                    if request.client
                    else "unknown"
                ),
            )
        return {"client": account}

    @app.post("/api/clients/{client_id}/keys")
    async def create_client_key(
        client_id: str,
        request: Request,
    ) -> JSONResponse:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        key, plaintext = await current.clients.create_key(
            client_id,
            str(value.get("label", "")),
        )
        current.audit.write(
            "client_key_created",
            client_id=client_id,
            key_id=key["key_id"],
            source=request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            {"key": key, "api_key": plaintext},
            status_code=201,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    @app.post("/api/clients/{client_id}/keys/{key_id}/revoke")
    async def revoke_client_key(
        client_id: str,
        key_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        key = await current.clients.revoke_key(client_id, key_id)
        current.audit.write(
            "client_key_revoked",
            client_id=client_id,
            key_id=key_id,
            source=request.client.host if request.client else "unknown",
        )
        return {"key": key}

    @app.get("/api/route-graph")
    async def route_graph(
        request: Request,
        mode: str = Query(default="simple"),
    ) -> dict[str, Any]:
        _authorized_runtime(request)
        if mode not in {"simple", "detailed"}:
            raise RouterError(
                "mode must be simple or detailed",
                status_code=400,
                code="invalid_graph_mode",
            )
        return graph_document(mode=mode)

    @app.get("/api/route-traces")
    async def route_traces(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
        request_mode: str = Query(default="auto"),
        client_id: str | None = None,
        conversation_id: str | None = None,
        task: str | None = None,
        route_profile: str | None = None,
        selected_model: str | None = None,
        status: str | None = None,
        review_status: str | None = None,
        search: str | None = None,
        node: str | None = None,
        privacy_decision: str | None = None,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        if privacy_decision not in {None, "", "reviewed", "normal", "internal_info", "uncertain"}:
            raise RouterError("invalid privacy decision", status_code=400, code="invalid_trace_filter")
        if request_mode not in {"auto", "explicit", "all"}:
            raise RouterError(
                "request_mode must be auto, explicit, or all",
                status_code=400,
                code="invalid_trace_filter",
            )
        node_value = _query_text(node)
        endpoint_nodes = {
            item.id: item.node
            for item in current.registry.endpoints
        }
        endpoint_ids = (
            tuple(
                item.id
                for item in current.registry.endpoints
                if item.node == node_value
            )
            if node_value
            else None
        )
        payload = await current.route_traces.list(
            limit=limit,
            cursor=cursor,
            request_mode=request_mode,
            client_id=_query_text(client_id),
            conversation_id=_query_text(conversation_id),
            task=_query_text(task),
            route_profile=_query_text(route_profile),
            selected_model=_query_text(selected_model),
            status=_query_text(status),
            review_status=_query_text(review_status),
            search=_query_text(search),
            endpoint_ids=endpoint_ids,
            privacy_decision=privacy_decision,
            auto_models=("auto", str(current.settings.section("identity").get("public_model_id") or "siyuan/auto")),
        )
        cache_rows = await CacheAudit(current.route_traces.database_path).for_requests([x["request_id"] for x in payload["items"]])
        for item in payload["items"]:
            item["cache_audit"] = cache_rows.get(item["request_id"])
            item["node"] = (
                item.get("node")
                or endpoint_nodes.get(item.get("endpoint_id"))
            )
        for summary in payload["conversation_summaries"]:
            summary["latest_node"] = (
                summary.get("latest_node")
                or endpoint_nodes.get(summary.get("latest_endpoint_id"))
            )
        return payload

    @app.get("/api/cache/summary")
    async def cache_summary(request: Request, since: float | None = None, until: float | None = None,
                            client_id: str | None = None, device: str | None = None, client_group: str | None = None,
                            model: str | None = None, conversation_id: str | None = None,
                            status: str | None = None, event: str | None = None):
        current = _authorized_runtime(request)
        result = await CacheAudit(current.route_traces.database_path).query(
            since=since, until=until, client_id=client_id, device=device, model=model, client_group=client_group,
            conversation_id=conversation_id, status=status, event=event)
        return CacheAudit.summarize(result)

    @app.get("/api/cache/requests")
    async def cache_requests(request: Request, since: float | None = None, until: float | None = None,
                             client_id: str | None = None, device: str | None = None, client_group: str | None = None,
                             model: str | None = None, conversation_id: str | None = None,
                             status: str | None = None, event: str | None = None,
                             offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        current = _authorized_runtime(request)
        result = await CacheAudit(current.route_traces.database_path).query(
            since=since, until=until, client_id=client_id, device=device, model=model, client_group=client_group,
            conversation_id=conversation_id, status=status, event=event)
        result["items"] = result["items"][offset:offset+limit]
        result["next_offset"] = offset+limit if offset+limit < result["total"] else None
        return result

    @app.get("/api/route-traces/{request_id}/content")
    async def trace_content(request: Request, request_id: str, stage: str | None = None,
                            offset: int = Query(default=0, ge=0), limit: int = Query(default=16384, ge=1, le=65536)):
        current = _authorized_runtime(request)
        if not await current.route_traces.get(request_id):
            raise RouterError("request not found", status_code=404, code="route_trace_not_found")
        def read():
            reader = ArchiveReader(os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3"),
                                   os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key"))
            return reader.content(request_id, stage, offset, limit)
        try:
            value = await asyncio.to_thread(read)
        except KeyError:
            raise RouterError("stage not available", status_code=404, code="content_stage_not_found")
        except sqlite3.OperationalError:
            # A WAL database may have no -wal/-shm files after the last writer
            # disconnects. A read-only bind mount cannot create these sidecars.
            # Read through an authenticated local API using the same mode=ro
            # reader; never use immutable=1 against a live changing database.
            value = None
            client = getattr(current, "internal_client", None)
            bases = ["http://127.0.0.1:4000"]
            if os.environ.get("AI_ROUTER_TAILSCALE_IP"):
                bases.append("http://" + os.environ["AI_ROUTER_TAILSCALE_IP"] + ":4000")
            if client:
                for base in bases:
                    try:
                        response = await client.get(base + "/internal/request-content/" + quote(request_id, safe=""),
                            params={"offset": offset, "limit": limit, **({"stage": stage} if stage else {})},
                            headers={"Authorization": request.headers.get("authorization", "")}, timeout=5)
                    except Exception:
                        continue
                    if response.status_code == 200:
                        value = response.json()
                        break
                    if response.status_code == 404:
                        raise RouterError("content or stage not archived", status_code=404, code="content_not_archived")
            if value is None:
                raise RouterError("encrypted archive is unavailable", status_code=503, code="content_archive_unavailable")
        except Exception:
            raise RouterError("encrypted archive is unavailable", status_code=503, code="content_archive_unavailable")
        if value is None:
            raise RouterError("content was not archived", status_code=404, code="content_not_archived")
        current.audit.write("admin_content_viewed", request_id=request_id, stage=stage, offset=offset)
        return JSONResponse(value, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/api/route-traces/{request_id}")
    async def route_trace(
        request_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await current.route_traces.get(request_id)
        if value is None:
            raise RouterError(
                "route trace was not found",
                status_code=404,
                code="route_trace_not_found",
            )
        value["cache_audit"] = await CacheAudit(current.route_traces.database_path).detail(value)
        return {"trace": value}

    @app.get("/api/route-traces/{request_id}/diagnosis")
    async def route_trace_diagnosis(
        request_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        trace = await current.route_traces.get(request_id)
        if trace is None:
            raise RouterError(
                "route trace was not found",
                status_code=404,
                code="route_trace_not_found",
            )
        conversation_id = str(trace.get("conversation_id") or "")
        conversation = (
            await current.route_traces.conversation(
                conversation_id,
                limit=500,
            )
            if conversation_id
            else [trace]
        )
        return {
            "diagnosis": diagnose_route(
                trace,
                conversation,
                current.settings,
                current.registry,
            )
        }

    @app.get("/api/conversations/{conversation_id}/control")
    async def get_conversation_control(
        conversation_id: str,
        request: Request,
        client_id: str = Query(min_length=1, max_length=256),
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        return {
            "control": await current.conversation_controls.get(
                client_id,
                conversation_id,
            )
        }

    @app.post("/api/conversations/{conversation_id}/actions/{action}")
    async def conversation_action(
        conversation_id: str,
        action: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        client_id = str(value.get("client_id") or "").strip()[:256]
        if not client_id:
            raise RouterError(
                "client_id is required",
                status_code=400,
                code="invalid_conversation_action",
            )
        reason = (
            str(value.get("reason") or "admin console").strip()[:500]
            or "admin console"
        )
        source = request.client.host if request.client else "unknown"
        before = await current.conversation_controls.get(
            client_id,
            conversation_id,
        )
        try:
            if action == "reset":
                result = await current.conversation_controls.request_reset(
                    client_id=client_id,
                    conversation_id=conversation_id,
                    operator=source,
                    reason=reason,
                )
            elif action == "unpin":
                result = await current.conversation_controls.unpin(
                    client_id,
                    conversation_id,
                )
            elif action == "pin":
                endpoint_id = str(
                    value.get("endpoint_id") or ""
                ).strip()
                endpoint = current.registry.by_id(endpoint_id)
                if (
                    endpoint is None
                    or endpoint.role != "responder"
                    or not endpoint.enabled
                ):
                    raise RouterError(
                        "the pin target is not an enabled responder endpoint",
                        status_code=404,
                        code="conversation_pin_target_not_found",
                    )
                result = await current.conversation_controls.pin(
                    client_id=client_id,
                    conversation_id=conversation_id,
                    endpoint_id=endpoint_id,
                    ttl_seconds=int(value.get("ttl_seconds", 3600)),
                    operator=source,
                    reason=reason,
                )
            else:
                raise RouterError(
                    "action must be reset, pin, or unpin",
                    status_code=400,
                    code="invalid_conversation_action",
                )
        except (TypeError, ValueError) as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_conversation_action",
            ) from exc
        after = await current.conversation_controls.get(
            client_id,
            conversation_id,
        )
        current.audit.write(
            "conversation_control_updated",
            action=action,
            client_id=client_id,
            conversation_id=conversation_id,
            reason=reason,
            before=before,
            after=after,
            source=source,
        )
        return {
            "ok": True,
            "result": result,
            "control": after,
        }

    @app.post("/api/route-traces/{request_id}/privacy-feedback")
    async def privacy_feedback(request_id: str, request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        try:
            feedback = await current.route_traces.add_privacy_feedback(
                request_id, decision=str(value.get("decision", "")),
                note=value.get("note"),
                reviewer_source=request.client.host if request.client else "unknown",
            )
        except ValueError as exc:
            raise RouterError(str(exc), status_code=400, code="invalid_privacy_feedback") from exc
        except KeyError as exc:
            raise RouterError("route trace was not found", status_code=404, code="route_trace_not_found") from exc
        current.audit.write("privacy_feedback", request_id=request_id, **feedback)
        return {"feedback": feedback}

    @app.post("/api/route-traces/{request_id}/reviews")
    async def review_route_trace(
        request_id: str,
        request: Request,
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        value = await _json_body(request)
        try:
            verdict, expected_task, expected_model, note = (
                validate_review(value)
            )
            review = await current.route_traces.add_review(
                request_id,
                verdict=verdict,
                expected_task=expected_task,
                expected_model=expected_model,
                note=note,
                reviewer_source=(
                    request.client.host
                    if request.client
                    else "unknown"
                ),
            )
        except ValueError as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_route_review",
            ) from exc
        except KeyError as exc:
            raise RouterError(
                "route trace was not found",
                status_code=404,
                code="route_trace_not_found",
            ) from exc
        current.audit.write(
            "route_trace_reviewed",
            request_id=request_id,
            verdict=verdict,
            expected_task=expected_task,
            expected_model=expected_model,
            source=(
                request.client.host
                if request.client
                else "unknown"
            ),
        )
        return {"review": review}

    @app.get("/api/dashboard")
    async def dashboard(
        request: Request,
        limit: int = Query(default=40, ge=10, le=100),
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        endpoint_config_revision = await current.reload_endpoint_config()
        endpoints = await _endpoint_values(current, cache_catalog)
        events = current.audit.recent(max(1000, limit * 8))
        requests = _request_rows(events, limit, current.settings.value)
        cloud = await _cloud_budget(current)
        workers = _worker_rows(endpoints)
        router_instances = await current.instance_states()
        control_registry_fingerprint = registry_fingerprint(current.registry)
        router_instances = classify_instances(router_instances, control_registry_fingerprint, now=time.time())
        compared_instances = [item for item in router_instances
                              if item["registry_comparison"] in {"match", "mismatch"}]
        unknown_instances = [item for item in router_instances if item["registry_comparison"] == "unknown"]
        mismatched_registry_instances = [
            str(item.get("instance_id", "unknown"))
            for item in router_instances
            if item["registry_comparison"] == "mismatch"
        ]
        completed = [
            item for item in requests
            if item["status"] in {"succeeded", "failed"}
        ]
        succeeded = [
            item for item in completed
            if item["status"] == "succeeded"
        ]
        active = [
            item for item in requests
            if item["status"] == "running"
        ]
        latencies = [
            float(item["latency_ms"])
            for item in completed
            if item.get("latency_ms") is not None
        ]
        return {
            "generated_at": time.time(),
            "summary": {
                "healthy_endpoints": sum(
                    bool(item["status"]["healthy"]) for item in endpoints
                ),
                "total_endpoints": len(endpoints),
                "ready_workers": sum(
                    bool(
                        item["ready"]
                        and item.get("schedulable", True)
                    )
                    for item in workers
                ),
                "total_workers": len(workers),
                "active_requests": len(active),
                "success_rate": (
                    len(succeeded) / len(completed)
                    if completed
                    else None
                ),
                "average_latency_ms": (
                    sum(latencies) / len(latencies)
                    if latencies
                    else None
                ),
            },
            "endpoints": endpoints,
            "workers": workers,
            "router_instances": router_instances,
            "configuration": {
                "registry_fingerprint": control_registry_fingerprint,
                "endpoint_config_revision": endpoint_config_revision,
                "registry_consistent": (False if mismatched_registry_instances else
                                        True if compared_instances and not unknown_instances else None),
                "registry_compared_instances": len(compared_instances),
                "mismatched_registry_instances": (
                    mismatched_registry_instances
                ),
            },
            "requests": requests,
            "node_distribution": _node_distribution(completed),
            "cloud_budget": cloud,
            "routing_mode": str(
                current.settings.section("routing").get(
                    "provider_priority",
                    "local_first",
                )
            ),
            "alerts": _alerts(
                endpoints,
                workers,
                cloud,
                mismatched_registry_instances=(
                    mismatched_registry_instances
                ),
            ),
        }

    return app


def _runtime(request: Request) -> RouterRuntime:
    return request.app.state.runtime


def _authorized_runtime(request: Request) -> RouterRuntime:
    current = _runtime(request)
    current.auth.authenticate_admin(request.headers.get("authorization"))
    return current


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise RouterError(
            "request body must be valid JSON",
            status_code=400,
            code="invalid_json",
        ) from exc
    if not isinstance(value, dict):
        raise RouterError(
            "request body must be a JSON object",
            status_code=400,
            code="invalid_request",
        )
    return value


def _allowed_client_models(current: RouterRuntime) -> set[str]:
    values = {
        "*",
        "auto",
        *current.base_registry.public_models(),
    }
    identity = IdentityProfile.from_settings(
        current.settings.section("identity")
    )
    if identity.public_model_id:
        values.add(identity.public_model_id)
    return values


def _public_model_id(current: RouterRuntime) -> str:
    return str(
        current.settings.section("identity").get(
            "public_model_id",
            "siyuan/auto",
        )
    ).strip()


def _query_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text[:256] if text else None


def _editable(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key in EDITABLE_SECTIONS and isinstance(item, dict)
    }


async def _endpoint_values(current: RouterRuntime, cache_catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    # Declared architecture is separate from runtime health and cache counters.
    declarations = {
        item["target"]["endpoint_id"]: {
            "strategy": item["strategy"], "layers": item["layers"],
            "verified_at": item["validation"].get("validated_at"),
        }
        for item in (cache_catalog or {}).get("deployments", [])
        if not item["target"].get("worker_id")
    }
    statuses = await current.health.statuses(current.registry.endpoints)
    records = await current.endpoint_configs.records()
    return [
        {
            "endpoint": endpoint.to_dict(),
            "status": statuses[endpoint.id].to_dict(),
            "management": records[endpoint.id],
            "cache_declaration": declarations.get(endpoint.id),
        }
        for endpoint in current.registry.endpoints
    ]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RouterError(
            "expected_revision must be an integer",
            status_code=400,
            code="invalid_endpoint_config",
        ) from exc


def _optional_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _prompt_change_records(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    global_changed = any(
        previous.get(field) != current.get(field)
        for field in ("enabled", "match", "persistence", "fallback")
    )
    previous_routes = previous.get("routes", {})
    current_routes = current.get("routes", {})
    for directive_id in sorted(
        set(previous_routes) | set(current_routes)
    ):
        old = previous_routes.get(directive_id, {})
        new = current_routes.get(directive_id, {})
        fields = [
            field
            for field in ("phrase", "endpoint_id")
            if old.get(field) != new.get(field)
        ]
        if fields or global_changed:
            result.append(
                {
                    "directive_id": directive_id,
                    "fields": ",".join(fields or ["global"]),
                }
            )
    old_reset = previous.get("reset", {})
    new_reset = current.get("reset", {})
    if old_reset.get("phrase") != new_reset.get("phrase") or global_changed:
        result.append(
            {
                "directive_id": "reset",
                "fields": (
                    "phrase"
                    if old_reset.get("phrase") != new_reset.get("phrase")
                    else "global"
                ),
            }
        )
    return result


def _worker_rows(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in endpoints:
        endpoint = item["endpoint"]
        workers = item["status"].get("detail", {}).get("workers", [])
        for worker in workers:
            result.append(
                {
                    "endpoint_id": endpoint["id"],
                    "node": endpoint["node"],
                    "model": endpoint["public_model"],
                    **worker,
                }
            )
    return result


def _request_rows(
    events: list[dict[str, Any]],
    limit: int,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    stale_after = max(
        300.0,
        float(settings.get("queue", {}).get("timeout_seconds", 120)) * 2,
    )
    now = time.time()
    for event in events:
        request_id = str(event.get("request_id", ""))
        if not request_id or request_id in seen:
            continue
        kind = event.get("event")
        if kind not in {
            "request_started",
            "request_completed",
            "request_interrupted_by_restart",
        }:
            continue
        seen.add(request_id)
        status_code = event.get("status_code")
        if kind == "request_interrupted_by_restart":
            status = "stale"
        elif kind == "request_completed":
            status = (
                "succeeded"
                if int(status_code or 500) < 400
                else "failed"
            )
        else:
            status = (
                "stale"
                if now - float(event.get("timestamp", 0)) > stale_after
                else "running"
            )
        rows.append(
            {
                "request_id": request_id,
                "timestamp": float(event.get("timestamp", 0)),
                "status": status,
                "status_code": status_code,
                "client_id": event.get("client_id"),
                "key_id": event.get("key_id"),
                "conversation_id": event.get("conversation_id"),
                "conversation_mode": event.get("conversation_mode"),
                "branch_id": event.get("branch_id"),
                "parent_branch_id": event.get("parent_branch_id"),
                "lineage_relation": event.get("lineage_relation"),
                "context_compacted": event.get(
                    "context_compacted",
                    False,
                ),
                "context_compaction_source": event.get(
                    "context_compaction_source"
                ),
                "requested_model": event.get("requested_model"),
                "selected_model": event.get("selected_model"),
                "endpoint_id": event.get("endpoint_id"),
                "deployment_id": event.get("deployment_id"),
                "deployment_profile_id": event.get(
                    "deployment_profile_id"
                ),
                "deployment_vision_status": event.get(
                    "deployment_vision_status"
                ),
                "image_resizes": event.get("image_resizes", 0),
                "node": event.get("node"),
                "task": event.get("task"),
                "route_profile": event.get("route_profile"),
                "complexity": event.get("complexity"),
                "strategy_version": event.get("strategy_version"),
                "history_mode": event.get("history_mode"),
                "context_required": event.get("context_required"),
                "remote_fallback_position": event.get(
                    "remote_fallback_position"
                ),
                "reason": event.get("reason"),
                "affinity": event.get("affinity"),
                "prompt_tokens": event.get("prompt_tokens"),
                "output_reserve_tokens": event.get(
                    "output_reserve_tokens"
                ),
                "attempts": event.get("attempts"),
                "capacity_attempts": event.get("capacity_attempts"),
                "queue_wait_ms": event.get("queue_wait_ms"),
                "cached_prompt_tokens": event.get(
                    "cached_prompt_tokens"
                ),
                "cache_hit_ratio": event.get("cache_hit_ratio"),
                "latency_ms": event.get("latency_ms"),
                "required_capabilities": event.get(
                    "required_capabilities",
                    [],
                ),
                "tool_history_repairs": event.get(
                    "tool_history_repairs",
                    0,
                ),
                "protocol": event.get("protocol"),
                "native_or_adapter": event.get("native_or_adapter"),
                "candidate_rejections": event.get(
                    "candidate_rejections",
                    [],
                ),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _node_distribution(
    requests: list[dict[str, Any]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for request in requests:
        node = str(request.get("node") or "unknown")
        result[node] = result.get(node, 0) + 1
    return result


async def _cloud_budget(current: RouterRuntime) -> dict[str, Any]:
    cloud = current.settings.section("cloud")
    monthly_budget = float(cloud.get("monthly_budget", 0))
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    ledger = await current.store.get_json(
        f"router:cloud-budget:{month}"
    ) or {}
    spent = float(ledger.get("spent_usd", 0))
    reservations = ledger.get("reservations", {})
    reserved = sum(
        float(item.get("amount_usd", 0))
        for item in reservations.values()
        if isinstance(item, dict)
    )
    return {
        "month": month,
        "enabled": bool(cloud.get("enabled", False)),
        "auto_escalate": bool(cloud.get("auto_escalate", False)),
        "monthly_budget_usd": monthly_budget,
        "spent_usd": spent,
        "reserved_usd": reserved,
        "remaining_usd": max(0.0, monthly_budget - spent - reserved),
        "usage_ratio": (
            min(1.0, (spent + reserved) / monthly_budget)
            if monthly_budget > 0
            else 0.0
        ),
    }


def _alerts(
    endpoints: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    cloud: dict[str, Any],
    *,
    mismatched_registry_instances: list[str] | None = None,
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if mismatched_registry_instances:
        alerts.append(
            {
                "level": "critical",
                "title": "管理面与数据面注册表不一致",
                "detail": (
                    "以下 Router API 实例使用了不同的注册表："
                    + "、".join(mismatched_registry_instances)
                ),
            }
        )
    for item in endpoints:
        if item["status"]["healthy"]:
            continue
        endpoint = item["endpoint"]
        detail = item["status"].get("detail", {})
        alerts.append(
            {
                "level": "critical",
                "title": f"{endpoint['node']} 节点不可用",
                "detail": str(
                    detail.get("error")
                    or f"健康检查返回 {detail.get('status_code', '未知状态')}"
                ),
            }
        )
    unavailable_workers = [
        item for item in workers
        if (
            not item.get("ready")
            or not item.get("schedulable", True)
            or item.get("state") != "available"
        )
    ]
    if unavailable_workers:
        alerts.append(
            {
                "level": "warning",
                "title": "本地模型池容量下降",
                "detail": (
                    f"{len(unavailable_workers)} 个物理 worker "
                    "当前不可调度"
                ),
            }
        )
    drifted_workers = [
        item for item in workers
        if item.get("config_drift")
    ]
    if drifted_workers:
        alerts.append(
            {
                "level": "critical",
                "title": "物理节点配置不一致",
                "detail": (
                    f"{len(drifted_workers)} 个 worker "
                    "已从调度中隔离"
                ),
            }
        )
    if cloud["monthly_budget_usd"] > 0 and cloud["usage_ratio"] >= 0.8:
        alerts.append(
            {
                "level": "warning",
                "title": "云端预算接近上限",
                "detail": (
                    f"本月已使用 {cloud['usage_ratio'] * 100:.1f}%"
                ),
            }
        )
    return alerts


app = create_app()
