from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.api import (
    _ensure_prompt_directive_access,
    create_app as create_router_app,
)
from ai_router.auth import AuthenticatedClient
from ai_router.config import Registry, Settings
from ai_router.control import create_app as create_control_app
from ai_router.errors import (
    AuthenticationError,
    RouteDirectiveIncompatibleError,
    RouteDirectiveUnavailableError,
    RouterError,
)
from ai_router.policy import RoutingPolicy
from ai_router.prompt_directives import (
    PromptDirective,
    PromptDirectiveStore,
    prepare_prompt_directive_update,
    resolve_conversation_directive,
    sanitize_prompt_directives,
)
from ai_router.types import (
    ClientPolicy,
    ConversationState,
    EndpointStatus,
    Evaluation,
    RequestCapabilities,
)
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from ai_router.token_counter import SimpleTokenCounter


ROOT = Path(__file__).resolve().parents[1]


def _directive_settings() -> dict:
    settings = Settings(
        defaults_path=ROOT / "config/defaults.yaml",
        runtime_path=Path("/tmp/ai-router-test-missing-settings.yaml"),
    )
    value = settings.section("routing")["prompt_directives"]
    value["enabled"] = True
    value["retired_phrases"] = ["按旧约定处理"]
    return value


def test_latest_user_contiguous_match_strips_whole_line() -> None:
    body = {
        "model": "auto",
        "messages": [
            {
                "role": "user",
                "content": "历史任务\n按旧约定处理",
            },
            {"role": "assistant", "content": "收到"},
            {
                "role": "user",
                "content": "检查这个仓库\n麻烦按日轮协议处理一下。",
            },
        ],
    }

    result = sanitize_prompt_directives(
        body,
        "chat",
        _directive_settings(),
    )

    assert result.directive == PromptDirective(
        id="rilun",
        generation=1,
        endpoint_id="codex-pro-gpt-5.6-sol",
    )
    assert result.body["messages"][0]["content"] == "历史任务"
    assert result.body["messages"][-1]["content"] == "检查这个仓库"
    assert "日轮" not in str(result.body)
    assert "旧约定" not in str(result.body)


def test_old_message_cannot_activate_and_fuzzy_keywords_do_not_match() -> None:
    settings = _directive_settings()
    old_only = sanitize_prompt_directives(
        {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "按北辰协议处理\n旧任务"},
                {"role": "assistant", "content": "收到"},
                {"role": "user", "content": "继续"},
            ],
        },
        "chat",
        settings,
    )
    fuzzy = sanitize_prompt_directives(
        {
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "新任务\n按 北辰 协议 处理",
                }
            ],
        },
        "chat",
        settings,
    )

    assert old_only.directive is None
    assert "北辰" not in str(old_only.body)
    assert fuzzy.directive is None
    assert fuzzy.body["messages"][0]["content"].endswith(
        "按 北辰 协议 处理"
    )


def test_disabled_directives_are_scrubbed_without_activation() -> None:
    settings = _directive_settings()
    settings["enabled"] = False

    result = sanitize_prompt_directives(
        {
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "检查这个仓库\n按日轮协议处理",
                }
            ],
        },
        "chat",
        settings,
    )

    assert result.directive is None
    assert result.body["messages"][0]["content"] == "检查这个仓库"
    assert "日轮" not in str(result.body)


def test_auto_directive_is_global_but_explicit_model_scope_is_preserved(
) -> None:
    registry = Registry(ROOT / "config/registry.yaml")
    runtime = SimpleNamespace(registry=registry)
    directive = PromptDirective(
        id="beichen",
        generation=1,
        endpoint_id="codex-pro-gpt-6-astra",
    )

    def authenticated(
        models: tuple[str, ...],
        *,
        disclosure_mode: str = "internal",
    ) -> AuthenticatedClient:
        return AuthenticatedClient(
            policy=ClientPolicy(
                id="test",
                key_env="TEST_KEY",
                models=models,
                rpm_limit=10,
                tpm_limit=10000,
                max_parallel_requests=1,
                disclosure_mode=disclosure_mode,
            ),
            key_id="test-key",
        )

    _ensure_prompt_directive_access(
        runtime,
        authenticated(("auto",)),
        directive,
        requested_model="auto",
    )
    _ensure_prompt_directive_access(
        runtime,
        authenticated(
            ("siyuan/auto",),
            disclosure_mode="public",
        ),
        directive,
        requested_model="auto",
    )

    with pytest.raises(AuthenticationError):
        _ensure_prompt_directive_access(
            runtime,
            authenticated(("zhipu/glm-5.3-flash",)),
            directive,
            requested_model="zhipu/glm-5.3-flash",
        )

    _ensure_prompt_directive_access(
        runtime,
        authenticated(("codex-pro/gpt-6-astra",)),
        directive,
        requested_model="codex-pro/gpt-6-astra",
    )
    _ensure_prompt_directive_access(
        runtime,
        authenticated(("*",)),
        directive,
        requested_model="codex-pro/gpt-6-astra",
    )


