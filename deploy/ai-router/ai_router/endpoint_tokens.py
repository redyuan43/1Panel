"""Candidate-specific tokenization. No inference, prompts persisted, or remote URL input."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from collections import OrderedDict

import httpx

from .token_counter import request_modalities


class EndpointTokenCounter:
    def __init__(self):
        self.cache = OrderedDict()
        self.client = httpx.AsyncClient(trust_env=False, timeout=10, follow_redirects=False)

    async def close(self):
        try:
            await self.client.aclose()
        except RuntimeError as error:
            # The client may have been created and used inside an event loop that
            # has already stopped: the runtime is built, exercised and closed from
            # separate asyncio.run scopes in reload paths and tests, so its pooled
            # sockets are torn down together with the loop that owned them. Only
            # that specific failure is tolerated; AsyncClient.aclose() is
            # idempotent, so closing again from a live loop still releases it.
            if "Event loop is closed" not in str(error):
                raise

    async def count(self, endpoint, body, api_kind, fallback):
        config = endpoint.metadata.get("token_counting", {})
        if not config.get("enabled") or api_kind != "chat" or request_modalities(body, api_kind) != {"text"}:
            return {"tokens": fallback, "source": "shared_estimate", "exact": False}
        render = config.get("method") == "render"
        payload = copy.deepcopy(body) if render else {key: copy.deepcopy(body[key]) for key in
                   ("messages", "tools", "chat_template_kwargs", "chat_template", "add_special_tokens", "continue_final_message", "add_generation_prompt")
                   if key in body}
        payload["model"] = endpoint.provider_model
        payload["chat_template_kwargs"] = {
            **config.get("chat_template_kwargs", {}),
            **payload.get("chat_template_kwargs", {}),
        }
        version = str(config.get("version", "unversioned"))
        format_version = "vllm-render-chat-v1" if render else "chat-v1"
        # Templates can iterate tool-schema fields in insertion order.
        signature = hashlib.sha256(json.dumps(
            [endpoint.api_base, endpoint.provider_model, version, format_version, payload],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        cached = self.cache.get(signature)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(signature)
            return {**cached[1], "cache_hit": True}
        headers = {}
        if endpoint.backend_api_key_env and os.environ.get(endpoint.backend_api_key_env):
            headers["Authorization"] = "Bearer " + os.environ[endpoint.backend_api_key_env]
        try:
            url = endpoint.api_base.rstrip("/") + "/chat/completions/render" if render else endpoint.api_base.removesuffix("/v1") + "/tokenize"
            response = await self.client.post(url,
                                              json=payload, headers=headers)
            response.raise_for_status()
            value = response.json()
            if render and not isinstance(value.get("token_ids"), list):
                raise ValueError("invalid rendered tokens")
            tokens = len(value["token_ids"]) if render else value["count"]
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError("invalid count")
            result = {"tokens": tokens, "source": "backend_render" if render else "backend_tokenize", "exact": True,
                      "version": version, "format": format_version}
            self.cache[signature] = (time.monotonic() + 300, result)
            self.cache.move_to_end(signature)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {"tokens": fallback, "source": "shared_estimate", "exact": False,
                    "reason": "backend_tokenization_unavailable", "version": version}
