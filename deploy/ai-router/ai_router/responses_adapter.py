from __future__ import annotations

import json
import re
import time
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx


SSE_EVENT_SEPARATOR = re.compile(r"(?:(?:\r\n)|\r|\n){2,}")


def responses_request_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append(
            {"role": "system", "content": instructions.strip()}
        )
    messages.extend(_responses_input_to_messages(body.get("input")))

    result: dict[str, Any] = {
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for key in (
        "temperature",
        "top_p",
        "parallel_tool_calls",
        "seed",
        "stop",
        "user",
        "chat_template_kwargs",
    ):
        if key in body:
            result[key] = body[key]

    max_output_tokens = body.get("max_output_tokens")
    if max_output_tokens is not None:
        result["max_tokens"] = max_output_tokens

    tools = _responses_tools_to_chat(body.get("tools"))
    if tools:
        result["tools"] = tools
    if body.get("tool_choice") is not None:
        result["tool_choice"] = _responses_tool_choice_to_chat(
            body["tool_choice"]
        )

    text = body.get("text")
    output_format = (
        text.get("format")
        if isinstance(text, dict)
        else None
    )
    if isinstance(output_format, dict):
        format_type = str(output_format.get("type", ""))
        if format_type == "json_object":
            result["response_format"] = {"type": "json_object"}
        elif format_type == "json_schema":
            result["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    key: output_format[key]
                    for key in (
                        "name",
                        "description",
                        "schema",
                        "strict",
                    )
                    if key in output_format
                },
            }

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        result["reasoning_effort"] = reasoning["effort"]
    return result


def chat_response_to_responses(
    payload: bytes,
    *,
    model: str,
) -> bytes:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("chat completion response must be an object")
    response = _chat_value_to_response(value, model=model)
    return json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


async def chat_stream_to_responses(
    upstream: httpx.Response,
    *,
    model: str,
    usage_observer=None,
) -> AsyncIterator[bytes]:
    response_id = f"resp_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    created_at = int(time.time())
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    started = False
    finish_reason: str | None = None

    def start_events() -> list[bytes]:
        response = _response_shell(
            response_id=response_id,
            model=model,
            created_at=created_at,
            status="in_progress",
        )
        message = _message_item(message_id, "", status="in_progress")
        return [
            _sse_event(
                "response.created",
                {"type": "response.created", "response": response},
            ),
            _sse_event(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": message,
                },
            ),
            _sse_event(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": "",
                        "annotations": [],
                    },
                },
            ),
        ]

    saw_done = False
    async for block in _iter_sse_blocks(upstream):
        raw = _sse_data(block)
        if raw == "[DONE]":
            saw_done = True
            continue
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and (event.get("error") or event.get("type") == "error"):
            raise httpx.RemoteProtocolError("Chat upstream returned a stream error")
        if not started:
            started = True
            for item in start_events():
                yield item
        current_usage = event.get("usage")
        if isinstance(current_usage, dict):
            usage = current_usage
            if usage_observer is not None:
                usage_observer(current_usage)
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        current_finish_reason = choice.get("finish_reason")
        if current_finish_reason is not None:
            finish_reason = str(current_finish_reason)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning_delta = delta.get("reasoning_content")
        if isinstance(reasoning_delta, str) and reasoning_delta:
            reasoning_parts.append(reasoning_delta)
        content_delta = delta.get("content")
        if isinstance(content_delta, str) and content_delta:
            text_parts.append(content_delta)
            yield _sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": content_delta,
                },
            )
        for offset, call in enumerate(delta.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            try:
                index = int(call.get("index", offset))
            except (TypeError, ValueError):
                index = offset
            current = tool_calls.setdefault(
                index,
                {
                    "id": f"fc_{uuid4().hex}",
                    "call_id": "",
                    "name": "",
                    "arguments": "",
                    "added": False,
                },
            )
            if call.get("id"):
                current["call_id"] = str(call["id"])
            function = call.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    current["name"] += str(function["name"])
                arguments = str(function.get("arguments") or "")
            else:
                arguments = ""
            if not current["added"]:
                current["added"] = True
                yield _sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": index + 1,
                        "item": _function_call_item(
                            current,
                            status="in_progress",
                        ),
                    },
                )
            if arguments:
                current["arguments"] += arguments
                yield _sse_event(
                    "response.function_call_arguments.delta",
                    {
                        "type": (
                            "response.function_call_arguments.delta"
                        ),
                        "item_id": current["id"],
                        "output_index": index + 1,
                        "call_id": current["call_id"],
                        "name": current["name"],
                        "delta": arguments,
                    },
                )

    if not saw_done and finish_reason is None:
        raise httpx.RemoteProtocolError("Chat stream ended without a terminal marker")
    if not started:
        for item in start_events():
            yield item
    status, incomplete_reason = _response_completion(finish_reason)
    text = "".join(text_parts)
    message = _message_item(message_id, text, status=status)
    yield _sse_event(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
    )
    yield _sse_event(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": message["content"][0],
        },
    )
    yield _sse_event(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": message,
        },
    )
    output: list[dict[str, Any]] = [message]
    for index in sorted(tool_calls):
        item = _function_call_item(
            tool_calls[index],
            status=status,
        )
        output.append(item)
        yield _sse_event(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": index + 1,
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": item["arguments"],
            },
        )
        yield _sse_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": index + 1,
                "item": item,
            },
        )
    response = _response_shell(
        response_id=response_id,
        model=model,
        created_at=created_at,
        status=status,
    )
    response["output"] = output
    response["output_text"] = text
    response["usage"] = _chat_usage_to_responses(usage)
    if reasoning_parts:
        response["reasoning"] = {"summary": "".join(reasoning_parts)}
    if incomplete_reason:
        response["incomplete_details"] = {"reason": incomplete_reason}
    terminal_event = (
        "response.incomplete"
        if status == "incomplete"
        else "response.completed"
    )
    yield _sse_event(
        terminal_event,
        {"type": terminal_event, "response": response},
    )
    yield b"data: [DONE]\n\n"


