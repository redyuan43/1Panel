from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

from ai_router.api import (
    _compact_body_for_target,
    _compaction_allowed,
    _error_response,
    _maybe_compact_for_route,
    _public_error_message,
    _target_allows_compaction,
)
from ai_router.codex_adapter import _catalog_context_limits
from ai_router.compaction import CapsuleCipher, ContextCompactor
from ai_router.config import Registry, Settings, validate_settings
from ai_router.context_policy import apply_context_policy, request_strategy, validate_target
from ai_router.errors import (
    CompactionUnavailableError,
    ContextTooLargeForSelectedModelError,
    RouteDirectiveIncompatibleError,
    RouteDirectiveUnavailableError,
)
from ai_router.identity import IdentityProfile
from ai_router.health import HealthMonitor
from ai_router.policy import RoutingPolicy
from ai_router.policy_config import PolicyConfigManager
from ai_router.routing_modes import resolve as resolve_objectives
from ai_router.store import InMemoryStateStore
from ai_router.types import EndpointStatus, Evaluation, ModelCallTarget, RequestCapabilities

ROOT = Path(__file__).resolve().parents[1]
TARGET = "codex-pro-gpt-6-astra"


@pytest.fixture
def setup(tmp_path):
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "settings.yaml")
    settings._value["cloud"].update(enabled=True, allowed_models=["codex-pro/gpt-6-astra"],
                                  allowed_providers=["openai-codex"])
    settings.write_runtime({"compaction": {"enabled": True}, "cloud": settings.section("cloud")})
    registry = Registry(ROOT / "config/registry.yaml")
    worker = {"worker_id": "account", "account_alias": "test", "api_base": "http://offline.invalid/v1",
              "safe_context_tokens": 272000, "ready": True, "state": "available",
              "models": ["gpt-6-astra"], "model_context_limits": {
                  "gpt-6-astra": {"default_context_tokens": 272000, "max_context_tokens": 872000}}}
    status = EndpointStatus(TARGET, True, time.time(), eligible_context_tokens=272000,
                            detail={"workers": [worker], "available_worker_ids": ["account"]})
    health = SimpleNamespace(statuses=AsyncMock(return_value={TARGET: status}),
                             in_cooldown=AsyncMock(return_value=False))
    policy = RoutingPolicy(registry, settings, health, InMemoryStateStore())
    return SimpleNamespace(settings=settings, registry=registry, policy=policy, status=status)


def choose(runtime, tokens=322718, modalities=None):
    return runtime.policy.choose(requested_model="auto", evaluation=Evaluation(
        "long-context", None, 1.0, "test", required_endpoint_id=TARGET),
        prompt_tokens=tokens, output_reserve_tokens=16384, modalities=modalities or {"text"},
        has_tools=False, required_capabilities=RequestCapabilities(protocol="chat"), conversation=None)


def test_extended_window_routes_without_mutating_registry_or_health(setup):
    setup.settings._value["context_policy"]["mode"] = "extended"
    decision = asyncio.run(choose(setup))
    assert decision.endpoint.id == TARGET
    assert decision.deployment_safe_context_tokens == 500000
    assert setup.registry.by_id(TARGET).safe_context_tokens == 272000
    assert setup.status.detail["workers"][0]["safe_context_tokens"] == 272000
    setup.settings._value["context_policy"]["mode"] = "compact"
    with pytest.raises(ContextTooLargeForSelectedModelError):
        asyncio.run(choose(setup))


@pytest.mark.parametrize("limit,maximum,expected", [(1050000,872000,872000), (400000,872000,400000),
                                                  (500000,None,272000), (500000,True,272000)])
def test_extended_window_clamps_to_per_account_catalog(setup, limit, maximum, expected):
    setup.settings._value["context_policy"].update(mode="extended", extended_context_tokens=limit)
    setup.status.detail["workers"][0]["model_context_limits"]["gpt-6-astra"]["max_context_tokens"] = maximum
    endpoint, status = apply_context_policy(setup.settings, setup.registry.by_id(TARGET), setup.status)
    assert endpoint.safe_context_tokens == expected
    assert status.eligible_context_tokens == expected


