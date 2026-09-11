from __future__ import annotations

import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.routing import Route

from .errors import RouterError
from .h3_mcp_management import install_management, permits, record
from .h3_mcp_assets import routes as asset_routes


PREFIX = "/mcp/h3"
MAX_BODY = 128 * 1024
READ_TOOLS = {"h3_capabilities", "h3_prompt_guidance", "h3_list_tasks", "h3_get_task"}
TOOLS = READ_TOOLS | {"h3_save_draft", "h3_confirm_prompt", "h3_start_preview", "h3_review_preview", "h3_cancel_task"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def enabled(name):
    return os.environ.get(name, "false").lower() == "true"


def studio_configuration():
    base = os.environ.get("AI_ROUTER_H3_STUDIO_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise RouterError("工作室内部连接配置不可用", status_code=503, code="h3_configuration")
    try:
        key = Path(os.environ["AI_ROUTER_H3_CONNECTOR_KEY_FILE"]).read_text().strip()
    except (KeyError, OSError):
        key = ""
    if len(key) < 32:
        raise RouterError("工作室内部凭证不可用", status_code=503, code="h3_configuration")
    return base, key


def public_origin():
    origin = os.environ.get("AI_ROUTER_H3_MCP_PUBLIC_ORIGIN", "https://ai-x10drg.taild500c8.ts.net:4001").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("H3 MCP public origin must be HTTPS")
    return origin


def add_links(value):
    origin = os.environ.get("AI_ROUTER_H3_STUDIO_PUBLIC_ORIGIN", "https://ai-x10drg.taild500c8.ts.net:8445").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.path or parsed.query or parsed.fragment:
        raise RouterError("预览入口配置不可用", status_code=503, code="h3_configuration")
    task_id = value.get("task_id")
    if isinstance(task_id, str) and IDENTIFIER.fullmatch(task_id):
        value["view_url"] = f"{origin}/?project={task_id}&stage=preview"
        value["preview_access"] = "使用现有 Studio / Tailscale 浏览器身份；链接不含凭证。"
        preview = value.get("preview") or {}
        output_id = preview.get("output_id")
        if isinstance(output_id, str) and IDENTIFIER.fullmatch(output_id):
            value["download_url"] = f"{origin}/api/projects/{task_id}/connector-outputs/{output_id}"
    for task in value.get("tasks", []):
        if isinstance(task, dict):
            add_links(task)
    return value


class H3Gateway:
    def __init__(self, host_app):
        self.host_app = host_app

    async def authenticate(self, request):
        runtime = self.host_app.state.runtime
        client = await runtime.auth.authenticate(request.headers.get("authorization"))
        allowed = {entry.strip() for entry in os.environ.get("AI_ROUTER_H3_MCP_CLIENTS", "").split(",") if entry.strip()}
        if client.policy.id not in allowed or "siyuan-video" not in client.policy.media_models:
            raise RouterError("此账号没有 H3 连接器权限", status_code=403, code="h3_forbidden")
        if not IDENTIFIER.fullmatch(client.policy.id):
            raise RouterError("客户端身份无效", status_code=403, code="h3_forbidden")
        return client

    async def call(self, owner, name, arguments):
        if name not in TOOLS:
            raise RouterError("未知 H3 工具", code="unknown_tool")
        if name not in READ_TOOLS and name != "h3_cancel_task":
            if (not enabled("AI_ROUTER_H3_MCP_WRITES_ENABLED") or getattr(self.host_app.state.runtime, "draining", False)
                    or not await permits(self.host_app.state.runtime, "writes")):
                raise RouterError("连接器暂停新操作；现有任务不受影响", status_code=503, code="h3_writes_paused")
        if name == "h3_start_preview" and (not enabled("AI_ROUTER_H3_MCP_GENERATION_ENABLED")
                or not await permits(self.host_app.state.runtime, "generation")):
            raise RouterError("当前仅允许草稿验收，视频生成入口关闭", status_code=503, code="h3_generation_paused")
        base, key = studio_configuration()
        transport = getattr(self.host_app.state, "h3_mcp_transport", None)
        try:
            async with httpx.AsyncClient(base_url=base, timeout=25, trust_env=False, follow_redirects=False,
                                         transport=transport) as client:
                response = await client.post("/api/router/connector/call/" + name, json=arguments,
                                             headers={"Authorization": "Bearer " + key, "X-H3-Connector-Owner": owner})
            if response.status_code >= 300:
                status = response.status_code if response.status_code in {400, 401, 403, 404, 409, 413, 422, 503} else 502
                messages = {409: "任务版本已变化或操作冲突，请查询当前任务后再确认", 404: "任务或产物不存在，或不属于本账号",
                            422: "提示词、配方或参数不符合本轮规格", 503: "工作室暂不可执行，请查询状态，不要创建替代任务"}
                raise RouterError(messages.get(status, "工作室拒绝此操作，请核对参数或联系管理员"),
                                  status_code=status, code="h3_contract_rejected")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid result")
            if name == "h3_capabilities":
                value["writes_enabled"] = (value.get("writes_enabled") is True
                    and enabled("AI_ROUTER_H3_MCP_WRITES_ENABLED")
                    and await permits(self.host_app.state.runtime, "writes")
                    and not getattr(self.host_app.state.runtime, "draining", False))
                value["generation_enabled"] = (value.get("generation_enabled") is True
                    and value["writes_enabled"] and enabled("AI_ROUTER_H3_MCP_GENERATION_ENABLED")
                    and await permits(self.host_app.state.runtime, "generation"))
                value["poll_interval_seconds"] = 10
        except (httpx.HTTPError, ValueError) as error:
            raise RouterError("调用结果未知；先查询同一任务，重试必须保留原 operation_id，不要新建替代任务",
                              status_code=503, code="h3_outcome_unknown") from error
        return add_links(value)

    async def output(self, request):
        try:
            client_account = await self.authenticate(request)
            identifiers = [request.path_params[part] for part in ("task_id", "output_id")]
            if not all(IDENTIFIER.fullmatch(identifier) for identifier in identifiers):
                raise RouterError("无效的产物编号", status_code=400)
            base, key = studio_configuration()
            headers = {"Authorization": "Bearer " + key, "X-H3-Connector-Owner": client_account.policy.id,
                       "Accept-Encoding": "identity"}
            for header in ("range", "if-range"):
                if request.headers.get(header):
                    headers[header] = request.headers[header]
            upstream = httpx.AsyncClient(base_url=base, timeout=25, trust_env=False, follow_redirects=False,
                                        transport=getattr(self.host_app.state, "h3_mcp_transport", None))
            try:
                response = await upstream.send(upstream.build_request(request.method,
                    f"/api/router/connector/tasks/{identifiers[0]}/outputs/{identifiers[1]}", headers=headers), stream=True)
            except BaseException:
                await upstream.aclose()
                raise
            if response.status_code not in {200, 206, 416}:
                await response.aclose()
                await upstream.aclose()
                raise RouterError("产物不可访问", status_code=404)

            async def close_upstream():
                await response.aclose()
                await upstream.aclose()

            async def chunks():
                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                finally:
                    await close_upstream()

            outgoing = {name: response.headers[name] for name in
                        ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified")
                        if name in response.headers}
            outgoing.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            return StreamingResponse(chunks(), status_code=response.status_code, headers=outgoing,
                                     background=BackgroundTask(close_upstream))
        except RouterError as error:
            return JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status_code)
        except httpx.HTTPError:
            return JSONResponse({"error": {"code": "h3_unavailable", "message": "产物暂不可访问"}}, status_code=503)


class AuthenticatedMCP:
    def __init__(self, app, gateway):
        self.app, self.gateway = app, gateway

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        try:
            origin = request.headers.get("origin")
            if origin and origin != public_origin():
                raise RouterError("不允许跨来源调用", status_code=403, code="h3_origin_rejected")
            principal = await self.gateway.authenticate(request)
            runtime = self.gateway.host_app.state.runtime
            await record(runtime, principal.policy.id, "authentication")
            count = await runtime.store.increment_window("router:h3-mcp:" + principal.policy.id, 1, 60)
            if count > min(60, principal.policy.rpm_limit):
                raise RouterError("调用过于频繁，请稍后再试", status_code=429, code="h3_rate_limited")
            scope.setdefault("state", {})["h3_owner"] = principal.policy.id
            scope["state"]["h3_request_id"] = uuid4().hex
            if request.method == "POST" and request.url.path != "/mcp/h3/assets/uploads":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise RouterError("请求体过大", status_code=413, code="h3_request_too_large")
                original_receive = receive
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = bounded_receive
        except RouterError as error:
            response = JSONResponse({"error": {"code": error.code, "message": str(error)}}, status_code=error.status_code,
                                    headers={"Cache-Control": "no-store", **({"WWW-Authenticate": "Bearer"} if error.status_code == 401 else {})})
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def install_h3_mcp(app):
    if not enabled("AI_ROUTER_H3_MCP_ENABLED"):
        return
    from jsonschema import Draft202012Validator
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import CallToolResult, TextContent, Tool

    schema_path = Path(os.environ.get("AI_ROUTER_H3_MCP_SCHEMA_FILE", str(Path(__file__).with_name("h3_mcp_schema.json"))))
    definitions = json.loads(schema_path.read_text())
    schemas = {entry["name"]: entry for entry in definitions}
    if set(schemas) != TOOLS or len(definitions) != len(TOOLS):
        raise ValueError("H3 MCP tool schema mismatch")
    validators = {}
    for name, definition in schemas.items():
        Draft202012Validator.check_schema(definition["inputSchema"])
        validators[name] = Draft202012Validator(definition["inputSchema"])
    gateway = H3Gateway(app)
    server = Server("H3 工作室", version="2.0.0",
                    instructions="先核对能力和素材实际绑定，再展示提示词并等待确认；确认不等于启动。图片用途必须明确，不能降级为纯文本。成片等待人工评价。")

    @server.list_tools()
    async def list_tools():
        return [Tool.model_validate(entry) for entry in definitions]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        context = server.request_context
        arguments = arguments or {}
        started = time.monotonic()
        owner = context.request.state.h3_owner
        request_id = context.request.state.h3_request_id
        outcome = "failed"
        try:
            if name not in schemas or not validators[name].is_valid(arguments):
                raise RouterError("工具参数不符合接口要求，请按工具参数说明调用", code="invalid_tool_arguments")
            value = await gateway.call(owner, name, arguments)
            outcome = "completed"
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                                  structuredContent=value)
        except RouterError as error:
            value = {"status": "needs_context" if error.status_code in {400, 409, 422} else "failed",
                     "error": {"code": error.code, "message": str(error)}, "request_id": request_id}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                                  structuredContent=value, isError=True)
        finally:
            await record(app.state.runtime, owner, "tool", tool=name if name in TOOLS else "unknown", outcome=outcome)
            app.state.runtime.audit.write("h3_mcp_tool", client_id=owner, request_id=request_id,
                tool=name if name in TOOLS else "unknown", outcome=outcome,
                elapsed_seconds=round(time.monotonic() - started, 3),
                operation_id=arguments.get("operation_id") if isinstance(arguments, dict) else None)

    host = urlsplit(public_origin()).netloc
    security = TransportSecuritySettings(allowed_hosts=[host, "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
                                         allowed_origins=[public_origin()])
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True, security_settings=security)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    mcp_app = Starlette(routes=[*asset_routes(gateway, studio_configuration, enabled),
        Route("/h3/tasks/{task_id}/outputs/{output_id}", gateway.output, methods=["GET", "HEAD"]),
        Route("/h3", Endpoint(), methods=["GET", "POST", "DELETE"]),
    ])
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(current_app):
        async with original_lifespan(current_app):
            async with manager.run():
                yield

    app.router.lifespan_context = lifespan
    app.mount("/mcp", AuthenticatedMCP(mcp_app, gateway))
    install_management(app)