def _responses_input_to_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", ""))
        if item_type == "function_call":
            pending_calls.append(
                {
                    "id": str(
                        item.get("call_id")
                        or item.get("id")
                        or f"call_{uuid4().hex}"
                    ),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name", "")),
                        "arguments": _arguments(item.get("arguments")),
                    },
                }
            )
            continue
        if pending_calls:
            result.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_calls,
                }
            )
            pending_calls = []
        if item_type == "function_call_output":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(
                        item.get("call_id") or item.get("id") or ""
                    ),
                    "content": _text(item.get("output")),
                }
            )
            continue
        role = str(item.get("role") or "user")
        result.append(
            {
                "role": role,
                "content": _responses_content_to_chat(
                    item.get("content")
                ),
            }
        )
    if pending_calls:
        result.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": pending_calls,
            }
        )
    return result


def _responses_content_to_chat(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return _text(value)
    result: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type", ""))
        if part_type in {"input_text", "output_text", "text"}:
            result.append(
                {"type": "text", "text": str(part.get("text", ""))}
            )
        elif part_type in {"input_image", "image_url"}:
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            result.append(
                {
                    "type": "image_url",
                    "image_url": {"url": str(image_url or "")},
                }
            )
    return result


def _responses_tools_to_chat(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        function = item.get("function", item)
        if not isinstance(function, dict) or not function.get("name"):
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    key: function[key]
                    for key in (
                        "name",
                        "description",
                        "parameters",
                        "strict",
                    )
                    if key in function
                },
            }
        )
    return result


def _responses_tool_choice_to_chat(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("type") != "function":
        return value
    function = value.get("function")
    name = (
        function.get("name")
        if isinstance(function, dict)
        else value.get("name")
    )
    return {
        "type": "function",
        "function": {"name": str(name or "")},
    }


def _chat_value_to_response(
    value: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    choices = value.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = (
        choice.get("message")
        if isinstance(choice, dict)
        else {}
    )
    message = message if isinstance(message, dict) else {}
    response_id = _response_id(value.get("id"))
    created_at = int(value.get("created") or time.time())
    finish_reason = (
        choice.get("finish_reason")
        if isinstance(choice, dict)
        else None
    )
    status, incomplete_reason = _response_completion(finish_reason)
    text = _text(message.get("content"))
    output: list[dict[str, Any]] = [
        _message_item(f"msg_{uuid4().hex}", text, status=status)
    ]
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        output.append(
            {
                "id": f"fc_{uuid4().hex}",
                "type": "function_call",
                "status": status,
                "call_id": str(
                    call.get("id") or f"call_{uuid4().hex}"
                ),
                "name": str(function.get("name", "")),
                "arguments": _arguments(function.get("arguments")),
            }
        )
    response = _response_shell(
        response_id=response_id,
        model=model,
        created_at=created_at,
        status=status,
    )
    response["output"] = output
    response["output_text"] = text
    response["usage"] = _chat_usage_to_responses(value.get("usage"))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        response["reasoning"] = {"summary": reasoning}
    if incomplete_reason:
        response["incomplete_details"] = {"reason": incomplete_reason}
    return response


def _response_shell(
    *,
    response_id: str,
    model: str,
    created_at: int,
    status: str,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": [],
        "error": None,
        "incomplete_details": None,
    }


def _response_completion(finish_reason: Any) -> tuple[str, str | None]:
    reason = str(finish_reason or "").strip().lower()
    if reason == "length":
        return "incomplete", "max_output_tokens"
    if reason == "content_filter":
        return "incomplete", "content_filter"
    return "completed", None


async def _iter_sse_blocks(
    upstream: httpx.Response,
) -> AsyncIterator[str]:
    buffer = ""
    async for chunk in upstream.aiter_text():
        buffer += chunk
        while match := SSE_EVENT_SEPARATOR.search(buffer):
            block = buffer[: match.start()]
            buffer = buffer[match.end() :]
            yield block
    if buffer.strip():
        yield buffer


def _message_item(
    item_id: str,
    text: str,
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }


def _function_call_item(
    value: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "id": str(value.get("id") or f"fc_{uuid4().hex}"),
        "type": "function_call",
        "status": status,
        "call_id": str(
            value.get("call_id") or f"call_{uuid4().hex}"
        ),
        "name": str(value.get("name", "")),
        "arguments": _arguments(value.get("arguments")),
    }


def _chat_usage_to_responses(value: Any) -> dict[str, Any]:
    usage = value if isinstance(value, dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(
        usage.get("total_tokens", prompt_tokens + completion_tokens)
        or 0
    )
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens_details": usage.get(
            "prompt_tokens_details",
            {},
        ),
        "output_tokens_details": usage.get(
            "completion_tokens_details",
            {},
        ),
    }


def _response_id(value: Any) -> str:
    text = str(value or "")
    if text.startswith("resp_"):
        return text
    return f"resp_{text or uuid4().hex}"


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sse_data(block: str) -> str:
    values = [
        line[5:].strip()
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    return "\n".join(values)


def _sse_event(event_type: str, payload: dict[str, Any]) -> bytes:
    return (
        f"event: {event_type}\n"
        "data: "
        + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode()
