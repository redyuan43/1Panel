from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fastapi import Request

from ai_router.api import (
    _candidate_history_token_evidence,
    _prepare_routed_body,
    _send_upstream,
)
from ai_router.config import Registry, validate_routing_declarations
from ai_router.endpoint_tokens import EndpointTokenCounter
from ai_router.errors import HistoryMigrationRequiredError
from ai_router.history import (
    SSEAccumulator,
    assistant_items_from_response,
    history_contract_violations,
    history_identities,
    normalize_history_for_provider,
)
from ai_router.identity import IdentityProfile
from ai_router.protocol import normalize_request
from ai_router.public_protocol import private_history_items
from ai_router.responses_adapter import (
    chat_response_to_responses,
    chat_stream_to_responses,
    responses_request_to_chat,
)
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import ConversationState, RouteDecision


ROOT = Path(__file__).resolve().parents[1]


def endpoint_with_contract(**contract):
    endpoint = Registry(ROOT / "config" / "registry.yaml").by_id(
        "cloud-deepseek-v4-flash"
    )
    assert endpoint is not None
    return replace(
        endpoint,
        metadata={
            **endpoint.metadata,
            "history_contract": contract,
        },
    )


def test_chat_history_is_converted_for_target_contract() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "private chain",
                "codex_reasoning_items": [{"encrypted_content": "cipher"}],
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"b":2,"a":1}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "result",
            },
        ]
    }
    original = copy.deepcopy(body)

    stripped = normalize_history_for_provider(
        body,
        "chat",
        endpoint_with_contract(),
    )
    assert body == original
    assert [item["role"] for item in stripped["messages"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assistant = stripped["messages"][1]
    assert "reasoning_content" not in assistant
    assert "codex_reasoning_items" not in assistant
    assert assistant["tool_calls"][0]["function"]["arguments"] == (
        '{"a":1,"b":2}'
    )
    assert stripped["messages"][2]["tool_call_id"] == "call-1"

    accepted = normalize_history_for_provider(
        body,
        "chat",
        endpoint_with_contract(accepts_reasoning_content=True),
    )
    assert accepted["messages"][1]["reasoning_content"] == "private chain"
    assert "codex_reasoning_items" not in accepted["messages"][1]


def test_responses_history_preserves_order_and_target_supported_fields() -> None:
    body = {
        "input": [
            {
                "type": "reasoning",
                "id": "rs-1",
                "encrypted_content": "cipher",
                "summary": [],
            },
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"b":2,"a":1}',
                "reasoning_content": "private chain",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "result",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
                "reasoning_content": "message chain",
            },
        ]
    }

    stripped = normalize_history_for_provider(
        body,
        "responses",
        endpoint_with_contract(),
    )
    assert [item["type"] for item in stripped["input"]] == [
        "function_call",
        "function_call_output",
        "message",
    ]
    assert all("reasoning_content" not in item for item in stripped["input"])

    accepted = normalize_history_for_provider(
        body,
        "responses",
        endpoint_with_contract(
            accepts_reasoning_content=True,
            accepts_reasoning_items=True,
        ),
    )
    assert [item["type"] for item in accepted["input"]] == [
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert accepted["input"][0]["encrypted_content"] == "cipher"
    assert accepted["input"][1]["reasoning_content"] == "private chain"
    assert accepted["input"][3]["reasoning_content"] == "message chain"


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_required_reasoning_content_accepts_empty_and_nonempty_strings(
    api_kind: str,
) -> None:
    endpoint = endpoint_with_contract(requires_reasoning_content=True)
    if api_kind == "chat":
        item = {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {}}],
        }
        body_key = "messages"
    else:
        item = {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{}",
        }
        body_key = "input"

    assert history_contract_violations(
        endpoint,
        {body_key: [item]},
        api_kind,
    ) == ["missing_reasoning_content"]
    for value in ("", "private chain"):
        with_reasoning = copy.deepcopy(item)
        with_reasoning["reasoning_content"] = value
        assert history_contract_violations(
            endpoint,
            {body_key: [with_reasoning]},
            api_kind,
        ) == []


def test_history_contract_registry_validation_is_fail_closed() -> None:
    validate_routing_declarations(
        {
            "history_contract": {
                "accepts_reasoning_content": True,
                "accepts_reasoning_items": False,
                "requires_reasoning_content": True,
            }
        }
    )
    with pytest.raises(ValueError, match="must be an object"):
        validate_routing_declarations({"history_contract": True})
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_routing_declarations(
            {"history_contract": {"unknown_field": True}}
        )
    with pytest.raises(ValueError, match="must be booleans"):
        validate_routing_declarations(
            {"history_contract": {"accepts_reasoning_content": "yes"}}
        )


