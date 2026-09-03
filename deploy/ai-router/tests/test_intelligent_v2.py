from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import (
    _maybe_compact_for_route,
    _prepare_routed_body,
    create_app,
)
from ai_router.config import Registry, Settings
from ai_router.errors import (
    HistoryMigrationRequiredError,
    NoCompatibleModelError,
    NoEligibleModelError,
)
from ai_router.history import (
    deepseek_history_requires_migration,
    normalize_history_for_provider,
)
from ai_router.identity import IdentityProfile
from ai_router.policy import RoutingPolicy
from ai_router.responses_adapter import (
    chat_response_to_responses,
    chat_stream_to_responses,
)
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import (
    Endpoint,
    EndpointStatus,
    Evaluation,
    RequestCapabilities,
    RouteDecision,
)


ROOT = Path(__file__).resolve().parents[1]


def run(value):
    return asyncio.run(value)


class FakeHealth:
    def __init__(
        self,
        statuses: dict[str, EndpointStatus],
        *,
        cooldown_ids: set[str] | None = None,
    ) -> None:
        self.status_values = statuses
        self.cooldown_ids = cooldown_ids or set()

    async def statuses(self, endpoints, *, force_refresh: bool = False):
        return {
            endpoint.id: self.status_values[endpoint.id]
            for endpoint in endpoints
        }

    async def status(self, endpoint, *, force_refresh: bool = False):
        return self.status_values[endpoint.id]

    async def in_cooldown(self, endpoint_id: str) -> bool:
        return endpoint_id in self.cooldown_ids

    async def in_capability_cooldown(
        self,
        _deployment_id: str,
        _capability: str,
    ) -> bool:
        return False


def v2_settings(
    tmp_path: Path,
    *,
    identity_enabled: bool = False,
) -> Settings:
    value = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    runtime_value = {
        "routing": {"strategy": "intelligent_v2"},
        "cloud": {
            "enabled": True,
            "auto_escalate": True,
            "monthly_budget": 10,
            "allowed_providers": [
                "deepseek",
                "zhipu-coding",
                "openai-codex",
            ],
            "allowed_models": [
                "deepseek/deepseek-v4-flash",
                "zhipu/glm-5.3-flash",
                "codex-pro/gpt-5.6-sol",
            ],
        }
    }
    if identity_enabled:
        runtime_value["identity"] = {"enabled": True}
    value.write_runtime(runtime_value)
    return value


