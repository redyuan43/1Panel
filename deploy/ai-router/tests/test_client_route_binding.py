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
from ai_router.client_route_binding import (
    SSEModelRewriter,
    resolve_client_route,
    rewrite_response_model,
)
from ai_router.config import Registry, Settings
from ai_router.errors import RouterError
from ai_router.policy import RoutingPolicy
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import EndpointStatus, Evaluation
from tests.test_prompt_directives import _Health


ROOT = Path(__file__).resolve().parents[1]
TARGET_ENDPOINT = "ai-qwen38-27b"
TARGET_MODEL = "siyuan/qwen38-v100-196k"
LEGACY_MODELS = (
    "auto",
    "huihui/Qwen3.8-27B-Q4-DFlash2",
    "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF",
)
CLIENTS = ("home-assistant", "check-boards")


def route_binding_rules() -> list[dict]:
    return [
        {
            "client_id": client_id,
            "requested_models": list(LEGACY_MODELS),
            "target_endpoint_id": TARGET_ENDPOINT,
            "ignored_route_tiers": ["local-large"],
        }
        for client_id in CLIENTS
    ]


def test_resolver_is_client_scoped_and_fail_closed() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    routing = {"client_route_bindings": route_binding_rules()}
    endpoint = registry.by_id(TARGET_ENDPOINT)
    assert endpoint is not None

    resolved = resolve_client_route(
        routing,
        registry,
        client_id="home-assistant",
        requested_model=LEGACY_MODELS[2],
        disclosure_mode="internal",
        headers={"x-1panel-route-tier": "local-large"},
    )
    assert resolved is not None
    assert resolved.target_model == TARGET_MODEL
    assert resolved.target_endpoint_id == TARGET_ENDPOINT
    assert resolved.requested_tier == "local-large"
    assert resolved.normalized_headers({
        "x-1panel-route-tier": "local-large",
        "x-test": "kept",
    }) == {"x-test": "kept"}
    assert resolved.normalized_headers({
        "x-1panel-route-tier": "cloud-frontier",
    }) == {"x-1panel-route-tier": "cloud-frontier"}
    evaluation = Evaluation("general", None, 1.0, "test")
    resolved.apply_to_evaluation(evaluation)
    assert evaluation.required_endpoint_id == TARGET_ENDPOINT
    with pytest.raises(RouterError, match="conflicts"):
        resolved.ensure_directive_compatible("another-endpoint")
    assert resolve_client_route(
        routing,
        registry,
        client_id="unrelated-client",
        requested_model=LEGACY_MODELS[2],
        disclosure_mode="internal",
    ) is None
    assert resolve_client_route(
        routing,
        registry,
        client_id="home-assistant",
        requested_model=LEGACY_MODELS[2],
        disclosure_mode="public",
    ) is None

    broken = {
        "client_route_bindings": [{
            "client_id": "home-assistant",
            "requested_models": ["auto"],
            "target_endpoint_id": "missing-endpoint",
            "ignored_route_tiers": ["local-large"],
        }]
    }
    with pytest.raises(
        RouterError,
        match="route binding target is unavailable",
    ):
        resolve_client_route(
            broken,
            registry,
            client_id="home-assistant",
            requested_model="auto",
            disclosure_mode="internal",
        )


def test_settings_reject_duplicate_compatibility_scope(tmp_path: Path) -> None:
    settings = Settings(
        ROOT / "config" / "defaults.yaml",
        tmp_path / "settings.yaml",
    )
    duplicate = route_binding_rules()[0]
    with pytest.raises(ValueError, match="scopes must be unique"):
        settings.write_runtime({
            "routing": {
                "client_route_bindings": [duplicate, duplicate],
            }
        })


@pytest.mark.parametrize(
    "rule,error",
    [
        (
            {
                "client_id": "home-assistant",
                "requested_models": [],
                "target_endpoint_id": TARGET_ENDPOINT,
                "ignored_route_tiers": ["local-large"],
            },
            "unique nonempty model IDs",
        ),
        (
            {
                "client_id": "home-assistant",
                "requested_models": ["auto"],
                "target_endpoint_id": " ai-qwen38-27b",
                "ignored_route_tiers": ["local-large"],
            },
            "invalid identifiers",
        ),
        (
            {
                "client_id": "home-assistant",
                "requested_models": ["auto"],
                "target_endpoint_id": TARGET_ENDPOINT,
                "ignored_route_tiers": ["unknown-tier"],
            },
            "unique supported tiers",
        ),
        (
            {
                "client_id": "home-assistant",
                "requested_models": ["auto"],
                "target_endpoint_id": TARGET_ENDPOINT,
                "ignored_route_tiers": ["local-large"],
                "unexpected": True,
            },
            "invalid fields",
        ),
    ],
)
def test_settings_reject_malformed_route_bindings(
    tmp_path: Path,
    rule: dict,
    error: str,
) -> None:
    settings = Settings(
        ROOT / "config" / "defaults.yaml",
        tmp_path / "settings.yaml",
    )
    with pytest.raises(ValueError, match=error):
        settings.write_runtime({
            "routing": {"client_route_bindings": [rule]},
        })


