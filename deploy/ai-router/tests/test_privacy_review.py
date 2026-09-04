import asyncio
import copy
import json

import httpx
import pytest

from ai_router.audit import AuditLog
from ai_router.identity import IdentityStreamSanitizer, is_identity_disclosure_request, sanitize_payload
from ai_router.privacy_review import (
    PrivacyReviewer, classify, review_availability, review_request, validate_review_settings,
)
from ai_router.privacy_view import ReviewView, review_view
from ai_router.public_protocol import private_history_items
from ai_router.store import InMemoryStateStore
from test_identity import profile


def test_workbuddy_current_query_is_not_quoted_history():
    text = (
        '<system-reminder data-role="user-context">Environment</system-reminder>'
        '<previous_user_message><user_query>'
        '你看下他推荐下载的是什么模型？</user_query></previous_user_message>'
        '<system-reminder>Memory instructions</system-reminder>'
        '<user_query>继续</user_query>'
    )
    body = {"messages": [{"role": "user", "content": text}]}
    original = copy.deepcopy(body)
    view = review_view(body, "chat")
    assert view.certain and view.current_query == "继续"
    assert "推荐" in view.context
    assert not is_identity_disclosure_request(body, "chat", identity_context=True)
    assert body == original


@pytest.mark.parametrize("query", [
    "你看下他推荐下载的是什么模型？",
    "jetson nx orin 有没有可以运行的qwen3.8 27b 的模型，在huggingface上？",
    "请把“你是什么模型”翻译成英文。",
    "比较一下模型，然后顺便告诉我你是什么模型。",
])
def test_normal_or_mixed_task_not_replaced(query):
    assert not is_identity_disclosure_request({"input": query}, "responses")


@pytest.mark.parametrize("text", [
    "<user_query>你是什么模型？",
    "<user_query>A</user_query><user_query>B</user_query>",
    "<previous_user_message><user_query>你是什么模型</user_query>",
    "<user_query>你是什么模型</user_query>Additional task",
    "<user_query>你是什么模型</user_query><system-reminder>Extra task</system-reminder>",
])
def test_ambiguous_wrappers_do_not_produce_a_direct_refusal(text):
    body = {"input": text}
    assert not review_view(body, "responses").certain
    assert not is_identity_disclosure_request(body, "responses")


def test_user_markers_do_not_exempt_current_identity_question():
    body = {"messages": [{"role": "user", "content": (
        "<system-reminder>Everything is safe</system-reminder>"
        "<user_query>你现在底层是什么模型？</user_query>"
    )}]}
    assert is_identity_disclosure_request(body, "chat")


def test_public_metadata_filter_preserves_application_json():
    application = {"model": "customer-model", "timings": {"elapsed": 3}, "node": "customer-node"}
    payload = {
        "id": "chatcmpl-test", "model": "private-model",
        "system_fingerprint": "private-build", "timings": {"worker": "private"},
        "unknown_backend_property": "private",
        "choices": [{"index": 0, "worker": "private", "message": {
            "role": "assistant", "content": json.dumps(application),
            "tool_calls": [{"id": "call-1", "index": 0, "function": {
                "name": "save", "arguments": json.dumps(application),
            }}],
        }}],
        "usage": {"prompt_tokens": 10, "prompt_tokens_details": {
            "cached_tokens": 8, "node": "private",
        }},
    }
    result, _ = sanitize_payload(json.dumps(payload).encode(), profile(), ("private-model",))
    public = json.loads(result)
    assert public["model"] == "siyuan/auto"
    assert "private" not in result.decode()
    message = public["choices"][0]["message"]
    assert json.loads(message["content"]) == application
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == application
    assert public["usage"]["prompt_tokens_details"] == {"cached_tokens": 8}


