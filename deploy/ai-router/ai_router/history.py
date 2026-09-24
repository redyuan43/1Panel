from __future__ import annotations

import codecs
import copy
import hashlib
import json
from typing import Any, Protocol

from .compaction import (
    ContextCompactor,
    canonical_message_for_hash,
    extract_messages,
    message_hash,
    replace_messages,
)
from .errors import ConversationStateConflictError, HistoryMigrationRequiredError
from .phase_timing import timed_async
from .privacy_view import content_text
from .reasoning_fields import chat_reasoning, is_plain_reasoning_item
from .history_identity import _canonical_tool_arguments, _messages_hash, verified_history_identity, public_history_identity
from .types import ConversationState, Endpoint


class ConversationWriter(Protocol):
    async def save(self, state: ConversationState) -> None: ...

    async def map_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
        conversation_id: str,
    ) -> None: ...

    async def map_lineage(
        self,
        client_id: str,
        lineage_id: str,
        branch_id: str,
    ) -> None: ...


def provider_family(endpoint: Endpoint | None) -> str:
    if endpoint is None:
        return ""
    configured = str(endpoint.metadata.get("provider", "")).strip()
    if configured:
        return configured
    if endpoint.backend_type in {"ai_pool", "llama_cpp", "vllm"}:
        return endpoint.backend_type
    return endpoint.node or endpoint.backend_type


def normalize_history_for_provider(
    body: dict[str, Any],
    api_kind: str,
    endpoint: Endpoint | None = None,
) -> dict[str, Any]:
    contract = (
        endpoint.metadata.get("history_contract", {})
        if endpoint is not None
        else {}
    )
    if not isinstance(contract, dict):
        contract = {}
    accepts_reasoning_content = bool(
        contract.get("accepts_reasoning_content")
        or contract.get("requires_reasoning_content")
        or endpoint is None
        or (provider_family(endpoint) in {"deepseek", "vllm", "openai-codex"}
            and endpoint is not None
            and "history_contract" not in endpoint.metadata)
    )
    accepts_reasoning_items = bool(contract.get("accepts_reasoning_items") or (
        provider_family(endpoint) in {"deepseek", "openai-codex"} and endpoint is not None
        and "history_contract" not in endpoint.metadata
    )) and (
        endpoint is None or endpoint.capabilities.responses == "native"
    )
    if endpoint is not None and endpoint.backend_type == "halogen":
        from .halogen import prepare_history
        value = prepare_history(
            body, api_kind,
            responses_adapter=endpoint.capabilities.responses == "adapter",
        )
    else:
        value = copy.deepcopy(body)
    native_responses = endpoint is not None and endpoint.capabilities.responses == "native"
    codex_chat_history = native_responses and provider_family(endpoint) == "openai-codex"
    # Never make a candidate fit by dropping reasoning. An incompatible target
    # must be rejected before token counting, including on the first turn.
    items = value.get("messages" if api_kind == "chat" else "input", [])
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if chat_reasoning(item) and not accepts_reasoning_content:
            raise HistoryMigrationRequiredError("the target cannot preserve historical reasoning")
        accepts_plain_item = (
            native_responses and contract.get("accepts_text_reasoning_items") is True
            and is_plain_reasoning_item(item)
        )
        if item.get("type") == "reasoning" and not (accepts_reasoning_items or accepts_plain_item):
            raise HistoryMigrationRequiredError("the target cannot preserve historical reasoning items")
        if api_kind == "chat" and (item.get("codex_reasoning_items") or item.get("codex_message_items")):
            if not codex_chat_history:
                raise HistoryMigrationRequiredError("opaque provider history requires its native protocol")
            reasoning_items = item.get("codex_reasoning_items") or []
            message_items = item.get("codex_message_items") or []
            if (item.get("role") != "assistant"
                    or not isinstance(reasoning_items, list) or not isinstance(message_items, list)
                    or any(not isinstance(entry, dict) or entry.get("type") != "reasoning"
                           or not entry.get("encrypted_content") for entry in reasoning_items)
                    or any(not isinstance(entry, dict) or entry.get("type") != "message"
                           or entry.get("role") != "assistant" for entry in message_items)):
                raise HistoryMigrationRequiredError("invalid opaque provider history")
    if contract.get("preserve_thinking"):
        history_items = value.get("messages" if api_kind == "chat" else "input", [])
        if isinstance(history_items, dict):
            history_items = [history_items]
        if isinstance(history_items, list) and any(
            isinstance(item, dict) and chat_reasoning(item) for item in history_items
        ):
            from .errors import HistoryMigrationRequiredError as HistoryPreservationError
            kwargs = value.get("chat_template_kwargs", {})
            if (not isinstance(kwargs, dict) or kwargs.get("preserve_thinking") is False
                    or value.get("preserve_thinking") is False):
                raise HistoryPreservationError("historical reasoning must be preserved")
            value["chat_template_kwargs"] = {**kwargs, "preserve_thinking": True}
    if api_kind == "chat":
        messages = value.get("messages")
        if isinstance(messages, list):
            value["messages"] = [
                normalized
                for item in messages
                if isinstance(item, dict)
                if (
                    normalized := _normalize_chat_item(
                        item,
                        accepts_reasoning_content=accepts_reasoning_content,
                        accepts_codex_history=codex_chat_history,
                    )
                ) is not None
            ]
        return value

    items = value.get("input")
    if isinstance(items, list):
        value["input"] = [
            normalized
            for item in items
            if isinstance(item, dict)
            if (
                normalized := _normalize_responses_item(
                    item,
                    accepts_reasoning_content=accepts_reasoning_content,
                    accepts_reasoning_items=accepts_reasoning_items,
                    native=native_responses,
                )
            )
            is not None
        ]
    return value


