"""Filter protocol metadata without interpreting application JSON as metadata."""
from __future__ import annotations

import copy
from typing import Any


CHAT = frozenset("id object created model choices usage error".split())
RESPONSE = frozenset((
    "id object created_at completed_at status error incomplete_details model output "
    "parallel_tool_calls previous_response_id conversation max_output_tokens "
    "max_tool_calls reasoning text tool_choice tools temperature top_p truncation usage"
).split())
EVENT = frozenset((
    "type sequence_number response response_id output_index item_id content_index "
    "summary_index delta text arguments item part annotation annotation_index "
    "logprobs obfuscation error code message param"
).split()) - {"obfuscation"}
ITEM = frozenset((
    "id type status role content call_id name arguments output summary encrypted_content "
    "action results queries code container_id outputs tools error"
).split())
MESSAGE = frozenset((
    "role content refusal reasoning_content reasoning tool_calls function_call "
    "audio annotations"
).split())
USAGE = frozenset((
    "prompt_tokens completion_tokens total_tokens input_tokens output_tokens "
    "prompt_tokens_details completion_tokens_details input_tokens_details output_tokens_details"
).split())
TOKEN_DETAILS = frozenset((
    "cached_tokens audio_tokens reasoning_tokens accepted_prediction_tokens "
    "rejected_prediction_tokens"
).split())


def _pick(value: Any, fields: frozenset[str]) -> Any:
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    return {key: copy.deepcopy(item) for key, item in value.items() if key in fields}


def _usage(value: Any) -> Any:
    result = _pick(value, USAGE)
    if isinstance(result, dict):
        for key in result:
            if key.endswith("_details"):
                result[key] = _pick(result[key], TOKEN_DETAILS)
    return result


def _item(value: Any) -> Any:
    result = _pick(value, ITEM)
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        result["content"] = [
            _pick(part, frozenset("type text refusal annotations logprobs".split()))
            for part in result["content"]
        ]
    return result


def public_payload(value: Any, model: str) -> Any:
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if "choices" in value:
        result = _pick(value, CHAT)
        choices = []
        for choice in result.get("choices", []):
            choice = _pick(choice, frozenset("index message delta finish_reason logprobs".split()))
            if isinstance(choice, dict):
                for key in ("message", "delta"):
                    if isinstance(choice.get(key), dict):
                        message = _pick(choice[key], MESSAGE)
                        for call in message.get("tool_calls", []) or []:
                            # Arguments are application data, not protocol fields.
                            if isinstance(call, dict):
                                for extra in set(call) - {"index", "id", "type", "function"}:
                                    del call[extra]
                                if "function" in call:
                                    call["function"] = _pick(
                                        call["function"], frozenset({"name", "arguments"}),
                                    )
                        choice[key] = message
            choices.append(choice)
        result["choices"] = choices
    elif str(value.get("type", "")).startswith("response.") or value.get("type") == "error":
        result = _pick(value, EVENT)
        if isinstance(result.get("response"), dict):
            result["response"] = public_payload(result["response"], model)
        if "item" in result:
            result["item"] = _item(result["item"])
        if "part" in result:
            result["part"] = _pick(
                result["part"], frozenset("type text refusal annotations logprobs".split()),
            )
    else:
        result = _pick(value, RESPONSE)
        if isinstance(result.get("output"), list):
            result["output"] = [_item(item) for item in result["output"]]
    if "model" in result:
        result["model"] = model
    if "usage" in result:
        result["usage"] = _usage(result["usage"])
    if "error" in result and isinstance(result["error"], dict):
        result["error"] = _pick(result["error"], frozenset({"code", "type", "message", "param"}))
    return result


def private_history_items(public: list[dict], original: list[dict]) -> list[dict]:
    """Keep provider replay state in the encrypted capsule, never in the wire JSON."""
    result = copy.deepcopy(public)
    if len(result) != len(original):
        return result
    for visible, raw in zip(result, original):
        if visible.get("role") == raw.get("role") == "assistant":
            for key in ("codex_reasoning_items", "codex_message_items"):
                if key in raw:
                    visible[key] = copy.deepcopy(raw[key])
    return result