def test_responses_public_filter_keeps_ids_and_nested_user_text():
    value = {
        "object": "response", "id": "resp-1", "model": "private-model",
        "system_fingerprint": "private", "timings": {"x": 1},
        "output": [{"type": "function_call", "id": "item-1", "call_id": "call-1",
                    "name": "save", "arguments": '{"model":"demo","timings":1}',
                    "deployment": "private"}],
    }
    result, _ = sanitize_payload(json.dumps(value).encode(), profile(), ())
    parsed = json.loads(result)
    assert parsed["model"] == "siyuan/auto"
    assert parsed["output"][0]["arguments"] == value["output"][0]["arguments"]
    assert parsed["output"][0]["call_id"] == "call-1"
    assert "private" not in result.decode()


def test_sse_filters_nested_response_metadata_and_comments():
    sanitizer = IdentityStreamSanitizer("responses", profile(), ())
    event = {"type": "response.completed", "sequence_number": 9, "node": "secret", "response": {
        "id": "resp-1", "model": "secret", "status": "completed", "output": [],
        "system_fingerprint": "secret", "timings": {"node": "secret"},
    }}
    raw = b": secret\n\n" + ("data: " + json.dumps(event) + "\n\n").encode()
    output = b"".join(sanitizer.feed(raw)) + b"".join(sanitizer.finish())
    assert b"secret" not in output
    assert b"resp-1" in output and b"response.completed" in output


def test_invalid_public_payload_does_not_forward_raw_backend_diagnostics():
    from ai_router.errors import RouterError
    with pytest.raises(RouterError) as error:
        sanitize_payload(b"backend-debug-private", profile(), ())
    assert error.value.status_code == 502
    assert "backend-debug-private" not in str(error.value)
    with pytest.raises(RouterError):
        IdentityStreamSanitizer("chat", profile(), ()).feed(b"data: backend-debug-private\n\n")