def test_directive_requires_task_on_another_line() -> None:
    with pytest.raises(RouterError) as error:
        sanitize_prompt_directives(
            {
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": "请按青岚协议处理这个任务",
                    }
                ],
            },
            "chat",
            _directive_settings(),
        )

    assert error.value.code == "invalid_route_directive"


def test_responses_reset_and_multiple_directive_validation() -> None:
    reset = sanitize_prompt_directives(
        {
            "model": "auto",
            "input": "继续普通处理\n恢复常规模式",
        },
        "responses",
        _directive_settings(),
    )
    assert reset.directive is not None and reset.directive.reset
    assert reset.body["input"] == "继续普通处理"

    with pytest.raises(RouterError) as error:
        sanitize_prompt_directives(
            {
                "model": "auto",
                "input": [
                    {
                        "role": "user",
                        "content": (
                            "检查任务\n按日轮协议处理\n按北辰协议处理"
                        ),
                    }
                ],
            },
            "responses",
            _directive_settings(),
        )
    assert error.value.code == "invalid_route_directive"


def test_rotation_increments_generation_and_retires_old_phrase() -> None:
    current = _directive_settings()
    proposed = copy.deepcopy(current)
    proposed["routes"]["beichen"]["phrase"] = "按新北辰章法处理"

    updated, changes = prepare_prompt_directive_update(current, proposed)

    assert updated["revision"] == current["revision"] + 1
    assert (
        updated["routes"]["beichen"]["generation"]
        == current["routes"]["beichen"]["generation"] + 1
    )
    assert (
        updated["routes"]["rilun"]["generation"]
        == current["routes"]["rilun"]["generation"]
    )
    assert "按北辰协议处理" in updated["retired_phrases"]
    assert changes == [{"directive_id": "beichen", "fields": "phrase"}]


def test_rotated_conversation_pin_is_invalidated() -> None:
    settings = _directive_settings()
    conversation = ConversationState(
        conversation_id="conversation-1",
        public_model="codex-pro/gpt-5.6-sol",
        endpoint_id="codex-pro-gpt-5.6-sol",
        tier_rank=50,
        task="code",
        last_seen=time.time(),
        directive_id="rilun",
        directive_generation=0,
        directive_endpoint_id="codex-pro-gpt-5.6-sol",
    )

    directive, clear_affinity = resolve_conversation_directive(
        None,
        conversation,
        settings,
    )

    assert directive is None
    assert clear_affinity is True


def test_sqlite_pool_suggestions_are_unique_and_never_reused(
    tmp_path: Path,
) -> None:
    store = PromptDirectiveStore(tmp_path / "directives.sqlite3")
    before = store.stats()
    first = store.suggest(["rilun", "beichen"])
    second = store.suggest(["rilun", "beichen"])
    after = store.stats()

    assert before["total"] >= 4096
    assert (tmp_path / "directives.sqlite3").stat().st_mode & 0o777 == 0o600
    assert len(set(first.values())) == 2
    assert set(first.values()).isdisjoint(second.values())
    assert after["available"] == before["available"] - 4


