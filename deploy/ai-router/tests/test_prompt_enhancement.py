from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router import prompt_enhancement as enhancement
from ai_router.api import create_app
from ai_router.config import Registry, Settings
from ai_router.policy import RoutingPolicy
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from test_core import FakeHealth, ROOT, _public_test_runtime, healthy


def _body(content="解释这个函数"):
    return {"model": "siyuan/auto", "messages": [
        {"role": "system", "content": "stable instructions"},
        {"role": "user", "content": content},
    ]}


def _runtime():
    events = []
    charges = []

    class Budget:
        async def reserve(self, endpoint, **kwargs):
            charges.append(("reserve", kwargs["request_id"]))
            return kwargs["request_id"]

        async def settle(self, reservation, usage):
            charges.append(("settle", reservation))

        async def commit(self, reservation):
            charges.append(("commit", reservation))

        async def release(self, reservation):
            charges.append(("release", reservation))

    class Limiter:
        async def check_rate_limits(self, *_args, **_kwargs):
            return True, None

        async def check_additional_tokens(self, *_args):
            return True

    current = SimpleNamespace(
        budget=Budget(), limiter=Limiter(),
        audit=SimpleNamespace(write=lambda event, **kwargs: events.append((event, kwargs))),
    )
    return current, events, charges


def _decision(endpoint_id="local"):
    return SimpleNamespace(
        endpoint=SimpleNamespace(
            id=endpoint_id, safe_context_tokens=32000,
            capabilities=SimpleNamespace(chat=True, output_token_limit=True),
        ),
        deployment_safe_context_tokens=32000,
        output_reserve_tokens=64,
        prompt_tokens=20,
        context_required=84,
    )


def _session(body, api_kind="chat"):
    return enhancement.EnhancementSession(
        body, api_kind, client_id="workbuddy-public",
        policy=SimpleNamespace(rpm_limit=1000, tpm_limit=100000),
        request_id="test-request",
    )


async def _count_chat(_body):
    return 800


async def _count_routed(_body):
    return 28


def test_original_workbuddy_templates_are_bound_to_verified_hashes():
    import hashlib
    from ai_router.prompt_enhancement_templates import (
        SYSTEM_TEMPLATE, SYSTEM_TEMPLATE_SHA256,
        USER_TEMPLATE, USER_TEMPLATE_SHA256,
    )

    assert hashlib.sha256(SYSTEM_TEMPLATE.encode()).hexdigest() == SYSTEM_TEMPLATE_SHA256
    assert hashlib.sha256(USER_TEMPLATE.encode()).hexdigest() == USER_TEMPLATE_SHA256
    request = enhancement.model_request("修复这个问题")
    assert request["messages"][1]["content"].count("修复这个问题") == 1
    assert "history" not in request
    assert "tools" not in request