def test_registered_ceiling_and_other_model_scope_are_preserved(setup):
    setup.settings._value["context_policy"].update(mode="extended", extended_context_tokens=1050000)
    endpoint = replace(setup.registry.by_id(TARGET), configured_context_tokens=400000)
    assert apply_context_policy(setup.settings, endpoint, setup.status)[0].safe_context_tokens == 400000
    other = setup.registry.by_id("codex-pro-gpt-5.6-sol")
    assert apply_context_policy(setup.settings, other, setup.status)[0] is other
    for endpoint_id in ("missing", "ai-qwen38-27b"):
        with pytest.raises(ValueError):
            validate_target({"mode":"extended", "endpoint_id":endpoint_id}, setup.registry)


@pytest.mark.parametrize("failure", ["health", "modality", "catalog", "capacity"])
def test_extended_does_not_bypass_other_constraints(setup, failure):
    setup.settings._value["context_policy"]["mode"] = "extended"
    if failure == "health":
        setup.status.healthy = False
    if failure == "catalog":
        setup.status.detail["workers"][0].pop("model_context_limits")
    if failure == "capacity":
        setup.status.detail["available_worker_ids"] = []
    with pytest.raises((
        ContextTooLargeForSelectedModelError,
        RouteDirectiveIncompatibleError,
        RouteDirectiveUnavailableError,
    )):
        asyncio.run(choose(setup, modalities={"audio"} if failure == "modality" else None))


def test_compaction_mode_scoped_permission_and_expanded_mode_no_compaction(setup):
    evaluation = Evaluation("general", None, 1, "test", required_endpoint_id=TARGET)
    setup.settings._value["context_policy"]["mode"] = "compact"
    mode = request_strategy(setup.settings, setup.registry, "auto", evaluation)
    assert _compaction_allowed(setup, False, "", context_strategy=mode)
    assert not _compaction_allowed(setup, False, "")
    setup.settings._value["compaction"]["mode"] = "automatic"
    assert not _compaction_allowed(setup, True, "true", context_strategy="extended")
    evaluation.required_endpoint_id = "ai-qwen38-27b"
    assert request_strategy(setup.settings, setup.registry, "auto", evaluation) == "legacy"


def test_selected_target_disables_automatic_compaction():
    automatic = Evaluation("general", None, 1, "test")
    directed = Evaluation(
        "general",
        None,
        1,
        "test",
        required_endpoint_id=TARGET,
    )

    assert _target_allows_compaction("auto", automatic)
    assert not _target_allows_compaction("auto", directed)
    assert not _target_allows_compaction("codex-pro/gpt-6-astra", automatic)
    assert not _target_allows_compaction("auto", automatic,
                                        conversation_control={"pin": {"endpoint_id": TARGET}})
    assert _target_allows_compaction("auto", automatic, conversation_control={"pin": None})


def test_pinned_target_rejects_full_context_before_compaction(setup):
    setup.settings._value["context_policy"]["mode"] = "compact"
    pinned = "ai-qwen38-27b"
    setup.policy.health.statuses = AsyncMock(return_value={
        endpoint.id: EndpointStatus(endpoint.id, endpoint.id == pinned, time.time(),
                                    eligible_context_tokens=endpoint.safe_context_tokens)
        for endpoint in setup.registry.responders()
    })
    evaluation = Evaluation("long-context", None, 1, "test")
    control = {"pin": {"endpoint_id": pinned}}
    assert not _target_allows_compaction("auto", evaluation, conversation_control=control)
    from ai_router.errors import RouterError
    with pytest.raises(RouterError) as error:
        asyncio.run(setup.policy.choose(requested_model="auto", evaluation=evaluation,
            prompt_tokens=322718, output_reserve_tokens=16384, modalities={"text"},
            has_tools=False, required_capabilities=RequestCapabilities(protocol="chat"),
            conversation=None, conversation_control=control))
    assert error.value.code == "conversation_pin_incompatible"
    assert error.value.details["rejection_reason"] == "context"


