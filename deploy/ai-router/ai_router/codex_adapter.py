from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .codex_auth import (
    CODEX_BASE_URL,
    CodexAccountStore,
    CodexAuthError,
    CodexCredentials,
    codex_headers,
)


DEFAULT_MODEL_IDS = ("gpt-5.6-sol", "gpt-6-astra")
SAFE_CONTEXT_TOKENS = 272000
CATALOG_TTL_SECONDS = 300
ACCOUNT_HEADER = "x-1panel-codex-account"


class CodexGateway:
    def __init__(
        self,
        *,
        accounts: CodexAccountStore | None = None,
        client: httpx.AsyncClient | None = None,
        account_max_concurrency: int | None = None,
        model_ids: tuple[str, ...] | None = None,
    ) -> None:
        accounts_dir = os.environ.get(
            "AI_ROUTER_CODEX_ACCOUNTS_DIR",
            "/data/codex-auth/accounts",
        )
        self.accounts = accounts or CodexAccountStore(accounts_dir)
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=10.0),
        )
        self._owned_client = client is None
        self._catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._locks: dict[str, asyncio.Semaphore] = {}
        configured_models = (
            model_ids
            or tuple(
                item.strip()
                for item in os.environ.get(
                    "AI_ROUTER_CODEX_MODEL_IDS",
                    ",".join(DEFAULT_MODEL_IDS),
                ).split(",")
                if item.strip()
            )
        )
        self.model_ids = tuple(dict.fromkeys(configured_models))
        if not self.model_ids:
            raise ValueError("at least one Codex model ID is required")
        self.account_max_concurrency = max(
            1,
            int(
                account_max_concurrency
                if account_max_concurrency is not None
                else os.environ.get(
                    "AI_ROUTER_CODEX_ACCOUNT_MAX_CONCURRENCY",
                    "2",
                )
            ),
        )

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    async def status(self) -> dict[str, Any]:
        workers = []
        for alias in self.accounts.aliases():
            ready = False
            models: list[dict[str, Any]] = []
            entitled_models: list[str] = []
            error_code = None
            if self.accounts.available(alias):
                try:
                    models = await self.catalog(alias)
                    entitled_models = sorted(
                        {
                            str(item.get("slug"))
                            for item in models
                            if item.get("slug") in self.model_ids
                        }
                    )
                    ready = bool(entitled_models)
                    if not ready:
                        error_code = "model_not_entitled"
                except CodexAuthError as exc:
                    error_code = exc.code
                except Exception:
                    error_code = "catalog_unavailable"
            else:
                error_code = str(
                    self.accounts.account_status(alias).get(
                        "last_error_code",
                        "account_cooldown",
                    )
                )
            workers.append(
                {
                    "worker_id": f"codex-{alias}",
                    "account_alias": alias,
                    "ready": ready,
                    "state": "available" if ready else "unavailable",
                    "safe_context_tokens": SAFE_CONTEXT_TOKENS,
                    "api_base": (
                        "http://127.0.0.1:14010"
                        f"/v1/accounts/{alias}"
                    ),
                    "models": [
                        model_id for model_id in entitled_models
                    ],
                    "error_code": error_code,
                    "cooldown_until": float(
                        self.accounts.account_status(alias).get(
                            "cooldown_until",
                            0,
                        )
                        or 0
                    ),
                    "max_concurrency": self.account_max_concurrency,
                }
            )
        available_models = sorted(
            {
                model_id
                for worker in workers
                if worker["ready"]
                for model_id in worker["models"]
            }
        )
        return {
            "ok": any(item["ready"] for item in workers),
            "model": available_models[0] if available_models else "",
            "models": available_models,
            "safe_context_tokens": SAFE_CONTEXT_TOKENS,
            "max_concurrency": self.account_max_concurrency,
            "workers": workers,
        }

    async def catalog(self, alias: str) -> list[dict[str, Any]]:
        cached = self._cached_catalog(alias)
        if cached is not None:
            return cached
        credentials = await asyncio.to_thread(
            self.accounts.credentials,
            alias,
        )
        response = await self.client.get(
            f"{CODEX_BASE_URL}/models?client_version=1.0.0",
            headers=codex_headers(credentials),
        )
        if response.status_code == 401:
            credentials = await asyncio.to_thread(
                self.accounts.credentials,
                alias,
                force_refresh=True,
            )
            response = await self.client.get(
                f"{CODEX_BASE_URL}/models?client_version=1.0.0",
                headers=codex_headers(credentials),
            )
        if response.status_code == 429:
            self._mark_rate_limit(alias, response)
        if response.status_code != 200:
            raise CodexAuthError(
                f"Codex model catalog returned {response.status_code}",
                code=f"codex_catalog_{response.status_code}",
                relogin_required=response.status_code in {401, 403},
            )
        payload = response.json()
        raw_models = payload.get("models", []) if isinstance(payload, dict) else []
        models = [
            dict(item)
            for item in raw_models
            if isinstance(item, dict)
            and isinstance(item.get("slug"), str)
        ]
        self._catalog_cache[alias] = (time.monotonic(), models)
        self.accounts.mark_available(alias)
        return models

    def _cached_catalog(
        self,
        alias: str,
    ) -> list[dict[str, Any]] | None:
        cached = self._catalog_cache.get(alias)
        if (
            cached is None
            or time.monotonic() - cached[0] >= CATALOG_TTL_SECONDS
        ):
            return None
        return cached[1]

    async def proxy(
        self,
        request: Request,
        *,
        api_kind: str,
        alias: str | None,
    ) -> Response:
        self._authorize(request)
        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid_json", "request body must be valid JSON")
        if not isinstance(body, dict):
            return _error(400, "invalid_request", "request body must be an object")
        model_id = str(body.get("model", "")).strip()
        if model_id not in self.model_ids:
            return _error(404, "model_not_found", f"unknown model: {body.get('model')}")
        selected_alias = alias or request.headers.get(ACCOUNT_HEADER, "").strip()
        if not selected_alias:
            selected_alias = await self._first_available_account(model_id)
        if not selected_alias:
            return _error(
                503,
                "codex_account_unavailable",
                "no Codex Pro account is currently available",
            )
        if not self.accounts.available(selected_alias):
            return _error(
                429,
                "codex_account_cooldown",
                "the selected Codex Pro account is temporarily unavailable",
                headers={"Retry-After": "1"},
            )
        account_models = self._cached_catalog(selected_alias)
        if account_models is not None and not any(
            item.get("slug") == model_id
            for item in account_models
        ):
            return _error(
                404,
                "model_not_entitled",
                "the selected Codex Pro account does not provide this model",
            )

        semaphore = self._locks.setdefault(
            selected_alias,
            asyncio.Semaphore(self.account_max_concurrency),
        )
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=3.0)
        except (asyncio.TimeoutError, TimeoutError):
            return _error(
                429,
                "codex_account_busy",
                "the selected Codex Pro account is busy",
                headers={"Retry-After": "1"},
            )

        try:
            client_stream = bool(body.get("stream"))
            original_to_wire, wire_to_original = _tool_name_maps(
                body.get("tools")
            )
            upstream_body = (
                _chat_to_responses(
                    body,
                    request,
                    original_to_wire,
                    model_id,
                )
                if api_kind == "chat"
                else _responses_payload(
                    body,
                    request,
                    original_to_wire,
                    model_id,
                )
            )
            upstream_body["stream"] = True
            upstream = await self._send(
                selected_alias,
                upstream_body,
            )
        except CodexAuthError as exc:
            semaphore.release()
            return _error(
                401 if exc.relogin_required else 503,
                exc.code,
                str(exc),
            )
        except httpx.RequestError as exc:
            semaphore.release()
            return _error(
                503,
                "codex_upstream_unavailable",
                f"Codex upstream request failed: {type(exc).__name__}",
            )
        except Exception:
            semaphore.release()
            raise

        response_headers = {
            "X-1Panel-Codex-Account": selected_alias,
            "X-1Panel-Codex-Model": model_id,
        }
        if upstream.status_code >= 400:
            payload = await upstream.aread()
            await upstream.aclose()
            semaphore.release()
            return Response(
                content=payload,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
                headers=response_headers,
            )

        if client_stream:
            generator = (
                _chat_stream(
                    upstream,
                    semaphore,
                    wire_to_original,
                    model_id,
                )
                if api_kind == "chat"
                else _responses_stream(
                    upstream,
                    semaphore,
                    wire_to_original,
                )
            )
            return StreamingResponse(
                generator,
                status_code=upstream.status_code,
                media_type="text/event-stream",
                headers=response_headers,
            )

        try:
            value = await _completed_response(upstream)
        except Exception:
            semaphore.release()
            return _error(
                502,
                "codex_response_invalid",
                "Codex returned an invalid streaming response",
            )
        semaphore.release()
        _restore_response_tool_names(value, wire_to_original)
        if api_kind == "chat":
            try:
                value = _responses_to_chat(value, model_id)
                payload = json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            except Exception:
                return _error(
                    502,
                    "codex_response_invalid",
                    "Codex returned an invalid response",
                )
        else:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        return Response(
            content=payload,
            status_code=200,
            media_type="application/json",
            headers=response_headers,
        )

    async def _send(
        self,
        alias: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        credentials = await asyncio.to_thread(
            self.accounts.credentials,
            alias,
        )
        response = await self.client.send(
            self.client.build_request(
                "POST",
                f"{CODEX_BASE_URL}/responses",
                headers=codex_headers(credentials),
                json=body,
            ),
            stream=True,
        )
        if response.status_code == 401:
            await response.aclose()
            credentials = await asyncio.to_thread(
                self.accounts.credentials,
                alias,
                force_refresh=True,
            )
            response = await self.client.send(
                self.client.build_request(
                    "POST",
                    f"{CODEX_BASE_URL}/responses",
                    headers=codex_headers(credentials),
                    json=body,
                ),
                stream=True,
            )
        if response.status_code == 429:
            self._mark_rate_limit(alias, response)
        elif response.is_success:
            self.accounts.mark_available(alias)
        return response

    async def _first_available_account(
        self,
        model_id: str,
    ) -> str | None:
        status = await self.status()
        for worker in status["workers"]:
            if worker["ready"] and model_id in worker["models"]:
                return str(worker["account_alias"])
        return None

    def _mark_rate_limit(
        self,
        alias: str,
        response: httpx.Response,
    ) -> None:
        retry_after = response.headers.get("retry-after", "").strip()
        try:
            seconds = int(float(retry_after))
        except ValueError:
            seconds = 300
        self.accounts.mark_cooldown(
            alias,
            seconds=max(1, min(seconds, 86400)),
            code="codex_rate_limited",
        )

    @staticmethod
    def _authorize(request: Request) -> None:
        expected = (
            os.environ.get("AI_ROUTER_CODEX_ADAPTER_KEY", "").strip()
            or os.environ.get(
                "AI_ROUTER_LITELLM_MASTER_KEY",
                "",
            ).strip()
        )
        provided = request.headers.get("authorization", "")
        if not expected or provided != f"Bearer {expected}":
            raise CodexAuthError(
                "invalid adapter credentials",
                code="codex_adapter_unauthorized",
            )


def create_app(gateway: CodexGateway | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = gateway is None
        app.state.gateway = gateway or CodexGateway()
        yield
        if owned:
            await app.state.gateway.close()

    app = FastAPI(
        title="1Panel Codex Subscription Adapter",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(CodexAuthError)
    async def auth_error_handler(
        _request: Request,
        exc: CodexAuthError,
    ) -> JSONResponse:
        return _error(
            401 if exc.relogin_required else 403,
            exc.code,
            str(exc),
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return await request.app.state.gateway.status()

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        request.app.state.gateway._authorize(request)
        status = await request.app.state.gateway.status()
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "openai-codex-subscription",
                    }
                    for model_id in status["models"]
                ],
            }
        )

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await request.app.state.gateway.proxy(
            request,
            api_kind="responses",
            alias=None,
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Response:
        return await request.app.state.gateway.proxy(
            request,
            api_kind="chat",
            alias=None,
        )

    @app.post("/v1/accounts/{alias}/responses")
    async def account_responses(alias: str, request: Request) -> Response:
        return await request.app.state.gateway.proxy(
            request,
            api_kind="responses",
            alias=alias,
        )

    @app.post("/v1/accounts/{alias}/chat/completions")
    async def account_chat(alias: str, request: Request) -> Response:
        return await request.app.state.gateway.proxy(
            request,
            api_kind="chat",
            alias=alias,
        )

    return app


def _responses_payload(
    body: dict[str, Any],
    request: Request,
    original_to_wire: dict[str, str],
    model_id: str,
) -> dict[str, Any]:
    result = json.loads(json.dumps(body))
    if isinstance(result.get("input"), str):
        result["input"] = [
            {
                "role": "user",
                "content": result["input"],
            }
        ]
    result["model"] = model_id
    result.setdefault("store", False)
    result.setdefault("reasoning", {"effort": "medium", "summary": "auto"})
    result.setdefault("include", ["reasoning.encrypted_content"])
    for key in (
        "max_output_tokens",
        "max_completion_tokens",
        "max_tokens",
        "temperature",
    ):
        result.pop(key, None)
    if isinstance(result.get("tools"), list):
        result["tools"] = _responses_tools(
            result["tools"],
            original_to_wire,
        )
    input_items = result.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and item.get("name")
            ):
                name = str(item["name"])
                item["name"] = original_to_wire.get(name, name)
    _map_tool_choice(result, original_to_wire)
    _set_prompt_cache_key(result, request)
    result.pop("conversation", None)
    return result


