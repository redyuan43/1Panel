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
from ai_router.config import Registry, Settings, endpoint_from_dict
from ai_router.policy import RoutingPolicy
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import EndpointStatus
from tests.test_prompt_directives import _Health

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT_ID = "nx1-ornith-35b-a3b-96k"
PUBLIC_MODEL = "siyuan/ornith-nx1-96k"
PROVIDER_MODEL = "/home/nx/weight/Ornith-1.5-35B-A3B-IQ2_S.gguf"
LEGACY_QWEN_MODEL = "huihui/Qwen3.8-27B-Q4-DFlash2"
ALLOWED_CLIENTS = ("home-assistant", "check-boards")
REQUESTED_MODELS = ("auto", PUBLIC_MODEL, LEGACY_QWEN_MODEL)


def test_registry_declares_dedicated_ornith_endpoint() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id(ENDPOINT_ID)
    assert endpoint is not None
    assert endpoint.public_model == PUBLIC_MODEL
    assert endpoint.provider_model == PROVIDER_MODEL
    assert endpoint.api_base == "http://100.119.5.57:18083/v1"
    assert endpoint.node == "nx1"
    assert endpoint.role == "responder"
    assert endpoint.backend_type == "llama_cpp"
    assert endpoint.safe_context_tokens == 98304
    assert endpoint.configured_context_tokens == 98304
    assert endpoint.max_concurrency == 1
    assert endpoint.modalities == ("text", "image")
    assert endpoint.supports_image_count(1)
    assert not endpoint.supports_image_count(2)
    assert endpoint.capabilities.structured_output == ("json_object",)
    assert endpoint.allowed_client_ids == ALLOWED_CLIENTS
    assert endpoint.enabled is False
    assert endpoint.auto_candidate is False
    assert list(registry.by_public_model(PUBLIC_MODEL)) == [endpoint]
    # EndpointConfigManager 运行时覆盖会做 to_dict→from_dict 往返重建；
    # 允许列表必须在往返后保留，否则端点级隔离会静默失效。
    roundtrip = endpoint_from_dict(
        json.loads(json.dumps(endpoint.to_dict()))
    )
    assert roundtrip.allowed_client_ids == ALLOWED_CLIENTS
    assert roundtrip.enabled == endpoint.enabled
    assert roundtrip.auto_candidate == endpoint.auto_candidate


def test_defaults_scoped_bindings_and_policy(tmp_path: Path) -> None:
    settings = Settings(
        ROOT / "config" / "defaults.yaml",
        tmp_path / "settings.yaml",
    )
    bindings = [
        rule
        for rule in settings.section("routing").get(
            "client_route_bindings",
            [],
        )
        if rule["target_endpoint_id"] == ENDPOINT_ID
    ]
    assert {rule["client_id"] for rule in bindings} == set(ALLOWED_CLIENTS)
    for rule in bindings:
        assert set(REQUESTED_MODELS) <= set(rule["requested_models"])
    policies = settings.section("clients").get("policies", [])
    check_boards = next(
        item for item in policies if item["id"] == "check-boards"
    )
    assert PUBLIC_MODEL in check_boards["models"]
    unrelated = [
        item
        for item in policies
        if item["id"] not in ALLOWED_CLIENTS
        and PUBLIC_MODEL in item.get("models", [])
    ]
    assert unrelated == []


