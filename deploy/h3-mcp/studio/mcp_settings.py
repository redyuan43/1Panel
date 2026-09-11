from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


def administrator(request):
    local = request.client is not None and request.client.host in {"127.0.0.1", "::1"}
    try:
        key = Path(os.environ["H3_STUDIO_KEY_FILE"]).read_text().strip()
    except (KeyError, OSError):
        key = ""
    relay = local and bool(key) and hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key)
    users = {item.strip() for item in os.environ.get("H3_MCP_ADMIN_USERS", "").split(",") if item.strip()}
    tailnet = local and request.headers.get("tailscale-user-login", "") in users
    if not relay and not tailnet:
        return False
    origin = request.headers.get("origin")
    return not origin or relay or origin == os.environ.get("H3_STUDIO_PUBLIC_ORIGIN", "")


def install_mcp_settings(app):
    api = APIRouter()

    @api.api_route("/api/mcp-admin/{action}", methods=["GET", "POST"])
    async def manage(action: str, request: Request):
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}
        if not administrator(request):
            return JSONResponse({"detail": "仅工作室管理员可管理 MCP，请从 OnePanel H3 入口或已授权的 Tailscale 身份访问。"}, status_code=403, headers=headers)
        if (request.method, action) not in {("GET", "status"), ("POST", "issue"), ("POST", "revoke"), ("POST", "policy"), ("GET", "backend"), ("POST", "backend")}:
            return JSONResponse({"detail": "未知管理操作"}, status_code=404, headers=headers)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({"detail": "管理请求过大"}, status_code=413, headers=headers)
        try:
            if action == "backend":
                connector = getattr(app.state, "h3_connector", None)
                if connector is None or not getattr(connector.module.COMFY, "is_fleet", False):
                    return JSONResponse({"detail": "Fleet 后台管理尚未接入"}, status_code=503, headers=headers)
                payload = json.loads(body) if request.method == "POST" else None
                value = await run_in_threadpool(connector.module.COMFY._request, request.method,
                    "/api/router/backend-lifecycle", payload, timeout=10)
                return JSONResponse(value, headers=headers)
            secret = Path(os.environ["H3_CONNECTOR_KEY_FILE"]).read_text().strip()
            if len(secret) < 32:
                raise ValueError()
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False,
                    transport=getattr(app.state, "mcp_management_transport", None)) as client:
                response = await client.request(request.method, "http://127.0.0.1:4001/internal/h3-mcp-management/" + action,
                    content=bytes(body), headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
            value = response.json()
            if response.status_code == 200 and action == "status":
                value["studio"] = {"healthy": True, "writes_configured": os.environ.get("H3_CONNECTOR_WRITES_ENABLED") == "true",
                    "generation_configured": os.environ.get("H3_CONNECTOR_GENERATION_ENABLED") == "true"}
                value["writes_enabled"] = value["writes_enabled"] and value["studio"]["writes_configured"]
                value["generation_enabled"] = value["generation_enabled"] and value["studio"]["generation_configured"] and value["writes_enabled"]
            return JSONResponse(value, status_code=response.status_code, headers=headers)
        except (httpx.HTTPError, KeyError, OSError, ValueError):
            return JSONResponse({"detail": "管理服务不可达或结果未知。发放密钥失败时先刷新列表，不重复提交。"}, status_code=503, headers=headers)

    app.include_router(api)