def _chat_to_responses(
    body: dict[str, Any],
    request: Request,
    original_to_wire: dict[str, str],
    model_id: str,
) -> dict[str, Any]:
    messages = body.get("messages", [])
    instructions = []
    input_items: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        if role in {"system", "developer"}:
            instructions.append(_content_text(message.get("content")))
            continue
        if role in {"user", "assistant"}:
            if role == "assistant":
                for reasoning in message.get(
                    "codex_reasoning_items",
                    [],
                ) or []:
                    if (
                        isinstance(reasoning, dict)
                        and reasoning.get("type") == "reasoning"
                        and reasoning.get("encrypted_content")
                    ):
                        input_items.append(
                            {
                                key: value
                                for key, value in reasoning.items()
                                if key != "id"
                            }
                        )
                replayed_message = False
                for prior_item in message.get(
                    "codex_message_items",
                    [],
                ) or []:
                    if (
                        isinstance(prior_item, dict)
                        and prior_item.get("type") == "message"
                        and prior_item.get("role") == "assistant"
                    ):
                        input_items.append(dict(prior_item))
                        replayed_message = True
            item: dict[str, Any] = {
                "role": role,
                "content": _responses_content(
                    message.get("content"),
                    assistant=role == "assistant",
                ),
            }
            if not (
                role == "assistant"
                and replayed_message
            ):
                input_items.append(item)
            if role == "assistant":
                for call in message.get("tool_calls", []) or []:
                    function = (
                        call.get("function", {})
                        if isinstance(call, dict)
                        else {}
                    )
                    if not isinstance(function, dict) or not function.get("name"):
                        continue
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": _call_id(
                                str(call.get("id") or uuid4().hex)
                            ),
                            "name": original_to_wire.get(
                                str(function["name"]),
                                str(function["name"]),
                            ),
                            "arguments": _arguments(function.get("arguments")),
                        }
                    )
            continue
        if role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": _call_id(
                        str(message.get("tool_call_id", ""))
                    ),
                    "output": _content_text(message.get("content")),
                }
            )

    result: dict[str, Any] = {
        "model": model_id,
        "input": input_items,
        "store": False,
        "stream": bool(body.get("stream")),
        "reasoning": {
            "effort": str(
                body.get("reasoning_effort")
                or (body.get("reasoning") or {}).get("effort")
                or "medium"
            ),
            "summary": "auto",
        },
        "include": ["reasoning.encrypted_content"],
    }
    if instructions:
        result["instructions"] = "\n\n".join(
            item for item in instructions if item
        )
    tools = _responses_tools(
        body.get("tools"),
        original_to_wire,
    )
    if tools:
        result["tools"] = tools
        result["tool_choice"] = body.get("tool_choice", "auto")
        _map_tool_choice(result, original_to_wire)
        result["parallel_tool_calls"] = bool(
            body.get("parallel_tool_calls", True)
        )
    response_format = body.get("response_format")
    if isinstance(response_format, dict):
        result["text"] = {"format": _response_format(response_format)}
    _set_prompt_cache_key(result, request)
    return result


