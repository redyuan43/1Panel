from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .config import NodeConfig, RouterConfig
from .remote import load_lab_api_key


@dataclass(frozen=True)
class CompletionResult:
    transport: str
    first_token_seconds: float
    total_seconds: float
    prompt_tokens: int | None
    cached_tokens: int | None
    new_prefill_tokens: int | None
    prefill_seconds: float | None
    decode_tokens: int | None
    decode_seconds: float | None
    decode_tps: float | None
    content: str
    response_headers: dict[str, str]
    raw_usage: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "first_token_seconds": self.first_token_seconds,
            "total_seconds": self.total_seconds,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "new_prefill_tokens": self.new_prefill_tokens,
            "prefill_seconds": self.prefill_seconds,
            "decode_tokens": self.decode_tokens,
            "decode_seconds": self.decode_seconds,
            "decode_tps": self.decode_tps,
            "content": self.content,
            "response_headers": self.response_headers,
            "raw_usage": self.raw_usage,
        }


def _sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        yield json.loads(payload)


def _cached_tokens(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details["cached_tokens"])
    for key in ("cache_read_input_tokens", "cached_prompt_tokens"):
        if usage.get(key) is not None:
            return int(usage[key])
    return None


class DirectLlamaClient:
    def __init__(self, node: NodeConfig, *, timeout: float = 1800) -> None:
        self.node = node
        api_key = load_lab_api_key(node)
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key
            else None
        )
        self.client = httpx.Client(
            base_url=node.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10),
        )

    def close(self) -> None:
        self.client.close()

    def health(self) -> dict[str, Any]:
        response = self.client.get(self.node.health_path)
        return {
            "status_code": response.status_code,
            "body": response.text[:1000],
        }

    def slots(self) -> Any:
        response = self.client.get("/slots")
        response.raise_for_status()
        return response.json()

    def token_count(self, prompt: str) -> int:
        response = self.client.post(
            "/tokenize",
            json={
                "content": prompt,
                "add_special": True,
                "parse_special": True,
            },
        )
        response.raise_for_status()
        tokens = response.json().get("tokens")
        if not isinstance(tokens, list):
            raise ValueError("llama.cpp /tokenize did not return tokens")
        return len(tokens)

    def slot_action(
        self,
        action: str,
        *,
        slot_id: int = 0,
        filename: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"erase", "save", "restore"}:
            raise ValueError(f"unsupported slot action: {action}")
        body = {"filename": filename} if filename else {}
        response = self.client.post(
            f"/slots/{slot_id}",
            params={"action": action},
            json=body,
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError(f"slot {action} did not return an object")
        return value

    def complete(
        self,
        *,
        prompt: str,
        cache_prompt: bool,
        seed: int,
        max_tokens: int,
    ) -> CompletionResult:
        body = {
            "prompt": prompt,
            "n_predict": max_tokens,
            "stream": True,
            "temperature": 0,
            "seed": seed,
            "cache_prompt": cache_prompt,
            "ignore_eos": True,
        }
        started = time.monotonic()
        first_token: float | None = None
        content = ""
        final: dict[str, Any] | None = None
        with self.client.stream("POST", "/completion", json=body) as response:
            response.raise_for_status()
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower().startswith(("x-", "server"))
            }
            for event in _sse_events(response.iter_lines()):
                if "error" in event:
                    raise RuntimeError(f"llama.cpp error: {event['error']}")
                part = event.get("content") or ""
                if part and first_token is None:
                    first_token = time.monotonic() - started
                content += part
                if event.get("stop"):
                    final = event
        if first_token is None:
            raise ValueError("llama.cpp stream produced no content token")
        timings = (final or {}).get("timings") or {}
        cached = _finite_int(timings.get("cache_n"))
        processed = _finite_int(timings.get("prompt_n"))
        prompt_tokens = (
            cached + processed
            if cached is not None and processed is not None
            else None
        )
        prefill_ms = _finite_float(timings.get("prompt_ms"))
        predicted = _finite_int(timings.get("predicted_n"))
        predicted_ms = _finite_float(timings.get("predicted_ms"))
        return CompletionResult(
            transport="direct",
            first_token_seconds=first_token,
            total_seconds=time.monotonic() - started,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached,
            new_prefill_tokens=processed,
            prefill_seconds=(prefill_ms / 1000 if prefill_ms is not None else None),
            decode_tokens=predicted,
            decode_seconds=(
                predicted_ms / 1000 if predicted_ms is not None else None
            ),
            decode_tps=(
                predicted * 1000 / predicted_ms
                if predicted is not None and predicted_ms
                else None
            ),
            content=content,
            response_headers=headers,
            raw_usage=None,
        )

    def chat_complete(
        self,
        *,
        prompt: str,
        suffix: str,
        seed: int,
        max_tokens: int,
    ) -> CompletionResult:
        started = time.monotonic()
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": self.node.provider_model or "prefix-cache-lab",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": suffix},
                ],
                "chat_template_kwargs": {"enable_thinking": False},
                "temperature": 0,
                "seed": seed,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        response.raise_for_status()
        elapsed = time.monotonic() - started
        value = response.json()
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("llama.cpp chat response has no choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or message.get("reasoning_content") or ""
        if not content:
            raise ValueError("llama.cpp chat response produced no content")
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else None
        prompt_tokens = (
            int(usage["prompt_tokens"])
            if usage and usage.get("prompt_tokens") is not None
            else None
        )
        cached = _cached_tokens(usage)
        return CompletionResult(
            transport="direct-chat",
            first_token_seconds=elapsed,
            total_seconds=elapsed,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached,
            new_prefill_tokens=(
                prompt_tokens - cached
                if prompt_tokens is not None and cached is not None
                else None
            ),
            prefill_seconds=None,
            decode_tokens=(
                int(usage["completion_tokens"])
                if usage and usage.get("completion_tokens") is not None
                else None
            ),
            decode_seconds=None,
            decode_tps=None,
            content=content,
            response_headers={
                key.lower(): header
                for key, header in response.headers.items()
                if key.lower().startswith(("x-", "server"))
            },
            raw_usage=usage,
        )

    def prefill(
        self,
        *,
        prompt: str,
        cache_prompt: bool,
        seed: int,
    ) -> dict[str, Any]:
        started = time.monotonic()
        response = self.client.post(
            "/completion",
            json={
                "prompt": prompt,
                "n_predict": 0,
                "stream": False,
                "temperature": 0,
                "seed": seed,
                "cache_prompt": cache_prompt,
                "ignore_eos": True,
            },
        )
        response.raise_for_status()
        value = response.json()
        timings = value.get("timings") or {}
        cached = _finite_int(timings.get("cache_n"))
        processed = _finite_int(timings.get("prompt_n"))
        return {
            "elapsed_seconds": time.monotonic() - started,
            "prompt_tokens": (
                cached + processed
                if cached is not None and processed is not None
                else None
            ),
            "cached_tokens": cached,
            "new_prefill_tokens": processed,
            "prefill_seconds": (
                _finite_float(timings.get("prompt_ms")) / 1000
                if _finite_float(timings.get("prompt_ms")) is not None
                else None
            ),
            "slot_id": value.get("id_slot"),
            "tokens_predicted": value.get("tokens_predicted"),
        }


class RouterClient:
    def __init__(self, config: RouterConfig, *, timeout: float = 1800) -> None:
        key = os.environ.get(config.api_key_env, "")
        if not key:
            raise RuntimeError(f"{config.api_key_env} is required for router mode")
        self.config = config
        self.client = httpx.Client(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(timeout, connect=10),
        )

    def close(self) -> None:
        self.client.close()

    def complete(
        self,
        *,
        prompt: str,
        suffix: str,
        seed: int,
        max_tokens: int,
        conversation_id: str,
        enable_thinking: bool | None = None,
    ) -> CompletionResult:
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": suffix},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "seed": seed,
            "max_tokens": max_tokens,
        }
        if enable_thinking is not None:
            body["chat_template_kwargs"] = {
                "enable_thinking": enable_thinking,
            }
        started = time.monotonic()
        first_token: float | None = None
        content = ""
        usage: dict[str, Any] | None = None
        with self.client.stream(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={"X-1Panel-Conversation-ID": conversation_id},
        ) as response:
            response.raise_for_status()
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower().startswith(("x-1panel-", "x-request-id", "server"))
            }
            for event in _sse_events(response.iter_lines()):
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta") or {}
                part = delta.get("content") or delta.get("reasoning_content") or ""
                if part and first_token is None:
                    first_token = time.monotonic() - started
                content += part
        if first_token is None:
            raise ValueError("router stream produced no content token")
        prompt_tokens = (
            int(usage["prompt_tokens"])
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None
            else None
        )
        cached = _cached_tokens(usage)
        return CompletionResult(
            transport="router",
            first_token_seconds=first_token,
            total_seconds=time.monotonic() - started,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached,
            new_prefill_tokens=(
                prompt_tokens - cached
                if prompt_tokens is not None and cached is not None
                else None
            ),
            prefill_seconds=None,
            decode_tokens=(
                int(usage["completion_tokens"])
                if isinstance(usage, dict)
                and usage.get("completion_tokens") is not None
                else None
            ),
            decode_seconds=None,
            decode_tps=None,
            content=content,
            response_headers=headers,
            raw_usage=usage,
        )


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _finite_int(value: Any) -> int | None:
    result = _finite_float(value)
    return int(result) if result is not None else None
