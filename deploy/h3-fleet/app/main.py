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
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from .workflow_builder import SUPPORTED_MODES, build_workflow
from .admission import BUSY, CapacityPolicy, InstanceLock, SwapRecovery, resource_snapshot


ACTIVE_STATUSES = {"queued", *BUSY}
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
                ("demand_json", "TEXT"),
                ("admission_reason", "TEXT"),
                ("submission_started_at", "REAL"),
                ("failure_reason", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_execution_id
                ON jobs(execution_id) WHERE execution_id IS NOT NULL
                """
            )
            connection.execute("CREATE TABLE IF NOT EXISTS controls (name TEXT PRIMARY KEY, value TEXT NOT NULL)")

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
                WHERE lane_id = ? AND status IN ('reserved', 'reconciling', 'submitted', 'running', 'cancelling')
                ORDER BY created_at
                """,
                (lane_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','reserved','reconciling','submitted','running','cancelling') ORDER BY created_at, prompt_id"
            ).fetchall()]

    def reserve(self, prompt_id: str, lane_id: str, policy: CapacityPolicy,
                snapshot: dict[str, Any]) -> bool:
        """Compare and reserve globally in one SQLite write transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = dict(connection.execute("SELECT * FROM jobs WHERE prompt_id = ?", (prompt_id,)).fetchone())
            if job["status"] != "queued":
                return False
            demand = json.loads(job["demand_json"])
            lease = self.validation_lease()
            experiment = lease.get("experiment") if lease and (job.get("execution_id") or "").startswith(lease["owner"] + "_") else None
            active = [dict(row) for row in connection.execute(
                "SELECT * FROM jobs WHERE status IN ('reserved','reconciling','submitted','running','cancelling')"
            ).fetchall()]
            demands = []
            for item in active:
                # Old rows with no persisted shape keep an exclusive reservation.
                demands.append(json.loads(item["demand_json"]) if item.get("demand_json") else {
                    "class": "long", "profile": item["profile"],
                    "memory_budget_bytes": policy.data["long"]["memory_budget_gib"] * 1024**3,
                    "disk_budget_bytes": policy.data["long"]["disk_budget_gib"] * 1024**3,
                })
            reason = policy.blocked(demand, demands, snapshot, experiment)
            if lease and lease["expires_at"] <= time.time():
                reason = "validation_lease_expired"
            if lane_id not in policy.rule(demand, experiment)["lanes"]:
                reason = "lane_not_eligible"
            if (snapshot.get("swap_recovery", {}).get("ready")
                    and max(snapshot["swap_used_bytes"], snapshot["cgroup_swap_bytes"]) > policy.data["resources"]["max_swap_gib"] * 1024**3
                    and lane_id != "fast"):
                reason = "swap_recovery_fast_only"
            if any(item["lane_id"] == lane_id for item in active):
                reason = "lane_reserved"
            # FIFO prevents a stream of short requests starving an older long job.
            older = connection.execute(
                "SELECT 1 FROM jobs WHERE status = 'queued' AND (created_at < ? OR (created_at = ? AND prompt_id < ?)) LIMIT 1",
                (job["created_at"], job["created_at"], prompt_id),
            ).fetchone()
            if older:
                reason = "earlier_job_queued"
            if reason:
                connection.execute("UPDATE jobs SET admission_reason = ? WHERE prompt_id = ?", (reason, prompt_id))
                return False
            connection.execute(
                "UPDATE jobs SET lane_id = ?, status = 'reserved', admission_reason = NULL, updated_at = ?, version = version + 1 WHERE prompt_id = ? AND status = 'queued'",
                (lane_id, time.time(), prompt_id),
            )
        return True

    def validation_lease(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM controls WHERE name = 'validation_lease'").fetchone()
        lease = json.loads(row[0]) if row else None
        if lease and (lease["expires_at"] > time.time() or self.active()):
            return lease
        return None

    def set_validation_lease(self, owner: str, ttl: int, experiment: dict[str, Any] | None = None) -> dict[str, Any]:
        if experiment is not None:
            CapacityPolicy.validate_experiment(experiment)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self.validation_lease()
            if lease and lease["owner"] != owner:
                raise HTTPException(status_code=409, detail="another validation window is active")
            if lease and experiment is not None and experiment != lease.get("experiment"):
                raise HTTPException(status_code=409, detail="validation experiment is immutable within its window")
            if any(not (job.get("execution_id") or "").startswith(owner + "_") for job in self.active()):
                raise HTTPException(status_code=409, detail="production executions are active or queued")
            value = {"owner": owner, "expires_at": time.time() + ttl}
            if experiment or (lease and lease.get("experiment")):
                value["experiment"] = experiment or lease["experiment"]
            connection.execute("INSERT OR REPLACE INTO controls(name, value) VALUES ('validation_lease', ?)", (json.dumps(value),))
        return value

    def release_validation_lease(self, owner: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self.validation_lease()
            if lease and lease["owner"] != owner:
                raise HTTPException(status_code=409, detail="validation window has a different owner")
            if self.active():
                raise HTTPException(status_code=409, detail="validation executions must be reconciled before release")
            connection.execute("DELETE FROM controls WHERE name = 'validation_lease'")

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
        self.policy = CapacityPolicy()
        self.swap_recovery = SwapRecovery(self.policy.data["resources"].get("swap_idle_stable_seconds", 60))
        self.instance_lock = InstanceLock(self.store.path)
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

    async def capacity_snapshot(self, queues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            queues = await self.inspect_queues() if queues is None else queues
            idle = not any(item["queued_or_running"] for item in queues) and not any(
                item["status"] in BUSY for item in self.store.active()
            )
        except (httpx.HTTPError, ValueError, TypeError):
            idle = False
        return self.swap_recovery.observe(resource_snapshot(), idle=idle)

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
        if job["status"] in {"reserved", "reconciling"}:
            if (job["status"] == "reserved" and job.get("demand_json")
                    and not job.get("submission_started_at") and not job["upstream_prompt_id"]):
                # A pre-POST reservation is safe to recover after a process crash.
                if time.time() - float(job["updated_at"]) >= 60:
                    return self.store.update(job["prompt_id"], lane_id="", status="queued")
                return job
            return await self.reconcile_submission(job)
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
            # History can be pruned or temporarily unavailable. Absence is not
            # proof of failure and must not free the reservation for another job.
            return self.store.update(job["prompt_id"], status="reconciling",
                                     failure_reason=job.get("failure_reason") or "upstream_result_missing")
        except Exception:
            return job

    async def refresh_active(self) -> None:
        jobs = self.store.active()
        # Release completed reservations before evaluating queued work.
        for job in jobs:
            if job["status"] in BUSY:
                await self.refresh_job(job)
        for job in jobs:
            if job["status"] == "queued":
                await self.refresh_job(job)

    async def reconcile_submission(self, job: dict[str, Any]) -> dict[str, Any]:
        lane = self.lanes_by_id[job["lane_id"]]
        try:
            response = await self.client.get(f"{lane.url}/queue", timeout=30)
            response.raise_for_status()
            queue = response.json()
            candidates = [item for key in ("queue_running", "queue_pending") for item in queue.get(key, [])]
            for item in candidates:
                if len(item) > 3 and isinstance(item[3], dict):
                    if item[3].get("h3", {}).get("fleet_prompt_id") == job["prompt_id"]:
                        return self.store.update(job["prompt_id"], upstream_prompt_id=str(item[1]), status="running")
                if len(item) > 1 and job["upstream_prompt_id"] and str(item[1]) == job["upstream_prompt_id"]:
                    return self.store.update(job["prompt_id"], status="running")
            response = await self.client.get(f"{lane.url}/history", params={"max_items": 1000}, timeout=30)
            response.raise_for_status()
            for upstream_id, record in response.json().items():
                prompt = record.get("prompt", [])
                metadata = prompt[3] if len(prompt) > 3 and isinstance(prompt[3], dict) else {}
                if (str(upstream_id) == job["upstream_prompt_id"] or
                        metadata.get("h3", {}).get("fleet_prompt_id") == job["prompt_id"]):
                    recovered = self.store.update(job["prompt_id"], upstream_prompt_id=str(upstream_id), status="submitted")
                    return await self.refresh_job(recovered)
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return self.store.update(job["prompt_id"], status="reconciling")

    async def select_lane(self, profile: str, demand: dict[str, Any] | None = None,
                          experiment: dict[str, Any] | None = None) -> Lane | None:
        try:
            queues = await self.inspect_queues()
            if any(item["untracked_count"] for item in queues):
                return None
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        allowed = self.policy.rule(demand, experiment)["lanes"] if demand else [lane.id for lane in self.lanes]
        for lane in self.lanes:
            if lane.id not in allowed:
                continue
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

    async def inspect_queues(self) -> list[dict[str, Any]]:
        known = {item["upstream_prompt_id"] for item in [*self.store.active(), *self.store.list(limit=500)]}
        result = []
        for lane in self.lanes:
            if not lane.enabled:
                continue
            response = await self.client.get(f"{lane.url}/queue", timeout=10)
            response.raise_for_status()
            records = [item for key in ("queue_running", "queue_pending") for item in response.json().get(key, [])]
            result.append({"lane_id": lane.id, "queued_or_running": len(records),
                           "running_count": len(response.json().get("queue_running", [])),
                           "pending_count": len(response.json().get("queue_pending", [])),
                           "untracked_count": sum(len(item) > 1 and str(item[1]) not in known for item in records)})
        return result

    async def dispatch_queued(self, job: dict[str, Any]) -> dict[str, Any]:
        async with self.assignment_lock:
            job = self.store.get(job["prompt_id"])
            if job["status"] != "queued":
                return job
            demand = json.loads(job["demand_json"]) if job.get("demand_json") else self.policy.demand(
                json.loads(job.get("request_json") or "{}"), job["profile"]
            )
            if not job.get("demand_json"):
                job = self.store.update(job["prompt_id"], demand_json=json.dumps(demand))
            lease = self.store.validation_lease()
            experiment = lease.get("experiment") if lease and (job.get("execution_id") or "").startswith(lease["owner"] + "_") else None
            lane = await self.select_lane(job["profile"], demand, experiment)
            if lane is None:
                return self.store.update(job["prompt_id"], admission_reason="no_eligible_lane")
            try:
                payload = json.loads(job.get("request_json") or "")
            except (TypeError, ValueError):
                return self.store.update(job["prompt_id"], status="error")
            if not self.store.reserve(job["prompt_id"], lane.id, self.policy, await self.capacity_snapshot()):
                return self.store.get(job["prompt_id"])
            job = self.store.get(job["prompt_id"])
            return await self.submit_reserved(job, payload, lane)

    async def submit_reserved(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        lane: Lane,
    ) -> dict[str, Any]:
        payload = json.loads(json.dumps(payload))
        payload.setdefault("extra_data", {}).setdefault("h3", {})["fleet_prompt_id"] = job["prompt_id"]
        self.store.update(job["prompt_id"], submission_started_at=time.time())
        try:
            response = await self.client.post(f"{lane.url}/prompt", json=payload)
        except httpx.HTTPError:
            return self.store.update(job["prompt_id"], status="reconciling", failure_reason="submission_transport_unknown")
        if not response.is_success:
            if response.status_code >= 500:
                return self.store.update(job["prompt_id"], status="reconciling", failure_reason="submission_server_outcome_unknown")
            updated = self.store.update(job["prompt_id"], status="error")
            self.cleanup_job_inputs(updated)
            return updated
        try:
            upstream_prompt_id = str(response.json().get("prompt_id", "")).strip()
        except (ValueError, AttributeError):
            upstream_prompt_id = ""
        if not upstream_prompt_id:
            return self.store.update(job["prompt_id"], status="reconciling", failure_reason="submission_response_unknown")
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
    fleet.instance_lock.acquire()
    try:
        fleet.store.initialize()
        yield
    finally:
        await fleet.close()
        fleet.instance_lock.release()


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
        "reconciling": "submitted",
        "cancelling": "running",
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
        "admission_reason": job.get("admission_reason"),
        "reconciliation_required": job["status"] == "reconciling",
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
            profile: {"max_parallel": fleet.policy.data["short"][profile]["max_parallel"],
                      "steps": fleet.policy.data["short"][profile]["steps"],
                      "max_parallel_frame_count": 124, "long_max_parallel": 1}
            for profile in ("preview", "quality")
        },
        "capacity_policy": fleet.policy.data,
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


@app.get("/api/router/capacity")
async def router_capacity(request: Request) -> dict[str, Any]:
    router_protected(request)
    queues = await fleet.inspect_queues()
    return {"policy": fleet.policy.data, "resources": await fleet.capacity_snapshot(queues),
            "active": [{"execution_id": job.get("execution_id"), "status": job["status"], "lane_id": job["lane_id"]}
                       for job in fleet.store.active()],
            "queues": queues, "validation_lease": fleet.store.validation_lease()}


@app.post("/api/router/validation-lease")
async def validation_lease(request: Request) -> dict[str, Any]:
    router_protected(request)
    body = await request.json()
    owner, ttl = body.get("owner", ""), body.get("ttl_seconds", 120)
    if not re.fullmatch(r"h3val_[a-f0-9]{16}", str(owner)) or type(ttl) is not int or not 30 <= ttl <= 21600:
        raise HTTPException(status_code=400, detail="invalid validation owner or ttl")
    async with fleet.assignment_lock:
        if any(item["untracked_count"] for item in await fleet.inspect_queues()):
            raise HTTPException(status_code=409, detail="untracked upstream executions are active")
        try:
            return fleet.store.set_validation_lease(owner, ttl, body.get("experiment"))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/api/router/validation-lease")
async def release_validation_lease(request: Request) -> dict[str, bool]:
    router_protected(request)
    body = await request.json()
    async with fleet.assignment_lock:
        fleet.store.release_validation_lease(str(body.get("owner", "")))
    return {"released": True}


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
    async with fleet.assignment_lock:
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
            job = await _cancel_job_locked(job["prompt_id"], {})
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
                "active_job_count": len(fleet.store.active_for_lane(lane.id)),
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
        lease = fleet.store.validation_lease()
        if (execution_id or "").startswith("h3val_") and not lease:
            raise HTTPException(status_code=409, detail="validation operation requires an active owned lease")
        if lease and not (execution_id or "").startswith(lease["owner"] + "_"):
            raise HTTPException(status_code=503, detail="H3 fleet is in a controlled capacity-validation window")
        if lease and lease["expires_at"] <= time.time():
            raise HTTPException(status_code=503, detail="H3 validation window must be renewed before submitting more work")
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
        try:
            demand = fleet.policy.demand(payload, profile)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        experiment = lease.get("experiment") if lease else None
        if experiment and not fleet.policy.matches_experiment(demand, experiment):
            raise HTTPException(status_code=400, detail="execution differs from the fixed validation workload")
        lane = await fleet.select_lane(profile, demand, experiment)
        prompt_id = uuid.uuid4().hex
        job = fleet.store.create(
            prompt_id=prompt_id,
            upstream_prompt_id="",
            execution_id=execution_id,
            request_digest=request_digest,
            request_data=payload,
            lane_id="",
            stage=stage,
            profile=profile,
            status="queued",
            execution_data=payload.get("extra_data", {}).get("h3", {}).get("contract"),
        )
        job = fleet.store.update(prompt_id, demand_json=json.dumps(demand))
        if lane is None:
            job = fleet.store.update(prompt_id, admission_reason="no_eligible_lane")
        if lane and fleet.store.reserve(prompt_id, lane.id, fleet.policy, await fleet.capacity_snapshot()):
            job = fleet.store.get(prompt_id)
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
    request = fleet.client.build_request(
        "GET",
        f"{lane.url}/view",
        params={"filename": filename, "subfolder": subfolder, "type": type},
    )
    response = await fleet.client.send(request, stream=True)
    if not response.is_success:
        detail = (await response.aread()).decode(errors="replace")[:1000]
        await response.aclose()
        raise HTTPException(status_code=response.status_code, detail=detail)

    async def chunks():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()

    headers = {"Cache-Control": "private, no-store"}
    if response.headers.get("content-length"):
        headers["Content-Length"] = response.headers["content-length"]
    return StreamingResponse(
        chunks(),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
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
    async with fleet.assignment_lock:
        return await _cancel_job_locked(prompt_id, body)


async def _cancel_job_locked(prompt_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        job = fleet.store.get(prompt_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown prompt id") from error
    if job["status"] in TERMINAL_STATUSES:
        return job
    if job["status"] in {"reserved", "reconciling"} and job.get("submission_started_at"):
        job = await fleet.reconcile_submission(job)
        if not job["upstream_prompt_id"] or job["status"] == "reconciling":
            raise HTTPException(status_code=409, detail="H3 submission outcome must be reconciled before cancellation")
        if job["status"] in TERMINAL_STATUSES:
            return job
    if job["status"] in {"queued", "reserved"} and not job.get("submission_started_at"):
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
        running_ids = {str(item[1]) for item in queue_response.json().get("queue_running", []) if len(item) > 1}
        if running_ids != {job["upstream_prompt_id"]}:
            raise HTTPException(status_code=409, detail="H3 lane interrupt would affect another execution")
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
