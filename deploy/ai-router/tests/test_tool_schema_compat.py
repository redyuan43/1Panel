from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from jsonschema import Draft7Validator, Draft202012Validator
from starlette.requests import Request

from ai_router.api import _acquire_route_capacity, _prepare_routed_body, _send_upstream
from ai_router.compaction import (
    CapsuleCipher,
    ContextCompactor,
    extract_messages,
    message_hash,
    replace_messages,
)
from ai_router.errors import NoCompatibleModelError
from ai_router.history import apply_stored_history, history_identities, persist_history
from ai_router.identity import IdentityProfile
from ai_router.protocol import normalize_llama_tool_schemas
from ai_router.types import ConversationState, Endpoint, RouteDecision


def tool_schema():
    return {
        "type": "object",
        "properties": {
            "toolName": {"type": "string"},
            "params": {"type": "object", "additionalProperties": {}},
        },
        "required": ["toolName", "params"],
        "additionalProperties": False,
    }


def tools_for(schema, api_kind="chat"):
    function = {
        "name": "deferred_execute",
        "parameters": copy.deepcopy(schema),
        "strict": False,
    }
    if api_kind == "chat":
        return [{"type": "function", "function": function}]
    return [{"type": "function", **function}]


def parameters(tools, api_kind="chat"):
    return (tools[0]["function"] if api_kind == "chat" else tools[0])["parameters"]


def normalized_schema(schema):
    return parameters(normalize_llama_tool_schemas(tools_for(schema), "chat"))


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_equivalent_rewrite_is_idempotent_and_does_not_mutate_input(api_kind):
    tools = tools_for(tool_schema(), api_kind)
    original = copy.deepcopy(tools)
    result = normalize_llama_tool_schemas(tools, api_kind)
    assert parameters(result, api_kind)["properties"]["params"] == {
        "type": "object", "additionalProperties": True,
    }
    assert parameters(result, api_kind)["additionalProperties"] is False
    assert tools == original
    assert normalize_llama_tool_schemas(result, api_kind) == result
    parameters(result, api_kind)["required"].append("new")
    assert tools == original


@pytest.mark.parametrize("validator", [Draft7Validator, Draft202012Validator])
@pytest.mark.parametrize("value", ["library", 17, 1.25, True, None, [], [1, "x"], {}, {"nested": 2}])
def test_all_json_value_types_keep_identical_validation(validator, value):
    original = tool_schema()
    converted = normalized_schema(original)
    validator.check_schema(original)
    validator.check_schema(converted)
    instance = {"toolName": "anything", "params": {"arbitrary_parameter": value}}
    assert validator(original).is_valid(instance)
    assert validator(converted).is_valid(instance)


@pytest.mark.parametrize("instance", [
    {},
    {"toolName": 1, "params": {}},
    {"toolName": "x", "params": "library"},
    {"toolName": "x", "params": {}, "unexpected": 1},
])
def test_existing_rejections_are_preserved(instance):
    original = tool_schema()
    assert not Draft7Validator(original).is_valid(instance)
    assert not Draft7Validator(normalized_schema(original)).is_valid(instance)


@pytest.mark.parametrize("schema", [
    {},
    True,
    False,
    {"type": "object"},
    {"additionalProperties": False},
    {"additionalProperties": True},
    {"additionalProperties": {"type": "string"}},
    {"additionalProperties": {"description": "any value"}},
    {"additionalProperties": {"$ref": "#/$defs/value"}},
])
def test_non_target_constraints_are_unchanged(schema):
    assert normalized_schema(schema) == schema


@pytest.mark.parametrize("key", [
    "properties", "patternProperties", "definitions", "$defs",
    "dependentSchemas", "dependencies",
])
def test_schema_maps_are_traversed_without_treating_property_names_as_keywords(key):
    schema = {key: {"additionalProperties": {}, "value": {"additionalProperties": {}}}}
    converted = normalized_schema(schema)
    assert converted[key]["additionalProperties"] == {}
    assert converted[key]["value"]["additionalProperties"] is True


@pytest.mark.parametrize("key", [
    "additionalProperties", "additionalItems", "items", "contains",
    "propertyNames", "not", "if", "then", "else",
    "unevaluatedProperties", "unevaluatedItems", "contentSchema",
])
def test_nested_schema_positions(key):
    converted = normalized_schema({key: {"additionalProperties": {}}})
    assert converted[key] == {"additionalProperties": True}


