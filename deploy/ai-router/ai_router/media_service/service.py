from __future__ import annotations

import asyncio
import base64
import hashlib
import fcntl
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
try:
    from asyncio import timeout
except ImportError:
    from async_timeout import timeout

from .contracts import (
    BACKGROUNDS, ID_PATTERN, RATIOS, TERMINAL, USE_CASES, MediaError, QuotaExceeded,
    UnknownOutcome, image_info, image_request, video_request,
)
from .providers import CodexProvider, H3Provider, QwenProvider
from .storage import MediaStore


NON_BILLABLE_QWEN_ERRORS = {
    "fallback_incompatible",
    "qwen_not_configured",
    "qwen_request_failed",
}


class MediaService:
    def __init__(self, store: MediaStore, *, client=None, codex=None, qwen=None, h3=None):
        self.store = store
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.codex = codex or CodexProvider()
        self.qwen = qwen or QwenProvider(self.client)
        self.h3 = h3 or H3Provider(self.client)
        self.runner = None
        self.image_task = None
        self.image_job = None
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_file = None

    def lock(self, job_id: str):
        return self.locks.setdefault(job_id, asyncio.Lock())

    def space(self):
        if shutil.disk_usage(self.store.root).free < self.store.settings()["min_free_bytes"]:
            raise MediaError("media_storage_full", "Insufficient media storage.", 507)

    async def start(self):
        self.lock_file = (self.store.root / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("another media worker owns this data directory")
        self.runner = asyncio.create_task(self._run())

    async def close(self):
        for task in (self.runner, self.image_task):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.runner, self.image_task) if task), return_exceptions=True)
        await self.client.aclose()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    async def _run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A provider failure cannot kill reconciliation for all other tasks.
                pass
            await asyncio.sleep(self.store.settings()["poll_interval"])

    async def tick(self):
        for job in self.store.active():
            if job["kind"] == "image":
                if self.image_task and not self.image_task.done():
                    continue
                if job.get("next_reconcile_at", 0) > time.time():
                    continue
                if job["status"] == "queued" and time.time() - job["created_at"] > self.store.settings()["queue_timeout"]:
                    self.store.update(job["id"], status="failed", error={"code": "media_queue_timeout", "message": "Queue wait expired."})
                    continue
                if job["status"] != "queued" and not job["provider_state"].get("thread_id") and not job["provider_state"].get("task_id"):
                    continue
                self.image_job = job["id"]
                self.image_task = asyncio.create_task(self._image(job))
            else:
                async with self.lock(job["id"]):
                    try:
                        for output_id in job.get("recovery_outputs", []):
                            output = self.store.artifact(output_id)
                            version = output["output_id"]
                            if output["content_type"] == "text/plain":
                                await self.archive(job["id"], version, data=output["text"].encode(),
                                                   content_type="text/plain", text=output["text"], stage=output.get("stage"))
                            else:
                                stream = await self.h3.download(job["provider_state"]["project_id"], version)
                                await self.archive(job["id"], version, stream=stream, content_type="video/mp4",
                                                   stage=output.get("stage"))
                        if job.get("recovery_outputs"):
                            self.store.update(job["id"], recovery_outputs=[])
                        await self._video(self.store.get(job["id"]))
                    except MediaError as exc:
                        self.store.update(job["id"], sync_error={"code": exc.code, "message": str(exc)})
                    except (httpx.HTTPError, OSError, ValueError):
                        self.store.update(job["id"], sync_error={"code": "media_sync_unavailable", "message": "Status synchronization is unavailable."})
                    except Exception:
                        self.store.update(job["id"], sync_error={"code": "media_sync_unavailable", "message": "Unexpected upstream state; reconciliation is paused for this task."})

    async def options(self, models: list[str]) -> dict:
        settings = self.store.settings()
        result = {
            "enabled": settings["enabled"], "models": models,
            "images": {"use_case": USE_CASES, "aspect_ratio": RATIOS, "background": BACKGROUNDS,
                       "n": [1], "max_edit_images": 5, "fallback_max_edit_images": 3,
                       "response_format": ["b64_json", "url"], "mask": False},
            "videos": {"available": False, "context_ir_billable": True},
        }
        if settings["h3_ready"] and settings["videos_enabled"] and "siyuan-video" in models:
            try:
                result["videos"] = {"available": True, **await self.h3.options()}
            except MediaError:
                pass
        return result

    def submit(self, owner: str, kind: str, body: dict, idem: str, request_id: str, *, edit=False):
        settings = self.store.settings()
        if not settings["enabled"] or not settings[kind + "s_enabled"]:
            raise MediaError("media_disabled", "Media generation is disabled.", 503)
        if kind == "image":
            body = image_request(body, edit)
            if body["model"] == "siyuan-image" and not settings["codex_ready"]:
                raise MediaError("codex_not_verified", "Codex media isolation has not been verified.", 503)
        else:
            body = video_request(body)
            if not settings["h3_ready"]:
                raise MediaError("h3_not_verified", "H3 media contract has not been verified.", 503)
        self.space()
        if not isinstance(idem, str) or not 1 <= len(idem) <= 128:
            raise MediaError("invalid_idempotency_key", "Idempotency key must contain 1-128 characters.")
        return self.store.create(owner, kind, body, idem, request_id, settings["queue_limit"])[0]

    async def _image(self, original: dict):
        job_id = original["id"]
        body = original["request"]
        state = original["provider_state"]
        directory = self.store.root / "jobs" / job_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        provider = state.get("provider", "qwen" if body["model"] == "qwen-image-3.0-pro" else "codex")

        def checkpoint(value):
            nonlocal state
            state = {**value, "provider": provider}
            self.store.update(job_id, provider_state=state)

        try:
            self.store.update(job_id, status="cancelling" if original.get("cancel_requested") else "in_progress")
            async with timeout(self.store.settings()["image_timeout"]):
                if provider == "qwen":
                    self.store.reserve_paid(job_id, self.store.settings()["daily_paid_images"])
                    result = await self.qwen.generate(body, state, checkpoint, directory)
                else:
                    if original.get("cancel_requested"):
                        if not state.get("thread_id"):
                            if state.get("submitted"):
                                raise UnknownOutcome()
                            self.store.update(job_id, status="cancelled")
                            return
                        await self.codex.cancel(state, directory)
                    try:
                        result = await self.codex.generate(body, state, checkpoint, directory)
                    except QuotaExceeded:
                        if self.store.get(job_id).get("cancel_requested"):
                            raise MediaError("media_cancelled", "Image cancellation was requested.", 409)
                        settings = self.store.settings()
                        if not settings["paid_fallback"]:
                            raise
                        if len(body["images"]) > 3 or body["background"] == "transparent":
                            raise MediaError("fallback_incompatible", "No compatible quota fallback for this request.", 422)
                        provider = "qwen"
                        self.store.reserve_paid(job_id, settings["daily_paid_images"])
                        checkpoint({})
                        self.store.update(job_id, fallback_applied=True)
                        result = await self.qwen.generate(body, state, checkpoint, directory)
                self.store.update(job_id, status="archiving")
                data = result.get("data")
                if data is None:
                    data = await self._image_url(result["url"])
                info = image_info(data)
                if body["background"] == "transparent" and not info["transparent"]:
                    raise MediaError("image_requirements_unmet", "Generated image is not transparent.", 422)
                output = await self.archive(job_id, "out_" + job_id, data=data, **info)
                self.store.update(job_id, status="completed", output=output,
                                  provider=provider, revised_prompt=result.get("revised_prompt"), error=None,
                                  recovery_outputs=[])
        except asyncio.CancelledError:
            current = self.store.get(job_id)
            self.store.update(job_id, status="reconciling" if not current.get("cancel_requested") else "cancelling")
            raise
        except (TimeoutError, UnknownOutcome, httpx.HTTPError, OSError):
            current = self.store.get(job_id)
            self.store.update(job_id, status="cancelling" if current.get("cancel_requested") else "reconciling",
                              error={"code": "media_outcome_unknown", "message": "Checking the original task; it will not be resubmitted."})
        except MediaError as exc:
            if (
                provider == "qwen"
                and exc.code in NON_BILLABLE_QWEN_ERRORS
                and not state.get("task_id")
            ):
                self.store.release_paid(job_id)
            self.store.update(job_id, status="cancelled" if exc.code == "media_cancelled" else "failed",
                              error={"code": exc.code, "message": str(exc)})
        except Exception:
            current = self.store.get(job_id)
            self.store.update(job_id, status="cancelling" if current.get("cancel_requested") else "reconciling",
                              error={"code": "media_outcome_unknown", "message": "Task requires reconciliation."})
        finally:
            if self.store.get(job_id)["status"] not in TERMINAL:
                # Leave admission windows for newer jobs while an old task is uncertain.
                self.store.update(job_id, next_reconcile_at=time.time() + 30)

    async def _image_url(self, url: str) -> bytes:
        parsed = urlparse(url)
        allowed = tuple(item.strip() for item in os.environ.get(
            "AI_ROUTER_IMAGE_DOWNLOAD_DOMAINS", ".aliyuncs.com,.aliyun.com",
        ).split(",") if item.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or not any(
            parsed.hostname and parsed.hostname.endswith(suffix) for suffix in allowed
        ):
            raise MediaError("invalid_image_url", "Untrusted image download endpoint.", 502)
        data = bytearray()
        async with self.client.stream("GET", url, timeout=120) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 32 * 1024 * 1024:
                    raise MediaError("media_too_large", "Generated image is too large.", 413)
        return bytes(data)

    async def archive(self, job_id: str, output_id: str, *, data: bytes | None = None, stream=None,
                      content_type="application/octet-stream", **metadata) -> dict:
        if not ID_PATTERN.fullmatch(output_id):
            raise MediaError("invalid_output_id", "Invalid output identifier.", 502)
        video = content_type == "video/mp4"
        artifact_id = ("out_" + hashlib.sha256(f"{job_id}:{output_id}:mp4-clean-v1".encode()).hexdigest()
                       if video else output_id)
        existing = None
        try:
            existing = self.store.artifact(artifact_id)
            if existing["job_id"] != job_id:
                raise MediaError("artifact_version_conflict", "Output belongs to another task.", 409)
            path = Path(existing["path"])
            if path.is_file() and path.stat().st_size == existing["bytes"]:
                return existing
        except MediaError as exc:
            if exc.status != 404:
                raise
        self.space()
        directory = self.store.root / "outputs" / job_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / artifact_id
        temporary = directory / (artifact_id + "." + uuid4().hex + ".part")
        cleaned = directory / (artifact_id + "." + uuid4().hex + ".clean.part")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as handle:
                if data is not None:
                    handle.write(data)
                    digest.update(data)
                    size = len(data)
                else:
                    async with stream as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 1024**3:
                                raise MediaError("media_too_large", "Stage output is too large.", 413)
                            handle.write(chunk)
                            digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            publish = temporary
            published_hash = digest.hexdigest()
            if video:
                source_hash = digest.hexdigest()
                if existing and existing.get("source_sha256") != source_hash:
                    raise MediaError("artifact_version_conflict", "Original output version changed.", 409)
                source_dir = self.store.root / "sources" / job_id
                source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                source_path = source_dir / output_id
                if source_path.exists() and self.file_digest(source_path) != source_hash:
                    raise MediaError("artifact_version_conflict", "Original output version changed.", 409)
                await self.clean_video(temporary, cleaned)
                metadata.update(source_path=str(source_path), source_sha256=source_hash,
                                source_bytes=size, metadata_stripped=True)
                published_hash = self.file_digest(cleaned)
                size = cleaned.stat().st_size
                publish = cleaned
            if existing and existing["sha256"] != published_hash:
                raise MediaError("artifact_version_conflict", "Recovered content differs from the archived version.", 409)
            if video:
                os.chmod(temporary, 0o600)
                os.replace(temporary, source_path)
                with cleaned.open("rb") as handle:
                    os.fsync(handle.fileno())
            os.chmod(publish, 0o600)
            os.replace(publish, target)
            return self.store.save_artifact({
                "id": artifact_id, "output_id": output_id, "job_id": job_id, "path": str(target),
                "content_type": content_type, "bytes": size, "sha256": published_hash,
                "created_at": time.time(), **metadata,
            })
        finally:
            temporary.unlink(missing_ok=True)
            cleaned.unlink(missing_ok=True)

    @staticmethod
    def file_digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                value.update(chunk)
        return value.hexdigest()

    @staticmethod
    async def media_command(*args: str, seconds=30) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), seconds)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise MediaError("invalid_video", "Video validation or metadata removal failed.", 502)
        return stdout

    async def clean_video(self, source: Path, target: Path):
        probe = json.loads(await self.media_command("ffprobe", "-v", "error", "-show_streams",
                                                   "-of", "json", str(source)))
        video = next((item for item in probe.get("streams", []) if item.get("codec_type") == "video"
                      and not item.get("disposition", {}).get("attached_pic")), None)
        if video is None:
            raise MediaError("invalid_video", "Stage output has no playable video stream.", 502)
        # Copy media packets; never publish provider workflow tags or data tracks.
        await self.media_command(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-map", f"0:{int(video['index'])}",
            "-map", "0:a?", "-c", "copy", "-map_metadata", "-1", "-map_metadata:s", "-1",
            "-map_chapters", "-1", "-fflags", "+bitexact", "-movflags", "+faststart",
            "-f", "mp4", str(target), seconds=120,
        )
        clean = json.loads(await self.media_command("ffprobe", "-v", "error", "-show_streams", "-show_format",
                                                   "-of", "json", str(target)))
        allowed = {"major_brand", "minor_version", "compatible_brands", "language", "handler_name", "vendor_id"}
        sections = [clean.get("format", {}), *clean.get("streams", [])]
        if any(set(item.get("tags", {})) - allowed for item in sections):
            raise MediaError("unsafe_video_metadata", "Video metadata could not be removed.", 502)

    async def _video(self, job: dict):
        state = job["provider_state"]
        if not state.get("project_id"):
            project = await self.h3.create(job)
            state = {"project_id": project["id"]}
            job = self.store.update(job["id"], provider_state=state, status="in_progress")
        if not state.get("context_submitted"):
            await self.h3.action(state["project_id"], "context_ir", "start",
                                 {"operation_id": job["id"] + "_context", "expected_run_id": None})
            state = {**state, "context_submitted": True}
            self.store.update(job["id"], provider_state=state)
        project = await self.h3.get(state["project_id"])
        stages = []
        for stage in project["pipeline"]:
            public = {name: stage.get(name) for name in ("id", "status", "progress", "output_id", "run_id")}
            output_id = stage.get("output_id")
            if output_id and stage["status"] in {"awaiting_approval", "approved"}:
                try:
                    if stage["id"] == "context_ir":
                        text = project.get("prompt_ir", "")
                        output = await self.archive(job["id"], output_id, data=text.encode(), content_type="text/plain",
                                                    text=text, stage=stage["id"])
                    else:
                        stream = await self.h3.download(state["project_id"], output_id)
                        output = await self.archive(job["id"], output_id, stream=stream, content_type="video/mp4",
                                                    stage=stage["id"])
                    public["output"] = output
                    public["approved_text"] = project.get("prompt_approved") if stage["id"] == "context_ir" else None
                except (MediaError, OSError, httpx.HTTPError):
                    public["status"] = "archiving"
            stages.append(public)
        status = "completed" if stages and all(stage["status"] == "approved" for stage in stages) else "in_progress"
        if any(stage["status"] == "failed" for stage in stages):
            status = "failed"
        if any(stage["status"] == "cancelled" for stage in stages):
            status = "cancelled"
        changes = {"stages": stages, "status": status, "sync_error": None,
                   "provider_updated_at": project.get("updated_at"),
                   "actual_duration": project.get("actual_duration"),
                   "provider_errors": project.get("internal_errors", {})}
        if status == "completed":
            changes["output"] = stages[-1].get("output")
        if any(job.get(name) != value for name, value in changes.items()):
            self.store.update(job["id"], **changes)

    async def action(self, job_id: str, stage_id: str, action: str, body: dict, idem: str, owner: str | None,
                     request_id: str | None = None):
        if not ID_PATTERN.fullmatch(stage_id) or action not in {"start", "approve", "cancel"}:
            raise MediaError("invalid_stage_action", "Invalid stage action.")
        allowed = {"output_id", "new_seed"} if action == "start" else (
            {"output_id", "prompt"} if action == "approve" and stage_id == "context_ir" else
            {"output_id"} if action == "approve" else set()
        )
        if not isinstance(body, dict) or set(body) - allowed:
            raise MediaError("invalid_stage_action", "Unexpected stage parameters.")
        if "new_seed" in body and type(body["new_seed"]) is not bool:
            raise MediaError("invalid_stage_action", "new_seed must be boolean.")
        async with self.lock(job_id):
            job = self.store.get(job_id, owner)
            if action == "start":
                settings = self.store.settings()
                if not settings["enabled"] or not settings["videos_enabled"]:
                    raise MediaError("media_disabled", "New video execution is disabled.", 503)
                if not settings["h3_ready"]:
                    raise MediaError("h3_not_verified", "H3 media contract has not been verified.", 503)
            if job["kind"] != "video" or not job["provider_state"].get("project_id"):
                raise MediaError("stage_not_ready", "Video project is not ready.", 409)
            operation, created = self.store.operation(job_id, idem, {"stage": stage_id, "action": action, **body}, request_id)
            if not created and operation["status"] == "completed":
                return {**job, "operation_id": operation["id"]}
            if operation.get("payload"):
                await self.h3.action(job["provider_state"]["project_id"], stage_id, action, operation["payload"])
                self.store.finish_operation(job_id, idem, {**operation, "status": "completed"})
                await self._video(job)
                return {**self.store.get(job_id, owner), "operation_id": operation["id"]}
            await self._video(job)
            job = self.store.get(job_id, owner)
            stage = next((item for item in job["stages"] if item["id"] == stage_id), None)
            if not stage:
                raise MediaError("stage_not_found", "Stage is not in this pipeline.", 404)
            if action == "approve" and (stage["status"] != "awaiting_approval" or not stage.get("output")
                                       or body.get("output_id") != stage.get("output_id")):
                raise MediaError("stale_stage_output", "Approval requires the current archived output.", 409)
            if action == "start" and stage_id != "context_ir":
                previous = job["stages"][job["stages"].index(stage) - 1]
                if previous["status"] != "approved" or previous.get("output_id") != body.get("output_id"):
                    raise MediaError("stale_stage_output", "Start requires the approved predecessor output.", 409)
            payload = {
                "operation_id": operation["id"], "expected_output_id": body.get("output_id"),
                "expected_run_id": stage.get("run_id"), "new_seed": body.get("new_seed", False),
            }
            if stage_id == "context_ir" and action == "approve":
                prompt = body.get("prompt", stage["output"].get("text", ""))
                if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 16000:
                    raise MediaError("invalid_prompt", "Approved prompt is required.")
                payload["prompt"] = prompt.strip()
            operation = {**operation, "payload": payload, "status": "submitted"}
            self.store.finish_operation(job_id, idem, operation)
            try:
                await self.h3.action(job["provider_state"]["project_id"], stage_id, action, payload)
                self.store.finish_operation(job_id, idem, {**operation, "status": "completed"})
            except UnknownOutcome:
                self.store.finish_operation(job_id, idem, {**operation, "status": "unknown"})
                raise
            await self._video(self.store.get(job_id))
            return {**self.store.get(job_id, owner), "operation_id": operation["id"]}

    async def cancel_image(self, job_id: str, owner: str | None):
        job = self.store.get(job_id, owner)
        if job["kind"] != "image":
            raise MediaError("media_not_found", "Image task was not found.", 404)
        if job["status"] in TERMINAL:
            return job
        status = "cancelled" if job["status"] == "queued" else "cancelling"
        self.store.update(job_id, status=status, cancel_requested=True)
        if self.image_job == job_id and self.image_task and not self.image_task.done():
            self.image_task.cancel()
            await asyncio.gather(self.image_task, return_exceptions=True)
        job = self.store.get(job_id, owner)
        if job["status"] in TERMINAL:
            return job
        state = job["provider_state"]
        if not state.get("submitted") and not state.get("thread_id") and not state.get("task_id"):
            return self.store.update(job_id, status="cancelled")
        if state.get("provider", "codex") == "codex" and state.get("thread_id"):
            try:
                await self.codex.cancel(state, self.store.root / "jobs" / job_id)
            except (MediaError, TimeoutError, OSError):
                self.store.update(job_id, error={
                    "code": "media_cancel_pending", "message": "Cancellation will be reconciled against the original task.",
                })
        return self.store.get(job_id, owner)

    def purge(self, job_id: str, confirmation: str):
        job = self.store.get(job_id, deleted=True)
        if confirmation != job_id or not job.get("deleted") or job["status"] not in TERMINAL:
            raise MediaError("purge_not_confirmed", "Purge requires a deleted terminal task and its exact ID.", 409)
        roots = {(self.store.root / name / job_id).resolve() for name in ("outputs", "sources")}
        outputs = self.store.outputs(job_id)
        paths = {Path(output[key]) for output in outputs for key in ("path", "source_path") if output.get(key)}
        if any(root.parent not in {(self.store.root / name).resolve() for name in ("outputs", "sources")} for root in roots) or any(
            path.is_symlink() or path.resolve().parent not in roots for path in paths
        ):
            raise MediaError("invalid_artifact_path", "Refusing to remove files outside this task.", 409)
        for path in paths:
            path.unlink(missing_ok=True)
        self.store.update(job_id, purged=True, request={"model": job["model"]}, output=None, stages=[])
        return {"id": job_id, "purged": True}

    def public(self, job: dict, *, internal=False, include_data=True) -> dict:
        retired = [output for output in [job.get("output"), *(stage.get("output") for stage in job.get("stages", []))]
                   if output and self.legacy_video(output)]
        if retired:
            pending = sorted(set(job.get("recovery_outputs", [])) | {output["id"] for output in retired})
            if job["status"] != "archiving" or job.get("recovery_outputs") != pending:
                job = self.store.update(job["id"], status="archiving", recovery_outputs=pending)
        fields = ("id", "kind", "model", "status", "created_at", "updated_at", "error",
                  "fallback_applied", "actual_duration", "operation_id")
        result = {field: job[field] for field in fields if field in job}
        result["object"] = "image" if job["kind"] == "image" else "video"
        if internal:
            result.update({field: job.get(field) for field in ("owner", "provider", "provider_state", "request_id", "sync_error", "provider_errors")})
        if job.get("output"):
            result["output"] = self.public_output(job["output"])
        result["stages"] = []
        for stage in job.get("stages", []):
            item = {key: value for key, value in stage.items() if key not in {"run_id", "output"}}
            if stage.get("output"):
                safe_output = self.public_output(stage["output"])
                if safe_output:
                    item["output"] = safe_output
                else:
                    item["status"] = "archiving"
            result["stages"].append(item)
        if include_data and job["kind"] == "image" and job["status"] == "completed":
            output = job["output"]
            result["created"] = int(job["created_at"])
            result["data"] = [{"revised_prompt": job.get("revised_prompt"),
                               "b64_json": base64.b64encode(self.output_path(job, output).read_bytes()).decode()}]
            result["x_1panel"] = {key: output.get(key) for key in ("width", "height", "transparent")}
            result["x_1panel"].update(id=job["id"], fallback_applied=job.get("fallback_applied", False))
        return result

    def output_path(self, job: dict, output: dict) -> Path:
        if self.legacy_video(output):
            raise MediaError("media_representation_retired", "Refresh the task to obtain its current download URL.", 410)
        path = Path(output["path"])
        if not path.is_file() or path.stat().st_size != output["bytes"]:
            pending = sorted(set(job.get("recovery_outputs", [])) | {output["id"]})
            self.store.update(job["id"], status="archiving", recovery_outputs=pending)
            raise MediaError("output_not_ready", "Archived output needs recovery.", 409)
        return path

    @staticmethod
    def legacy_video(output: dict) -> bool:
        return output.get("content_type") == "video/mp4" and not output.get("metadata_stripped")

    @staticmethod
    def public_output(output: dict) -> dict | None:
        if MediaService.legacy_video(output):
            return None
        return {key: value for key, value in output.items() if key not in {"path", "job_id"} and not key.startswith("source_")}