def test_nonstream_and_stream_chat_history_keep_reasoning_content() -> None:
    payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "private chain",
                    }
                }
            ]
        }
    ).encode()
    assert assistant_items_from_response(payload, "chat")[0][
        "reasoning_content"
    ] == "private chain"

    accumulator = SSEAccumulator("chat")
    accumulator.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"private "}}]}\n\n'
    )
    accumulator.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"chain",'
        b'"content":"done"}}]}\n\n'
    )
    accumulator.finish()
    assert accumulator.assistant_items() == [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "private chain",
        }
    ]

    reasoning_only = SSEAccumulator("chat")
    reasoning_only.feed(
        b'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}\n\n'
    )
    reasoning_only.finish()
    assert reasoning_only.assistant_items() == [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private",
        }
    ]


def test_responses_and_history_identity_keep_internal_reasoning() -> None:
    payload = json.dumps(
        {
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                    "reasoning_content": "private chain",
                },
            ]
        }
    ).encode()
    items = assistant_items_from_response(payload, "responses")
    assert [item["type"] for item in items] == ["reasoning", "function_call"]
    assert items[0]["summary"] == []
    assert items[1]["reasoning_content"] == "private chain"

    first = [{"role": "assistant", "reasoning_content": "one"}]
    second = [{"role": "assistant", "reasoning_content": "two"}]
    assert history_identities(first) != history_identities(second)


def test_responses_adapter_preserves_reasoning_on_chat_tool_transaction() -> None:
    converted = responses_request_to_chat(
        {
            "input": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "encrypted_content": "cipher",
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                    "reasoning_content": "private chain",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "done",
                },
            ]
        }
    )
    assert converted["messages"] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
            "reasoning_content": "private chain",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    required = endpoint_with_contract(requires_reasoning_content=True)
    assert history_contract_violations(required, converted, "chat") == []


def test_responses_adapter_keeps_parallel_calls_across_reasoning_items() -> None:
    converted = responses_request_to_chat(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-a",
                    "name": "alpha",
                    "arguments": "{}",
                },
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "encrypted_content": "cipher",
                },
                {
                    "type": "function_call",
                    "call_id": "call-b",
                    "name": "beta",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "output": "a",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-b",
                    "output": "b",
                },
            ]
        }
    )
    normalized = normalize_request(converted, "chat")
    assert [item["role"] for item in normalized.body["messages"]] == [
        "assistant",
        "tool",
        "tool",
    ]
    assert [
        call["id"]
        for call in normalized.body["messages"][0]["tool_calls"]
    ] == ["call-a", "call-b"]


def test_adapter_response_keeps_private_reasoning_out_of_public_payload() -> None:
    chat_payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "reasoning_content": "private chain",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    ).encode()
    public_payload = chat_response_to_responses(
        chat_payload,
        model="test/model",
    )
    private_payload = chat_response_to_responses(
        chat_payload,
        model="test/model",
        preserve_history_fields=True,
    )
    assert b"reasoning_content" not in public_payload
    public_items = assistant_items_from_response(public_payload, "responses")
    private_items = assistant_items_from_response(private_payload, "responses")
    merged = private_history_items(public_items, private_items)
    assert "reasoning_content" not in merged[0]
    assert merged[1]["reasoning_content"] == "private chain"


def test_parallel_adapter_roundtrip_does_not_duplicate_reasoning() -> None:
    chat_payload = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "REASON_ONCE",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": "{}",
                                },
                            }
                            for call_id, name in (
                                ("call-a", "alpha"),
                                ("call-b", "beta"),
                            )
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    ).encode()
    private_payload = json.loads(
        chat_response_to_responses(
            chat_payload,
            model="test/model",
            preserve_history_fields=True,
        )
    )
    replay = responses_request_to_chat({"input": private_payload["output"]})
    reasoning = [
        item["reasoning_content"]
        for item in replay["messages"]
        if "reasoning_content" in item
    ]
    assert reasoning == ["REASON_ONCE"]


def test_adapter_preflight_checks_parallel_calls_after_chat_projection() -> None:
    async def scenario() -> None:
        registry = Registry(ROOT / "config" / "registry.yaml")
        source = registry.by_id("cloud-deepseek-v4-pro")
        base = registry.by_id("zhipu-glm-5.3-flash")
        assert source is not None
        assert base is not None
        target = replace(
            base,
            metadata={
                **base.metadata,
                "history_contract": {"requires_reasoning_content": True},
            },
        )
        runtime = SimpleNamespace(
            registry=registry.with_endpoints([source, target]),
            token_counter=SimpleTokenCounter(),
            endpoint_token_counter=None,
        )
        decision = RouteDecision(
            endpoint=target,
            requested_model="auto",
            task="general",
            prompt_tokens=10,
            output_reserve_tokens=16,
            reason="migration",
            affinity="migrated",
            score=1,
            native_or_adapter="adapter",
        )
        conversation = ConversationState(
            conversation_id="parallel-adapter-preflight",
            public_model=source.public_model,
            endpoint_id=source.id,
            tier_rank=source.tier_rank,
            task="general",
            last_seen=1,
            provider_family="deepseek",
        )
        body = {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-a",
                    "name": "alpha",
                    "arguments": "{}",
                    "reasoning_content": "REASON_ONCE",
                },
                {
                    "type": "function_call",
                    "call_id": "call-b",
                    "name": "beta",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "output": "a",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-b",
                    "output": "b",
                },
            ]
        }

        routed, capsule = await _prepare_routed_body(
            runtime,
            body,
            api_kind="responses",
            decision=decision,
            request_id="parallel-adapter-preflight",
            identity=IdentityProfile.from_settings({"enabled": False}),
            conversation=conversation,
            allow_compaction=False,
        )

        assert capsule is None
        assert decision.history_mode == "normalized"
        assert routed["input"][0]["reasoning_content"] == "REASON_ONCE"
        assert "reasoning_content" not in routed["input"][1]
        projected = responses_request_to_chat(routed)
        assert history_contract_violations(target, projected, "chat") == []
        assert len(projected["messages"][0]["tool_calls"]) == 2

    asyncio.run(scenario())