@pytest.mark.parametrize("api_kind,body", [
    ("chat", _body()),
    ("responses", {"model": "siyuan/auto", "input": "解释这个函数"}),
    ("responses", {"model": "siyuan/auto", "input": [{"role": "user", "content": [
        {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
        {"type": "input_text", "text": "解释这个函数"},
    ]}]}),
])
def test_only_current_text_is_replaced(api_kind, body):
    original = copy.deepcopy(body)
    source, reason = enhancement.extract_source(body, api_kind)
    assert reason == "eligible"
    updated = enhancement.replace_current_input(body, api_kind, source, "请说明这个函数的用途")
    assert updated is not None
    assert body == original
    assert "请说明这个函数的用途" in str(updated)
    if api_kind == "chat":
        assert updated["messages"][0] == original["messages"][0]
    if isinstance(body.get("input"), list):
        assert updated["input"][0]["content"][0] == original["input"][0]["content"][0]


def test_workbuddy_wrapper_rewrites_only_root_query():
    raw = "<previous_user_message><user_query>旧任务</user_query></previous_user_message><user_query>改成表格</user_query>"
    body = _body(raw)
    source, reason = enhancement.extract_source(body, "chat")
    assert reason == "eligible" and source.text == "改成表格"
    routed = copy.deepcopy(body)
    routed["messages"][1]["content"] = "<workbuddy_dynamic_context>data</workbuddy_dynamic_context>\n\n" + raw
    routed["messages"].append({"role": "user", "content": "<workbuddy_tool_catalog>tools</workbuddy_tool_catalog>"})
    updated = enhancement.replace_current_input(routed, "chat", source, "请将前文整理成表格")
    assert updated["messages"][1]["content"].endswith(
        "<previous_user_message><user_query>旧任务</user_query></previous_user_message>"
        "<user_query>请将前文整理成表格</user_query>"
    )
    assert updated["messages"][2] == routed["messages"][2]


def test_long_or_code_history_does_not_hide_short_current_query():
    history = "旧消息" * 500 + "diff --git a/a b/a"
    raw = (
        f"<previous_user_message><user_query>{history}</user_query>"
        "</previous_user_message><user_query>把结果列成表格</user_query>"
    )
    source, reason = enhancement.extract_source(_body(raw), "chat")
    assert reason == "eligible"
    assert source.text == "把结果列成表格"
    rewritten = enhancement.replace_current_input(
        _body(raw), "chat", source, "请将结果整理成表格",
    )
    assert history in rewritten["messages"][-1]["content"]
    assert rewritten["messages"][-1]["content"].endswith(
        "<user_query>请将结果整理成表格</user_query>"
    )


def test_wrapped_query_entities_are_decoded_and_reescaped():
    raw = "<user_query>说明 A &amp; B</user_query>"
    source, reason = enhancement.extract_source(_body(raw), "chat")
    assert reason == "eligible"
    assert source.text == "说明 A & B"
    rewritten = enhancement.replace_current_input(
        _body(raw), "chat", source, "请说明 A < B",
    )
    assert rewritten["messages"][-1]["content"] == (
        "<user_query>请说明 A &lt; B</user_query>"
    )


def test_multiple_text_parts_are_kept_original():
    body = _body([
        {"type": "text", "text": "这里是上下文"},
        {"type": "text", "text": "解释这个函数"},
    ])
    assert enhancement.extract_source(body, "chat")[1] == "ambiguous_text_parts"


@pytest.mark.parametrize("content,reason", [
    ("x" * 801, "long_input"),
    ("修复 ```python\nprint(1)\n```", "code_block"),
    ("<user_query>未闭合", "ambiguous_wrapper"),
])
def test_unsafe_input_is_skipped(content, reason):
    assert enhancement.extract_source(_body(content), "chat")[1] == reason


def test_native_workbuddy_enhancer_is_not_enhanced_again():
    body = _body("USER INPUT:\n写个页面\nTASK:\n改写")
    body["messages"][0]["content"] = (
        "You are a Prompt Engineering Expert specializing in prompts. ANALYSIS PROCESS:"
    )
    assert enhancement.extract_source(body, "chat")[1] == "native_enhancer"


@pytest.mark.parametrize("candidate,reason", [
    ("Enhanced prompt: 修复 /app/main.py 里的 42 个问题", "invalid_output"),
    ("修复 /app/main.py 里的问题", "lost_literal"),
    ("Fix the 42 errors in /app/main.py", "language_changed"),
])
def test_output_must_preserve_literals_and_language(candidate, reason):
    assert enhancement.validate_output("修复 /app/main.py 里的 42 个问题", candidate)[1] == reason


def test_selected_model_call_applies_once_and_keeps_original(monkeypatch):
    calls = []

    async def fake_model(_current, decision, request):
        calls.append((decision.endpoint.id, request))
        return {"choices": [{"message": {"content": "请说明这个函数的用途"}}],
                "usage": {"prompt_tokens": 800, "completion_tokens": 20}}

    monkeypatch.setattr(enhancement, "_request_model", fake_model)
    body = _body()
    original = copy.deepcopy(body)
    current, events, charges = _runtime()
    decision = _decision()
    trace = SimpleNamespace(payload={})
    session = _session(body)
    result = asyncio.run(session.apply(
        current, decision, body, count_chat=_count_chat,
        count_routed=_count_routed, trace=trace,
    ))
    assert body == original
    assert result["messages"][-1]["content"] == "请说明这个函数的用途"
    assert decision.prompt_tokens == 28
    assert trace.payload["prompt_enhancement"]["state"] == "applied"
    assert charges == [("reserve", "test-request:enhance"), ("settle", "test-request:enhance")]
    second = asyncio.run(session.apply(
        current, decision, body, count_chat=_count_chat,
        count_routed=_count_routed, trace=trace,
    ))
    assert second == result and len(calls) == 1
    assert events[-1][1]["state"] == "applied"


def test_paid_cloud_skips_enhancer_when_main_budget_guard_is_unavailable(monkeypatch):
    calls = []

    async def fake_model(*_args):
        calls.append("called")
        return {"choices": [{"message": {"content": "请说明这个函数的用途"}}]}

    monkeypatch.setattr(enhancement, "_request_model", fake_model)
    current, events, _charges = _runtime()

    class Budget:
        async def reserve(self, _endpoint, **kwargs):
            if kwargs["prompt_tokens"] + kwargs["output_reserve_tokens"] > 100:
                raise RuntimeError("synthetic cloud budget limit")
            return kwargs["request_id"]

    current.budget = Budget()
    decision = _decision()
    decision.endpoint.cloud = True
    decision.endpoint.metadata = {}
    body = _body()
    trace = SimpleNamespace(payload={})
    result = asyncio.run(_session(body).apply(
        current, decision, body, count_chat=_count_chat,
        count_routed=_count_routed, trace=trace,
    ))
    assert result is body
    assert calls == []
    assert trace.payload["prompt_enhancement"]["reason"] == "main_budget_guard_unavailable"
    assert events[-1][1]["reason"] == "main_budget_guard_unavailable"
    assert decision.prompt_tokens + decision.output_reserve_tokens <= 100


def test_paid_cloud_holds_main_budget_guard_until_enhancement_finishes(monkeypatch):
    async def fake_model(*_args):
        return {"choices": [{"message": {"content": "请说明这个函数的用途"}}], "usage": {}}

    monkeypatch.setattr(enhancement, "_request_model", fake_model)
    current, _events, charges = _runtime()
    decision = _decision()
    decision.endpoint.cloud = True
    decision.endpoint.metadata = {}
    body = _body()
    result = asyncio.run(_session(body).apply(
        current, decision, body, count_chat=_count_chat,
        count_routed=_count_routed, trace=None,
    ))
    assert result["messages"][-1]["content"] == "请说明这个函数的用途"
    assert charges == [
        ("reserve", "test-request:enhance-main-guard"),
        ("reserve", "test-request:enhance"),
        ("settle", "test-request:enhance"),
        ("release", "test-request:enhance-main-guard"),
    ]


def test_timeout_falls_back_to_original_and_conservatively_charges(monkeypatch):
    async def timeout(*_args):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(enhancement, "_request_model", timeout)
    body = _body()
    current, _events, charges = _runtime()
    trace = SimpleNamespace(payload={})
    session = _session(body)
    result = asyncio.run(session.apply(
        current, _decision(), body,
        count_chat=_count_chat, count_routed=_count_routed, trace=trace))
    assert result is body
    assert trace.payload["prompt_enhancement"]["reason"] == "enhancer_error"
    assert charges == [("reserve", "test-request:enhance"), ("commit", "test-request:enhance")]


def test_changed_retry_target_uses_original(monkeypatch):
    async def fake_model(*_args):
        return {"choices": [{"message": {"content": "请说明这个函数的用途"}}], "usage": {}}

    monkeypatch.setattr(enhancement, "_request_model", fake_model)
    body = _body()
    current, _events, _charges = _runtime()
    session = _session(body)
    asyncio.run(session.apply(current, _decision("one"), body,
        count_chat=_count_chat, count_routed=_count_routed, trace=None))
    result = asyncio.run(session.apply(current, _decision("two"), body,
        count_chat=_count_chat, count_routed=_count_routed, trace=None))
    assert result is body


def test_token_count_failure_falls_back_without_changing_decision():
    async def fail_count(_body):
        raise RuntimeError("synthetic count failure")

    body = _body()
    current, _events, charges = _runtime()
    decision = _decision()
    trace = SimpleNamespace(payload={})
    result = asyncio.run(_session(body).apply(
        current, decision, body, count_chat=fail_count,
        count_routed=_count_routed, trace=trace,
    ))
    assert result is body
    assert (decision.prompt_tokens, decision.context_required) == (20, 84)
    assert trace.payload["prompt_enhancement"]["reason"] == "enhancer_error"
    assert charges == []


def test_extraction_failure_keeps_original(monkeypatch):
    def fail_extract(*_args):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(enhancement, "extract_source", fail_extract)
    body = _body()
    session = _session(body)
    current, events, charges = _runtime()
    result = asyncio.run(session.apply(
        current, _decision(), body, count_chat=_count_chat,
        count_routed=_count_routed, trace=None,
    ))
    assert result is body
    assert events[-1][1]["reason"] == "extract_error"
    assert charges == []


def test_local_model_request_omits_empty_authorization(monkeypatch):
    captured = []
    original_client = httpx.AsyncClient
    monkeypatch.delenv("AI_ROUTER_TEST_MISSING_KEY", raising=False)

    async def upstream(request):
        captured.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "已优化"}}],
        })

    def client_factory(*args, **kwargs):
        return original_client(
            *args, transport=httpx.MockTransport(upstream), **kwargs,
        )

    monkeypatch.setattr(enhancement.httpx, "AsyncClient", client_factory)
    current = SimpleNamespace(internal_base_url="", internal_api_key="")
    decision = SimpleNamespace(
        upstream_api_base="http://local.invalid/v1",
        deployment_details={}, deployment_id=None,
        endpoint=SimpleNamespace(
            id="local", provider_model="local-model",
            backend_api_key_env="AI_ROUTER_TEST_MISSING_KEY",
        ),
    )
    result = asyncio.run(enhancement._request_model(
        current, decision, enhancement.model_request("解释这段话"),
    ))
    assert result["choices"][0]["message"]["content"] == "已优化"
    assert len(captured) == 1
    assert "authorization" not in captured[0].headers
    assert captured[0].url.path == "/v1/chat/completions"
    assert json.loads(captured[0].content)["model"] == "local-model"