def history_contract_violations(
    endpoint: Endpoint,
    body: dict[str, Any],
    api_kind: str,
) -> list[str]:
    contract = endpoint.metadata.get("history_contract")
    if not isinstance(contract, dict) or not contract.get(
        "requires_reasoning_content"
    ):
        return []
    violations: list[str] = []
    if api_kind == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list):
            return violations
        for item in messages:
            if (
                isinstance(item, dict)
                and item.get("role") == "assistant"
                and (
                    bool(item.get("tool_calls"))
                    or bool(item.get("codex_reasoning_items"))
                    or bool(item.get("codex_message_items"))
                )
                and not isinstance(item.get("reasoning_content"), str)
            ):
                violations.append("missing_reasoning_content")
        return list(dict.fromkeys(violations))

    items = body.get("input")
    if not isinstance(items, list):
        return violations
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call"
            and not isinstance(item.get("reasoning_content"), str)
        ):
            violations.append("missing_reasoning_content")
    return list(dict.fromkeys(violations))


def _normalize_chat_item(
    item: dict[str, Any],
    *,
    accepts_reasoning_content: bool = False,
    accepts_codex_history: bool = False,
) -> dict[str, Any] | None:
    role = str(item.get("role", "")).lower()
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        return None
    allowed = {
        "role",
        "content",
        "name",
    }
    if role == "assistant":
        allowed.update({"tool_calls", "refusal", "audio"})
        if accepts_codex_history:
            allowed.update({"codex_reasoning_items", "codex_message_items"})
        if accepts_reasoning_content:
            allowed.add("reasoning_content")
    elif role == "tool":
        allowed.add("tool_call_id")
    result = {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key in allowed
    }
    if role == "assistant" and accepts_reasoning_content:
        reasoning = chat_reasoning(item)
        if reasoning is not None:
            result["reasoning_content"] = reasoning
    if isinstance(result.get("tool_calls"), list):
        result["tool_calls"] = [
            normalized
            for value in result["tool_calls"]
            if isinstance(value, dict)
            if (
                normalized := _normalize_chat_tool_call(value)
            )
            is not None
        ]
    return result


def _normalize_chat_tool_call(
    value: dict[str, Any],
) -> dict[str, Any] | None:
    function = value.get("function")
    if not isinstance(function, dict):
        return None
    result = {
        "id": str(value.get("id", "")),
        "type": "function",
        "function": {
            key: copy.deepcopy(function[key])
            for key in ("name", "arguments")
            if key in function
        },
    }
    if "arguments" in result["function"]:
        result["function"]["arguments"] = _canonical_tool_arguments(
            result["function"]["arguments"]
        )
    return result