def test_control_preview_then_save_rotates_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    runtime = build_runtime(
        settings=Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "settings.yaml",
        ),
        registry=Registry(ROOT / "config/registry.yaml"),
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    headers = {"Authorization": "Bearer admin-key"}

    with TestClient(create_control_app(runtime)) as client:
        original = client.get("/api/settings", headers=headers).json()[
            "settings"
        ]
        original_prompt = original["routing"]["prompt_directives"]
        preview = client.post(
            "/api/prompt-directives/suggest",
            headers=headers,
            json={"directive_ids": ["beichen"]},
        )
        unchanged = client.get("/api/settings", headers=headers).json()[
            "settings"
        ]
        assert preview.status_code == 200
        suggestion = preview.json()["suggestions"]["beichen"]
        assert (
            unchanged["routing"]["prompt_directives"]
            == original_prompt
        )

        draft = copy.deepcopy(original)
        draft["routing"]["prompt_directives"]["enabled"] = True
        draft["routing"]["prompt_directives"]["routes"]["beichen"][
            "phrase"
        ] = suggestion
        saved = client.put(
            "/api/settings",
            headers=headers,
            json=draft,
        )
        stale = client.put(
            "/api/settings",
            headers=headers,
            json=draft,
        )
        invalid_revision = copy.deepcopy(original)
        invalid_revision["routing"]["prompt_directives"][
            "revision"
        ] = "invalid"
        invalid = client.put(
            "/api/settings",
            headers=headers,
            json=invalid_revision,
        )

    assert saved.status_code == 200
    prompt = saved.json()["settings"]["routing"]["prompt_directives"]
    assert prompt["revision"] == original_prompt["revision"] + 1
    assert (
        prompt["routes"]["beichen"]["generation"]
        == original_prompt["routes"]["beichen"]["generation"] + 1
    )
    assert prompt["routes"]["rilun"]["generation"] == (
        original_prompt["routes"]["rilun"]["generation"] + 1
    )
    assert "按北辰协议处理" in prompt["retired_phrases"]
    assert stale.status_code == 409
    assert (
        stale.json()["error"]["code"]
        == "prompt_directive_revision_conflict"
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_route_directive"


class _Health:
    def __init__(self, status: EndpointStatus) -> None:
        self._status = status

    async def statuses(self, endpoints):
        return {
            endpoint.id: replace(
                self._status,
                endpoint_id=endpoint.id,
            )
            for endpoint in endpoints
        }

    async def in_cooldown(self, _endpoint_id: str) -> bool:
        return False

    async def status(self, endpoint, *, force_refresh: bool = False):
        return replace(self._status, endpoint_id=endpoint.id)

    async def in_capability_cooldown(
        self,
        _deployment_id: str,
        _capability: str,
    ) -> bool:
        return False

    async def mark_capability_failure(
        self,
        _deployment_id: str,
        _capability: str,
        _cooldown_seconds: int,
    ) -> None:
        return None

    async def mark_failure(
        self,
        _endpoint_id: str,
        _cooldown_seconds: int,
    ) -> None:
        return None

    async def prefix_cache_counters(self, _endpoint):
        return None


def _policy(
    tmp_path: Path,
    *,
    healthy: bool = True,
    modalities: tuple[str, ...] = ("text",),
) -> tuple[RoutingPolicy, str]:
    settings = Settings(
        defaults_path=ROOT / "config/defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    registry = Registry(ROOT / "config/registry.yaml")
    base = registry.by_id("cloud-deepseek-v4-pro")
    assert base is not None
    endpoint = replace(
        base,
        cloud=False,
        backend_type="openai",
        modalities=modalities,
        auto_candidate=False,
    )
    registry = registry.with_endpoints([endpoint])
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=healthy,
        checked_at=time.time(),
        eligible_context_tokens=endpoint.safe_context_tokens,
    )
    return RoutingPolicy(registry, settings, _Health(status)), endpoint.id


def _choose(
    policy: RoutingPolicy,
    endpoint_id: str,
    *,
    modalities: set[str],
    requested_model: str = "auto",
):
    return asyncio.run(
        policy.choose(
            requested_model=requested_model,
            evaluation=Evaluation(
                "code",
                None,
                1.0,
                "test",
                directive_id="qinglan",
                directive_generation=1,
                required_endpoint_id=endpoint_id,
            ),
            prompt_tokens=100,
            output_reserve_tokens=100,
            modalities=modalities,
            has_tools=False,
            required_capabilities=RequestCapabilities(protocol="chat"),
            conversation=None,
        )
    )


def test_directed_policy_ignores_auto_candidate_but_never_falls_back(
    tmp_path: Path,
) -> None:
    policy, endpoint_id = _policy(tmp_path)
    decision = _choose(policy, endpoint_id, modalities={"text"})

    assert decision.endpoint.id == endpoint_id
    assert decision.reason == "route_directive"
    assert decision.directive_id == "qinglan"

    explicit = _choose(
        policy,
        endpoint_id,
        modalities={"text"},
        requested_model="deepseek/deepseek-v4-pro",
    )
    assert explicit.endpoint.id == endpoint_id
    assert explicit.reason == "route_directive"

    unavailable, endpoint_id = _policy(tmp_path, healthy=False)
    with pytest.raises(RouteDirectiveUnavailableError):
        _choose(unavailable, endpoint_id, modalities={"text"})

    incompatible, endpoint_id = _policy(
        tmp_path,
        modalities=("text",),
    )
    with pytest.raises(RouteDirectiveIncompatibleError):
        _choose(incompatible, endpoint_id, modalities={"image"})


@pytest.mark.parametrize(
    ("directive_id", "phrase", "endpoint_id"),
    [
        (
            "rilun",
            "按日轮协议处理",
            "codex-pro-gpt-5.6-sol",
        ),
        (
            "beichen",
            "按北辰协议处理",
            "codex-pro-gpt-6-astra",
        ),
        (
            "qinglan",
            "按青岚协议处理",
            "cloud-deepseek-v4-pro",
        ),
        (
            "yuheng",
            "按玉衡协议处理",
            "zhipu-glm-5.3-flash",
        ),
    ],
)
def test_public_auto_directive_routes_without_target_model_acl(
    tmp_path: Path,
    monkeypatch,
    directive_id: str,
    phrase: str,
    endpoint_id: str,
) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "gateway-key")
    monkeypatch.setenv(
        "AI_ROUTER_STATE_KEY",
        Fernet.generate_key().decode(),
    )
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / f"{directive_id}-audit.jsonl"),
    )
    router_settings = Settings(
        defaults_path=ROOT / "config/defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    router_settings.write_runtime(
        {
            "identity": {"enabled": True},
            "routing": {
                "prompt_directives": {
                    "enabled": True,
                }
            },
        }
    )
    source_registry = Registry(ROOT / "config/registry.yaml")
    source_endpoint = source_registry.by_id(endpoint_id)
    assert source_endpoint is not None
    endpoint = replace(
        source_endpoint,
        api_base="http://upstream/v1",
        health_url="http://upstream/health",
        backend_type="openai",
        backend_api_key_env="AI_ROUTER_LITELLM_MASTER_KEY",
        enabled=True,
        auto_candidate=False,
        cloud=False,
        metadata={"provider": "test"},
    )
    registry = source_registry.with_endpoints([endpoint])
    runtime = build_runtime(
        settings=router_settings,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    asyncio.run(runtime.health.client.aclose())
    runtime.health = _Health(
        EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=time.time(),
            load_headroom=1,
            latency_score=1,
            eligible_context_tokens=endpoint.safe_context_tokens,
        )
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    asyncio.run(
        runtime.clients.create_account(
            {
                "id": "workbuddy-public",
                "name": "WorkBuddy Public",
                "enabled": True,
                "models": ["siyuan/auto"],
                "rpm_limit": 120,
                "tpm_limit": 1000000,
                "max_parallel_requests": 8,
                "disclosure_mode": "public",
            },
            allowed_models={
                "auto",
                "siyuan/auto",
                endpoint.public_model,
            },
            public_model_id="siyuan/auto",
        )
    )
    _public_key, public_secret = asyncio.run(
        runtime.clients.create_key("workbuddy-public", "test")
    )
    asyncio.run(
        runtime.clients.create_account(
            {
                "id": "explicit-only",
                "name": "Explicit Only",
                "enabled": True,
                "models": [endpoint.public_model],
                "rpm_limit": 10,
                "tpm_limit": 10000,
                "max_parallel_requests": 1,
                "disclosure_mode": "internal",
            },
            allowed_models={
                "auto",
                "siyuan/auto",
                endpoint.public_model,
            },
            public_model_id="siyuan/auto",
        )
    )
    _explicit_key, explicit_secret = asyncio.run(
        runtime.clients.create_key("explicit-only", "test")
    )
    captured: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": f"chatcmpl-{directive_id}",
                "object": "chat.completion",
                "model": endpoint.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                },
            },
        )

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_router_app(runtime)
    request_body = {
        "model": "siyuan/auto",
        "messages": [
            {
                "role": "user",
                "content": f"检查这个仓库\n{phrase}",
            }
        ],
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {public_secret}",
                "X-1Panel-Conversation-ID": (
                    f"public-directive-{directive_id}"
                ),
            },
            json=request_body,
        )
        unauthorized = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {explicit_secret}",
            },
            json={
                **request_body,
                "model": "auto",
            },
        )

    assert response.status_code == 200
    assert response.json()["model"] == "siyuan/auto"
    assert response.headers["x-1panel-public-model"] == "siyuan/auto"
    assert "x-1panel-route-model" not in response.headers
    assert "x-1panel-route-deployment" not in response.headers
    assert endpoint.id not in response.text
    assert endpoint.public_model not in response.text
    assert unauthorized.status_code == 401
    assert len(captured) == 1
    assert captured[0]["model"] == endpoint.provider_model
    user_messages = [
        item
        for item in captured[0]["messages"]
        if item.get("role") == "user"
    ]
    assert user_messages[-1]["content"] == "检查这个仓库"
    assert phrase not in str(captured)
    trace = asyncio.run(
        runtime.route_traces.get(response.headers["x-request-id"])
    )
    assert trace is not None
    assert trace["client_id"] == "workbuddy-public"
    assert trace["requested_model"] == "siyuan/auto"
    assert trace["disclosure_mode"] == "public"
    assert trace["evaluation"]["directive_id"] == directive_id
    assert trace["endpoint_id"] == endpoint.id
    assert phrase not in str(trace)
    accounts = asyncio.run(runtime.clients.list_accounts())
    workbuddy = next(
        item for item in accounts if item["id"] == "workbuddy-public"
    )
    assert workbuddy["disclosure_mode"] == "public"
    assert workbuddy["models"] == ["siyuan/auto"]
    asyncio.run(runtime.close())


