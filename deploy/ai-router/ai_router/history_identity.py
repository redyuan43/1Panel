"""Pure, versioned history evidence shared by API and archive consumers.

Changing normalization requires a new version and archive-backed reindexing.
Never use this evidence representation as model input or trust legacy indexes.
"""
import copy
import hashlib
import json
import re
from typing import Any

from .reasoning_fields import canonical_reasoning_fields

HISTORY_IDENTITY_VERSION = 7
HISTORY_IDENTITY_PREFIX = f"v{HISTORY_IDENTITY_VERSION}-history-"


def public_history_identity(messages, output, *, client_id, protocol):
    """Index the exact completed exchange visible to this authenticated client."""
    namespace = "wb-raw-v1:" if protocol == "chat" and client_id in {
        "workbuddy-public", "workbuddy-qwen36-shared"
    } else ""
    return namespace + verified_history_identity([*messages, *output])


def is_verified_history_identity(value: str) -> bool:
    """Recognize every evidence version, including retired metadata aliases."""
    return bool(re.match(r"v\d+-history-", value.removeprefix("wb-raw-v1:")))


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


def verified_history_identity(messages: list[dict[str, Any]]) -> str:
    """Routing evidence only: never use this representation as model input."""
    items = _verified_history_items(messages)
    anchor = any(
        (m.get("role") == "assistant" and (m.get("content") or m.get("tool_calls")))
        or m.get("type") in {"tool_transaction_v6", "function_call"}
        for m in items
    )
    strength = "strong" if anchor else "weak"
    return f"{HISTORY_IDENTITY_PREFIX}{strength}-{len(items)}-{_messages_hash(items)}"


def _verified_history_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Preserve all model-visible fields, including prose, images and reasoning.
    keys = {"role", "content", "name", "tool_calls", "tool_call_id", "refusal", "audio", "reasoning_content", "reasoning"}
    canonical = []
    for message in messages:
        if not isinstance(message, dict):
            canonical.append({"invalid_item": message})
            continue
        item = copy.deepcopy({k: v for k, v in message.items() if k in keys} if "role" in message else message)
        item = canonical_reasoning_fields(item)
        content = item.get("content")
        if isinstance(content, list) and content and all(
                isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
                and isinstance(part.get("text"), str) and set(part) <= {"type", "text", "annotations"}
                and part.get("annotations") in (None, []) for part in content):
            item["content"] = "".join(part["text"] for part in content)
        if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
            if item.get("content") in (None, ""):
                item["content"] = None
            for call in item["tool_calls"]:
                if isinstance(call, dict) and isinstance(call.get("function"), dict) and "arguments" in call["function"]:
                    call["function"]["arguments"] = _canonical_tool_arguments(call["function"]["arguments"])
        canonical.append(item)
    result, index = [], 0
    while index < len(canonical):
        transaction = _verified_tool_transaction(canonical, index)
        if transaction is None:
            result.append(canonical[index])
            index += 1
        else:
            item, index = transaction
            result.append(item)
    return result


def _verified_tool_transaction(messages, start):
    first = messages[start]
    if first.get("role") != "assistant" or not first.get("tool_calls"):
        return None
    head = {k: v for k, v in first.items() if k != "tool_calls"}
    calls, outputs, seen, answered = [], [], set(), set()
    index = start
    while index < len(messages):
        item = messages[index]
        if item.get("role") == "assistant" and item.get("tool_calls"):
            if index != start and (item.get("content") not in (None, "")
                    or set(item) - {"role", "content", "tool_calls"}):
                break
            if not isinstance(item["tool_calls"], list):
                return None
            for call in item["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in seen:
                    return None
                seen.add(call["id"])
                calls.append(call)
        elif item.get("role") == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in seen or call_id in answered:
                return None
            answered.add(call_id)
            outputs.append(item)
        else:
            break
        index += 1
    if not seen or seen != answered:
        return None
    # Separate ordered call/result lists allow only grouping/interleaving changes.
    # Never discard prose or sort IDs: reordered calls/results remain different.
    return {"type": "tool_transaction_v6", "head": head, "calls": calls, "outputs": outputs}, index


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
