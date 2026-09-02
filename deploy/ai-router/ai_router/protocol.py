from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .errors import InvalidToolHistoryError
from .types import RequestCapabilities


@dataclass(frozen=True)
class NormalizedRequest:
    body: dict[str, Any]
    repairs: int
    required: RequestCapabilities


def normalize_request(
    body: dict[str, Any],
    api_kind: str,
    *,
    validate_history: bool = True,
) -> NormalizedRequest:
    value = copy.deepcopy(body)
    repairs = 0
    if validate_history:
        repairs = (
            _normalize_chat_history(value)
            if api_kind == "chat"
            else _normalize_responses_history(value)
        )
    return NormalizedRequest(
        body=value,
        repairs=repairs,
        required=request_capabilities(value, api_kind),
    )


def request_capabilities(
    body: dict[str, Any],
    api_kind: str,
) -> RequestCapabilities:
    structured_output = _structured_output(body, api_kind)
    return RequestCapabilities(
        protocol=api_kind,
        tools=bool(body.get("tools")),
        parallel_tools=bool(body.get("parallel_tool_calls")),
        tool_choice=body.get("tool_choice") is not None,
        tool_choice_mode=_tool_choice_mode(body.get("tool_choice")),
        structured_output=structured_output,
        streaming=bool(body.get("stream")),
    )


def _tool_choice_mode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("type") or "function")
    return "unknown"


def _structured_output(
    body: dict[str, Any],
    api_kind: str,
) -> str | None:
    value: Any = body.get("response_format")
    if api_kind == "responses":
        text = body.get("text")
        if isinstance(text, dict) and isinstance(text.get("format"), dict):
            value = text["format"]
    if not isinstance(value, dict):
        return None
    output_type = str(value.get("type", ""))
    return output_type if output_type in {"json_object", "json_schema"} else None


def _normalize_chat_history(body: dict[str, Any]) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    pending: list[dict[str, Any]] = []
    repairs = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "")).lower()
        if role == "assistant":
            _ensure_no_pending(pending, index)
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id", "")).strip()
                if not call_id:
                    raise InvalidToolHistoryError(
                        "assistant tool call is missing id",
                        item_index=index,
                        candidate_count=0,
                        reason="missing_tool_call_id",
                    )
                if any(item["id"] == call_id for item in pending):
                    raise InvalidToolHistoryError(
                        "assistant tool call id is duplicated",
                        item_index=index,
                        candidate_count=1,
                        reason="duplicate_tool_call_id",
                    )
                pending.append(
                    {
                        "id": call_id,
                        "name": _tool_name(tool_call),
                        "matched": False,
                    }
                )
            continue
        if role == "tool":
            matched, repaired = _match_tool_result(
                message,
                pending,
                index,
                id_key="tool_call_id",
            )
            matched["matched"] = True
            repairs += int(repaired)
            continue
        if role in {"user", "system", "developer"}:
            _ensure_no_pending(pending, index)
            pending = []
    _ensure_no_pending(pending, len(messages))
    return repairs


def _normalize_responses_history(body: dict[str, Any]) -> int:
    items = body.get("input")
    if isinstance(items, str) or not isinstance(items, list):
        return 0
    pending: list[dict[str, Any]] = []
    repairs = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if not call_id:
                raise InvalidToolHistoryError(
                    "function call is missing call_id",
                    item_index=index,
                    candidate_count=0,
                    reason="missing_function_call_id",
                )
            pending.append(
                {
                    "id": call_id,
                    "name": str(item.get("name", "")).strip(),
                    "matched": False,
                }
            )
            continue
        if item_type == "function_call_output":
            matched, repaired = _match_tool_result(
                item,
                pending,
                index,
                id_key="call_id",
            )
            matched["matched"] = True
            repairs += int(repaired)
            continue
        if item_type == "message" or "role" in item:
            _ensure_no_pending(pending, index)
            pending = []
    _ensure_no_pending(pending, len(items))
    return repairs


def _match_tool_result(
    item: dict[str, Any],
    pending: list[dict[str, Any]],
    index: int,
    *,
    id_key: str,
) -> tuple[dict[str, Any], bool]:
    unmatched = [value for value in pending if not value["matched"]]
    explicit_id = str(item.get(id_key, "")).strip()
    if explicit_id:
        candidates = [value for value in unmatched if value["id"] == explicit_id]
        if len(candidates) != 1:
            raise InvalidToolHistoryError(
                "tool result references an unknown or already consumed call",
                item_index=index,
                candidate_count=len(candidates),
                reason="unknown_or_duplicate_tool_result",
            )
        return candidates[0], False

    name = str(item.get("name", "")).strip()
    candidates = (
        [value for value in unmatched if value["name"] == name]
        if name
        else unmatched
    )
    if len(candidates) != 1:
        raise InvalidToolHistoryError(
            "tool result is missing an unambiguous call id",
            item_index=index,
            candidate_count=len(candidates),
            reason="ambiguous_missing_tool_call_id",
        )
    item[id_key] = candidates[0]["id"]
    return candidates[0], True


def _ensure_no_pending(
    pending: list[dict[str, Any]],
    index: int,
) -> None:
    unmatched = [value for value in pending if not value["matched"]]
    if unmatched:
        raise InvalidToolHistoryError(
            "tool calls must be followed by matching tool results",
            item_index=index,
            candidate_count=len(unmatched),
            reason="unresolved_tool_calls",
        )


def _tool_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    return (
        str(function.get("name", "")).strip()
        if isinstance(function, dict)
        else ""
    )
