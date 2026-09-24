"""Public auto API regression with an isolated archive and simulated providers."""
import asyncio
import copy
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import create_app
from ai_router.config import Registry
from ai_router.training_archive import TrainingArchive
from test_client_route_binding import _runtime, ROOT


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("delayed_index", [False, True])
def test_auto_api_keeps_bonsai_after_client_renames_reasoning(tmp_path, monkeypatch, stream, protocol, delayed_index):
    runtime, _, _ = _runtime(tmp_path, monkeypatch)
    source = Registry(ROOT / "config/registry.yaml")
    bid, qid = "ivan-v10016-bonsai2-196k", "ai-qwen38-27b"
    endpoints = [replace(source.by_id(eid), enabled=True, auto_candidate=True,
                         api_base="http://upstream/v1", health_url="http://upstream/health", load_url=None)
                 for eid in (bid, qid)]
    runtime.settings.write_runtime({"identity": {"enabled": True}, "routing": {"client_route_bindings": [],
        "conversation_stability": {"enabled": True, "recovery_mode": "manual"},
        "objectives": {"enabled": True, "mode": "efficiency"}}})
    runtime.reload_endpoint_config = AsyncMock()
    runtime.registry = runtime.policy.registry = source.with_endpoints([endpoints[0]])
    client_id = "workbuddy-public"
    asyncio.run(runtime.clients.create_account({"id": client_id, "name": client_id, "enabled": True,
        "models": ["siyuan/auto"], "rpm_limit": 120, "tpm_limit": 1000000,
        "max_parallel_requests": 8, "disclosure_mode": "public", "local_only": True},
        allowed_models={"siyuan/auto"}))
    _, secret = asyncio.run(runtime.clients.create_key(client_id, "test"))
    db, key = tmp_path / "archive.sqlite3", tmp_path / "archive.key"
    key.write_bytes(Fernet.generate_key())
    runtime.training = TrainingArchive(str(db), str(key))
    if delayed_index:
        runtime.training.publish_history = AsyncMock()  # Worker has not processed history yet.
    monkeypatch.setenv("AI_ROUTER_TRAINING_DB_PATH", str(db))
    monkeypatch.setenv("AI_ROUTER_TRAINING_KEY_PATH", str(key))
    sent = []
    assistant = {"role": "assistant", "content": "answer", "reasoning_content": "private thought"}

    async def upstream(request):
        body = json.loads(request.content)
        sent.append(copy.deepcopy(body))
        value = {"id": "chat-test", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "message": assistant, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
        if not body.get("stream"):
            return httpx.Response(200, json=value)
        chunks = [{"id": "chat-test", "choices": [{"index": 0, "delta": assistant, "finish_reason": None}]},
                  {"id": "chat-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": value["usage"]}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(v) + "\n\n" for v in chunks) + "data: [DONE]\n\n")

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(runtime)) as client:
        field = "messages" if protocol == "chat" else "input"
        path = "/v1/chat/completions" if protocol == "chat" else "/v1/responses"
        body = {"model": "siyuan/auto", "stream": stream,
            field: [{"role": "user", "content": "Please answer this question."}]}
        headers = {"Authorization": "Bearer " + secret}
        first = client.post(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        if stream:
            assert ("data: [DONE]" if protocol == "chat" else "response.completed") in first.text
        trace1 = client.portal.call(runtime.route_traces.get, first.headers["x-request-id"])
        assert trace1["status"] == "succeeded"
        runtime.registry = runtime.policy.registry = source.with_endpoints(endpoints)
        if stream:
            events = [json.loads(line[6:]) for line in first.text.splitlines()
                      if line.startswith("data: ") and line[6:] != "[DONE]"]
            if protocol == "responses":
                output = next(event["response"]["output"] for event in events if event.get("type") == "response.completed")
            else:
                incoming = {"role": "assistant", "content": "", "reasoning_content": ""}
                for event in events:
                    for choice in event.get("choices", []):
                        for key in ("content", "reasoning_content"):
                            incoming[key] += choice.get("delta", {}).get(key) or ""
                output = [incoming]
        else:
            output = first.json()["output"] if protocol == "responses" else [first.json()["choices"][0]["message"]]
        if protocol == "chat":
            output[0]["reasoning"] = output[0].pop("reasoning_content")
        body[field].extend([*output, {"role": "user", "content": "Continue, please."}])
        second = client.post(path, headers=headers, json=body)
        assert second.status_code == 200, second.text
        trace2 = client.portal.call(runtime.route_traces.get, second.headers["x-request-id"])
        assert trace2["status"] == "succeeded"
        assert trace2["history_match"]["reason"] == "unique_history_match"
        assert sent[0]["model"] == sent[1]["model"]
        assert sent[1]["messages"][:len(sent[0]["messages"])] == sent[0]["messages"]
        assert any(m.get("reasoning_content") == "private thought" for m in sent[1]["messages"])
        conflicted = copy.deepcopy(body)
        conflicted[field][-2].update(reasoning="A", reasoning_content="B")
        rejected = client.post(path, headers=headers, json=conflicted)
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "history_migration_required"
        assert "private thought" not in rejected.text and bid not in rejected.text
        assert len(sent) == 2
        failed = client.portal.call(runtime.route_traces.get, rejected.headers["x-request-id"])
        assert failed["status"] == "failed"
