"""Bounded recovery and lossless continuation for the active local model."""
import asyncio
import copy
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_router.config import Registry, Settings, validate_settings
from ai_router.errors import HistoryMigrationRequiredError
from ai_router.history import normalize_history_for_provider
from ai_router.responses_adapter import responses_request_to_chat, _chat_value_to_response
from ai_router.types import ConversationState, RequestCapabilities
from test_route_diagnosis_policy import (
    FakeRegistry, FakeSettings, SequencedHealth, choose, conversation, endpoint, status,
)
from ai_router.policy import RoutingPolicy
from test_routing_modes import setup, request, run, trace

ROOT = Path(__file__).resolve().parents[1]
BID = 'ivan-v10016-bonsai2-196k'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        raise AssertionError('unexpected network access')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


def health_policy(wait=0.04):
    first = endpoint('ai-primary', tier_rank=1, safe_context=1000, quality=.8)
    second = endpoint('amd-next', tier_rank=2, safe_context=1000, quality=.9)
    settings = FakeSettings()
    settings.sections['routing']['conversation_stability']['health_wait_seconds'] = wait
    health = SequencedHealth({first.id: status(first.id, healthy=False),
                              second.id: status(second.id, healthy=True)})
    return RoutingPolicy(FakeRegistry([first, second]), settings, health), first, second


def test_recovered_health_retains_current_before_deadline():
    policy, first, _ = health_policy()
    policy.health.refreshes[first.id] = [status(first.id, healthy=True)]
    assert choose(policy, conversation(first.id, 1)).endpoint.id == first.id
    assert policy.health.refresh_calls == [first.id]


def test_continuous_failure_waits_then_migrates():
    policy, first, second = health_policy()
    start = time.monotonic()
    assert choose(policy, conversation(first.id, 1)).endpoint.id == second.id
    assert .035 <= time.monotonic() - start < 1


def test_slow_probe_cannot_exceed_wait_budget():
    policy, first, second = health_policy()
    cancelled = []
    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)
    policy.health.status = slow
    start = time.monotonic()
    assert choose(policy, conversation(first.id, 1)).endpoint.id == second.id
    assert time.monotonic() - start < 1 and cancelled == [True]


