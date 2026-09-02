from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .errors import CompactionUnavailableError, ConversationStateConflictError
from .token_counter import TokenCounter


HANDOFF_KEYS = (
    "facts",
    "user_preferences",
    "decisions",
    "open_goals",
    "tool_state",
    "key_references",
)


@dataclass
class Capsule:
    encrypted_messages: str
    boundary_hash: str
    before_tokens: int
    after_tokens: int


class CapsuleCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise ValueError("AI_ROUTER_STATE_KEY must be a valid Fernet key") from exc

    def encrypt(self, value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, value: str) -> Any:
        try:
            payload = self._fernet.decrypt(value.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise ConversationStateConflictError() from exc
        return json.loads(payload)


class ContextCompactor:
    def __init__(
        self,
        token_counter: TokenCounter,
        cipher: CapsuleCipher,
        *,
        internal_base_url: str,
        internal_api_key: str,
        model_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_counter = token_counter
        self.cipher = cipher
        self.internal_base_url = internal_base_url.rstrip("/")
        self.internal_api_key = internal_api_key
        self.model_id = model_id
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=3.0))

    async def compact(
        self,
        body: dict[str, Any],
        *,
        api_kind: str,
        target_context_tokens: int,
    ) -> Capsule:
        messages = extract_messages(body, api_kind)
        if not messages or not self.model_id:
            raise CompactionUnavailableError()

        before_tokens = self.token_counter.count_request(body, api_kind)
        system_messages = [
            item
            for item in messages
            if _item_role(item) in {"system", "developer"}
        ]
        conversation_messages = [
            item
            for item in messages
            if _item_role(item) not in {"system", "developer"}
        ]
        older, recent = self._partition_recent(
            conversation_messages,
            target_context_tokens,
            api_kind,
        )
        summary = await self._summarize(older)
        handoff_message = _handoff_message(summary, api_kind)
        compacted = [*system_messages, handoff_message, *recent]
        boundary = message_hash(recent[-1] if recent else handoff_message)
        compacted_body = replace_messages(body, api_kind, compacted)
        after_tokens = self.token_counter.count_request(compacted_body, api_kind)
        if after_tokens > int(target_context_tokens * 0.4):
            raise CompactionUnavailableError(
                "generated migration capsule exceeds 40% of the destination context"
            )
        return Capsule(
            encrypted_messages=self.cipher.encrypt(compacted),
            boundary_hash=boundary,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    def apply_existing(
        self,
        body: dict[str, Any],
        *,
        api_kind: str,
        encrypted_messages: str,
        boundary_hash: str,
    ) -> dict[str, Any]:
        base_messages = self.cipher.decrypt(encrypted_messages)
        if not isinstance(base_messages, list):
            raise ConversationStateConflictError()
        incoming = extract_messages(body, api_kind)
        boundary_index = -1
        for index, message in enumerate(incoming):
            if message_hash(message) == boundary_hash:
                boundary_index = index
        if boundary_index < 0:
            if incoming == base_messages:
                return body
            raise ConversationStateConflictError()
        next_messages = [*base_messages, *incoming[boundary_index + 1 :]]
        return replace_messages(body, api_kind, next_messages)

    def _partition_recent(
        self,
        messages: list[dict[str, Any]],
        target_context_tokens: int,
        api_kind: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if len(messages) <= 4:
            return [], messages
        budget = max(2048, int(target_context_tokens * 0.2))
        groups = _transaction_groups(messages, api_kind)
        selected_groups: list[list[dict[str, Any]]] = []
        for group in reversed(groups):
            candidate_groups = [group, *selected_groups]
            candidate = [
                item
                for current_group in candidate_groups
                for item in current_group
            ]
            count = self.token_counter.count_request(
                replace_messages({}, api_kind, candidate),
                api_kind,
            )
            selected_count = sum(len(item) for item in selected_groups)
            if selected_count >= 4 and count > budget:
                break
            selected_groups = candidate_groups
        recent = [
            item
            for group in selected_groups
            for item in group
        ]
        older_group_count = len(groups) - len(selected_groups)
        older = [
            item
            for group in groups[:older_group_count]
            for item in group
        ]
        return older, recent

    async def _summarize(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not messages:
            return {key: [] for key in HANDOFF_KEYS}
        request = {
            "model": self.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Compress the supplied conversation into JSON. Preserve only explicit facts, "
                        "preferences, decisions, open goals, tool state, and references. Never include "
                        "hidden reasoning. Return exactly one JSON object with these keys: "
                        + ", ".join(HANDOFF_KEYS)
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self.client.post(
                f"{self.internal_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.internal_api_key}"},
                json=request,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            value = json.loads(content)
        except Exception as exc:
            raise CompactionUnavailableError(str(exc)) from exc
        if not isinstance(value, dict):
            raise CompactionUnavailableError("compactor returned a non-object")
        return {
            key: value.get(key, []) if isinstance(value.get(key, []), list) else [value.get(key)]
            for key in HANDOFF_KEYS
        }


def extract_messages(body: dict[str, Any], api_kind: str) -> list[dict[str, Any]]:
    if api_kind == "chat":
        values = body.get("messages", [])
    else:
        values = body.get("input", [])
        if isinstance(values, str):
            values = [
                {
                    "type": "message",
                    "role": "user",
                    "content": values,
                }
            ]
    return [
        copy.deepcopy(item)
        for item in values if isinstance(values, list)
        if isinstance(item, dict)
    ]


def replace_messages(
    body: dict[str, Any],
    api_kind: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    result = copy.deepcopy(body)
    if api_kind == "chat":
        result["messages"] = copy.deepcopy(messages)
    else:
        result["input"] = copy.deepcopy(messages)
    return result


def message_hash(message: dict[str, Any]) -> str:
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _item_role(item: dict[str, Any]) -> str:
    return str(item.get("role", "")).lower()


def _handoff_message(
    summary: dict[str, Any],
    api_kind: str,
) -> dict[str, Any]:
    content = (
        "Conversation migration capsule. Treat these as prior conversation "
        "facts and state. Do not claim they were newly supplied by the user.\n"
        + json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    )
    if api_kind == "responses":
        return {
            "type": "message",
            "role": "system",
            "content": content,
        }
    return {"role": "system", "content": content}


def _transaction_groups(
    messages: list[dict[str, Any]],
    api_kind: str,
) -> list[list[dict[str, Any]]]:
    if api_kind == "chat":
        return _chat_transaction_groups(messages)
    return _responses_transaction_groups(messages)


def _chat_transaction_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        item = messages[index]
        calls = item.get("tool_calls")
        if _item_role(item) != "assistant" or not isinstance(calls, list):
            groups.append([item])
            index += 1
            continue
        call_ids = {
            str(call.get("id", ""))
            for call in calls
            if isinstance(call, dict) and call.get("id")
        }
        group = [item]
        index += 1
        while index < len(messages):
            candidate = messages[index]
            if (
                _item_role(candidate) != "tool"
                or str(candidate.get("tool_call_id", "")) not in call_ids
            ):
                break
            group.append(candidate)
            call_ids.discard(str(candidate.get("tool_call_id", "")))
            index += 1
            if not call_ids:
                break
        groups.append(group)
    return groups


def _responses_transaction_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        if messages[index].get("type") != "function_call":
            groups.append([messages[index]])
            index += 1
            continue
        group: list[dict[str, Any]] = []
        call_ids: set[str] = set()
        while (
            index < len(messages)
            and messages[index].get("type") == "function_call"
        ):
            item = messages[index]
            group.append(item)
            call_id = str(item.get("call_id") or item.get("id") or "")
            if call_id:
                call_ids.add(call_id)
            index += 1
        while (
            index < len(messages)
            and messages[index].get("type") == "function_call_output"
            and str(messages[index].get("call_id", "")) in call_ids
        ):
            item = messages[index]
            group.append(item)
            call_ids.discard(str(item.get("call_id", "")))
            index += 1
        groups.append(group)
    return groups