def v2_registry(tmp_path: Path) -> Registry:
    value = yaml.safe_load(
        (ROOT / "config" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    for endpoint in value["endpoints"]:
        if endpoint["id"] == "zhipu-glm-5.3-flash":
            endpoint["auto_candidate"] = True
            endpoint["safe_context_tokens"] = 262144
            endpoint["configured_context_tokens"] = 262144
            endpoint["capabilities"]["validation_status"] = (
                "test-validated"
            )
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return Registry(path)


def status_for(
    endpoint: Endpoint,
    *,
    healthy: bool = True,
) -> EndpointStatus:
    workers = []
    if endpoint.backend_type == "ai_pool" and healthy:
        workers = [
            {
                "worker_id": "ai-test-worker",
                "api_base": "http://127.0.0.1:18199/v1",
                "profile_id": "v10032-qwen38-196k",
                "tier": "v100_32_single",
                "priority": 0,
                "gpu_ids": ["3"],
                "gpu_uuids": ["GPU-test"],
                "names": ["Tesla V100"],
                "port": 18199,
                "context_size": 196608,
                "safe_context_tokens": 196608,
                "cache_type_k": "f16",
                "cache_type_v": "f16",
                "modalities": ["text", "image"],
                "vision_status": "test-validated",
                "max_images": 1,
                "runtime_fingerprint": "test",
                "ready": True,
                "state": "available",
                "config_drift": [],
                "short_request_rank": 0,
            }
        ]
    elif endpoint.backend_type == "codex_pool" and healthy:
        workers = [
            {
                "worker_id": "codex-primary",
                "ready": True,
                "state": "available",
                "safe_context_tokens": endpoint.safe_context_tokens,
                "api_base": (
                    "http://127.0.0.1:14010/v1/accounts/primary"
                ),
            }
        ]
    return EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=healthy,
        checked_at=time.time(),
        load_headroom=1.0 if healthy else 0.0,
        latency_score=1.0,
        cache_generation="test",
        eligible_context_tokens=(
            endpoint.safe_context_tokens if healthy else 0
        ),
        detail={"workers": workers},
    )


def policy_for(
    tmp_path: Path,
    *,
    local_healthy: bool,
) -> tuple[RoutingPolicy, Registry]:
    registry = v2_registry(tmp_path)
    statuses = {
        endpoint.id: status_for(
            endpoint,
            healthy=endpoint.cloud or local_healthy,
        )
        for endpoint in registry.endpoints
    }
    return (
        RoutingPolicy(
            registry,
            v2_settings(tmp_path),
            FakeHealth(statuses),
        ),
        registry,
    )


def test_v2_complex_code_cannot_bypass_eligible_local_model(
    tmp_path: Path,
) -> None:
    policy, _registry = policy_for(
        tmp_path,
        local_healthy=True,
    )
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code",
                None,
                1.0,
                "test",
                route_profile="code",
                complexity="complex",
            ),
            prompt_tokens=1000,
            output_reserve_tokens=65536,
            modalities={"text"},
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.cloud is False
    assert decision.reason == "local_sufficient"
    assert decision.strategy_version == "intelligent_v2"
    assert decision.remote_fallback_position is None


@pytest.mark.parametrize(
    ("profile", "complexity", "modalities", "expected"),
    [
        ("general", "standard", {"text"}, "cloud-deepseek-v4-flash"),
        ("agent_text", "standard", {"text"}, "cloud-deepseek-v4-flash"),
        ("code", "standard", {"text"}, "zhipu-glm-5.3-flash"),
        ("code", "complex", {"text"}, "codex-pro-gpt-5.6-sol"),
        (
            "multimodal",
            "standard",
            {"text", "image"},
            "zhipu-glm-5.3-flash",
        ),
        (
            "multimodal",
            "complex",
            {"text", "image"},
            "codex-pro-gpt-5.6-sol",
        ),
    ],
)
def test_v2_remote_order_is_profile_driven(
    tmp_path: Path,
    profile: str,
    complexity: str,
    modalities: set[str],
    expected: str,
) -> None:
    policy, _registry = policy_for(
        tmp_path,
        local_healthy=False,
    )
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code" if "code" in profile else "general",
                None,
                1.0,
                "test",
                route_profile=profile,
                complexity=complexity,
            ),
            prompt_tokens=1000,
            output_reserve_tokens=65536,
            modalities=modalities,
            image_count=1 if "image" in modalities else 0,
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.id == expected
    assert decision.remote_fallback_position == 1


def test_v2_output_limit_skips_codex_subscription(
    tmp_path: Path,
) -> None:
    policy, _registry = policy_for(
        tmp_path,
        local_healthy=False,
    )
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "code",
                None,
                1.0,
                "test",
                route_profile="code",
                complexity="complex",
            ),
            prompt_tokens=1000,
            output_reserve_tokens=65536,
            modalities={"text"},
            has_tools=False,
            required_capabilities=RequestCapabilities(
                protocol="chat",
                output_token_limit=True,
            ),
            conversation=None,
        )
    )
    assert decision.endpoint.id == "zhipu-glm-5.3-flash"
    assert decision.remote_fallback_position == 2
    assert "codex-pro-gpt-5.6-sol:capability" in (
        decision.candidate_rejections
    )


