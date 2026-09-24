from __future__ import annotations

import asyncio
import copy
import json
import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import create_app, _target_count_payload, _responses_chat_payload
from ai_router.config import Registry, Settings
from ai_router.errors import HistoryMigrationRequiredError
from ai_router.history import normalize_history_for_provider
from ai_router.identity import IdentityProfile
from ai_router.policy import RoutingPolicy
from ai_router.responses_adapter import responses_request_to_chat
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import Evaluation, RequestCapabilities
from tests.test_core import FakeHealth, healthy

ROOT = Path(__file__).resolve().parents[1]
EID = "amd-halogen-qwen38-256k"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("offline tests must not contact a real backend")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def endpoint():
    return Registry(ROOT / "config/registry.yaml").by_id(EID)


def test_single_responses_reasoning_object_is_preserved():
    body = {"input": {"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "SINGLE_MARKER"}]}}
    projected = normalize_history_for_provider(body, "responses", endpoint())
    assert responses_request_to_chat(projected)["messages"][0]["reasoning_content"] == "SINGLE_MARKER"


@pytest.mark.parametrize("item", [
    {"type": "reasoning", "encrypted_content": "opaque-secret"},
    {"type": "item_reference", "id": "opaque-secret"},
])
def test_single_opaque_responses_object_is_rejected(item):
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider({"input": item}, "responses", endpoint())


def history_body(protocol):
    if protocol == "chat":
        return {"messages": [
            {"role": "user", "content": "lookup"},
            {"role": "assistant", "content": "", "reasoning_content": "REASON_MARKER",
             "tool_calls": [{"id": "call-1", "type": "function", "function": {
                 "name": "lookup", "arguments": '{"key":1}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "RESULT_MARKER"},
        ]}
    return {"input": [
        {"role": "user", "content": "lookup"},
        {"type": "reasoning", "id": "rs-1", "summary": [
            {"type": "summary_text", "text": "REASON_MARKER"}],
         "content": [{"type": "reasoning_text", "text": "REASON_MARKER"}]},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": '{"key":1}'},
        {"type": "function_call_output", "call_id": "call-1", "output": "RESULT_MARKER"},
    ]}


@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_history_projection_matches_counting_and_preserves_tool_transaction(protocol):
    body = history_body(protocol)
    original = copy.deepcopy(body)
    projected = normalize_history_for_provider(body, protocol, endpoint())
    payload, kind = _target_count_payload(IdentityProfile.from_settings({"enabled": False}), projected, protocol, endpoint())
    assert kind == "chat"
    assert payload["messages"][1]["reasoning_content"] == "REASON_MARKER"
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert payload["messages"][2] == {"role": "tool", "tool_call_id": "call-1", "content": "RESULT_MARKER"}
    assert payload["chat_template_kwargs"]["preserve_thinking"] is True
    assert body == original
    assert normalize_history_for_provider(projected, protocol, endpoint()) == projected


@pytest.mark.parametrize("following", [
    {"role": "assistant", "content": "answer"},
    {"role": "user", "content": "next"},
    None,
])
def test_reasoning_content_and_distinct_summary_are_both_preserved(following):
    items = [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "FULL"}],
              "summary": [{"type": "summary_text", "text": "SUMMARY"}]}]
    if following:
        items.append(following)
    projected = normalize_history_for_provider({"input": items}, "responses", endpoint())
    messages = responses_request_to_chat(projected)["messages"]
    assert messages[0]["role"] == "assistant"
    assert messages[0]["reasoning_content"] == "FULL\nSUMMARY"
    if following and following["role"] == "user":
        assert messages[1] == following


@pytest.mark.parametrize("bad", [
    {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
    {"type": "reasoning", "content": [{"type": "unknown", "text": "secret"}]},
    {"type": "reasoning", "summary": "not-a-list"},
    {"type": "reasoning", "other_history": "must-not-disappear"},
    {"type": "item_reference", "id": "opaque-ref"},
])
def test_opaque_or_unrepresentable_history_is_rejected(bad):
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider({"input": [bad]}, "responses", endpoint())


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("nested", [False, True])
def test_explicit_history_discard_is_not_silently_honored(protocol, nested):
    body = history_body(protocol)
    body.update({"chat_template_kwargs": {"preserve_thinking": False}} if nested else {"preserve_thinking": False})
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider(body, protocol, endpoint())


def test_native_override_does_not_silently_drop_plaintext_reasoning():
    e = endpoint()
    e = replace(e, capabilities=replace(e.capabilities, responses="native"))
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider({"input": [{"role": "assistant", "content": "ok", "reasoning_content": "keep"}]}, "responses", e)


def test_other_adapters_do_not_acquire_halogen_history_conversion():
    e = replace(endpoint(), backend_type="llama_cpp", node="other")
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider(history_body("responses"), "responses", e)


def test_32_images_keep_their_order_in_responses_chat_projection():
    urls = ["data:image/png;base64," + str(i) for i in range(32)]
    body = {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": u} for u in urls]}]}
    projected = normalize_history_for_provider(body, "responses", endpoint())
    wire = responses_request_to_chat(projected)
    assert [p["image_url"]["url"] for p in wire["messages"][0]["content"]] == urls
    assert endpoint().supports_image_count(32)
    assert not endpoint().supports_image_count(33)


def test_responses_thinking_controls_survive_counting_and_wire_adaptation():
    body = {**history_body("responses"), "reasoning": {"enabled": True, "max_tokens": 128, "effort": "low"},
            "max_thinking_tokens": 128, "max_output_tokens": 512, "thinking_budget": 128}
    projected = normalize_history_for_provider(body, "responses", endpoint())
    counted, kind = _target_count_payload(IdentityProfile.from_settings({"enabled": False}), projected, "responses", endpoint())
    wire = _responses_chat_payload(projected, endpoint())
    for value in (counted, wire):
        assert value["reasoning"] == body["reasoning"]
        assert value["reasoning_effort"] == "low"
        assert value["max_thinking_tokens"] == value["thinking_budget"] == 128
        assert value["max_tokens"] == 512
        assert value["chat_template_kwargs"]["preserve_thinking"] is True
    assert kind == "chat"


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("explicit", [False, True])
def test_long_context_remains_eligible(tmp_path, protocol, explicit):
    e = replace(endpoint(), enabled=True, auto_candidate=True)
    registry = Registry(ROOT / "config/registry.yaml").with_endpoints([e])
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "settings.yaml")
    policy = RoutingPolicy(registry, settings, FakeHealth({e.id: healthy(e.id, context=262144)}))
    choice = asyncio.run(policy.choose(
        requested_model=e.public_model if explicit else "auto",
        evaluation=Evaluation("long-context", None, 1.0, "context_threshold"),
        prompt_tokens=200000, output_reserve_tokens=4096, modalities={"text"},
        has_tools=False, required_capabilities=RequestCapabilities(protocol=protocol), conversation=None,
    ))
    assert choice.endpoint.id == EID


def runtime_for(tmp_path, monkeypatch):
    for key, value in {
        "AI_ROUTER_STATE_KEY": Fernet.generate_key().decode(),
        "AI_ROUTER_1PANEL_API_KEY": "test-key",
        "AI_ROUTER_LITELLM_MASTER_KEY": "test-internal",
        "AI_ROUTER_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
        "AI_ROUTER_ROUTE_TRACE_DB_PATH": str(tmp_path / "traces.sqlite3"),
        "AI_ROUTER_TRAINING_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "settings.yaml")
    settings.write_runtime({"identity": {"enabled": False}, "evaluator": {"enabled": False},
                            "compaction": {"enabled": False}, "routing": {"client_route_bindings": []}})
    e = replace(endpoint(), enabled=True, auto_candidate=True, api_base="http://synthetic.test/v1", health_url="http://synthetic.test/health")
    registry = Registry(ROOT / "config/registry.yaml").with_endpoints([e])
    runtime = build_runtime(settings=settings, registry=registry, store=InMemoryStateStore(), token_counter=SimpleTokenCounter())
    asyncio.run(runtime.health.client.aclose())
    runtime.health = FakeHealth({e.id: healthy(e.id, context=262144)})
    runtime.policy = RoutingPolicy(registry, settings, runtime.health, store=runtime.store)
    captured = []

    async def upstream(request):
        body = json.loads(request.content)
        captured.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["messages"][1]["reasoning_content"] == "REASON_MARKER"
        assert body["messages"][2]["content"] == "RESULT_MARKER"
        assert body["chat_template_kwargs"]["preserve_thinking"] is True
        if body.get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"reasoning_content": "NEW_REASON"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "ANSWER"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 8}},
            ]
            return httpx.Response(200, content=("".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n").encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "synthetic-completion", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ANSWER", "reasoning_content": "NEW_REASON"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 8}})

    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return runtime, captured


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_api_history_output_and_terminal_audit(tmp_path, monkeypatch, protocol, stream, explicit):
    runtime, captured = runtime_for(tmp_path, monkeypatch)
    body = {**history_body(protocol), "model": endpoint().public_model if explicit else "auto", "stream": stream,
            "reasoning": {"enabled": True, "max_tokens": 128}, "max_thinking_tokens": 128}
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/" + ("responses" if protocol == "responses" else "chat/completions"), json=body, headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 200, response.text
    assert "ANSWER" in response.text
    assert len(captured) == 1
    assert captured[0]["reasoning"] == body["reasoning"]
    assert captured[0]["max_thinking_tokens"] == 128
    if protocol == "responses":
        assert "response.completed" in response.text if stream else response.json()["object"] == "response"
    with sqlite3.connect(tmp_path / "traces.sqlite3") as connection:
        assert connection.execute("select status,status_code,endpoint_id from route_traces").fetchall() == [("succeeded", 200, EID)]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_full_api_opaque_history_rejected_before_upstream(tmp_path, monkeypatch, protocol, stream, explicit):
    runtime, captured = runtime_for(tmp_path, monkeypatch)
    if protocol == "responses":
        history = {"input": [{"type": "reasoning", "encrypted_content": "opaque-secret", "summary": []},
                             {"role": "user", "content": "continue"}]}
    else:
        history = {"messages": [{"role": "assistant", "content": "", "reasoning_content": "one",
                                 "reasoning": "opaque-secret"}, {"role": "user", "content": "continue"}]}
    body = {"model": endpoint().public_model if explicit else "auto", "stream": stream, **history}
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/" + ("responses" if protocol == "responses" else "chat/completions"),
                               json=body, headers={"Authorization": "Bearer test-key"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "history_migration_required"
    assert not captured
    assert "opaque-secret" not in response.text
    with sqlite3.connect(tmp_path / "traces.sqlite3") as connection:
        assert connection.execute("select status from route_traces").fetchall() == [("failed",)]
