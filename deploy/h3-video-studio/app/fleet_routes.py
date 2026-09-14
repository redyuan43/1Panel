"""Durable node ownership. Unknown submissions never release a reservation."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


ACTIVE = ("reserved", "submitting", "submitted")


class RouteStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS fleet_routes (
                execution_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, origin TEXT NOT NULL,
                state TEXT NOT NULL, capability TEXT NOT NULL, batch_id TEXT,
                prompt_id TEXT, created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS fleet_batches (
                batch_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, origin TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, execution_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM fleet_routes WHERE execution_id=?", (execution_id,)).fetchone()
            return dict(row) if row else None

    def active(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM fleet_routes WHERE state IN (?,?,?)", ACTIVE)]

    def batch(self, batch_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM fleet_batches WHERE batch_id=?", (batch_id,)).fetchone()
            return dict(row) if row else None

    def reserve(self, execution_id, candidates, capability, batch_id=None, *, legacy=False):
        """Candidates are ordered; recheck durable ownership inside the write lock."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM fleet_routes WHERE execution_id=?", (execution_id,)).fetchone()
            if existing:
                return dict(existing)
            held = [dict(r) for r in db.execute("SELECT * FROM fleet_routes WHERE state IN (?,?,?)", ACTIVE)]
            leases = {r["node_id"]: r["batch_id"] for r in db.execute("SELECT * FROM fleet_batches")}
            for node, snapshot in candidates:
                local = [r for r in held if r["node_id"] == node.id]
                if not legacy:
                    if leases.get(node.id) not in (None, batch_id):
                        continue
                    # Remote active rows already observed by Fleet are not counted twice.
                    unseen = sum(r["execution_id"] not in snapshot["execution_ids"] for r in local)
                    if len(local) >= node.max_parallel or unseen >= snapshot["slots"]:
                        continue
                    if max(len(local), snapshot["active"] + unseen) >= node.max_parallel:
                        continue
                    if local and not snapshot["active"]:
                        continue
                state = "submitted" if legacy else "reserved"
                db.execute("INSERT INTO fleet_routes VALUES (?,?,?,?,?,?,NULL,?)",
                           (execution_id, node.id, node.url, state, capability, batch_id, time.time()))
                return dict(db.execute("SELECT * FROM fleet_routes WHERE execution_id=?", (execution_id,)).fetchone())
        return None

    def update(self, execution_id, state, prompt_id=None):
        if state not in (*ACTIVE, "completed", "cancelled", "error"):
            raise ValueError("invalid route state")
        with self.connect() as db:
            db.execute("UPDATE fleet_routes SET state=?, prompt_id=COALESCE(?,prompt_id) WHERE execution_id=?",
                       (state, prompt_id, execution_id))

    def claim_submission(self, execution_id):
        with self.connect() as db:
            return db.execute("UPDATE fleet_routes SET state='submitting' WHERE execution_id=? AND state='reserved'",
                              (execution_id,)).rowcount == 1

    def begin_batch(self, batch_id, candidates, *, recovering=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM fleet_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if existing:
                return dict(existing)
            for node in candidates:
                if db.execute("SELECT 1 FROM fleet_batches WHERE node_id=?", (node.id,)).fetchone():
                    continue
                if db.execute("SELECT 1 FROM fleet_routes WHERE node_id=? AND state IN (?,?,?) AND (?=0 OR batch_id IS NULL OR batch_id!=?)",
                              (node.id, *ACTIVE, int(recovering), batch_id)).fetchone():
                    continue
                db.execute("INSERT INTO fleet_batches VALUES (?,?,?)", (batch_id, node.id, node.url))
                return dict(db.execute("SELECT * FROM fleet_batches WHERE batch_id=?", (batch_id,)).fetchone())
        return None

    def end_batch(self, batch_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM fleet_routes WHERE batch_id=? AND state IN (?,?,?)", (batch_id, *ACTIVE)).fetchone():
                raise RuntimeError("批次仍有运行中或结果未知的任务，保留节点租约。")
            db.execute("DELETE FROM fleet_batches WHERE batch_id=?", (batch_id,))
