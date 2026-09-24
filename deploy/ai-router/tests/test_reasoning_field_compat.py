import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from ai_router.api import _send_upstream
from ai_router.errors import HistoryMigrationRequiredError
from ai_router.config import Registry
from ai_router.identity import IdentityProfile
from ai_router.history import SSEAccumulator, assistant_items_from_response, normalize_history_for_provider
from ai_router.reasoning_fields import chat_reasoning, deepseek_tool_history
from ai_router.responses_adapter import chat_response_to_responses
from ai_router.types import RouteDecision


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_stream_reasoning_is_preserved_even_without_visible_text(field):
    accumulator = SSEAccumulator('chat')
    for delta, finish in [({field: 'synthetic thought'}, None), ({}, 'stop')]:
        event = {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        wire = ('data: ' + json.dumps(event) + '\n\n').encode()
        for byte in wire:
            accumulator.feed(bytes([byte]))
    accumulator.feed(b'data: [DONE]\n\n')
    accumulator.finish()
    assert accumulator.assistant_items()[0]['reasoning_content'] == 'synthetic thought'
    assert accumulator.output_observation()['reasoning_chars'] == 17
    assert accumulator.output_observation()['effective']


@pytest.mark.parametrize('field', ['reasoning', 'reasoning_content'])
def test_nonstream_and_responses_conversion_keep_reasoning(field):
    payload = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'answer', field: 'thought'},
                                      'finish_reason': 'stop'}]}).encode()
    assert assistant_items_from_response(payload, 'chat')[0]['reasoning_content'] == 'thought'
    result = json.loads(chat_response_to_responses(payload, model='test', preserve_history_fields=True))
    assert result['output'][0]['reasoning_content'] == 'thought'


def test_deepseek_migration_keeps_existing_reasoning_without_inventing_it():
    endpoint = Registry(Path(__file__).resolve().parents[1] / 'config/registry.yaml').by_id('cloud-deepseek-v4-flash')
    body = {'messages': [{'role': 'assistant', 'content': 'answer', 'reasoning': 'thought'},
                         {'role': 'assistant', 'content': 'no recorded thought'}]}
    before = copy.deepcopy(body)
    result = normalize_history_for_provider(body, 'chat', endpoint)
    assert body == before
    assert result['messages'][0]['reasoning_content'] == 'thought'
    assert 'reasoning_content' not in result['messages'][1]


def test_alias_precedence_does_not_duplicate_or_stringify_metadata():
    with pytest.raises(HistoryMigrationRequiredError):
        chat_reasoning({'reasoning_content': 'one', 'reasoning': 'two'})
    assert chat_reasoning({'reasoning_content': '', 'reasoning': 'two'}) == 'two'
    assert chat_reasoning({'reasoning': {'effort': 'high'}}) is None


