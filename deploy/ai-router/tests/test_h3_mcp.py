from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_router.errors import AuthenticationError
from ai_router.h3_mcp import H3Gateway, TOOLS, add_links, install_h3_mcp, studio_configuration
from ai_router.store import InMemoryStateStore


pytest.importorskip("mcp")
SCHEMA_SOURCE = Path(__file__).resolve().parents[2] / "h3-mcp/studio/connector_api.py"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    key = tmp_path / "internal-key"
    key.write_text("internal-test-only-" * 4)
    spec = importlib.util.spec_from_file_location("h3_test_schema", SCHEMA_SOURCE)
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(source.TOOL_DEFINITIONS))
    for name, value in {
        "AI_ROUTER_H3_MCP_ENABLED": "true", "AI_ROUTER_H3_MCP_CLIENTS": "alice,bob",
        "AI_ROUTER_H3_MCP_WRITES_ENABLED": "true", "AI_ROUTER_H3_MCP_GENERATION_ENABLED": "false",
        "AI_ROUTER_H3_STUDIO_URL": "http://127.0.0.1:14830", "AI_ROUTER_H3_CONNECTOR_KEY_FILE": str(key),
        "AI_ROUTER_H3_MCP_SCHEMA_FILE": str(schema),
    }.items():
        monkeypatch.setenv(name, value)
    calls, audit = [], []

    async def authenticate(authorization):
        if authorization not in {"Bearer alice", "Bearer bob", "Bearer no-video", "Bearer outsider"}:
            raise AuthenticationError()
        owner = authorization.split()[1]
        policy = SimpleNamespace(id=owner, media_models=[] if owner == "no-video" else ["siyuan-video"], rpm_limit=60)
        return SimpleNamespace(policy=policy, key_id="test-key-id")

    runtime = SimpleNamespace(auth=SimpleNamespace(authenticate=authenticate), store=InMemoryStateStore(),
                              draining=False, audit=SimpleNamespace(write=lambda *args, **kwargs: audit.append(kwargs)))
    app = FastAPI()
    app.state.runtime = runtime

    async def upstream(request):
        calls.append(request)
        if request.url.path.endswith("h3_capabilities"):
            return httpx.Response(200, json={"recipes": ["A4", "A4_C0", "A4_C1", "B8"], "available_slots": 0,
                                            "admission_reason": "memory_pressure"})
        return httpx.Response(200, json={"task_id": "abc123", "status": "awaiting_approval", "revision": 1})

    app.state.h3_mcp_transport = httpx.MockTransport(upstream)
    install_h3_mcp(app)
    with TestClient(app, base_url="http://localhost") as client:
        yield SimpleNamespace(client=client, calls=calls, app=app, runtime=runtime, audit=audit, key=key)


def rpc(setup, method, params=None, owner="alice"):
    return setup.client.post("/mcp/h3", headers={"Authorization": "Bearer " + owner,
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})})


