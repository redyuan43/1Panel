from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import Settings, deep_merge, load_yaml, validate_settings
from .route_diagnosis import preview_policy_impact


EDITABLE_POLICY_SECTIONS = frozenset(
    {
        "affinity",
        "cloud",
        "compaction",
        "evaluator",
        "failover",
        "health",
        "identity",
        "lmcache",
        "queue",
        "routing",
        "vision",
    }
)


class PolicyConflictError(ValueError):
    pass


class PolicyConfigManager:
    def __init__(self, database_path: str | Path, settings: Settings) -> None:
        self.database_path = Path(database_path)
        self.settings = settings
        self.database_path.parent.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )
        self._initialize()
        self._bootstrap()

    async def snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._snapshot)

    async def patch_draft(
        self,
        changes: dict[str, Any],
        *,
        expected_revision: int | None,
        expected_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._patch_draft,
            changes,
            expected_revision,
            expected_fingerprint,
            source,
        )

    async def validate_draft(
        self,
        traces: list[dict[str, Any]],
        *,
        expected_revision: int | None,
        expected_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._validate_draft,
            traces,
            expected_revision,
            expected_fingerprint,
            source,
        )

    async def activate(
        self,
        *,
        expected_revision: int | None,
        expected_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        draft = await asyncio.to_thread(
            self._activation_candidate,
            expected_revision,
            expected_fingerprint,
        )
        self.settings.write_runtime(draft["settings"])
        return await asyncio.to_thread(
            self._finish_activation,
            draft["revision"],
            source,
        )

    async def rollback(
        self,
        revision: int,
        *,
        expected_active_revision: int | None,
        expected_active_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._rollback,
            revision,
            expected_active_revision,
            expected_active_fingerprint,
            source,
        )

    async def record_external_activation(
        self,
        settings: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._record_external_activation,
            settings,
            source,
        )

    def _initialize(self) -> None:
        with self._connect(initialize=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS policy_revisions (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    base_revision INTEGER,
                    settings_fingerprint TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    validation_json TEXT,
                    activated_at REAL
                );
                CREATE INDEX IF NOT EXISTS policy_revisions_status
                    ON policy_revisions(status, revision DESC);
                """
            )
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    def _bootstrap(self) -> None:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM policy_revisions LIMIT 1"
            ).fetchone():
                return
            now = time.time()
            settings = self._editable_runtime()
            connection.execute(
                """
                INSERT INTO policy_revisions (
                    status, created_at, updated_at, source,
                    settings_fingerprint, settings_json, activated_at
                ) VALUES ('active', ?, ?, 'bootstrap', ?, ?, ?)
                """,
                (
                    now,
                    now,
                    _fingerprint(settings),
                    _encode(settings),
                    now,
                ),
            )

    def _snapshot(self) -> dict[str, Any]:
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT * FROM policy_revisions
                WHERE status='active'
                ORDER BY revision DESC LIMIT 1
                """
            ).fetchone()
            draft = connection.execute(
                """
                SELECT * FROM policy_revisions
                WHERE status IN ('draft', 'validated')
                ORDER BY revision DESC LIMIT 1
                """
            ).fetchone()
            history = connection.execute(
                """
                SELECT * FROM policy_revisions
                WHERE status='superseded'
                ORDER BY revision DESC LIMIT 50
                """
            ).fetchall()
        return {
            "active": self._record(active),
            "draft": self._record(draft),
            "revisions": [self._record(item) for item in history],
        }

    def _patch_draft(
        self,
        changes: dict[str, Any],
        expected_revision: int | None,
        expected_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        now = time.time()
        with self._connect() as connection:
            active = self._active_row(connection)
            draft = self._draft_row(connection)
            base = (
                json.loads(draft["settings_json"])
                if draft
                else json.loads(active["settings_json"])
            )
            self._check_version(
                draft or active,
                expected_revision,
                expected_fingerprint,
            )
            settings = deep_merge(base, changes)
            merged = deep_merge(
                load_yaml(self.settings.defaults_path),
                settings,
            )
            validate_settings(merged)
            fingerprint = _fingerprint(settings)
            if draft:
                connection.execute(
                    """
                    UPDATE policy_revisions
                    SET status='draft', updated_at=?, source=?,
                        settings_fingerprint=?, settings_json=?,
                        validation_json=NULL
                    WHERE revision=?
                    """,
                    (
                        now,
                        source,
                        fingerprint,
                        _encode(settings),
                        int(draft["revision"]),
                    ),
                )
                revision = int(draft["revision"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO policy_revisions (
                        status, created_at, updated_at, source,
                        base_revision, settings_fingerprint, settings_json
                    ) VALUES ('draft', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        source,
                        int(active["revision"]),
                        fingerprint,
                        _encode(settings),
                    ),
                )
                revision = int(cursor.lastrowid)
            self._prune(connection)
            row = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (revision,),
            ).fetchone()
        return self._record(row)

    def _validate_draft(
        self,
        traces: list[dict[str, Any]],
        expected_revision: int | None,
        expected_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            draft = self._draft_row(connection)
            if draft is None:
                raise ValueError("policy draft does not exist")
            self._check_version(
                draft,
                expected_revision,
                expected_fingerprint,
            )
            settings = json.loads(draft["settings_json"])
            merged = deep_merge(
                load_yaml(self.settings.defaults_path),
                settings,
            )
            validate_settings(merged)
            report = preview_policy_impact(traces[:500], merged)
            validation = {
                "status": "passed",
                "validated_at": time.time(),
                "source": source,
                "impact": report,
            }
            connection.execute(
                """
                UPDATE policy_revisions
                SET status='validated', updated_at=?, source=?,
                    validation_json=?
                WHERE revision=?
                """,
                (
                    validation["validated_at"],
                    source,
                    _encode(validation),
                    int(draft["revision"]),
                ),
            )
            row = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (int(draft["revision"]),),
            ).fetchone()
        return self._record(row)

    def _activation_candidate(
        self,
        expected_revision: int | None,
        expected_fingerprint: str | None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            draft = self._draft_row(connection)
            if draft is None:
                raise ValueError("validated policy draft does not exist")
            self._check_version(
                draft,
                expected_revision,
                expected_fingerprint,
            )
            if draft["status"] != "validated":
                raise ValueError(
                    "policy draft must be validated before activation"
                )
        return {
            "revision": int(draft["revision"]),
            "settings": json.loads(draft["settings_json"]),
        }

    def _finish_activation(
        self,
        revision: int,
        source: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE policy_revisions
                SET status='superseded', updated_at=?
                WHERE status='active'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE policy_revisions
                SET status='active', updated_at=?, activated_at=?, source=?
                WHERE revision=?
                """,
                (now, now, source, revision),
            )
            row = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (revision,),
            ).fetchone()
            self._prune(connection)
        return self._record(row)

    def _rollback(
        self,
        revision: int,
        expected_active_revision: int | None,
        expected_active_fingerprint: str | None,
        source: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            active = self._active_row(connection)
            self._check_version(
                active,
                expected_active_revision,
                expected_active_fingerprint,
            )
            target = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (revision,),
            ).fetchone()
            if target is None:
                raise ValueError("policy revision was not found")
            connection.execute(
                """
                UPDATE policy_revisions
                SET status='abandoned', updated_at=?
                WHERE status IN ('draft', 'validated')
                """,
                (time.time(),),
            )
            now = time.time()
            cursor = connection.execute(
                """
                INSERT INTO policy_revisions (
                    status, created_at, updated_at, source,
                    base_revision, settings_fingerprint, settings_json
                ) VALUES ('draft', ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    source,
                    int(active["revision"]),
                    target["settings_fingerprint"],
                    target["settings_json"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (int(cursor.lastrowid),),
            ).fetchone()
            self._prune(connection)
        return self._record(row)

    def _record_external_activation(
        self,
        settings: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        value = self._editable_value(settings)
        fingerprint = _fingerprint(value)
        now = time.time()
        with self._connect() as connection:
            active = self._active_row(connection)
            if active["settings_fingerprint"] == fingerprint:
                return self._record(active)
            connection.execute(
                """
                UPDATE policy_revisions
                SET status=CASE
                    WHEN status='active' THEN 'superseded'
                    ELSE 'abandoned'
                END,
                updated_at=?
                WHERE status IN ('active', 'draft', 'validated')
                """,
                (now,),
            )
            cursor = connection.execute(
                """
                INSERT INTO policy_revisions (
                    status, created_at, updated_at, source,
                    base_revision, settings_fingerprint,
                    settings_json, activated_at
                ) VALUES ('active', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    source,
                    int(active["revision"]),
                    fingerprint,
                    _encode(value),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM policy_revisions WHERE revision=?",
                (int(cursor.lastrowid),),
            ).fetchone()
            self._prune(connection)
        return self._record(row)

    def _editable_runtime(self) -> dict[str, Any]:
        return self._editable_value(self.settings.value)

    @staticmethod
    def _editable_value(
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: copy.deepcopy(item)
            for key, item in settings.items()
            if (
                key in EDITABLE_POLICY_SECTIONS
                and isinstance(item, dict)
            )
        }

    @staticmethod
    def _check_version(
        row: sqlite3.Row,
        expected_revision: int | None,
        expected_fingerprint: str | None,
    ) -> None:
        if expected_revision is None or expected_fingerprint is None:
            raise PolicyConflictError(
                "policy revision and fingerprint are required"
            )
        if int(row["revision"]) != expected_revision:
            raise PolicyConflictError("policy revision changed")
        if row["settings_fingerprint"] != expected_fingerprint:
            raise PolicyConflictError("policy fingerprint changed")

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "revision": int(row["revision"]),
            "status": row["status"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "source": row["source"],
            "base_revision": row["base_revision"],
            "settings_fingerprint": row["settings_fingerprint"],
            "settings": json.loads(row["settings_json"]),
            "validation": (
                json.loads(row["validation_json"])
                if row["validation_json"]
                else None
            ),
            "activated_at": row["activated_at"],
        }

    @staticmethod
    def _active_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM policy_revisions
            WHERE status='active'
            ORDER BY revision DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("active policy revision is missing")
        return row

    @staticmethod
    def _draft_row(
        connection: sqlite3.Connection,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM policy_revisions
            WHERE status IN ('draft', 'validated')
            ORDER BY revision DESC LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _prune(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM policy_revisions
            WHERE revision IN (
                SELECT revision FROM policy_revisions
                WHERE status IN ('superseded', 'abandoned')
                ORDER BY revision DESC
                LIMIT -1 OFFSET 50
            )
            """
        )

    def _connect(self, *, initialize: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if initialize:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection


def default_policy_database_path(settings: Settings) -> str:
    return os.environ.get(
        "AI_ROUTER_POLICY_DB_PATH",
        str(
            settings.runtime_path.parent
            / "policy"
            / "policy.sqlite3"
        ),
    )


def _encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()[:16]
