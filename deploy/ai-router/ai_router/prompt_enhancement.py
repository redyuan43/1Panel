"""Bounded, request-local WorkBuddy prompt enhancement."""

from __future__ import annotations

import copy
import os
import re
import time
from dataclasses import dataclass
from html import escape, unescape
from typing import Any, Awaitable, Callable

import httpx

from .privacy_view import review_view
from .prompt_enhancement_templates import SYSTEM_TEMPLATE, USER_TEMPLATE


CLIENT_IDS = frozenset({"workbuddy-public", "workbuddy-qwen36-shared"})
MAX_INPUT_CHARS = 800
MAX_OUTPUT_CHARS = 800
MAX_OUTPUT_TOKENS = 1024
MAX_EXTRA_PROMPT_TOKENS = MAX_OUTPUT_CHARS * 5
TIMEOUT_SECONDS = 15.0
_QUERY = re.compile(
    r"<user_query(?:\s[^>]*)?>((?:(?!</user_query\s*>).)*)</user_query\s*>\s*$",
    re.I | re.S,
)
_URL = re.compile(r"https?://[^\s\"'<>]+")
_PATH = re.compile(r"(?<!\w)(?:/|\./)[\w./-]+")
_NUMBER = re.compile(r"(?<!\w)\d+(?:\.\d+)?(?:[%KkMm]|[万亿])?(?!\w)")
_QUOTED = re.compile(r"[\"“‘']([^\"”’'\n]+)[\"”’']")


@dataclass(frozen=True)
class PromptSource:
    text: str
    original_content: str | list[Any]
    query_span: tuple[int, int] | None = None
    text_part_indexes: tuple[int, ...] = ()
    input_is_string: bool = False


def _text_key(part: Any) -> str | None:
    if not isinstance(part, dict) or part.get("type") not in {"text", "input_text"}:
        return None
    return next((key for key in ("text", "input_text") if isinstance(part.get(key), str)), None)


def _content_source(content: Any, body: dict[str, Any], api_kind: str) -> tuple[PromptSource | None, str]:
    if isinstance(content, str):
        text = content
        indexes: tuple[int, ...] = ()
    elif isinstance(content, list):
        indexes = tuple(i for i, part in enumerate(content) if _text_key(part) is not None)
        if not indexes:
            return None, "no_text"
        if len(indexes) != 1:
            return None, "ambiguous_text_parts"
        text = str(content[indexes[0]][_text_key(content[indexes[0]])])
    else:
        return None, "no_text"
    if not text.strip():
        return None, "empty_text"
    systems = body.get("messages") if api_kind == "chat" else body.get("input")
    if isinstance(systems, list) and any(
        isinstance(item, dict)
        and item.get("role") == "system"
        and isinstance(item.get("content"), str)
        and "You are a Prompt Engineering Expert specializing" in item["content"]
        and "ANALYSIS PROCESS:" in item["content"]
        for item in systems
    ) and "USER INPUT:" in text:
        return None, "native_enhancer"
    query_span = None
    if any(marker in text.lower() for marker in ("<user_query", "<system-reminder", "<previous_user_message")):
        if indexes and len(indexes) != 1:
            return None, "ambiguous_wrapper"
        view = review_view(body, api_kind)
        match = _QUERY.search(text)
        if not view.certain or view.source != "workbuddy" or match is None:
            return None, "ambiguous_wrapper"
        if view.current_query != unescape(match.group(1)).strip():
            return None, "ambiguous_wrapper"
        query_span = match.span(1)
        text = view.current_query
    else:
        text = text.strip()
    if not text:
        return None, "empty_text"
    if len(text) > MAX_INPUT_CHARS:
        return None, "long_input"
    if "```" in text or "diff --git" in text:
        return None, "code_block"
    if "<workbuddy_tool_catalog>" in text or "<workbuddy_dynamic_context>" in text:
        return None, "internal_context"
    return PromptSource(text, copy.deepcopy(content), query_span, indexes), "eligible"


def extract_source(body: dict[str, Any], api_kind: str) -> tuple[PromptSource | None, str]:
    values = body.get("messages" if api_kind == "chat" else "input")
    if api_kind == "responses" and isinstance(values, str):
        source, reason = _content_source(values, body, api_kind)
        if source is not None:
            return PromptSource(source.text, source.original_content, source.query_span,
                                source.text_part_indexes, True), reason
        return source, reason
    if not isinstance(values, list):
        return None, "invalid_input"
    for item in reversed(values):
        if isinstance(item, dict) and str(item.get("role", "")).lower() == "user":
            return _content_source(item.get("content"), body, api_kind)
    return None, "no_user_message"