def test_initialize_list_and_readonly_capacity(setup):
    response = rpc(setup, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "WorkBuddy-test", "version": "1"}})
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"
    listed = rpc(setup, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == TOOLS
    assert not setup.calls
    result = rpc(setup, "tools/call", {"name": "h3_capabilities", "arguments": {}}).json()["result"]
    assert result["structuredContent"]["available_slots"] == 0
    assert result["structuredContent"]["admission_reason"] == "memory_pressure"
    assert not result.get("isError")
    assert len(setup.calls) == 1
    assert setup.calls[0].headers["x-h3-connector-owner"] == "alice"
    assert setup.calls[0].headers["authorization"] == "Bearer " + setup.key.read_text()
    assert "internal-test-only" not in json.dumps(result)


@pytest.mark.parametrize("owner,code", [("missing", 401), ("no-video", 403), ("outsider", 403)])
def test_auth_denies_before_tool_discovery(setup, owner, code):
    assert rpc(setup, "tools/list", owner=owner).status_code == code
    assert not setup.calls


def test_cross_origin_and_host_rejected(setup):
    headers = {"Authorization": "Bearer alice", "Accept": "application/json, text/event-stream"}
    response = setup.client.post("/mcp/h3", headers={**headers, "Origin": "https://evil.example"}, json={})
    assert response.status_code == 403
    response = setup.client.post("/mcp/h3", headers={**headers, "Host": "evil.example"}, json={})
    assert response.status_code == 421
    assert not setup.calls


def test_clients_are_separated_without_shared_mcp_session(setup):
    for owner in ("alice", "bob", "alice"):
        response = rpc(setup, "tools/call", {"name": "h3_capabilities", "arguments": {}}, owner=owner)
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers
    assert [request.headers["x-h3-connector-owner"] for request in setup.calls] == ["alice", "bob", "alice"]


@pytest.mark.parametrize("name,args", [("shell", {}), ("h3_capabilities", {"owner": "bob"}),
                                     ("h3_start_preview", {}), ("h3_save_draft", {"duration": 25})])
def test_unknown_or_invalid_tools_never_forwarded(setup, name, args):
    result = rpc(setup, "tools/call", {"name": name, "arguments": args}).json()["result"]
    assert result["isError"] is True
    assert not setup.calls


def test_generation_gate_and_write_gate(monkeypatch, setup):
    gateway = H3Gateway(setup.app)
    with pytest.raises(Exception, match="生成入口关闭"):
        asyncio.run(gateway.call("alice", "h3_start_preview", {}))
    monkeypatch.setenv("AI_ROUTER_H3_MCP_WRITES_ENABLED", "false")
    with pytest.raises(Exception, match="暂停新操作"):
        asyncio.run(gateway.call("alice", "h3_save_draft", {}))
    asyncio.run(gateway.call("alice", "h3_cancel_task", {}))
    assert len(setup.calls) == 1
    assert setup.calls[0].url.path.endswith("h3_cancel_task")


@pytest.mark.parametrize("failure", ["timeout", "redirect", "error", "invalid_json"])
def test_unknown_outcome_no_automatic_retry_or_sensitive_error_echo(setup, failure):
    async def upstream(request):
        setup.calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret=should-not-leak")
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://evil.example"})
        if failure == "error":
            return httpx.Response(500, json={"detail": "secret=should-not-leak"})
        return httpx.Response(200, content="secret=should-not-leak")
    setup.app.state.h3_mcp_transport = httpx.MockTransport(upstream)
    result = rpc(setup, "tools/call", {"name": "h3_capabilities", "arguments": {}}).json()["result"]
    assert result["isError"]
    assert "should-not-leak" not in json.dumps(result)
    assert len(setup.calls) == 1


def test_request_size_limit(setup):
    response = setup.client.post("/mcp/h3", headers={"Authorization": "Bearer alice",
        "Accept": "application/json, text/event-stream"}, json={"padding": "x" * (130 * 1024)})
    assert response.status_code == 413
    assert not setup.calls


def test_rate_limit_shared_store(setup):
    asyncio.run(setup.runtime.store.increment_window("router:h3-mcp:alice", 60, 60))
    assert rpc(setup, "tools/list").status_code == 429
    assert not setup.calls


def test_links_bind_immutable_output_not_secret_or_current_file():
    result = add_links({"task_id": "abc123", "preview": {"output_id": "out_123"}})
    assert "project=abc123" in result["view_url"]
    assert result["download_url"].endswith("/api/projects/abc123/connector-outputs/out_123")
    assert "token=" not in json.dumps(result)


@pytest.mark.parametrize("url", ["https://127.0.0.1:14830", "http://ivan:8789", "http://127.0.0.1/path",
                                "http://user:password@127.0.0.1", "http://127.0.0.1/?key=secret"])
def test_no_client_selected_upstream(monkeypatch, setup, url):
    monkeypatch.setenv("AI_ROUTER_H3_STUDIO_URL", url)
    with pytest.raises(Exception):
        studio_configuration()


def test_disabled_integration_does_not_mount_or_load_sdk(monkeypatch):
    monkeypatch.delenv("AI_ROUTER_H3_MCP_ENABLED", raising=False)
    app = FastAPI()
    install_h3_mcp(app)
    assert all(getattr(route, "path", "") != "/mcp" for route in app.routes)


def test_audit_has_ids_but_not_prompt_or_credentials(setup):
    rpc(setup, "tools/call", {"name": "h3_capabilities", "arguments": {}})
    record = setup.audit[-1]
    assert record["tool"] == "h3_capabilities"
    assert record["client_id"] == "alice"
    assert record["request_id"]
    assert "internal-test-only" not in json.dumps(record)
    assert "prompt" not in record


@pytest.mark.parametrize("owner,status", [("alice", 206), ("bob", 404)])
def test_video_ranges_and_ownership(setup, owner, status):
    async def upstream(request):
        setup.calls.append(request)
        assert request.headers["accept-encoding"] == "identity"
        if request.headers["x-h3-connector-owner"] != "alice":
            return httpx.Response(404, json={"detail": "hidden"})
        assert request.headers["range"] == "bytes=2-5"
        return httpx.Response(206, stream=httpx.ByteStream(b"2345"), headers={
            "content-type": "video/mp4", "content-range": "bytes 2-5/10", "content-length": "4",
            "accept-ranges": "bytes"})
    setup.app.state.h3_mcp_transport = httpx.MockTransport(upstream)
    response = setup.client.get("/mcp/h3/tasks/abc123/outputs/out_123",
                                headers={"Authorization": "Bearer " + owner, "Range": "bytes=2-5"})
    assert response.status_code == status
    if status == 206:
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert response.headers["accept-ranges"] == "bytes"
    assert len(setup.calls) == 1


def test_initializer_notification_and_ping_do_not_dispatch(setup):
    headers = {"Authorization": "Bearer alice", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    response = setup.client.post("/mcp/h3", headers=headers,
                                 json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202
    assert rpc(setup, "ping").json()["result"] == {}
    assert not setup.calls


def test_capabilities_cannot_advertise_generation_while_gateway_is_closed(setup, monkeypatch):
    async def upstream(request):
        return httpx.Response(200, json={"writes_enabled": True, "generation_enabled": True})
    setup.app.state.h3_mcp_transport = httpx.MockTransport(upstream)
    gateway = H3Gateway(setup.app)
    result = asyncio.run(gateway.call("alice", "h3_capabilities", {}))
    assert result["writes_enabled"] is True
    assert result["generation_enabled"] is False
    monkeypatch.setenv("AI_ROUTER_H3_MCP_GENERATION_ENABLED", "true")
    assert asyncio.run(gateway.call("alice", "h3_capabilities", {}))["generation_enabled"] is True
    setup.runtime.draining = True
    result = asyncio.run(gateway.call("alice", "h3_capabilities", {}))
    assert result["writes_enabled"] is result["generation_enabled"] is False
