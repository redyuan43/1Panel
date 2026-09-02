from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .errors import RouterError
from .runtime import RouterRuntime, build_runtime


STATIC_DIR = Path(__file__).resolve().parent / "static"
EDITABLE_SECTIONS = {
    "affinity",
    "cloud",
    "compaction",
    "evaluator",
    "failover",
    "health",
    "queue",
    "routing",
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
        title="1Panel AI Router Control",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

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
        try:
            current.settings.write_runtime(override)
        except (TypeError, ValueError) as exc:
            raise RouterError(
                str(exc),
                status_code=400,
                code="invalid_settings",
            ) from exc
        current.reload_settings()
        current.audit.write(
            "settings_updated",
            sections=sorted(override),
            source=request.client.host if request.client else "unknown",
        )
        return {
            "ok": True,
            "settings": _editable(current.settings.value),
        }

    @app.get("/api/endpoints")
    async def endpoints(request: Request) -> dict[str, Any]:
        current = _authorized_runtime(request)
        return {"endpoints": await _endpoint_values(current)}

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
        )
        current.audit.write(
            "client_created",
            client_id=account["id"],
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
        )
        current.audit.write(
            "client_updated",
            client_id=client_id,
            enabled=account["enabled"],
            models=account["models"],
            rpm_limit=account["rpm_limit"],
            tpm_limit=account["tpm_limit"],
            max_parallel_requests=account["max_parallel_requests"],
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

    @app.get("/api/dashboard")
    async def dashboard(
        request: Request,
        limit: int = Query(default=40, ge=10, le=100),
    ) -> dict[str, Any]:
        current = _authorized_runtime(request)
        current.reload_settings()
        endpoints = await _endpoint_values(current)
        events = current.audit.recent(max(1000, limit * 8))
        requests = _request_rows(events, limit, current.settings.value)
        cloud = await _cloud_budget(current)
        workers = _worker_rows(endpoints)
        router_instances = await current.instance_states()
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
                    bool(item["ready"]) for item in workers
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
            "requests": requests,
            "node_distribution": _node_distribution(completed),
            "cloud_budget": cloud,
            "routing_mode": str(
                current.settings.section("routing").get(
                    "provider_priority",
                    "local_first",
                )
            ),
            "alerts": _alerts(endpoints, workers, cloud),
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
    return {"*", "auto", *current.registry.public_models()}


def _editable(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key in EDITABLE_SECTIONS and isinstance(item, dict)
    }


async def _endpoint_values(current: RouterRuntime) -> list[dict[str, Any]]:
    statuses = await current.health.statuses(current.registry.endpoints)
    return [
        {
            "endpoint": endpoint.to_dict(),
            "status": statuses[endpoint.id].to_dict(),
        }
        for endpoint in current.registry.endpoints
    ]


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
                "requested_model": event.get("requested_model"),
                "selected_model": event.get("selected_model"),
                "endpoint_id": event.get("endpoint_id"),
                "deployment_id": event.get("deployment_id"),
                "node": event.get("node"),
                "task": event.get("task"),
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
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
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
        if not item.get("ready") or item.get("state") != "available"
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