def _runtime(
    tmp_path: Path,
    monkeypatch,
    *,
    endpoint_enabled: bool = True,
    enable_compaction: bool = False,
):
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-internal")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "test-legacy")
    monkeypatch.setenv("AI_ROUTER_AI_BACKEND_KEY", "test-nx1")
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
    runtime_overrides: dict = {
        "identity": {"enabled": False},
        "routing": {
            "client_route_bindings": [
                {
                    "client_id": client_id,
                    "requested_models": list(REQUESTED_MODELS),
                    "target_endpoint_id": ENDPOINT_ID,
                    "ignored_route_tiers": ["local-large"],
                }
                for client_id in ALLOWED_CLIENTS
            ],
        },
    }
    if enable_compaction:
        # 强制开启压缩预检路径（_maybe_compact_for_route），
        # 用于断言内部 choose 调用携带认证身份的 client_id。
        runtime_overrides["compaction"] = {
            "enabled": True,
            "mode": "automatic",
        }
    settings.write_runtime(runtime_overrides)
    source = Registry(ROOT / "config" / "registry.yaml")
    endpoint = replace(
        source.by_id(ENDPOINT_ID),
        enabled=endpoint_enabled,
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
        endpoint_id=ENDPOINT_ID,
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
    for client_id in (*ALLOWED_CLIENTS, "unrelated-client"):
        asyncio.run(runtime.clients.create_account({
            "id": client_id,
            "name": client_id,
            "enabled": True,
            "models": [PUBLIC_MODEL],
            "rpm_limit": 120,
            "tpm_limit": 1000000,
            "max_parallel_requests": 8,
            "disclosure_mode": "internal",
            "local_only": True,
        }, allowed_models={PUBLIC_MODEL}))
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
                "model": PUBLIC_MODEL,
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
            "model": PUBLIC_MODEL,
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
                "model": PUBLIC_MODEL,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": None,
                }],
            },
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "model": PUBLIC_MODEL,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            },
            {
                "id": "chat-1",
                "object": "chat.completion.chunk",
                "model": PUBLIC_MODEL,
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


@pytest.mark.parametrize("client_id", ALLOWED_CLIENTS)
@pytest.mark.parametrize("requested_model", REQUESTED_MODELS)
def test_bound_clients_route_to_ornith(
    tmp_path: Path,
    monkeypatch,
    client_id: str,
    requested_model: str,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets[client_id]},
            json={
                "model": requested_model,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == requested_model
    assert response.headers["x-1panel-route-model"] == PUBLIC_MODEL
    assert response.headers["x-1panel-route-deployment"] == ENDPOINT_ID
    assert captured[0]["body"]["model"] == PROVIDER_MODEL
    trace = asyncio.run(
        runtime.route_traces.get(response.headers["x-request-id"])
    )
    assert trace["endpoint_id"] == ENDPOINT_ID
    assert trace["evaluation"]["required_endpoint_source"] == (
        "client_route_binding"
    )
    asyncio.run(runtime.close())


def test_image_json_request_routes_to_ornith(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lJkAAAAASUVORK5CYII="
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": image}},
        ]}],
    }
    with TestClient(create_app(runtime)) as client:
        image_only = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json=body,
        )
        image_json = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json={**body, "response_format": {"type": "json_object"}},
        )
        two_images = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json={
                **body,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Compare both images"},
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "image_url", "image_url": {"url": image}},
                ]}],
                "response_format": {"type": "json_object"},
            },
        )
    assert image_only.status_code == 200, image_only.text
    assert captured[0]["body"]["model"] == PROVIDER_MODEL
    assert image_json.status_code == 200, image_json.text
    assert captured[1]["body"]["model"] == PROVIDER_MODEL
    assert captured[1]["body"]["response_format"] == {"type": "json_object"}
    assert two_images.status_code == 422, two_images.text
    assert two_images.json()["error"]["code"] == "no_compatible_model"
    assert len(captured) == 2
    trace = asyncio.run(
        runtime.route_traces.get(two_images.headers["x-request-id"])
    )
    assert trace["status"] == "failed"
    assert trace["error"]["code"] == "no_compatible_model"
    asyncio.run(runtime.close())


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("image_json", [False, True])
def test_protocols_preserve_response_model_for_ornith(
    tmp_path: Path,
    monkeypatch,
    api_kind: str,
    stream: bool,
    image_json: bool,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    path = "/v1/chat/completions" if api_kind == "chat" else "/v1/responses"
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lJkAAAAASUVORK5CYII="
    body = (
        {
            "model": PUBLIC_MODEL,
            "messages": [{"role": "user", "content": (
                [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": image}},
                ] if image_json else "hello"
            )}],
            "stream": stream,
        }
        if api_kind == "chat"
        else {"model": PUBLIC_MODEL, "input": (
            [{"role": "user", "content": [
                {"type": "input_text", "text": "describe"},
                {"type": "input_image", "image_url": image},
            ]}] if image_json else "hello"
        ), "stream": stream}
    )
    if image_json:
        if api_kind == "chat":
            body["response_format"] = {"type": "json_object"}
        else:
            body["text"] = {"format": {"type": "json_object"}}
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer " + secrets["check-boards"]},
            json=body,
        )
    assert response.status_code == 200, response.text
    if not stream:
        assert response.json()["model"] == PUBLIC_MODEL
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
        assert set(models) == {PUBLIC_MODEL}
    assert response.headers["x-1panel-route-model"] == PUBLIC_MODEL
    assert captured[0]["body"]["model"] == PROVIDER_MODEL
    if image_json:
        assert captured[0]["body"]["response_format"] == {"type": "json_object"}
    asyncio.run(runtime.close())


