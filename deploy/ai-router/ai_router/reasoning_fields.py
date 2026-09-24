"""Lossless compatibility between Chat reasoning field names."""
import copy
from typing import Any

from .errors import HistoryMigrationRequiredError


def chat_reasoning(value: dict[str, Any]) -> str | None:
    """Prefer the canonical field without duplicating providers' aliases."""
    canonical = value.get("reasoning_content")
    alias = value.get("reasoning")
    if (isinstance(canonical, str) and canonical and isinstance(alias, str)
            and alias and canonical != alias):
        raise HistoryMigrationRequiredError("conflicting historical reasoning fields")
    if isinstance(canonical, str) and canonical:
        return canonical
    if isinstance(alias, str):
        return alias
    return canonical if isinstance(canonical, str) else None


def canonical_reasoning_fields(message: dict[str, Any]) -> dict[str, Any]:
    """Compare aliases without changing the caller's messages or opaque fields."""
    result = copy.deepcopy(message)
    if message.get("reasoning_content") is not None and not isinstance(message["reasoning_content"], str):
        return result
    reasoning = chat_reasoning(message)
    if reasoning is not None:
        result["reasoning_content"] = reasoning
        if isinstance(result.get("reasoning"), str):
            result.pop("reasoning")
    return result


def is_plain_reasoning_item(item: dict[str, Any]) -> bool:
    """Recognize native textual reasoning without accepting opaque provider state."""
    content = item.get("content")
    return (
        item.get("type") == "reasoning"
        and set(item) <= {"id", "type", "content", "summary", "encrypted_content", "status"}
        and item.get("encrypted_content") in (None, "")
        and item.get("summary") in (None, [])
        and isinstance(content, list) and bool(content)
        and all(isinstance(part, dict) and set(part) <= {"type", "text"}
                and part.get("type") == "reasoning_text" and isinstance(part.get("text"), str)
                for part in content)
    )


def deepseek_tool_history(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Represent absent historical reasoning explicitly, never manufacture it.

    DeepSeek accepts an empty string for history produced without reasoning,
    but rejects an omitted field when continuing tool use in thinking mode.
    """
    result = copy.deepcopy(body)
    counts = {"alias_fields_preserved": 0, "absent_fields_explicit": 0}
    if not result.get("tools"):
        return result, counts
    for message in result.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        reasoning = chat_reasoning(message)
        if reasoning is not None:
            if message.get("reasoning_content") != reasoning:
                counts["alias_fields_preserved"] += 1
            message["reasoning_content"] = reasoning
        elif message.get("reasoning_content") is None:
            message["reasoning_content"] = ""
            counts["absent_fields_explicit"] += 1
        if isinstance(message.get("reasoning"), str):
            message.pop("reasoning")
    return result, counts
