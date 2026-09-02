from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from typing import Any

import httpx

from .store import StateStore
from .types import Endpoint, EndpointStatus


_VLLM_METRIC_PATTERNS = {
    "running": re.compile(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "waiting": re.compile(r"^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "kv": re.compile(r"^vllm:kv_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prefix_queries": re.compile(r"^vllm:prefix_cache_queries_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prefix_hits": re.compile(r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
}


class HealthMonitor:
    def __init__(
        self,
        store: StateStore,
        *,
        refresh_seconds: float = 5.0,
        stale_after_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.refresh_seconds = refresh_seconds
        self.stale_after_seconds = stale_after_seconds
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0))

    async def statuses(
        self,
        endpoints: list[Endpoint] | tuple[Endpoint, ...],
        *,
        force_refresh: bool = False,
    ) -> dict[str, EndpointStatus]:
        values = await asyncio.gather(
            *(
                self.status(item, force_refresh=force_refresh)
                for item in endpoints
            )
        )
        return {item.endpoint_id: item for item in values}

    async def status(
        self,
        endpoint: Endpoint,
        *,
        force_refresh: bool = False,
    ) -> EndpointStatus:
        key = f"router:health:{endpoint.id}"
        now = time.time()
        if not force_refresh:
            cached = await self.store.get_json(key)
            if cached:
                value = EndpointStatus.from_dict(cached)
                if now - value.checked_at <= self.refresh_seconds:
                    return value
        value = await self._probe(endpoint)
        await self.store.set_json(key, value.to_dict(), ttl_seconds=max(30, int(self.stale_after_seconds * 3)))
        return value

    async def mark_failure(self, endpoint_id: str, cooldown_seconds: int = 20) -> None:
        await self.store.set_json(
            f"router:cooldown:{endpoint_id}",
            {"until": time.time() + cooldown_seconds},
            ttl_seconds=cooldown_seconds,
        )

    async def in_cooldown(self, endpoint_id: str) -> bool:
        value = await self.store.get_json(f"router:cooldown:{endpoint_id}")
        return bool(value and float(value.get("until", 0)) > time.time())

    async def prefix_cache_counters(
        self,
        endpoint: Endpoint,
    ) -> dict[str, float] | None:
        if endpoint.backend_type != "vllm" or not endpoint.load_url:
            return None
        try:
            response = await self.client.get(endpoint.load_url)
            response.raise_for_status()
        except Exception:
            return None
        return {
            "queries": _metric(response.text, "prefix_queries"),
            "hits": _metric(response.text, "prefix_hits"),
        }

    async def _probe(self, endpoint: Endpoint) -> EndpointStatus:
        checked_at = time.time()
        try:
            if endpoint.backend_type == "ai_pool":
                return await self._probe_ai_pool(endpoint, checked_at)
            if endpoint.backend_type == "codex_pool":
                return await self._probe_codex_pool(endpoint, checked_at)
            if endpoint.backend_type == "vllm":
                return await self._probe_vllm(endpoint, checked_at)
            if endpoint.backend_type == "llama_cpp":
                return await self._probe_llama_cpp(endpoint, checked_at)
            headers = {}
            api_key = os.environ.get(endpoint.backend_api_key_env, "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = await self.client.get(
                endpoint.health_url,
                headers=headers,
            )
            return EndpointStatus(
                endpoint_id=endpoint.id,
                healthy=response.is_success,
                checked_at=checked_at,
                cache_generation=_generation(response.headers.get("server", ""), response.headers.get("date", "")),
                detail={"status_code": response.status_code},
            )
        except Exception as exc:
            return EndpointStatus(
                endpoint_id=endpoint.id,
                healthy=False,
                checked_at=checked_at,
                load_headroom=0,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )

    async def _probe_ai_pool(self, endpoint: Endpoint, checked_at: float) -> EndpointStatus:
        response = await self.client.get(endpoint.health_url)
        response.raise_for_status()
        payload = response.json()
        workers = payload.get("workers", [])
        available = [
            worker
            for worker in workers
            if worker.get("ready") and worker.get("state") == "available"
        ]
        ready = [worker for worker in workers if worker.get("ready")]
        eligible_context = max(
            (int(worker.get("safe_context_tokens", 0)) for worker in available),
            default=0,
        )
        worker_fingerprint = "|".join(
            f"{item.get('worker_id')}:{item.get('state')}:{item.get('safe_context_tokens')}"
            for item in workers
        )
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=bool(payload.get("ok")) and bool(available),
            checked_at=checked_at,
            load_headroom=len(available) / max(1, len(ready)),
            latency_score=0.5,
            cache_generation=_generation(
                str(payload.get("runtime_fingerprint", "")),
                worker_fingerprint,
            ),
            eligible_context_tokens=eligible_context,
            detail={
                "ready_workers": len(ready),
                "available_workers": len(available),
                "available_worker_ids": [item.get("worker_id") for item in available],
                "workers": [
                    {
                        "worker_id": item.get("worker_id"),
                        "port": item.get("port"),
                        "priority": int(item.get("priority", 999)),
                        "ready": bool(item.get("ready")),
                        "state": item.get("state"),
                        "safe_context_tokens": int(
                            item.get("safe_context_tokens", 0)
                        ),
                    }
                    for item in workers
                ],
            },
        )

    async def _probe_codex_pool(
        self,
        endpoint: Endpoint,
        checked_at: float,
    ) -> EndpointStatus:
        headers = {}
        api_key = os.environ.get(endpoint.backend_api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = await self.client.get(
            endpoint.health_url,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        workers = [
            item
            for item in payload.get("workers", [])
            if isinstance(item, dict)
        ]
        available = [
            item
            for item in workers
            if item.get("ready") and item.get("state") == "available"
        ]
        ready = [item for item in workers if item.get("ready")]
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=bool(payload.get("ok")) and bool(available),
            checked_at=checked_at,
            load_headroom=len(available) / max(1, len(ready)),
            latency_score=0.5,
            cache_generation=_generation(
                str(payload.get("model", "")),
                "|".join(
                    f"{item.get('worker_id')}:{item.get('state')}"
                    for item in workers
                ),
            ),
            eligible_context_tokens=max(
                (
                    int(item.get("safe_context_tokens", 0))
                    for item in available
                ),
                default=0,
            ),
            detail={
                "ready_workers": len(ready),
                "available_workers": len(available),
                "available_worker_ids": [
                    item.get("worker_id") for item in available
                ],
                "workers": [
                    {
                        "worker_id": item.get("worker_id"),
                        "account_alias": item.get("account_alias"),
                        "api_base": item.get("api_base"),
                        "ready": bool(item.get("ready")),
                        "state": item.get("state"),
                        "safe_context_tokens": int(
                            item.get("safe_context_tokens", 0)
                        ),
                        "error_code": item.get("error_code"),
                        "cooldown_until": item.get("cooldown_until"),
                    }
                    for item in workers
                ],
            },
        )

    async def _probe_vllm(self, endpoint: Endpoint, checked_at: float) -> EndpointStatus:
        health_response, metrics_response = await asyncio.gather(
            self.client.get(endpoint.health_url),
            self.client.get(endpoint.load_url or endpoint.health_url),
        )
        health_response.raise_for_status()
        metrics_response.raise_for_status()
        metrics = metrics_response.text
        running = _metric(metrics, "running")
        waiting = _metric(metrics, "waiting")
        kv_usage = _metric(metrics, "kv")
        prefix_queries = _metric(metrics, "prefix_queries")
        prefix_hits = _metric(metrics, "prefix_hits")
        capacity = max(1, endpoint.max_concurrency)
        load = min(1.0, (running + waiting) / capacity)
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=checked_at,
            load_headroom=max(0.0, 1.0 - load),
            latency_score=max(0.0, 1.0 - min(1.0, waiting / capacity)),
            cache_generation=_generation(
                health_response.headers.get("server", ""),
                str(endpoint.metadata.get("runtime_generation", "")),
            ),
            eligible_context_tokens=endpoint.safe_context_tokens,
            detail={
                "running": running,
                "waiting": waiting,
                "kv_usage": kv_usage,
                "prefix_cache_queries": prefix_queries,
                "prefix_cache_hits": prefix_hits,
            },
        )

    async def _probe_llama_cpp(self, endpoint: Endpoint, checked_at: float) -> EndpointStatus:
        health_response, slots_response = await asyncio.gather(
            self.client.get(endpoint.health_url),
            self.client.get(endpoint.load_url or endpoint.health_url),
        )
        health_response.raise_for_status()
        slots_response.raise_for_status()
        slots = slots_response.json()
        processing = sum(bool(item.get("is_processing")) for item in slots)
        slot_ids = [str(item.get("id")) for item in slots]
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=checked_at,
            load_headroom=1.0 if processing == 0 else 0.0,
            latency_score=1.0 if processing == 0 else 0.0,
            cache_generation=_generation(
                health_response.headers.get("server", ""),
                ",".join(slot_ids),
            ),
            eligible_context_tokens=min(
                endpoint.safe_context_tokens,
                max((int(item.get("n_ctx", 0)) for item in slots), default=0),
            ),
            detail={"processing": processing, "slots": len(slots)},
        )


def _metric(text: str, name: str) -> float:
    match = _VLLM_METRIC_PATTERNS[name].search(text)
    return float(match.group(1)) if match else 0.0


def _generation(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:16]
