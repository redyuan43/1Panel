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
from .errors import ConversationStateConflictError
from .types import ConversationState, Endpoint


class ConversationWriter(Protocol):
    async def save(self, state: ConversationState) -> None: ...

    async def map_history(
        self,
        client_id: str,
        identities: tuple[str, ...],
        conversation_id: str,
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
) -> dict[str, Any]:
    value = copy.deepcopy(body)
    if api_kind == "chat":
        messages = value.get("messages")
        if isinstance(messages, list):
            value["messages"] = [
                normalized
                for item in messages
                if isinstance(item, dict)
                if (normalized := _normalize_chat_item(item)) is not None
            ]
        return value

    items = value.get("input")
    if isinstance(items, list):
        value["input"] = [
            normalized
            for item in items
            if isinstance(item, dict)
            if (
                normalized := _normalize_responses_item(item)
            )
            is not None
        ]
    return value


def deepseek_history_requires_migration(
    body: dict[str, Any],
    api_kind: str,
) -> bool:
    if api_kind == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("role") == "assistant"
            and (
                bool(item.get("tool_calls"))
                or bool(item.get("codex_reasoning_items"))
                or bool(item.get("codex_message_items"))
            )
            and not isinstance(item.get("reasoning_content"), str)
            for item in messages
        )

    items = body.get("input")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and (
            item.get("type") == "reasoning"
            or (
                item.get("type") == "function_call"
                and not isinstance(item.get("reasoning_content"), str)
            )
        )
        for item in items
    )


def _normalize_chat_item(
    item: dict[str, Any],
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
    elif role == "tool":
        allowed.add("tool_call_id")
    result = {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key in allowed
    }
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
) -> dict[str, Any] | None:
    item_type = str(item.get("type", ""))
    if item_type == "reasoning":
        return None
    if item_type == "function_call":
        allowed = {
            "type",
            "call_id",
            "name",
            "arguments",
            "status",
        }
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
    else:
        return None
    result = {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key in allowed
    }
    if item_type == "function_call" and "arguments" in result:
        result["arguments"] = _canonical_tool_arguments(
            result["arguments"]
        )
    return result


def _canonical_tool_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return copy.deepcopy(value)
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    return json.dumps(
        parsed,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def history_identities(
    messages: list[dict[str, Any]],
) -> tuple[str, ...]:
    if not messages:
        return ()
    canonical = _canonical_history_items(messages)
    if not canonical:
        return ()
    return (f"v3-full-{_messages_hash(canonical)}",)


def history_lookup_identities(
    messages: list[dict[str, Any]],
) -> tuple[str, ...]:
    values: list[str] = []
    for end in range(len(messages) - 1, 0, -1):
        values.extend(history_identities(messages[:end]))
    return tuple(dict.fromkeys(values))


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_history_items(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        if "role" in item:
            normalized = _normalize_chat_item(item)
        else:
            normalized = _normalize_responses_item(item)
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
    state.encrypted_capsule = compactor.cipher.encrypt(messages)
    state.boundary_hash = message_hash(messages[-1])
    await conversations.save(state)
    await conversations.map_history(
        client_id,
        history_identities(messages),
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
                or (
                    item.get("type") == "reasoning"
                    and item.get("encrypted_content")
                )
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


class SSEAccumulator:
    def __init__(self, api_kind: str = "chat") -> None:
        self.api_kind = api_kind
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        self._buffer = ""
        self._text: list[str] = []
        self._chat_tool_calls: dict[int, dict[str, Any]] = {}
        self._chat_reasoning_items: list[dict[str, Any]] = []
        self._chat_message_items: list[dict[str, Any]] = []
        self._response_items: dict[str, dict[str, Any]] = {}
        self._completed_output: list[dict[str, Any]] | None = None
        self.response_id: str | None = None
        self.usage: dict[str, Any] | None = None
        self.completed = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk)
        self._consume_lines(final=False)

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        self._consume_lines(final=True)

    def assistant_items(self) -> list[dict[str, Any]]:
        if self.api_kind == "chat":
            if not self._text and not self._chat_tool_calls:
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
            if self._chat_reasoning_items:
                message["codex_reasoning_items"] = copy.deepcopy(
                    self._chat_reasoning_items
                )
            if self._chat_message_items:
                message["codex_message_items"] = copy.deepcopy(
                    self._chat_message_items
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
                or (
                    item.get("type") == "reasoning"
                    and item.get("encrypted_content")
                )
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
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta", {})
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str):
                    self._text.append(delta["content"])
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
            self.completed = True
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
