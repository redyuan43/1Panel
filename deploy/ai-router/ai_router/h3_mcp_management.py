from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from .errors import RouterError


PREFIX = "/internal/h3-mcp-management"
POLICY = "router:h3-management:policy"
CLIENT = "workbuddy-ivan-h3"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


async def record(runtime, owner, kind, **fields):
    try:
        await runtime.store.set_json("router:h3-management:client:" + owner + ":" + kind,
                                     {"at": time.time(), **fields}, ttl_seconds=30 * 86400)
    except Exception:
        pass


async def permits(runtime, name):
    policy = await runtime.store.get_json(POLICY)
    return policy is None or policy.get(name) is True


def install_management(app):
    if os.environ.get("AI_ROUTER_H3_MCP_MANAGEMENT_ENABLED") != "true":
        return

    def authorize(request):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise RouterError("仅允许工作室管理入口", status_code=403)
        try:
            secret = Path(os.environ["AI_ROUTER_H3_CONNECTOR_KEY_FILE"]).read_text().strip()
        except (KeyError, OSError):
            secret = ""
        if len(secret) < 32 or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
            raise RouterError("需要管理员身份", status_code=401)
        if request.headers.get("origin"):
            raise RouterError("不接受浏览器直接管理请求", status_code=403)
        return request.app.state.runtime

    async def account(runtime):
        value = await runtime.clients._required_account(CLIENT)
        if value.get("models") != [] or value.get("media_models") != ["siyuan-video"]:
            raise RouterError("专用账号权限不符合最小授权，停止发放凭证", status_code=409)
        return value

    async def heartbeat():
        instance = os.environ.get("AI_ROUTER_H3_MCP_INSTANCE_ID", "unidentified")
        runtime = app.state.runtime
        while True:
            try:
                await runtime.store.set_json("router:h3-management:instance:" + instance, {
                    "instance": instance, "at": time.time(),
                    "release": os.environ.get("AI_ROUTER_H3_MCP_RELEASE", "unknown"),
                    "writes_configured": os.environ.get("AI_ROUTER_H3_MCP_WRITES_ENABLED") == "true",
                    "generation_configured": os.environ.get("AI_ROUTER_H3_MCP_GENERATION_ENABLED") == "true",
                    "draining": bool(getattr(runtime, "draining", False)),
                }, ttl_seconds=120)
            except Exception:
                pass
            await asyncio.sleep(15)

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(current):
        async with original(current):
            task = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan

    @app.get(PREFIX + "/status")
    async def status(request: Request):
        runtime = authorize(request)
        value = await account(runtime)
        instances = []
        for identity in ("control-local", "control-tail"):
            sample = await runtime.store.get_json("router:h3-management:instance:" + identity)
            age = time.time() - sample["at"] if sample else None
            instances.append({**(sample or {"instance": identity}), "healthy": age is not None and 0 <= age < 45})
        policy = await runtime.store.get_json(POLICY) or {"revision": "initial", "writes": True, "generation": True}
        keys = await runtime.clients._keys_for(CLIENT)
        return JSONResponse({"endpoint": os.environ.get("AI_ROUTER_H3_MCP_PUBLIC_ORIGIN", "") + "/mcp/h3",
            "instances": instances, "client": {"id": CLIENT, "enabled": value["enabled"], "media_models": value["media_models"]},
            "keys": keys, "policy": policy,
            "writes_enabled": policy.get("writes") is True and all(item.get("healthy") and item.get("writes_configured") and not item.get("draining") for item in instances),
            "generation_enabled": policy.get("generation") is True and policy.get("writes") is True and all(item.get("healthy") and item.get("generation_configured") and item.get("writes_configured") and not item.get("draining") for item in instances),
            "last_authentication": await runtime.store.get_json("router:h3-management:client:" + CLIENT + ":authentication"),
            "last_tool": await runtime.store.get_json("router:h3-management:client:" + CLIENT + ":tool"),
            "connection_note": "仅记录服务端观察到的认证与调用；无持续连接不代表离线，客户端名称不作为身份凭据。"}, headers=NO_STORE)

    @app.post(PREFIX + "/{action}")
    async def mutate(action: str, request: Request):
        runtime = authorize(request)
        if action not in {"issue", "revoke", "policy"}:
            raise RouterError("未知管理操作", status_code=404)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise RouterError("请求过大", status_code=413)
        try:
            value = json.loads(raw)
        except ValueError:
            raise RouterError("无效的管理请求", status_code=400)
        allowed = {"operation_id", "label"} if action == "issue" else {"operation_id", "key_id"} if action == "revoke" else {"operation_id", "revision", "writes", "generation"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise RouterError("无效的管理参数", status_code=400)
        operation = value.get("operation_id")
        try:
            if str(UUID(operation)) != operation:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise RouterError("需要有效的操作编号", status_code=400)
        await account(runtime)
        lock = uuid4().hex
        if not await runtime.store.acquire_lock("router:h3-management:mutation", lock, 60):
            raise RouterError("管理操作进行中，请查询后再操作", status_code=409)
        try:
            receipt_key = "router:h3-management:operation:" + operation
            previous = await runtime.store.get_json(receipt_key)
            if previous:
                raise RouterError("此操作已处理或结果待核实；请刷新密钥列表，不重复发放", status_code=409)
            if action == "issue":
                label = value.get("label", "WorkBuddy H3")
                if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
                    raise RouterError("密钥名称长度应为1至80", status_code=400)
                if len([key for key in await runtime.clients._keys_for(CLIENT) if key["status"] == "active"]) >= 5:
                    raise RouterError("最多保留五个有效密钥，请先撤销不用的密钥", status_code=409)
            elif action == "revoke":
                if not isinstance(value.get("key_id"), str) or value["key_id"] not in {key["key_id"] for key in await runtime.clients._keys_for(CLIENT)}:
                    raise RouterError("密钥不属于本账号", status_code=404)
            else:
                current = await runtime.store.get_json(POLICY) or {"revision": "initial"}
                if value.get("revision") != current.get("revision", "invalid"):
                    raise RouterError("设置已改变，请刷新", status_code=409)
                if type(value.get("writes")) is not bool or type(value.get("generation")) is not bool:
                    raise RouterError("开关必须是布尔值", status_code=400)
                if value["generation"] and (not value["writes"] or os.environ.get("AI_ROUTER_H3_MCP_GENERATION_ENABLED") != "true"):
                    raise RouterError("生成未通过发布开放条件，不能从页面绕过", status_code=409)
            await runtime.store.set_json(receipt_key, {"state": "started", "action": action, "at": time.time()})
            if action == "issue":
                key, token = await runtime.clients.create_key(CLIENT, label.strip())
                result = {"key": key, "api_key": token, "display_once": True}
                summary = {"key_id": key["key_id"]}
            elif action == "revoke":
                key = await runtime.clients.revoke_key(CLIENT, value["key_id"])
                result = {"key": key}
                summary = {"key_id": key["key_id"]}
            else:
                result = {"revision": uuid4().hex, "writes": value["writes"], "generation": value["generation"]}
                await runtime.store.set_json(POLICY, result)
                summary = result
            await runtime.store.set_json(receipt_key, {"state": "completed", "action": action, **summary})
            runtime.audit.write("h3_management_" + action, client_id=CLIENT, operation_id=operation, **summary)
            return JSONResponse(result, headers=NO_STORE)
        finally:
            await runtime.store.release_lock("router:h3-management:mutation", lock)
