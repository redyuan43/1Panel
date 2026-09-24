"""Regression cases for complete history, explicit compaction and cloud affinity."""
import asyncio
import copy
import json
import time
from dataclasses import replace
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_router.api import _candidate_history_token_evidence, _compaction_allowed, _compact_body_for_target, _project_history_for_target
from ai_router.background_context import _store
from ai_router.compaction_policy import require_compaction_enabled
from ai_router.compute import count_tokens, token_cache
from ai_router.endpoint_tokens import EndpointTokenCounter
from ai_router.errors import CompactionUnavailableError, HistoryMigrationRequiredError
from ai_router.history import normalize_history_for_provider
from ai_router.identity import IdentityProfile
from ai_router.token_counter import HuggingFaceTokenCounter
from ai_router.types import ConversationState, ModelCallTarget
from test_context_policy import setup as context_setup
from test_routing_modes import setup, request, trace


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


@pytest.mark.parametrize("strategy", ["legacy", "compact", "extended"])
@pytest.mark.parametrize("section", [{}, {"enabled": False}, {"enabled": True, "mode": "disabled"}])
def test_master_switch_cannot_be_overridden(strategy, section):
    runtime = SimpleNamespace(settings=SimpleNamespace(section=lambda _: section))
    assert not _compaction_allowed(runtime, True, "true", context_strategy=strategy)
    with pytest.raises(CompactionUnavailableError):
        require_compaction_enabled(runtime.settings)


@async_test
async def test_switch_closed_during_foreground_admission(tmp_path, monkeypatch):
    runtime = context_setup.__wrapped__(tmp_path)
    lease = SimpleNamespace(release=AsyncMock())
    runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
    runtime.compactor = SimpleNamespace(model_id="ai-qwen38-27b", summary_request_tokens=lambda _: 100,
                                       compact=AsyncMock(), summary_output_tokens=8192)
    async def acquire(*args, **kwargs):
        runtime.settings.write_runtime({"compaction": {"enabled": False}})
        return ModelCallTarget("http://offline.invalid", "summary", safe_context_tokens=16384)
    monkeypatch.setattr("ai_router.api._acquire_internal_model", acquire)
    with pytest.raises(CompactionUnavailableError, match="disabled"):
        await _compact_body_for_target(runtime, {"messages": []}, api_kind="chat", request_id="test",
                                       target_context=16384, identity=IdentityProfile.from_settings({}))
    runtime.compactor.compact.assert_not_awaited()
    lease.release.assert_awaited_once()


@async_test
async def test_background_submission_needs_master_switch():
    runtime = SimpleNamespace(settings=SimpleNamespace(section=lambda _: {"enabled": False, "background_enabled": True}),
                              clients=SimpleNamespace(current_policy=AsyncMock()))
    assert await _store(runtime, "client") is None
    runtime.clients.current_policy.assert_not_awaited()


@async_test
@pytest.mark.parametrize("mode", ["cost", "quality", "efficiency"])
async def test_cloud_stays_despite_schedule_local_capacity_or_slowness(tmp_path, mode):
    policy, registry, options = setup(tmp_path, mode)
    cloud = registry.by_id("cloud-deepseek-v4-flash")
    conv = ConversationState("conversation", "auto", cloud.id, cloud.tier_rank, "general", time.time())
    # The new-session schedule does not contain the existing cloud model.
    options["flash_order"]["general"] = ["zhipu-glm-5.3-flash"]
    await policy.performance.observe(trace("slow", cloud.id, first=1000), options["performance"])
    decision = await request(policy, options, conversation=conv)
    assert decision.endpoint.id == cloud.id
    assert decision.reason == "cloud_conversation_affinity"
    assert not decision.migration
    # A hard context constraint still wins over affinity.
    policy.health.status_values[cloud.id].eligible_context_tokens = 100
    decision = await request(policy, options, conversation=conv)
    assert decision.endpoint.id != cloud.id


