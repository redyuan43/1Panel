from __future__ import annotations

import json
import hashlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_connector_api import OWNER, connector_api, studio, studio_factory


pytest.importorskip("mcp")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-router"))
from ai_router.errors import AuthenticationError
from ai_router.h3_mcp import install_h3_mcp
from ai_router.store import InMemoryStateStore


@pytest.fixture
def gateway(studio, monkeypatch, tmp_path):
    secret = tmp_path / "e2e-internal-key"
    secret.write_text("isolated-e2e-internal-secret-never-production")
    schema = tmp_path / "e2e-schema.json"
    schema.write_text(json.dumps(connector_api.TOOL_DEFINITIONS))
    for name, value in {
        "H3_CONNECTOR_KEY_FILE": str(secret), "AI_ROUTER_H3_CONNECTOR_KEY_FILE": str(secret),
        "AI_ROUTER_H3_STUDIO_URL": "http://127.0.0.1:14830", "AI_ROUTER_H3_MCP_ENABLED": "true",
        "AI_ROUTER_H3_MCP_CLIENTS": OWNER + ",second-client", "AI_ROUTER_H3_MCP_WRITES_ENABLED": "true",
        "AI_ROUTER_H3_MCP_GENERATION_ENABLED": "true", "AI_ROUTER_H3_MCP_SCHEMA_FILE": str(schema),
    }.items():
        monkeypatch.setenv(name, value)

    async def authenticate(authorization):
        if authorization not in {"Bearer " + OWNER, "Bearer second-client"}:
            raise AuthenticationError()
        return SimpleNamespace(policy=SimpleNamespace(id=authorization.split()[1],
                                media_models=["siyuan-video"], rpm_limit=60), key_id="fake")

    app = FastAPI()
    app.state.runtime = SimpleNamespace(auth=SimpleNamespace(authenticate=authenticate),
        store=InMemoryStateStore(), audit=SimpleNamespace(write=lambda *args, **kwargs: None), draining=False)
    app.state.h3_mcp_transport = httpx.ASGITransport(app=studio.module.app)
    install_h3_mcp(app)
    with TestClient(app, base_url="http://localhost") as client:
        yield SimpleNamespace(app=app, client=client, studio=studio)


def call(gateway, tool, arguments, owner=OWNER, success=True):
    response = gateway.client.post("/mcp/h3", headers={"Authorization": "Bearer " + owner,
        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert bool(result.get("isError")) is not success, result
    return result["structuredContent"]


def operation(task, **values):
    return {"operation_id": uuid4().hex, "task_id": task["task_id"], "expected_revision": task["revision"], **values}


def test_real_protocol_to_studio_and_fake_fleet_preserves_review_boundaries(gateway):
    prompt = "  integrated_multimodal_description:\n粉色复古卧室，15秒竖版；保留原生声音。\n"
    draft = call(gateway, "h3_save_draft", {"operation_id": uuid4().hex, "original_prompt": prompt,
        "prompt": prompt, "verbatim": True, "recipe_id": "B8", "name": "离线-MCP验收"})
    assert draft["original_prompt"] == prompt
    assert not gateway.studio.fleet.submissions
    blocked = call(gateway, "h3_start_preview", operation(draft,
        expected_output_id=draft["context_output_id"], expected_run_id=None), success=False)
    assert blocked["error"]["code"] == "h3_contract_rejected"
    approved = call(gateway, "h3_confirm_prompt", operation(draft, expected_output_id=draft["context_output_id"]))
    assert not gateway.studio.fleet.submissions
    call(gateway, "h3_get_task", {"task_id": draft["task_id"]}, owner="second-client", success=False)
    start = operation(approved, expected_output_id=approved["context_output_id"], expected_run_id=None)
    queued = call(gateway, "h3_start_preview", start)
    assert queued["preview"]["status"] == "queued"
    assert call(gateway, "h3_start_preview", start) == queued
    assert len(gateway.studio.pending) == 1
    gateway.studio.pending.pop()()
    complete = call(gateway, "h3_get_task", {"task_id": draft["task_id"]})
    assert complete["preview"]["status"] == "awaiting_approval"
    assert len(gateway.studio.fleet.submissions) == 1
    assert complete["download_url"].endswith("/connector-outputs/" + complete["preview"]["output_id"])
    output = gateway.client.get(f"/mcp/h3/tasks/{draft['task_id']}/outputs/{complete['preview']['output_id']}",
        headers={"Authorization": "Bearer " + OWNER, "Range": "bytes=0-3"})
    assert output.status_code == 206
    assert output.content == gateway.studio.fleet.artifact[:4]
    reviewed = call(gateway, "h3_review_preview", operation(complete, output_id=complete["preview"]["output_id"],
        expected_run_id=complete["preview"]["run_id"], decision="approve", feedback="仅离线流程验证，不是画质评价"))
    assert reviewed["preview"]["status"] == "approved"
    assert len(gateway.studio.fleet.submissions) == 1
    assert not gateway.studio.pending


def test_dropped_creation_response_can_be_reconciled_readonly(gateway):
    upstream = gateway.app.state.h3_mcp_transport
    captured = []

    class LoseResponseOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await upstream.handle_async_request(request)
            if request.url.path.endswith("h3_save_draft"):
                captured.append(request)
                await response.aclose()
                raise httpx.ReadTimeout("simulated response loss after Studio committed")
            return response

    gateway.app.state.h3_mcp_transport = LoseResponseOnce()
    identifier = uuid4().hex
    result = call(gateway, "h3_save_draft", {"operation_id": identifier, "original_prompt": "原始需求",
        "prompt": "待确认草稿"}, success=False)
    assert result["error"]["code"] == "h3_outcome_unknown"
    assert len(captured) == 1
    recovered = call(gateway, "h3_get_task", {"operation_id": identifier})
    assert recovered["task_id"]
    tasks = call(gateway, "h3_list_tasks", {})
    assert [task["task_id"] for task in tasks["tasks"]] == [recovered["task_id"]]
    assert len(captured) == 1
    assert not gateway.studio.fleet.submissions


def test_binary_asset_relay_exceeds_json_limit_without_exposing_internal_key(gateway):
    buffer = io.BytesIO()
    Image.new("RGB", (512, 512), "pink").save(buffer, "PNG", compress_level=0)
    data = buffer.getvalue()
    assert len(data) > 128 * 1024
    operation_id = uuid4().hex
    headers = {"Authorization": "Bearer " + OWNER, "Content-Type": "application/octet-stream",
               "X-H3-Upload-Metadata": json.dumps({"operation_id": operation_id, "filename": "photo.png",
                  "kind": "first_frame", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})}
    response = gateway.client.post("/mcp/h3/assets/uploads", content=data, headers=headers)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["state"] == "ready" and "path" not in receipt
    result = gateway.client.get("/mcp/h3/assets/uploads/" + operation_id, headers=headers)
    assert result.json() == receipt
    url = "/mcp/h3/assets/" + receipt["asset_id"] + "/content"
    output = gateway.client.get(url, headers={**headers, "Range": "bytes=0-7"})
    assert output.status_code == 206 and output.content == data[:8]
    forbidden = gateway.client.get(url, headers={"Authorization": "Bearer second-client"})
    assert forbidden.status_code == 404
    assert gateway.client.get(url).status_code == 401
    assert not gateway.studio.fleet.submissions