def _set_prompt_cache_key(
    body: dict[str, Any],
    request: Request,
) -> None:
    conversation_id = (
        request.headers.get("x-1panel-conversation-id")
        or request.headers.get("x-litellm-session-id")
        or ""
    )
    static = json.dumps(
        {
            "conversation": conversation_id,
            "instructions": body.get("instructions", ""),
            "tools": body.get("tools", []),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    body.setdefault(
        "prompt_cache_key",
        "pck_" + hashlib.sha256(static.encode()).hexdigest()[:24],
    )


def _responses_tools(
    value: Any,
    original_to_wire: dict[str, str],
) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function":
            function = item.get("function", item)
            if not isinstance(function, dict) or not function.get("name"):
                continue
            result.append(
                {
                    "type": "function",
                    "name": original_to_wire.get(
                        str(function["name"]),
                        str(function["name"]),
                    ),
                    "description": str(function.get("description", "")),
                    "parameters": function.get(
                        "parameters",
                        {"type": "object", "properties": {}},
                    ),
                    "strict": bool(function.get("strict", False)),
                }
            )
        else:
            result.append(dict(item))
    return result


def _responses_to_chat(
    value: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    content = []
    tool_calls = []
    for item in value.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") in {
                    "output_text",
                    "text",
                }:
                    content.append(str(part.get("text", "")))
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name", "")),
                        "arguments": _arguments(item.get("arguments")),
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    reasoning_items, message_items = _codex_state(value)
    if reasoning_items:
        message["codex_reasoning_items"] = reasoning_items
    if message_items:
        message["codex_message_items"] = message_items
    usage = value.get("usage", {}) if isinstance(value.get("usage"), dict) else {}
    return {
        "id": str(value.get("id") or f"chatcmpl-{uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "prompt_tokens_details": usage.get("input_tokens_details", {}),
        },
    }


async def _responses_stream(
    upstream: httpx.Response,
    semaphore: asyncio.Semaphore,
    wire_to_original: dict[str, str],
) -> AsyncIterator[bytes]:
    buffer = ""
    try:
        async for chunk in upstream.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                yield _restore_sse_block(
                    block,
                    wire_to_original,
                )
        if buffer:
            yield _restore_sse_block(
                buffer,
                wire_to_original,
            )
    finally:
        await upstream.aclose()
        semaphore.release()


async def _completed_response(
    upstream: httpx.Response,
) -> dict[str, Any]:
    payload = await upstream.aread()
    await upstream.aclose()
    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not any(line.startswith("data:") for line in lines):
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Codex response must be an object")
        return value

    completed: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    completed_items: dict[int, dict[str, Any]] = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        event = json.loads(raw)
        if event.get("type") == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                completed_items[int(event.get("output_index", 0))] = item
        elif event.get("type") == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                completed = response
        elif event.get("type") in {
            "error",
            "response.failed",
        }:
            error = event
    if completed is not None:
        if not completed.get("output") and completed_items:
            completed["output"] = [
                completed_items[index]
                for index in sorted(completed_items)
            ]
        return completed
    if error is not None:
        raise ValueError(str(error.get("error") or error))
    raise ValueError("Codex stream ended without response.completed")


async def _chat_stream(
    upstream: httpx.Response,
    semaphore: asyncio.Semaphore,
    wire_to_original: dict[str, str],
    model_id: str,
) -> AsyncIterator[bytes]:
    response_id = f"chatcmpl-{uuid4().hex}"
    buffer = ""
    tool_indexes: dict[str, int] = {}
    completed_items: dict[int, dict[str, Any]] = {}
    yield _chat_chunk(
        response_id,
        {"role": "assistant", "content": ""},
        model_id=model_id,
    )
    try:
        async for chunk in upstream.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data_lines = [
                    line[5:].strip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                raw = "\n".join(data_lines)
                if raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type", ""))
                if event_type == "response.created":
                    response = event.get("response", {})
                    response_id = str(response.get("id") or response_id)
                elif event_type == "response.output_text.delta":
                    yield _chat_chunk(
                        response_id,
                        {"content": str(event.get("delta", ""))},
                        model_id=model_id,
                    )
                elif event_type == "response.output_item.added":
                    item = event.get("item", {})
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call"
                    ):
                        item_id = str(
                            item.get("id")
                            or item.get("call_id")
                            or len(tool_indexes)
                        )
                        index = tool_indexes.setdefault(item_id, len(tool_indexes))
                        yield _chat_chunk(
                            response_id,
                            {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": str(
                                            item.get("call_id")
                                            or item.get("id")
                                            or ""
                                        ),
                                        "type": "function",
                                        "function": {
                                            "name": wire_to_original.get(
                                                str(item.get("name", "")),
                                                str(item.get("name", "")),
                                            ),
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                            model_id=model_id,
                        )
                elif event_type == "response.function_call_arguments.delta":
                    item_id = str(
                        event.get("item_id")
                        or event.get("call_id")
                        or ""
                    )
                    index = tool_indexes.setdefault(item_id, len(tool_indexes))
                    yield _chat_chunk(
                        response_id,
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {
                                        "arguments": str(
                                            event.get("delta", "")
                                        )
                                    },
                                }
                            ]
                        },
                        model_id=model_id,
                    )
                elif event_type == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict):
                        completed_items[
                            int(event.get("output_index", 0))
                        ] = item
                elif event_type == "response.completed":
                    response = event.get("response", {})
                    if (
                        isinstance(response, dict)
                        and not response.get("output")
                        and completed_items
                    ):
                        response["output"] = [
                            completed_items[index]
                            for index in sorted(completed_items)
                        ]
                    usage = (
                        response.get("usage", {})
                        if isinstance(response, dict)
                        else {}
                    )
                    finish = "tool_calls" if tool_indexes else "stop"
                    reasoning_items, message_items = _codex_state(
                        response
                        if isinstance(response, dict)
                        else {}
                    )
                    delta: dict[str, Any] = {}
                    if reasoning_items:
                        delta["codex_reasoning_items"] = reasoning_items
                    if message_items:
                        delta["codex_message_items"] = message_items
                    yield _chat_chunk(
                        response_id,
                        delta,
                        model_id=model_id,
                        finish_reason=finish,
                        usage=usage,
                    )
        yield b"data: [DONE]\n\n"
    finally:
        await upstream.aclose()
        semaphore.release()