def test_history_keeps_reasoning_and_rejects_lossy_target(tmp_path):
    _, registry, _ = setup(tmp_path)
    v100 = registry.by_id("ai-qwen38-27b")
    body = {"messages": [{"role": "user", "content": "task"},
                          {"role": "assistant", "content": "answer", "reasoning": "real history"}]}
    original = copy.deepcopy(body)
    projected = _project_history_for_target(body, "chat", v100)
    assert projected["messages"][1]["reasoning_content"] == "real history"
    assert body == original
    assert _project_history_for_target(projected, "chat", v100) == projected
    unsupported = replace(v100, metadata={"history_contract": {"accepts_reasoning_content": False}})
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider(body, "chat", unsupported)


@async_test
async def test_legacy_recovery_does_not_evict_eligible_cloud(tmp_path):
    policy, registry, options = setup(tmp_path)
    options["enabled"] = False
    policy.settings._value["routing"]["conversation_stability"]["recovery_mode"] = "next_turn"
    policy.local_pool.member = lambda _: False
    cloud = registry.by_id("cloud-deepseek-v4-flash")
    conv = ConversationState("conversation", "auto", cloud.id, cloud.tier_rank, "general", time.time(),
                             recovery_endpoint_id="ai-qwen38-27b")
    decision = await request(policy, options, conversation=conv)
    assert decision.endpoint.id == cloud.id
    assert not decision.migration


@async_test
async def test_candidates_share_counts_and_preserve_first_turn_history(tmp_path):
    _, registry, _ = setup(tmp_path)
    base = registry.by_id("ai-qwen38-27b")
    endpoints = [replace(base, id=f"candidate-{i}", enabled=True) for i in range(4)]
    calls = []
    def counter(body, api_kind):
        calls.append(copy.deepcopy(body))
        assert body["messages"][1]["reasoning_content"] == "original thought"
        time.sleep(.01)
        return 42
    runtime = SimpleNamespace(registry=registry.with_endpoints(endpoints), token_counter=SimpleNamespace(count_request=counter))
    body = {"messages": [{"role": "user", "content": "task"},
                          {"role": "assistant", "content": "answer", "reasoning": "original thought"}]}
    identity = IdentityProfile.from_settings({"enabled": False})
    context = token_cache.set({})
    try:
        for previous in (None, ConversationState("c", "auto", base.id, 20, "general", 0)):
            result = await _candidate_history_token_evidence(runtime, body=body, api_kind="chat", prompt_tokens=999,
                                                            requested_model="auto", conversation=previous, identity=identity)
            assert all(e["tokens"] == 42 for e in result.values())
        assert len(calls) == 1
        body["messages"][1]["content"] += " changed"
        await count_tokens(runtime, _project_history_for_target(body, "chat", base), "chat")
        assert len(calls) == 2
    finally:
        token_cache.reset(context)
        runtime.compute_executor.close()


def test_template_receives_parsed_arguments_and_reasoning_without_mutating_wire(tmp_path):
    counter = HuggingFaceTokenCounter(tmp_path)
    def template(messages, **kwargs):
        assert messages[0]["tool_calls"][0]["function"]["arguments"] == {"x": 1}
        assert messages[0]["reasoning_content"] == "recorded thought"
        return [1, 2, 3]
    counter._tokenizer = SimpleNamespace(apply_chat_template=template)
    body = {"messages": [{"role": "assistant", "content": "", "reasoning": "recorded thought",
                           "tool_calls": [{"function": {"name": "lookup", "arguments": '{"x":1}'}}]}]}
    original = copy.deepcopy(body)
    assert counter.count_request(body, "chat") == 3
    assert body == original


def test_responses_counter_includes_historical_reasoning():
    from ai_router.token_counter import _responses_to_messages
    messages = _responses_to_messages([
        {"type": "message", "role": "assistant", "content": "answer", "reasoning": "thought"},
        {"type": "reasoning", "encrypted_content": "opaque-history"},
    ])
    assert messages[0]["reasoning_content"] == "thought"
    assert "opaque-history" in messages[1]["content"]


