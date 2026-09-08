from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from typing import Any

import httpx

from .store import StateStore
from .types import (
    DeploymentProfile,
    Endpoint,
    EndpointStatus,
    PhysicalDeployment,
)


_VLLM_METRIC_PATTERNS = {
    "running": re.compile(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "waiting": re.compile(r"^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "kv": re.compile(r"^vllm:kv_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prefix_queries": re.compile(r"^vllm:prefix_cache_queries_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prefix_hits": re.compile(r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "external_prefix_queries": re.compile(r"^vllm:external_prefix_cache_queries_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "external_prefix_hits": re.compile(r"^vllm:external_prefix_cache_hits_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prompt_tokens_cached": re.compile(r"^vllm:prompt_tokens_cached_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.MULTILINE),
    "prompt_tokens_local_compute": re.compile(r'^vllm:prompt_tokens_by_source_total\{[^}]*source="local_compute"[^}]*\}\s+([0-9.eE+-]+)$', re.MULTILINE),
    "prompt_tokens_local_cache_hit": re.compile(r'^vllm:prompt_tokens_by_source_total\{[^}]*source="local_cache_hit"[^}]*\}\s+([0-9.eE+-]+)$', re.MULTILINE),
    "prompt_tokens_external_transfer": re.compile(r'^vllm:prompt_tokens_by_source_total\{[^}]*source="external_kv_transfer"[^}]*\}\s+([0-9.eE+-]+)$', re.MULTILINE),
    "process_start": re.compile(r"^process_start_time_seconds\s+([0-9.eE+-]+)$", re.MULTILINE),
}

_LMCACHE_METRIC_PATTERNS = {
    "lookup_requested_tokens": re.compile(
        r"^lmcache_mp_lookup_requested_tokens_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "lookup_hit_tokens": re.compile(
        r"^lmcache_mp_lookup_hit_tokens_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "l1_read_chunks": re.compile(
        r"^lmcache_mp_l1_read_chunks_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "l1_write_chunks": re.compile(
        r"^lmcache_mp_l1_write_chunks_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "l1_memory_usage_bytes": re.compile(
        r"^lmcache_mp_l1_memory_usage_bytes(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "l1_usage_ratio": re.compile(
        r"^lmcache_mp_l1_usage_ratio(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "process_start": re.compile(
        r"^process_start_time_seconds(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
}


class HealthMonitor:
    def __init__(
        self,
        store: StateStore,
        *,
        refresh_seconds: float = 5.0,
        stale_after_seconds: float = 15.0,
        probe_timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.refresh_seconds = refresh_seconds
        self.stale_after_seconds = stale_after_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self._vllm_progress: dict[str, dict[str, Any]] = {}
        self._vllm_watch_tasks: dict[str, asyncio.Task] = {}
        self._vllm_watch_targets: dict[str, tuple] = {}
        self._vllm_probe_locks: dict[str, asyncio.Lock] = {}
        self._closing = False
        self._owns_client = client is None
        self.client = client or self._new_client()

    def configure(
        self,
        *,
        refresh_seconds: float,
        stale_after_seconds: float,
        probe_timeout_seconds: float,
    ) -> None:
        self.refresh_seconds = refresh_seconds
        self.stale_after_seconds = stale_after_seconds
        if probe_timeout_seconds == self.probe_timeout_seconds:
            return
        self.probe_timeout_seconds = probe_timeout_seconds
        if self._owns_client:
            self.client.timeout = self._timeout()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout())

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.probe_timeout_seconds,
            connect=min(2.0, self.probe_timeout_seconds),
        )

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
        now = time.time()
        if not endpoint.enabled and not force_refresh:
            return EndpointStatus(
                endpoint_id=endpoint.id,
                healthy=False,
                checked_at=now,
                load_headroom=0,
                detail={"disabled": True},
            )
        key = f"router:health:{endpoint.id}"
        if not force_refresh:
            cached = await self.store.get_json(key)
            if cached:
                value = EndpointStatus.from_dict(cached)
                if now - value.checked_at <= self.refresh_seconds:
                    return value
        if endpoint.backend_type == "vllm":
            value = await self._sample_vllm_status(endpoint)
        else:
            value = await self._probe(endpoint)
            await self.store.set_json(key, value.to_dict(), ttl_seconds=max(30, int(self.stale_after_seconds * 3)))
        self._manage_vllm_watch(endpoint, value)
        return value

    async def close(self) -> None:
        self._closing = True
        current_task = asyncio.current_task()
        tasks = [task for task in self._vllm_watch_tasks.values() if task is not current_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._vllm_watch_tasks.clear()
        self._vllm_watch_targets.clear()
        self._vllm_progress.clear()
        if self._owns_client:
            await self.client.aclose()

    async def _sample_vllm_status(self, endpoint: Endpoint) -> EndpointStatus:
        # Foreground and watch probes must publish in the same order they sample.
        lock = self._vllm_probe_locks.setdefault(endpoint.id, asyncio.Lock())
        async with lock:
            value = await self._probe(endpoint)
            await self.store.set_json(
                f"router:health:{endpoint.id}", value.to_dict(),
                ttl_seconds=max(30, int(self.stale_after_seconds * 3)),
            )
            return value

    @staticmethod
    def _vllm_watch_needed(value: EndpointStatus) -> bool:
        progress = value.detail.get("backend_progress", {})
        return bool(value.healthy and progress.get("running") == 0
                    and progress.get("waiting", 0) > 0
                    and progress.get("state") in {"observing", "counter_reset", "observation_reset"})

    def _manage_vllm_watch(self, endpoint: Endpoint, value: EndpointStatus) -> None:
        task = self._vllm_watch_tasks.get(endpoint.id)
        target = (endpoint.health_url, endpoint.load_url, endpoint.safe_context_tokens,
                  endpoint.max_concurrency, repr(endpoint.metadata))
        needed = (not self._closing and endpoint.enabled and endpoint.backend_type == "vllm"
                  and self._vllm_watch_needed(value))
        if task is not None and not task.done():
            if needed and self._vllm_watch_targets.get(endpoint.id) == target:
                return
            if task is not asyncio.current_task():
                task.cancel()
        if not needed:
            return
        task = asyncio.create_task(self._watch_vllm(endpoint), name=f"vllm-progress:{endpoint.id}")
        self._vllm_watch_tasks[endpoint.id] = task
        self._vllm_watch_targets[endpoint.id] = target
        task.add_done_callback(lambda finished, key=endpoint.id: self._finish_vllm_watch(key, finished))

    def _finish_vllm_watch(self, endpoint_id: str, task: asyncio.Task) -> None:
        if self._vllm_watch_tasks.get(endpoint_id) is task:
            self._vllm_watch_tasks.pop(endpoint_id, None)
            self._vllm_watch_targets.pop(endpoint_id, None)

    async def _watch_vllm(self, endpoint: Endpoint) -> None:
        # No recursive status() calls: each suspicious endpoint owns one bounded
        # observer. A terminal unhealthy sample is published before stopping;
        # a later foreground probe can establish recovery.
        state = self._vllm_progress.get(endpoint.id, {})
        deadline = time.monotonic() + max(120.0, state.get("window", 30.0) + self.refresh_seconds * 2)
        try:
            while not self._closing and endpoint.enabled:
                await asyncio.sleep(max(0.1, self.refresh_seconds))
                if self._closing or time.monotonic() > deadline:
                    break
                value = await self._sample_vllm_status(endpoint)
                if not self._vllm_watch_needed(value):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            # HTTP failures are published by _probe. A store failure cannot leave
            # an unobserved task exception or preserve a partial observation.
            self._vllm_progress.pop(endpoint.id, None)

    async def mark_failure(self, endpoint_id: str, cooldown_seconds: int = 20) -> None:
        await self.store.set_json(
            f"router:cooldown:{endpoint_id}",
            {"until": time.time() + cooldown_seconds},
            ttl_seconds=cooldown_seconds,
        )

    async def in_cooldown(self, endpoint_id: str) -> bool:
        value = await self.store.get_json(f"router:cooldown:{endpoint_id}")
        return bool(value and float(value.get("until", 0)) > time.time())

    async def mark_capability_failure(
        self,
        deployment_id: str,
        capability: str,
        cooldown_seconds: int = 20,
    ) -> None:
        await self.store.set_json(
            f"router:cooldown:{deployment_id}:{capability}",
            {"until": time.time() + cooldown_seconds},
            ttl_seconds=cooldown_seconds,
        )

    async def in_capability_cooldown(
        self,
        deployment_id: str,
        capability: str,
    ) -> bool:
        value = await self.store.get_json(
            f"router:cooldown:{deployment_id}:{capability}"
        )
        return bool(
            value and float(value.get("until", 0)) > time.time()
        )

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
            if endpoint.backend_type == "vllm":
                self._vllm_progress.pop(endpoint.id, None)
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
        pool_fingerprint = str(payload.get("runtime_fingerprint", ""))
        worker_values = [
            worker
            for worker in payload.get("workers", [])
            if isinstance(worker, dict)
        ]
        processing = await asyncio.gather(
            *(
                self._ai_worker_is_processing(worker)
                for worker in worker_values
            )
        )
        worker_values = [
            (
                {**worker, "state": "busy"}
                if is_processing and worker.get("state") == "available"
                else worker
            )
            for worker, is_processing in zip(
                worker_values,
                processing,
                strict=True,
            )
        ]
        workers = [
            _physical_deployment(
                endpoint,
                worker,
                pool_fingerprint=pool_fingerprint,
            )
            for worker in worker_values
        ]
        available = [
            worker
            for worker in workers
            if (
                worker.schedulable
                and worker.state == "available"
            )
        ]
        ready = [worker for worker in workers if worker.ready]
        schedulable = [worker for worker in workers if worker.schedulable]
        eligible_context = max(
            (worker.safe_context_tokens for worker in schedulable),
            default=0,
        )
        worker_fingerprint = "|".join(
            (
                f"{item.worker_id}:{item.state}:"
                f"{item.safe_context_tokens}:{item.runtime_fingerprint}"
            )
            for item in workers
        )
        effective_modalities = sorted(
            {
                modality
                for item in schedulable
                for modality in item.modalities
            }
        )
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=bool(payload.get("ok")) and bool(schedulable),
            checked_at=checked_at,
            load_headroom=len(available) / max(1, len(schedulable)),
            latency_score=0.5,
            cache_generation=_generation(
                pool_fingerprint,
                worker_fingerprint,
            ),
            eligible_context_tokens=eligible_context,
            detail={
                "ready_workers": len(ready),
                "schedulable_workers": len(schedulable),
                "available_workers": len(available),
                "available_worker_ids": [
                    item.worker_id for item in available
                ],
                "effective_modalities": effective_modalities,
                "workers": [item.to_dict() for item in workers],
            },
        )

    async def _ai_worker_is_processing(
        self,
        worker: dict[str, Any],
    ) -> bool:
        api_base = str(worker.get("api_base") or "").rstrip("/")
        if api_base.endswith("/v1"):
            root = api_base[:-3].rstrip("/")
        else:
            root = str(worker.get("url") or "").rstrip("/")
        if not root and worker.get("port") is not None:
            root = f"http://127.0.0.1:{int(worker['port'])}"
        if not root:
            return False
        headers = {}
        key_env = str(worker.get("backend_api_key_env", ""))
        api_key = os.environ.get(key_env, "") if key_env else ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = await self.client.get(
                f"{root}/slots",
                headers=headers,
            )
            response.raise_for_status()
            slots = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return False
        return bool(
            isinstance(slots, list)
            and any(
                isinstance(slot, dict)
                and bool(slot.get("is_processing"))
                for slot in slots
            )
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
        workers = [
            item
            for item in workers
            if (
                endpoint.provider_model
                in {
                    str(model)
                    for model in item.get("models", [])
                }
                or (
                    not item.get("models")
                    and str(payload.get("model", ""))
                    == endpoint.provider_model
                )
            )
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
                endpoint.provider_model,
                "|".join(
                    (
                        f"{item.get('worker_id')}:{item.get('state')}:"
                        + ",".join(
                            str(model)
                            for model in item.get("models", [])
                        )
                    )
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
                        "models": [
                            str(model)
                            for model in item.get("models", [])
                        ],
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

    def _observe_vllm_progress(self, endpoint: Endpoint, metrics: str) -> dict[str, Any]:
        # These observations are local to this monitor: monotonic timestamps must
        # never be persisted/shared as wall-clock data between Router processes.
        configured = endpoint.metadata.get("backend_no_progress_seconds", 30.0)
        try:
            window = float(configured) if not isinstance(configured, bool) else 0.0
        except (TypeError, ValueError, OverflowError):
            window = 0.0
        if not math.isfinite(window) or window <= 0:
            window = 30.0
        now = time.monotonic()
        samples = _vllm_progress_samples(metrics)
        detail: dict[str, Any] = {"state": "unavailable", "window_seconds": window,
                                  "no_progress_seconds": None}
        if samples is None:
            self._vllm_progress.pop(endpoint.id, None)
            return detail
        running, waiting, counters, process_start = samples
        detail.update(running=running, waiting=waiting)
        if running != 0 or waiting <= 0:
            self._vllm_progress.pop(endpoint.id, None)
            detail["state"] = "running" if running else "idle"
            return detail
        generation = (endpoint.health_url, endpoint.load_url,
                      str(endpoint.metadata.get("runtime_generation", "")), process_start)
        previous = self._vllm_progress.get(endpoint.id)
        since = now
        state = "observing"
        max_gap = max(window, self.stale_after_seconds, self.refresh_seconds * 3,
                      self.probe_timeout_seconds * 2)
        if previous is not None:
            comparable = (previous["generation"] == generation
                          and previous["window"] == window
                          and previous["counters"].keys() == counters.keys()
                          and 0 <= now - previous["sampled_at"]
                          and (previous.get("stalled") or now - previous["sampled_at"] <= max_gap))
            if comparable:
                old = previous["counters"]
                if any(value < old[key] for key, value in counters.items()):
                    state = "counter_reset"
                elif any(value > old[key] for key, value in counters.items()):
                    state = "progressing"
                else:
                    since = previous["since"]
            else:
                state = "observation_reset"
        self._vllm_progress[endpoint.id] = {
            "generation": generation, "counters": counters, "sampled_at": now,
            "since": since, "window": window, "stalled": now - since >= window,
        }
        elapsed = max(0.0, now - since)
        detail.update(state="backend_no_progress" if elapsed >= window else state,
                      no_progress_seconds=elapsed)
        return detail

    async def _probe_vllm(self, endpoint: Endpoint, checked_at: float) -> EndpointStatus:
        health_response, metrics_response, lmcache = await asyncio.gather(
            self.client.get(endpoint.health_url),
            self.client.get(endpoint.load_url or endpoint.health_url),
            self._probe_lmcache(endpoint),
        )
        health_response.raise_for_status()
        metrics_response.raise_for_status()
        metrics = metrics_response.text
        running = _metric(metrics, "running")
        waiting = _metric(metrics, "waiting")
        kv_usage = _metric(metrics, "kv")
        prefix_queries = _metric(metrics, "prefix_queries")
        prefix_hits = _metric(metrics, "prefix_hits")
        external_prefix_queries = _metric(
            metrics,
            "external_prefix_queries",
        )
        external_prefix_hits = _metric(metrics, "external_prefix_hits")
        prompt_tokens_external_transfer = _metric(
            metrics,
            "prompt_tokens_external_transfer",
        )
        lmcache["connector_active"] = bool(
            lmcache.get("healthy") and lmcache.get("registered")
        )
        progress = self._observe_vllm_progress(endpoint, metrics)
        running = progress.get("running", running)
        waiting = progress.get("waiting", waiting)
        stalled = progress["state"] == "backend_no_progress"
        capacity = max(1, endpoint.max_concurrency)
        load = min(1.0, (running + waiting) / capacity)
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=not stalled,
            checked_at=checked_at,
            load_headroom=0.0 if stalled else max(0.0, 1.0 - load),
            latency_score=0.0 if stalled else max(0.0, 1.0 - min(1.0, waiting / capacity)),
            cache_generation=_generation(
                str(_metric(metrics, "process_start")),
                str(endpoint.metadata.get("runtime_generation", "")),
                str(lmcache.get("generation", "")),
            ),
            eligible_context_tokens=endpoint.safe_context_tokens,
            detail={
                "running": running,
                "waiting": waiting,
                "kv_usage": kv_usage,
                "prefix_cache_queries": prefix_queries,
                "prefix_cache_hits": prefix_hits,
                "external_prefix_cache_queries": external_prefix_queries,
                "external_prefix_cache_hits": external_prefix_hits,
                "prompt_tokens_cached": _metric(
                    metrics,
                    "prompt_tokens_cached",
                ),
                "prompt_tokens_local_compute": _metric(
                    metrics,
                    "prompt_tokens_local_compute",
                ),
                "prompt_tokens_local_cache_hit": _metric(
                    metrics,
                    "prompt_tokens_local_cache_hit",
                ),
                "prompt_tokens_external_transfer": (
                    prompt_tokens_external_transfer
                ),
                "lmcache": lmcache,
                "backend_progress": progress,
                **({"reason": "backend_no_progress"} if stalled else {}),
            },
        )

    async def _probe_lmcache(self, endpoint: Endpoint) -> dict[str, Any]:
        base_url = str(
            endpoint.metadata.get("lmcache_http_url", "")
        ).rstrip("/")
        if not base_url:
            return {"supported": False, "healthy": False}
        try:
            status_response, metrics_response = await asyncio.gather(
                self.client.get(f"{base_url}/status"),
                self.client.get(f"{base_url}/metrics"),
            )
            status_response.raise_for_status()
            metrics_response.raise_for_status()
            payload = status_response.json()
            if not isinstance(payload, dict):
                raise ValueError("LMCache status must be an object")
            storage = payload.get("storage_manager", {})
            l1 = (
                storage.get("l1_manager", {})
                if isinstance(storage, dict)
                else {}
            )
            contexts = payload.get("cache_context_meta", {})
            registered_gpu_ids = payload.get("registered_gpu_ids", [])
            registered_count = max(
                len(contexts) if isinstance(contexts, dict) else 0,
                (
                    len(registered_gpu_ids)
                    if isinstance(registered_gpu_ids, list)
                    else 0
                ),
            )
            expected_registrations = int(
                endpoint.metadata.get(
                    "lmcache_expected_registrations",
                    1,
                )
            )
            registered = registered_count >= expected_registrations
            metrics = metrics_response.text
            process_start = _lmcache_metric(metrics, "process_start")
            generation = _generation(
                str(process_start),
                str(payload.get("engine_type", "")),
                str(payload.get("chunk_size", "")),
            )
            return {
                "supported": True,
                "healthy": bool(payload.get("is_healthy", False)),
                "registered": registered,
                "registered_count": registered_count,
                "expected_registrations": expected_registrations,
                "generation": generation,
                "process_start_time_seconds": process_start,
                "chunk_size": int(payload.get("chunk_size", 0)),
                "active_sessions": int(
                    payload.get("active_sessions", 0)
                ),
                "memory_used_bytes": int(
                    l1.get("memory_used_bytes", 0)
                    if isinstance(l1, dict)
                    else 0
                ),
                "memory_total_bytes": int(
                    l1.get("memory_total_bytes", 0)
                    if isinstance(l1, dict)
                    else 0
                ),
                "memory_usage_ratio": float(
                    l1.get("memory_usage_ratio", 0)
                    if isinstance(l1, dict)
                    else 0
                ),
                "lookup_requested_tokens": _lmcache_metric(
                    metrics,
                    "lookup_requested_tokens",
                ),
                "lookup_hit_tokens": _lmcache_metric(
                    metrics,
                    "lookup_hit_tokens",
                ),
                "l1_read_chunks": _lmcache_metric(
                    metrics,
                    "l1_read_chunks",
                ),
                "l1_write_chunks": _lmcache_metric(
                    metrics,
                    "l1_write_chunks",
                ),
                "metrics_memory_used_bytes": _lmcache_metric(
                    metrics,
                    "l1_memory_usage_bytes",
                ),
                "metrics_usage_ratio": _lmcache_metric(
                    metrics,
                    "l1_usage_ratio",
                ),
            }
        except Exception as exc:
            return {
                "supported": True,
                "healthy": False,
                "registered": False,
                "connector_active": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _probe_llama_cpp(self, endpoint: Endpoint, checked_at: float) -> EndpointStatus:
        headers = {}
        api_key = os.environ.get(endpoint.backend_api_key_env, "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        health_response, slots_response = await asyncio.gather(
            self.client.get(endpoint.health_url, headers=headers),
            self.client.get(
                endpoint.load_url or endpoint.health_url,
                headers=headers,
            ),
        )
        health_response.raise_for_status()
        slots_response.raise_for_status()
        slots = slots_response.json()
        processing = sum(bool(item.get("is_processing")) for item in slots)
        slot_ids = [str(item.get("id")) for item in slots]
        task_ids = [
            int(item.get("id_task", 0))
            for item in slots
            if isinstance(item, dict)
        ]
        max_task_id = max(task_ids, default=0)
        generation_key = f"router:cache-generation-state:{endpoint.id}"
        previous = await self.store.get_json(generation_key) or {}
        epoch = max(1, int(previous.get("epoch", 1)))
        if max_task_id < int(previous.get("max_task_id", 0)):
            epoch += 1
        await self.store.set_json(
            generation_key,
            {"epoch": epoch, "max_task_id": max_task_id},
        )
        return EndpointStatus(
            endpoint_id=endpoint.id,
            healthy=True,
            checked_at=checked_at,
            load_headroom=1.0 if processing == 0 else 0.0,
            latency_score=1.0 if processing == 0 else 0.0,
            cache_generation=_generation(
                health_response.headers.get("server", ""),
                ",".join(slot_ids),
                str(epoch),
            ),
            eligible_context_tokens=min(
                endpoint.safe_context_tokens,
                max((int(item.get("n_ctx", 0)) for item in slots), default=0),
            ),
            detail={
                "processing": processing,
                "slots": len(slots),
                "max_task_id": max_task_id,
                "cache_epoch": epoch,
            },
        )


def _vllm_progress_samples(text: str):
    # Unlike the display-only _metric helper, absence/invalid samples are never
    # zero. Keep each engine/label series separate so one reset cannot be hidden
    # by another engine's increase. At least one prefill counter is required;
    # a decode-only counter cannot demonstrate a long prefill is not progressing.
    names = ("num_requests_running", "num_requests_waiting", "prompt_tokens_total",
             "generation_tokens_total", "prompt_tokens_by_source_total",
             "process_start_time_seconds")
    gauges: dict[str, dict[str, float]] = {}
    counters: dict[str, float] = {}
    process_start = None
    for name in names:
        metric_name = name if name == "process_start_time_seconds" else "vllm:" + name
        pattern = r"^(" + re.escape(metric_name) + r"(?:\{[^}]*\})?)\s+(\S+)\s*$"
        values = {}
        for match in re.finditer(pattern, text, re.MULTILINE):
            series, raw = match.groups()
            if name == "prompt_tokens_by_source_total" and not re.search(r'(?:\{|,)\s*source="local_compute"(?:,|\})', series):
                continue
            try:
                value = float(raw)
            except ValueError:
                return None
            if not math.isfinite(value) or value < 0 or (name != "process_start_time_seconds" and not value.is_integer()):
                return None
            if series in values:
                return None
            values[series] = value
        if name == "process_start_time_seconds":
            process_start = tuple(sorted(values.items())) if values else None
        elif name in {"num_requests_running", "num_requests_waiting"}:
            gauges[name] = values
        else:
            counters.update(values)
    if not all(gauges.values()) or not any(key.startswith(("vllm:prompt_tokens_total", "vllm:prompt_tokens_by_source_total")) for key in counters):
        return None
    return (sum(gauges["num_requests_running"].values()),
            sum(gauges["num_requests_waiting"].values()), counters, process_start)


def _metric(text: str, name: str) -> float:
    match = _VLLM_METRIC_PATTERNS[name].search(text)
    return float(match.group(1)) if match else 0.0


def _lmcache_metric(text: str, name: str) -> float:
    return sum(
        float(match.group(1))
        for match in _LMCACHE_METRIC_PATTERNS[name].finditer(text)
    )


def _physical_deployment(
    endpoint: Endpoint,
    worker: dict[str, Any],
    *,
    pool_fingerprint: str,
) -> PhysicalDeployment:
    tier = str(worker.get("tier", ""))
    profile = _deployment_profile(endpoint, worker, tier)
    profile_id = profile.id if profile else "unmatched"
    expected_context = (
        profile.context_size
        if profile
        else int(worker.get("context_size", 0))
    )
    expected_safe_context = (
        profile.safe_context_tokens
        if profile
        else int(worker.get("safe_context_tokens", 0))
    )
    expected_cache_k = (
        profile.cache_type_k
        if profile
        else str(worker.get("cache_type_k", ""))
    )
    expected_cache_v = (
        profile.cache_type_v
        if profile
        else str(worker.get("cache_type_v", ""))
    )
    actual_context = int(
        worker.get("context_size") or expected_context
    )
    actual_safe_context = int(
        worker.get("safe_context_tokens") or expected_safe_context
    )
    actual_cache_k = str(
        worker.get("cache_type_k") or expected_cache_k
    )
    actual_cache_v = str(
        worker.get("cache_type_v") or expected_cache_v
    )
    drift: list[str] = []
    if endpoint.deployment_profiles and profile is None:
        drift.append("unmatched_profile")
    if profile:
        if actual_context != profile.context_size:
            drift.append("context_size")
        if actual_safe_context != profile.safe_context_tokens:
            drift.append("safe_context_tokens")
        if actual_cache_k != profile.cache_type_k:
            drift.append("cache_type_k")
        if actual_cache_v != profile.cache_type_v:
            drift.append("cache_type_v")

    worker_id = str(worker.get("worker_id", ""))
    override = _deployment_override(endpoint, worker_id)
    modalities = tuple(
        str(item)
        for item in override.get(
            "modalities",
            profile.modalities if profile else endpoint.modalities,
        )
    )
    vision_status = str(
        override.get(
            "vision_status",
            profile.vision_status if profile else "unverified",
        )
    )
    max_images_value = override.get(
        "max_images",
        profile.max_images if profile else None,
    )
    max_images = (
        int(max_images_value)
        if max_images_value is not None
        else None
    )
    port_value = worker.get("port")
    port = int(port_value) if port_value is not None else None
    api_base = str(worker.get("api_base") or "").rstrip("/")
    if not api_base:
        worker_url = str(worker.get("url") or "").rstrip("/")
        if worker_url:
            api_base = f"{worker_url}/v1"
        elif port is not None:
            api_base = f"http://127.0.0.1:{port}/v1"
    fingerprint_payload = {
        "pool": pool_fingerprint,
        "worker_id": worker_id,
        "profile_id": profile_id,
        "tier": tier,
        "context_size": actual_context,
        "safe_context_tokens": actual_safe_context,
        "cache_type_k": actual_cache_k,
        "cache_type_v": actual_cache_v,
        "gpu_uuids": worker.get("gpu_uuids", []),
        "modalities": modalities,
        "max_images": max_images,
    }
    deployment_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return PhysicalDeployment(
        worker_id=worker_id,
        api_base=api_base,
        profile_id=profile_id,
        tier=tier,
        priority=int(worker.get("priority", 999)),
        gpu_ids=tuple(str(item) for item in worker.get("gpu_ids", [])),
        gpu_uuids=tuple(
            str(item) for item in worker.get("gpu_uuids", [])
        ),
        names=tuple(str(item) for item in worker.get("names", [])),
        port=port,
        context_size=actual_context,
        safe_context_tokens=actual_safe_context,
        cache_type_k=actual_cache_k,
        cache_type_v=actual_cache_v,
        modalities=modalities,
        vision_status=vision_status,
        max_images=max_images,
        runtime_fingerprint=deployment_fingerprint,
        ready=bool(worker.get("ready")),
        state=str(worker.get("state", "unknown")),
        backend_api_key_env=str(
            override.get(
                "backend_api_key_env",
                worker.get("backend_api_key_env", ""),
            )
        ),
        cache_generation=str(worker.get("cache_generation", "")),
        prefill_tokens_per_second=float(
            worker.get("prefill_tokens_per_second", 1.0)
        ),
        config_drift=tuple(drift),
        short_request_rank=(
            profile.short_request_rank
            if profile
            else int(worker.get("priority", 999))
        ),
        error_code=(
            str(worker["error_code"])
            if worker.get("error_code")
            else None
        ),
        cooldown_until=(
            float(worker["cooldown_until"])
            if worker.get("cooldown_until") is not None
            else None
        ),
    )


def _deployment_profile(
    endpoint: Endpoint,
    worker: dict[str, Any],
    tier: str,
) -> DeploymentProfile | None:
    requested = str(worker.get("profile_id", ""))
    if requested:
        return next(
            (
                item
                for item in endpoint.deployment_profiles
                if item.id == requested
            ),
            None,
        )
    return next(
        (
            item
            for item in endpoint.deployment_profiles
            if item.matches(tier)
        ),
        None,
    )


def _deployment_override(
    endpoint: Endpoint,
    worker_id: str,
) -> dict[str, Any]:
    values = endpoint.metadata.get("deployment_overrides", {})
    if not isinstance(values, dict):
        return {}
    value = values.get(worker_id, {})
    return value if isinstance(value, dict) else {}


def _generation(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:16]