def _chat_chunk(
    response_id: str,
    delta: dict[str, Any],
    *,
    model_id: str,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bytes:
    value: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        value["usage"] = {
            "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    return (
        "data: "
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


def _responses_content(value: Any, *, assistant: bool) -> Any:
    if isinstance(value, list):
        result = []
        for part in value:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", ""))
            if part_type in {"text", "input_text", "output_text"}:
                result.append(
                    {
                        "type": "output_text" if assistant else "input_text",
                        "text": str(part.get("text", "")),
                    }
                )
            elif part_type in {"image_url", "input_image"} and not assistant:
                image = part.get("image_url")
                if isinstance(image, dict):
                    image = image.get("url")
                if image:
                    result.append(
                        {
                            "type": "input_image",
                            "image_url": str(image),
                        }
                    )
        return result
    return _content_text(value)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and item.get("text")
        )
    return "" if value is None else str(value)


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value or "{}"
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _call_id(value: str) -> str:
    current = value.strip()
    if len(current) <= 64:
        return current
    digest = hashlib.sha256(current.encode()).hexdigest()[:32]
    return f"call_{digest}"


def _tool_name_maps(
    tools: Any,
) -> tuple[dict[str, str], dict[str, str]]:
    original_to_wire: dict[str, str] = {}
    wire_to_original: dict[str, str] = {}
    for item in tools if isinstance(tools, list) else []:
        if not isinstance(item, dict):
            continue
        function = item.get("function", item)
        if not isinstance(function, dict):
            continue
        original = str(function.get("name", "")).strip()
        if not original:
            continue
        wire = _wire_tool_name(original)
        original_to_wire[original] = wire
        wire_to_original[wire] = original
    return original_to_wire, wire_to_original


def _wire_tool_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name) and len(name) <= 64:
        return name
    base = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "tool"
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{base[:53]}__{digest}"