def test_v2_image_150k_plus_65536_routes_to_glm(
    tmp_path: Path,
) -> None:
    policy, _registry = policy_for(
        tmp_path,
        local_healthy=True,
    )
    decision = run(
        policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "long-context",
                None,
                1.0,
                "test",
                route_profile="multimodal",
            ),
            prompt_tokens=150000,
            output_reserve_tokens=65536,
            modalities={"text", "image"},
            image_count=1,
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.id == "zhipu-glm-5.3-flash"
    assert decision.context_required == 215536
    assert (
        "cloud-deepseek-v4-flash:"
        "deepseek_multimodal_unsupported"
    ) in (
        decision.candidate_rejections
    )


def test_v2_returns_422_when_full_context_has_no_candidate(
    tmp_path: Path,
) -> None:
    policy, _registry = policy_for(
        tmp_path,
        local_healthy=True,
    )
    with pytest.raises(NoCompatibleModelError) as raised:
        run(
            policy.choose(
                requested_model="auto",
                evaluation=Evaluation(
                    "long-context",
                    None,
                    1.0,
                    "test",
                ),
                prompt_tokens=1_100_000,
                output_reserve_tokens=65536,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )
    assert raised.value.status_code == 422
    assert raised.value.code == "no_compatible_model"


@pytest.mark.parametrize("unavailable_reason", ["health", "cooldown"])
def test_v2_returns_503_for_temporarily_unavailable_candidates(
    tmp_path: Path,
    unavailable_reason: str,
) -> None:
    registry = v2_registry(tmp_path)
    statuses = {
        endpoint.id: status_for(
            endpoint,
            healthy=unavailable_reason != "health",
        )
        for endpoint in registry.endpoints
    }
    cooldown_ids = (
        {endpoint.id for endpoint in registry.endpoints}
        if unavailable_reason == "cooldown"
        else set()
    )
    policy = RoutingPolicy(
        registry,
        v2_settings(tmp_path),
        FakeHealth(statuses, cooldown_ids=cooldown_ids),
    )
    with pytest.raises(NoEligibleModelError) as raised:
        run(
            policy.choose(
                requested_model="auto",
                evaluation=Evaluation(
                    "general",
                    None,
                    1.0,
                    "test",
                ),
                prompt_tokens=100,
                output_reserve_tokens=100,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )
    assert raised.value.status_code == 503
    assert raised.value.code == "no_eligible_model"


def test_v2_returns_503_for_mixed_incompatible_and_temporary_rejections(
    tmp_path: Path,
) -> None:
    registry = v2_registry(tmp_path)
    statuses = {
        endpoint.id: status_for(endpoint)
        for endpoint in registry.endpoints
    }
    policy = RoutingPolicy(
        registry,
        v2_settings(tmp_path),
        FakeHealth(
            statuses,
            cooldown_ids={registry.endpoints[0].id},
        ),
    )
    with pytest.raises(NoEligibleModelError) as raised:
        run(
            policy.choose(
                requested_model="auto",
                evaluation=Evaluation(
                    "long-context",
                    None,
                    1.0,
                    "test",
                ),
                prompt_tokens=1_100_000,
                output_reserve_tokens=65536,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )
    assert raised.value.status_code == 503


def test_deepseek_tool_history_is_rejected_before_upstream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = v2_registry(tmp_path)
    endpoint = registry.by_id("cloud-deepseek-v4-flash")
    assert endpoint is not None
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_LITELLM_MASTER_KEY",
        "internal-key",
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=v2_settings(tmp_path),
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": "{}",
                        },
                    }
                ],
                "codex_reasoning_items": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "private",
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "result",
            },
            {"role": "user", "content": "continue"},
        ]
    }
    assert deepseek_history_requires_migration(body, "chat")
    normalized = normalize_history_for_provider(body, "chat")
    assert "codex_reasoning_items" not in normalized["messages"][0]
    assert normalized["messages"][0]["tool_calls"][0]["id"] == "call_1"
    decision = RouteDecision(
        endpoint=endpoint,
        requested_model="auto",
        task="general",
        prompt_tokens=100,
        output_reserve_tokens=65536,
        reason="remote_profile_fallback",
        affinity="new",
        score=1,
        strategy_version="intelligent_v2",
        route_profile="general",
        context_required=65636,
    )
    with pytest.raises(HistoryMigrationRequiredError):
        run(
            _prepare_routed_body(
                runtime,
                body,
                api_kind="chat",
                decision=decision,
                request_id="history-preflight",
                allow_compaction=False,
            )
        )
    run(runtime.close())