def test_unrelated_client_cannot_reach_dedicated_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(tmp_path, monkeypatch)
    with TestClient(create_app(runtime)) as client:
        explicit = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["unrelated-client"]},
            json={
                "model": PUBLIC_MODEL,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        auto = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["unrelated-client"]},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert explicit.status_code == 404, explicit.text
    assert "model_not_found" in explicit.text
    # auto 请求对无关账号先被账号级模型白名单拒绝（401/403）；
    # 两种路径都必须失败关闭且不触达上游。
    assert auto.status_code in {401, 403, 404}, auto.text
    assert captured == []
    asyncio.run(runtime.close())


def test_disabled_endpoint_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(
        tmp_path,
        monkeypatch,
        endpoint_enabled=False,
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code in {404, 503}, response.text
    assert captured == []
    asyncio.run(runtime.close())


def test_compaction_preflight_receives_authenticated_client_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # 端点级 allowlist 依赖 choose 的 client_id 过滤候选。压缩预检
    # （_maybe_compact_for_route）是主路径之外的额外 choose 调用点，
    # 必须携带认证身份的 client_id，否则带 allowlist 的端点会从预检
    # 候选集中静默消失（未来 allowlist×auto_candidate 组合会放大为
    # 历史超限误路由）。后台 compaction_worker / memory_query 同理。
    # 注意：绑定账号的 evaluation 在预检前已带 required_endpoint_id、
    # 预检被短路，因此必须用无绑定账号才能覆盖预检路径。
    runtime, secrets, _ = _runtime(
        tmp_path,
        monkeypatch,
        enable_compaction=True,
    )
    # 预检只对「auto 请求 + 无绑定」的账号运行；无绑定账号还需通过
    # 账号级 models 白名单，因此单独建一个 models 含 auto 的账号。
    asyncio.run(runtime.clients.create_account({
        "id": "loose-auto-client",
        "name": "loose-auto-client",
        "enabled": True,
        "models": ["auto", PUBLIC_MODEL],
        "rpm_limit": 120,
        "tpm_limit": 1000000,
        "max_parallel_requests": 8,
        "disclosure_mode": "internal",
        "local_only": True,
    }, allowed_models={"auto", PUBLIC_MODEL}))
    _, loose_key = asyncio.run(
        runtime.clients.create_key("loose-auto-client", "test")
    )
    calls: list[str] = []
    original_choose = RoutingPolicy.choose

    async def spy_choose(self, **kwargs):
        calls.append(kwargs.get("client_id", "<missing>"))
        return await original_choose(self, **kwargs)

    monkeypatch.setattr(RoutingPolicy, "choose", spy_choose)
    with TestClient(create_app(runtime)) as client:
        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + loose_key},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert "<missing>" not in calls, calls
    assert calls.count("loose-auto-client") >= 1, calls
    asyncio.run(runtime.close())


def test_auto_with_compaction_enabled_routes_to_ornith(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, secrets, captured = _runtime(
        tmp_path,
        monkeypatch,
        enable_compaction=True,
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    assert captured[0]["body"]["model"] == PROVIDER_MODEL
    asyncio.run(runtime.close())