def _map_tool_choice(
    body: dict[str, Any],
    original_to_wire: dict[str, str],
) -> None:
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return
    function = choice.get("function")
    if isinstance(function, dict) and function.get("name"):
        name = str(function["name"])
        body["tool_choice"] = {
            "type": "function",
            "name": original_to_wire.get(name, name),
        }
        return
    if choice.get("type") == "function" and choice.get("name"):
        name = str(choice["name"])
        choice["name"] = original_to_wire.get(name, name)


def _restore_response_tool_names(
    response: dict[str, Any],
    wire_to_original: dict[str, str],
) -> None:
    for item in response.get("output", []) or []:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("name")
        ):
            name = str(item["name"])
            item["name"] = wire_to_original.get(name, name)


def _restore_sse_block(
    block: str,
    wire_to_original: dict[str, str],
) -> bytes:
    lines = []
    for line in block.splitlines():
        if not line.startswith("data:"):
            lines.append(line)
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            lines.append(line)
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        item = event.get("item")
        if isinstance(item, dict):
            _restore_response_tool_names(
                {"output": [item]},
                wire_to_original,
            )
        response = event.get("response")
        if isinstance(response, dict):
            _restore_response_tool_names(
                response,
                wire_to_original,
            )
        lines.append(
            "data: "
            + json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return ("\n".join(lines) + "\n\n").encode()


def _codex_state(
    response: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reasoning_items = []
    message_items = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if (
            item.get("type") == "reasoning"
            and item.get("encrypted_content")
        ):
            reasoning_items.append(dict(item))
        elif (
            item.get("type") == "message"
            and item.get("role") == "assistant"
        ):
            message_items.append(dict(item))
    return reasoning_items, message_items


def _response_format(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("type") == "json_schema":
        schema = value.get("json_schema", {})
        if isinstance(schema, dict):
            return {
                "type": "json_schema",
                **{
                    key: schema[key]
                    for key in ("name", "description", "schema", "strict")
                    if key in schema
                },
            }
    return {"type": str(value.get("type", "text"))}


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error"
                if status_code < 500
                else "server_error",
                "code": code,
            }
        },
    )


app = create_app()