def test_response_model_rewriters_preserve_protocol_data() -> None:
    payload = json.dumps({
        "id": "chat-1",
        "model": TARGET_MODEL,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [{
                    "type": "function",
                    "function": {
                        "name": "report",
                        "arguments": '{"model":"application-value"}',
                    },
                }],
            },
        }],
    }).encode()
    rewritten = json.loads(rewrite_response_model(payload, LEGACY_MODELS[2]))
    assert rewritten["model"] == LEGACY_MODELS[2]
    assert json.loads(
        rewritten["choices"][0]["message"]["tool_calls"][0]
        ["function"]["arguments"]
    )["model"] == "application-value"

    event = (
        'event: response.completed\r\n'
        'data: {"type":"response.completed","response":'
        '{"id":"resp-1","model":"siyuan/qwen38-v100-196k"}}\r\n\r\n'
    ).encode()
    rewriter = SSEModelRewriter("auto")
    chunks = [event[:17], event[17:61], event[61:]]
    output = b"".join(
        part
        for chunk in chunks
        for part in rewriter.feed(chunk)
    ) + b"".join(rewriter.finish())
    data_line = next(
        line for line in output.splitlines() if line.startswith(b"data: ")
    )
    value = json.loads(data_line[6:])
    assert value["response"]["model"] == "auto"


def _runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-internal")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "test-legacy")
    monkeypatch.setenv("AI_ROUTER_AI_BACKEND_KEY", "test-ai")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "traces.sqlite3"),
    )
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    settings = Settings(
        ROOT / "config" / "defaults.yaml",
        tmp_path / "settings.yaml",
    )
    settings.write_runtime({
        "identity": {"enabled": False},
        "routing": {
            "client_route_bindings": route_binding_rules(),
        },
    })
    source = Registry(ROOT / "config" / "registry.yaml")
    endpoint = replace(
        source.by_id(TARGET_ENDPOINT),
        enabled=True,
        api_base="http://upstream/v1",
        health_url="http://upstream/health",
        load_url=None,
        backend_api_key_env="AI_ROUTER_AI_BACKEND_KEY",
    )
    registry = source.with_endpoints([endpoint])
    runtime = build_runtime(
        settings=settings,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    asyncio.run(runtime.health.client.aclose())
    runtime.health = _Health(EndpointStatus(
        endpoint_id=TARGET_ENDPOINT,
        healthy=True,
        checked_at=time.time(),
        eligible_context_tokens=endpoint.safe_context_tokens,
        load_headroom=1,
    ))
    runtime.policy = RoutingPolicy(
        registry,
        settings,
        runtime.health,
        store=runtime.store,
    )
    secrets = {}
    for client_id in (*CLIENTS, "unrelated-client"):
        asyncio.run(runtime.clients.create_account({
            "id": client_id,
            "name": client_id,
            "enabled": True,
            "models": [TARGET_MODEL],
            "rpm_limit": 120,
            "tpm_limit": 1000000,
            "max_parallel_requests": 8,
            "disclosure_mode": "internal",
            "local_only": True,
        }, allowed_models={TARGET_MODEL}))
        _, secrets[client_id] = asyncio.run(
            runtime.clients.create_key(client_id, "test")
        )
    captured = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append({"path": request.url.path, "body": body})
        stream = bool(body.get("stream"))
        if request.url.path.endswith("/responses"):
            response = {
                "id": "resp-1",
                "object": "response",
                "status": "completed",
                "model": TARGET_MODEL,
                "output": [{
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{
                        "type": "output_text",
                        "text": "ok",
                        "annotations": [],
                    }],
                }],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            }
            if not stream:
                return httpx.Response(200, json=response)
            events = [
                {
                    "type": "response.created",
                    "response": {
                        **response,
                        "status": "in_progress",
                        "output": [],
                    },
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg-1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "ok",
                },
                {"type": "response.completed", "response": response},
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(
                    "data: " + json.dumps(item) + "\n\n"
                    for item in events
                ),
            )
        response = {
            "id": "chat-1",
            "object": "chat.completion",
            "model": TARGET_MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        if not stream:
            return httpx.Response(200, json=response)
        chunks = [
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "model": TARGET_MODEL,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": None,
                }],
            },
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "model": TARGET_MODEL,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            },
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "model": TARGET_MODEL,
                "choices": [],
                "usage": response["usage"],
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                "data: " + json.dumps(item) + "\n\n"
                for item in chunks
            ) + "data: [DONE]\n\n",
        )

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    return runtime, secrets, captured