def test_compaction_acquires_bounded_capacity_and_releases_on_error(setup, monkeypatch):
    lease = SimpleNamespace(release=AsyncMock())
    setup.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
    setup.compactor = SimpleNamespace(model_id="ai-qwen38-27b", summary_output_tokens=8192,
        summary_request_tokens=lambda messages: 900,
        compact=AsyncMock(side_effect=CompactionUnavailableError("mock summary failed")))
    acquire = AsyncMock(return_value=ModelCallTarget(
        "http://offline.invalid", "summary", safe_context_tokens=16384))
    monkeypatch.setattr("ai_router.api._acquire_internal_model", acquire)
    with pytest.raises(CompactionUnavailableError):
        asyncio.run(_compact_body_for_target(setup, {"messages":[]}, api_kind="chat", request_id="test",
            target_context=272000, identity=IdentityProfile.from_settings(setup.settings.section("identity"))))
    assert acquire.call_args.kwargs["prompt_tokens"] == 900
    assert acquire.call_args.kwargs["output_reserve_tokens"] == 8192
    assert setup.compactor.compact.call_args.kwargs["summary_input_tokens"] == 8192
    assert lease.release.await_count == 1


def test_compaction_caps_admission_and_chunk_budget_to_selected_worker(setup, monkeypatch):
    lease = SimpleNamespace(release=AsyncMock())
    setup.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
    setup.compactor = SimpleNamespace(
        model_id="ai-qwen38-27b",
        summary_output_tokens=8192,
        summary_request_tokens=lambda messages: 64000,
        compact=AsyncMock(side_effect=CompactionUnavailableError("stop after inspection")),
    )
    acquire = AsyncMock(return_value=ModelCallTarget(
        "http://offline.invalid", "summary", safe_context_tokens=16384))
    monkeypatch.setattr("ai_router.api._acquire_internal_model", acquire)

    with pytest.raises(CompactionUnavailableError):
        asyncio.run(_compact_body_for_target(
            setup,
            {"messages": [{"role": "user", "content": "large history"}]},
            api_kind="chat",
            request_id="test",
            target_context=272000,
            identity=IdentityProfile.from_settings(setup.settings.section("identity")),
        ))

    assert acquire.call_args.kwargs["prompt_tokens"] == 4096
    assert setup.compactor.compact.call_args.kwargs["summary_input_tokens"] == 8192
    assert lease.release.await_count == 1


