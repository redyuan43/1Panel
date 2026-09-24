"""Halogen health and lossless plaintext history adaptation."""
import copy

from .errors import HistoryMigrationRequiredError
from .reasoning_fields import chat_reasoning
from .types import EndpointStatus


def responses_chat_payload(body):
    from .responses_adapter import responses_request_to_chat

    result = responses_request_to_chat(body)
    # Keep Halogen's native request controls when moving to its Chat wire.
    # In particular, reasoning.enabled/max_tokens must not disappear merely
    # because the generic adapter only maps reasoning.effort.
    for name in ("reasoning", "reasoning_effort", "enable_thinking",
                 "preserve_thinking", "max_thinking_tokens", "thinking",
                 "thinking_budget_tokens", "thinking_budget", "thinking_token_budget",
                 "drafter", "top_k", "min_p", "repetition_penalty",
                 "presence_penalty", "frequency_penalty", "stream_options"):
        if name in body:
            result[name] = copy.deepcopy(body[name])
    return result


def prepare_history(body, api_kind, *, responses_adapter=False):
    """Project Responses reasoning onto the Chat path verified for Halogen.

    Halogen 0.12.3 drops native Responses reasoning items. Keep the public
    protocol but send plaintext history through Chat; opaque history cannot
    be translated. Projection runs before both token counting and dispatch.
    """
    value = copy.deepcopy(body)
    key = "messages" if api_kind == "chat" else "input"
    items = value.get(key)
    if api_kind == "responses" and isinstance(items, dict):
        value[key] = items = [items]
    if not isinstance(items, list):
        return value
    if api_kind == "responses" and responses_adapter:
        converted, pending = [], []

        def flush():
            if pending:
                converted.append({"role": "assistant", "content": "",
                                  "reasoning_content": "\n".join(pending)})
                pending.clear()

        for item in items:
            if not isinstance(item, dict):
                raise HistoryMigrationRequiredError("invalid Responses history item")
            if item.get("type") == "reasoning":
                text = _plaintext_reasoning(item)
                if text:
                    pending.append(text)
                continue
            if pending and (item.get("type") == "function_call"
                            or item.get("role") == "assistant"):
                existing = chat_reasoning(item)
                # Conflicting aliases must still reach the common validator.
                if item.get("reasoning") and item.get("reasoning_content") \
                        and item["reasoning"] != item["reasoning_content"]:
                    raise HistoryMigrationRequiredError("conflicting historical reasoning fields")
                text = "\n".join(pending)
                if existing and existing != text:
                    text += "\n" + existing
                item["reasoning_content"] = text
                item.pop("reasoning", None)
                pending.clear()
            else:
                flush()
            converted.append(item)
        flush()
        value[key] = items = converted
    has_reasoning = any(isinstance(item, dict) and (
        chat_reasoning(item) or item.get("type") == "reasoning"
    ) for item in items)
    if has_reasoning:
        if api_kind == "responses" and not responses_adapter:
            raise HistoryMigrationRequiredError("the native target cannot preserve historical reasoning")
        kwargs = value.get("chat_template_kwargs", {})
        if not isinstance(kwargs, dict):
            raise HistoryMigrationRequiredError("invalid history template options")
        if value.get("preserve_thinking") is False or kwargs.get("preserve_thinking") is False:
            raise HistoryMigrationRequiredError("historical reasoning must be preserved")
        value["chat_template_kwargs"] = {**kwargs, "preserve_thinking": True}
    return value


def _plaintext_reasoning(item):
    if item.get("encrypted_content"):
        raise HistoryMigrationRequiredError("opaque provider history requires its native protocol")
    if set(item) - {"type", "id", "status", "content", "summary", "encrypted_content"}:
        raise HistoryMigrationRequiredError("unsupported reasoning history fields")
    texts = []
    for field, kind in (("content", "reasoning_text"), ("summary", "summary_text")):
        parts = item.get(field, [])
        if not isinstance(parts, list):
            raise HistoryMigrationRequiredError("invalid plaintext reasoning history")
        chunks = []
        for part in parts:
            if (not isinstance(part, dict) or part.get("type") != kind
                    or not isinstance(part.get("text"), str)
                    or set(part) - {"type", "text"}):
                raise HistoryMigrationRequiredError("unsupported plaintext reasoning part")
            chunks.append(part["text"])
        text = "".join(chunks)
        if text and text not in texts:
            texts.append(text)
    return "\n".join(texts)


def health_status(endpoint, value, checked_at):
    if not isinstance(value, dict):
        raise ValueError("invalid Halogen health response")
    version = value.get("version", {})
    if (value.get("status") != "ok" or value.get("model") != endpoint.provider_model
            or not isinstance(version, dict) or version.get("match") is not True
            or value.get("engine", {}).get("responds") is not True):
        raise ValueError("Halogen identity or engine health mismatch")
    for name in ("in_flight", "queued", "context", "slots"):
        number = value.get(name)
        if type(number) is not int or number < (1 if name in ("context", "slots") else 0):
            raise ValueError("invalid Halogen occupancy metadata")
    if type(value.get("busy")) is not bool:
        raise ValueError("missing Halogen busy state")
    idle = not value['busy'] and value['in_flight'] == 0 and value['queued'] == 0
    return EndpointStatus(
        endpoint_id=endpoint.id, healthy=True, checked_at=checked_at,
        load_headroom=1.0 if idle else 0.0, latency_score=1.0 if idle else 0.0,
        eligible_context_tokens=min(endpoint.safe_context_tokens, value['context']),
        # No stable cache generation is available; do not fabricate cache affinity.
        detail={'running':value['in_flight'], 'waiting':value['queued'],
                'slots':value['slots'], 'busy':value['busy'],
                'backend_version':version.get('api'), 'idle_only':True})
