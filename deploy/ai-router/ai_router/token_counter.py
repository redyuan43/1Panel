from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .errors import TokenizationUnavailableError


class TokenCounter(Protocol):
    def count_request(self, body: dict[str, Any], api_kind: str) -> int: ...


def output_reserve_tokens(body: dict[str, Any], api_kind: str, default_value: int = 4096) -> int:
    names = (
        ("max_output_tokens",)
        if api_kind == "responses"
        else ("max_completion_tokens", "max_tokens")
    )
    for name in names:
        value = body.get(name)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                break
    return default_value


def request_modalities(body: dict[str, Any], api_kind: str) -> set[str]:
    modalities = {"text"}
    values: list[Any] = []
    if api_kind == "chat":
        values = [item.get("content") for item in body.get("messages", []) if isinstance(item, dict)]
    else:
        values = [body.get("input")]
    for value in values:
        _collect_modalities(value, modalities)
    return modalities


def _collect_modalities(value: Any, modalities: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_modalities(item, modalities)
        return
    if not isinstance(value, dict):
        return
    item_type = str(value.get("type", ""))
    if "image" in item_type:
        modalities.add("image")
    if "audio" in item_type:
        modalities.add("audio")
    for nested in value.values():
        if isinstance(nested, (list, dict)):
            _collect_modalities(nested, modalities)


class HuggingFaceTokenCounter:
    def __init__(self, tokenizer_path: str | Path) -> None:
        self.tokenizer_path = Path(tokenizer_path)
        self._tokenizer: Any | None = None

    def _load(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        if not self.tokenizer_path.exists():
            raise TokenizationUnavailableError(
                f"tokenizer path does not exist: {self.tokenizer_path}"
            )
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise TokenizationUnavailableError("transformers is not installed") from exc
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                str(self.tokenizer_path),
                local_files_only=True,
                trust_remote_code=False,
            )
        except Exception as exc:
            raise TokenizationUnavailableError(str(exc)) from exc
        return self._tokenizer

    def count_request(self, body: dict[str, Any], api_kind: str) -> int:
        tokenizer = self._load()
        if api_kind == "chat":
            messages = body.get("messages")
        else:
            messages = _responses_to_messages(
                body.get("input"),
                instructions=body.get("instructions"),
            )
        if not isinstance(messages, list):
            raise TokenizationUnavailableError("request does not contain tokenizable messages")
        tools = body.get("tools")
        try:
            token_ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                tokenize=True,
                add_generation_prompt=True,
            )
            return len(token_ids)
        except Exception:
            rendered = json.dumps(
                {"messages": messages, "tools": tools or []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return len(tokenizer.encode(rendered, add_special_tokens=True))


def _responses_to_messages(
    value: Any,
    *,
    instructions: Any = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(value, str):
        messages.append({"role": "user", "content": value})
        return messages
    if not isinstance(value, list):
        return messages
    pending_calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            pending_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )
            continue
        if pending_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": pending_calls,
                }
            )
            pending_calls = []
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                }
            )
        elif item_type == "message":
            messages.append(
                {
                    "role": item.get("role", "user"),
                    "content": item.get("content", []),
                }
            )
        elif "role" in item:
            messages.append(
                {
                    "role": item.get("role", "user"),
                    "content": item.get("content", ""),
                }
            )
    if pending_calls:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": pending_calls,
            }
        )
    return messages


class SimpleTokenCounter:
    """Deterministic test counter; production must use the model tokenizer."""

    def count_request(self, body: dict[str, Any], api_kind: str) -> int:
        if api_kind == "chat":
            value = body.get("messages", [])
        else:
            value = body.get("input", "")
        return max(1, len(json.dumps(value, ensure_ascii=False)) // 4)