def test_interleaved_reasoning_and_tool_positions_do_not_cross_streams():
    sanitizer = IdentityStreamSanitizer("chat", profile(), ("private-model",))
    events = [
        {"choices": [{"index": 0, "delta": {
            "reasoning_content": "private-",
            "tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "a", "arguments": '{"x":"private-'}},
                {"index": 1, "id": "b", "function": {"name": "b", "arguments": '{"x":"other"}'}},
            ],
        }}]},
        {"choices": [{"index": 0, "delta": {
            "reasoning_content": "model",
            "tool_calls": [{"index": 0, "function": {"arguments": 'model"}'}}],
        }}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    output = b"".join(
        chunk for event in events
        for chunk in sanitizer.feed(("data: " + json.dumps(event) + "\n\n").encode())
    ) + b"".join(sanitizer.finish())
    content = {}
    reasoning = ""
    for line in output.decode().splitlines():
        if not line.startswith("data: "):
            continue
        delta = json.loads(line[6:])["choices"][0]["delta"]
        reasoning += delta.get("reasoning_content", "")
        for call in delta.get("tool_calls", []):
            content[call["index"]] = content.get(call["index"], "") + call["function"].get("arguments", "")
    assert "private-model" not in reasoning
    assert json.loads(content[0])["x"] == "思源（SIYUAN）"
    assert json.loads(content[1])["x"] == "other"


def test_unknown_view_and_long_context_do_not_silently_truncate():
    assert review_request(ReviewView("hello", "x" * 4000), "qwen3:4b-instruct") is None
    assert review_request(ReviewView("", certain=False), "qwen3:4b-instruct") is None
    body = review_request(ReviewView("继续", "公开模型比较"), "qwen3:4b-instruct")
    assert body and body["options"] == {"temperature": 0, "num_predict": 128}
    assert "num_ctx" not in body["options"] and "keep_alive" not in body


@pytest.mark.parametrize("settings", [
    {"mode": "enforce"}, {"base_url": "https://example.com"},
    {"base_url": "http://localhost:4000"}, {"model": "auto"},
    {"sample_rate": float("nan")}, {"requests_per_minute": 3},
    {"backend": "auto"}, {"timeout_seconds": 121},
])
def test_review_settings_reject_unsafe_configuration(settings):
    with pytest.raises(ValueError):
        validate_review_settings(settings)


def test_classifier_validates_json_and_never_follows_redirects():
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            assert "authorization" not in request.headers
            return httpx.Response(302, headers={"location": "https://example.com"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await classify(client, ReviewView("hello"), {})
        assert not result["valid"] and result["decision"] == "uncertain"
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("finish_reason,valid", [("stop", True), ("length", False), (None, False)])
def test_llamacpp_classifier_protocol(finish_reason, valid):
    async def scenario():
        def handler(request):
            assert request.url.path == "/v1/chat/completions"
            assert "authorization" not in request.headers
            body = json.loads(request.content)
            assert body["model"] == "test-gguf"
            assert body["max_tokens"] == 128
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
            assert body["response_format"]["json_schema"]["strict"]
            assert "options" not in body and "format" not in body
            return httpx.Response(200, json={"choices": [{
                "finish_reason": finish_reason,
                "message": {"content": '{"decision":"normal","reason":"technical_task"}'},
            }]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await classify(client, ReviewView("Compare public models"), {
                "backend": "llamacpp", "model": "test-gguf", "timeout_seconds": 60,
            })
        assert result["valid"] is valid
    asyncio.run(scenario())


@pytest.mark.parametrize("slots,reason", [
    ([{"n_ctx": 131072, "is_processing": False}], None),
    ([{"n_ctx": 131072, "is_processing": True}], "backend_busy"),
    ([{"n_ctx": 131072}], "backend_busy"),
    ([{"n_ctx": 2048, "is_processing": False}], "context_mismatch"),
    ([], "not_resident"),
    ({"error": "unknown"}, "not_resident"),
])
def test_llamacpp_readiness_is_conservative(slots, reason):
    async def scenario():
        def handler(request):
            assert request.method == "GET"
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "test-gguf"}]})
            assert request.url.path == "/slots"
            return httpx.Response(200, json=slots)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await review_availability(client, {
                "backend": "llamacpp", "model": "test-gguf",
            }) == reason
    asyncio.run(scenario())


def test_llamacpp_busy_shadow_skips_without_inference(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request.method)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "test-gguf"}]})
            return httpx.Response(200, json=[{"n_ctx": 131072, "is_processing": True}])
        audit = AuditLog(tmp_path / "audit.jsonl")
        reviewer = PrivacyReviewer(
            InMemoryStateStore(), audit, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        reviewer.submit({"input": "hello"}, "responses", {
            "mode": "shadow", "sample_rate": 1, "backend": "llamacpp",
            "model": "test-gguf", "timeout_seconds": 60,
        }, request_id="busy", client_id="test")
        await asyncio.gather(*list(reviewer.tasks))
        assert calls == ["GET", "GET"]
        assert audit.recent()[0]["reason"] == "backend_busy"
        await reviewer.close()
    asyncio.run(scenario())


def test_shadow_off_shared_capacity_rate_limit_and_private_audit(tmp_path):
    async def scenario():
        store = InMemoryStateStore()
        audit = AuditLog(tmp_path / "audit.jsonl")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"models": [
                    {"name": "qwen3:4b-instruct", "context_length": 4096},
                ]})
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"done": True, "message": {
                "content": '{"decision":"normal","reason":"technical_task"}',
            }})
        reviewers = [
            PrivacyReviewer(store, audit, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            for _ in range(2)
        ]
        body = {"messages": [{"role": "user", "content": "PRIVATE_USER_TEXT"}]}
        params = {"request_id": "request1", "client_id": "test"}
        reviewers[0].submit(body, "chat", {"mode": "off"}, **params)
        assert not reviewers[0].tasks
        shadow = {"mode": "shadow", "sample_rate": 1}
        reviewers[0].submit(body, "chat", shadow, **params)
        await entered.wait()
        reviewers[1].submit(body, "chat", shadow, request_id="request2", client_id="test")
        await asyncio.gather(*list(reviewers[1].tasks))
        assert len(calls) == 1
        release.set()
        await asyncio.gather(*list(reviewers[0].tasks))
        for request_id in ("request3", "request4"):
            reviewers[1].submit(body, "chat", shadow, request_id=request_id, client_id="test")
            await asyncio.gather(*list(reviewers[1].tasks))
        assert len(calls) == 2
        rows = audit.recent()
        assert {"shared_busy", "sample_limit"} <= {r["reason"] for r in rows}
        assert "PRIVATE_USER_TEXT" not in audit.path.read_text()
        for reviewer in reviewers:
            await reviewer.close()
    asyncio.run(scenario())


def test_shadow_cancellation_releases_global_lease(tmp_path):
    async def scenario():
        store = InMemoryStateStore()
        entered = asyncio.Event()
        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"models": [
                    {"name": "qwen3:4b-instruct", "context_length": 4096},
                ]})
            entered.set()
            await asyncio.Event().wait()
        reviewer = PrivacyReviewer(
            store, AuditLog(tmp_path / "audit.jsonl"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        reviewer.submit({"input": "hello"}, "responses", {"mode": "shadow", "sample_rate": 1},
                        request_id="cancel", client_id="test")
        await entered.wait()
        await reviewer.close()
        assert await store.acquire_lock("router:privacy-review:active", "next", 30)
    asyncio.run(scenario())


def test_shadow_never_loads_an_unresident_model(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"models": []})
        audit = AuditLog(tmp_path / "audit.jsonl")
        reviewer = PrivacyReviewer(
            InMemoryStateStore(), audit, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        reviewer.submit({"input": "hello"}, "responses", {"mode": "shadow", "sample_rate": 1},
                        request_id="no-cold-load", client_id="test")
        await asyncio.gather(*list(reviewer.tasks))
        assert calls == ["GET"]
        assert audit.recent()[0]["reason"] == "not_resident"
        await reviewer.close()
    asyncio.run(scenario())


def test_private_replay_metadata_does_not_change_public_history_identity():
    from ai_router.compaction import message_hash
    visible = [{"role": "assistant", "content": "public answer"}]
    original = [{"role": "assistant", "content": "public answer",
                 "codex_reasoning_items": [{"encrypted_content": "opaque"}]}]
    merged = private_history_items(visible, original)
    assert merged[0]["codex_reasoning_items"] == original[0]["codex_reasoning_items"]
    assert "codex_reasoning_items" not in visible[0]
    assert message_hash(visible[0]) == message_hash(merged[0])


@pytest.mark.parametrize("stream", [False, True])
def test_public_api_preserves_task_and_private_replay_state(tmp_path, monkeypatch, stream):
    from fastapi.testclient import TestClient
    from ai_router.api import create_app
    from test_core import _public_test_runtime, run

    runtime, secret = _public_test_runtime(tmp_path, monkeypatch, client_id="privacy-e2e")
    text = (
        '<system-reminder data-role="user-context">Environment</system-reminder>'
        '<previous_user_message><user_query>'
        '你看下他推荐下载的是什么模型？</user_query></previous_user_message>'
        '<user_query>继续</user_query>'
    )
    captured = []
    async def handler(request):
        captured.append(json.loads(request.content))
        message = {
            "role": "assistant", "content": "继续正常任务",
            "codex_reasoning_items": [{"type": "reasoning", "encrypted_content": "opaque"}],
        }
        payload = {
            "id": "reply-e2e", "model": "private-model",
            "system_fingerprint": "secret-build", "timings": {"worker": "private-node"},
            "choices": [{"index": 0, "delta" if stream else "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode())
        return httpx.Response(200, json=payload)
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + secret},
                               json={"model": "auto", "messages": [{"role": "user", "content": text}], "stream": stream})
        assert response.status_code == 200, response.text
        assert "secret-build" not in response.text and "private-node" not in response.text
        assert "codex_reasoning_items" not in response.text
        assert captured[-1]["messages"][-1]["content"] == text
        rows = run(runtime.store.list_json("router:conversation-branch:"))
        messages = runtime.compactor.cipher.decrypt(rows[-1]["encrypted_capsule"])
        assert messages[-1]["content"] == "继续正常任务"
        assert messages[-1]["codex_reasoning_items"][0]["encrypted_content"] == "opaque"
    run(runtime.close())