@pytest.mark.parametrize("api_kind,field", [("chat", "messages"), ("responses", "input")])
def test_local_only_request_never_sends_history_to_cloud_compactor(
    setup, monkeypatch, api_kind, field
):
    cloud = replace(
        setup.registry.by_id("zhipu-glm-5.3-flash"),
        id="cloud-summary",
        enabled=True,
        cloud=True,
    )
    setup.registry = setup.registry.with_endpoints([*setup.registry.endpoints, cloud])
    setup.compactor = SimpleNamespace(
        model_id=cloud.id,
        compact=AsyncMock(),
    )
    setup.scheduler = SimpleNamespace(begin_request=AsyncMock())
    acquire = AsyncMock()
    monkeypatch.setattr("ai_router.api._acquire_internal_model", acquire)

    with pytest.raises(CompactionUnavailableError, match="local-only"):
        asyncio.run(_compact_body_for_target(
            setup,
            {field: [{"role": "user", "content": "private history"}]},
            api_kind=api_kind,
            request_id="local-only",
            target_context=32000,
            identity=IdentityProfile.from_settings(setup.settings.section("identity")),
            routing_options={"local_only": True},
        ))

    setup.scheduler.begin_request.assert_not_awaited()
    setup.compactor.compact.assert_not_awaited()
    acquire.assert_not_awaited()


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_directed_overflow_is_rejected_without_compaction(setup, monkeypatch, api_kind):
    setup.settings._value["context_policy"]["mode"] = "compact"
    body = {"model": "auto", "messages" if api_kind == "chat" else "input": [
        {"role": "user", "content": "old " * 100}, {"role": "user", "content": "latest task"}]}
    async def compact(current, source, **kwargs):
        assert kwargs["target_context"] == 272000
        assert kwargs["routing_options"] is routing_options
        return object(), {**source, "compacted": True}, 1000
    mock = AsyncMock(side_effect=compact)
    monkeypatch.setattr("ai_router.api._compact_body_for_target", mock)
    choose = AsyncMock(wraps=setup.policy.choose)
    monkeypatch.setattr(setup.policy, "choose", choose)
    routing_options = resolve_objectives(setup.settings.section("routing"))
    args = dict(api_kind=api_kind, request_id="test", requested_model="auto",
                evaluation=Evaluation("long-context", None, 1, "test", required_endpoint_id=TARGET),
                prompt_tokens=322718, output_reserve_tokens=16384, modalities={"text"}, image_count=0,
                has_tools=False, required_capabilities=RequestCapabilities(protocol=api_kind),
                conversation=None, excluded_endpoints=set(),
                identity=IdentityProfile.from_settings(setup.settings.section("identity")),
                routing_options=routing_options)
    with pytest.raises(ContextTooLargeForSelectedModelError) as raised:
        asyncio.run(_maybe_compact_for_route(setup, body, **args))
    assert raised.value.status_code == 422
    assert raised.value.code == "context_too_large_for_selected_model"
    assert raised.value.details == {
        "requested_model": "auto",
        "endpoint_id": TARGET,
        "required_context_tokens": 339102,
        "model_context_tokens": 272000,
    }
    assert "larger-context model" in _public_error_message(raised.value)
    response = _error_response(raised.value, public=True)
    payload = json.loads(response.body)
    assert response.status_code == 422
    assert payload["error"]["code"] == "context_too_large_for_selected_model"
    assert "larger-context model" in payload["error"]["message"]
    assert mock.await_count == 0
    assert choose.await_count == 1
    assert all(call.kwargs["routing_options"] is routing_options for call in choose.await_args_list)
    setup.compute_executor.close()
    del setup.compute_executor
    args["modalities"] = {"audio"}
    with pytest.raises(RouteDirectiveIncompatibleError):
        asyncio.run(_maybe_compact_for_route(setup, body, **args))
    assert mock.await_count == 0


def test_explicit_model_overflow_is_rejected_without_compaction(setup, monkeypatch):
    body = {
        "model": "codex-pro/gpt-6-astra",
        "messages": [{"role": "user", "content": "large history"}],
    }
    compact = AsyncMock()
    monkeypatch.setattr("ai_router.api._compact_body_for_target", compact)

    with pytest.raises(ContextTooLargeForSelectedModelError) as raised:
        asyncio.run(_maybe_compact_for_route(
            setup,
            body,
            api_kind="chat",
            request_id="explicit-overflow",
            requested_model="codex-pro/gpt-6-astra",
            evaluation=Evaluation("long-context", None, 1, "test"),
            prompt_tokens=322718,
            output_reserve_tokens=16384,
            modalities={"text"},
            image_count=0,
            has_tools=False,
            required_capabilities=RequestCapabilities(protocol="chat"),
            conversation=None,
            excluded_endpoints=set(),
            identity=IdentityProfile.from_settings(
                setup.settings.section("identity")
            ),
            routing_options=resolve_objectives(
                setup.settings.section("routing")
            ),
        ))

    assert raised.value.details["requested_model"] == "codex-pro/gpt-6-astra"
    compact.assert_not_awaited()


@pytest.mark.parametrize("changes", [{"mode":"other"}, {"extended_context_tokens":True},
                                    {"extended_context_tokens":0}, {"extended_context_tokens":1050001},
                                    {"extended_context_tokens":"500000"}, {"endpoint_id":""}])
def test_invalid_policy_rejected(setup, changes):
    value = setup.settings.value
    value["context_policy"].update(changes)
    with pytest.raises(ValueError):
        validate_settings(value)