def _replace_content(content: Any, source: PromptSource, enhanced: str) -> Any | None:
    original = source.original_content
    if isinstance(original, str):
        if not isinstance(content, str) or not content.endswith(original):
            return None
        replacement = original
        if source.query_span is None:
            replacement = enhanced
        else:
            start, end = source.query_span
            replacement = original[:start] + escape(enhanced, quote=False) + original[end:]
        return content[:-len(original)] + replacement if original else None
    if not isinstance(original, list) or not isinstance(content, list) or content[-len(original):] != original:
        return None
    replacement = copy.deepcopy(original)
    indexes = source.text_part_indexes
    if not indexes:
        return None
    for index in indexes:
        key = _text_key(replacement[index])
        if key is None:
            return None
        if source.query_span is not None:
            start, end = source.query_span
            value = replacement[index][key]
            replacement[index][key] = (
                value[:start] + escape(enhanced, quote=False) + value[end:]
            )
        else:
            replacement[index][key] = enhanced if index == indexes[0] else ""
    return content[:-len(original)] + replacement


def replace_current_input(body: dict[str, Any], api_kind: str, source: PromptSource, enhanced: str) -> dict[str, Any] | None:
    result = copy.deepcopy(body)
    key = "messages" if api_kind == "chat" else "input"
    if api_kind == "responses" and source.input_is_string:
        value = _replace_content(result.get("input"), source, enhanced)
        if value is None:
            return None
        result["input"] = value
        return result
    values = result.get(key)
    if not isinstance(values, list):
        return None
    for item in reversed(values):
        if not isinstance(item, dict) or str(item.get("role", "")).lower() != "user":
            continue
        value = _replace_content(item.get("content"), source, enhanced)
        if value is not None:
            item["content"] = value
            return result
    return None


def validate_output(original: str, output: str) -> tuple[str | None, str]:
    if not isinstance(output, str):
        return None, "invalid_output"
    value = output.strip().strip('"\'“”‘’').strip()
    if not value or len(value) > MAX_OUTPUT_CHARS or value.startswith(("```", "Enhanced prompt:", "优化后的提示词：")):
        return None, "invalid_output"
    if any(token not in value for pattern in (_URL, _PATH, _NUMBER, _QUOTED)
           for token in pattern.findall(original)):
        return None, "lost_literal"
    if re.search(r"[\u4e00-\u9fff]", original) and not re.search(r"[\u4e00-\u9fff]", value):
        return None, "language_changed"
    if not re.search(r"[\u4e00-\u9fff]", original) and re.search(r"[\u4e00-\u9fff]", value):
        return None, "language_changed"
    return value, "valid"


def model_request(text: str) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_TEMPLATE},
            {"role": "user", "content": USER_TEMPLATE.replace("{input}", text, 1)},
        ],
        "temperature": 0,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }


async def _request_model(current: Any, decision: Any, request: dict[str, Any]) -> dict[str, Any]:
    direct = bool(decision.upstream_api_base)
    base_url = decision.upstream_api_base.rstrip("/") if direct else f"{current.internal_base_url.rstrip('/')}/v1"
    key = current.internal_api_key
    if direct:
        deployment = getattr(decision, "deployment_details", {}).get(
            getattr(decision, "deployment_id", "") or "", {}
        )
        key = os.environ.get(str(deployment.get("backend_api_key_env") or decision.endpoint.backend_api_key_env), "")
    payload = {**request, "model": decision.endpoint.provider_model if direct else decision.endpoint.id}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=3.0)) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
    response.raise_for_status()
    return response.json()