@pytest.mark.parametrize("key", ["allOf", "anyOf", "oneOf", "items", "prefixItems"])
def test_schema_arrays_preserve_boolean_schemas(key):
    converted = normalized_schema({key: [False, {}, {"additionalProperties": {}}]})
    assert converted[key] == [False, {}, {"additionalProperties": True}]


def test_literal_data_and_unknown_extensions_are_untouched():
    literal = {"additionalProperties": {}, "properties": {"x": {"additionalProperties": {}}}}
    schema = {
        "additionalProperties": {},
        "default": literal,
        "const": literal,
        "examples": [literal],
        "enum": [literal],
        "x-metadata": literal,
        "description": json.dumps(literal),
        "dependencies": {"x": ["additionalProperties"]},
    }
    original = copy.deepcopy(schema)
    converted = normalized_schema(schema)
    expected = copy.deepcopy(schema)
    expected["additionalProperties"] = True
    assert converted == expected
    assert schema == original


def test_local_refs_keep_equivalent_targets_without_dereferencing():
    schema = {
        "$defs": {"value": {"type": "object", "additionalProperties": {}}},
        "type": "object",
        "properties": {"params": {"$ref": "#/$defs/value"}},
        "additionalProperties": False,
    }
    converted = normalized_schema(schema)
    assert converted["properties"] == schema["properties"]
    for value in ["s", 1, False, None, [], {}]:
        instance = {"params": {"x": value}}
        assert Draft202012Validator(schema).is_valid(instance)
        assert Draft202012Validator(converted).is_valid(instance)
    remote = {"$ref": "https://must-not-fetch.invalid/schema"}
    assert normalized_schema(remote) == remote


@pytest.mark.parametrize("tools", [None, {}, "bad", [], [None, 1], [
    {"type": "custom", "parameters": {"additionalProperties": {}}},
    {"type": "web_search", "parameters": {"additionalProperties": {}}},
    {"type": "function", "function": None},
    {"type": "function", "function": {"parameters": []}},
]])
def test_unknown_or_malformed_tools_are_not_repaired(tools):
    assert normalize_llama_tool_schemas(tools, "chat") == tools


def test_unknown_protocol_is_untouched():
    tools = tools_for(tool_schema())
    assert normalize_llama_tool_schemas(tools, "unknown") == tools


def test_responses_legacy_nested_function_matches_adapter_precedence():
    tools = tools_for(tool_schema(), "chat")
    tools[0]["parameters"] = {"additionalProperties": {}}
    original = copy.deepcopy(tools)
    result = normalize_llama_tool_schemas(tools, "responses")
    assert parameters(result)["properties"]["params"]["additionalProperties"] is True
    assert result[0]["parameters"] == {"additionalProperties": {}}
    assert tools == original


def endpoint_for(backend_type="llama_cpp", cloud=False):
    return Endpoint(
        id=f"test-{backend_type}",
        public_model="test/local",
        provider_model="test-model",
        api_base="http://backend.test/v1",
        node="test",
        role="responder",
        tier="local",
        tier_rank=1,
        modalities=("text",),
        tasks=("general",),
        safe_context_tokens=100000,
        configured_context_tokens=100000,
        max_concurrency=1,
        backend_type=backend_type,
        health_url="http://backend.test/health",
        backend_api_key_env="SCHEMA_COMPAT_TEST_BACKEND_KEY",
        cloud=cloud,
    )


def decision_for(endpoint, api_kind="chat", mode="native"):
    return RouteDecision(
        endpoint=endpoint, requested_model="auto", task="general",
        prompt_tokens=0, output_reserve_tokens=16,
        reason="affinity", affinity="hit", score=1,
        upstream_api_base=endpoint.api_base,
        protocol=api_kind, native_or_adapter=mode,
        branch_id="branch-test", parent_branch_id="parent-test",
    )


class RecordingCounter:
    def __init__(self):
        self.bodies = []

    def count_request(self, body, api_kind):
        self.bodies.append(copy.deepcopy(body))
        return len(json.dumps(body, sort_keys=True))


def runtime_for(endpoint):
    return SimpleNamespace(
        token_counter=RecordingCounter(),
        settings=SimpleNamespace(section=lambda _: {}),
        registry=SimpleNamespace(by_id=lambda _: endpoint),
    )