@pytest.mark.parametrize("client_id", CLIENTS)
@pytest.mark.parametrize("requested_model", LEGACY_MODELS)
def test_all_legacy_names_route_to_v100(
    tmp_path: Path,
    monkeypatch,
    client_id: str,
    requested_model: str,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + secrets[client_id],
                "X-1Panel-Route-Tier": "local-large",
            },
            json={
                "model": requested_model,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == requested_model
    assert response.headers["x-1panel-route-model"] == TARGET_MODEL
    assert response.headers["x-1panel-route-deployment"] == TARGET_ENDPOINT
    assert captured[0]["body"]["model"] == TARGET_MODEL
    trace = asyncio.run(
        runtime.route_traces.get(response.headers["x-request-id"])
    )
    assert trace["requested_model"] == requested_model
    assert trace["client_route_resolution"] == {
        "requested_model": requested_model,
        "resolved_model": TARGET_MODEL,
        "target_endpoint_id": TARGET_ENDPOINT,
        "replaced_constraints": {"route_tier": "local-large"},
    }
    assert trace["endpoint_id"] == TARGET_ENDPOINT
    assert trace["evaluation"]["required_endpoint_id"] == TARGET_ENDPOINT
    assert trace["evaluation"]["required_endpoint_source"] == (
        "client_route_binding"
    )
    assert trace["evaluation"]["required_tier"] is None
    assert trace["evaluation"]["evidence"]["client_route_resolution"] == (
        TARGET_ENDPOINT
    )
    listed = asyncio.run(
        runtime.route_traces.list(limit=10, request_mode="all")
    )
    listed_trace = next(
        item
        for item in listed["items"]
        if item["request_id"] == response.headers["x-request-id"]
    )
    assert listed_trace["client_route_resolution"] == (
        trace["client_route_resolution"]
    )
    asyncio.run(runtime.close())


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_compatibility_preserves_response_model_for_all_protocols(
    tmp_path: Path,
    monkeypatch,
    api_kind: str,
    stream: bool,
) -> None:
    runtime, secrets, _ = _runtime(tmp_path, monkeypatch)
    requested_model = LEGACY_MODELS[2]
    path = "/v1/chat/completions" if api_kind == "chat" else "/v1/responses"
    body = (
        {
            "model": requested_model,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        }
        if api_kind == "chat"
        else {"model": requested_model, "input": "hello", "stream": stream}
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json=body,
        )
    assert response.status_code == 200, response.text
    if not stream:
        assert response.json()["model"] == requested_model
    else:
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        models = [
            model
            for event in events
            for model in (
                event.get("model"),
                (
                    event.get("response", {}).get("model")
                    if isinstance(event.get("response"), dict)
                    else None
                ),
            )
            if model is not None
        ]
        assert models
        assert set(models) == {requested_model}
    assert response.headers["x-1panel-route-model"] == TARGET_MODEL
    asyncio.run(runtime.close())


def test_unrelated_client_cannot_use_compatibility_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + secrets["unrelated-client"],
            },
            json={
                "model": LEGACY_MODELS[2],
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert captured == []
    asyncio.run(runtime.close())


def test_route_tier_is_still_enforced_without_compatibility_match(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + secrets["unrelated-client"],
                "X-1Panel-Route-Tier": "local-large",
            },
            json={
                "model": TARGET_MODEL,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_eligible_model"
    assert captured == []
    trace = asyncio.run(
        runtime.route_traces.get(response.headers["x-request-id"])
    )
    assert trace["evaluation"]["required_tier"] == "local-large"
    assert trace["evaluation"]["required_endpoint_id"] is None
    assert trace["evaluation"]["required_endpoint_source"] is None
    asyncio.run(runtime.close())


def test_route_binding_replaces_only_declared_legacy_tier(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + secrets["home-assistant"],
                "X-1Panel-Route-Tier": "cloud-frontier",
            },
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_compatible_model"
    assert captured == []
    trace = asyncio.run(
        runtime.route_traces.get(response.headers["x-request-id"])
    )
    assert trace["evaluation"]["required_tier"] == "cloud-frontier"
    assert trace["evaluation"]["required_endpoint_id"] == TARGET_ENDPOINT
    assert trace["evaluation"]["required_endpoint_source"] == (
        "client_route_binding"
    )
    assert "replaced_constraints" not in trace["client_route_resolution"]
    asyncio.run(runtime.close())


def test_console_distinguishes_request_target_and_actual_result() -> None:
    app = (ROOT / "ai_router" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "客户端请求" in app
    assert "兼容目标" in app
    assert "尚未调用模型" in app
    assert "client_route_resolution" in app
