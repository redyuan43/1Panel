from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable


class ProjectStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    data_json TEXT NOT NULL
                )
                """
            )

    def save(self, project: dict) -> dict:
        now = time.time()
        project["updated_at"] = now
        payload = json.dumps(project, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (id, created_at, updated_at, data_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    data_json = excluded.data_json
                """,
                (
                    project["id"],
                    float(project["created_at"]),
                    now,
                    payload,
                ),
            )
        return project

    def get(self, project_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM projects
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, project_id: str, mutator: Callable[[dict], None]) -> dict:
        with self._lock:
            project = self.get(project_id)
            if project is None:
                raise KeyError(project_id)
            mutator(project)
            return self.save(project)

    def delete(self, project_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM projects WHERE id = ?",
                (project_id,),
            )
        return cursor.rowcount > 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection


class BatchStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_schedules (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_run_at REAL,
                    status TEXT NOT NULL,
                    data_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_batch_schedules_due
                ON batch_schedules (status, next_run_at)
                """
            )

    def save(self, schedule: dict) -> dict:
        now = time.time()
        schedule["updated_at"] = now
        payload = json.dumps(schedule, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO batch_schedules (
                    id, created_at, updated_at, next_run_at, status, data_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    next_run_at = excluded.next_run_at,
                    status = excluded.status,
                    data_json = excluded.data_json
                """,
                (
                    schedule["id"],
                    float(schedule["created_at"]),
                    now,
                    schedule.get("next_run_at"),
                    schedule["status"],
                    payload,
                ),
            )
        return schedule

    def get(self, schedule_id: str) -> dict | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM batch_schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM batch_schedules
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def due(self, now: float) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM batch_schedules
                WHERE status = 'scheduled'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                ORDER BY next_run_at ASC, created_at ASC
                """,
                (now,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, schedule_id: str, mutator: Callable[[dict], None]) -> dict:
        with self._lock:
            schedule = self.get(schedule_id)
            if schedule is None:
                raise KeyError(schedule_id)
            mutator(schedule)
            return self.save(schedule)

    def delete(self, schedule_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM batch_schedules WHERE id = ?",
                (schedule_id,),
            )
        return cursor.rowcount > 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
