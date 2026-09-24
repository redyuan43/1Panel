"""Lossless alias comparison and archived Bonsai continuation regression."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from ai_router.errors import HistoryMigrationRequiredError
from ai_router.history import history_lookup_identities, verified_history_identity, normalize_history_for_provider
from ai_router.config import Registry
from pathlib import Path
from ai_router.history_index import index_completed
from ai_router.reasoning_fields import canonical_reasoning_fields
from ai_router.workbuddy_history import chain, reconcile, prepare
from test_history_migration import fixture, save
from test_workbuddy_history import req
from test_routing_modes import setup, request
from test_intelligent_v2 import status_for


@pytest.mark.parametrize("fields", [
    {"reasoning": "thought"}, {"reasoning_content": "thought"},
    {"reasoning": "thought", "reasoning_content": "thought"},
    {"reasoning": "thought", "reasoning_content": ""},
    {"reasoning": "", "reasoning_content": "thought"},
])
def test_text_aliases_share_identity_and_prefix_without_mutation(fields):
    messages = [{"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer", **fields}]
    before = copy.deepcopy(messages)
    expected = [messages[0], {"role": "assistant", "content": "answer", "reasoning_content": "thought"}]
    assert verified_history_identity(messages) == verified_history_identity(expected)
    assert chain({"messages": messages}) == chain({"messages": expected})
    assert messages == before


@pytest.mark.parametrize("fields", [
    {"reasoning": {"encrypted": "opaque"}},
    {"reasoning_content": {"encrypted": "opaque"}, "reasoning": "text"},
    {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
])
def test_structured_provider_state_is_never_overwritten(fields):
    before = copy.deepcopy(fields)
    result = canonical_reasoning_fields(fields)
    assert result == fields == before
    assert result is not fields


def test_conflicting_text_aliases_are_rejected_consistently():
    message = {"role": "assistant", "content": "answer", "reasoning": "A", "reasoning_content": "B"}
    for operation in (lambda: verified_history_identity([message]),
                      lambda: chain({"messages": [message]})):
        with pytest.raises(HistoryMigrationRequiredError):
            operation()


@pytest.mark.parametrize("damage", [None, "encrypted", "summary", "unknown", "wrong_type"])
def test_qwen_native_text_reasoning_roundtrip_preserves_only_supported_items(damage):
    registry = Registry(Path(__file__).resolve().parents[1] / "config/registry.yaml")
    item = {"id": "rs-test", "type": "reasoning", "summary": [], "encrypted_content": None,
            "content": [{"type": "reasoning_text", "text": "original thought"}], "status": None}
    if damage == "encrypted": item["encrypted_content"] = "opaque-provider-state"
    if damage == "summary": item["summary"] = [{"type": "summary_text", "text": "summary"}]
    if damage == "unknown": item["provider_state"] = "opaque"
    if damage == "wrong_type": item["content"][0]["type"] = "unknown"
    body = {"input": [item]}
    before = copy.deepcopy(body)
    if damage is None:
        assert normalize_history_for_provider(body, "responses", registry.by_id("ai-qwen38-27b")) == body
    else:
        with pytest.raises(HistoryMigrationRequiredError):
            normalize_history_for_provider(body, "responses", registry.by_id("ai-qwen38-27b"))
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider(body, "responses", registry.by_id("ivan-v10016-bonsai2-196k"))
    assert body == before


def test_overlay_reuses_archived_input_when_only_alias_changes():
    raw = req()
    raw["messages"].append({"role": "assistant", "content": "answer", "reasoning_content": "original thought"})
    old = prepare(raw, "workbuddy-public")[0].body
    fresh = copy.deepcopy(raw)
    fresh["messages"][2]["reasoning"] = fresh["messages"][2].pop("reasoning_content")
    fresh["messages"].append({"role": "user", "content": "next"})
    before = copy.deepcopy(fresh)
    restored, _ = reconcile(fresh, "workbuddy-public", raw, old)
    assert restored["messages"][:len(old["messages"])] == old["messages"]
    assert fresh == before
    fresh["messages"][2]["reasoning"] = "changed thought"
    assert reconcile(fresh, "workbuddy-public", raw, old) is None


@pytest.mark.parametrize("stream", [False, True])
def test_archived_output_alias_recovers_bonsai_and_auto_keeps_it(tmp_path, monkeypatch, stream):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=1)
    policy, registry, options = setup(tmp_path)
    endpoint_id = "ivan-v10016-bonsai2-196k"
    current = replace(registry.by_id(endpoint_id), enabled=True, auto_candidate=True)
    registry.endpoints = tuple(current if e.id == endpoint_id else e for e in registry.endpoints)
    policy.health.status_values[endpoint_id] = status_for(current)
    policy.settings._value["routing"]["conversation_stability"].update(enabled=True, recovery_mode="manual")
    archive = archives["0"]
    assistant = {"role": "assistant", "content": "", "reasoning_content": "preserved thought",
                 "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    archive["response"]["assistant_items"] = [assistant] if stream else None
    if not stream:
        archive["response"]["body"] = {"encoding": "json", "value": {"choices": [{"message": assistant}]}}
    original = copy.deepcopy(archive)
    incoming = copy.deepcopy(assistant)
    incoming["reasoning"] = incoming.pop("reasoning_content")
    messages = [*archive["pipeline"]["bodies"]["body"]["messages"], incoming,
                {"role": "tool", "tool_call_id": "c1", "content": "result"}]

    async def case():
        await save(runtime.conversations, endpoint=endpoint_id, directive=None)
        trace = {**archive["request"], "branch_id": "branch", "status": "succeeded"}
        assert await index_completed(runtime, trace, NS(read=archives.get))
        lineage = await runtime.conversations.lineage_context(client_id="workbuddy-public",
            identities=tuple("wb-raw-v1:" + value for value in history_lookup_identities(messages)),
            explicit_lineage_id=None, previous_response_id=None, force_new=False)
        assert lineage.parent.endpoint_id == endpoint_id
        assert policy.health.status_values["ai-qwen38-27b"].healthy
        selected = await request(policy, options, conversation=lineage.parent)
        assert selected.endpoint.id == endpoint_id
        assert selected.reason == "efficiency_affinity"
        assert archive == original
    asyncio.run(case())