def test_adapter_stream_reports_private_reasoning_without_emitting_it() -> None:
    async def scenario() -> None:
        captured = []
        upstream = httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"reasoning_content":"private "},'
                '"finish_reason":null}]}\n\n'
                'data: {"choices":[{"delta":{"reasoning_content":"chain",'
                '"content":"done"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )
        output = b"".join(
            [
                chunk
                async for chunk in chat_stream_to_responses(
                    upstream,
                    model="test/model",
                    history_observer=lambda items: captured.extend(items),
                )
            ]
        )
        assert b"reasoning_content" not in output
        assert captured[0]["reasoning_content"] == "private chain"

    asyncio.run(scenario())


def test_final_adapter_payload_is_checked_before_network_send() -> None:
    async def scenario() -> None:
        registry = Registry(ROOT / "config" / "registry.yaml")
        base = registry.by_id("zhipu-glm-5.3-flash")
        assert base is not None
        endpoint = replace(
            base,
            metadata={
                **base.metadata,
                "history_contract": {"requires_reasoning_content": True},
            },
        )
        calls = []

        def upstream(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        runtime = SimpleNamespace(
            internal_base_url="http://router.invalid/v1",
            internal_api_key="internal-key",
            internal_client=client,
        )
        decision = RouteDecision(
            endpoint=endpoint,
            requested_model="auto",
            task="general",
            prompt_tokens=10,
            output_reserve_tokens=10,
            reason="test",
            affinity="new",
            score=1,
            native_or_adapter="adapter",
            upstream_api_base="http://upstream.invalid/v1",
        )
        body = {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "done",
                },
            ]
        }
        request = Request({"type": "http", "headers": []})
        with pytest.raises(HistoryMigrationRequiredError) as raised:
            await _send_upstream(
                runtime,
                request,
                body,
                api_kind="responses",
                decision=decision,
                identity=IdentityProfile.from_settings({"enabled": False}),
            )
        assert getattr(raised.value, "code", None) == (
            "history_migration_required"
        )
        assert calls == []
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("tokenizer_enabled", [False, True])
def test_candidate_count_uses_projected_shared_fallback(
    tokenizer_enabled: bool,
) -> None:
    async def scenario() -> None:
        registry = Registry(ROOT / "config" / "registry.yaml")
        base = registry.by_id("cloud-deepseek-v4-flash")
        source = registry.by_id("cloud-deepseek-v4-pro")
        assert base is not None
        assert source is not None
        metadata = {**base.metadata}
        if tokenizer_enabled:
            metadata["token_counting"] = {
                "enabled": True,
                "version": "test",
            }
        else:
            metadata.pop("token_counting", None)
        target = replace(base, metadata=metadata)
        target_registry = registry.with_endpoints([target])
        counter = EndpointTokenCounter()
        await counter.client.aclose()
        calls = []

        def unavailable(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(503, json={"error": "unavailable"})

        counter.client = httpx.AsyncClient(
            transport=httpx.MockTransport(unavailable)
        )
        token_counter = SimpleTokenCounter()
        runtime = SimpleNamespace(
            registry=target_registry,
            token_counter=token_counter,
            endpoint_token_counter=counter,
        )
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private " * 1000,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            ]
        }
        original_tokens = token_counter.count_request(body, "chat")
        conversation = ConversationState(
            conversation_id="candidate-count",
            public_model=source.public_model,
            endpoint_id=source.id,
            tier_rank=source.tier_rank,
            task="general",
            last_seen=1,
            provider_family="deepseek",
        )
        evidence = await _candidate_history_token_evidence(
            runtime,
            body=body,
            api_kind="chat",
            prompt_tokens=original_tokens,
            requested_model=target.public_model,
            conversation=conversation,
            identity=IdentityProfile.from_settings({"enabled": False}),
        )
        projected = normalize_history_for_provider(body, "chat", target)
        projected_tokens = token_counter.count_request(projected, "chat")
        assert evidence[target.id]["tokens"] == projected_tokens
        assert projected_tokens < original_tokens
        assert evidence[target.id]["source"] == "shared_estimate"
        assert len(calls) == int(tokenizer_enabled)
        if tokenizer_enabled:
            assert evidence[target.id]["reason"] == (
                "backend_tokenization_unavailable"
            )
        await counter.close()

    asyncio.run(scenario())
