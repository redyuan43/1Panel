from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import create_app
from ai_router.config import Registry, Settings
from ai_router.policy import RoutingPolicy
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import EndpointStatus
from tests.test_prompt_directives import _Health

ROOT = Path(__file__).resolve().parents[1]
IMAGE = {"type": "image_url", "image_url": {"url": "https://example.invalid/test.png"}}
TOOL = {"type": "function", "function": {
    "name": "report_colors", "description": "Report visible colors.",
    "parameters": {"type": "object", "properties": {"colors": {"type": "string"}},
                   "required": ["colors"]},
}}


@pytest.mark.parametrize("limit_key", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("endpoint_id,phrase,advisory,image_allowed", [
    ("codex-pro-gpt-6-astra", "按北辰协议处理", True, True),
    ("codex-pro-gpt-5.6-sol", "按日轮协议处理", True, True),
    ("zhipu-glm-5.3-flash", "按玉衡协议处理", False, True),
    ("cloud-deepseek-v4-pro", "按青岚协议处理", False, False),
])
def test_workbuddy_image_tools_stream_output_limit(
    tmp_path, monkeypatch, limit_key, endpoint_id, phrase, advisory, image_allowed,
):
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-internal")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "test-legacy")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AI_ROUTER_ROUTE_TRACE_DB_PATH", str(tmp_path / "traces.sqlite3"))
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "settings.yaml")
    settings.write_runtime({"identity": {"enabled": True},
                            "routing": {"prompt_directives": {"enabled": True}}})
    source = Registry(ROOT / "config/registry.yaml")
    endpoint = replace(source.by_id(endpoint_id), enabled=True, cloud=False,
                       backend_type="openai", api_base="http://upstream/v1",
                       backend_api_key_env="AI_ROUTER_LITELLM_MASTER_KEY")
    registry = source.with_endpoints([endpoint])
    runtime = build_runtime(settings=settings, registry=registry,
                            store=InMemoryStateStore(), token_counter=SimpleTokenCounter())
    asyncio.run(runtime.health.client.aclose())
    runtime.health = _Health(EndpointStatus(
        endpoint_id=endpoint_id, healthy=True, checked_at=time.time(),
        eligible_context_tokens=endpoint.safe_context_tokens, load_headroom=1))
    runtime.policy = RoutingPolicy(registry, settings, runtime.health)
    asyncio.run(runtime.clients.create_account({
        "id": "workbuddy-public", "name": "WorkBuddy", "enabled": True,
        "models": ["siyuan/auto"], "rpm_limit": 120, "tpm_limit": 1000000,
        "max_parallel_requests": 2, "disclosure_mode": "public",
    }, allowed_models={"siyuan/auto", endpoint.public_model}, public_model_id="siyuan/auto"))
    _, secret = asyncio.run(runtime.clients.create_key("workbuddy-public", "test"))
    captured = []

    async def upstream(request):
        captured.append(json.loads(request.content))
        chunks = [
            {"id": "test", "object": "chat.completion.chunk", "model": endpoint.provider_model,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "red and blue"},
                          "finish_reason": None}]},
            {"id": "test", "object": "chat.completion.chunk", "model": endpoint.provider_model,
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                              + "data: [DONE]\n\n")

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    body = {
        "model": "siyuan/auto", "messages": [{"role": "user", "content": [
            {"type": "text", "text": phrase + "\nDescribe the colors."}, IMAGE]}],
        "tools": [TOOL], "stream": True, limit_key: 16384,
    }
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": "Bearer " + secret}, json=body)
    if not image_allowed:
        assert response.status_code == 422
        assert captured == []
    else:
        assert response.status_code == 200, response.text
        assert "data: [DONE]" in response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        answer = "".join(c.get("delta", {}).get("content", "")
                         for event in events for c in event.get("choices", []))
        assert answer == "red and blue"
        assert len(captured) == 1
        assert captured[0]["tools"] == [TOOL]
        assert any(IMAGE in m.get("content", []) for m in captured[0]["messages"]
                   if isinstance(m.get("content"), list))
        if advisory:
            assert all(k not in captured[0] for k in
                       ["max_tokens", "max_completion_tokens"])
            assert response.headers["x-1panel-output-limit-mode"] == "advisory"
        else:
            assert captured[0][limit_key] == 16384
            assert response.headers.get("x-1panel-output-limit-mode") != "advisory"
        trace = asyncio.run(runtime.route_traces.get(response.headers["x-request-id"]))
        assert trace["request"]["output_reserve_tokens"] == 16384
        assert set(trace["request"]["modalities"]) == {"image", "text"}
        assert trace["endpoint_id"] == endpoint_id
        assert ("output_token_limit" not in trace["request"]["required_capabilities"]) == advisory
    asyncio.run(runtime.close())
