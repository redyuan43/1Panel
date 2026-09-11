"""Router-managed H3 projects. Install before the catch-all frontend mount.

This module deliberately leaves generation workflows unchanged. A shared SQLite
transaction wraps legacy storage calls so the operation receipt and state change
commit together; dispatch threads cannot read uncommitted state.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool


class StaleRun(Exception):
    pass


def _executor_base() -> str:
    value = os.environ.get("H3_LOCAL_EXECUTOR_URL", "").rstrip("/")
    parsed = urlparse(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
    private_host = (
        parsed.hostname in {"127.0.0.1", "localhost"}
        or (parsed.hostname or "").endswith(".taild500c8.ts.net")
        or bool(address and (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")))
    )
    if parsed.scheme != "http" or not private_host or parsed.path or parsed.username or parsed.password:
        raise HTTPException(503, "private H3 executor is not configured")
    return value


def _executor_headers() -> dict[str, str]:
    secret = os.environ.get("H3_ROUTER_KEY", "")
    if not secret:
        raise HTTPException(503, "private H3 executor credential is not configured")
    return {"Authorization": "Bearer " + secret}


async def _executor_request(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(
            base_url=_executor_base(),
            headers=_executor_headers(),
            timeout=httpx.Timeout(180, read=300),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as error:
        raise HTTPException(503, "Ivan H3 executor is unavailable") from error
    if response.status_code == 503:
        raise HTTPException(503, "No eligible Ivan H3 execution lane is available")
    if response.is_error:
        raise HTTPException(502, "Ivan H3 executor request failed")
    return response


def assert_local_gpu_exclusive():
    service = os.environ.get("H3_COMFY_SERVICE", "comfyui-edge.service")
    try:
        comfy = subprocess.run(
            ["systemctl", "--user", "show", service, "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )
        consumers = subprocess.run([
            "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Local 768P GPU exclusivity check could not complete.") from error
    if comfy.returncode or consumers.returncode:
        raise RuntimeError("Local 768P GPU exclusivity check is unavailable.")
    try:
        comfy_pid = int(comfy.stdout.strip())
    except ValueError as error:
        raise RuntimeError("ComfyUI service has no active process.") from error
    external = []
    for row in csv.reader(consumers.stdout.splitlines()):
        if not row:
            continue
        try:
            pid = int(row[0].strip())
        except (ValueError, IndexError):
            raise RuntimeError("Local 768P GPU consumer data is invalid.")
        if pid != comfy_pid:
            external.append(pid)
    if external:
        raise RuntimeError(
            f"Local 768P requires exclusive GPU access; {len(external)} external compute process(es) are active."
        )


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Cloud upload media verification could not complete.") from exc
    if result.returncode:
        # ffmpeg diagnostics may contain the input's private metadata.
        raise RuntimeError("Cloud upload media verification command failed.")
    return result.stdout


def _media_probe(path):
    return json.loads(_media_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-show_chapters",
        "-show_packets", "-show_data_hash", "sha256", "-of", "json", str(path),
    ]))


def _av_streams(probe):
    streams = probe.get("streams", [])
    video = [stream for stream in streams if stream.get("codec_type") == "video"
             and not stream.get("disposition", {}).get("attached_pic")]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not video or not audio:
        raise RuntimeError("Cloud upload requires the approved video and audio streams.")
    return video + audio


def av_packet_proof(probe):
    result = []
    for stream in _av_streams(probe):
        clock = Fraction(stream["time_base"])
        packets = []
        for packet in probe.get("packets", []):
            if packet["stream_index"] != stream["index"]:
                continue
            if not packet.get("data_hash"):
                raise RuntimeError("Cloud upload packet hashes are unavailable.")
            packet_time = lambda key: str(Fraction(packet[key]) * clock) if key in packet else None
            packets.append([packet["data_hash"], int(packet["size"]),
                            packet_time("pts"), packet_time("dts"), packet_time("duration")])
        if not packets:
            raise RuntimeError("Cloud upload contains an empty audio/video stream.")
        codec = {key: stream.get(key) for key in (
            "codec_type", "codec_name", "codec_tag_string", "profile", "level", "extradata_hash",
            "width", "height", "pix_fmt", "sample_aspect_ratio", "sample_rate", "channels",
            "channel_layout", "color_range", "color_space", "color_transfer", "color_primaries",
        )}
        result.append({"codec": codec, "packets": len(packets),
                       "sha256": hashlib.sha256(json.dumps(packets, separators=(",", ":")).encode()).hexdigest()})
    return result


def decoded_frame_proof(path):
    text = _media_command([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:V", "-map", "0:a",
        "-f", "framehash", "-hash", "sha256", "-",
    ])
    streams = {}
    for row in csv.reader(line for line in text.splitlines() if line and not line.startswith("#")):
        if len(row) != 6:
            raise RuntimeError("Cloud upload decoded frame hashes are unavailable.")
        values = [value.strip() for value in row]
        streams.setdefault(int(values[0]), []).append(values[1:])
    if not streams:
        raise RuntimeError("Cloud upload has no decodable frames.")
    return [{"stream": index, "frames": len(rows),
             "sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}
            for index, rows in sorted(streams.items())]


def assert_clean_metadata(probe):
    if probe.get("chapters") or any(stream.get("codec_type") not in {"video", "audio"}
                                   or stream.get("disposition", {}).get("attached_pic")
                                   for stream in probe.get("streams", [])):
        raise RuntimeError("Cloud upload still contains non-audio/video content.")
    if set(probe.get("format", {}).get("tags", {})) - {"major_brand", "minor_version", "compatible_brands"}:
        raise RuntimeError("Cloud upload still contains private container metadata.")
    # MP4 muxers synthesize these structural fields even when tags are cleared.
    allowed = {"language": {"und", ""}, "handler_name": {"VideoHandler", "SoundHandler", ""},
               "vendor_id": {"[0][0][0][0]", ""}}
    for stream in probe.get("streams", []):
        for key, value in stream.get("tags", {}).items():
            if key not in allowed or value not in allowed[key]:
                raise RuntimeError("Cloud upload still contains private stream metadata.")


def sanitize_upload(source, target):
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file() or source.resolve() == target.resolve():
        raise RuntimeError("Refusing to alter the approved source artifact.")
    original_hash = _file_sha256(source)
    original_probe = _media_probe(source)
    original_packets = av_packet_proof(original_probe)
    original_frames = decoded_frame_proof(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    temporary = target.with_name("." + uuid4().hex + ".mp4")
    try:
        _media_command([
            "ffmpeg", "-nostdin", "-v", "error", "-copyts", "-i", str(source),
            "-map", "0:V", "-map", "0:a", "-c", "copy", "-map_metadata", "-1",
            "-map_metadata:s", "-1", "-map_chapters", "-1", "-dn", "-sn",
            "-metadata", "encoder=", "-metadata:s", "encoder=",
            "-metadata:s", "language=und", "-metadata:s:v", "handler_name=VideoHandler",
            "-metadata:s:a", "handler_name=SoundHandler", "-write_tmcd", "0",
            "-fflags", "+bitexact", "-avoid_negative_ts", "disabled",
            "-movflags", "+faststart", str(temporary),
        ])
        os.chmod(temporary, 0o600)
        clean_probe = _media_probe(temporary)
        assert_clean_metadata(clean_probe)
        if av_packet_proof(clean_probe) != original_packets:
            raise RuntimeError("Cloud upload remux changed audio/video packets or timing.")
        if decoded_frame_proof(temporary) != original_frames:
            raise RuntimeError("Cloud upload remux changed decoded frame data.")
        if _file_sha256(source) != original_hash:
            raise RuntimeError("The approved source changed while preparing its upload.")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {"source_path": str(source), "source_sha256": original_hash, "source_bytes": source.stat().st_size,
                "upload_path": str(target), "upload_sha256": _file_sha256(target), "upload_bytes": target.stat().st_size,
                "packet_proof": original_packets, "decoded_frame_proof": original_frames,
                "metadata_clean": True, "transcoded": False, "prepared_at": time.time()}
    finally:
        temporary.unlink(missing_ok=True)


class Contract:
    def __init__(self, module):
        self.m = module
        self.store = module.STORE
        self.local = threading.local()
        self.original_connect = self.store._connect
        self.original_update = self.store.update
        self.original_spawn = module._spawn
        self.original_regenerate_2k = module.MINIMAX.regenerate_2k
        self.original_run_local_stage = getattr(module, "_run_local_stage", None)
        self.store._connect = self.connect
        self.store.update = self.update
        module._spawn = self.spawn
        module.MINIMAX.regenerate_2k = self.regenerate_2k
        if self.original_run_local_stage is not None:
            module._run_local_stage = self.run_local_stage

    def run_local_stage(self, project_id, stage_id):
        if stage_id == "local_768":
            try:
                assert_local_gpu_exclusive()
            except RuntimeError as error:
                infrastructure_error = getattr(
                    self.m, "BatchInfrastructureError", RuntimeError
                )
                raise infrastructure_error(str(error)) from error
        return self.original_run_local_stage(project_id, stage_id)

    def _approved_upload_source(self, project, source):
        current = self.store.get(project["id"])
        if current is None:
            raise RuntimeError("The approved cloud upload project no longer exists.")
        pipeline = [stage["id"] for stage in self.m.pipeline_for(current)]
        index = pipeline.index("regenerate_2k")
        previous = pipeline[index - 1] if index else None
        if previous not in {"local_768", "cloud_768"}:
            raise RuntimeError("Cloud regeneration requires an approved 768P predecessor.")
        stage, expected = current["stages"][previous], project["stages"][previous]
        active, execution = current["stages"]["regenerate_2k"], project["stages"]["regenerate_2k"]
        if (stage["status"] != "approved" or expected["status"] != "approved"
                or active["status"] != "running" or active.get("cancel_requested")
                or active.get("run_id") != execution.get("run_id")
                or stage.get("run_id") != expected.get("run_id")
                or stage.get("output_id") != expected.get("output_id")
                or current["prompt_approved"] != project["prompt_approved"]
                or not stage.get("artifact") or Path(stage["artifact"]).resolve() != Path(source).resolve()
                or stage["artifact"] != expected.get("artifact")):
            raise RuntimeError("The approved cloud upload version changed.")
        if current.get("router_managed"):
            output_id = stage.get("output_id")
            output = current.get("router_outputs", {}).get(output_id, {})
            if (not output_id or not stage.get("run_id") or output.get("stage") != previous
                    or output.get("run_id") != stage["run_id"]
                    or not output.get("path") or Path(output["path"]).resolve() != Path(source).resolve()):
                raise RuntimeError("The upload does not match the immutable approved H3 output.")
        return current, previous

    def regenerate_2k(self, project, source, destination, *, progress, cancelled):
        current, previous = self._approved_upload_source(project, source)
        if cancelled():
            raise RuntimeError("Cloud upload was cancelled before submission.")
        directory = self.m._project_dir(project["id"]) / "router-upload-private"
        target = directory / ("upload_" + uuid4().hex + ".mp4")
        proof = sanitize_upload(source, target)
        current, previous = self._approved_upload_source(project, source)
        if cancelled():
            raise RuntimeError("Cloud upload was cancelled before submission.")
        proof.update(source_stage=previous, source_output_id=current["stages"][previous].get("output_id"),
                     source_run_id=current["stages"][previous].get("run_id"),
                     generation_run_id=current["stages"]["regenerate_2k"].get("run_id"))

        def record(item):
            self._approved_upload_source(project, source)
            item["stages"]["regenerate_2k"]["router_upload_source"] = proof
        current = self.update(project["id"], record)
        return self.original_regenerate_2k(current, target, destination, progress=progress, cancelled=cancelled)

    def initialize(self):
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS router_operations (
                operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                project_id TEXT, result_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS router_executions (
                execution_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                value_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS router_execution_operations (
                operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                result_json TEXT NOT NULL)""")

    @contextlib.contextmanager
    def connect(self):
        existing = getattr(self.local, "db", None)
        if existing is not None:
            yield existing
            return
        dispatches = []
        with self.store._lock:
            db = self.original_connect()
            self.local.rollbacks = []
            try:
                db.execute("BEGIN IMMEDIATE")
                self.local.db = db
                self.local.dispatches = dispatches
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                for cleanup in getattr(self.local, "rollbacks", []):
                    cleanup()
                raise
            finally:
                self.local.db = None
                self.local.dispatches = None
                self.local.rollbacks = None
                db.close()
        # No external work may escape a transaction that did not commit.
        for dispatch in dispatches:
            dispatch()

    def update(self, project_id, mutator):
        with self.connect():
            project = self.store.get(project_id)
            if project is None:
                raise KeyError(project_id)
            execution = getattr(self.local, "execution", None)
            if execution and execution[0] == project_id:
                _, stage_id, run_id = execution
                if project["stages"][stage_id].get("run_id") != run_id:
                    raise StaleRun("stale stage callback")
            before = json.loads(json.dumps(project))
            mutator(project)
            if project.get("router_managed"):
                outputs = project.setdefault("router_outputs", {})
                for stage_id, stage in project["stages"].items():
                    old = before["stages"][stage_id]
                    if stage["status"] == "queued" and old["status"] not in {"queued", "running"} and not (old.get("fleet_pending") and old.get("execution_id") and stage.get("execution_id") == old["execution_id"]):
                        stage["run_id"] = "run_" + uuid4().hex
                        stage.pop("output_id", None)
                    elif not stage.get("run_id") and stage["status"] not in {"pending", "queued"}:
                        stage["run_id"] = old.get("run_id")
                    if stage["status"] == "awaiting_approval" and not stage.get("output_id"):
                        output_id = "out_" + uuid4().hex
                        if stage_id == "context_ir":
                            output = {"text": project["prompt_ir"], "content_type": "text/plain"}
                        else:
                            source = Path(stage["artifact"])
                            directory = self.m._project_dir(project_id) / "router-outputs"
                            directory.mkdir(exist_ok=True)
                            target = directory / (output_id + ".mp4")
                            temporary = directory / (output_id + ".part")
                            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                                shutil.copyfileobj(incoming, outgoing)
                                outgoing.flush()
                                os.fsync(outgoing.fileno())
                            os.replace(temporary, target)
                            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                            try:
                                os.fsync(directory_fd)
                            finally:
                                os.close(directory_fd)
                            stage["artifact"] = str(target)
                            output = {"path": str(target), "content_type": "video/mp4"}
                        stage["output_id"] = output_id
                        outputs[output_id] = {"id": output_id, "stage": stage_id,
                                              "run_id": stage.get("run_id"), **output}
                    # Preserve an existing immutable output through status-only updates.
                    elif stage["status"] in {"approved", "awaiting_approval"} and old.get("output_id"):
                        stage["output_id"] = old["output_id"]
            return self.store.save(project)

    def spawn(self, target, *args):
        project_id = args[0] if args else None
        project = self.store.get(project_id) if project_id else None
        if not project or not project.get("router_managed"):
            return self.original_spawn(target, *args)
        stage = "context_ir" if target == self.m._run_context_ir else args[1]
        run_id = project["stages"][stage].get("run_id")

        def run():
            self.local.execution = (project_id, stage, run_id)
            try:
                target(*args)
            except StaleRun:
                pass
            finally:
                self.local.execution = None
        dispatch = lambda: self.original_spawn(run)
        if getattr(self.local, "db", None) is not None:
            self.local.dispatches.append(dispatch)
            if len(args) > 2 and args[2]:
                self.local.rollbacks.append(self.m.GPU_GATE.release_manual)
        else:
            dispatch()

    def operation(self, key, digest, project_id, callback):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise HTTPException(400, "operation_id is required")
        with self.connect() as db:
            row = db.execute("SELECT digest,result_json FROM router_operations WHERE operation_id=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise HTTPException(409, "operation key conflict")
                return json.loads(row[1])
            value = callback()
            db.execute("INSERT INTO router_operations VALUES (?,?,?,?)",
                       (key, digest, project_id or value["id"], json.dumps(value)))
            return value

    def execution(self, execution_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT digest,value_json FROM router_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def save_execution(self, execution_id, digest, value):
        with self.connect() as db:
            row = db.execute(
                "SELECT digest,value_json FROM router_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise HTTPException(409, "execution operation conflict")
                return json.loads(row[1])
            db.execute(
                "INSERT INTO router_executions VALUES (?,?,?)",
                (execution_id, digest, json.dumps(value)),
            )
        return value

    def update_execution(self, execution_id, **changes):
        with self.connect() as db:
            row = db.execute(
                "SELECT value_json FROM router_executions WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "execution not found")
            value = {**json.loads(row[0]), **changes, "updated_at": time.time()}
            db.execute(
                "UPDATE router_executions SET value_json=? WHERE execution_id=?",
                (json.dumps(value), execution_id),
            )
        return value

    def execution_operation(self, operation_id, digest):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise HTTPException(400, "operation_id is required")
        with self.connect() as db:
            row = db.execute(
                "SELECT digest,result_json FROM router_execution_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        if row[0] != digest:
            raise HTTPException(409, "execution operation conflict")
        return json.loads(row[1])

    def save_execution_operation(self, operation_id, digest, value):
        with self.connect() as db:
            row = db.execute(
                "SELECT digest,result_json FROM router_execution_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise HTTPException(409, "execution operation conflict")
                return json.loads(row[1])
            db.execute(
                "INSERT INTO router_execution_operations VALUES (?,?,?)",
                (operation_id, digest, json.dumps(value)),
            )
        return value


def install(module):
    from .workflows import (
        VALID_AUDIO_POLICIES,
        VALID_MODES,
        VALID_STRATEGIES,
        actual_duration,
        build_workflow,
        validate_project_config,
    )
    from starlette.datastructures import UploadFile

    contract = Contract(module)
    module.app.router.add_event_handler("startup", contract.initialize)
    api = APIRouter(prefix="/api/router")

    def protected(request):
        secret = os.environ.get("H3_ROUTER_KEY", "")
        if not secret or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret):
            raise HTTPException(401, "router authentication required")

    def managed(project_id):
        project = module._require_project(project_id)
        if not project.get("router_managed"):
            raise HTTPException(404, "router project not found")
        return project

    def public(project):
        # Never return H3 filesystem paths, workflow JSON or provider credentials.
        stages = []
        for stage in module.pipeline_for(project):
            value = {key: stage.get(key) for key in ("id", "status", "progress", "run_id", "output_id")}
            if stage.get("cancel_requested") and stage["status"] in {"queued", "running"}:
                value["status"] = "cancelling"
            stages.append(value)
        return {"id": project["id"], "updated_at": project.get("updated_at"),
                "pipeline": stages, "prompt_ir": project["prompt_ir"],
                "prompt_approved": project["prompt_approved"], "actual_duration": project["actual_duration"],
                "internal_errors": {name: stage["error"] for name, stage in project["stages"].items() if stage.get("error")}}

    def public_execution(value):
        return {
            key: value.get(key)
            for key in (
                "execution_id", "status", "progress", "run_id", "output_id",
                "actual_duration", "lane_id", "gpu_uuid", "created_at", "updated_at",
                "error",
            )
            if value.get(key) is not None
        }

    async def refresh_execution(value):
        if value["status"] in {"completed", "failed", "cancelled"}:
            return value
        response = await _executor_request("GET", f"/api/jobs/{value['prompt_id']}")
        job = response.json()
        status = {
            "submitted": "submitted",
            "running": "running",
            "completed": "completed",
            "error": "failed",
            "missing": "failed",
            "cancelled": "cancelled",
        }.get(job.get("status"), "running")
        changes = {
            "status": status,
            "progress": 100 if status == "completed" else 50 if status == "running" else 0,
        }
        if status == "completed":
            if not job.get("output_filename"):
                changes.update(
                    status="failed",
                    progress=0,
                    error="Ivan H3 execution completed without a video output.",
                )
            else:
                changes["output"] = {
                    "filename": job["output_filename"],
                    "subfolder": job.get("output_subfolder") or "",
                    "type": job.get("output_type") or "output",
                }
        elif status == "failed":
            changes["error"] = "Ivan H3 execution failed."
        return await run_in_threadpool(
            contract.update_execution,
            value["execution_id"],
            **changes,
        )

    async def execution_form(form):
        fields, uploads, hashes = {}, {}, {}
        total = 0
        for name, value in form.multi_items():
            if name in fields or name in uploads:
                raise HTTPException(400, "duplicate execution field")
            if isinstance(value, UploadFile):
                content = await value.read()
                total += len(content)
                if not content or total > 512 * 1024 * 1024:
                    raise HTTPException(413, "execution assets exceed the upload limit")
                uploads[name] = {
                    "content": content,
                    "filename": value.filename or name,
                    "content_type": value.content_type or "application/octet-stream",
                }
                hashes[name] = hashlib.sha256(content).hexdigest()
            else:
                fields[name] = value
        allowed = {
            "operation_id", "profile", "mode", "prompt", "duration", "seed",
            "audio_policy", "aspect_ratio", "watermark", "metadata",
        }
        asset_names = {
            "first_frame", "last_frame", "reference_image",
            "reference_video", "reference_audio",
        }
        if set(fields) - allowed or set(uploads) - asset_names:
            raise HTTPException(400, "invalid execution fields")
        execution_id = fields.get("operation_id")
        if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 128:
            raise HTTPException(400, "operation_id is required")
        digest = hashlib.sha256(
            json.dumps([fields, hashes], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return fields, uploads, execution_id, digest

    async def upload_execution_assets(execution_id, uploads):
        assets = {}
        allowed_extensions = {
            "first_frame": {".png", ".jpg", ".jpeg", ".webp"},
            "last_frame": {".png", ".jpg", ".jpeg", ".webp"},
            "reference_image": {".png", ".jpg", ".jpeg", ".webp"},
            "reference_video": {".mp4", ".mov", ".mkv", ".webm"},
            "reference_audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"},
        }
        prefix = hashlib.sha256(execution_id.encode()).hexdigest()[:20]
        for name, upload in uploads.items():
            extension = Path(upload["filename"]).suffix.lower()
            if extension not in allowed_extensions[name]:
                raise HTTPException(400, f"unsupported {name} asset")
            comfy_name = f"h3exec_{prefix}_{name}{extension}"
            await _executor_request(
                "POST",
                f"/api/inputs/{comfy_name}",
                files={
                    "file": (
                        upload["filename"],
                        upload["content"],
                        upload["content_type"],
                    )
                },
            )
            assets[name] = {
                "name": upload["filename"],
                "comfy_name": comfy_name,
                "mime": upload["content_type"],
                "size": len(upload["content"]),
            }
        return assets

    def execution_workflow(fields, assets, execution_id):
        profile = fields.get("profile", "preview")
        if profile not in {"preview", "quality"}:
            raise HTTPException(400, "profile must be preview or quality")
        mode = fields.get("mode")
        prompt = fields.get("prompt")
        if mode not in VALID_MODES or not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, "valid mode and prompt are required")
        try:
            duration = int(fields.get("duration", 5))
            seed = int(fields.get("seed", -1))
        except ValueError as error:
            raise HTTPException(400, "invalid duration or seed") from error
        if seed < 0:
            seed = int.from_bytes(hashlib.sha256(execution_id.encode()).digest()[:8], "big") % (2**63)
        audio_policy = fields.get("audio_policy", "native")
        aspect_ratio = fields.get("aspect_ratio", "16:9")
        if aspect_ratio not in {"16:9", "9:16"}:
            raise HTTPException(400, "invalid aspect_ratio")
        if fields.get("watermark", "false") not in {"true", "false"}:
            raise HTTPException(400, "invalid watermark")
        project = {
            "id": "router-" + hashlib.sha256(execution_id.encode()).hexdigest()[:20],
            "mode": mode,
            "strategy": "fast",
            "duration": duration,
            "actual_duration": actual_duration(duration),
            "seed": seed,
            "audio_policy": audio_policy,
            "watermark": fields.get("watermark", "false") == "true",
            "use_embedded_video_audio": False,
            "prompt_original": prompt.strip(),
            "prompt_approved": prompt.strip(),
            "assets": assets,
        }
        try:
            validate_project_config(project)
            stage = "preview" if profile == "preview" else "local_768"
            workflow, template = build_workflow(project, stage, module.SETTINGS.workflow_root)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(400, str(error)) from error
        width, height = (
            (480, 864) if profile == "preview" else (768, 1344)
        ) if aspect_ratio == "9:16" else (
            (864, 480) if profile == "preview" else (1344, 768)
        )
        steps = 6 if profile == "preview" else 14
        for node in workflow.values():
            inputs = node.setdefault("inputs", {})
            if node.get("class_type") in {
                "MiniMaxH3AudioConditioningT8",
                "MiniMaxH3ImageToVideo",
                "MiniMaxH3ReferenceToVideo",
            }:
                inputs["width"] = width
                inputs["height"] = height
            if node.get("class_type") in {"MiniMaxH3DualClockSamplerT8", "BasicScheduler"}:
                inputs["steps"] = steps
            if node.get("class_type") == "SaveVideo":
                inputs["filename_prefix"] = f"video/router/{project['id']}"
        return project, workflow, template, profile, stage

    @api.get("/options")
    def options(request: Request):
        protected(request)
        return {"contract_version": 1, "mode": sorted(VALID_MODES), "strategy": sorted(VALID_STRATEGIES),
                "audio_policy": sorted(VALID_AUDIO_POLICIES), "duration": {"min": 4, "max": 15},
                "workflow_contract_version": 2,
                "execution_profiles": {"preview": {"max_parallel": 3, "steps": 6},
                                       "quality": {"max_parallel": 2, "steps": 14}},
                "context_ir_billable": True, "stage_outputs": "immutable",
                "cloud_upload_metadata_clean": True,
                "stage_heartbeat": True, "local_768_gpu_exclusive": True,
                "required_assets": {"i2v": ["first_frame"], "l2v": ["last_frame"],
                                    "fl2v": ["first_frame", "last_frame"],
                                    "hybrid": ["first_frame", "last_frame", "reference_image"]}}

    @api.post("/executions")
    async def create_execution(request: Request):
        protected(request)
        form = await request.form()
        try:
            fields, uploads, execution_id, digest = await execution_form(form)
            existing = await run_in_threadpool(contract.execution, execution_id)
            if existing:
                if existing[0] != digest:
                    raise HTTPException(409, "execution operation conflict")
                return public_execution(await refresh_execution(existing[1]))
            assets = await upload_execution_assets(execution_id, uploads)
            project, workflow, template, profile, stage = execution_workflow(
                fields,
                assets,
                execution_id,
            )
            metadata = {}
            if fields.get("metadata"):
                try:
                    metadata = json.loads(fields["metadata"])
                except (TypeError, ValueError) as error:
                    raise HTTPException(400, "metadata must be a JSON object") from error
                if not isinstance(metadata, dict):
                    raise HTTPException(400, "metadata must be a JSON object")
            response = await _executor_request(
                "POST",
                "/prompt",
                json={
                    "prompt": workflow,
                    "extra_data": {
                        "h3": {
                            **metadata,
                            "execution_id": execution_id,
                            "stage": stage,
                            "profile": profile,
                        }
                    },
                },
            )
            payload = response.json()
            prompt_id = str(payload.get("prompt_id", "")).strip()
            if not prompt_id:
                raise HTTPException(502, "Ivan H3 executor returned no prompt id")
            now = time.time()
            value = {
                "execution_id": execution_id,
                "status": "submitted",
                "progress": 3,
                "run_id": prompt_id,
                "prompt_id": prompt_id,
                "output_id": "out_" + hashlib.sha256(execution_id.encode()).hexdigest(),
                "actual_duration": project["actual_duration"],
                "lane_id": payload.get("h3_lane"),
                "gpu_uuid": payload.get("h3_gpu_uuid"),
                "workflow_template": template,
                "profile": profile,
                "metadata": metadata,
                "created_at": now,
                "updated_at": now,
            }
            saved = await run_in_threadpool(
                contract.save_execution,
                execution_id,
                digest,
                value,
            )
            return public_execution(saved)
        finally:
            await form.close()

    @api.get("/executions/{execution_id}")
    async def get_execution(execution_id: str, request: Request):
        protected(request)
        existing = await run_in_threadpool(contract.execution, execution_id)
        if not existing:
            raise HTTPException(404, "execution not found")
        return public_execution(await refresh_execution(existing[1]))

    @api.post("/executions/{execution_id}/cancel")
    async def cancel_execution(execution_id: str, request: Request):
        protected(request)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as error:
            raise HTTPException(400, "invalid JSON body") from error
        if not isinstance(body, dict) or set(body) != {"operation_id"}:
            raise HTTPException(400, "cancel requires operation_id")
        digest = hashlib.sha256(
            json.dumps([execution_id, body], sort_keys=True).encode()
        ).hexdigest()
        replay = await run_in_threadpool(
            contract.execution_operation,
            body["operation_id"],
            digest,
        )
        if replay:
            return replay
        existing = await run_in_threadpool(contract.execution, execution_id)
        if not existing:
            raise HTTPException(404, "execution not found")
        value = await refresh_execution(existing[1])
        if value["status"] not in {"completed", "failed", "cancelled"}:
            await _executor_request(
                "POST",
                f"/api/jobs/{value['prompt_id']}/cancel",
                json={},
            )
            value = await run_in_threadpool(
                contract.update_execution,
                execution_id,
                status="cancelled",
                progress=0,
            )
        result = public_execution(value)
        await run_in_threadpool(
            contract.save_execution_operation,
            body["operation_id"],
            digest,
            result,
        )
        return result

    @api.get("/executions/{execution_id}/output")
    async def execution_output(execution_id: str, request: Request):
        protected(request)
        existing = await run_in_threadpool(contract.execution, execution_id)
        if not existing:
            raise HTTPException(404, "execution not found")
        value = await refresh_execution(existing[1])
        if value["status"] != "completed" or not value.get("output"):
            raise HTTPException(409, "execution output is not ready")
        output = value["output"]
        query = urlencode({
            "filename": output["filename"],
            "subfolder": output["subfolder"],
            "type": output["type"],
        })
        client = httpx.AsyncClient(
            base_url=_executor_base(),
            headers=_executor_headers(),
            timeout=httpx.Timeout(180, read=300),
            trust_env=False,
            follow_redirects=False,
        )
        try:
            response = await client.send(
                client.build_request("GET", f"/view?{query}"),
                stream=True,
            )
            if response.is_error:
                await response.aclose()
                await client.aclose()
                raise HTTPException(502, "Ivan H3 output download failed")
        except BaseException:
            await client.aclose()
            raise

        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            chunks(),
            media_type=response.headers.get("content-type", "video/mp4"),
            headers={"Cache-Control": "private, no-store"},
        )

    @api.post("/projects")
    async def create(request: Request):
        protected(request)
        form = await request.form()
        try:
            return await create_form(form)
        finally:
            await form.close()

    async def create_form(form):
        fields, uploads, hashes = {}, {}, {}
        for name, value in form.multi_items():
            if name in fields or name in uploads:
                raise HTTPException(400, "duplicate project field")
            if isinstance(value, UploadFile):
                uploads[name] = value
                digest = hashlib.sha256()
                while chunk := await value.read(1024 * 1024):
                    digest.update(chunk)
                await value.seek(0)
                hashes[name] = digest.hexdigest()
            else:
                fields[name] = value
        key = fields.pop("operation_id", None)
        digest = hashlib.sha256(json.dumps([fields, hashes], sort_keys=True).encode()).hexdigest()
        allowed = {"name", "mode", "strategy", "prompt", "duration", "seed", "audio_policy",
                   "watermark", "use_embedded_video_audio"}
        if set(fields) - allowed or set(uploads) - {"first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"}:
            raise HTTPException(400, "invalid project fields")
        if not fields.get("mode") or not fields.get("prompt"):
            raise HTTPException(400, "mode and prompt are required")
        fields = {"name": "Video", "strategy": "fast", "audio_policy": "native", **fields}
        try:
            fields["duration"] = int(fields.get("duration", 4))
            fields["seed"] = int(fields.get("seed", -1))
        except ValueError as exc:
            raise HTTPException(400, "invalid duration or seed") from exc
        for name in ("watermark", "use_embedded_video_audio"):
            value = fields.get(name, "false")
            if value not in {"true", "false"}:
                raise HTTPException(400, "invalid boolean field")
            fields[name] = value == "true"
        for name in ("first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"):
            uploads.setdefault(name, None)

        def save():
            project = module.create_project(**fields, **uploads)
            def mark(item):
                item["router_managed"] = True
                item["router_outputs"] = {}
            project = contract.update(project["id"], mark)
            return public(project)
        return await run_in_threadpool(contract.operation, key, digest, None, save)

    @api.get("/projects/{project_id}")
    def get(project_id: str, request: Request):
        protected(request)
        return public(managed(project_id))

    @api.post("/projects/{project_id}/stages/{stage_id}/{action}")
    async def action(project_id: str, stage_id: str, action: str, request: Request):
        protected(request)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "invalid JSON body") from exc
        if not isinstance(body, dict) or stage_id not in module.STAGE_IDS or action not in {"start", "approve", "cancel"}:
            raise HTTPException(400, "invalid stage action")
        allowed = {"operation_id", "expected_run_id", "expected_output_id", "new_seed", "prompt"}
        if set(body) - allowed or "expected_run_id" not in body:
            raise HTTPException(400, "invalid stage action fields")
        if body["expected_run_id"] is not None and not isinstance(body["expected_run_id"], str):
            raise HTTPException(400, "invalid expected_run_id")
        if "new_seed" in body and not isinstance(body["new_seed"], bool):
            raise HTTPException(400, "new_seed must be boolean")
        if action == "approve" or (action == "start" and stage_id != "context_ir"):
            if not isinstance(body.get("expected_output_id"), str) or not body["expected_output_id"]:
                raise HTTPException(400, "expected_output_id is required")
        if action == "approve" and stage_id == "context_ir":
            if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
                raise HTTPException(400, "approved prompt is required")
        digest = hashlib.sha256(json.dumps([project_id, stage_id, action, body], sort_keys=True).encode()).hexdigest()

        def mutate():
            project = managed(project_id)
            stage = project["stages"][stage_id]
            if body.get("expected_run_id") != stage.get("run_id"):
                raise HTTPException(409, "stale stage run")
            if action == "approve" and body.get("expected_output_id") != stage.get("output_id"):
                raise HTTPException(409, "stale stage output")
            if action == "start" and any(item["status"] in {"queued", "running", "cancelling"}
                                         for item in project["stages"].values()):
                raise HTTPException(409, "a project stage is already active")
            if action == "start" and stage_id != "context_ir":
                pipeline = module.pipeline_for(project)
                ids = [item["id"] for item in pipeline]
                if stage_id not in ids:
                    raise HTTPException(409, "stage is not in pipeline")
                previous = pipeline[ids.index(stage_id) - 1]
                if previous["status"] != "approved" or previous.get("output_id") != body.get("expected_output_id"):
                    raise HTTPException(409, "stale prerequisite")
            if action == "start":
                if stage_id == "context_ir":
                    module.start_context_ir(project_id)
                else:
                    module.start_stage(project_id, stage_id, {"new_seed": body.get("new_seed", False)})
            elif action == "approve":
                if stage_id == "context_ir":
                    module.approve_context_ir(project_id, {"prompt": body.get("prompt")})
                else:
                    module.approve_stage(project_id, stage_id)
            else:
                module.cancel_stage(project_id, stage_id)
            return public(managed(project_id))
        return await run_in_threadpool(contract.operation, body.get("operation_id"), digest, project_id, mutate)

    @api.get("/projects/{project_id}/outputs/{output_id}")
    def output(project_id: str, output_id: str, request: Request):
        protected(request)
        artifact = managed(project_id).get("router_outputs", {}).get(output_id)
        if not artifact:
            raise HTTPException(404, "output not found")
        if "text" in artifact:
            return PlainTextResponse(artifact["text"])
        if not artifact.get("path") or not Path(artifact["path"]).is_file():
            raise HTTPException(404, "output not found")
        return FileResponse(artifact["path"], media_type=artifact["content_type"])

    @module.app.middleware("http")
    async def guard_legacy_mutation(request, call_next):
        # Managed projects have one approval writer. The legacy H3 page remains
        # readable, but cannot bypass the versioned contract.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/projects/"):
            project_id = request.url.path.split("/")[3]
            project = module.STORE.get(project_id)
            if project and project.get("router_managed"):
                return JSONResponse({"detail": "Use the Router media console for this versioned project."}, status_code=409)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path.startswith("/api/768-queue"):
            raw = await request.body()
            if raw:
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
                ids = body.get("project_ids", []) if isinstance(body, dict) else []
                projects = (module.STORE.get(str(key).strip()) for key in ids) if isinstance(ids, list) else ()
                if any(project and project.get("router_managed") for project in projects):
                    return JSONResponse({"detail": "Router-managed projects cannot enter legacy batches."}, status_code=409)
        return await call_next(request)

    module.app.include_router(api)
    return contract
