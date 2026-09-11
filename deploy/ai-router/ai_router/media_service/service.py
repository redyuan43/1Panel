from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import fcntl
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image, ImageOps
try:
    from asyncio import timeout
except ImportError:
    from async_timeout import timeout

from .contracts import (
    BACKGROUNDS, ID_PATTERN, RATIOS, TERMINAL, USE_CASES, MediaError, QuotaExceeded,
    UnknownOutcome, decode_asset, image_info, image_request, legacy_video_enabled,
    video_request,
)
from .providers import CodexProvider, H3Provider, QwenProvider
from .storage import MediaStore
from .video_review import VideoReviewer, technical_review
from .video_workflows import (
    build_prompt_package,
    managed_stages,
    options as video_workflow_options,
    refresh_prompt_hash,
    segmentation_blockers,
    segmentation_enabled,
    workflow_mode,
)


NON_BILLABLE_QWEN_ERRORS = {
    "fallback_incompatible",
    "qwen_not_configured",
    "qwen_request_failed",
}


class _LocalStream:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    async def __aenter__(self):
        self.handle = self.path.open("rb")
        return self

    async def __aexit__(self, *_):
        if self.handle:
            self.handle.close()

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        while chunk := self.handle.read(1024 * 1024):
            yield chunk


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
        self.video_tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.lock_file = None
        self.reviewer = VideoReviewer(self.client)
        from .creative import CreativeService
        self.creative = CreativeService(self)

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
        tasks = [self.runner, self.image_task, *self.video_tasks.values(), *self.creative.tasks.values()]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
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
        await self.creative.tick()
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
                if workflow_mode(job["request"]) != "legacy_pipeline":
                    task = self.video_tasks.get(job["id"])
                    if task and task.done():
                        self.video_tasks.pop(job["id"], None)
                        task = None
                    active_stage = next(
                        (stage for stage in job.get("stages", [])
                         if stage["status"] in {"queued", "running", "reconciling", "cancelling"}),
                        None,
                    )
                    if active_stage and task is None:
                        self.video_tasks[job["id"]] = asyncio.create_task(
                            self._managed_video(job),
                            name=f"media-video-{job['id']}",
                        )
                    continue
                if not legacy_video_enabled():
                    continue
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
            "creative_workflows": {"version": 1, "candidate_counts": [1, 2, 3], "default_duration": 15,
                                   "default_aspect_ratio": "9:16", "automatic_regenerations": 0,
                                   "asset_roles": ["subject", "product", "style", "first_frame", "last_frame", "reference"]},
            "images": {"use_case": USE_CASES, "aspect_ratio": RATIOS, "background": BACKGROUNDS,
                       "n": [1], "max_edit_images": 5, "fallback_max_edit_images": 3,
                       "response_format": ["b64_json", "url"], "mask": False},
            "videos": {
                "available": False,
                "context_ir_billable": True,
                **video_workflow_options(),
            },
        }
        if settings["h3_ready"] and settings["videos_enabled"] and "siyuan-video" in models:
            try:
                result["videos"] = {
                    "available": True,
                    **await self.h3.options(),
                    **video_workflow_options(),
                }
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
        if kind == "video" and workflow_mode(body) == "duration_ladder":
            if not segmentation_enabled():
                raise MediaError(
                    "workflow_unavailable",
                    "Duration ladder is temporarily unavailable; use quality_gate "
                    "for one continuous video generation.",
                    409,
                )
            blockers = segmentation_blockers(body)
            if blockers:
                raise MediaError(
                    "workflow_incompatible",
                    "Duration ladder cannot safely split this prompt; use quality_gate "
                    "so it can remain one continuous 15-second generation.",
                )
        job = self.store.create(owner, kind, body, idem, request_id, settings["queue_limit"])[0]
        if kind == "video" and workflow_mode(body) != "legacy_pipeline" and not job.get("stages"):
            job = self.store.update(
                job["id"],
                status="in_progress",
                stages=managed_stages(body),
                workflow_mode=workflow_mode(body),
                creative_profile=body["creative_profile"],
                aspect_ratio=body["aspect_ratio"],
            )
        return job

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

    def _replace_stage(self, job_id: str, stage_id: str, **changes) -> dict:
        job = self.store.get(job_id)
        stages = []
        found = False
        for stage in job.get("stages", []):
            if stage["id"] == stage_id:
                stages.append({**stage, **changes})
                found = True
            else:
                stages.append(stage)
        if not found:
            raise MediaError("stage_not_found", "Stage is not in this pipeline.", 404)
        return self.store.update(job_id, stages=stages)

    @staticmethod
    def _stage(job: dict, stage_id: str) -> dict:
        stage = next((item for item in job.get("stages", []) if item["id"] == stage_id), None)
        if stage is None:
            raise MediaError("stage_not_found", "Stage is not in this pipeline.", 404)
        return stage

    async def _managed_video(self, original: dict):
        job_id = original["id"]
        job = self.store.get(job_id)
        stage = next(
            (item for item in job.get("stages", [])
             if item["status"] in {"queued", "running", "reconciling", "cancelling"}),
            None,
        )
        if stage is None:
            return
        stage_id = stage["id"]
        try:
            if stage["status"] == "queued":
                run_id = "run_" + uuid4().hex
                job = self._replace_stage(
                    job_id,
                    stage_id,
                    status="running",
                    progress=2,
                    run_id=run_id,
                    output_id=None,
                    output=None,
                    review=None,
                    error=None,
                    started_at=time.time(),
                )
                stage = self._stage(job, stage_id)
            if stage.get("cancel_requested") or stage["status"] == "cancelling":
                await self._cancel_managed_executions(job_id, stage_id)
                self._replace_stage(job_id, stage_id, status="cancelled", progress=0)
                self.store.update(job_id, status="cancelled")
                return
            if stage_id == "plan":
                await self._run_plan(job_id, stage)
            else:
                await self._run_generated_stage(job_id, stage)
        except asyncio.CancelledError:
            current = self.store.get(job_id)
            stage = self._stage(current, stage_id)
            if stage["status"] not in TERMINAL | {"awaiting_approval", "approved"}:
                self._replace_stage(
                    job_id,
                    stage_id,
                    status="cancelling" if stage.get("cancel_requested") else "reconciling",
                )
            raise
        except UnknownOutcome as exc:
            current = self.store.get(job_id)
            current_stage = self._stage(current, stage_id)
            self._replace_stage(
                job_id,
                stage_id,
                status=(
                    "cancelling"
                    if current_stage.get("cancel_requested")
                    else "reconciling"
                ),
                error={"code": exc.code, "message": str(exc)},
            )
        except MediaError as exc:
            self._replace_stage(
                job_id,
                stage_id,
                status="cancelled" if exc.code == "media_cancelled" else "failed",
                error={"code": exc.code, "message": str(exc)},
            )
            self.store.update(job_id, status="cancelled" if exc.code == "media_cancelled" else "failed")
        except Exception:
            self._replace_stage(
                job_id,
                stage_id,
                status="reconciling",
                error={"code": "media_outcome_unknown", "message": "Managed video task requires reconciliation."},
            )

    async def _run_plan(self, job_id: str, stage: dict):
        job = self.store.get(job_id)
        package = job.get("prompt_package") or build_prompt_package(job["request"])
        if package.get("anchor_seconds"):
            package = await self._ensure_anchors(job_id, package, stage["run_id"], "plan")
        anchors = [
            self.store.artifact(item["artifact_id"])
            for item in package.get("anchors", [])
        ]
        if anchors:
            sheet = await self._anchor_contact_sheet(job_id, package, anchors)
            package["anchor_contact_sheet"] = {
                "artifact_id": sheet["id"],
                "sha256": sheet["sha256"],
            }
            anchors.append(sheet)
        package = refresh_prompt_hash(package)
        text = json.dumps(package, ensure_ascii=False, indent=2)
        version = "out_" + hashlib.sha256(
            f"{job_id}:{stage['run_id']}:{package['prompt_hash']}".encode()
        ).hexdigest()
        output = await self.archive(
            job_id,
            version,
            data=text.encode(),
            content_type="text/plain",
            text=text,
            stage="plan",
            prompt_hash=package["prompt_hash"],
        )
        self.store.update(job_id, prompt_package=package)
        self._replace_stage(
            job_id,
            "plan",
            status="awaiting_approval",
            progress=100,
            output_id=version,
            output=output,
            artifacts=anchors,
            completed_at=time.time(),
        )

    async def _anchor_contact_sheet(
        self,
        job_id: str,
        package: dict,
        anchors: list[dict],
    ) -> dict:
        digest = hashlib.sha256(
            ":".join(anchor["sha256"] for anchor in anchors).encode()
        ).hexdigest()
        version = "out_anchor_sheet_" + digest
        try:
            existing = self.store.artifact(version)
            if Path(existing["path"]).is_file():
                return existing
        except MediaError:
            pass
        cells = []
        for anchor in anchors:
            with Image.open(anchor["path"]) as source:
                cells.append(ImageOps.contain(source.convert("RGB"), (360, 360)))
        columns = min(2, len(cells))
        rows = (len(cells) + columns - 1) // columns
        canvas = Image.new("RGB", (columns * 360, rows * 360), "black")
        for index, cell in enumerate(cells):
            left = (index % columns) * 360 + (360 - cell.width) // 2
            top = (index // columns) * 360 + (360 - cell.height) // 2
            canvas.paste(cell, (left, top))
        output = io.BytesIO()
        canvas.save(output, format="PNG", optimize=False)
        data = output.getvalue()
        return await self.archive(
            job_id,
            version,
            data=data,
            stage="plan",
            role="anchor_contact_sheet",
            prompt_hash=package["prompt_hash"],
            **image_info(data),
        )

    async def _ensure_anchors(self, job_id: str, package: dict, run_id: str, stage_id: str) -> dict:
        job = self.store.get(job_id)
        state = dict(job.get("provider_state", {}))
        anchors = dict(state.get("anchors", {}))
        previous = None
        output = []
        for seconds in package["anchor_seconds"]:
            key = str(seconds)
            existing = anchors.get(key)
            if existing:
                try:
                    artifact = self.store.artifact(existing["artifact_id"])
                    if Path(artifact["path"]).is_file():
                        output.append({
                            "timestamp_seconds": seconds,
                            "artifact_id": artifact["id"],
                            "sha256": artifact["sha256"],
                        })
                        previous = artifact
                        continue
                except MediaError:
                    pass
            artifact = await self._create_anchor(
                job_id,
                seconds,
                package,
                run_id,
                previous,
            )
            anchors[key] = {"artifact_id": artifact["id"], "run_id": run_id}
            state["anchors"] = anchors
            self.store.update(job_id, provider_state=state)
            output.append({
                "timestamp_seconds": seconds,
                "artifact_id": artifact["id"],
                "sha256": artifact["sha256"],
            })
            previous = artifact
            self._replace_stage(
                job_id,
                stage_id,
                progress=min(85, 10 + int(70 * len(output) / len(package["anchor_seconds"]))),
            )
        return {**package, "anchors": output}

    async def _create_anchor(
        self,
        job_id: str,
        seconds: int,
        package: dict,
        run_id: str,
        previous: dict | None,
    ) -> dict:
        job = self.store.get(job_id)
        request = job["request"]
        supplied = None
        if seconds == 0:
            supplied = request["assets"].get("first_frame")
        if seconds == request["duration"]:
            supplied = request["assets"].get("last_frame") or supplied
        version = "out_anchor_" + hashlib.sha256(
            f"{job_id}:{run_id}:{seconds}:{package['prompt_hash']}".encode()
        ).hexdigest()
        if supplied:
            data = decode_asset(supplied, image=True)
            return await self.archive(
                job_id,
                version,
                data=data,
                stage="plan",
                role="anchor",
                timestamp_seconds=seconds,
                **image_info(data),
            )
        references = []
        if previous:
            data = Path(previous["path"]).read_bytes()
            references = [{
                "data": base64.b64encode(data).decode(),
                "content_type": previous["content_type"],
            }]
        profile = package["creative_profile"]
        use_case = "product" if profile in {"ecommerce", "social_commerce", "tvc", "ai_ad", "seeding"} else (
            "illustration" if profile == "dynamic_comic" else "photo"
        )
        prompt = (
            f"Create the exact approved boundary frame at {seconds} seconds for a {package['duration_seconds']}-second "
            f"video. Preserve the same adult subject identity, face, hair, body proportions, props, environment, "
            f"lighting, camera axis and aspect ratio. This is a continuity anchor, not a collage. "
            f"Primary request: {request['prompt']}"
        )
        body = image_request({
            "model": "siyuan-image",
            "prompt": prompt,
            "use_case": use_case,
            "aspect_ratio": "portrait" if request["aspect_ratio"] == "9:16" else "landscape",
            "background": "opaque",
            "response_format": "b64_json",
            "n": 1,
            "images": references,
        }, edit=bool(references))
        directory = self.store.root / "jobs" / job_id / "anchors" / str(seconds)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self.store.get(job_id)
        state = dict(current.get("provider_state", {}))
        image_states = dict(state.get("anchor_generation", {}))
        image_state = dict(image_states.get(str(seconds), {}))

        def checkpoint(value):
            nonlocal image_state
            image_state = dict(value)
            latest = self.store.get(job_id)
            provider_state = dict(latest.get("provider_state", {}))
            values = dict(provider_state.get("anchor_generation", {}))
            values[str(seconds)] = image_state
            provider_state["anchor_generation"] = values
            self.store.update(job_id, provider_state=provider_state)

        try:
            result = await self.codex.generate(body, image_state, checkpoint, directory)
        except QuotaExceeded:
            settings = self.store.settings()
            if not settings["paid_fallback"]:
                raise
            reservation = f"{job_id}:anchor:{run_id}:{seconds}"
            self.store.reserve_paid(reservation, settings["daily_paid_images"])
            try:
                result = await self.qwen.generate(body, {}, lambda _: None, directory)
            except Exception:
                self.store.release_paid(reservation)
                raise
        data = result.get("data")
        if data is None:
            data = await self._image_url(result["url"])
        return await self.archive(
            job_id,
            version,
            data=data,
            stage="plan",
            role="anchor",
            timestamp_seconds=seconds,
            **image_info(data),
        )

    async def _run_generated_stage(self, job_id: str, stage: dict):
        job = self.store.get(job_id)
        package = job.get("prompt_package") or build_prompt_package(job["request"])
        if len(package["segments"]) > 1 and not package.get("anchors"):
            package = await self._ensure_anchors(
                job_id,
                package,
                stage["run_id"],
                stage["id"],
            )
            package = refresh_prompt_hash(package)
            self.store.update(job_id, prompt_package=package)
        package = self._effective_prompt_package(job, stage, package)
        stage_id = stage["id"]
        profile = "quality" if stage_id == "final" else "preview"
        if workflow_mode(job["request"]) == "duration_ladder":
            index = {"clip_5s": 0, "clip_10s": 1, "clip_15s": 2}[stage_id]
            requested_segments = [package["segments"][index]]
        else:
            requested_segments = package["segments"]
        results = await self._execution_batches(
            job_id,
            stage_id,
            stage["run_id"],
            profile,
            package,
            requested_segments,
        )
        self._require_managed_stage_active(job_id, stage_id)
        sources = results
        boundary_anchors = self._boundary_anchor_evidence(package, requested_segments)
        if workflow_mode(job["request"]) == "duration_ladder" and stage_id != "clip_5s":
            previous_id = "clip_5s" if stage_id == "clip_10s" else "clip_10s"
            previous = self._stage(self.store.get(job_id), previous_id)
            if previous["status"] != "approved" or not previous.get("output"):
                raise MediaError("stale_stage_output", "Duration extension requires the approved preceding clip.", 409)
            sources = [previous["output"], *results]
            boundary_anchors = [
                *previous["output"].get("shared_boundary_anchors", []),
                self._anchor_evidence(
                    package,
                    requested_segments[0]["first_anchor_seconds"],
                ),
            ]
        expected = self._expected_video(job["request"], profile)
        expected_duration = 0.0
        expected_frames = 0
        for source in sources:
            gate = source.get("internal_technical_gate", {})
            expected_duration += float(
                source.get("expected_duration_seconds")
                or gate.get("duration_seconds")
                or 0
            )
            expected_frames += int(
                source.get("expected_frame_count")
                or gate.get("frame_count")
                or 0
            )
        if len(sources) > 1:
            expected_duration -= (len(sources) - 1) / 24
            expected_frames -= len(sources) - 1
        if expected_duration > 0:
            expected["duration_seconds"] = expected_duration
        if expected_frames > 0:
            expected["frame_count"] = expected_frames
        assembled = await self._assemble_stage(
            job_id,
            stage_id,
            stage["run_id"],
            sources,
            boundary_anchors,
            expected,
        )
        self._require_managed_stage_active(job_id, stage_id)
        expected["boundaries_seconds"] = assembled.get("boundary_seconds", [])
        expected["shared_boundary_anchors"] = boundary_anchors
        try:
            reference_sheets = []
            contact_sheet_id = (package.get("anchor_contact_sheet") or {}).get("artifact_id")
            if contact_sheet_id:
                reference_sheets.append(Path(self.store.artifact(contact_sheet_id)["path"]))
            review = await self.reviewer.review(
                Path(assembled["path"]),
                output_id=assembled["output_id"],
                artifact_sha256=assembled["sha256"],
                prompt_package=package,
                expected=expected,
                reference_sheets=reference_sheets,
                technical=assembled["internal_technical_gate"],
            )
        except MediaError as exc:
            if exc.code == "video_technical_review_failed":
                raise
            review = {
                "review_id": "rev_" + hashlib.sha256(
                    f"{assembled['output_id']}:{assembled['sha256']}:manual".encode()
                ).hexdigest(),
                "output_id": assembled["output_id"],
                "artifact_sha256": assembled["sha256"],
                "prompt_hash": package.get("prompt_hash"),
                "manual_review_required": True,
                "technical": assembled["internal_technical_gate"],
                "semantic": {
                    "status": "manual_required",
                    "verdict": "CONDITIONAL_PASS",
                    "confidence": 0.0,
                    "issues": [{"category": "reviewer", "severity": "warning", "message": str(exc)}],
                    "scores": {},
                    "revised_prompt": "",
                    "recommended_action": "manual_review",
                },
            }
        duration = review.get("technical", {}).get("duration_seconds")
        if duration:
            self.store.update(job_id, actual_duration=duration)
        self._require_managed_stage_active(job_id, stage_id)
        self._replace_stage(
            job_id,
            stage_id,
            status="awaiting_approval",
            progress=100,
            output_id=assembled["output_id"],
            output=assembled,
            review=review,
            execution_outputs=[item["output_id"] for item in results],
            execution_lanes=[
                item["internal_lane_id"]
                for item in results
                if item.get("internal_lane_id")
            ],
            internal_gpu_uuids=[
                item["internal_gpu_uuid"]
                for item in results
                if item.get("internal_gpu_uuid")
            ],
            completed_at=time.time(),
        )

    async def _execution_batches(
        self,
        job_id: str,
        stage_id: str,
        run_id: str,
        profile: str,
        package: dict,
        segments: list[dict],
    ) -> list[dict]:
        batch_size = 2 if profile == "quality" else 3
        results = []
        for offset in range(0, len(segments), batch_size):
            group = segments[offset:offset + batch_size]
            tasks = [
                asyncio.create_task(
                    self._execute_segment(job_id, stage_id, run_id, profile, package, segment),
                    name=f"{job_id}-{stage_id}-{segment['id']}",
                )
                for segment in group
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure = next(
                (
                    task.exception()
                    for task in tasks
                    if task in done
                    and not task.cancelled()
                    and task.exception() is not None
                ),
                None,
            )
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await self._cancel_managed_executions(job_id, stage_id)
                raise failure
            completed = await asyncio.gather(*tasks)
            results.extend(completed)
        return results

    async def _execute_segment(
        self,
        job_id: str,
        stage_id: str,
        run_id: str,
        profile: str,
        package: dict,
        segment: dict,
    ) -> dict:
        job = self.store.get(job_id)
        state = dict(job.get("provider_state", {}))
        runs = dict(state.get("managed_executions", {}))
        run = dict(runs.get(run_id, {}))
        executions = dict(run.get("segments", {}))
        saved = dict(executions.get(segment["id"], {}))
        execution_id = saved.get("execution_id")
        assets, mode = self._execution_assets(job, package, segment)
        active_stage = self._stage(job, stage_id)
        prompt = segment["prompt"]
        requested_seed = int(job["request"]["seed"])
        seed_offset = int(active_stage.get("seed_offset", 0))
        body = {
            "operation_id": f"{job_id}_{stage_id}_{run_id}_{segment['id']}",
            "profile": profile,
            "mode": mode,
            "prompt": prompt,
            "duration": segment["duration_seconds"],
            "seed": requested_seed + package["segments"].index(segment) + seed_offset
            if requested_seed >= 0 else -1,
            "audio_policy": job["request"]["audio_policy"],
            "aspect_ratio": job["request"]["aspect_ratio"],
            "watermark": job["request"]["watermark"],
            "metadata": {
                "router_job_id": job_id,
                "stage": stage_id,
                "run_id": run_id,
                "segment_id": segment["id"],
                "prompt_hash": package["prompt_hash"],
            },
        }
        if not execution_id:
            created = await self.h3.create_execution(body, assets)
            execution_id = created.get("execution_id")
            if not isinstance(execution_id, str) or not ID_PATTERN.fullmatch(execution_id):
                raise UnknownOutcome("H3 execution creation returned no stable identifier.")
            saved.update(
                execution_id=execution_id,
                status=created.get("status", "submitted"),
                lane_id=created.get("lane_id"),
                gpu_uuid=created.get("gpu_uuid"),
                actual_duration=created.get("actual_duration"),
                frame_count=created.get("frame_count"),
            )
            executions = await self._save_managed_execution(
                job_id,
                run_id,
                segment["id"],
                saved,
            )
        deadline = time.monotonic() + int(os.environ.get(
            "AI_ROUTER_H3_QUALITY_TIMEOUT" if profile == "quality" else "AI_ROUTER_H3_PREVIEW_TIMEOUT",
            "14400" if profile == "quality" else "3600",
        ))
        while True:
            current_job = self.store.get(job_id)
            current_stage = self._stage(current_job, stage_id)
            if current_stage.get("cancel_requested") or current_stage["status"] == "cancelling":
                await self.h3.cancel_execution(
                    execution_id,
                    f"{job_id}_{stage_id}_{run_id}_{segment['id']}_cancel",
                )
                raise MediaError("media_cancelled", "Video execution was cancelled.", 409)
            execution = await self.h3.get_execution(execution_id)
            status = execution.get("status")
            progress = max(3, min(95, int(execution.get("progress") or 0)))
            saved.update(
                status=status,
                progress=progress,
                lane_id=execution.get("lane_id") or saved.get("lane_id"),
                gpu_uuid=execution.get("gpu_uuid") or saved.get("gpu_uuid"),
                actual_duration=execution.get("actual_duration") or saved.get("actual_duration"),
                frame_count=execution.get("frame_count") or saved.get("frame_count"),
            )
            executions = await self._save_managed_execution(
                job_id,
                run_id,
                segment["id"],
                saved,
            )
            average = sum(int(value.get("progress") or 0) for value in executions.values()) / max(1, len(executions))
            self._replace_stage(job_id, stage_id, progress=max(3, min(94, int(average))))
            if status == "completed":
                self._require_managed_stage_active(job_id, stage_id)
                output_id = execution.get("output_id")
                if not isinstance(output_id, str) or not ID_PATTERN.fullmatch(output_id):
                    raise UnknownOutcome("Completed H3 execution has no immutable output identifier.")
                stream = await self.h3.download_execution(execution_id)
                artifact = await self.archive(
                    job_id,
                    output_id,
                    stream=stream,
                    content_type="video/mp4",
                    stage=f"{stage_id}_{segment['id']}",
                    execution_id=execution_id,
                    segment_id=segment["id"],
                    internal_lane_id=saved.get("lane_id"),
                    internal_gpu_uuid=saved.get("gpu_uuid"),
                    expected_duration_seconds=saved.get("actual_duration"),
                    expected_frame_count=saved.get("frame_count"),
                    internal_hidden=True,
                )
                self._require_managed_stage_active(job_id, stage_id)
                saved.update(status="completed", output_id=output_id, artifact_id=artifact["id"], progress=100)
                await self._save_managed_execution(
                    job_id,
                    run_id,
                    segment["id"],
                    saved,
                )
                return artifact
            if status in {"failed", "cancelled"}:
                code = "media_cancelled" if status == "cancelled" else "h3_execution_failed"
                raise MediaError(code, execution.get("error") or "H3 video execution failed.", 502)
            if time.monotonic() >= deadline:
                raise UnknownOutcome("H3 execution exceeded its reconciliation window.")
            await asyncio.sleep(5)

    async def _save_managed_execution(
        self,
        job_id: str,
        run_id: str,
        segment_id: str,
        value: dict,
    ) -> dict:
        async with self.lock(job_id):
            latest = self.store.get(job_id)
            provider_state = dict(latest.get("provider_state", {}))
            runs = dict(provider_state.get("managed_executions", {}))
            run = dict(runs.get(run_id, {}))
            executions = dict(run.get("segments", {}))
            executions[segment_id] = dict(value)
            run["segments"] = executions
            runs[run_id] = run
            provider_state["managed_executions"] = runs
            self.store.update(job_id, provider_state=provider_state)
            return executions

    def _execution_assets(self, job: dict, package: dict, segment: dict) -> tuple[dict, str]:
        request = job["request"]
        anchors = {item["timestamp_seconds"]: self.store.artifact(item["artifact_id"])
                   for item in package.get("anchors", [])}
        if not anchors:
            return dict(request["assets"]), request["mode"]

        def asset(seconds: int) -> dict:
            artifact = anchors[seconds]
            data = Path(artifact["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
                raise MediaError(
                    "boundary_anchor_hash_mismatch",
                    f"Approved boundary anchor T{seconds} no longer matches its immutable hash.",
                    409,
                )
            return {
                "data": base64.b64encode(data).decode(),
                "content_type": artifact["content_type"],
            }

        assets = {
            "first_frame": asset(segment["first_anchor_seconds"]),
            "last_frame": asset(segment["last_anchor_seconds"]),
        }
        for name in ("reference_image", "reference_video", "reference_audio"):
            if name in request["assets"]:
                assets[name] = request["assets"][name]
        return assets, "fl2v"

    @staticmethod
    def _anchor_evidence(package: dict, timestamp: int) -> dict:
        anchor = next(
            (
                item
                for item in package.get("anchors", [])
                if item.get("timestamp_seconds") == timestamp
            ),
            None,
        )
        if not anchor or not anchor.get("artifact_id") or not anchor.get("sha256"):
            raise MediaError(
                "missing_boundary_anchor",
                f"Approved boundary anchor T{timestamp} is unavailable.",
                409,
            )
        return {
            "timestamp_seconds": timestamp,
            "artifact_id": anchor["artifact_id"],
            "sha256": anchor["sha256"],
        }

    @classmethod
    def _boundary_anchor_evidence(cls, package: dict, segments: list[dict]) -> list[dict]:
        evidence = []
        for left, right in zip(segments, segments[1:]):
            timestamp = left.get("last_anchor_seconds")
            if timestamp != right.get("first_anchor_seconds"):
                raise MediaError(
                    "boundary_anchor_mismatch",
                    "Adjacent segments do not share the same approved boundary timestamp.",
                    409,
                )
            evidence.append(cls._anchor_evidence(package, timestamp))
        return evidence

    async def _assemble_stage(
        self,
        job_id: str,
        stage_id: str,
        run_id: str,
        sources: list[dict],
        boundary_anchors: list[dict],
        expected: dict,
    ) -> dict:
        directory = self.store.root / "jobs" / job_id / "assembled"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        assembled = directory / f"{stage_id}-{run_id}.mp4"
        component_durations = [
            float(json.loads(await self.media_command(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(item["path"]),
            )).get("format", {}).get("duration") or 0)
            for item in sources
        ]
        boundaries = []
        elapsed = 0.0
        for index, duration in enumerate(component_durations):
            if index:
                boundaries.append(round(elapsed, 6))
                elapsed += max(0.0, duration - 1 / 24)
            else:
                elapsed += duration
        await self._concat_videos([Path(item["path"]) for item in sources], assembled)
        gate = await technical_review(
            assembled,
            expected={
                **expected,
                "boundaries_seconds": boundaries,
                "shared_boundary_anchors": boundary_anchors,
            },
        )
        if not gate["passed"]:
            raise MediaError(
                "video_technical_review_failed",
                "Video failed its technical quality gate before publication.",
                502,
            )
        version = "out_" + hashlib.sha256(
            json.dumps(
                [
                    job_id,
                    stage_id,
                    run_id,
                    [item["output_id"] for item in sources],
                    boundary_anchors,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return await self.archive(
            job_id,
            version,
            stream=_LocalStream(assembled),
            content_type="video/mp4",
            stage=stage_id,
            component_output_ids=[item["output_id"] for item in sources],
            component_duration_seconds=component_durations,
            boundary_seconds=boundaries,
            shared_boundary_anchors=boundary_anchors,
            internal_technical_gate=gate,
        )

    async def _concat_videos(self, sources: list[Path], destination: Path):
        if len(sources) == 1:
            shutil.copyfile(sources[0], destination)
            return
        probes = [
            json.loads(await self.media_command(
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path),
            ))
            for path in sources
        ]
        all_audio = all(any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))
                        for probe in probes)
        command = ["ffmpeg", "-v", "error", "-nostdin"]
        for source in sources:
            command.extend(["-i", str(source)])
        filters = []
        video_labels = []
        audio_labels = []
        for index in range(len(sources)):
            trim = "trim=start_frame=1," if index else ""
            filters.append(f"[{index}:v]{trim}setpts=PTS-STARTPTS[v{index}]")
            video_labels.append(f"[v{index}]")
            if all_audio:
                atrim = "atrim=start=0.041667," if index else ""
                filters.append(f"[{index}:a]{atrim}asetpts=PTS-STARTPTS[a{index}]")
                audio_labels.append(f"[a{index}]")
        filters.append("".join(video_labels) + f"concat=n={len(sources)}:v=1:a=0[vout]")
        audio_output = None
        if all_audio:
            current = audio_labels[0]
            for index, label in enumerate(audio_labels[1:], 1):
                target = f"[ax{index}]"
                filters.append(f"{current}{label}acrossfade=d=0.08:c1=tri:c2=tri{target}")
                current = target
            filters.append(f"{current}apad=pad_dur=1[aout]")
            audio_output = "[aout]"
        temporary = destination.with_suffix(".part.mp4")
        try:
            command.extend([
                "-filter_complex", ";".join(filters),
                "-map", "[vout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p",
            ])
            if audio_output:
                command.extend(["-map", audio_output, "-c:a", "aac", "-b:a", "192k", "-shortest"])
            command.extend(["-movflags", "+faststart", "-y", str(temporary)])
            await self.media_command(*command, seconds=900)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _expected_video(request: dict, profile: str) -> dict:
        portrait = request.get("aspect_ratio") == "9:16"
        if profile == "quality":
            width, height = ((768, 1344) if portrait else (1344, 768))
        else:
            width, height = ((480, 864) if portrait else (864, 480))
        return {
            "width": width,
            "height": height,
            "fps": 24,
            "audio_required": request.get("audio_policy") in {
                "native",
                "reference",
                "lock_source",
            },
        }

    async def _cancel_managed_executions(self, job_id: str, stage_id: str):
        job = self.store.get(job_id)
        stage = self._stage(job, stage_id)
        run = job.get("provider_state", {}).get("managed_executions", {}).get(stage.get("run_id"), {})
        for segment_id, execution in run.get("segments", {}).items():
            if execution.get("status") not in TERMINAL and execution.get("execution_id"):
                try:
                    await self.h3.cancel_execution(
                        execution["execution_id"],
                        f"{job_id}_{stage_id}_{stage.get('run_id')}_{segment_id}_cancel",
                    )
                except UnknownOutcome:
                    raise
                except MediaError as exc:
                    raise UnknownOutcome(
                        f"Cancellation outcome is unknown for segment {segment_id}: {exc}"
                    ) from exc

    def _require_managed_stage_active(self, job_id: str, stage_id: str) -> None:
        stage = self._stage(self.store.get(job_id), stage_id)
        if stage.get("cancel_requested") or stage["status"] == "cancelling":
            raise MediaError("media_cancelled", "Video execution was cancelled.", 409)

    @staticmethod
    def _effective_prompt_package(job: dict, stage: dict, package: dict) -> dict:
        override = stage.get("prompt_override") or job.get("provider_state", {}).get(
            "approved_prompt_override"
        )
        if not override:
            return package
        value = json.loads(json.dumps(package))
        value["effective_prompt_override"] = override
        for segment in value.get("segments", []):
            segment["prompt"] = override
        return refresh_prompt_hash(value)

    async def _video(self, job: dict):
        if workflow_mode(job["request"]) != "legacy_pipeline":
            await self._managed_video(job)
            return
        if not legacy_video_enabled():
            raise MediaError(
                "workflow_unavailable",
                "The Edge H3 pipeline is retired; this historical task is read-only.",
                409,
            )
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
        if not ID_PATTERN.fullmatch(stage_id) or action not in {"start", "approve", "cancel", "regenerate"}:
            raise MediaError("invalid_stage_action", "Invalid stage action.")
        job = self.store.get(job_id, owner)
        if workflow_mode(job["request"]) != "legacy_pipeline":
            return await self._managed_action(job, stage_id, action, body, idem, request_id)
        if not legacy_video_enabled():
            raise MediaError(
                "workflow_unavailable",
                "The Edge H3 pipeline is retired; this historical task cannot be advanced.",
                409,
            )
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

    async def _managed_action(
        self,
        original: dict,
        stage_id: str,
        action: str,
        body: dict,
        idem: str,
        request_id: str | None,
    ) -> dict:
        if not isinstance(body, dict):
            raise MediaError("invalid_stage_action", "Expected an operation object.")
        allowed = {
            "start": {"output_id", "new_seed"},
            "approve": {"output_id"},
            "cancel": set(),
            "regenerate": {"output_id", "review_id", "prompt", "apply_suggestion", "new_seed"},
        }[action]
        if set(body) - allowed:
            raise MediaError("invalid_stage_action", "Unexpected stage parameters.")
        for name in ("new_seed", "apply_suggestion"):
            if name in body and type(body[name]) is not bool:
                raise MediaError("invalid_stage_action", f"{name} must be boolean.")
        if action in {"start", "regenerate"}:
            settings = self.store.settings()
            if not settings["enabled"] or not settings["videos_enabled"]:
                raise MediaError("media_disabled", "New video execution is disabled.", 503)
            if not settings["h3_ready"]:
                raise MediaError("h3_not_verified", "H3 media contract has not been verified.", 503)
            self.space()
        async with self.lock(original["id"]):
            job = self.store.get(original["id"], original["owner"])
            stage = self._stage(job, stage_id)
            operation, created = self.store.operation(
                job["id"],
                idem,
                {"stage": stage_id, "action": action, **body},
                request_id,
            )
            if not created and operation["status"] == "completed":
                return {**job, "operation_id": operation["id"]}
            if (
                not created
                and stage.get("internal_operation_id") == operation["id"]
                and (
                    (action == "approve" and stage["status"] == "approved")
                    or (
                        action in {"start", "regenerate"}
                        and stage["status"] in {
                            "queued",
                            "running",
                            "reconciling",
                            "awaiting_approval",
                            "approved",
                        }
                    )
                    or (
                        action == "cancel"
                        and stage["status"] in {"cancelling", "cancelled"}
                    )
                )
            ):
                self.store.finish_operation(
                    job["id"],
                    idem,
                    {**operation, "status": "completed"},
                )
                return {**job, "operation_id": operation["id"]}
            if action == "approve":
                if (stage["status"] != "awaiting_approval" or not stage.get("output")
                        or body.get("output_id") != stage.get("output_id")):
                    raise MediaError("stale_stage_output", "Approval requires the current archived output.", 409)
                job = self._replace_stage(
                    job["id"],
                    stage_id,
                    status="approved",
                    progress=100,
                    approved_at=time.time(),
                    internal_operation_id=operation["id"],
                )
                if stage.get("prompt_override"):
                    provider_state = dict(job.get("provider_state", {}))
                    provider_state["approved_prompt_override"] = stage["prompt_override"]
                    job = self.store.update(job["id"], provider_state=provider_state)
                if stage_id in {"final", "clip_15s"}:
                    approved = self._stage(job, stage_id)
                    job = self.store.update(job["id"], status="completed", output=approved["output"])
            elif action == "start":
                index = job["stages"].index(stage)
                if index == 0:
                    if stage["status"] not in {"failed", "cancelled"} or body.get("output_id"):
                        raise MediaError(
                            "invalid_stage_action",
                            "The initial managed stage can be restarted only after failure or cancellation.",
                            409,
                        )
                else:
                    previous = job["stages"][index - 1]
                    if previous["status"] != "approved" or previous.get("output_id") != body.get("output_id"):
                        raise MediaError("stale_stage_output", "Start requires the approved predecessor output.", 409)
                    if stage["status"] not in {"pending", "failed", "cancelled"}:
                        raise MediaError(
                            "invalid_stage_action",
                            "Start requires a pending, failed or cancelled stage.",
                            409,
                        )
                job = self._queue_managed_attempt(
                    job,
                    stage_id,
                    new_seed=body.get("new_seed", False),
                    operation_id=operation["id"],
                )
            elif action == "regenerate":
                if (stage["status"] not in {"awaiting_approval", "approved"}
                        or not stage.get("output")
                        or body.get("output_id") != stage.get("output_id")):
                    raise MediaError("stale_stage_output", "Regeneration requires the current archived output.", 409)
                review = stage.get("review") or {}
                if stage_id != "plan" and (
                    not isinstance(body.get("review_id"), str)
                    or body["review_id"] != review.get("review_id")
                ):
                    raise MediaError(
                        "stale_stage_output",
                        "Regeneration requires the exact review for the current output.",
                        409,
                    )
                prompt = body.get("prompt")
                if body.get("apply_suggestion"):
                    prompt = (review.get("semantic") or {}).get("revised_prompt")
                if prompt is not None and (not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 16000):
                    raise MediaError("invalid_prompt", "A non-empty revised prompt is required.")
                job = self._queue_managed_attempt(
                    job,
                    stage_id,
                    new_seed=body.get("new_seed", True),
                    prompt_override=prompt.strip() if isinstance(prompt, str) else None,
                    operation_id=operation["id"],
                )
            else:
                if stage["status"] not in {"queued", "running", "reconciling"}:
                    raise MediaError("stage_not_active", "Stage is not active.", 409)
                job = self._replace_stage(
                    job["id"],
                    stage_id,
                    status="cancelling",
                    cancel_requested=True,
                    internal_operation_id=operation["id"],
                )
            self.store.finish_operation(job["id"], idem, {**operation, "status": "completed"})
            return {**job, "operation_id": operation["id"]}

    def _queue_managed_attempt(
        self,
        job: dict,
        stage_id: str,
        *,
        new_seed: bool,
        prompt_override: str | None = None,
        operation_id: str | None = None,
    ) -> dict:
        index = next(index for index, stage in enumerate(job["stages"]) if stage["id"] == stage_id)
        stages = []
        for current, stage in enumerate(job["stages"]):
            if current < index:
                stages.append(stage)
            elif current == index:
                stages.append({
                    "id": stage_id,
                    "label": stage.get("label", stage_id),
                    "status": "queued",
                    "progress": 1,
                    "run_id": None,
                    "output_id": None,
                    "seed_offset": int(stage.get("seed_offset", 0)) + (1 if new_seed else 0),
                    **(
                        {"internal_operation_id": operation_id}
                        if operation_id
                        else {}
                    ),
                    **({"prompt_override": prompt_override} if prompt_override else {}),
                })
            else:
                stages.append({
                    "id": stage["id"],
                    "label": stage.get("label", stage["id"]),
                    "status": "pending",
                    "progress": 0,
                    "run_id": None,
                    "output_id": None,
                })
        changes = {"status": "in_progress", "stages": stages, "output": None}
        if stage_id == "plan":
            request = dict(job["request"])
            if prompt_override:
                request["prompt"] = prompt_override
            provider_state = dict(job.get("provider_state", {}))
            provider_state.pop("anchors", None)
            provider_state.pop("anchor_generation", None)
            changes.update(
                request=request,
                prompt_package=None,
                provider_state=provider_state,
            )
        return self.store.update(job["id"], **changes)

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
        roots = {(self.store.root / name / job_id).resolve() for name in ("outputs", "sources", "jobs")}
        outputs = self.store.outputs(job_id)
        paths = {Path(output[key]) for output in outputs for key in ("path", "source_path") if output.get(key)}
        allowed_parents = {(self.store.root / name).resolve() for name in ("outputs", "sources", "jobs")}
        if any(root.parent not in allowed_parents for root in roots) or any(
            path.is_symlink() or not any(path.resolve().is_relative_to(root) for root in roots)
            for path in paths
        ):
            raise MediaError("invalid_artifact_path", "Refusing to remove files outside this task.", 409)
        for path in paths:
            path.unlink(missing_ok=True)
        for root in roots:
            if root.exists() and not root.is_symlink():
                shutil.rmtree(root)
        self.store.update(job_id, purged=True, request={"model": job["model"]}, output=None, stages=[])
        return {"id": job_id, "purged": True}

    def public(self, job: dict, *, internal=False, include_data=True) -> dict:
        retired = [output for output in [job.get("output"), *(stage.get("output") for stage in job.get("stages", []))]
                   if output and self.legacy_video(output)]
        if retired:
            pending = sorted(set(job.get("recovery_outputs", [])) | {output["id"] for output in retired})
            if job["status"] != "archiving" or job.get("recovery_outputs") != pending:
                job = self.store.update(job["id"], status="archiving", recovery_outputs=pending)
        fields = (
            "id", "kind", "model", "status", "created_at", "updated_at", "error",
            "fallback_applied", "actual_duration", "operation_id", "workflow_mode",
            "creative_profile", "aspect_ratio",
        )
        result = {field: job[field] for field in fields if field in job}
        if job["kind"] == "video":
            result.setdefault("workflow_mode", workflow_mode(job["request"]))
            result.setdefault("creative_profile", job["request"].get("creative_profile", "auto"))
            result.setdefault("aspect_ratio", job["request"].get("aspect_ratio", "16:9"))
        result["object"] = "image" if job["kind"] == "image" else "video"
        if internal:
            result.update({field: job.get(field) for field in ("owner", "provider", "provider_state", "request_id", "sync_error", "provider_errors")})
        if job.get("output"):
            result["output"] = self.public_output(job["output"], internal=internal)
        result["stages"] = []
        for stage in job.get("stages", []):
            excluded = {"output", "artifacts", "run_id"}
            item = {key: value for key, value in stage.items()
                    if key not in excluded and (internal or not key.startswith("internal_"))}
            if stage.get("review"):
                item["review"] = self._public_review(stage["review"], internal=internal)
            if stage.get("output"):
                safe_output = self.public_output(stage["output"], internal=internal)
                if safe_output:
                    item["output"] = safe_output
                else:
                    item["status"] = "archiving"
            if stage.get("artifacts"):
                item["artifacts"] = [
                    value
                    for output in stage["artifacts"]
                    if (value := self.public_output(output, internal=internal)) is not None
                ]
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
    def public_output(output: dict, *, internal: bool = False) -> dict | None:
        if MediaService.legacy_video(output) or output.get("internal_hidden") and not internal:
            return None
        return {
            key: value
            for key, value in output.items()
            if key not in {"path", "job_id", "internal_hidden"}
            and not key.startswith("source_")
            and (internal or not key.startswith("internal_"))
        }

    @staticmethod
    def _public_review(review: dict, *, internal: bool) -> dict:
        return {
            key: value
            for key, value in review.items()
            if key != "contact_sheet_path" and (internal or not key.startswith("internal_"))
        }