def body_for(api_kind="chat"):
    messages = [
        {"role": "user", "content": 'Literal: {"additionalProperties": {}}'},
        {"role": "assistant", "content": "Previous answer"},
        {"role": "user", "content": "Continue with the tool"},
    ]
    return {
        "model": "auto",
        "tools": tools_for(tool_schema(), api_kind),
        "messages" if api_kind == "chat" else "input": messages,
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


async def prepare(runtime, body, decision, **kwargs):
    return await _prepare_routed_body(
        runtime, body, api_kind=decision.protocol, decision=decision,
        request_id="schema-compat-test",
        identity=IdentityProfile.from_settings({"enabled": False}),
        **kwargs,
    )


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("backend_type, cloud, converted", [
    ("llama_cpp", False, True),
    ("ai_pool", False, True),
    ("llama_cpp", True, False),
    ("ai_pool", True, False),
    ("openai", True, False),
    ("openai", False, False),
    ("vllm", False, False),
    ("codex_pool", False, False),
])
def test_backend_scope_history_and_token_accounting(api_kind, backend_type, cloud, converted):
    endpoint = endpoint_for(backend_type, cloud)
    runtime = runtime_for(endpoint)
    decision = decision_for(endpoint, api_kind)
    body = body_for(api_kind)
    original = copy.deepcopy(body)
    conversation = ConversationState(
        conversation_id="lineage-test", public_model=endpoint.public_model,
        endpoint_id=endpoint.id, tier_rank=1, task="general", last_seen=1,
        branch_id="branch-test", parent_branch_id="parent-test",
    )
    old_conversation = copy.deepcopy(conversation)
    routed, capsule = asyncio.run(prepare(runtime, body, decision, conversation=conversation))
    additional = parameters(routed["tools"], api_kind)["properties"]["params"]["additionalProperties"]
    assert additional == (True if converted else {})
    assert body == original
    assert conversation == old_conversation
    assert decision.affinity == "hit"
    assert decision.branch_id == "branch-test"
    assert decision.parent_branch_id == "parent-test"
    assert capsule is None
    assert runtime.token_counter.bodies == [routed]
    assert decision.prompt_tokens == len(json.dumps(routed, sort_keys=True))
    assert extract_messages(routed, api_kind) == extract_messages(body, api_kind)
    assert history_identities(extract_messages(routed, api_kind)) == history_identities(
        extract_messages(body, api_kind)
    )


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_cloud_retry_starts_from_original_tools(api_kind):
    body = body_for(api_kind)
    original = copy.deepcopy(body)
    local = endpoint_for()
    local_routed, _ = asyncio.run(prepare(runtime_for(local), body, decision_for(local, api_kind)))
    cloud = endpoint_for("openai", True)
    cloud_routed, _ = asyncio.run(prepare(runtime_for(cloud), body, decision_for(cloud, api_kind)))
    assert parameters(local_routed["tools"], api_kind)["properties"]["params"]["additionalProperties"] is True
    assert cloud_routed == original
    assert body == original


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_explicit_compaction_preserves_converted_tools_and_original_body(monkeypatch, api_kind):
    endpoint = replace(endpoint_for(), safe_context_tokens=256)
    runtime = runtime_for(endpoint)
    decision = decision_for(endpoint, api_kind)
    body = body_for(api_kind)
    original = copy.deepcopy(body)
    capsule = object()

    async def compact(current, value, **kwargs):
        assert parameters(value["tools"], api_kind)["properties"]["params"]["additionalProperties"] is True
        return capsule, replace_messages(value, api_kind, [{"role": "user", "content": "summary"}]), 128

    monkeypatch.setattr("ai_router.api._compact_body_for_target", compact)
    with pytest.raises(NoCompatibleModelError):
        asyncio.run(prepare(runtime, body, decision))
    routed, actual = asyncio.run(prepare(runtime, body, decision, allow_compaction=True))
    assert actual is capsule
    assert decision.history_mode == "capsule"
    assert decision.prompt_tokens == 128
    assert routed["tools"] == normalize_llama_tool_schemas(body["tools"], api_kind)
    assert body == original


@pytest.mark.parametrize("api_kind, mode, tool_shape", [
    ("chat", "native", "chat"),
    ("responses", "native", "responses"),
    ("responses", "adapter", "responses"),
    ("responses", "adapter", "chat"),
])
@pytest.mark.parametrize("stream", [False, True])
def test_actual_outgoing_payload_and_response_are_preserved(api_kind, mode, tool_shape, stream):
    async def check():
        endpoint = endpoint_for()
        decision = decision_for(endpoint, api_kind, mode)
        runtime = runtime_for(endpoint)
        body = body_for(api_kind)
        body["tools"] = tools_for(tool_schema(), tool_shape)
        body["stream"] = stream
        # Interleaved argument fragments must remain owned by their original call.
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": '{"b":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}}]},
        ]
        response_bytes = (
            ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            if stream else json.dumps({"choices": [{"message": {"content": "unchanged"}}]}).encode()
        )
        observed = []

        async def upstream(request):
            payload = json.loads(request.content)
            outgoing_kind = "chat" if mode == "adapter" else api_kind
            schema = parameters(payload["tools"], outgoing_kind)
            assert schema["properties"]["params"]["additionalProperties"] is True
            assert request.url.path == ("/v1/chat/completions" if outgoing_kind == "chat" else "/v1/responses")
            assert payload["stream"] is stream
            assert payload["tool_choice"] == "auto"
            assert payload["parallel_tool_calls"] is True
            observed.append(payload)
            return httpx.Response(200, content=response_bytes)

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime.internal_client = client
            runtime.internal_api_key = ""
            routed, _ = await prepare(runtime, body, decision)
            response = await _send_upstream(
                runtime, Request({"type": "http", "headers": []}), routed,
                api_kind=api_kind, decision=decision,
                identity=IdentityProfile.from_settings({"enabled": False}),
            )
            assert await response.aread() == response_bytes
            await response.aclose()
        assert len(observed) == 1
        assert parameters(body["tools"], tool_shape)["properties"]["params"]["additionalProperties"] == {}

    asyncio.run(check())