def test_route_headers_preserve_requested_65536() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("edge-qwen38-flash")
    assert endpoint is not None
    decision = RouteDecision(
        endpoint=endpoint,
        requested_model="auto",
        task="general",
        prompt_tokens=150000,
        output_reserve_tokens=65536,
        reason="local_sufficient",
        affinity="new",
        score=1,
        strategy_version="intelligent_v2",
        route_profile="general",
        context_required=215536,
    )
    headers = decision.response_headers("request-1")
    assert decision.output_reserve_tokens == 65536
    assert headers["X-1Panel-Context-Required"] == "215536"
    assert headers["X-1Panel-Route-Strategy"] == "intelligent_v2"


def test_explicit_compaction_rechecks_candidates_without_lowering_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = v2_registry(tmp_path)
    settings_value = v2_settings(tmp_path)
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_LITELLM_MASTER_KEY",
        "internal-key",
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "route-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=settings_value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: status_for(endpoint)
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "inspect image"}],
        "max_tokens": 65536,
    }

    async def compact(
        _current,
        source,
        *,
        api_kind,
        request_id,
        target_context,
        identity,
    ):
        assert source["max_tokens"] == 65536
        compacted = {
            **source,
            "messages": [
                {
                    "role": "system",
                    "content": "neutral history capsule",
                },
                source["messages"][-1],
            ],
        }
        return object(), compacted, 1000

    monkeypatch.setattr(
        "ai_router.api._compact_body_for_target",
        compact,
    )
    routed, prompt_tokens, capsule = run(
        _maybe_compact_for_route(
            runtime,
            body,
            api_kind="chat",
            request_id="compaction-recheck",
            requested_model="auto",
            evaluation=Evaluation(
                "long-context",
                None,
                1.0,
                "test",
                route_profile="multimodal",
            ),
            prompt_tokens=250000,
            output_reserve_tokens=65536,
            modalities={"text", "image"},
            image_count=1,
            has_tools=False,
            required_capabilities=RequestCapabilities(
                protocol="chat"
            ),
            conversation=None,
            excluded_endpoints=set(),
            identity=IdentityProfile.from_settings(
                runtime.settings.section("identity")
            ),
        )
    )
    assert capsule is not None
    assert prompt_tokens == 1000
    assert routed["max_tokens"] == 65536
    decision = run(
        runtime.policy.choose(
            requested_model="auto",
            evaluation=Evaluation(
                "long-context",
                None,
                1.0,
                "test",
                route_profile="multimodal",
            ),
            prompt_tokens=prompt_tokens,
            output_reserve_tokens=65536,
            requested_context_tokens=315536,
            modalities={"text", "image"},
            image_count=1,
            has_tools=False,
            conversation=None,
        )
    )
    assert decision.endpoint.cloud is False
    assert decision.context_required == 315536
    run(runtime.close())