def test_settings_default_off_and_rejects_invalid_type(tmp_path):
    settings = Settings(runtime_path=tmp_path / "missing.yaml")
    assert settings.section("routing")["prompt_enhancement"] == {"enabled": False}
    from ai_router.config import validate_settings
    value = copy.deepcopy(settings.value)
    value["routing"]["prompt_enhancement"]["enabled"] = "true"
    with pytest.raises(ValueError, match="prompt_enhancement"):
        validate_settings(value)


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_api_routes_only_enhanced_current_input(
    tmp_path, monkeypatch, api_kind, stream, enabled,
):
    endpoint = Registry(ROOT / "config" / "registry.yaml").by_id(
        "ivan-qwen38-flash-128k"
    )
    assert endpoint is not None
    settings = Settings(runtime_path=tmp_path / "settings.yaml")
    configured = settings.value
    configured["routing"]["prompt_enhancement"]["enabled"] = enabled
    settings.write_runtime(configured)
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    registry = Registry(ROOT / "config" / "registry.yaml")
    runtime = build_runtime(
        settings=settings, registry=registry, store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth({
        endpoint.id: healthy(endpoint.id, context=endpoint.safe_context_tokens),
    })
    runtime.policy = RoutingPolicy(registry, settings, runtime.health)
    asyncio.run(runtime.clients.create_account(
        {
            "id": "workbuddy-qwen36-shared", "name": "WorkBuddy test",
            "enabled": True, "models": [endpoint.public_model],
            "rpm_limit": 100, "tpm_limit": 100000,
            "max_parallel_requests": 2, "disclosure_mode": "internal",
        },
        allowed_models={"auto", "siyuan/auto", endpoint.public_model},
    ))
    _key, secret = asyncio.run(runtime.clients.create_key(
        "workbuddy-qwen36-shared", "test",
    ))
    enhanced_calls = []
    upstream_calls = []

    async def fake_enhancer(_current, decision, request):
        enhanced_calls.append((decision.endpoint.id, request))
        return {
            "choices": [{"message": {"content": "请分两句话说明该函数的作用"}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 20},
        }

    monkeypatch.setattr(enhancement, "_request_model", fake_enhancer)

    async def upstream(request):
        payload = json.loads(request.content)
        upstream_calls.append(payload)
        if api_kind == "chat":
            if stream:
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    content=(
                        b'data: {"choices":[{"index":0,"delta":{"content":"ok"},'
                        b'"finish_reason":null}]}\n\n'
                        b'data: {"choices":[{"index":0,"delta":{},'
                        b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
                    ),
                )
            return httpx.Response(200, json={
                "id": "chatcmpl-test", "object": "chat.completion",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "ok",
                }, "finish_reason": "stop"}],
            })
        if stream:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"type":"response.output_text.delta",'
                    b'"delta":"ok"}\n\n'
                    b'data: {"type":"response.completed",'
                    b'"response":{"id":"resp-test","object":"response",'
                    b'"status":"completed","output":[{"type":"message",'
                    b'"role":"assistant","content":[{"type":"output_text",'
                    b'"text":"ok"}]}]}}\n\ndata: [DONE]\n\n'
                ),
            )
        return httpx.Response(200, json={
            "id": "resp-test", "object": "response", "status": "completed",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}]}],
        })

    asyncio.run(runtime.internal_client.aclose())

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
    )
    original = "解释这个函数"
    body = {"model": endpoint.public_model, "stream": stream}
    if api_kind == "chat":
        body["messages"] = [
            {"role": "system", "content": "keep this instruction"},
            {"role": "user", "content": "昨天的问题"},
            {"role": "assistant", "content": "昨天的回答"},
            {"role": "user", "content": original},
        ]
    else:
        body["input"] = original
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions" if api_kind == "chat" else "/v1/responses",
            headers={"Authorization": f"Bearer {secret}"}, json=body,
        )
    assert response.status_code == 200, response.text
    assert len(enhanced_calls) == int(enabled)
    if enabled:
        assert enhanced_calls[0][0] == endpoint.id
    assert len(upstream_calls) == 1
    sent = upstream_calls[0]
    expected = "请分两句话说明该函数的作用" if enabled else original
    if api_kind == "chat":
        assert sent["messages"][-1]["content"] == expected
        assert any(item.get("content") == "昨天的问题" for item in sent["messages"])
        assert any(item.get("content") == "keep this instruction" for item in sent["messages"])
    else:
        assert expected in str(sent["input"])
    if enabled:
        assert original not in str(sent)
    assert any(
        item["reason"] == "enhanced" for item in (
            json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        ) if item.get("event") == "prompt_enhancement"
    ) is enabled
    trace_items = asyncio.run(runtime.route_traces.list(
        limit=10, request_mode="all",
    ))["items"]
    assert len(trace_items) == 1
    trace = asyncio.run(runtime.route_traces.get(trace_items[0]["request_id"]))
    assert trace["status"] == "succeeded"
    if enabled:
        assert trace["prompt_enhancement"]["state"] == "applied"
    asyncio.run(runtime.internal_client.aclose())
    asyncio.run(runtime.close())



