from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .errors import TokenizationUnavailableError


DEFAULT_IMAGE_TOKEN_ESTIMATE = 1024
DEFAULT_AUDIO_TOKEN_ESTIMATE = 4096
MEDIA_PAYLOAD_KEYS = {
    "audio",
    "audio_url",
    "data",
    "file_data",
    "file_id",
    "image",
    "image_url",
    "url",
}


class TokenCounter(Protocol):
    def count_request(self, body: dict[str, Any], api_kind: str) -> int: ...

    def prefix_token_ids(
        self,
        body: dict[str, Any],
        api_kind: str,
    ) -> tuple[int, ...]: ...


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


def redact_media_payloads(value: Any) -> Any:
    sanitized, _ = _sanitize_media(
        value,
        image_token_estimate=DEFAULT_IMAGE_TOKEN_ESTIMATE,
        audio_token_estimate=DEFAULT_AUDIO_TOKEN_ESTIMATE,
    )
    return sanitized


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
    def __init__(
        self,
        tokenizer_path: str | Path,
        *,
        image_token_estimate: int = DEFAULT_IMAGE_TOKEN_ESTIMATE,
        audio_token_estimate: int = DEFAULT_AUDIO_TOKEN_ESTIMATE,
    ) -> None:
        self.tokenizer_path = Path(tokenizer_path)
        self.image_token_estimate = max(1, int(image_token_estimate))
        self.audio_token_estimate = max(1, int(audio_token_estimate))
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
        messages = _request_messages(body, api_kind)
        if not isinstance(messages, list):
            raise TokenizationUnavailableError("request does not contain tokenizable messages")
        messages, media_tokens = _sanitize_media(
            messages,
            image_token_estimate=self.image_token_estimate,
            audio_token_estimate=self.audio_token_estimate,
        )
        tools = body.get("tools")
        try:
            token_ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                tokenize=True,
                add_generation_prompt=True,
                **_chat_template_kwargs(body),
            )
            return len(token_ids) + media_tokens
        except Exception:
            rendered = json.dumps(
                {"messages": messages, "tools": tools or []},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return (
                len(tokenizer.encode(rendered, add_special_tokens=True))
                + media_tokens
            )

    def prefix_token_ids(
        self,
        body: dict[str, Any],
        api_kind: str,
    ) -> tuple[int, ...]:
        tokenizer = self._load()
        messages = _request_messages(body, api_kind)
        if (
            not isinstance(messages, list)
            or len(messages) < 2
            or not isinstance(messages[-1], dict)
            or str(messages[-1].get("role", "")).lower() != "user"
        ):
            return ()
        prefix_messages, media_tokens = _sanitize_media(
            messages[:-1],
            image_token_estimate=self.image_token_estimate,
            audio_token_estimate=self.audio_token_estimate,
        )
        if media_tokens:
            return ()
        tools = body.get("tools")
        kwargs = _chat_template_kwargs(body)
        try:
            rendered = [
                tokenizer.apply_chat_template(
                    [
                        *prefix_messages,
                        {
                            "role": "user",
                            "content": sentinel,
                        },
                    ],
                    tools=tools,
                    tokenize=True,
                    add_generation_prompt=True,
                    **kwargs,
                )
                for sentinel in ("A", "九", "🧪")
            ]
        except Exception as exc:
            raise TokenizationUnavailableError(
                f"prefix tokenization failed: {exc}"
            ) from exc
        common = 0
        for values in zip(*rendered, strict=False):
            if len(set(values)) != 1:
                break
            common += 1
        return tuple(int(token) for token in rendered[0][:common])


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

    def __init__(
        self,
        *,
        image_token_estimate: int = DEFAULT_IMAGE_TOKEN_ESTIMATE,
        audio_token_estimate: int = DEFAULT_AUDIO_TOKEN_ESTIMATE,
    ) -> None:
        self.image_token_estimate = max(1, int(image_token_estimate))
        self.audio_token_estimate = max(1, int(audio_token_estimate))

    def count_request(self, body: dict[str, Any], api_kind: str) -> int:
        if api_kind == "chat":
            value = body.get("messages", [])
        else:
            value = body.get("input", "")
        sanitized, media_tokens = _sanitize_media(
            value,
            image_token_estimate=self.image_token_estimate,
            audio_token_estimate=self.audio_token_estimate,
        )
        text_tokens = max(
            1,
            len(json.dumps(sanitized, ensure_ascii=False)) // 4,
        )
        return text_tokens + media_tokens

    def prefix_token_ids(
        self,
        body: dict[str, Any],
        api_kind: str,
    ) -> tuple[int, ...]:
        messages = _request_messages(body, api_kind)
        if (
            not isinstance(messages, list)
            or len(messages) < 2
            or not isinstance(messages[-1], dict)
            or str(messages[-1].get("role", "")).lower() != "user"
        ):
            return ()
        prefix, media_tokens = _sanitize_media(
            messages[:-1],
            image_token_estimate=self.image_token_estimate,
            audio_token_estimate=self.audio_token_estimate,
        )
        if media_tokens:
            return ()
        rendered = json.dumps(
            {
                "messages": prefix,
                "tools": body.get("tools") or [],
                "chat_template_kwargs": _chat_template_kwargs(body),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return tuple(rendered)


def _request_messages(
    body: dict[str, Any],
    api_kind: str,
) -> Any:
    if api_kind == "chat":
        return body.get("messages")
    return _responses_to_messages(
        body.get("input"),
        instructions=body.get("instructions"),
    )


def _chat_template_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    value = body.get("chat_template_kwargs")
    if not isinstance(value, dict):
        return {}
    reserved = {
        "messages",
        "tools",
        "tokenize",
        "add_generation_prompt",
    }
    return {
        str(key): item
        for key, item in value.items()
        if str(key) not in reserved
    }


def _sanitize_media(
    value: Any,
    *,
    image_token_estimate: int,
    audio_token_estimate: int,
) -> tuple[Any, int]:
    if isinstance(value, str):
        modality = _data_uri_modality(value)
        if modality == "image":
            return "<image>", image_token_estimate
        if modality == "audio":
            return "<audio>", audio_token_estimate
        return value, 0
    if isinstance(value, list):
        result = []
        total = 0
        for item in value:
            sanitized, tokens = _sanitize_media(
                item,
                image_token_estimate=image_token_estimate,
                audio_token_estimate=audio_token_estimate,
            )
            result.append(sanitized)
            total += tokens
        return result, total
    if not isinstance(value, dict):
        return value, 0

    item_type = str(value.get("type", "")).lower()
    modality = (
        "image"
        if "image" in item_type
        else "audio"
        if "audio" in item_type
        else None
    )
    if modality:
        estimate = (
            image_token_estimate
            if modality == "image"
            else audio_token_estimate
        )
        return _sanitize_media_item(value, modality), estimate

    result: dict[str, Any] = {}
    total = 0
    for key, item in value.items():
        sanitized, tokens = _sanitize_media(
            item,
            image_token_estimate=image_token_estimate,
            audio_token_estimate=audio_token_estimate,
        )
        result[key] = sanitized
        total += tokens
    return result, total


def _sanitize_media_item(
    value: dict[str, Any],
    modality: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key.lower() in MEDIA_PAYLOAD_KEYS:
            result[key] = _media_placeholder(item, modality)
        else:
            result[key] = _redact_nested_media(item, modality)
    return result


def _media_placeholder(value: Any, modality: str) -> Any:
    marker = f"<{modality}>"
    if not isinstance(value, dict):
        return marker
    return {
        key: (
            marker
            if key.lower() in MEDIA_PAYLOAD_KEYS
            else _redact_nested_media(item, modality)
        )
        for key, item in value.items()
    }


def _redact_nested_media(value: Any, modality: str) -> Any:
    marker = f"<{modality}>"
    if isinstance(value, str):
        return marker if _data_uri_modality(value) else value
    if isinstance(value, list):
        return [_redact_nested_media(item, modality) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                marker
                if key.lower() in MEDIA_PAYLOAD_KEYS
                else _redact_nested_media(item, modality)
            )
            for key, item in value.items()
        }
    return value


def _data_uri_modality(value: str) -> str | None:
    lowered = value.lstrip().lower()
    if lowered.startswith("data:image/"):
        return "image"
    if lowered.startswith("data:audio/"):
        return "audio"
    return None