def test_glm_responses_adapter_uses_chat_upstream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = v2_registry(tmp_path)
    settings_value = v2_settings(tmp_path, identity_enabled=True)
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv(
        "AI_ROUTER_LITELLM_MASTER_KEY",
        "internal-key",
    )
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "responses-adapter-audit.jsonl"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(tmp_path / "responses-adapter-traces.sqlite3"),
    )
    runtime = build_runtime(
        settings=settings_value,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    runtime.health = FakeHealth(
        {
            endpoint.id: status_for(endpoint)
            for endpoint in registry.endpoints
        }
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    requests: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(
            {
                "path": request.url.path,
                "body": body,
            }
        )
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"id":"chatcmpl-stream","choices":[{"index":0,'
                    '"delta":{"role":"assistant","content":"STREAM_OK"},'
                    '"finish_reason":null}]}\n\n'
                    'data: {"id":"chatcmpl-stream","choices":[{"index":0,'
                    '"delta":{},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":5,"completion_tokens":2,'
                    '"total_tokens":7}}\n\n'
                    "data: [DONE]\n\n"
                ).encode(),
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "chatcmpl-adapter",
                "object": "chat.completion",
                "created": 1,
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "RESPONSES_OK",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            },
        )

    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_app(runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "zhipu/glm-5.3-flash",
                "input": "respond",
                "max_output_tokens": 65536,
            },
        )
        streamed = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer client-key"},
            json={
                "model": "zhipu/glm-5.3-flash",
                "input": "stream",
                "max_output_tokens": 64,
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["object"] == "response"
    assert response.json()["output_text"] == "RESPONSES_OK"
    assert response.headers["X-SIYUAN-Identity-Revision"]
    assert "X-1Panel-Protocol-Mode" not in response.headers
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["body"]["max_tokens"] == 65536
    assert "max_output_tokens" not in requests[0]["body"]
    assert streamed.status_code == 200
    assert "response.output_text.delta" in streamed.text
    assert "response.completed" in streamed.text
    assert streamed.text.endswith("\n\n")
    assert requests[1]["path"] == "/v1/chat/completions"
    run(runtime.internal_client.aclose())


def test_responses_adapter_parses_crlf_and_trailing_event() -> None:
    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"choices":[{"delta":{"content":"CRLF_OK"},'
                b'"finish_reason":null}]}\r'
            )
            yield b"\n\r"
            yield (
                b'\ndata: {"choices":[{"delta":{},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":5,'
                b'"completion_tokens":2,"total_tokens":7}}'
            )

    async def collect() -> bytes:
        upstream = httpx.Response(200, stream=ChunkedStream())
        return b"".join(
            [
                item
                async for item in chat_stream_to_responses(
                    upstream,
                    model="test/model",
                )
            ]
        )

    text = run(collect()).decode()
    assert '"delta":"CRLF_OK"' in text
    assert '"input_tokens":5' in text
    assert '"output_tokens":2' in text
    assert "response.completed" in text


def test_responses_adapter_maps_incomplete_finish_reasons() -> None:
    async def collect() -> bytes:
        upstream = httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"partial"},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{},'
                '"finish_reason":"length"}],'
                '"usage":{"prompt_tokens":4,"completion_tokens":2,'
                '"total_tokens":6}}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )
        return b"".join(
            [
                item
                async for item in chat_stream_to_responses(
                    upstream,
                    model="test/model",
                )
            ]
        )

    streamed = run(collect()).decode()
    terminal = next(
        json.loads(line[6:])
        for line in streamed.splitlines()
        if line.startswith("data: {")
        and '"type":"response.incomplete"' in line
    )
    assert terminal["response"]["status"] == "incomplete"
    assert terminal["response"]["incomplete_details"] == {
        "reason": "max_output_tokens"
    }
    assert terminal["response"]["output"][0]["status"] == "incomplete"
    assert terminal["response"]["output_text"] == "partial"
    assert terminal["response"]["usage"]["output_tokens"] == 2

    filtered = json.loads(
        chat_response_to_responses(
            json.dumps(
                {
                    "id": "chatcmpl-filtered",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "filtered partial",
                            },
                            "finish_reason": "content_filter",
                        }
                    ],
                }
            ).encode(),
            model="test/model",
        )
    )
    assert filtered["status"] == "incomplete"
    assert filtered["incomplete_details"] == {
        "reason": "content_filter"
    }
    assert filtered["output"][0]["status"] == "incomplete"