def test_router_strips_directive_before_upstream_trace_and_training(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "client-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "internal-key")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv(
        "AI_ROUTER_AUDIT_PATH",
        str(tmp_path / "audit.jsonl"),
    )
    training_key = tmp_path / "training.key"
    training_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "true")
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_DB_PATH",
        str(tmp_path / "training.sqlite3"),
    )
    monkeypatch.setenv(
        "AI_ROUTER_TRAINING_KEY_PATH",
        str(training_key),
    )
    router_settings = Settings(
        defaults_path=ROOT / "config/defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    router_settings.write_runtime(
        {
            "cloud": {
                "enabled": True,
                "auto_escalate": False,
                "monthly_budget": 10,
                "allowed_providers": ["deepseek"],
                "allowed_models": ["deepseek/deepseek-v4-pro"],
            },
            "routing": {
                "prompt_directives": {
                    "enabled": True,
                }
            },
        }
    )
    registry = Registry(ROOT / "config/registry.yaml")
    endpoint = registry.by_id("cloud-deepseek-v4-pro")
    assert endpoint is not None
    runtime = build_runtime(
        settings=router_settings,
        registry=registry,
        store=InMemoryStateStore(),
        token_counter=SimpleTokenCounter(),
    )
    asyncio.run(runtime.health.client.aclose())
    runtime.health = _Health(
        EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=time.time(),
            load_headroom=1,
            latency_score=1,
            eligible_context_tokens=endpoint.safe_context_tokens,
        )
    )
    runtime.policy = RoutingPolicy(
        registry,
        runtime.settings,
        runtime.health,
    )
    captured: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": f"chatcmpl-{len(captured)}",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    asyncio.run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    app = create_router_app(runtime)
    headers = {
        "Authorization": "Bearer client-key",
        "X-1Panel-Conversation-ID": "directive-conversation",
    }
    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "检查这个仓库\n"
                            "麻烦按青岚协议处理一下。"
                        ),
                    }
                ],
                "max_tokens": 16,
            },
        )
        second = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "继续检查"}
                ],
                "max_tokens": 16,
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["x-1panel-route-deployment"] == endpoint.id
    assert second.headers["x-1panel-route-deployment"] == endpoint.id
    assert [item["model"] for item in captured] == [
        endpoint.id,
        endpoint.id,
    ]
    assert captured[0]["messages"][0]["content"] == "检查这个仓库"
    assert "青岚" not in str(captured)
    trace = asyncio.run(
        runtime.route_traces.get(
            first.headers["x-1panel-route-request-id"]
        )
    )
    assert trace is not None
    assert trace["evaluation"]["directive_id"] == "qinglan"
    assert "青岚" not in str(trace)
    assert runtime.training is not None
    export_path = tmp_path / "training-export.jsonl"
    assert asyncio.run(
        runtime.training.export_jsonl(str(export_path))
    ) == 2
    assert "青岚" not in export_path.read_text(encoding="utf-8")
    asyncio.run(runtime.close())