def test_policy_persists_and_reloads(setup):
    value = setup.settings.value
    value["context_policy"].update(mode="extended", extended_context_tokens=400000)
    setup.settings.write_runtime(value)
    fresh = Settings(setup.settings.defaults_path, setup.settings.runtime_path)
    assert fresh.section("context_policy") == value["context_policy"]


def test_policy_draft_activation_preserves_both_modes(setup, tmp_path):
    manager = PolicyConfigManager(tmp_path / "policy.sqlite3", setup.settings)
    async def scenario():
        for mode in ("compact", "extended", "legacy"):
            active = (await manager.snapshot())["active"]
            draft = await manager.patch_draft({"context_policy": {"mode": mode}},
                expected_revision=active["revision"], expected_fingerprint=active["settings_fingerprint"], source="test")
            assert setup.settings.section("context_policy")["mode"] == active["settings"]["context_policy"]["mode"]
            validated = await manager.validate_draft([], expected_revision=draft["revision"],
                expected_fingerprint=draft["settings_fingerprint"], source="test")
            await manager.activate(expected_revision=validated["revision"],
                expected_fingerprint=validated["settings_fingerprint"], source="test")
            fresh = Settings(setup.settings.defaults_path, setup.settings.runtime_path)
            assert fresh.section("context_policy")["mode"] == mode
    asyncio.run(scenario())


def test_health_preserves_per_account_per_model_limits(setup):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"ok": True, "workers": setup.status.detail["workers"]}))) as client:
            monitor = HealthMonitor(InMemoryStateStore(), client=client)
            status = await monitor.status(setup.registry.by_id(TARGET), force_refresh=True)
            assert status.healthy
            assert status.detail["workers"][0]["model_context_limits"]["gpt-6-astra"]["max_context_tokens"] == 872000
    asyncio.run(scenario())


@pytest.mark.parametrize("catalog,maximum", [({},272000), ({"max_context_window":True},272000),
                                            ({"context_window":272000,"max_context_window":872000},872000)])
def test_adapter_reports_only_positive_integer_catalog_limits(catalog, maximum):
    assert _catalog_context_limits(catalog)["max_context_tokens"] == maximum


def test_large_history_summary_is_bounded_and_keeps_recent_tools():
    class Counter:
        def count_request(self, body, api_kind):
            return len(json.dumps(body, ensure_ascii=False)) // 4 + 1
    counter = Counter()
    calls = []
    def upstream(request):
        body = json.loads(request.content)
        assert counter.count_request(body, "chat") <= 1024
        calls.append(body)
        content = json.dumps({"facts": ["saved fact"], "open_goals": ["finish"]})
        # This fixture tests input batching, not missing provider usage. Report
        # its short synthetic completion so conservative unknown-usage charging
        # does not consume a full 8192-token reservation for each tiny fragment.
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"completion_tokens": counter.count_request({"content": content}, "chat")},
        })
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            compactor = ContextCompactor(counter, CapsuleCipher(Fernet.generate_key().decode()),
                internal_base_url="http://offline.invalid", internal_api_key="test", model_id="summary", client=client)
            recent = [{"role":"user", "content":"latest task"},
                      {"role":"assistant", "tool_calls":[{"id":"call", "type":"function",
                       "function":{"name":"read", "arguments":"{}"}}]},
                      {"role":"tool", "tool_call_id":"call", "content":"result"}]
            body = {"messages":[{"role":"system","content":"keep this constraint"},
                                *[{"role":"user","content":"old " * 4000} for _ in range(4)],
                                *[{"role":"user","content":"recent detail"} for _ in range(4)], *recent]}
            capsule = await compactor.compact(body, api_kind="chat", target_context_tokens=10000,
                                              summary_input_tokens=1024)
            messages = compactor.cipher.decrypt(capsule.encrypted_messages)
            assert messages[0] == body["messages"][0]
            assert messages[-3:] == recent
            assert capsule.after_tokens < capsule.before_tokens
    asyncio.run(scenario())
    assert len(calls) > 1