def test_cancellation_does_not_select_another_model():
    policy, first, _ = health_policy(wait=20)
    async def scenario():
        from ai_router.types import Evaluation
        entered = asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await asyncio.sleep(5)
        policy.health.status = slow
        task = asyncio.create_task(policy.choose(requested_model='auto',
            evaluation=Evaluation('general', None, 1, 'test'), prompt_tokens=100,
            output_reserve_tokens=10, modalities={'text'}, has_tools=False,
            conversation=conversation(first.id, 1)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())


def test_zero_wait_and_hard_incompatibility_do_not_probe():
    policy, first, second = health_policy(wait=0)
    assert choose(policy, conversation(first.id, 1)).endpoint.id == second.id
    assert not policy.health.refresh_calls
    policy, first, second = health_policy(wait=20)
    policy.health.initial[first.id] = status(first.id, healthy=True)
    first = replace(first, safe_context_tokens=50)
    policy.registry = FakeRegistry([first, second])
    assert choose(policy, conversation(first.id, 1)).endpoint.id == second.id
    assert not policy.health.refresh_calls


@pytest.mark.parametrize('protocol,stream', [('chat', False), ('chat', True), ('responses', False), ('responses', True)])
def test_efficiency_manual_keeps_model_even_if_slow(tmp_path, protocol, stream):
    policy, registry, options = setup(tmp_path)
    policy.settings._value['routing']['conversation_stability'].update(enabled=True, recovery_mode='manual')
    async def scenario():
        current = registry.by_id('ai-qwen38-27b')
        await policy.performance.observe(trace('slow', current.id, first=300), options['performance'])
        conv = ConversationState(conversation_id='conversation', public_model='auto', task='general',
            last_seen=time.time(), endpoint_id=current.id, tier_rank=current.tier_rank,
            recovery_endpoint_id='amd-qwen38-rocmfpx-128k')
        selected = await request(policy, options, conversation=conv,
            required_capabilities=RequestCapabilities(protocol, streaming=stream))
        assert selected.endpoint.id == current.id and selected.reason == 'efficiency_affinity'
        changed = await request(policy, options, conversation=conv,
            candidate_history_errors={current.id: 'history_incompatible'})
        assert changed.endpoint.id != current.id
    run(scenario())


@pytest.mark.parametrize('wait', [-1, 121, float('nan'), float('inf'), True, '20'])
def test_invalid_wait_rejected(tmp_path, wait):
    settings = Settings(ROOT / 'config/defaults.yaml', tmp_path / 'settings.yaml')
    value = copy.deepcopy(settings.value)
    value['routing']['conversation_stability']['health_wait_seconds'] = wait
    with pytest.raises(ValueError, match='health_wait_seconds'):
        validate_settings(value)


def bonsai():
    return Registry(ROOT / 'config/registry.yaml').by_id(BID)


@pytest.mark.parametrize('alias', ['reasoning', 'reasoning_content'])
def test_bonsai_chat_tool_continuation_preserves_history(alias):
    body = {'messages': [{'role': 'user', 'content': 'Read the lookup.'},
        {'role': 'assistant', 'content': '', alias: 'Private marker orange-82.',
         'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'lookup-result'},
        {'role': 'user', 'content': 'Continue.'}]}
    original = copy.deepcopy(body)
    result = normalize_history_for_provider(body, 'chat', bonsai())
    assert body == original
    assert result['messages'][1]['reasoning_content'] == 'Private marker orange-82.'
    assert result['chat_template_kwargs']['preserve_thinking'] is True
    assert result['messages'][2] == body['messages'][2]


def test_bonsai_responses_own_output_tool_roundtrip():
    response = _chat_value_to_response({'choices': [{'finish_reason': 'tool_calls', 'message': {
        'role': 'assistant', 'content': '', 'reasoning_content': 'marker-blue-93',
        'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]}}]},
        model='auto', preserve_history_fields=True)
    body = {'input': [{'role': 'user', 'content': 'Look up.'}, *response['output'],
                     {'type': 'function_call_output', 'call_id': 'c1', 'output': 'value-green-47'}]}
    normalized = normalize_history_for_provider(body, 'responses', bonsai())
    chat = responses_request_to_chat(normalized)
    assert any(m.get('reasoning_content') == 'marker-blue-93' for m in chat['messages'])
    assert chat['messages'][-1]['content'] == 'value-green-47'
    assert chat['chat_template_kwargs']['preserve_thinking'] is True


@pytest.mark.parametrize('options', [{'preserve_thinking': False}, {'chat_template_kwargs': {'preserve_thinking': False}}])
def test_bonsai_rejects_discarding_thinking(options):
    body = {**options, 'messages': [{'role': 'assistant', 'content': 'answer', 'reasoning_content': 'keep'}]}
    with pytest.raises(HistoryMigrationRequiredError):
        normalize_history_for_provider(body, 'chat', bonsai())


def test_wait_refreshes_expired_fallback_health():
    policy, first, second = health_policy()
    policy.settings.sections['health']['stale_after_seconds'] = .02
    calls = []
    async def refresh(endpoints, **kwargs):
        calls.append([e.id for e in endpoints])
        return {e.id: status(e.id, healthy=e.id != first.id) for e in endpoints}
    policy.health.statuses = refresh
    assert choose(policy, conversation(first.id, 1)).endpoint.id == second.id
    assert calls == [[first.id, second.id], [second.id]]


def test_incompatible_history_skips_unhealthy_model_wait():
    policy, first, second = health_policy(wait=20)
    from ai_router.types import Evaluation
    result = run(policy.choose(requested_model='auto',
        evaluation=Evaluation('general', None, 1, 'test'), prompt_tokens=100,
        output_reserve_tokens=10, modalities={'text'}, has_tools=False,
        conversation=conversation(first.id, 1),
        candidate_history_errors={first.id: 'history_incompatible'}))
    assert result.endpoint.id == second.id
    assert not policy.health.refresh_calls


def test_bonsai_stays_selected_when_qwen_is_healthy(tmp_path):
    from test_intelligent_v2 import status_for
    policy, registry, options = setup(tmp_path)
    current = replace(registry.by_id(BID), enabled=True, auto_candidate=True)
    registry.endpoints = tuple(current if e.id == BID else e for e in registry.endpoints)
    policy.health.status_values[BID] = status_for(current)
    policy.settings._value['routing']['conversation_stability'].update(enabled=True, recovery_mode='manual')
    conv = ConversationState(conversation_id='bonsai-after-failover', public_model='auto', task='general',
        last_seen=time.time(), endpoint_id=BID, tier_rank=current.tier_rank,
        recovery_endpoint_id='ai-qwen38-27b')
    assert policy.health.status_values['ai-qwen38-27b'].healthy
    assert run(request(policy, options, conversation=conv)).endpoint.id == BID
    assert run(request(policy, options, conversation=conv,
        requested_model=registry.by_id('ai-qwen38-27b').public_model)).endpoint.id == 'ai-qwen38-27b'
