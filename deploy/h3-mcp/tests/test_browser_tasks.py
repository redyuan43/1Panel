from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from test_connector_api import OWNER, studio, studio_factory


@pytest.fixture
def browser(studio, monkeypatch):
    monkeypatch.setenv("H3_MCP_ADMIN_USERS", "admin@example.test")
    policy = {"writes_enabled": True, "generation_enabled": True}
    studio.module.app.state.mcp_management_transport = httpx.MockTransport(lambda request: httpx.Response(200, json=policy))
    with TestClient(studio.module.app, client=("127.0.0.1", 5678)) as client:
        yield client, {"tailscale-user-login": "admin@example.test"}, policy


def test_browser_uses_same_owner_revision_and_immutable_operation_receipt(studio, browser):
    client, headers, _ = browser
    draft = studio.draft()
    operation = uuid4().hex
    body = {"operation_id": operation, "task_id": draft["task_id"], "expected_revision": draft["revision"],
            "expected_output_id": draft["context_output_id"]}
    response = client.post("/api/h3-browser/call/h3_confirm_prompt", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["connector_owner"] == OWNER
    approved = studio.get(draft)
    assert approved["prompt_approved"]
    edited = studio.ok("h3_save_draft", {**studio.arguments(approved), "original_prompt": draft["original_prompt"], "prompt": "新的明确提示词"})
    receipt = client.get("/api/h3-browser/operations/" + operation, headers=headers, params={"task_id": draft["task_id"]})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["connector_revision"] == edited["revision"]
    assert receipt.json()["operation_receipt"]["result_revision"] == approved["revision"]
    assert receipt.json()["operation_receipt"]["result"]["prompt_approved"]
    assert not studio.fleet.submissions


def test_browser_admin_authority_and_release_policy_cannot_be_bypassed(studio, browser):
    client, headers, policy = browser
    draft = studio.draft()
    body = {**studio.arguments(draft), "expected_output_id": draft["context_output_id"]}
    url = "/api/h3-browser/call/h3_confirm_prompt"
    assert client.post(url, json=body).status_code == 403
    policy["writes_enabled"] = False
    assert client.post(url, headers=headers, json=body).status_code == 403
    policy["writes_enabled"] = True
    approved = studio.confirm(draft)
    policy["generation_enabled"] = False
    body = {**studio.arguments(approved), "expected_output_id": approved["context_output_id"], "expected_run_id": None}
    assert client.post("/api/h3-browser/call/h3_start_preview", headers=headers, json=body).status_code == 403
    assert studio.pending == []


def test_browser_stale_approval_and_unrelated_tool_rejected(studio, browser):
    client, headers, _ = browser
    draft = studio.draft()
    edited = studio.ok("h3_save_draft", {**studio.arguments(draft), "original_prompt": draft["original_prompt"], "prompt": "changed"})
    body = {**studio.arguments(draft), "expected_output_id": draft["context_output_id"]}
    assert client.post("/api/h3-browser/call/h3_confirm_prompt", headers=headers, json=body).status_code == 409
    assert client.post("/api/h3-browser/call/arbitrary_request", headers=headers, json={}).status_code == 404
    assert studio.get(edited)["prompt_approved"] == ""
