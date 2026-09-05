import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from ai_router.audit import AuditLog
from ai_router.privacy_review import PrivacyReviewer, classify, review_availability, validate_review_settings
from ai_router.privacy_view import ReviewView
from ai_router.route_trace import DecisionTrace, RouteTraceStore
from ai_router.store import InMemoryStateStore


def make_trace(request_id="original"):
    return DecisionTrace(
        request_id=request_id, client_id="public-test", key_id="test", protocol="chat",
        requested_model="auto", excerpt={"text": "safe excerpt"}, instance_id="local",
        boot_id="test", settings_hash="test", registry_hash="test",
    )


def test_observation_survives_trace_updates_and_feedback_is_separate(tmp_path):
    async def scenario():
        store = RouteTraceStore(tmp_path / "traces.sqlite3")
        trace = make_trace()
        await store.save(trace)
        value = {
            "request_id": "original", "updated_at": time.time(), "status": "completed",
            "decision": "normal", "reason": "technical_task", "valid": True,
            "raw_prompt": "NEVER_PERSIST",
        }
        await store.save_privacy_assessment(value)
        await store.save_privacy_assessment({**value, "updated_at": 1, "status": "pending"})
        await store.save(trace)
        await store.add_privacy_feedback(
            "original", decision="internal_info", note="human correction", reviewer_source="test",
        )
        result = await store.get("original")
        assert result["privacy_assessment"]["status"] == "completed"
        assert "raw_prompt" not in result["privacy_assessment"]
        assert result["privacy_feedback"][0]["decision"] == "internal_info"
        assert result["current_review"] is None and result["review_status"] == "unreviewed"
        assert (await store.list(privacy_decision="normal"))["total_count"] == 1
        assert (await store.list(privacy_decision="internal_info"))["total_count"] == 0
        assert (await store.list())["items"][0]["privacy_assessment"]["decision"] == "normal"
        with pytest.raises(KeyError):
            await store.add_privacy_feedback("missing", decision="normal", note=None, reviewer_source="test")
        with pytest.raises(ValueError):
            await store.add_privacy_feedback("original", decision="allow", note=None, reviewer_source="test")
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("UPDATE route_traces SET started_at=1")
        await store.cleanup()
        with sqlite3.connect(store.database_path) as connection:
            assert connection.execute("SELECT count(*) FROM privacy_assessments").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM privacy_feedback").fetchone()[0] == 0
    asyncio.run(scenario())


def test_expired_pending_observation_is_not_stuck_forever(tmp_path):
    async def scenario():
        store = RouteTraceStore(tmp_path / "traces.sqlite3")
        await store.save(make_trace())
        await store.save_privacy_assessment({
            "request_id": "original", "updated_at": time.time(), "status": "pending", "deadline_at": 1,
        })
        assert (await store.get("original"))["privacy_assessment"]["reason"] == "expired"
        assert (await store.list())["items"][0]["privacy_assessment"]["status"] == "unavailable"
    asyncio.run(scenario())


def test_auto_filter_includes_public_alias_and_excludes_reviewer_model(tmp_path):
    async def scenario():
        store = RouteTraceStore(tmp_path / "traces.sqlite3")
        for index, model in enumerate(["auto", "siyuan/auto", "custom-unified", "local-reviewer"]):
            trace = make_trace(str(index))
            trace.payload["requested_model"] = model
            await store.save(trace)
        assert (await store.list(request_mode="auto"))["total_count"] == 2
        explicit = await store.list(request_mode="explicit")
        assert {item["requested_model"] for item in explicit["items"]} == {"custom-unified", "local-reviewer"}
        configured = await store.list(request_mode="auto", auto_models=("custom-unified",))
        assert {item["requested_model"] for item in configured["items"]} == {"auto", "custom-unified"}
    asyncio.run(scenario())


@pytest.mark.parametrize("url", [
    "http://localhost:4000", "http://127.0.0.1:4001", "http://agx.taild500c8.ts.net:4000",
    "http://127.0.0.1:8080", "https://127.0.0.1:4000",
])
def test_router_credential_is_restricted_to_exact_loopback(url):
    with pytest.raises(ValueError):
        validate_review_settings({"backend": "router", "base_url": url})


def test_router_review_auth_correlation_and_durable_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_PRIVACY_REVIEW_KEY", "test-review-key")
    async def scenario():
        store = RouteTraceStore(tmp_path / "traces.sqlite3")
        await store.save(make_trace())
        calls = []
        def handler(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer test-review-key"
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "local-test"}]})
            assert request.headers["x-request-id"] == "privacy-review:original"
            assert request.headers["x-1panel-conversation-id"] == "privacy-review-original"
            assert json.loads(request.content)["model"] == "local-test"
            return httpx.Response(200, headers={"x-request-id": "review-actual-id"}, json={
                "choices": [{"finish_reason": "stop", "message": {
                    "content": '{"decision":"internal_info","reason":"internal_identity"}',
                }}],
            })
        reviewer = PrivacyReviewer(
            InMemoryStateStore(), AuditLog(tmp_path / "audit.jsonl"),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)), traces=store,
        )
        reviewer.submit({"input": "what backend are you?"}, "responses", {
            "mode": "shadow", "backend": "router", "base_url": "http://127.0.0.1:4000",
            "model": "local-test", "sample_rate": 1,
        }, request_id="original", client_id="public-test")
        await asyncio.gather(*list(reviewer.tasks))
        await reviewer.close()
        value = (await store.get("original"))["privacy_assessment"]
        assert value["decision"] == "internal_info"
        assert value["status"] == "completed"
        assert value["review_request_id"] == "review-actual-id"
        assert len(calls) == 2
    asyncio.run(scenario())


