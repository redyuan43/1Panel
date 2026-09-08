from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .errors import InvalidToolHistoryError
from .types import RequestCapabilities


@dataclass(frozen=True)
class NormalizedRequest:
    body: dict[str, Any]
    repairs: int
    required: RequestCapabilities


@dataclass(frozen=True)
class WorkBuddyDynamicContextMove:
    body: dict[str, Any]
    moved: bool
    moved_chars: int
    mode: str | None = None
    target_user_index: int | None = None
    stable_prefix_sha256: str | None = None
    skip_reason: str | None = None


_WORKBUDDY_CLIENT_ID = "workbuddy-qwen36-shared"
_WORKBUDDY_MODEL_ID = "siyuan/qwen36-shared"
_WORKBUDDY_DYNAMIC_START = "<workbuddy_dynamic_context>"
_WORKBUDDY_DYNAMIC_END = "</workbuddy_dynamic_context>"
_WORKBUDDY_MEMORY_HEADING = (
    "# Layer 3 \u2014 Workspace Memory (read/write)"
)
_WORKBUDDY_MEMORY_END = "</memory_system>"
_WORKBUDDY_MEMORY_PLACEHOLDER = (
    "Workspace memory details are provided in the final user "
    "dynamic context."
)
_WORKBUDDY_DYNAMIC_TOOL_DESCRIPTIONS = {
    "Agent": (
        "Delegate work to an available subagent. The current agent "
        "catalog is provided in the final user dynamic context."
    ),
    "Skill": (
        "Run an available skill. The current skill catalog is provided "
        "in the final user dynamic context."
    ),
    "ToolSearch": (
        "Search and load deferred tools. The current tool catalog is "
        "provided in the final user dynamic context."
    ),
}


