"""Encrypted compaction candidates and resumable, fenced model-call checkpoints.

This store does not send requests or modify conversation state. A dispatch intent
without a saved result is an unknown outcome, never permission to replay a call.
"""
from __future__ import annotations
from .compaction_limits import job_limits, parse_limits

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet

from .compaction import extract_messages, replace_messages


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class CompactionJobConflict(ValueError):
    pass


class CompactionJobs:
    def __init__(self, path, key, *, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        self.cipher = Fernet(key.encode())
        self.key = hmac.new(base64.urlsafe_b64decode(key), b"router-compaction-jobs-v1", hashlib.sha256).digest()
        if read_only:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS compaction_jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, dedupe TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, worker TEXT, lease_until REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL, ciphertext BLOB NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS compaction_jobs_owner ON compaction_jobs(owner,state)")
        os.chmod(self.path, 0o600)

    def _owner(self, owner):
        if not owner:
            raise ValueError("compaction owner is required")
        return hmac.new(self.key, owner.encode(), hashlib.sha256).hexdigest()

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise CompactionJobConflict("job store is read-only")
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    def _decode(self, row):
        value = json.loads(self.cipher.decrypt(row["ciphertext"]))
        if (value["id"] != row["id"] or self._owner(value["owner"]) != row["owner"]
                or value["state"] != row["state"]):
            raise CompactionJobConflict("job provenance mismatch")
        return value

    def _save(self, db, value, *, state, worker=None, lease_until=0):
        now = time.time()
        self._account_time(value, now)
        value["state"] = state
        value["accounted_at"] = now
        db.execute("""UPDATE compaction_jobs SET state=?,worker=?,lease_until=?,updated_at=?,ciphertext=?
            WHERE id=?""", (state, worker, lease_until, time.time(),
            self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()), value["id"]))

    @staticmethod
    def _account_time(value, now):
        if value["state"] == "running":
            value["elapsed_seconds"] = value.get("elapsed_seconds", 0) + max(0, now - value.get("accounted_at", now))
        value["accounted_at"] = now

    def create(self, owner, branch, body, api_kind, parameters):
        if not branch or api_kind not in {"chat", "responses"}:
            raise ValueError("branch and supported protocol are required")
        parameters = {**parameters, "limits": parse_limits(parameters.get("limits", {}))}
        owner_hash = self._owner(owner)
        dedupe = hmac.new(self.key, json.dumps([owner_hash, branch, digest(body), api_kind,
                            parameters], sort_keys=True).encode(), hashlib.sha256).hexdigest()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM compaction_jobs WHERE dedupe=?", (dedupe,)).fetchone()
            if row:
                return self._decode(row)
            value = dict(id=uuid4().hex, owner=owner, branch=branch, body=body,
                api_kind=api_kind, parameters=parameters, state="queued", steps={},
                calls=0, input_tokens=0, output_tokens=0, elapsed_seconds=0, created_at=time.time())
            db.execute("INSERT INTO compaction_jobs VALUES(?,?,?,'queued',NULL,0,?,?)",
                (value["id"], owner_hash, dedupe, time.time(), self.cipher.encrypt(json.dumps(value).encode())))
            return value

    def read(self, owner, job_id):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM compaction_jobs WHERE owner=? AND id=?",
                             (self._owner(owner), job_id)).fetchone()
            return self._decode(row) if row else None

    @staticmethod
    def public(value):
        result = {key: value.get(key) for key in ("id", "branch", "state", "calls", "input_tokens",
            "output_tokens", "elapsed_seconds", "created_at", "error", "cancel_requested")}
        result["limits"] = job_limits(value)
        result["unresolved_operation_ids"] = [step["operation_id"] for step in value["steps"].values()
                                               if step["state"] == "dispatched"]
        return result

    def list_jobs(self, owner, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid job page limit")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM compaction_jobs WHERE owner=? ORDER BY updated_at DESC LIMIT ?",
                              (self._owner(owner), limit)).fetchall()
            return [self.public(self._decode(row)) for row in rows]

    def ready(self, owner, branch, *, ancestors=()):
        if len(ancestors) > 32:
            raise ValueError("too many candidate ancestors")
        branches = {branch, *ancestors}
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM compaction_jobs WHERE owner=? AND state='ready' ORDER BY updated_at DESC LIMIT 50",
                              (self._owner(owner),)).fetchall()
            return [value for row in rows if (value := self._decode(row))["branch"] in branches]

    def claim(self, worker, *, ttl=60):
        if not worker or not 1 <= ttl <= 600:
            raise ValueError("worker and bounded lease are required")
        with self._transaction() as db:
            now = time.time()
            for row in db.execute("SELECT * FROM compaction_jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall():
                value = self._decode(row)
                self._account_time(value, min(now, row["lease_until"]))
                value["accounted_at"] = now
                unknown = any(step["state"] == "dispatched" for step in value["steps"].values())
                if unknown:
                    value["error"] = "upstream_outcome_unknown"
                self._save(db, value, state="needs_context" if unknown else "queued")
            if db.execute("SELECT 1 FROM compaction_jobs WHERE state IN ('running','needs_context') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM compaction_jobs WHERE state='queued' ORDER BY updated_at,id LIMIT 1").fetchone()
            if not row:
                return None
            value = self._decode(row)
            if value.get("elapsed_seconds", 0) >= job_limits(value)["max_seconds"]:
                value["error"] = "compaction_time_budget_exhausted"
                self._save(db, value, state="failed")
                return None
            self._save(db, value, state="running", worker=worker, lease_until=now + ttl)
            return value

    def _leased(self, db, job_id, worker):
        row = db.execute("SELECT * FROM compaction_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["state"] != "running" or row["worker"] != worker or row["lease_until"] <= time.time():
            raise CompactionJobConflict("compaction lease expired or task is no longer running")
        value = self._decode(row)
        self._account_time(value, time.time())
        return row, value

    def heartbeat(self, job_id, worker, *, ttl=60):
        if not 1 <= ttl <= 600:
            raise ValueError("invalid lease duration")
        with self._transaction() as db:
            _, value = self._leased(db, job_id, worker)
            self._save(db, value, state="running", worker=worker, lease_until=time.time() + ttl)

    def dispatch(self, job_id, worker, step_key, input_tokens, output_reserve):
        if not step_key or type(input_tokens) is not int or type(output_reserve) is not int or min(input_tokens, output_reserve) < 0:
            raise ValueError("invalid compaction call budget")
        with self._transaction() as db:
            row, value = self._leased(db, job_id, worker)
            existing = value["steps"].get(step_key)
            if existing:
                if existing["state"] == "completed":
                    return existing["result"]
                if not (existing["state"] == "failed" and existing.get("retryable")
                        and existing.get("attempt", 1) < 2):
                    raise CompactionJobConflict("upstream result unknown or retry exhausted; do not redispatch")
            limits = job_limits(value)
            if (value.get("elapsed_seconds", 0) >= limits["max_seconds"]
                    or value["calls"] >= limits["max_calls"]
                    or value["input_tokens"] + input_tokens > limits["max_input_tokens"]
                    or value["output_tokens"] + output_reserve > limits["max_output_tokens"]):
                raise CompactionJobConflict("compaction workload budget exhausted")
            if any(step["state"] == "dispatched" for step in value["steps"].values()):
                raise CompactionJobConflict("another compaction call is unresolved")
            value["calls"] += 1
            value["input_tokens"] += input_tokens
            prior = list(existing.get("previous_attempts", [])) if existing else []
            if existing:
                prior.append({"operation_id": existing["operation_id"], "status_code": existing.get("status_code")})
            value["steps"][step_key] = dict(state="dispatched", operation_id=uuid4().hex,
                input_tokens=input_tokens, output_reserve=output_reserve,
                attempt=(existing.get("attempt", 1) + 1) if existing else 1,
                previous_attempts=prior)
            self._save(db, value, state="running", worker=worker, lease_until=row["lease_until"])
            return None

    def cached_step(self, job_id, worker, step_key):
        with self._transaction() as db:
            _, value = self._leased(db, job_id, worker)
            step = value["steps"].get(step_key)
            if not step:
                return None
            if step["state"] == "failed" and step.get("retryable") and step.get("attempt", 1) < 2:
                return None
            if step["state"] != "completed":
                raise CompactionJobConflict("upstream result unknown; do not redispatch")
            return step["result"]

    def not_sent_step(self, job_id, worker, step_key):
        """Resolve only a locally rejected operation, never a transport failure."""
        with self._transaction() as db:
            row, value = self._leased(db, job_id, worker)
            step = value["steps"].get(step_key)
            if not step or step["state"] != "dispatched" or "input_tokens" not in step:
                raise CompactionJobConflict("step cannot be proven unsent")
            step.update(state="failed", status_code=0, reason_code="settings_changed_before_send",
                        retryable=False)
            value["calls"] -= 1
            value["input_tokens"] -= step["input_tokens"]
            value["error"] = "settings_changed_before_send"
            self._save(db, value, state="running", worker=worker, lease_until=row["lease_until"])

    def failed_step(self, job_id, worker, step_key, status_code, retryable, reason_code="invalid_response"):
        # Persist only fixed diagnostic categories, never upstream text or history.
        if reason_code not in {"invalid_response", "http_error", "invalid_envelope", "incomplete_response",
                               "invalid_content", "invalid_json", "non_object", "invalid_fields", "empty_handoff"}:
            reason_code = "invalid_response"
        with self._transaction() as db:
            row, value = self._leased(db, job_id, worker)
            step = value["steps"].get(step_key)
            if not step or step["state"] != "dispatched":
                raise CompactionJobConflict("step is not awaiting a response")
            step.update(state="failed", status_code=int(status_code), reason_code=reason_code,
                        retryable=bool(retryable and status_code in {429, 503}))
            value["error"] = f"summary_http_{status_code}_{reason_code}"
            value.setdefault("first_error", {"status_code": status_code, "reason_code": reason_code,
                                             "operation_id": step["operation_id"]})
            value["output_tokens"] += step["output_reserve"]
            self._save(db, value, state="running", worker=worker, lease_until=row["lease_until"])

    def complete_step(self, job_id, worker, step_key, result, output_tokens):
        with self._transaction() as db:
            row, value = self._leased(db, job_id, worker)
            step = value["steps"].get(step_key)
            if not step or step["state"] != "dispatched":
                raise CompactionJobConflict("step was not dispatched")
            if type(output_tokens) is not int or not 0 <= output_tokens <= step["output_reserve"]:
                raise CompactionJobConflict("invalid summary output accounting")
            step.update(state="completed", result=result)
            value["output_tokens"] += output_tokens
            self._save(db, value, state="running", worker=worker, lease_until=row["lease_until"])

    def operation(self, job_id, worker, step_key):
        with self._transaction() as db:
            _, value = self._leased(db, job_id, worker)
            step = value["steps"].get(step_key)
            if not step or step["state"] != "dispatched":
                raise CompactionJobConflict("operation is no longer dispatchable")
            return step["operation_id"]

    def candidate(self, job_id, worker, messages, *, summary_indices=()):
        with self._transaction() as db:
            _, value = self._leased(db, job_id, worker)
            if value.get("elapsed_seconds", 0) >= job_limits(value)["max_seconds"]:
                raise CompactionJobConflict("compaction time budget exhausted")
            if any(step["state"] != "completed" for step in value["steps"].values()):
                raise CompactionJobConflict("unresolved calls cannot publish a candidate")
            if not isinstance(messages, list) or not messages:
                raise ValueError("validated candidate messages are required")
            value["candidate"] = messages
            value["summary_indices"] = list(summary_indices)
            value.pop("error", None)
            self._save(db, value, state="ready")

    def cancel(self, owner, job_id):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM compaction_jobs WHERE owner=? AND id=?", (self._owner(owner), job_id)).fetchone()
            if not row:
                return False
            value = self._decode(row)
            unresolved = any(step["state"] == "dispatched" for step in value["steps"].values())
            value["cancel_requested"] = True
            if unresolved:
                value["error"] = "cancelled_with_upstream_outcome_unknown"
            self._save(db, value, state="needs_context" if unresolved else "cancelled")
            return True

    def fail(self, job_id, worker, reason):
        with self._transaction() as db:
            _, value = self._leased(db, job_id, worker)
            unresolved = any(step["state"] == "dispatched" for step in value["steps"].values())
            value["error"] = "upstream_outcome_unknown" if unresolved else value.get("error", reason)
            self._save(db, value, state="needs_context" if unresolved else "failed")

    def abandon_verified_operation(self, owner, job_id, operation_id, evidence_reference):
        """Operator-attested terminal outcome; never retries or fabricates output."""
        if not isinstance(evidence_reference, str) or not 8 <= len(evidence_reference.strip()) <= 512:
            raise ValueError("a bounded terminal-outcome evidence reference is required")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM compaction_jobs WHERE id=? AND owner=?",
                             (job_id, self._owner(owner))).fetchone()
            if not row:
                raise CompactionJobConflict("job not found for account")
            value = self._decode(row)
            if value["state"] != "needs_context":
                raise CompactionJobConflict("job is not awaiting outcome reconciliation")
            step = next((item for item in value["steps"].values()
                         if item["operation_id"] == operation_id and item["state"] == "dispatched"), None)
            if step is None:
                raise CompactionJobConflict("unresolved operation does not match")
            step.update(state="abandoned", reconciliation="operator_attested_terminal",
                        evidence_reference=evidence_reference.strip(), reconciled_at=time.time())
            value["cancel_requested"] = True
            unresolved = any(item["state"] == "dispatched" for item in value["steps"].values())
            value["error"] = "result_discarded_after_operator_reconciliation"
            self._save(db, value, state="needs_context" if unresolved else "cancelled")
            return self.public(value)

    def apply_candidate(self, owner, job_id, branch, body, api_kind):
        value = self.read(owner, job_id)
        if not value or value["state"] != "ready" or value["branch"] != branch or value["api_kind"] != api_kind:
            return None
        before = extract_messages(value["body"], api_kind)
        incoming = extract_messages(body, api_kind)
        if not before or len(incoming) < len(before) or digest(incoming[:len(before)]) != digest(before):
            return None
        # The live request owns application. Preserve newly appended messages and
        # current tools/settings; no background write to the conversation store.
        return replace_messages(body, api_kind, [*value["candidate"], *incoming[len(before):]])
