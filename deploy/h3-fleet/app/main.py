from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from .workflow_builder import SUPPORTED_MODES, build_workflow


ACTIVE_STATUSES = {"queued", "reserved", "submitted", "running"}
TERMINAL_STATUSES = {"completed", "error", "cancelled", "missing"}


@dataclass(frozen=True)
class Lane:
    id: str
    url: str
    gpu_uuid: str
    device: str
    preview_only: bool = False
    enabled: bool = True


def load_lanes() -> list[Lane]:
    raw = os.environ.get("H3_FLEET_LANES", "[]")
    values = json.loads(raw)
    lanes = []
    for value in values:
        lanes.append(
            Lane(
                id=str(value["id"]),
                url=str(value["url"]).rstrip("/"),
                gpu_uuid=str(value["gpu_uuid"]),
                device=str(value["device"]),
                preview_only=bool(value.get("preview_only", False)),
                enabled=bool(value.get("enabled", True)),
            )
        )
    if not lanes:
        raise RuntimeError("H3_FLEET_LANES must define at least one lane")
    if len({lane.id for lane in lanes}) != len(lanes):
        raise RuntimeError("H3_FLEET_LANES contains duplicate lane ids")
    if len({lane.url for lane in lanes}) != len(lanes):
        raise RuntimeError("H3_FLEET_LANES contains duplicate lane URLs")
    if len({lane.gpu_uuid for lane in lanes}) != len(lanes):
        raise RuntimeError("H3_FLEET_LANES contains duplicate GPU UUIDs")
    return lanes


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    prompt_id TEXT PRIMARY KEY,
                    upstream_prompt_id TEXT NOT NULL,
                    execution_id TEXT,
                    request_digest TEXT,
                    request_json TEXT,
                    execution_json TEXT,
                    cancel_operation_id TEXT,
                    lane_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    output_filename TEXT,
                    output_subfolder TEXT,
                    output_type TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, definition in (
                ("execution_id", "TEXT"),
                ("request_digest", "TEXT"),
                ("request_json", "TEXT"),
                ("execution_json", "TEXT"),
                ("cancel_operation_id", "TEXT"),
                ("version", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_execution_id
                ON jobs(execution_id) WHERE execution_id IS NOT NULL
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def create(
        self,
        *,
        prompt_id: str,
        upstream_prompt_id: str,
        execution_id: str | None,
        request_digest: str | None,
        lane_id: str,
        stage: str,
        profile: str,
        status: str = "submitted",
        request_data: dict[str, Any] | None = None,
        execution_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    prompt_id, upstream_prompt_id, execution_id, request_digest,
                    request_json, execution_json, lane_id, stage, profile,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_id,
                    upstream_prompt_id,
                    execution_id,
                    request_digest,
                    json.dumps(request_data) if request_data else None,
                    json.dumps(execution_data) if execution_data else None,
                    lane_id,
                    stage,
                    profile,
                    status,
                    now,
                    now,
                ),
            )
        return self.get(prompt_id)

    def get_by_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row else None

    def get(self, prompt_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE prompt_id = ?",
                (prompt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(prompt_id)
        return dict(row)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_for_lane(self, lane_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE lane_id = ? AND status IN ('reserved', 'submitted', 'running')
                ORDER BY created_at
                """,
                (lane_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_output(
        self,
        filename: str,
        subfolder: str,
        output_type: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE output_filename = ?
                  AND COALESCE(output_subfolder, '') = ?
                  AND COALESCE(output_type, 'output') = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (filename, subfolder, output_type),
            ).fetchone()
        return dict(row) if row else None

    def update(self, prompt_id: str, **values: Any) -> dict[str, Any]:
        if not values:
            return self.get(prompt_id)
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments}, version = version + 1 WHERE prompt_id = ?",
                (*values.values(), prompt_id),
            )
        return self.get(prompt_id)


class Fleet:
    def __init__(self) -> None:
        self.lanes = load_lanes()
        self.lanes_by_id = {lane.id: lane for lane in self.lanes}
        self.store = JobStore(
            Path(
                os.environ.get(
                    "H3_FLEET_DATABASE",
                    "/mnt/ivan-ext4-offload/h3-fleet/fleet.sqlite3",
                )
            )
        )
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, read=300.0))
        self.assignment_lock = asyncio.Lock()
        self.draining = False

    @property
    def lane_data_root(self) -> Path:
        return Path(
            os.environ.get(
                "H3_LANE_DATA_ROOT",
                "/mnt/ivan-ext4-offload/h3-fleet/lanes",
            )
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def lane_health(self, lane: Lane) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{lane.url}/system_stats", timeout=10)
            response.raise_for_status()
            payload = response.json()
            device = (payload.get("devices") or [{}])[0]
            return {
                "ok": True,
                "comfyui_version": payload.get("system", {}).get("comfyui_version"),
                "reported_device": device.get("name"),
                "vram_total": device.get("vram_total"),
                "vram_free": device.get("vram_free"),
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    async def refresh_job(self, job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] in TERMINAL_STATUSES:
            return job
        if job["status"] == "queued":
            return await self.dispatch_queued(job)
        if job["status"] == "reserved":
            if time.time() - float(job["updated_at"]) >= 60:
                return self.store.update(job["prompt_id"], status="error")
            return job
        lane = self.lanes_by_id[job["lane_id"]]
        try:
            response = await self.client.get(
                f"{lane.url}/history/{job['upstream_prompt_id']}",
                timeout=30,
            )
            response.raise_for_status()
            record = response.json().get(job["upstream_prompt_id"])
            if record:
                status = record.get("status", {})
                if status.get("status_str") == "error":
                    updated = self.store.update(job["prompt_id"], status="error")
                    self.cleanup_job_inputs(updated)
                    return updated
                output = find_video_output(record)
                if output:
                    updated = self.store.update(
                        job["prompt_id"],
                        status="completed",
                        output_filename=output["filename"],
                        output_subfolder=output["subfolder"],
                        output_type=output["type"],
                    )
                    self.cleanup_job_inputs(updated)
                    return updated
                if status.get("completed"):
                    updated = self.store.update(job["prompt_id"], status="error")
                    self.cleanup_job_inputs(updated)
                    return updated
            queue_response = await self.client.get(f"{lane.url}/queue", timeout=30)
            queue_response.raise_for_status()
            upstream_id = job["upstream_prompt_id"]
            for key in ("queue_running", "queue_pending"):
                if any(
                    len(item) > 1 and str(item[1]) == upstream_id
                    for item in queue_response.json().get(key, [])
                ):
                    return self.store.update(job["prompt_id"], status="running")
            if time.time() - float(job["created_at"]) < 60:
                return job
            updated = self.store.update(job["prompt_id"], status="missing")
            self.cleanup_job_inputs(updated)
            return updated
        except Exception:
            return job

    async def refresh_active(self) -> None:
        for job in reversed(self.store.list(limit=500)):
            if job["status"] in ACTIVE_STATUSES:
                await self.refresh_job(job)

    async def select_lane(self, profile: str) -> Lane | None:
        for lane in self.lanes:
            if not lane.enabled:
                continue
            if lane.preview_only and profile != "preview":
                continue
            if self.store.active_for_lane(lane.id):
                continue
            health = await self.lane_health(lane)
            if health["ok"]:
                return lane
        return None

    async def dispatch_queued(self, job: dict[str, Any]) -> dict[str, Any]:
        async with self.assignment_lock:
            job = self.store.get(job["prompt_id"])
            if job["status"] != "queued":
                return job
            lane = await self.select_lane(job["profile"])
            if lane is None:
                return job
            try:
                payload = json.loads(job.get("request_json") or "")
            except (TypeError, ValueError):
                return self.store.update(job["prompt_id"], status="error")
            job = self.store.update(
                job["prompt_id"],
                lane_id=lane.id,
                status="reserved",
            )
            return await self.submit_reserved(job, payload, lane)

    async def submit_reserved(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        lane: Lane,
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(f"{lane.url}/prompt", json=payload)
        except httpx.HTTPError:
            updated = self.store.update(job["prompt_id"], status="error")
            self.cleanup_job_inputs(updated)
            return updated
        if not response.is_success:
            updated = self.store.update(job["prompt_id"], status="error")
            self.cleanup_job_inputs(updated)
            return updated
        upstream_prompt_id = str(response.json().get("prompt_id", "")).strip()
        if not upstream_prompt_id:
            updated = self.store.update(job["prompt_id"], status="error")
            self.cleanup_job_inputs(updated)
            return updated
        return self.store.update(
            job["prompt_id"],
            upstream_prompt_id=upstream_prompt_id,
            status="submitted",
        )

    def cleanup_input_files(self, filenames: list[str]) -> None:
        for filename in filenames:
            if not re.fullmatch(r"h3exec_[A-Za-z0-9_]{1,120}\.(?:png|jpg|jpeg|webp)", filename):
                continue
            for lane in self.lanes:
                path = self.lane_data_root / lane.id / "input" / filename
                try:
                    if path.parent.resolve() == (
                        self.lane_data_root / lane.id / "input"
                    ).resolve():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    def cleanup_job_inputs(self, job: dict[str, Any]) -> None:
        try:
            contract = json.loads(job.get("execution_json") or "{}")
        except (TypeError, ValueError):
            return
        filenames = contract.get("input_files", [])
        if isinstance(filenames, list):
            self.cleanup_input_files([
                value for value in filenames if isinstance(value, str)
            ])

    async def free_idle_lanes(self) -> None:
        await self.refresh_active()
        for lane in self.lanes:
            if not lane.enabled or self.store.active_for_lane(lane.id):
                continue
            try:
                await self.client.post(
                    f"{lane.url}/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=30,
                )
            except Exception:
                pass


def find_video_output(record: dict[str, Any]) -> dict[str, str] | None:
    for output in record.get("outputs", {}).values():
        for key in ("images", "videos"):
            for item in output.get(key, []):
                filename = str(item.get("filename", ""))
                if filename.lower().endswith((".mp4", ".webm", ".mov")):
                    return {
                        "filename": filename,
                        "subfolder": str(item.get("subfolder", "")),
                        "type": str(item.get("type", "output")),
                    }
    return None


def classify(payload: dict[str, Any]) -> tuple[str, str]:
    metadata = payload.get("extra_data", {}).get("h3", {})
    stage = str(metadata.get("stage", "preview"))
    profile = str(metadata.get("profile", "preview"))
    if profile not in {"preview", "quality"}:
        profile = "preview" if stage == "preview" else "quality"
    return stage, profile


def execution_identity(payload: dict[str, Any]) -> tuple[str | None, str]:
    metadata = payload.get("extra_data", {}).get("h3", {})
    execution_id = metadata.get("execution_id")
    if execution_id is not None:
        if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 160:
            raise HTTPException(status_code=400, detail="invalid h3 execution_id")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return execution_id, digest


def rewrite_record(
    payload: dict[str, Any],
    upstream_prompt_id: str,
    prompt_id: str,
) -> dict[str, Any]:
    if upstream_prompt_id not in payload:
        return {}
    return {prompt_id: payload[upstream_prompt_id]}


@asynccontextmanager
async def lifespan(_: FastAPI):
    fleet.store.initialize()
    yield
    await fleet.close()


fleet = Fleet()
app = FastAPI(title="H3 Fleet", version="0.1.0", lifespan=lifespan)


def router_authenticated(request: Request) -> bool:
    key = os.environ.get("H3_ROUTER_KEY", "")
    return bool(key) and hmac.compare_digest(
        request.headers.get("authorization", ""),
        "Bearer " + key,
    )


def router_protected(request: Request) -> None:
    if not router_authenticated(request):
        raise HTTPException(status_code=401, detail="router authentication required")


@app.middleware("http")
async def private_contract(request: Request, call_next):
    if request.url.path != "/api/health" and not router_authenticated(request):
        return JSONResponse(
            {"detail": "router authentication required"},
            status_code=401,
        )
    return await call_next(request)


def execution_public(job: dict[str, Any]) -> dict[str, Any]:
    status = {
        "queued": "queued",
        "reserved": "submitted",
        "submitted": "submitted",
        "running": "running",
        "completed": "completed",
        "error": "failed",
        "missing": "failed",
        "cancelled": "cancelled",
    }.get(job["status"], "running")
    contract = json.loads(job["execution_json"]) if job.get("execution_json") else {}
    lane = fleet.lanes_by_id.get(job["lane_id"])
    result = {
        "execution_id": job["execution_id"],
        "status": status,
        "progress": 100 if status == "completed" else 50 if status == "running" else 3 if status == "submitted" else 0,
        "run_id": job["prompt_id"],
        "output_id": "out_" + hashlib.sha256(job["execution_id"].encode()).hexdigest(),
        "actual_duration": contract.get("actual_duration"),
        "frame_count": contract.get("frame_count"),
        "lane_id": lane.id if lane else None,
        "gpu_uuid": lane.gpu_uuid if lane else None,
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }
    if status == "failed":
        result["error"] = "Ivan H3 execution failed."
    return result


def queue_contains(payload: dict[str, Any], prompt_id: str) -> str | None:
    for source, status in (
        ("queue_running", "running"),
        ("queue_pending", "pending"),
    ):
        if any(
            len(item) > 1 and str(item[1]) == prompt_id
            for item in payload.get(source, [])
        ):
            return status
    return None


async def replicate_input(
    filename: str,
    content: bytes,
    content_type: str,
    *,
    overwrite: str = "true",
) -> list[dict[str, Any]]:
    async def upload(lane: Lane) -> dict[str, Any]:
        response = await fleet.client.post(
            f"{lane.url}/upload/image",
            data={"type": "input", "overwrite": overwrite},
            files={"image": (filename, content, content_type)},
        )
        return {
            "lane": lane.id,
            "status_code": response.status_code,
            "ok": response.is_success,
            "detail": response.text[:500] if not response.is_success else None,
        }

    results = await asyncio.gather(*(
        upload(lane)
        for lane in fleet.lanes
        if lane.enabled
    ))
    if not all(result["ok"] for result in results):
        raise HTTPException(status_code=502, detail=results)
    return results


@app.get("/api/router/options")
async def router_options(request: Request) -> dict[str, Any]:
    router_protected(request)
    return {
        "contract_version": 1,
        "workflow_contract_version": 2,
        "mode": sorted(SUPPORTED_MODES),
        "strategy": ["fast", "safe"],
        "audio_policy": ["native"],
        "duration": {"min": 4, "max": 15},
        "aspect_ratio": ["16:9", "9:16"],
        "execution_profiles": {
            "preview": {"max_parallel": 3, "steps": 6},
            "quality": {"max_parallel": 2, "steps": 14},
        },
        "required_assets": {
            "i2v": ["first_frame"],
            "l2v": ["last_frame"],
            "fl2v": ["first_frame", "last_frame"],
        },
        "stage_outputs": "immutable",
        "draining": fleet.draining,
    }


@app.post("/api/router/drain")
async def router_drain(request: Request) -> dict[str, Any]:
    router_protected(request)
    fleet.draining = True
    await fleet.refresh_active()
    active = [
        execution_public(job)
        for job in fleet.store.list(limit=500)
        if job["status"] in ACTIVE_STATUSES
    ]
    return {"draining": True, "active": active}


@app.post("/api/router/resume")
async def router_resume(request: Request) -> dict[str, bool]:
    router_protected(request)
    fleet.draining = False
    return {"draining": False}


@app.post("/api/router/executions")
async def router_create_execution(request: Request) -> dict[str, Any]:
    router_protected(request)
    if fleet.draining:
        raise HTTPException(status_code=503, detail="H3 fleet is draining")
    form = await request.form(max_files=2, max_fields=16)
    try:
        fields: dict[str, str] = {}
        uploads: dict[str, dict[str, Any]] = {}
        hashes: dict[str, str] = {}
        total = 0
        for name, value in form.multi_items():
            if name in fields or name in uploads:
                raise HTTPException(status_code=400, detail="duplicate execution field")
            if isinstance(value, StarletteUploadFile):
                content = await value.read()
                total += len(content)
                if not content or total > 20 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="execution anchors exceed the upload limit")
                if name not in {"first_frame", "last_frame"}:
                    raise HTTPException(status_code=400, detail="unsupported managed execution asset")
                extension = Path(value.filename or "").suffix.lower()
                if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise HTTPException(status_code=400, detail="unsupported managed anchor format")
                uploads[name] = {
                    "content": content,
                    "content_type": value.content_type or "application/octet-stream",
                    "extension": extension,
                }
                hashes[name] = hashlib.sha256(content).hexdigest()
            else:
                fields[name] = str(value)
        allowed = {
            "operation_id", "profile", "mode", "prompt", "duration", "seed",
            "audio_policy", "aspect_ratio", "watermark", "metadata",
        }
        if set(fields) - allowed:
            raise HTTPException(status_code=400, detail="invalid execution fields")
        execution_id = fields.get("operation_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
            raise HTTPException(status_code=400, detail="invalid operation_id")
        if fields.get("audio_policy", "native") != "native":
            raise HTTPException(status_code=400, detail="managed Ivan execution supports native audio only")
        if fields.get("watermark", "false") not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="invalid watermark")
        try:
            duration = int(fields.get("duration", "5"))
            seed = int(fields.get("seed", "-1"))
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid duration or seed") from error
        prefix = hashlib.sha256(execution_id.encode()).hexdigest()[:20]
        asset_names = {}
        for name, upload in uploads.items():
            asset_names[name] = (
                f"h3exec_{prefix}_{hashes[name][:12]}_{name}{upload['extension']}"
            )
        try:
            workflow, contract = build_workflow(
                execution_id=execution_id,
                profile=fields.get("profile", "preview"),
                mode=fields.get("mode", ""),
                prompt=fields.get("prompt", ""),
                duration=duration,
                seed=seed,
                aspect_ratio=fields.get("aspect_ratio", "16:9"),
                assets=asset_names,
            )
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        metadata = {}
        if fields.get("metadata"):
            try:
                metadata = json.loads(fields["metadata"])
            except ValueError as error:
                raise HTTPException(status_code=400, detail="metadata must be JSON") from error
            if not isinstance(metadata, dict):
                raise HTTPException(status_code=400, detail="metadata must be an object")
        contract["input_files"] = sorted(asset_names.values())
        replicated = []
        try:
            for name, upload in uploads.items():
                filename = asset_names[name]
                await replicate_input(
                    filename,
                    upload["content"],
                    upload["content_type"],
                )
                replicated.append(filename)
        except Exception:
            fleet.cleanup_input_files(replicated)
            raise
        result = await submit_prompt({
            "prompt": workflow,
            "extra_data": {
                "h3": {
                    **metadata,
                    "execution_id": execution_id,
                    "stage": "preview" if fields.get("profile", "preview") == "preview" else "local_768",
                    "profile": fields.get("profile", "preview"),
                    "contract": contract,
                },
            },
        })
        job = fleet.store.get(result["prompt_id"])
        return execution_public(job)
    finally:
        await form.close()


@app.get("/api/router/executions/{execution_id}")
async def router_get_execution(execution_id: str, request: Request) -> dict[str, Any]:
    router_protected(request)
    job = fleet.store.get_by_execution(execution_id)
    if not job:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution_public(await fleet.refresh_job(job))


@app.post("/api/router/executions/{execution_id}/cancel")
async def router_cancel_execution(execution_id: str, request: Request) -> dict[str, Any]:
    router_protected(request)
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"operation_id"} or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        str(body.get("operation_id", "")),
    ):
        raise HTTPException(status_code=400, detail="cancel requires a valid operation_id")
    job = fleet.store.get_by_execution(execution_id)
    if not job:
        raise HTTPException(status_code=404, detail="execution not found")
    operation_id = str(body["operation_id"])
    existing_operation = job.get("cancel_operation_id")
    if (
        existing_operation
        and existing_operation != operation_id
        and job["status"] not in TERMINAL_STATUSES
    ):
        raise HTTPException(
            status_code=409,
            detail="execution cancellation is already owned by another operation",
        )
    if not existing_operation:
        job = fleet.store.update(
            job["prompt_id"],
            cancel_operation_id=operation_id,
        )
    if job["status"] not in TERMINAL_STATUSES:
        job = await _cancel_job(job["prompt_id"], {})
    return execution_public(job)


@app.get("/api/router/executions/{execution_id}/output")
async def router_execution_output(execution_id: str, request: Request) -> Response:
    router_protected(request)
    job = fleet.store.get_by_execution(execution_id)
    if not job:
        raise HTTPException(status_code=404, detail="execution not found")
    job = await fleet.refresh_job(job)
    if job["status"] != "completed" or not job.get("output_filename"):
        raise HTTPException(status_code=409, detail="execution output is not ready")
    return await view(
        filename=job["output_filename"],
        subfolder=job.get("output_subfolder") or "",
        type=job.get("output_type") or "output",
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    lane_results = []
    for lane in fleet.lanes:
        lane_results.append(
            {
                "id": lane.id,
                "gpu_uuid": lane.gpu_uuid,
                "device": lane.device,
                "preview_only": lane.preview_only,
                "enabled": lane.enabled,
                "active_jobs": fleet.store.active_for_lane(lane.id),
                **await fleet.lane_health(lane),
            }
        )
    root = shutil.disk_usage("/")
    offload = shutil.disk_usage("/mnt/ivan-ext4-offload")
    meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
    available_kib = next(
        int(line.split()[1])
        for line in meminfo.splitlines()
        if line.startswith("MemAvailable:")
    )
    return {
        "ok": any(item["ok"] and item["enabled"] for item in lane_results),
        "service": "h3-fleet",
        "lanes": lane_results,
        "root_available_bytes": root.free,
        "offload_available_bytes": offload.free,
        "memory_available_bytes": available_kib * 1024,
    }


@app.get("/system_stats")
async def system_stats() -> dict[str, Any]:
    for lane in fleet.lanes:
        if not lane.enabled:
            continue
        response = await fleet.client.get(f"{lane.url}/system_stats", timeout=10)
        if response.is_success:
            return response.json()
    raise HTTPException(status_code=503, detail="No healthy H3 GPU lane")


@app.post("/prompt")
async def submit_prompt(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if fleet.draining:
        raise HTTPException(status_code=503, detail="H3 fleet is draining")
    if not isinstance(payload.get("prompt"), dict):
        raise HTTPException(status_code=400, detail="prompt must be an object")
    stage, profile = classify(payload)
    execution_id, request_digest = execution_identity(payload)
    await fleet.refresh_active()
    async with fleet.assignment_lock:
        if execution_id:
            existing = fleet.store.get_by_execution(execution_id)
            if existing:
                if existing["request_digest"] != request_digest:
                    raise HTTPException(
                        status_code=409,
                        detail="H3 execution_id was already used with a different request",
                    )
                lane = fleet.lanes_by_id.get(existing["lane_id"])
                return {
                    "prompt_id": existing["prompt_id"],
                    "h3_lane": lane.id if lane else None,
                    "h3_gpu_uuid": lane.gpu_uuid if lane else None,
                    "idempotent_replay": True,
                }
        lane = await fleet.select_lane(profile)
        prompt_id = uuid.uuid4().hex
        job = fleet.store.create(
            prompt_id=prompt_id,
            upstream_prompt_id="",
            execution_id=execution_id,
            request_digest=request_digest,
            request_data=payload,
            lane_id=lane.id if lane else "",
            stage=stage,
            profile=profile,
            status="reserved" if lane else "queued",
            execution_data=payload.get("extra_data", {}).get("h3", {}).get("contract"),
        )
        if lane:
            job = await fleet.submit_reserved(job, payload, lane)
            if job["status"] == "error":
                raise HTTPException(status_code=502, detail="ComfyUI did not accept the H3 execution")
    lane = fleet.lanes_by_id.get(job["lane_id"])
    return {
        "prompt_id": prompt_id,
        "h3_lane": lane.id if lane else None,
        "h3_gpu_uuid": lane.gpu_uuid if lane else None,
        "queued": job["status"] == "queued",
    }


@app.get("/history/{prompt_id}")
async def history(prompt_id: str) -> dict[str, Any]:
    try:
        job = fleet.store.get(prompt_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown prompt id") from error
    job = await fleet.refresh_job(job)
    if job["status"] in {"queued", "reserved"} or not job["upstream_prompt_id"]:
        return {}
    lane = fleet.lanes_by_id[job["lane_id"]]
    response = await fleet.client.get(
        f"{lane.url}/history/{job['upstream_prompt_id']}",
        timeout=30,
    )
    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    await fleet.refresh_job(job)
    return rewrite_record(response.json(), job["upstream_prompt_id"], prompt_id)


@app.get("/queue")
async def queue() -> dict[str, Any]:
    running: list[list[Any]] = []
    pending: list[list[Any]] = []
    for job in fleet.store.list(limit=500):
        if job["status"] not in ACTIVE_STATUSES:
            continue
        if job["status"] == "queued":
            pending.append([0, job["prompt_id"], {}, {}, []])
            continue
        if not job["lane_id"] or not job["upstream_prompt_id"]:
            continue
        lane = fleet.lanes_by_id[job["lane_id"]]
        response = await fleet.client.get(f"{lane.url}/queue", timeout=30)
        if not response.is_success:
            continue
        for source_key, destination in (
            ("queue_running", running),
            ("queue_pending", pending),
        ):
            for item in response.json().get(source_key, []):
                rewritten = list(item)
                if len(rewritten) > 1 and str(rewritten[1]) == job["upstream_prompt_id"]:
                    rewritten[1] = job["prompt_id"]
                    destination.append(rewritten)
    return {"queue_running": running, "queue_pending": pending}


@app.get("/view")
async def view(
    filename: str,
    subfolder: str = "",
    type: str = "output",
) -> Response:
    job = fleet.store.find_output(filename, subfolder, type)
    if not job:
        for candidate in fleet.store.list(limit=100):
            if candidate["status"] in ACTIVE_STATUSES:
                await fleet.refresh_job(candidate)
        job = fleet.store.find_output(filename, subfolder, type)
    if not job:
        raise HTTPException(status_code=404, detail="Output is not mapped to an H3 job")
    lane = fleet.lanes_by_id[job["lane_id"]]
    response = await fleet.client.get(
        f"{lane.url}/view",
        params={"filename": filename, "subfolder": subfolder, "type": type},
        timeout=300,
    )
    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return Response(
        response.content,
        media_type=response.headers.get("content-type", "application/octet-stream"),
    )


@app.post("/free")
async def free() -> dict[str, bool]:
    await fleet.free_idle_lanes()
    return {"ok": True}


@app.post("/interrupt")
async def interrupt() -> dict[str, Any]:
    active = [
        job
        for job in fleet.store.list(limit=500)
        if job["status"] in ACTIVE_STATUSES
    ]
    if len(active) != 1:
        raise HTTPException(
            status_code=409,
            detail="Use /api/jobs/{prompt_id}/cancel when zero or multiple jobs are active",
        )
    return await _cancel_job(active[0]["prompt_id"], {})


@app.get("/api/jobs")
async def list_jobs(limit: int = 100) -> dict[str, Any]:
    return {"jobs": fleet.store.list(limit=max(1, min(limit, 500)))}


@app.get("/api/jobs/{prompt_id}")
async def get_job(prompt_id: str) -> dict[str, Any]:
    try:
        return await fleet.refresh_job(fleet.store.get(prompt_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown prompt id") from error


@app.post("/api/jobs/{prompt_id}/cancel")
async def cancel_job(
    prompt_id: str,
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    return await _cancel_job(prompt_id, body or {})


async def _cancel_job(prompt_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        job = fleet.store.get(prompt_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown prompt id") from error
    if job["status"] in TERMINAL_STATUSES:
        return job
    if job["status"] in {"queued", "reserved"} and not job["upstream_prompt_id"]:
        updated = fleet.store.update(prompt_id, status="cancelled")
        fleet.cleanup_job_inputs(updated)
        return updated
    expected_version = body.get("expected_version")
    if expected_version is not None:
        if not isinstance(expected_version, int):
            raise HTTPException(status_code=400, detail="expected_version must be an integer")
        if expected_version != job["version"]:
            raise HTTPException(status_code=409, detail="stale H3 job version")
    lane = fleet.lanes_by_id[job["lane_id"]]
    queue_response = await fleet.client.get(f"{lane.url}/queue", timeout=30)
    if not queue_response.is_success:
        raise HTTPException(
            status_code=queue_response.status_code,
            detail=queue_response.text,
        )
    presence = queue_contains(queue_response.json(), job["upstream_prompt_id"])
    if presence == "pending":
        response = await fleet.client.post(
            f"{lane.url}/queue",
            json={"delete": [job["upstream_prompt_id"]]},
            timeout=30,
        )
    elif presence == "running":
        response = await fleet.client.post(
            f"{lane.url}/interrupt",
            json={},
            timeout=30,
        )
    else:
        refreshed = await fleet.refresh_job(job)
        if refreshed["status"] in TERMINAL_STATUSES:
            return refreshed
        raise HTTPException(
            status_code=409,
            detail="H3 cancellation outcome is unknown because the task is not in its lane queue",
        )
    if not response.is_success:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    deadline = time.monotonic() + 15
    while True:
        queue_response = await fleet.client.get(f"{lane.url}/queue", timeout=30)
        if (
            queue_response.is_success
            and queue_contains(queue_response.json(), job["upstream_prompt_id"]) is None
        ):
            updated = fleet.store.update(prompt_id, status="cancelled")
            fleet.cleanup_job_inputs(updated)
            return updated
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=409,
                detail="H3 cancellation was not confirmed by the lane queue",
            )
        await asyncio.sleep(0.25)


@app.post("/api/inputs/{filename}")
async def upload_input(
    filename: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", filename):
        raise HTTPException(status_code=400, detail="invalid input filename")
    overwrite = request.query_params.get("overwrite", "true")
    content = await file.read()
    results = await replicate_input(
        filename,
        content,
        file.content_type or "application/octet-stream",
        overwrite=overwrite,
    )
    return {"ok": True, "filename": filename, "lanes": results}