def move_workbuddy_dynamic_context(
    body: dict[str, Any],
    api_kind: str,
    *,
    client_id: str,
) -> WorkBuddyDynamicContextMove:
    value = copy.deepcopy(body)
    target_user_index: int | None = None

    def result(
        moved: bool = False,
        moved_chars: int = 0,
        mode: str | None = None,
        skip_reason: str | None = None,
    ) -> WorkBuddyDynamicContextMove:
        # Any rejected transformation returns the entire original request.
        output = value if moved else copy.deepcopy(body)
        stable_hash = None
        if target_user_index is not None:
            stable_hash = hashlib.sha256(json.dumps(
                {"tools": output.get("tools"),
                 "messages": output["messages"][:target_user_index]},
                ensure_ascii=False, separators=(",", ":"),
            ).encode()).hexdigest()
        return WorkBuddyDynamicContextMove(
            output, moved, moved_chars, mode, target_user_index,
            stable_hash, skip_reason,
        )

    if (
        api_kind != "chat"
        or not (
            client_id == "workbuddy-public"
            or (client_id == _WORKBUDDY_CLIENT_ID
                and str(value.get("model", "")) == _WORKBUDDY_MODEL_ID)
        )
    ):
        return result(skip_reason="not_applicable")
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return result(skip_reason="invalid_messages")
    target_user_index = next((
        i for i in range(len(messages) - 1, -1, -1)
        if isinstance(messages[i], dict)
        and str(messages[i].get("role", "")).lower() == "user"
    ), None)
    if target_user_index is None:
        return result(skip_reason="no_user_message")
    user_message = messages[target_user_index]
    user_content = user_message.get("content")
    if not isinstance(user_content, (str, list)):
        return result(skip_reason="unsupported_user_content")

    dynamic_parts: list[str] = []
    moved_chars = 0
    move_mode: str | None = None
    tagged_messages: list[tuple[dict[str, Any], str, int, int]] = []
    raw_memory_messages: list[tuple[dict[str, Any], str, int, int]] = []
    for message in messages[:target_user_index]:
        if (
            not isinstance(message, dict)
            or str(message.get("role", "")).lower() != "system"
            or not isinstance(message.get("content"), str)
        ):
            continue
        content = message["content"]
        start = content.find(_WORKBUDDY_DYNAMIC_START)
        end = content.find(_WORKBUDDY_DYNAMIC_END)
        if start >= 0 or end >= 0:
            if (
                start < 0 or end < start
                or content.count(_WORKBUDDY_DYNAMIC_START) != 1
                or content.count(_WORKBUDDY_DYNAMIC_END) != 1
            ):
                return result(skip_reason="invalid_dynamic_markers")
            marker_end = end + len(_WORKBUDDY_DYNAMIC_END)
            if content[marker_end:].strip():
                return result(skip_reason="dynamic_marker_not_at_system_tail")
            tagged_messages.append((message, content, start, marker_end))
            continue

        heading = content.find(_WORKBUDDY_MEMORY_HEADING)
        if heading < 0:
            continue
        if content.count(_WORKBUDDY_MEMORY_HEADING) != 1:
            return result(skip_reason="ambiguous_workspace_memory")
        dynamic_start = heading + len(_WORKBUDDY_MEMORY_HEADING)
        dynamic_end = content.find(_WORKBUDDY_MEMORY_END, dynamic_start)
        if dynamic_end <= dynamic_start or content.count(_WORKBUDDY_MEMORY_END) != 1:
            return result(skip_reason="invalid_workspace_memory")
        # Our own placeholder is stable scaffolding, not new workspace data.
        if content[dynamic_start:dynamic_end].strip() == _WORKBUDDY_MEMORY_PLACEHOLDER:
            continue
        raw_memory_messages.append((message, content, dynamic_start, dynamic_end))

    if tagged_messages and raw_memory_messages:
        return result(skip_reason="mixed_dynamic_formats")
    if len(tagged_messages) > 1 or len(raw_memory_messages) > 1:
        return result(skip_reason="multiple_dynamic_blocks")

    if tagged_messages:
        system_message, system_content, start, end = tagged_messages[0]
        stable_system = system_content[:start].rstrip()
        inner_start = start + len(_WORKBUDDY_DYNAMIC_START)
        inner_end = end - len(_WORKBUDDY_DYNAMIC_END)
        dynamic_value = system_content[inner_start:inner_end].strip()
        if not stable_system or not dynamic_value:
            return result(skip_reason="empty_dynamic_or_stable_context")
        system_message["content"] = stable_system
        dynamic_parts.append(dynamic_value)
        moved_chars += len(system_content[start:end].strip())
        move_mode = "tagged"
    elif raw_memory_messages:
        system_message, system_content, start, end = raw_memory_messages[0]
        workspace_memory = system_content[start:end].strip()
        if not workspace_memory:
            return result(skip_reason="empty_workspace_memory")
        system_message["content"] = (
            system_content[:start].rstrip() + "\n\n"
            + _WORKBUDDY_MEMORY_PLACEHOLDER + "\n\n" + system_content[end:]
        )
        dynamic_parts.append(
            "<workbuddy_workspace_memory>\n" + workspace_memory
            + "\n</workbuddy_workspace_memory>"
        )
        moved_chars += len(workspace_memory)
        move_mode = "raw_workbuddy"

    tools = value.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name", ""))
            stable_description = _WORKBUDDY_DYNAMIC_TOOL_DESCRIPTIONS.get(name)
            description = function.get("description")
            if (
                stable_description is None
                or not isinstance(description, str)
                or description == stable_description
            ):
                continue
            dynamic_parts.append(
                f'<workbuddy_tool_description name="{name}">\n'
                f"{description}\n</workbuddy_tool_description>"
            )
            moved_chars += len(description)
            function["description"] = stable_description

    if not dynamic_parts:
        return result(skip_reason="no_dynamic_context")
    user_texts = [user_content] if isinstance(user_content, str) else [
        item.get("text", "") for item in user_content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    if any(_WORKBUDDY_DYNAMIC_START in text or _WORKBUDDY_DYNAMIC_END in text
           for text in user_texts):
        return result(skip_reason="existing_user_dynamic_context")
    dynamic_context = (
        _WORKBUDDY_DYNAMIC_START + "\n" + "\n\n".join(dynamic_parts)
        + "\n" + _WORKBUDDY_DYNAMIC_END
    )
    if not _prepend_workbuddy_dynamic_context(user_message, dynamic_context):
        return result(skip_reason="unsupported_user_content")
    return result(True, moved_chars, move_mode or "dynamic_tools")


def _prepend_workbuddy_dynamic_context(
    message: dict[str, Any],
    dynamic_context: str,
) -> bool:
    user_content = message.get("content")
    if isinstance(user_content, str):
        message["content"] = (
            f"{dynamic_context}\n\n{user_content}"
            if user_content
            else dynamic_context
        )
        return True
    if isinstance(user_content, list):
        message["content"] = [
            {"type": "text", "text": dynamic_context},
            *user_content,
        ]
        return True
    return False


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


def normalize_llama_tool_schemas(tools: Any, api_kind: str) -> Any:
    """Avoid llama.cpp treating an empty additional-property schema as object-only."""
    result = copy.deepcopy(tools)
    if not isinstance(result, list) or api_kind not in {"chat", "responses"}:
        return result
    pending: list[Any] = []
    for tool in result:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = (
            tool.get("function")
            if api_kind == "chat"
            else tool.get("function", tool)
        )
        if isinstance(function, dict):
            pending.append(function.get("parameters"))

    # Traverse schema positions only, never examples, defaults or arbitrary data.
    while pending:
        schema = pending.pop()
        if not isinstance(schema, dict):
            continue
        if schema.get("additionalProperties") == {}:
            schema["additionalProperties"] = True
        for key in (
            "properties", "patternProperties", "definitions", "$defs",
            "dependentSchemas", "dependencies",
        ):
            children = schema.get(key)
            if isinstance(children, dict):
                pending.extend(
                    child for child in children.values() if isinstance(child, dict)
                )
        for key in (
            "additionalProperties", "additionalItems", "contains",
            "propertyNames", "not", "if", "then", "else",
            "unevaluatedProperties", "unevaluatedItems", "contentSchema",
        ):
            child = schema.get(key)
            if isinstance(child, dict):
                pending.append(child)
        items = schema.get("items")
        if isinstance(items, dict):
            pending.append(items)
        elif isinstance(items, list):
            pending.extend(items)
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = schema.get(key)
            if isinstance(children, list):
                pending.extend(children)
    return result


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
        output_token_limit=any(
            body.get(key) is not None
            for key in (
                "max_output_tokens",
                "max_completion_tokens",
                "max_tokens",
            )
        ),
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


def stabilize_workbuddy_tools(body, api_kind, *, client_id):
    """Canonical ordering only: never retain removed tools or alter schemas."""
    if api_kind != "chat" or not (client_id == "workbuddy-public" or (client_id == _WORKBUDDY_CLIENT_ID and body.get("model") == _WORKBUDDY_MODEL_ID)):
        return body, {"status": "skipped", "reason": "not_applicable"}
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return body, {"status": "skipped", "reason": "no_tools"}
    names = [t.get("function", {}).get("name") if isinstance(t, dict)
             and isinstance(t.get("function"), dict) and t.get("type") == "function" else None for t in tools]
    if not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
        return body, {"status": "skipped", "reason": "unsupported_or_duplicate_tools"}
    ordered = sorted(tools, key=lambda t: t["function"]["name"])
    # JSON object key order is not part of function-call schemas. Arrays (enum,
    # required, oneOf, etc.) and all values retain their original order/content.
    ordered = json.loads(json.dumps(ordered, ensure_ascii=False, sort_keys=True))
    value = {**body, "tools": ordered}
    changed = json.dumps(tools, ensure_ascii=False) != json.dumps(ordered, ensure_ascii=False)
    return value, {"status": "passed", "changed": changed, "tool_count": len(tools),
                   "names_parameters_and_choice_preserved": True}
