"""Shared, bounded local routing observations. No prompts or inference calls."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import hashlib
import math
import statistics
import time
from uuid import uuid4

MEMBERS = ("ai-qwen38-27b", "edge-qwen38-flash", "amd-qwen38-rocmfpx-128k")
PREFIX = "router:local-pool:v1:"


def bucket(tokens, minimum=32768):
    return max(minimum, 2 ** max(0, int(max(1, tokens) - 1).bit_length()))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


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
                raise TimeoutError("local pool allocation lock busy")
            await asyncio.sleep(.02)
        try:
            yield
        finally:
            await self.store.release_lock(PREFIX + "lock", token)

    def identity(self, trace, conversation=None):
        payload = trace.payload if trace else {}
        identifier = (getattr(conversation, "branch_id", None) or getattr(conversation, "conversation_id", None)
                      or payload.get("branch_id") or payload.get("conversation_id") or payload.get("request_id") or uuid4().hex)
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
                group="local-peers", candidates=[v[-1] for v in values], claim=claim)
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
        recent = await self.store.get_json(PREFIX + "recent:" + claim["endpoint_id"] + ":" + claim["owner"])
        claim["cache_assumption"] = ("hot" if recent and recent.get("cache_state") == "hot"
                                     and recent.get("generation") == claim["generation"] else "cold")
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
        from .cache_audit import metrics
        m = metrics(trace.payload, [])
        async with self.lock():
            await self.release(trace.request_id)
            if (trace.payload.get("status") != "succeeded"
                    or trace.payload.get("endpoint_id") != claim["endpoint_id"]):
                return
            ratio = m.get("backend_reuse_ratio")
            state = "hot" if ratio is not None and ratio >= .8 else "cold" if ratio == 0 else None
            generation = claim.get("generation")
            key = PREFIX + "recent:" + claim["endpoint_id"] + ":" + claim["owner"]
            previous = await self.store.get_json(key) or {}
            record = {**claim, "assigned_at": previous.get("assigned_at", claim["assigned_at"]),
                      "cache_state": state if m["attempts"] == 1 else None, "last_used_at": time.time()}
            await self.store.set_json(PREFIX + "recent:" + claim["endpoint_id"] + ":" + claim["owner"], record,
                                      ttl_seconds=int(self.config.get("recent_seconds", 600)))
            # Retried/partial/estimated/nonstreaming samples cannot label a warm execution.
            if (not generation or state is None or m["attempts"] != 1 or not finite(m.get("ttft_ms"))
                    or not finite(m.get("queue_ms")) or not finite(m.get("total_ms"))):
                return
            first = (m["ttft_ms"] - m["queue_ms"]) / 1000
            duration = (m["total_ms"] - m["queue_ms"]) / 1000
            if first <= 0 or duration < first:
                return
            key = PREFIX + "samples:" + claim["endpoint_id"]
            samples = (await self.store.get_json(key) or {}).get("items", [])
            samples = [x for x in samples if x["request_id"] != trace.request_id and x["at"] > time.time() - 86400]
            samples.append({"request_id": trace.request_id, "generation": generation, "at": time.time(),
                            "prompt_bucket": claim["prompt_bucket"], "output_bucket": claim["output_bucket"],
                            "cache_state": state, "first_s": first, "duration_s": duration})
            await self.store.set_json(key, {"items": samples[-256:]}, ttl_seconds=86400)

    async def samples(self, endpoint_id, generation, prompt_bucket, output_bucket, cache_state=None):
        items = (await self.store.get_json(PREFIX + "samples:" + endpoint_id) or {}).get("items", [])
        return [s for s in items if s.get("generation") == generation and generation
                and s.get("prompt_bucket") == prompt_bucket and s.get("output_bucket") == output_bucket
                and s.get("at", 0) > time.time() - 86400 and (cache_state is None or s.get("cache_state") == cache_state)]

    async def costs(self, endpoints, statuses, *, trace, conversation, prompt_tokens, output_tokens):
        recent, claims = await self.observations()
        owner = self.identity(trace, conversation)
        result = []
        for e in endpoints:
            st = statuses[e.id]
            previous = next((r for r in recent if r["endpoint_id"] == e.id and r["owner"] == owner
                             and r.get("generation") == st.cache_generation and r.get("cache_state") == "hot"), None)
            state = "hot" if previous else "cold"
            samples = await self.samples(e.id, st.cache_generation, bucket(prompt_tokens), bucket(output_tokens, 1024), state)
            row = {"endpoint_id": e.id, "cache_assumption": state, "sample_count": len(samples),
                   "source": "historical_measured_usage", "estimated": True, "queue_s": None, "first_s": None, "total_s": None}
            if len(samples) < max(5, int(self.config.get("min_samples", 5))):
                row["unavailable_reason"] = "insufficient_matching_samples"
                result.append(row)
                continue
            row["first_s"] = statistics.median(s["first_s"] for s in samples)
            active = [c for c in claims if c["endpoint_id"] == e.id and c.get("phase") in {"selected", "running"}
                      and (trace is None or c["request_id"] != trace.request_id)]
            running = st.detail.get("running", st.detail.get("processing", 0)) or 0
            if st.load_headroom > 0 and max(running, len(active)) < e.max_concurrency:
                row["queue_s"] = 0.0
            elif running <= len(active) and active:
                remaining = []
                for c in active:
                    history = await self.samples(e.id, st.cache_generation, c["prompt_bucket"], c["output_bucket"], c.get("cache_assumption", "cold"))
                    if len(history) < 5 or not c.get("started_at"):
                        break
                    service = sorted(s["duration_s"] for s in history)[math.ceil(len(history) * .75) - 1]
                    elapsed = time.time() - c["started_at"]
                    if elapsed >= service:
                        break  # Overrunning requests have unknown remaining time, never zero.
                    remaining.append(service - elapsed)
                if len(remaining) == len(active):
                    row["queue_s"] = sorted(remaining)[max(0, len(active) - e.max_concurrency)]
            if row["queue_s"] is not None:
                row["total_s"] = row["queue_s"] + row["first_s"]
            else:
                row["unavailable_reason"] = "unknown_remaining_service_time"
            result.append(row)
        return result

    async def alternative(self, original, endpoints, statuses, *, trace, conversation, prompt_tokens, output_tokens):
        if trace is None or not self.member(original):
            return None
        endpoints = [e for e in endpoints if self.member(e)]
        rows = await self.costs(endpoints, statuses, trace=trace, conversation=conversation,
                                prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        info = trace.payload.setdefault("local_pool", {"group": "local-peers"})
        info.pop("target", None)
        info.pop("selection", None)
        info.pop("wait_reason", None)
        info.pop("estimated_saving_s", None)
        info.update(costs=rows, generation=statuses[original.id].cache_generation)
        origin = next((r for r in rows if r["endpoint_id"] == original.id), None)
        if not origin or origin["total_s"] is None:
            info["wait_reason"] = "insufficient_cost_evidence_keep_affinity"
            return None
        if origin["queue_s"] == 0:
            info["wait_reason"] = "original_device_available_keep_affinity"
            return None
        candidates = [r for r in rows if r["endpoint_id"] != original.id and r["total_s"] is not None]
        if not candidates:
            return None
        best = min(candidates, key=lambda r: r["total_s"])
        saved = origin["total_s"] - best["total_s"]
        if saved >= float(self.config.get("min_saving_seconds", 5)) and saved >= origin["total_s"] * float(self.config.get("min_saving_ratio", .2)):
            info.update(selection="estimated_faster_first_output", estimated_saving_s=saved, target=best["endpoint_id"])
            return next(e for e in endpoints if e.id == best["endpoint_id"])
        info["wait_reason"] = "migration_not_materially_faster"
        return None