def _normalize_responses_item(
    item: dict[str, Any],
    *,
    accepts_reasoning_content: bool = False,
    accepts_reasoning_items: bool = False,
    native: bool = False,
) -> dict[str, Any] | None:
    item_type = str(item.get("type", ""))
    if native:
        # Native Responses items include provider tools and item references.
        # Preserve their full payload; the target validates its own schema.
        result = copy.deepcopy(item)
        if accepts_reasoning_content and chat_reasoning(item) is not None:
            result["reasoning_content"] = chat_reasoning(item)
            result.pop("reasoning", None)
        return result
    if item_type == "reasoning":
        if not accepts_reasoning_items:
            return None
        allowed = {
            "type",
            "id",
            "encrypted_content",
            "summary",
            "content",
            "status",
        }
    elif item_type == "function_call":
        allowed = {
            "type",
            "call_id",
            "name",
            "arguments",
            "status",
        }
        if accepts_reasoning_content:
            allowed.add("reasoning_content")
    elif item_type == "function_call_output":
        allowed = {
            "type",
            "call_id",
            "output",
            "status",
        }
    elif item_type == "message" or "role" in item:
        allowed = {
            "type",
            "role",
            "content",
            "status",
            "name",
        }
        if accepts_reasoning_content:
            allowed.add("reasoning_content")
    else:
        raise HistoryMigrationRequiredError("the target cannot preserve this Responses history item")
    result = {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key in allowed
    }
    if accepts_reasoning_content and (
        item_type == "function_call" or item.get("role") == "assistant"
    ):
        reasoning = chat_reasoning(item)
        if reasoning is not None:
            result["reasoning_content"] = reasoning
    if item_type == "function_call" and "arguments" in result:
        result["arguments"] = _canonical_tool_arguments(
            result["arguments"]
        )
    return result


def history_identities(
    messages: list[dict[str, Any]],
) -> tuple[str, ...]:
    if not messages:
        return ()
    canonical = _canonical_history_items(messages)
    if not canonical:
        return ()
    semantic = _semantic_history_items(canonical)
    return (
        verified_history_identity(messages),
        f"v4-tooltxn-{_messages_hash(semantic)}",
        f"v3-full-{_messages_hash(canonical)}",
    )


def history_lookup_identities(
    messages: list[dict[str, Any]],
) -> tuple[str, ...]:
    values: list[str] = []
    for end in range(len(messages) - 1, 0, -1):
        values.extend(history_identities(messages[:end]))
    return tuple(dict.fromkeys(values))


