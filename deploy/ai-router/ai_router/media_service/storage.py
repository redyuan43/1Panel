from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .contracts import MediaError, fingerprint, validate_settings


class MediaStore:
    """Only the media daemon owns these files; API processes use its HTTP API."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "media.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    idem TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL,
                    value TEXT NOT NULL, UNIQUE(owner, kind, idem));
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    job_id TEXT NOT NULL, idem TEXT NOT NULL, digest TEXT NOT NULL,
                    value TEXT NOT NULL, PRIMARY KEY(job_id, idem));
                CREATE TABLE IF NOT EXISTS paid (
                    job_id TEXT PRIMARY KEY, day TEXT NOT NULL, released INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    created REAL NOT NULL, value TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return validate_settings(json.loads(row[0]) if row else {})

    def configure(self, value: dict) -> dict:
        value = validate_settings(value)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(value),))
        return value

    def create(self, owner: str, kind: str, body: dict, idem: str, request_id: str, limit: int) -> tuple[dict, bool]:
        digest = fingerprint(body)
        with self.connect() as db:
            row = db.execute("SELECT digest,value FROM jobs WHERE owner=? AND kind=? AND idem=?", (owner, kind, idem)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise MediaError("idempotency_conflict", "Idempotency key was used with different parameters.", 409)
                existing = json.loads(row["value"])
                if existing.get("deleted"):
                    raise MediaError("media_deleted", "The original task was deleted; it will not be regenerated.", 410)
                return existing, False
            pending = db.execute(
                "SELECT count(*) FROM jobs WHERE json_extract(value,'$.status')='queued' AND kind=?", (kind,),
            ).fetchone()[0]
            if pending >= limit:
                raise MediaError("media_queue_full", "Media queue is full.", 429)
            job = {
                "id": ("img_" if kind == "image" else "vid_") + uuid4().hex,
                "owner": owner, "kind": kind, "model": body["model"], "request": body,
                "request_id": request_id, "status": "queued", "created_at": time.time(),
                "updated_at": time.time(), "stages": [], "deleted": False, "provider_state": {},
            }
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                       (job["id"], owner, kind, idem, digest, job["created_at"], json.dumps(job)))
            self._event(db, job["id"], {"type": "accepted", "request_id": request_id})
        return job, True

    def get(self, job_id: str, owner: str | None = None, *, deleted: bool = False) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = json.loads(row[0]) if row else None
        if not job or (owner is not None and job["owner"] != owner) or (job.get("deleted") and not deleted):
            raise MediaError("media_not_found", "Media task was not found.", 404)
        return job

    def update(self, job_id: str, **changes) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise MediaError("media_not_found", "Media task was not found.", 404)
            job = {**json.loads(row[0]), **changes, "updated_at": time.time()}
            db.execute("UPDATE jobs SET value=? WHERE id=?", (json.dumps(job), job_id))
            self._event(db, job_id, {"type": "updated", "fields": list(changes), "status": job["status"]})
        return job

    def list(self, owner: str | None = None, kind: str | None = None, *, after: str | None = None, limit: int = 50) -> dict:
        clauses, args = ["json_extract(value,'$.deleted')=0"], []
        if owner is not None:
            clauses.append("owner=?")
            args.append(owner)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if after:
            anchor = self.get(after, owner)
            clauses.append("(created < ? OR (created=? AND id<?))")
            args.extend([anchor["created_at"], anchor["created_at"], after])
        limit = max(1, min(limit, 100))
        with self.connect() as db:
            rows = db.execute("SELECT value FROM jobs WHERE " + " AND ".join(clauses)
                              + " ORDER BY created DESC,id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        jobs = [json.loads(row[0]) for row in rows[:limit]]
        return {"data": jobs, "next_cursor": jobs[-1]["id"] if len(rows) > limit else None}

    def active(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT value FROM jobs WHERE json_extract(value,'$.status') NOT IN ('completed','failed','cancelled')"
                " AND json_extract(value,'$.deleted')=0 ORDER BY created",
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def artifact(self, output_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT value FROM artifacts WHERE id=?", (output_id,)).fetchone()
        if not row:
            raise MediaError("artifact_not_found", "Output was not found.", 404)
        return json.loads(row[0])

    def save_artifact(self, value: dict) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT value FROM artifacts WHERE id=?", (value["id"],)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing["sha256"] != value["sha256"] or existing["job_id"] != value["job_id"]:
                    raise MediaError("artifact_version_conflict", "Output version changed unexpectedly.", 409)
                return existing
            db.execute("INSERT INTO artifacts VALUES (?,?,?)", (value["id"], value["job_id"], json.dumps(value)))
        return value

    def outputs(self, job_id: str) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT value FROM artifacts WHERE job_id=?", (job_id,))]

    def operation(self, job_id: str, idem: str, body: dict, request_id: str | None = None) -> tuple[dict, bool]:
        digest = fingerprint(body)
        with self.connect() as db:
            row = db.execute("SELECT digest,value FROM operations WHERE job_id=? AND idem=?", (job_id, idem)).fetchone()
            if row:
                if row["digest"] != digest:
                    raise MediaError("idempotency_conflict", "Operation key has different parameters.", 409)
                return json.loads(row["value"]), False
            value = {"id": "op_" + uuid4().hex, "status": "pending", "request_id": request_id, **body}
            db.execute("INSERT INTO operations VALUES (?,?,?,?)", (job_id, idem, digest, json.dumps(value)))
            self._event(db, job_id, {"type": "operation", **value})
        return value, True

    def finish_operation(self, job_id: str, idem: str, value: dict):
        with self.connect() as db:
            db.execute("UPDATE operations SET value=? WHERE job_id=? AND idem=?", (json.dumps(value), job_id, idem))

    def reserve_paid(self, job_id: str, limit: int):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.connect() as db:
            if db.execute("SELECT 1 FROM paid WHERE job_id=?", (job_id,)).fetchone():
                return
            count = db.execute("SELECT count(*) FROM paid WHERE day=? AND released=0", (day,)).fetchone()[0]
            if count >= limit:
                raise MediaError("paid_media_limit", "Daily paid image allowance is exhausted.", 429)
            db.execute("INSERT INTO paid(job_id,day) VALUES (?,?)", (job_id, day))

    def release_paid(self, job_id: str):
        with self.connect() as db:
            db.execute("UPDATE paid SET released=1 WHERE job_id=?", (job_id,))

    @staticmethod
    def _event(db, job_id: str, value: dict):
        db.execute("INSERT INTO events(job_id,created,value) VALUES (?,?,?)", (job_id, time.time(), json.dumps(value)))
