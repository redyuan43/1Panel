"""Shared, bounded local routing observations. No prompts or inference calls."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import hashlib
import math
import time
from uuid import uuid4

MEMBERS = ("ai-qwen38-27b", "edge-qwen38-flash", "amd-qwen38-rocmfpx-128k")
PREFIX = "router:local-pool:v1:"


def bucket(tokens, minimum=32768):
    return max(minimum, 2 ** max(0, int(max(1, tokens) - 1).bit_length()))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class LocalPoolLockBusy(TimeoutError):
    """Only allocation-lock contention; backend/store failures remain errors."""


class LocalPool:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    @property
    def config(self):
        return self.settings.section("routing").get("local_pool", {})

    def member(self, endpoint):
        return bool(self.store and self.config.get("enabled") and endpoint and not endpoint.cloud
                    and endpoint.id in self.config.get("members", MEMBERS))

    def peers(self, first, second):
        return self.member(first) and self.member(second)

    @asynccontextmanager
    async def lock(self):
        token = uuid4().hex
        deadline = time.monotonic() + 2
        while not await self.store.acquire_lock(PREFIX + "lock", token, 10):
            if time.monotonic() >= deadline:
                raise LocalPoolLockBusy("local pool allocation lock busy")
            await asyncio.sleep(.02)
        try:
            yield
        finally:
            await self.store.release_lock(PREFIX + "lock", token)

    def identity(self, trace, conversation=None):
        payload = trace.payload if trace else {}
        identifier = (getattr(conversation, "conversation_id", None)
                      or payload.get("conversation_id") or payload.get("request_id") or uuid4().hex)
        return hashlib.sha256((str(payload.get("client_id", "")) + ":" + identifier).encode()).hexdigest()

    async def observations(self):
        return await self.store.list_json(PREFIX + "recent:"), await self.store.list_json(PREFIX + "claim:")

    async def select(self, endpoints, statuses, *, trace, conversation, prompt_tokens, output_tokens):
        """Reserve under one shared lock so simultaneous new sessions spread out."""
        if not endpoints or not all(self.member(e) for e in endpoints) or trace is None:
            return None
        async with self.lock():
            recent, claims = await self.observations()
            owner = self.identity(trace, conversation)
            rid = trace.request_id
            claims = [c for c in claims if c.get("request_id") != rid]
            values = []
            for e in endpoints:
                st = statuses[e.id]
                ours = [c for c in claims if c["endpoint_id"] == e.id]
                sessions = {c["owner"] for c in recent + ours if c["endpoint_id"] == e.id and c["owner"] != owner}
                last = max((c.get("assigned_at", 0) for c in recent + ours if c["endpoint_id"] == e.id), default=0)
                detail = st.detail
                backend_running = detail.get("running", detail.get("processing", 0)) or 0
                running = max(backend_running, len(ours))
                available = st.load_headroom > 0 and running < e.max_concurrency
                row = {"endpoint_id": e.id, "available": available, "running": running,
                       "recent_conversations": len(sessions), "capacity": e.max_concurrency,
                       "recent_per_slot": len(sessions) / e.max_concurrency, "last_assignment": last,
                       "generation": st.cache_generation}
                values.append((not available, bool(running or sessions), row["recent_per_slot"], last, e.id, e, row))
            chosen = min(values, key=lambda x: x[:5])
            e, row = chosen[-2:]
            claim = {"request_id": rid, "endpoint_id": e.id, "owner": owner, "assigned_at": time.time(),
                     "phase": "selected", "prompt_bucket": bucket(prompt_tokens), "output_bucket": bucket(output_tokens, 1024),
                     "generation": row["generation"], "started_at": None}
            await self.store.set_json(PREFIX + "claim:" + rid, claim, ttl_seconds=30)
            trace.payload.setdefault("local_pool", {}).update(
                group="local-peers", policy="fixed_continuation_v1", candidates=[v[-1] for v in values], claim=claim)
            trace.payload["local_pool"].setdefault("selection", "spread_new_conversations")
            return e

    async def start(self, decision, trace, conversation):
        if not self.member(decision.endpoint) or trace is None:
            return
        info = trace.payload.setdefault("local_pool", {"group": "local-peers"})
        claim = await self.store.get_json(PREFIX + "claim:" + trace.request_id)
        if not claim or claim["endpoint_id"] != decision.endpoint.id:
            claim = {"request_id": trace.request_id, "endpoint_id": decision.endpoint.id,
                     "owner": self.identity(trace, conversation), "assigned_at": time.time(),
                     "prompt_bucket": bucket(decision.prompt_tokens), "output_bucket": bucket(decision.output_reserve_tokens, 1024),
                     "generation": info.get("generation", "")}
        claim.update(phase="running", started_at=time.time(), generation=info.get("generation", claim.get("generation", "")))
        info["claim"] = claim
        await self.store.set_json(PREFIX + "claim:" + trace.request_id, claim, ttl_seconds=3600)

    async def release(self, request_id, trace=None):
        if trace is not None:
            trace.payload.get("local_pool", {}).pop("claim", None)
        if self.store:
            await self.store.delete(PREFIX + "claim:" + request_id)

    async def finish(self, trace):
        info = trace.payload.get("local_pool", {})
        claim = info.get("claim")
        if not claim or not trace.terminal:
            return
        # Capacity observations must not survive completion just because the
        # best-effort history writer cannot acquire the allocation lock.
        await self.release(trace.request_id)
        async with self.lock():
            if (trace.payload.get("status") != "succeeded"
                    or trace.payload.get("endpoint_id") != claim["endpoint_id"]):
                return
            key = PREFIX + "recent:" + claim["endpoint_id"] + ":" + claim["owner"]
            previous = await self.store.get_json(key) or {}
            record = {**claim, "assigned_at": previous.get("assigned_at", claim["assigned_at"]),
                      "last_used_at": time.time()}
            await self.store.set_json(key, record, ttl_seconds=int(self.config.get("recent_seconds", 600)))