def test_tool_result_data_and_boundary_hash_are_unchanged():
    body = body_for()
    call = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-test", "type": "function",
        "function": {"name": "deferred_execute", "arguments": '{"additionalProperties":{}}'},
    }]}
    result = {"role": "tool", "tool_call_id": "call-test", "content": '{"additionalProperties":{}}'}
    body["messages"].extend([call, result])
    endpoint = endpoint_for()
    routed, _ = asyncio.run(prepare(runtime_for(endpoint), body, decision_for(endpoint)))
    assert routed["messages"] == body["messages"]
    assert message_hash(routed["messages"][-1]) == message_hash(body["messages"][-1])


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_structured_output_schema_is_out_of_scope(api_kind):
    body = body_for(api_kind)
    output_schema = {"type": "object", "additionalProperties": {}}
    if api_kind == "chat":
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "result", "schema": output_schema,
        }}
        key = "response_format"
    else:
        body["text"] = {"format": {
            "type": "json_schema", "name": "result", "schema": output_schema,
        }}
        key = "text"
    original = copy.deepcopy(body)
    endpoint = endpoint_for()
    routed, _ = asyncio.run(prepare(runtime_for(endpoint), body, decision_for(endpoint, api_kind)))
    assert routed[key] == original[key]
    assert body == original


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_history_persistence_and_next_turn_keep_original_boundaries(api_kind):
    async def check():
        endpoint = endpoint_for()
        runtime = runtime_for(endpoint)
        state = ConversationState(
            conversation_id="lineage-test", public_model=endpoint.public_model,
            endpoint_id=endpoint.id, tier_rank=1, task="general", last_seen=1,
            branch_id="branch-test",
        )
        writer = SimpleNamespace(
            save=AsyncMock(), map_history=AsyncMock(), map_lineage=AsyncMock(),
        )
        body = body_for(api_kind)
        original = copy.deepcopy(body)
        answer = {"role": "assistant", "content": "First round completed"}
        expected_history = [*extract_messages(body, api_kind), answer]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: pytest.fail("History persistence must not call an upstream")
        )) as client:
            compactor = ContextCompactor(
                runtime.token_counter, CapsuleCipher(Fernet.generate_key().decode()),
                internal_base_url="http://unused.test", internal_api_key="",
                model_id="", client=client,
            )
            first_routed, _ = await prepare(
                runtime, body, decision_for(endpoint, api_kind), conversation=state,
            )
            await persist_history(
                compactor, writer, state=state, client_id="test-client",
                body=first_routed, api_kind=api_kind, assistant_message=answer,
            )
            assert compactor.cipher.decrypt(state.encrypted_capsule) == expected_history
            assert state.boundary_hash == message_hash(answer)
            writer.map_history.assert_awaited_once_with(
                "test-client", history_identities(expected_history), "branch-test",
            )
            writer.map_lineage.assert_awaited_once_with(
                "test-client", "lineage-test", "branch-test",
            )
            next_message = {"role": "user", "content": "Next round"}
            next_body = copy.deepcopy(original)
            next_body["messages" if api_kind == "chat" else "input"] = (
                [*expected_history, next_message] if api_kind == "chat" else [next_message]
            )
            incoming = copy.deepcopy(next_body)
            restored = await apply_stored_history(
                compactor, writer, next_body, api_kind=api_kind, conversation=state,
            )
            second_routed, _ = await prepare(
                runtime, restored, decision_for(endpoint, api_kind), conversation=state,
            )
            assert extract_messages(second_routed, api_kind) == [*expected_history, next_message]
            assert second_routed["tools"] == first_routed["tools"]
            assert state.boundary_hash == message_hash(answer)
            assert state.conversation_id == "lineage-test"
            assert state.branch_id == "branch-test"
            assert next_body == incoming
            assert body == original

    asyncio.run(check())


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("next_cloud", [False, True])
def test_compaction_reselection_keeps_backend_neutral_tools(monkeypatch, api_kind, next_cloud):
    async def check():
        body = body_for(api_kind)
        original = copy.deepcopy(body)
        prompt_tokens = len(json.dumps(body, sort_keys=True))
        local = replace(endpoint_for(), safe_context_tokens=prompt_tokens + 16)
        next_endpoint = replace(
            endpoint_for("openai" if next_cloud else "llama_cpp", next_cloud),
            id="next-endpoint",
            public_model="test/next",
        )
        runtime = runtime_for(local)
        runtime.settings = SimpleNamespace(section=lambda _: {"affinity_capacity_wait_seconds": 0})
        decisions = [decision_for(local, api_kind), decision_for(next_endpoint, api_kind)]
        runtime.policy = SimpleNamespace(choose=AsyncMock(side_effect=decisions))
        runtime.draining_marker = AsyncMock(return_value=None)
        lease = SimpleNamespace(release_deployment=AsyncMock())

        async def acquire(_lease, ids, **kwargs):
            return ids[0]

        runtime.scheduler = SimpleNamespace(try_acquire_deployment_candidates=acquire)
        runtime.budget = SimpleNamespace(reserve=AsyncMock(return_value=None))
        state = ConversationState(
            conversation_id="lineage-test", public_model=local.public_model,
            endpoint_id=local.id, tier_rank=1, task="general", last_seen=1,
            branch_id="branch-test",
        )
        original_state = copy.deepcopy(state)
        capsule = object()
        compacted_messages = [{"role": "user", "content": "summary"}]

        async def compact(current, value, **kwargs):
            assert parameters(value["tools"], api_kind)["properties"]["params"]["additionalProperties"] is True
            compacted = replace_messages(value, api_kind, compacted_messages)
            return capsule, compacted, current.token_counter.count_request(compacted, api_kind)

        monkeypatch.setattr("ai_router.api._compact_body_for_target", compact)
        result = await _acquire_route_capacity(
            runtime, request_id="compaction-reselection",
            requested_model="auto", evaluation=None, prompt_tokens=prompt_tokens,
            output_reserve_tokens=16, requested_context_tokens=prompt_tokens + 16,
            modalities={"text"}, has_tools=True, required_capabilities=None,
            conversation=state, body=body, api_kind=api_kind, lease=lease,
            excluded_endpoints=set(), excluded_deployments=set(),
            capacity_attempts=0, queue_wait_ms=0,
            identity=IdentityProfile.from_settings({"enabled": False}),
            allow_compaction=True,
        )
        decision, routed, actual_capsule = result[:3]
        expected = replace_messages(original, api_kind, compacted_messages)
        neutral_tokens = runtime.token_counter.count_request(expected, api_kind)
        if not next_cloud:
            expected["tools"] = normalize_llama_tool_schemas(expected["tools"], api_kind)
        assert routed == expected
        second_choice = runtime.policy.choose.await_args_list[1].kwargs
        assert second_choice["prompt_tokens"] == neutral_tokens
        assert second_choice["requested_context_tokens"] == neutral_tokens + 16
        assert second_choice["conversation"] is None
        assert decision.prompt_tokens == runtime.token_counter.count_request(expected, api_kind)
        assert actual_capsule is capsule
        assert body == original and state == original_state
        lease.release_deployment.assert_awaited_once()

    asyncio.run(check())