@pytest.mark.parametrize("api_kind,stream", [
    ("chat", False), ("responses", True),
])
def test_public_auto_enhancement_follows_selected_model(
    tmp_path, monkeypatch, api_kind, stream,
):
    runtime, secret = _public_test_runtime(
        tmp_path, monkeypatch, client_id="workbuddy-public",
    )
    runtime.settings.write_runtime({
        "identity": {"enabled": True},
        "routing": {"prompt_enhancement": {"enabled": True}},
    })
    selected = []
    upstream_calls = []

    async def fake_enhancer(_current, decision, request):
        selected.append(decision.endpoint.id)
        assert request["messages"][1]["content"].count("整理这段话") == 1
        return {
            "choices": [{"message": {"content": "请把这段话整理成简明摘要"}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 20},
        }

    monkeypatch.setattr(enhancement, "_request_model", fake_enhancer)

    async def upstream(request):
        payload = json.loads(request.content)
        upstream_calls.append(payload)
        if stream:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                    b'data: {"type":"response.completed","response":{"id":"resp-auto",'
                    b'"object":"response","status":"completed","output":[{"type":"message",'
                    b'"role":"assistant","content":[{"type":"output_text","text":"ok"}]}]}}'
                    b'\n\ndata: [DONE]\n\n'
                ),
            )
        return httpx.Response(200, json={
            "id": "chatcmpl-auto", "object": "chat.completion",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "ok",
            }, "finish_reason": "stop"}],
        })

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
    )
    body = {"model": "siyuan/auto", "stream": stream}
    if api_kind == "chat":
        body["messages"] = [
            {"role": "system", "content": (
                "stable instructions\n"
                "<workbuddy_dynamic_context>dynamic data</workbuddy_dynamic_context>"
            )},
            {"role": "user", "content": "整理这段话"},
        ]
    else:
        body["input"] = "整理这段话"
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions" if api_kind == "chat" else "/v1/responses",
            headers={"Authorization": f"Bearer {secret}"}, json=body,
        )
    assert response.status_code == 200, response.text
    assert len(selected) == len(upstream_calls) == 1
    assert "请把这段话整理成简明摘要" in str(upstream_calls[0])
    assert "整理这段话" not in str(upstream_calls[0])
    if api_kind == "chat":
        assert "<workbuddy_dynamic_context>" in str(upstream_calls[0])
        assert "dynamic data" in str(upstream_calls[0])
    assert upstream_calls[0]["model"] == runtime.registry.by_id(selected[0]).provider_model
    asyncio.run(runtime.internal_client.aclose())
    asyncio.run(runtime.close())


def test_public_error_does_not_expose_internal_model_with_switch_on(
    tmp_path, monkeypatch,
):
    runtime, secret = _public_test_runtime(
        tmp_path, monkeypatch, client_id="workbuddy-public",
    )
    runtime.settings.write_runtime({
        "identity": {"enabled": True},
        "routing": {"prompt_enhancement": {"enabled": True}},
    })
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": "internal/unknown", "messages": [
                {"role": "user", "content": "整理这段话"},
            ]},
        )
    assert response.status_code == 404
    assert "ivan-qwen38-flash-128k" not in response.text
    assert "workbuddy-qwen36-shared" not in response.text
    traces = asyncio.run(runtime.route_traces.list(
        limit=10, request_mode="all",
    ))["items"]
    assert len(traces) == 1
    assert traces[0]["status"] == "failed"
    asyncio.run(runtime.internal_client.aclose())
    asyncio.run(runtime.close())
