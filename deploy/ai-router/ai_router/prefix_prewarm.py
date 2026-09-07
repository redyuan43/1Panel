"""Best-effort preparation of a configured overflow worker using live request bytes."""
from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx


class PrefixPrewarmer:
    def __init__(self, runtime):
        self.runtime = runtime
        self.tasks = set()

    def submit(self, decision, body, *, client_id, request_id, api_kind):
        pin = decision.endpoint.metadata.get("client_deployment_pin") or {}
        threshold = pin.get("prewarm_min_prompt_tokens")
        if (not threshold or self.runtime.draining or self.tasks
            or api_kind != "chat" or body.get("cache_prompt") is False
            or decision.deployment_id != pin.get("deployment_id")
            or decision.prompt_tokens < threshold
            or not pin.get("context_overflow_deployment_id")):
            return
        # The gateway validates the native template and token boundary. A missing
        # Router tokenizer signature must not disable that independent preparation.
        # Retain only bytes from this live, already-routed request, never a fabricated prompt.
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        task = asyncio.create_task(self._prepare(decision, raw, client_id, request_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _prepare(self, decision, raw, client_id, request_id):
        current = self.runtime
        pin = decision.endpoint.metadata["client_deployment_pin"]
        target_id = pin["context_overflow_deployment_id"]
        tracking_id = "prefix-prepare:" + uuid4().hex
        lease = None
        tracked = False
        try:
            if current.draining:
                return
            status = await current.health.status(decision.endpoint)
            candidates = await current.policy._eligible_physical_deployments(
                decision.endpoint, status,
                required_context=decision.prompt_tokens,
                modalities={"text"}, image_count=0,
                excluded_deployment_ids=set(), require_available=True,
            )
            target = next((x for x in candidates if x.worker_id == target_id), None)
            if target is None or await current.draining_marker(target_id):
                return
            key = os.environ.get(target.backend_api_key_env, "")
            if not key:
                return
            lease = await current.scheduler.begin_request(None)
            # Never queue a warmup ahead of normal model requests.
            if not await current.scheduler.try_acquire_deployment_candidates(lease, (target_id,)):
                return
            if not await current.store.acquire_lock(
                f"router:prefix-prepare-rate:{client_id}:{target_id}", tracking_id, 120,
            ):
                return
            await current.track_request_started(tracking_id, request_id + ":prefix-prepare", None)
            tracked = True
            await current.track_request_routed(
                tracking_id, requested_model=decision.requested_model,
                selected_model=decision.endpoint.public_model,
                endpoint_id=decision.endpoint.id, deployment_id=target_id,
                node=decision.endpoint.node, task="prefix-prepare", reason="context_overflow_prepare",
                affinity="background", prompt_tokens=decision.prefix_affinity_prefix_tokens,
                output_reserve_tokens=0,
            )
            parts = urlsplit(target.api_base)
            root = urlunsplit((parts.scheme, parts.netloc, parts.path.removesuffix("/v1").rstrip("/"), "", ""))
            response = await current.internal_client.post(
                root + "/cache/prepare", content=raw,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                timeout=httpx.Timeout(180, connect=10),
            )
            response.raise_for_status()
            result = response.json()
            current.audit.write(
                "prefix_overflow_prepared", request_id=request_id, client_id=client_id,
                deployment_id=target_id,
                cache_event=result.get("event"),
                **{k: result.get(k) for k in ("fixed_tokens", "prime_tokens", "reused_tokens", "seconds")},
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            current.audit.write(
                "prefix_overflow_prepare_failed", request_id=request_id,
                deployment_id=target_id, error_type=type(error).__name__,
            )
        finally:
            if lease is not None:
                await lease.release()
            if tracked:
                await current.track_request_finished(tracking_id)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