class EnhancementSession:
    def __init__(self, body: dict[str, Any], api_kind: str, *, client_id: str, policy: Any, request_id: str) -> None:
        try:
            self.source, self.reason = extract_source(body, api_kind)
        except Exception:
            self.source, self.reason = None, "extract_error"
        self.api_kind = api_kind
        self.client_id = client_id
        self.policy = policy
        self.request_id = request_id
        self.attempted = False
        self.target_id: str | None = None
        self.enhanced: str | None = None

    async def apply(
        self, current: Any, decision: Any, body: dict[str, Any], *,
        count_chat: Callable[[dict[str, Any]], Awaitable[int]],
        count_routed: Callable[[dict[str, Any]], Awaitable[int]],
        trace: Any | None,
    ) -> dict[str, Any]:
        original_tokens = decision.prompt_tokens
        original_context = decision.context_required
        main_guard = None
        try:
            if (
                self.source is not None
                and not self.attempted
                and decision.endpoint.capabilities.chat
                and decision.endpoint.capabilities.output_token_limit
                and getattr(decision.endpoint, "cloud", False)
                and getattr(decision.endpoint, "metadata", {}).get("billing_mode") != "subscription"
            ):
                try:
                    main_guard = await current.budget.reserve(
                        decision.endpoint,
                        request_id=f"{self.request_id}:enhance-main-guard",
                        prompt_tokens=original_tokens + MAX_EXTRA_PROMPT_TOKENS,
                        output_reserve_tokens=decision.output_reserve_tokens,
                    )
                except Exception as error:
                    self.attempted = True
                    details = {
                        "state": "skipped", "reason": "main_budget_guard_unavailable",
                        "target_id": decision.endpoint.id,
                        "error_type": type(error).__name__,
                    }
                    if trace is not None:
                        trace.payload["prompt_enhancement"] = details
                    try:
                        current.audit.write(
                            "prompt_enhancement", request_id=self.request_id,
                            client_id=self.client_id, **details,
                        )
                    except Exception:
                        pass
                    return body
            try:
                return await self._apply(
                    current, decision, body,
                    count_chat=count_chat, count_routed=count_routed, trace=trace,
                )
            finally:
                if main_guard is not None:
                    await current.budget.release(main_guard)
        except Exception as error:
            decision.prompt_tokens = original_tokens
            decision.context_required = original_context
            if trace is not None:
                trace.payload["prompt_enhancement"] = {
                    "state": "skipped", "reason": "enhancer_error",
                    "target_id": decision.endpoint.id,
                    "error_type": type(error).__name__,
                }
            try:
                current.audit.write(
                    "prompt_enhancement", request_id=self.request_id,
                    client_id=self.client_id, state="skipped",
                    reason="enhancer_error", target_id=decision.endpoint.id,
                    error_type=type(error).__name__,
                )
            except Exception:
                pass
            return body

    async def _apply(
        self, current: Any, decision: Any, body: dict[str, Any], *,
        count_chat: Callable[[dict[str, Any]], Awaitable[int]],
        count_routed: Callable[[dict[str, Any]], Awaitable[int]],
        trace: Any | None,
    ) -> dict[str, Any]:
        def record(state: str, reason: str, **details: Any) -> None:
            if trace is not None:
                trace.payload["prompt_enhancement"] = {
                    "state": state, "reason": reason,
                    "target_id": decision.endpoint.id, **details,
                }
            current.audit.write("prompt_enhancement", request_id=self.request_id,
                                client_id=self.client_id, state=state,
                                reason=reason, target_id=decision.endpoint.id,
                                **details)

        if self.source is None:
            if not self.attempted:
                self.attempted = True
                record("skipped", self.reason)
            return body
        if self.attempted:
            if self.target_id != decision.endpoint.id or self.enhanced is None:
                record("skipped", "retry_target_changed")
                return body
            enhanced = self.enhanced
        else:
            self.attempted = True
            self.target_id = decision.endpoint.id
            if not decision.endpoint.capabilities.chat or not decision.endpoint.capabilities.output_token_limit:
                record("skipped", "target_unbounded_or_incompatible")
                return body
            request = model_request(self.source.text)
            prompt_tokens = await count_chat(request)
            safe_context = min(decision.endpoint.safe_context_tokens,
                               decision.deployment_safe_context_tokens or decision.endpoint.safe_context_tokens)
            if prompt_tokens + MAX_OUTPUT_TOKENS > safe_context:
                record("skipped", "enhancer_context_overflow")
                return body
            reservation = None
            dispatched = False
            started = time.monotonic()
            try:
                reservation = await current.budget.reserve(
                    decision.endpoint, request_id=f"{self.request_id}:enhance",
                    prompt_tokens=prompt_tokens, output_reserve_tokens=MAX_OUTPUT_TOKENS)
                allowed, _ = await current.limiter.check_rate_limits(
                    self.client_id, prompt_tokens=prompt_tokens,
                    rpm_limit=self.policy.rpm_limit, tpm_limit=self.policy.tpm_limit)
                if not allowed:
                    await current.budget.release(reservation)
                    record("skipped", "enhancer_rate_limit")
                    return body
                dispatched = True
                payload = await _request_model(current, decision, request)
                await current.budget.settle(reservation, payload.get("usage"))
                reservation = None
                message = payload["choices"][0]["message"]
                raw = message.get("content") if isinstance(message, dict) else None
                enhanced, reason = validate_output(self.source.text, raw)
                if enhanced is None:
                    record("skipped", reason, elapsed_ms=round((time.monotonic() - started) * 1000))
                    return body
                self.enhanced = enhanced
            except Exception as error:
                if reservation is not None:
                    if dispatched:
                        await current.budget.commit(reservation)
                    else:
                        await current.budget.release(reservation)
                record("skipped", "enhancer_error", error_type=type(error).__name__,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
                return body
        proposed = replace_current_input(body, self.api_kind, self.source, enhanced)
        if proposed is None:
            record("skipped", "input_position_changed")
            return body
        new_tokens = await count_routed(proposed)
        safe_context = min(decision.endpoint.safe_context_tokens,
                           decision.deployment_safe_context_tokens or decision.endpoint.safe_context_tokens)
        if new_tokens + decision.output_reserve_tokens > safe_context:
            record("skipped", "main_context_overflow")
            return body
        extra_tokens = max(0, new_tokens - decision.prompt_tokens)
        if extra_tokens:
            allowed = await current.limiter.check_additional_tokens(
                self.client_id, extra_tokens, self.policy.tpm_limit)
            if not allowed:
                record("skipped", "main_rate_limit")
                return body
        previous = decision.prompt_tokens
        decision.prompt_tokens = new_tokens
        decision.context_required = new_tokens + decision.output_reserve_tokens
        record("applied", "enhanced", original_tokens=previous, routed_tokens=new_tokens,
               input_chars=len(self.source.text), output_chars=len(enhanced))
        return proposed
