"""Single video generation jobs. Client workflows and GPU recipes stay separate."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .contracts import ID_PATTERN, MediaError, TERMINAL, UnknownOutcome, video_request
from .providers import H3Provider


SINGLE = "single_generation"
logger = logging.getLogger(__name__)


def single_video_request(value: dict) -> dict:
    # Reuse upload/prompt validation without entering the creative workflow.
    model = value.get("model", "siyuan-video")
    if model not in {"siyuan-video", "minimax-h3"}:
        raise MediaError("model_not_found", "Video model is not available.", 404)
    if value.get("workflow_mode", SINGLE) != SINGLE:
        raise MediaError("invalid_media_parameters", "This model uses single_generation.")
    if set(value) - {"model", "prompt", "mode", "duration", "seed", "aspect_ratio", "assets", "workflow_mode"}:
        raise MediaError("invalid_media_parameters", "Unsupported single generation parameters.")
    result = video_request({**value, "model": "siyuan-video", "workflow_mode": "quality_gate"})
    if result["mode"] not in {"t2v", "i2v"} or set(result["assets"]) - {"first_frame"}:
        raise MediaError("invalid_media_parameters", "Single generation supports text or one first frame.")
    if result["mode"] == "t2v" and result["assets"]:
        raise MediaError("invalid_media_parameters", "Text-to-video cannot include a first frame.")
    return {**result, "model": model, "workflow_mode": SINGLE}


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
    path = path or os.environ.get("AI_ROUTER_VIDEO_EXECUTORS_FILE")
    if not path:
        return []
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) != {"version", "executors"} or value["version"] != 1:
        raise ValueError("Invalid media executor configuration")
    result, identifiers = [], set()
    for endpoint in value["executors"]:
        required = {"id", "adapter", "url", "key_env", "resource_id", "model", "capabilities", "enabled", "qualified"}
        if not isinstance(endpoint, dict):
            raise ValueError("Invalid media executor fields")
        optional = {"priority", "max_parallel"}
        if not required <= set(endpoint) or set(endpoint) - required - optional:
            raise ValueError("Invalid media executor fields")
        for field in ("id", "resource_id", "key_env"):
            if not isinstance(endpoint[field], str) or not ID_PATTERN.fullmatch(endpoint[field]):
                raise ValueError("Invalid media executor identifier")
        if endpoint["id"] in identifiers:
            raise ValueError("Duplicate media executor")
        identifiers.add(endpoint["id"])
        if (endpoint["adapter"], endpoint["model"]) != ("h3", "minimax-h3"):
            raise ValueError("Unsupported media executor model")
        if any(type(endpoint[key]) is not bool for key in ("enabled", "qualified")):
            raise ValueError("Executor enablement and qualification must be explicit booleans")
        if (type(endpoint.get("priority", 100)) is not int or not 0 <= endpoint.get("priority", 100) <= 1000
                or type(endpoint.get("max_parallel", 1)) is not int
                or not 1 <= endpoint.get("max_parallel", 1) <= 2):
            raise ValueError("Invalid H3 priority or physical capacity")
        endpoint = {**endpoint, "url": private_url(endpoint["url"])}
        if not isinstance(endpoint["capabilities"], list) or not endpoint["capabilities"]:
            raise ValueError("Executor capabilities are required")
        for capability in endpoint["capabilities"]:
            if not isinstance(capability, dict) or capability.get("mode") not in {"t2v", "i2v"}:
                raise ValueError("Invalid executor capability")
            if (capability.get("profile") not in {"preview", "quality"}
                    or not capability.get("durations") or not capability.get("aspect_ratios")):
                raise ValueError("H3 capabilities need tested profile, durations and aspect ratios")
            if (any(type(value) is not int or not 4 <= value <= 15 for value in capability["durations"])
                    or any(value not in {"16:9", "9:16"} for value in capability["aspect_ratios"])
                    or ("recipe_id" in capability and (
                        not isinstance(capability["recipe_id"], str)
                        or not ID_PATTERN.fullmatch(capability["recipe_id"])))):
                raise ValueError("Invalid H3 qualified capability")
        result.append(endpoint)
    return result


def compatible(endpoint: dict, body: dict) -> dict | None:
    if not endpoint["enabled"] or not endpoint["qualified"]:
        return None
    if body["model"] not in {endpoint["model"], "siyuan-video"}:
        return None
    for capability in endpoint["capabilities"]:
        if capability["mode"] != body["mode"] or body["aspect_ratio"] not in capability["aspect_ratios"]:
            continue
        if body["duration"] not in capability["durations"]:
            continue
        return capability
    return None


class VideoGeneration:
    def __init__(self, media, executors=None):
        self.media = media
        self.store = media.store
        self.executors = load_pool() if executors is None else executors
        self.tasks: dict[str, asyncio.Task] = {}
        self.allocation_lock = asyncio.Lock()

    def candidates(self, body):
        return [(endpoint, capability) for endpoint in self.executors
                if (capability := compatible(endpoint, body)) is not None]

    async def tick(self):
        for job in self.store.active():
            if job["kind"] != "video" or job.get("execution_kind") != SINGLE or job.get("next_reconcile_at", 0) > time.time():
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
            except httpx.HTTPError as exc:
                if method == "GET" and attempt == 0:
                    continue
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

    async def ready(self, endpoint, capability):
        options = await self.call(endpoint, "GET", "/api/router/options")
        if (options.get("contract_version") != 1 or options.get("workflow_contract_version", 0) < 2 or options.get("draining")
                or capability["mode"] not in options.get("mode", [])):
            return False
        if capability.get("recipe_id") and capability["recipe_id"] not in options.get("managed_recipes", []):
            return False
        capacity = await self.call(endpoint, "GET", "/api/router/capacity")
        if capacity.get("validation_lease") or capacity.get("release_validation_gate", {}).get("enabled"):
            return False
        resources = capacity.get("resources")
        if resources is not None:
            if resources.get("ok") is not True:
                return False
            limits = capacity.get("policy", {}).get("resources", {})
            for setting, metric in (("min_root_free_gib", "root_available_bytes"),
                                    ("min_offload_free_gib", "offload_available_bytes"),
                                    ("min_available_ram_gib", "memory_available_bytes")):
                if setting in limits and resources.get(metric, 0) < limits[setting] * 1024**3:
                    return False
        # Fleet retains final GPU/memory admission and handles on-demand runtimes.
        active = capacity.get("active")
        if not isinstance(active, list) or any(row.get("status") == "queued" for row in active):
            return False
        maximum = endpoint.get("max_parallel", 1)
        if maximum > 1 and capacity.get("policy", {}).get("quality480_i2v", {}).get("max_parallel", 0) < maximum:
            return False
        return len(active) < maximum and not any(row.get("untracked_count") for row in capacity.get("queues", []))

    async def select(self, job):
        def stopped():
            current = self.store.get(job["id"])
            if current["status"] in TERMINAL:
                return True
            if current.get("cancel_requested"):
                self.store.update(job["id"], status="cancelled")
                return True
            timeout = self.store.settings()["video_queue_timeout"]
            if time.time() - current["created_at"] > timeout:
                self.fail(job["id"], "media_queue_timeout", "No eligible executor became available in time.")
                return True
            return False

        if stopped():
            return None
        # A persisted reservation is safe to resume until a send was attempted.
        if job["provider_state"].get("endpoint"):
            return job["provider_state"]

        async def probe(endpoint, capability):
            try:
                return endpoint, capability, await self.ready(endpoint, capability)
            except (MediaError, httpx.HTTPError, ValueError, KeyError, TypeError):
                return endpoint, capability, False

        probes = [asyncio.create_task(probe(endpoint, capability))
                  for endpoint, capability in sorted(
                      self.candidates(job["request"]),
                      key=lambda candidate: candidate[0].get("priority", 100))]
        try:
            for endpoint, capability, available in await asyncio.gather(*probes):
                if not available:
                    continue
                # Network probes never hold the shared resource allocation lock.
                async with self.allocation_lock:
                    if stopped():
                        return None
                    state = self.store.reserve_video_executor(job["id"], endpoint, capability)
                    if state is not None:
                        return state
        finally:
            for task in probes:
                task.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
        async with self.allocation_lock:
            if stopped():
                return None
            return None

    def h3(self, endpoint):
        return H3Provider(self.media.client, executor_url=endpoint["url"], key_env=endpoint["key_env"])

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
                    payload = {key: body[key] for key in ("mode", "prompt", "duration", "seed", "aspect_ratio")}
                    payload.update(operation_id=identifier, profile=capability["profile"], audio_policy="native", watermark=False)
                    if capability.get("recipe_id"):
                        payload["recipe_id"] = capability["recipe_id"]
                    # Selection already checked the contract. A second GET
                    # after marking submission would make a harmless read
                    # failure look like an ambiguous generation POST.
                    await self.h3(endpoint).create_execution(payload, body["assets"], check_options=False)
                    submitting = False
                endpoint = state["endpoint"]
                current = self.store.get(identifier)
                root = "/api/router/executions/"
                execution = await self.call(endpoint, "GET", root + identifier)
                status = execution.get("status")
                if (status == "queued" and not current.get("cancel_requested")
                        and time.time() - current["created_at"] > self.store.settings()["video_queue_timeout"]):
                    current = self.store.update(identifier, queue_timeout_requested=True)
                if status == "completed":
                    finishing = True
                    await self.finish(job, endpoint, execution, root)
                elif status in {"failed", "cancelled"}:
                    if status == "cancelled" and current.get("queue_timeout_requested"):
                        self.fail(identifier, "media_queue_timeout", "Video left the executor queue after its wait limit.")
                    else:
                        self.store.update(identifier, status=status, error=(None if status == "cancelled" else {
                            "code": "media_execution_failed", "message": "Generation failed on the selected executor.",
                        }))
                elif status == "reconciling" or execution.get("reconciliation_required") is True:
                    # Preserve the adapter's uncertainty instead of resetting
                    # its recovery deadline on every successful status poll.
                    if current.get("cancel_requested") or current.get("queue_timeout_requested"):
                        await self.call(endpoint, "POST", root + identifier + "/cancel",
                                        json={"operation_id": identifier + "_cancel"})
                    self.uncertain(identifier, "executor_reconciling",
                                   attention=execution.get("error") == "operator_verification_required")
                else:
                    if status not in {"queued", "submitted", "in_progress", "running", "cancelling", "archiving"}:
                        raise UnknownOutcome()
                    if current.get("cancel_requested") or current.get("queue_timeout_requested"):
                        await self.call(endpoint, "POST", root + identifier + "/cancel",
                                        json={"operation_id": identifier + "_cancel"})
                    self.store.update(identifier, status=("cancelling" if current.get("cancel_requested")
                                                          or current.get("queue_timeout_requested") else "in_progress"),
                                      progress=max(0, min(99, int(execution.get("progress") or 0))), error=None,
                                      reconcile_since=None)
            except asyncio.CancelledError:
                # Shutdown detaches, never interrupts the model or frees its slot.
                raise
            except MediaError as exc:
                if finishing and exc.code in {"invalid_image", "invalid_video", "media_too_large", "artifact_version_conflict"}:
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
        output = await self.media.archive(job["id"], "out_" + job["id"], stream=stream, content_type="video/mp4",
                                          expected_duration_seconds=execution.get("actual_duration"),
                                          expected_frame_count=execution.get("frame_count"))
        probe = json.loads(await self.media.media_command(
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output["path"],
        ))
        try:
            track = next(item for item in probe["streams"] if item.get("codec_type") == "video")
            duration = float(track.get("duration") or probe["format"]["duration"])
            ratio = float(track["width"]) / float(track["height"])
            audio = any(item.get("codec_type") == "audio" for item in probe["streams"])
            width, height = (int(item) for item in job["request"]["aspect_ratio"].split(":"))
        except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError) as error:
            raise MediaError("invalid_video", "Video has unreadable dimensions or duration.", 502) from error
        # H3 accepts 107+17*n frames at 24 fps; allow its native duration.
        requested = job["request"]["duration"]
        frames = min(range(107, 363, 17), key=lambda value: (abs(value - requested * 24), value))
        duration_error = min(abs(duration - requested), abs(duration - frames / 24))
        if (not math.isfinite(duration) or not math.isfinite(ratio) or not audio
                or duration_error > 0.25 or abs(ratio / (width / height) - 1) > 0.03):
            self.store.update(job["id"], status="failed", error={
                "code": "video_requirements_unmet", "message": "Video duration, audio or aspect ratio does not match the request.",
            })
            return
        execution = {**execution, "actual_duration": duration}
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