def test_router_without_credential_never_calls_backend(monkeypatch):
    monkeypatch.delenv("AI_ROUTER_PRIVACY_REVIEW_KEY", raising=False)
    async def scenario():
        def handler(request):
            pytest.fail("must not call without credential")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            settings = {"backend": "router", "base_url": "http://127.0.0.1:4000", "model": "test"}
            with pytest.raises(ValueError):
                await review_availability(client, settings)
            result = await classify(client, ReviewView("hello"), settings)
            assert not result["valid"]
    asyncio.run(scenario())


def test_shadow_storage_failure_does_not_escape(tmp_path):
    class BrokenStorage:
        async def save_privacy_assessment(self, value):
            raise OSError("disk unavailable")
    async def scenario():
        reviewer = PrivacyReviewer(InMemoryStateStore(), AuditLog(tmp_path / "audit.jsonl"), traces=BrokenStorage())
        reviewer.submit({"input": "x" * 10000}, "responses", {"mode": "shadow", "sample_rate": 1},
                        request_id="original", client_id="test")
        await reviewer.close()
    asyncio.run(scenario())


def test_privacy_feedback_admin_api_and_filter(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from ai_router.control import create_app
    from test_core import _public_test_runtime, run

    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-test")
    runtime, public_key = _public_test_runtime(tmp_path, monkeypatch, client_id="observation-test")
    run(runtime.route_traces.save(make_trace()))
    run(runtime.route_traces.save_privacy_assessment({
        "request_id": "original", "updated_at": time.time(), "status": "completed",
        "decision": "internal_info", "reason": "internal_identity", "valid": True,
    }))
    path = "/api/route-traces/original/privacy-feedback"
    headers = {"Authorization": "Bearer admin-test"}
    with TestClient(create_app(runtime)) as client:
        assert client.post(path, json={"decision": "normal"}).status_code == 401
        assert client.post(path, headers={"Authorization": "Bearer " + public_key},
                           json={"decision": "normal"}).status_code in {401, 403}
        assert client.post(path, headers=headers, json={"decision": "allow"}).status_code == 400
        assert client.post(path, headers=headers, json={"decision": "normal", "note": "false positive"}).status_code == 200
        detail = client.get("/api/route-traces/original", headers=headers).json()["trace"]
        assert detail["privacy_feedback"][0]["decision"] == "normal"
        assert detail["privacy_assessment"]["decision"] == "internal_info"
        assert detail["review_status"] == "unreviewed"
        listing = client.get("/api/route-traces", headers=headers,
                             params={"privacy_decision": "internal_info"}).json()
        assert listing["total_count"] == 1
        assert client.get("/api/route-traces", headers=headers,
                          params={"privacy_decision": "invalid"}).status_code == 400
    run(runtime.close())


def test_wrong_shadow_verdict_never_replaces_primary_answer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from ai_router.api import create_app
    from test_core import _public_test_runtime, run

    runtime, secret = _public_test_runtime(tmp_path, monkeypatch, client_id="shadow-primary")
    runtime.settings.write_runtime({"identity": {
        **runtime.settings.section("identity"),
        "review": {"mode": "shadow", "sample_rate": 1},
    }})

    def reviewer_handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{
                "name": "qwen3:4b-instruct", "context_length": 4096,
            }]})
        return httpx.Response(200, json={"done": True, "message": {
            "content": '{"decision":"internal_info","reason":"internal_identity"}',
        }})

    runtime.privacy_reviewer = PrivacyReviewer(
        runtime.store, runtime.audit,
        httpx.AsyncClient(transport=httpx.MockTransport(reviewer_handler)),
        traces=runtime.route_traces,
    )
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={
            "id": "main-reply", "model": "private-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "9",
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        })
    ))
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + secret},
                               json={"model": "auto", "messages": [{"role": "user", "content": "Calculate 4+5."}]})
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "9"
        assert "privacy_assessment" not in response.text
        request_id = response.headers["x-request-id"]
        for _ in range(100):
            trace = run(runtime.route_traces.get(request_id))
            assessment = trace.get("privacy_assessment")
            if assessment and assessment["status"] == "completed":
                break
            time.sleep(0.01)
        assert assessment["decision"] == "internal_info"
        assert trace["status"] == "succeeded"
    run(runtime.close())
