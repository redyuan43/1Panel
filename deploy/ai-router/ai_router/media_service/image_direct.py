"""Single generation jobs. Workflows and model loading belong to executors."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..image_generation import cloud_allowed, route

from .contracts import ID_PATTERN, MediaError, TERMINAL, UnknownOutcome, image_info


SINGLE = "single_generation"
logger = logging.getLogger(__name__)


def private_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
    private = (parsed.hostname or "").endswith(".taild500c8.ts.net") or parsed.hostname == "localhost" or bool(
        address and (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10"))
    )
    if (parsed.scheme not in {"http", "https"} or not private or parsed.path not in {"", "/"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Media executors must use a loopback or Tailscale endpoint")
    return value.rstrip("/")


def load_pool(path: str | None = None) -> list[dict]:
    path = path or os.environ.get("AI_ROUTER_IMAGE_EXECUTORS_FILE")
    if not path:
        return []
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) != {"version", "executors"} or value["version"] != 1:
        raise ValueError("Invalid media executor configuration")
    result, identifiers = [], set()
    for endpoint in value["executors"]:
        required = {"id", "adapter", "url", "key_env", "resource_id", "model", "capabilities", "enabled", "qualified"}
        if not isinstance(endpoint, dict) or set(endpoint) != required:
            raise ValueError("Invalid media executor fields")
        for field in ("id", "resource_id", "key_env"):
            if not isinstance(endpoint[field], str) or not ID_PATTERN.fullmatch(endpoint[field]):
                raise ValueError("Invalid media executor identifier")
        if endpoint["id"] in identifiers:
            raise ValueError("Duplicate media executor")
        identifiers.add(endpoint["id"])
        if (endpoint["adapter"], endpoint["model"]) not in {
            ("comfy-image", "qwen-image-2.1"),
        }:
            raise ValueError("Unsupported media executor model")
        if any(type(endpoint[key]) is not bool for key in ("enabled", "qualified")):
            raise ValueError("Executor enablement and qualification must be explicit booleans")
        endpoint = {**endpoint, "url": private_url(endpoint["url"])}
        if not isinstance(endpoint["capabilities"], list) or not endpoint["capabilities"]:
            raise ValueError("Executor capabilities are required")
        for capability in endpoint["capabilities"]:
            if not isinstance(capability, dict) or capability.get("mode") not in {"t2i", "i2i"}:
                raise ValueError("Invalid executor capability")
            if capability.get("mode") not in {"t2i", "i2i"} or not capability.get("recipe") or not capability.get("aspect_ratios") or not capability.get("backgrounds"):
                raise ValueError("Image capabilities need a versioned recipe, ratios and backgrounds")
        result.append(endpoint)
    return result


def compatible(endpoint: dict, body: dict, kind: str) -> dict | None:
    if not endpoint["enabled"] or not endpoint["qualified"]:
        return None
    if body["model"] not in {endpoint["model"], "siyuan-" + kind}:
        return None
    if kind != "image" or endpoint["adapter"] != "comfy-image":
        return None
    mode = "i2i" if body["images"] else "t2i"
    for capability in endpoint["capabilities"]:
        if (capability["mode"] == mode and body["aspect_ratio"] in capability["aspect_ratios"]
                and len(body["images"]) <= capability.get("max_images", 0)
                and body["background"] in capability["backgrounds"]):
            return capability
    return None


class DirectGeneration:
    def __init__(self, media, executors=None):
        self.media = media
        self.store = media.store
        self.executors = load_pool() if executors is None else executors
        self.tasks: dict[str, asyncio.Task] = {}
        self.allocation_lock = asyncio.Lock()

    def candidates(self, body, kind, generation=None):
        return [(endpoint, capability) for endpoint in self.executors
                if (generation is None or endpoint["id"] in generation["policy"]["local_resources"])
                and (capability := compatible(endpoint, body, kind)) is not None]

    async def tick(self):
        for job in self.store.active():
            if job["kind"] != "image" or job.get("execution_kind") != SINGLE or job.get("next_reconcile_at", 0) > time.time():
                continue
            task = self.tasks.get(job["id"])
            if task is None or task.done():
                self.tasks[job["id"]] = asyncio.create_task(self.step(job["id"]))
        for identifier, task in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(identifier)

    async def call(self, endpoint, method, path, **kwargs):
        key = os.environ.get(endpoint["key_env"], "")
        if not key:
            raise MediaError("media_executor_unavailable", "Executor credential is not configured.", 503)
        # A server can close an idle keep-alive connection while a status poll
        # reuses it. Retry only reads, once; never replay an ambiguous mutation.
        for attempt in range(2):
            try:
                response = await self.media.client.request(
                    method, endpoint["url"] + path, headers={"Authorization": "Bearer " + key},
                    timeout=15, **kwargs,
                )
                break
            except httpx.RemoteProtocolError as exc:
                if method == "GET" and attempt == 0:
                    continue
                raise UnknownOutcome() from exc
            except httpx.HTTPError as exc:
                raise UnknownOutcome() from exc
        if response.is_error:
            # Do not include backend addresses, raw errors, or model details.
            if response.status_code == 404:
                raise UnknownOutcome("Original execution is not visible yet; no new submission was made.")
            raise MediaError("media_executor_rejected", "Executor rejected this operation.", 502,
                             upstream_status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise UnknownOutcome() from exc

    async def probe(self, endpoint, capability=None):
        try:
            value = await self.call(endpoint, "GET", "/capabilities")
            if not isinstance(value, dict):
                return "unavailable"
            if capability and capability["recipe"] not in value.get("recipes", []):
                return "unavailable"
            return "ready" if value.get("ready") is True else (
                "busy" if value.get("busy") is True else "unavailable")
        except (MediaError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return "unavailable"

    async def resources(self, generation=None):
        active = self.store.active()
        occupied = {job["provider_state"].get("endpoint", {}).get("resource_id") for job in active}
        async def row(endpoint):
            selected = generation is None or endpoint["id"] in generation["policy"]["local_resources"]
            status = ("disabled" if not endpoint["enabled"] else "unqualified" if not endpoint["qualified"]
                      else "busy" if endpoint["resource_id"] in occupied else await self.probe(endpoint))
            return {key: endpoint[key] for key in ("id", "resource_id", "enabled", "qualified", "capabilities")} | {
                "selected": selected, "status": status,
                "active_jobs": [job["id"] for job in active if job["provider_state"].get("endpoint", {}).get("resource_id") == endpoint["resource_id"]]}
        return await asyncio.gather(*(row(endpoint) for endpoint in self.executors))

    async def select(self, job):
        def stopped():
            current = self.store.get(job["id"])
            if current["status"] in TERMINAL:
                return True
            if current.get("cancel_requested"):
                self.store.update(job["id"], status="cancelled")
                return True
            timeout = job.get("image_generation", {}).get("policy", {}).get("queue_timeout", self.store.settings()["queue_timeout"])
            if time.time() - current["created_at"] > timeout:
                self.fail(job["id"], "media_queue_timeout", "No eligible executor became available in time.")
                return True
            return False

        if stopped():
            return None
        # A persisted reservation is safe to resume until a send was attempted.
        if job["provider_state"].get("endpoint"):
            return job["provider_state"]

        candidates = self.candidates(job["request"], "image", job.get("image_generation"))
        async with self.allocation_lock:
            occupied = {item["provider_state"].get("endpoint", {}).get("resource_id")
                        for item in self.store.active() if item["id"] != job["id"]}
        busy = any(endpoint["resource_id"] in occupied for endpoint, _ in candidates)
        async def probe_pair(endpoint, capability):
            return endpoint, capability, await self.probe(endpoint, capability)
        probes = [asyncio.create_task(probe_pair(endpoint, capability))
                  for endpoint, capability in candidates if endpoint["resource_id"] not in occupied]
        try:
            for probe_result in asyncio.as_completed(probes):
                endpoint, capability, status = await probe_result
                busy = busy or status == "busy"
                if status != "ready":
                    continue
                async with self.allocation_lock:
                    if stopped():
                        return None
                    active = self.store.active()
                    if any(item["provider_state"].get("endpoint", {}).get("resource_id") == endpoint["resource_id"]
                           for item in active if item["id"] != job["id"]):
                        busy = True
                        continue
                    # Preserve FIFO for earlier requests eligible for this card.
                    older = [item for item in active if item["created_at"] < job["created_at"]
                             and item.get("execution_kind") == SINGLE and not item["provider_state"].get("endpoint")
                             and not item.get("cancel_requested")]
                    if any(endpoint["id"] in [e["id"] for e, _ in self.candidates(
                            item["request"], "image", item.get("image_generation"))] for item in older):
                        busy = True
                        continue
                    state = {"endpoint": endpoint, "capability": capability,
                             "operation_id": job["id"], "submitted": False}
                    self.store.update(job["id"], provider_state=state, provider="local", routing_reason="local_available")
                    return state
        finally:
            for task in probes:
                task.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
        if stopped():
            return None
        generation = job.get("image_generation")
        reason = "local_busy" if busy else "local_unavailable"
        action = generation["policy"]["when_busy" if busy else "when_unavailable"] if generation else "queue"
        automatic = generation and route(job) == "local_first"
        if (automatic and action == "cloud" and cloud_allowed(job) and self.store.settings()["codex_ready"]):
            self.store.update(job["id"], execution_kind=None, routing_reason=reason + "_cloud", provider="codex")
        elif action == "error":
            self.fail(job["id"], "media_" + reason, "No local image resource is available under this policy.")
        elif job.get("routing_reason") != reason + "_queued":
            self.store.update(job["id"], routing_reason=reason + "_queued")
        return None

    async def step(self, identifier):
        async with self.media.lock(identifier):
            job = self.store.get(identifier)
            if job["status"] in TERMINAL:
                return
            state = job["provider_state"]
            submitting = False
            finishing = False
            try:
                if not state.get("submitted"):
                    state = await self.select(job)
                    if state is None:
                        return
                    endpoint, capability = state["endpoint"], state["capability"]
                    body = job["request"]
                    if self.store.get(identifier).get("cancel_requested"):
                        self.store.update(identifier, status="cancelled")
                        return
                    # No await between checking cancellation and persisting the
                    # attempt. A crash after this marker is genuinely ambiguous.
                    state = {**state, "submitted": True}
                    self.store.update(identifier, status="in_progress", provider_state=state)
                    submitting = True
                    await self.call(endpoint, "POST", "/executions", json={
                        "operation_id": identifier, "recipe": capability["recipe"], "request": body,
                    })
                    submitting = False
                endpoint = state["endpoint"]
                current = self.store.get(identifier)
                root = "/executions/"
                execution = await self.call(endpoint, "GET", root + identifier)
                status = execution.get("status")
                if status == "completed":
                    finishing = True
                    await self.finish(job, endpoint, execution, root)
                elif status in {"failed", "cancelled"}:
                    self.store.update(identifier, status=status, error=(None if status == "cancelled" else {
                        "code": "media_execution_failed", "message": "Generation failed on the selected executor.",
                    }))
                elif status == "reconciling":
                    # Preserve the adapter's uncertainty instead of resetting
                    # its recovery deadline on every successful status poll.
                    if current.get("cancel_requested"):
                        await self.call(endpoint, "POST", root + identifier + "/cancel",
                                        json={"operation_id": identifier + "_cancel"})
                    self.uncertain(identifier, "executor_reconciling",
                                   attention=execution.get("error") == "operator_verification_required")
                else:
                    if status not in {"queued", "in_progress", "running", "reconciling", "cancelling", "archiving"}:
                        raise UnknownOutcome()
                    if current.get("cancel_requested"):
                        await self.call(endpoint, "POST", root + identifier + "/cancel",
                                        json={"operation_id": identifier + "_cancel"})
                    self.store.update(identifier, status="cancelling" if current.get("cancel_requested") else "in_progress",
                                      progress=max(0, min(99, int(execution.get("progress") or 0))), error=None,
                                      reconcile_since=None)
            except asyncio.CancelledError:
                # Shutdown detaches, never interrupts the model or frees its slot.
                raise
            except MediaError as exc:
                if finishing and exc.code in {"invalid_image", "media_too_large", "artifact_version_conflict"}:
                    self.fail(identifier, "media_output_invalid", "The completed execution returned an invalid output.")
                elif submitting and exc.details.get("upstream_status") in {400, 401, 403, 413, 422, 429}:
                    self.store.update(identifier, status="failed", error={
                        "code": "media_executor_rejected", "message": "Executor rejected generation before admission.",
                    })
                else:
                    reason = exc.code
                    if exc.__cause__ is not None:
                        reason += ":" + type(exc.__cause__).__name__
                    self.uncertain(identifier, reason)
            except Exception as exc:
                # Once selection has persisted, even a 5xx/invalid response is
                # ambiguous. Only an observed terminal execution releases a slot.
                self.uncertain(identifier, type(exc).__name__)

    def uncertain(self, identifier, reason="media_outcome_unknown", *, attention=False):
        previous = self.store.get(identifier)
        if previous.get("reconcile_reason") != reason:
            logger.warning("Media execution %s requires reconciliation: %s", identifier, reason)
        since = previous.get("reconcile_since") or time.time()
        attention = attention or time.time() - since >= 600
        self.store.update(identifier, status="reconciling", reconcile_since=since, reconcile_reason=reason,
                          next_reconcile_at=time.time() + 30,
                          error={"code": "media_recovery_required" if attention else "media_outcome_unknown",
                                 "message": ("Original execution needs operator verification; its resource remains reserved."
                                             if attention else "Checking the original execution; no resubmission was made.")})

    def fail(self, identifier, code, message):
        self.store.update(identifier, status="failed", error={"code": code, "message": message})

    async def finish(self, job, endpoint, execution, root):
        key = os.environ.get(endpoint["key_env"], "")
        if not key:
            raise UnknownOutcome()
        stream = self.media.client.stream("GET", endpoint["url"] + root + job["id"] + "/output",
                                          headers={"Authorization": "Bearer " + key}, timeout=300)
        self.store.update(job["id"], status="archiving")
        if job["kind"] == "image":
            data = bytearray()
            async with stream as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 32 * 1024 * 1024:
                        raise MediaError("media_too_large", "Generated image exceeds its limit.", 413)
            info = image_info(bytes(data))
            ratio = job["request"]["aspect_ratio"]
            width, height = info["width"], info["height"]
            if ((ratio == "square" and width != height) or (ratio == "portrait" and width >= height)
                    or (ratio == "landscape" and width <= height)):
                self.fail(job["id"], "image_requirements_unmet", "Generated image aspect ratio does not match the request.")
                return
            if job["request"]["background"] == "transparent" and not info["transparent"]:
                self.store.update(job["id"], status="failed", error={
                    "code": "image_requirements_unmet", "message": "Generated image is not transparent.",
                })
                return
            output = await self.media.archive(job["id"], "out_" + job["id"], data=bytes(data), **info)
        self.store.update(job["id"], status="completed", output=output, error=None, recovery_outputs=[],
                          actual_duration=execution.get("actual_duration"))

    async def cancel(self, job):
        # Synchronous store operations cannot interleave with the single worker's
        # state updates. Do not wait for its network/download lock to save intent.
        job = self.store.get(job["id"])
        if job["status"] in TERMINAL:
            return job
        return self.store.update(job["id"], cancel_requested=True, next_reconcile_at=0,
                                 status="cancelling" if job["provider_state"].get("submitted") else "cancelled")