def test_deepseek_absence_is_explicit_without_disabling_thinking_or_changing_tools():
    call = {'id': 'call1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}
    body = {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high', 'tools': [{'type': 'function'}],
            'messages': [{'role': 'user', 'content': 'test'},
                         {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                         {'role': 'tool', 'tool_call_id': 'call1', 'content': '7'},
                         {'role': 'assistant', 'content': '7', 'reasoning': 'real reasoning'},
                         {'role': 'assistant', 'content': 'done', 'reasoning_content': 'keep'}]}
    original = copy.deepcopy(body)
    result, counts = deepseek_tool_history(body)
    assert body == original
    assert result['messages'][1]['reasoning_content'] == ''
    assert result['messages'][3]['reasoning_content'] == 'real reasoning'
    assert result['messages'][4]['reasoning_content'] == 'keep'
    assert result['messages'][1]['tool_calls'] == [call]
    assert result['messages'][2] == original['messages'][2]
    assert result['thinking'] == original['thinking']
    assert result['reasoning_effort'] == 'high'
    assert counts == {'alias_fields_preserved': 1, 'absent_fields_explicit': 1}
    assert deepseek_tool_history(result)[0] == result


def test_deepseek_non_tool_request_is_unchanged():
    body = {'messages': [{'role': 'assistant', 'content': 'hello'}]}
    assert deepseek_tool_history(body)[0] == body


def test_responses_migration_retains_alias_before_chat_conversion():
    from ai_router.responses_adapter import responses_request_to_chat
    endpoint = Registry(Path(__file__).resolve().parents[1] / 'config/registry.yaml').by_id('cloud-deepseek-v4-flash')
    body = {'input': [{'type': 'function_call', 'call_id': 'c1', 'name': 'lookup', 'arguments': '{}',
                       'reasoning': 'actual recorded reasoning'},
                      {'type': 'function_call_output', 'call_id': 'c1', 'output': '7'}]}
    projected = normalize_history_for_provider(body, 'responses', endpoint)
    converted = responses_request_to_chat(projected)
    assert converted['messages'][0]['reasoning_content'] == 'actual recorded reasoning'
    assert body['input'][0]['reasoning'] == 'actual recorded reasoning'


def test_native_responses_migration_preserves_real_reasoning():
    endpoint = Registry(Path(__file__).resolve().parents[1] / 'config/registry.yaml').by_id('cloud-deepseek-v4-flash')
    real = {'type': 'reasoning', 'content': [{'type': 'reasoning_text', 'text': 'real thought'}], 'summary': []}
    body = {'tools': [{'type': 'function', 'name': 'lookup'}], 'reasoning': {'effort': 'high'}, 'input': [
        {'role': 'user', 'content': 'test'}, real,
        {'type': 'function_call', 'call_id': 'a', 'name': 'lookup', 'arguments': '{}'},
        {'type': 'function_call_output', 'call_id': 'a', 'output': '7'},
        {'role': 'assistant', 'content': '7'}]}
    original = copy.deepcopy(body)
    projected = normalize_history_for_provider(body, 'responses', endpoint)
    assert body == original
    assert projected['input'][1] == real
    assert len(projected['input']) == len(body['input'])
    assert projected['reasoning'] == body['reasoning']


@pytest.mark.parametrize('api_kind,adapter', [('chat', False), ('responses', True), ('responses', False)])
def test_final_wire_deepseek_tool_continuation(api_kind, adapter):
    async def run():
        endpoint = Registry(Path(__file__).resolve().parents[1] / 'config/registry.yaml').by_id('cloud-deepseek-v4-flash')
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = SimpleNamespace(internal_base_url='http://test.invalid', internal_api_key='test',
                                      internal_client=client, scheduler=SimpleNamespace(admission=SimpleNamespace(enabled=False)))
            decision = RouteDecision(endpoint=endpoint, requested_model='auto', task='general', prompt_tokens=10,
                                     output_reserve_tokens=16, reason='test', affinity='new', score=1,
                                     native_or_adapter='adapter' if adapter else 'native')
            tool = {'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}
            if api_kind == 'chat':
                body = {'thinking': {'type': 'enabled'}, 'tools': [tool], 'messages': [
                    {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function',
                     'function': {'name': 'lookup', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'c1', 'content': '7'}]}
            else:
                body = {'reasoning': {'effort': 'high'}, 'tools': [{'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object'}}],
                        'input': [{'type': 'function_call', 'call_id': 'c1', 'name': 'lookup', 'arguments': '{}'},
                                  {'type': 'function_call_output', 'call_id': 'c1', 'output': '7'}]}
            before = copy.deepcopy(body)
            response = await _send_upstream(runtime, Request({'type': 'http', 'headers': []}), body,
                                            api_kind=api_kind, decision=decision,
                                            identity=IdentityProfile.from_settings({'enabled': False}))
            await response.aclose()
            assert body == before
            if api_kind == 'responses' and not adapter:
                # Native Responses must not silently become Chat or lose thinking.
                assert captured[0]['input'] == body['input']
                assert captured[0]['reasoning'] == body['reasoning']
            else:
                assert captured[0]['messages'][0]['reasoning_content'] == ''
                assert captured[0]['messages'][1]['content'] == '7'
            assert captured[0].get('extra_body', {}).get('thinking', {}).get('type') != 'disabled'
    asyncio.run(run())
