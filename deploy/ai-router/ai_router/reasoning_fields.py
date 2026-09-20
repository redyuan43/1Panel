"""Lossless compatibility between Chat reasoning field names."""
import copy
from typing import Any


def chat_reasoning(value: dict[str, Any]) -> str | None:
    """Prefer the canonical field without duplicating providers' aliases."""
    canonical = value.get("reasoning_content")
    alias = value.get("reasoning")
    if isinstance(canonical, str) and canonical:
        return canonical
    if isinstance(alias, str):
        return alias
    return canonical if isinstance(canonical, str) else None


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