def test_codex_chat_continuation_preserves_native_history(tmp_path):
    from fastapi import Request
    from ai_router.codex_adapter import _chat_to_responses
    _, registry, _ = setup(tmp_path)
    endpoint = registry.by_id("codex-pro-gpt-6-astra")
    reasoning = {"type": "reasoning", "encrypted_content": "test-cipher", "summary": []}
    message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
    body = {"messages": [{"role": "assistant", "content": "answer",
                           "codex_reasoning_items": [reasoning], "codex_message_items": [message]},
                          {"role": "user", "content": "continue"}]}
    original = copy.deepcopy(body)
    projected = _project_history_for_target(body, "chat", endpoint)
    sent = _chat_to_responses(projected, Request({"type": "http", "headers": []}), {}, endpoint.provider_model)
    assert reasoning in sent["input"]
    assert message in sent["input"]
    assert body == original
    with pytest.raises(HistoryMigrationRequiredError):
        _project_history_for_target(body, "chat", registry.by_id("ai-qwen38-27b"))


@pytest.mark.parametrize("items", [[{"type": "reasoning"}], "not-a-list"])
def test_invalid_codex_history_is_rejected_instead_of_dropped(tmp_path, items):
    _, registry, _ = setup(tmp_path)
    with pytest.raises(HistoryMigrationRequiredError, match="invalid opaque"):
        _project_history_for_target({"messages": [{"role": "assistant", "content": "",
                                                  "codex_reasoning_items": items}]},
                                    "chat", registry.by_id("codex-pro-gpt-6-astra"))


@pytest.mark.parametrize("item", [
    {"type": "web_search_call", "id": "ws_test", "status": "completed",
     "action": {"type": "search", "query": "history"}},
    {"type": "item_reference", "id": "msg_test"},
    {"type": "function_call", "id": "fc_test", "call_id": "call_test",
     "name": "lookup", "arguments": '{"b": 2, "a": 1}'},
])
def test_native_responses_history_is_preserved(tmp_path, item):
    _, registry, _ = setup(tmp_path)
    body = {"input": [item]}
    projected = _project_history_for_target(body, "responses", registry.by_id("codex-pro-gpt-6-astra"))
    assert projected == body
    assert projected is not body
    if item["type"] != "function_call":
        adapter = registry.by_id("ai-qwen38-27b")
        adapter = replace(adapter, capabilities=replace(adapter.capabilities, responses="adapter"))
        with pytest.raises(HistoryMigrationRequiredError):
            _project_history_for_target(body, "responses", adapter)


def test_opaque_history_is_included_in_count_estimate(tmp_path):
    counter = HuggingFaceTokenCounter(tmp_path)
    def encode(value, **kwargs):
        assert "test-cipher" in value
        return [1, 2, 3]
    counter._tokenizer = SimpleNamespace(encode=encode)
    body = {"messages": [{"role": "assistant", "content": "",
                           "codex_reasoning_items": [{"type": "reasoning", "encrypted_content": "test-cipher"}]},
                          {"role": "user", "content": "continue"}]}
    assert counter.count_request(body, "chat") == 3
    assert counter.prefix_token_ids(body, "chat") == ()
    assert counter.count_request({"input": [{"type": "web_search_call", "action": {"query": "test-cipher"}}]}, "responses") == 3


@async_test
async def test_render_overflow_is_not_a_tokenizer_outage(tmp_path):
    _, registry, _ = setup(tmp_path)
    endpoint = registry.by_id("ai-qwen38-27b")
    endpoint = replace(endpoint, metadata={"token_counting": {"enabled": True, "method": "render"}})
    counter = EndpointTokenCounter()
    await counter.client.aclose()
    message = ("This model's maximum context length is 262144 tokens. However, you requested 65536 output tokens "
               "and your prompt contains at least 196609 input tokens, for a total of at least 262145 tokens.")
    counter.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"error": {"message": message}})))
    try:
        evidence = await counter.count(endpoint, {"messages": [{"role": "user", "content": "task"}], "max_tokens": 65536}, "chat", 100)
        assert evidence["reason"] == "backend_context_exceeded"
        assert evidence["tokens"] + 65536 > 262144
        assert evidence["exact"] is False
    finally:
        await counter.close()