def _canonical_history_items(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        if "role" in item:
            normalized = _normalize_chat_item(
                item,
                accepts_reasoning_content=True,
            )
        else:
            normalized = _normalize_responses_item(
                item,
                accepts_reasoning_content=True,
                accepts_reasoning_items=True,
            )
        if normalized is None:
            continue
        normalized = canonical_message_for_hash(normalized)
        role = str(normalized.get("role", "")).lower()
        if role == "assistant" and normalized.get("tool_calls"):
            content = normalized.get("content")
            if content is None or content == "":
                normalized["content"] = None
        result.append(normalized)
    return result


def _semantic_history_items(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        transaction = _closed_tool_transaction(messages, index)
        if transaction is None:
            result.append(copy.deepcopy(messages[index]))
            index += 1
            continue
        item, index = transaction
        result.append(item)
    return result


def _closed_tool_transaction(
    messages: list[dict[str, Any]],
    start: int,
) -> tuple[dict[str, Any], int] | None:
    first = messages[start]
    protocol = _tool_call_protocol(first)
    if protocol is None:
        return None

    calls: dict[str, dict[str, Any]] = {}
    outputs: dict[str, dict[str, Any]] = {}
    index = start
    while index < len(messages):
        item = messages[index]
        item_protocol = _tool_call_protocol(item)
        if item_protocol == protocol:
            for call_id, call in _tool_calls(item, protocol):
                if not call_id or call_id in calls:
                    return None
                calls[call_id] = call
            index += 1
            continue

        output = _tool_output(item, protocol)
        if output is not None:
            call_id, value = output
            if (
                not call_id
                or call_id not in calls
                or call_id in outputs
            ):
                return None
            outputs[call_id] = value
            index += 1
            continue
        break

    if not calls or set(calls) != set(outputs):
        return None
    return (
        {
            "type": "tool_transaction",
            "protocol": protocol,
            "calls": [
                {
                    "call_id": call_id,
                    "call": calls[call_id],
                    "output": outputs[call_id],
                }
                for call_id in sorted(calls)
            ],
        },
        index,
    )


def _tool_call_protocol(item: dict[str, Any]) -> str | None:
    if (
        str(item.get("role", "")).lower() == "assistant"
        and isinstance(item.get("tool_calls"), list)
        and item["tool_calls"]
        and (
            item.get("content") is None
            or item.get("content") == ""
        )
    ):
        return "chat"
    if item.get("type") == "function_call":
        return "responses"
    return None


def _tool_calls(
    item: dict[str, Any],
    protocol: str,
) -> list[tuple[str, dict[str, Any]]]:
    if protocol == "responses":
        return [
            (
                str(item.get("call_id", "")),
                {
                    key: copy.deepcopy(item[key])
                    for key in ("name", "arguments", "status")
                    if key in item
                },
            )
        ]

    result: list[tuple[str, dict[str, Any]]] = []
    for call in item.get("tool_calls", []):
        if not isinstance(call, dict):
            continue
        result.append(
            (
                str(call.get("id", "")),
                {
                    key: copy.deepcopy(call[key])
                    for key in ("type", "function")
                    if key in call
                },
            )
        )
    return result


def _tool_output(
    item: dict[str, Any],
    protocol: str,
) -> tuple[str, dict[str, Any]] | None:
    if protocol == "chat":
        if str(item.get("role", "")).lower() != "tool":
            return None
        return (
            str(item.get("tool_call_id", "")),
            {
                key: copy.deepcopy(item[key])
                for key in ("content", "name")
                if key in item
            },
        )
    if item.get("type") != "function_call_output":
        return None
    return (
        str(item.get("call_id", "")),
        {
            key: copy.deepcopy(item[key])
            for key in ("output", "status")
            if key in item
        },
    )


async def apply_stored_history(
    compactor: ContextCompactor,
    conversations: ConversationWriter,
    body: dict[str, Any],
    *,
    api_kind: str,
    conversation: ConversationState | None,
) -> dict[str, Any]:
    if (
        not conversation
        or not conversation.encrypted_capsule
        or not conversation.boundary_hash
    ):
        return body
    try:
        application = compactor.apply_existing(
            body,
            api_kind=api_kind,
            encrypted_messages=conversation.encrypted_capsule,
            boundary_hash=conversation.boundary_hash,
        )
        if application.upgraded_boundary_hash:
            conversation.boundary_hash = application.upgraded_boundary_hash
            await conversations.save(conversation)
        return application.body
    except ConversationStateConflictError:
        if api_kind != "responses":
            raise
        base_messages = compactor.cipher.decrypt(
            conversation.encrypted_capsule
        )
        if not isinstance(base_messages, list):
            raise
        incoming = extract_messages(body, api_kind)
        result = replace_messages(body, api_kind, [*base_messages, *incoming])
        result.pop("previous_response_id", None)
        return result


def strip_identity_intercept_history(
    body: dict[str, Any],
    api_kind: str,
    identity_response: str,
) -> tuple[dict[str, Any], bool]:
    """Remove the immediately preceding Router-only identity exchange.

    The pair is recognized narrowly: current user message, preceded by the
    exact configured identity response, preceded by one user message. Older
    normal history and the current request remain model-visible.
    """
    messages = extract_messages(body, api_kind)
    if len(messages) < 3 or messages[-1].get("role") != "user":
        return body, False
    assistant = messages[-2]
    prior_user = messages[-3]
    if (
        assistant.get("role") != "assistant"
        or prior_user.get("role") != "user"
        or content_text(assistant.get("content")).strip()
        != identity_response.strip()
    ):
        return body, False
    return replace_messages(body, api_kind, [*messages[:-3], messages[-1]]), True


@timed_async("history_persist")
async def persist_history(
    compactor: ContextCompactor,
    conversations: ConversationWriter,
    *,
    state: ConversationState | None,
    client_id: str,
    body: dict[str, Any],
    api_kind: str,
    response_payload: bytes | None = None,
    assistant_message: dict[str, Any] | None = None,
    assistant_items: list[dict[str, Any]] | None = None,
    persist_messages: bool = True,
    public_body: dict[str, Any] | None = None,
    public_assistant_items: list[dict[str, Any]] | None = None,
) -> None:
    if state is None:
        return
    messages = extract_messages(body, api_kind)
    response_items = assistant_items
    if response_items is None and assistant_message is not None:
        response_items = [assistant_message]
    if response_items is None:
        response_items = assistant_items_from_response(
            response_payload,
            api_kind,
        )
    messages.extend(response_items)
    if not messages:
        return
    if persist_messages:
        state.encrypted_capsule = compactor.cipher.encrypt(messages)
        state.boundary_hash = message_hash(messages[-1])
    await conversations.save(state)
    if public_body is not None and public_assistant_items:
        # Publish before the success response ends; archive queue latency must
        # not make a completed exchange look like a new conversation.
        await conversations.map_history(client_id, (public_history_identity(
            extract_messages(public_body, api_kind), public_assistant_items,
            client_id=client_id, protocol=api_kind,
        ),), state.branch_id or state.conversation_id)
    await conversations.map_history(
        client_id,
        history_identities(messages),
        state.branch_id or state.conversation_id,
    )
    await conversations.map_lineage(
        client_id,
        state.conversation_id,
        state.branch_id or state.conversation_id,
    )


def assistant_from_response(
    payload: bytes | None,
    api_kind: str,
) -> dict[str, Any] | None:
    items = assistant_items_from_response(payload, api_kind)
    return items[0] if items else None


def assistant_items_from_response(
    payload: bytes | None,
    api_kind: str,
) -> list[dict[str, Any]]:
    if not payload:
        return []
    try:
        value = json.loads(payload)
    except Exception:
        return []
    if api_kind == "chat":
        choices = value.get("choices", [])
        if not isinstance(choices, list) or not choices:
            return []
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return []
        result = copy.deepcopy(message)
        reasoning = chat_reasoning(result)
        if reasoning is not None:
            result["reasoning_content"] = reasoning
            if isinstance(result.get("reasoning"), str):
                result.pop("reasoning")
        result["role"] = "assistant"
        return [result]
    output = value.get("output", [])
    if isinstance(output, list):
        items = [
            copy.deepcopy(item)
            for item in output
            if isinstance(item, dict)
            and (
                (
                    item.get("type") == "message"
                    and item.get("role") == "assistant"
                )
                or item.get("type") == "function_call"
                or item.get("type") == "reasoning"
            )
        ]
        if items:
            return items
    if isinstance(value.get("output_text"), str):
        return [
            {
                "type": "message",
                "role": "assistant",
                "content": value["output_text"],
            }
        ]
    return []


def assistant_output_observation(
    items: list[dict[str, Any]],
    *,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """Return privacy-safe counters for effective assistant output."""
    content_chars = 0
    refusal_chars = 0
    reasoning_chars = 0
    tool_call_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "function_call":
            if str(item.get("name", "")).strip():
                tool_call_count += 1
            continue
        if item_type == "reasoning":
            reasoning_chars += _text_chars(
                item.get("summary", item.get("content", item.get("text")))
            )
            continue
        content_chars += _text_chars(item.get("content"))
        refusal_chars += _text_chars(item.get("refusal"))
        reasoning_chars += _text_chars(item.get("reasoning_content"))
        reasoning_chars += _text_chars(item.get("reasoning"))
        reasoning_chars += _text_chars(item.get("codex_reasoning_items"))
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict) and str(
                    function.get("name", "")
                ).strip():
                    tool_call_count += 1
    effective = any(
        value > 0
        for value in (
            content_chars,
            refusal_chars,
            reasoning_chars,
            tool_call_count,
        )
    )
    return {
        "effective": effective,
        "content_chars": content_chars,
        "refusal_chars": refusal_chars,
        "reasoning_chars": reasoning_chars,
        "tool_call_count": tool_call_count,
        "finish_reason": finish_reason,
    }


def response_output_observation(
    payload: bytes | None,
    api_kind: str,
) -> dict[str, Any]:
    finish_reason: str | None = None
    if payload:
        try:
            value = json.loads(payload)
            if api_kind == "chat":
                choices = value.get("choices")
                if isinstance(choices, list) and choices:
                    raw_reason = choices[0].get("finish_reason")
                    if raw_reason is not None:
                        finish_reason = str(raw_reason)
            elif isinstance(value, dict):
                status = value.get("status")
                if status is not None:
                    finish_reason = str(status)
        except (TypeError, ValueError):
            pass
    return assistant_output_observation(
        assistant_items_from_response(payload, api_kind),
        finish_reason=finish_reason,
    )


def _text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        return sum(_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(
            _text_chars(value.get(key))
            for key in (
                "text",
                "content",
                "output_text",
                "refusal",
                "summary",
            )
            if value.get(key) is not None
        )
    return 0


class SSEAccumulator:
    def __init__(self, api_kind: str = "chat") -> None:
        self.api_kind = api_kind
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        self._buffer = ""
        self._text: list[str] = []
        self._chat_refusal: list[str] = []
        self._chat_reasoning_content: list[str] = []
        self._chat_tool_calls: dict[int, dict[str, Any]] = {}
        self._chat_reasoning_items: list[dict[str, Any]] = []
        self._chat_message_items: list[dict[str, Any]] = []
        self._response_items: dict[str, dict[str, Any]] = {}
        self._completed_output: list[dict[str, Any]] | None = None
        self.response_id: str | None = None
        self.usage: dict[str, Any] | None = None
        self.terminal = False
        self.usage_incomplete = False
        self.completed = False
        self.finish_reason: str | None = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk)
        self._consume_lines(final=False)

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        self._consume_lines(final=True)

    def assistant_items(self) -> list[dict[str, Any]]:
        if self.api_kind == "chat":
            if not any(
                (
                    self._text,
                    self._chat_refusal,
                    self._chat_reasoning_content,
                    self._chat_tool_calls,
                    self._chat_reasoning_items,
                    self._chat_message_items,
                )
            ):
                return []
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(self._text),
            }
            if self._chat_tool_calls:
                message["tool_calls"] = [
                    copy.deepcopy(self._chat_tool_calls[index])
                    for index in sorted(self._chat_tool_calls)
                ]
            if self._chat_refusal:
                message["refusal"] = "".join(self._chat_refusal)
            if self._chat_reasoning_items:
                message["codex_reasoning_items"] = copy.deepcopy(
                    self._chat_reasoning_items
                )
            if self._chat_message_items:
                message["codex_message_items"] = copy.deepcopy(
                    self._chat_message_items
                )
            if self._chat_reasoning_content:
                message["reasoning_content"] = "".join(
                    self._chat_reasoning_content
                )
            return [message]

        source = (
            self._completed_output
            if self._completed_output is not None
            else list(self._response_items.values())
        )
        result = [
            copy.deepcopy(item)
            for item in source
            if (
                (
                    item.get("type") == "message"
                    and item.get("role") == "assistant"
                )
                or item.get("type") == "function_call"
                or item.get("type") == "reasoning"
            )
        ]
        if self._text and not any(
            item.get("type") == "message" for item in result
        ):
            result.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "".join(self._text),
                }
            )
        return result

    def output_observation(self) -> dict[str, Any]:
        observation = assistant_output_observation(
            self.assistant_items(),
            finish_reason=self.finish_reason,
        )
        if isinstance(self.usage, dict):
            completion_tokens = self.usage.get(
                "completion_tokens",
                self.usage.get("output_tokens"),
            )
            if isinstance(completion_tokens, int) and not isinstance(
                completion_tokens,
                bool,
            ):
                observation["completion_tokens"] = completion_tokens
        observation["usage_complete"] = (
            self.usage is not None and not self.usage_incomplete
        )
        return observation

    def has_effective_output(self) -> bool:
        return bool(self.output_observation()["effective"])

    def assistant_message(self) -> dict[str, Any] | None:
        items = self.assistant_items()
        return items[0] if items else None

    def _consume_lines(self, *, final: bool) -> None:
        lines = self._buffer.splitlines(keepends=True)
        self._buffer = ""
        for line in lines:
            if not final and not line.endswith(("\n", "\r")):
                self._buffer = line
                continue
            self._consume_line(line.strip())

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        value = line[5:].strip()
        if not value:
            return
        if value == "[DONE]":
            self.terminal = True
            self.completed = True
            return
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return
        response_id = payload.get("id")
        response = payload.get("response")
        if not response_id and isinstance(response, dict):
            response_id = response.get("id")
        if response_id and not self.response_id:
            self.response_id = str(response_id)
        usage = payload.get("usage")
        if not isinstance(usage, dict) and isinstance(response, dict):
            usage = response.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
        if (payload.get("error") or payload.get("type") in {"response.incomplete", "response.failed", "error"}
                or (isinstance(response, dict) and response.get("status") in {"incomplete", "failed", "cancelled"})):
            self.usage_incomplete = True
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            if choices[0].get("finish_reason") is not None:
                self.terminal = True
                self.finish_reason = str(choices[0]["finish_reason"])
            delta = choices[0].get("delta", {})
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str):
                    self._text.append(delta["content"])
                if isinstance(delta.get("refusal"), str):
                    self._chat_refusal.append(delta["refusal"])
                reasoning = chat_reasoning(delta)
                if reasoning is not None:
                    self._chat_reasoning_content.append(reasoning)
                self._consume_chat_tool_calls(delta.get("tool_calls"))
                if isinstance(
                    delta.get("codex_reasoning_items"),
                    list,
                ):
                    self._chat_reasoning_items = [
                        copy.deepcopy(item)
                        for item in delta["codex_reasoning_items"]
                        if isinstance(item, dict)
                    ]
                if isinstance(
                    delta.get("codex_message_items"),
                    list,
                ):
                    self._chat_message_items = [
                        copy.deepcopy(item)
                        for item in delta["codex_message_items"]
                        if isinstance(item, dict)
                    ]
        if (
            payload.get("type") == "response.output_text.delta"
            and isinstance(payload.get("delta"), str)
        ):
            self._text.append(payload["delta"])
        self._consume_response_event(payload)

    def _consume_chat_tool_calls(self, value: Any) -> None:
        if not isinstance(value, list):
            return
        for offset, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", offset))
            except (TypeError, ValueError):
                index = offset
            current = self._chat_tool_calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if item.get("id"):
                current["id"] = str(item["id"])
            if item.get("type"):
                current["type"] = str(item["type"])
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            target = current["function"]
            if function.get("name"):
                target["name"] += str(function["name"])
            if function.get("arguments"):
                target["arguments"] += str(function["arguments"])

    def _consume_response_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type", ""))
        if event_type in {
            "response.output_item.added",
            "response.output_item.done",
        }:
            item = payload.get("item")
            if isinstance(item, dict):
                key = self._response_item_key(payload, item)
                existing = self._response_items.get(key, {})
                self._response_items[key] = {
                    **existing,
                    **copy.deepcopy(item),
                }
            return
        if event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            key = self._response_item_key(payload, {})
            item = self._response_items.setdefault(
                key,
                {
                    "type": "function_call",
                    "id": payload.get("item_id", ""),
                    "call_id": payload.get("call_id", ""),
                    "name": payload.get("name", ""),
                    "arguments": "",
                },
            )
            if payload.get("call_id"):
                item["call_id"] = payload["call_id"]
            if payload.get("name"):
                item["name"] = payload["name"]
            if event_type.endswith(".delta"):
                item["arguments"] = (
                    str(item.get("arguments", ""))
                    + str(payload.get("delta", ""))
                )
            elif payload.get("arguments") is not None:
                item["arguments"] = payload["arguments"]
            return
        if event_type == "response.completed":
            self.terminal = True
            self.completed = True
            self.finish_reason = "completed"
            response = payload.get("response")
            if isinstance(response, dict):
                output = response.get("output")
                if isinstance(output, list):
                    self._completed_output = [
                        copy.deepcopy(item)
                        for item in output
                        if isinstance(item, dict)
                    ]

    def _response_item_key(
        self,
        payload: dict[str, Any],
        item: dict[str, Any],
    ) -> str:
        value = (
            item.get("id")
            or payload.get("item_id")
            or item.get("call_id")
            or payload.get("call_id")
        )
        if value:
            return str(value)
        return f"output-{payload.get('output_index', len(self._response_items))}"
