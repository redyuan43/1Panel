from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .errors import TrainingArchiveUnavailableError


SCHEMA_VERSION = 1
TERMINAL_STATES = {"completed", "failed", "interrupted"}


class TrainingArchive:
    def __init__(self, database_path: str, key_path: str) -> None:
        self.database_path = Path(database_path)
        self.key_path = Path(key_path)
        try:
            key = self.key_path.read_bytes().strip()
            self._cipher = Fernet(key)
            raw_key = base64.urlsafe_b64decode(key)
        except Exception as exc:
            raise TrainingArchiveUnavailableError(
                "training archive key is missing or invalid"
            ) from exc
        self._index_key = hmac.new(
            raw_key,
            b"1panel-ai-router-training-index-v1",
            hashlib.sha256,
        ).digest()
        try:
            self.database_path.parent.mkdir(
                mode=0o700,
                parents=True,
                exist_ok=True,
            )
            os.chmod(self.database_path.parent, 0o700)
            self._initialize()
        except Exception as exc:
            raise TrainingArchiveUnavailableError() from exc

    async def begin(
        self,
        *,
        request_id: str,
        conversation_id: str | None,
        conversation_mode: str,
        client_id: str,
        key_id: str,
        protocol: str,
        received_body: dict[str, Any],
        instance_id: str,
        boot_id: str,
    ) -> str | None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "request",
            "state": "received",
            "trainable": False,
            "received_at": time.time(),
            "request": {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "conversation_mode": conversation_mode,
                "client_id": client_id,
                "key_id": key_id,
                "protocol": protocol,
                "instance_id": instance_id,
                "boot_id": boot_id,
                "received_body": received_body,
            },
            "routing_attempts": [],
        }
        token = self._digest(f"request:{request_id}")
        try:
            inserted = await asyncio.to_thread(
                self._insert,
                token,
                self._digest_optional(conversation_id),
                protocol,
                payload,
            )
        except Exception as exc:
            raise TrainingArchiveUnavailableError() from exc
        return token if inserted else None

    async def mark_routed(
        self,
        token: str | None,
        *,
        effective_body: dict[str, Any],
        routed_body: dict[str, Any],
        route: dict[str, Any],
    ) -> None:
        if not token:
            return

        def update(payload: dict[str, Any]) -> None:
            payload["state"] = "routed"
            payload["request"]["effective_body"] = effective_body
            payload["routing_attempts"].append(
                {
                    **route,
                    "routed_at": time.time(),
                    "routed_body": routed_body,
                }
            )

        await self._merge(
            token,
            update,
            selected_model=str(route.get("selected_model", "")),
            endpoint_id=str(route.get("endpoint_id", "")),
        )

    async def record_pipeline(self, token, pipeline):
        if token:
            await self._merge(token, lambda payload: payload.update(pipeline=pipeline))

    async def set_effective_context(
        self,
        token: str | None,
        *,
        effective_body: dict[str, Any],
    ) -> None:
        if not token:
            return

        def update(payload: dict[str, Any]) -> None:
            payload["state"] = "prepared"
            payload["request"]["effective_body"] = effective_body

        await self._merge(token, update)

    async def complete(
        self,
        token: str | None,
        *,
        status_code: int,
        response_payload: bytes | None = None,
        assistant_items: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        if not token:
            return

        def update(payload: dict[str, Any]) -> None:
            payload["state"] = "completed"
            payload["trainable"] = 200 <= status_code < 300
            payload["completed_at"] = time.time()
            payload["response"] = {
                "status_code": status_code,
                "body": _response_value(response_payload),
                "assistant_items": assistant_items,
                "usage": usage,
                "complete": True,
            }

        await self._merge(
            token,
            update,
            status_code=status_code,
            trainable=200 <= status_code < 300,
        )

    async def fail(
        self,
        token: str | None,
        *,
        status_code: int,
        error: dict[str, Any],
        response_payload: bytes | None = None,
        interrupted: bool = False,
    ) -> None:
        if not token:
            return

        def update(payload: dict[str, Any]) -> None:
            payload["state"] = "interrupted" if interrupted else "failed"
            payload["trainable"] = False
            payload["completed_at"] = time.time()
            payload["response"] = {
                "status_code": status_code,
                "body": _response_value(response_payload),
                "error": error,
                "complete": False,
            }

        await self._merge(
            token,
            update,
            status_code=status_code,
            trainable=False,
        )

    async def status(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._status)
        except Exception as exc:
            raise TrainingArchiveUnavailableError() from exc

    async def needs_legacy_backfill(self) -> bool:
        try:
            return await asyncio.to_thread(self._needs_legacy_backfill)
        except Exception as exc:
            raise TrainingArchiveUnavailableError() from exc

    async def export_jsonl(
        self,
        output_path: str,
        *,
        trainable_only: bool = False,
    ) -> int:
        try:
            return await asyncio.to_thread(
                self._export_jsonl,
                Path(output_path),
                trainable_only,
            )
        except Exception as exc:
            raise TrainingArchiveUnavailableError(
                "training archive export failed"
            ) from exc

    async def backfill_conversation_snapshots(
        self,
        states: list[dict[str, Any]],
        state_key: str,
    ) -> int:
        try:
            return await asyncio.to_thread(
                self._backfill_conversation_snapshots,
                states,
                state_key,
            )
        except Exception as exc:
            raise TrainingArchiveUnavailableError(
                "training archive backfill failed"
            ) from exc

    async def _merge(
        self,
        token: str,
        update: Any,
        *,
        selected_model: str | None = None,
        endpoint_id: str | None = None,
        status_code: int | None = None,
        trainable: bool | None = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._merge_sync,
                token,
                update,
                selected_model,
                endpoint_id,
                status_code,
                trainable,
            )
        except TrainingArchiveUnavailableError:
            raise
        except Exception as exc:
            raise TrainingArchiveUnavailableError() from exc

    def _initialize(self) -> None:
        with closing(self._connect(initialize=True)) as connection:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS training_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_hash TEXT NOT NULL UNIQUE,
                        conversation_hash TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        protocol TEXT NOT NULL,
                        status_code INTEGER,
                        trainable INTEGER NOT NULL DEFAULT 0,
                        selected_model TEXT,
                        endpoint_id TEXT,
                        payload_ciphertext BLOB NOT NULL,
                        payload_bytes INTEGER NOT NULL,
                        encrypted_bytes INTEGER NOT NULL,
                        schema_version INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS training_records_created_at
                        ON training_records(created_at);
                    CREATE INDEX IF NOT EXISTS training_records_conversation
                        ON training_records(conversation_hash, created_at);
                    CREATE INDEX IF NOT EXISTS training_records_trainable
                        ON training_records(trainable, created_at);
                    CREATE TABLE IF NOT EXISTS training_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO training_metadata(key, value)
                    VALUES('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(SCHEMA_VERSION),),
                )
        os.chmod(self.database_path, 0o600)

    def _insert(
        self,
        request_hash: str,
        conversation_hash: str | None,
        protocol: str,
        payload: dict[str, Any],
    ) -> bool:
        ciphertext, payload_bytes = self._encrypt(payload)
        now = time.time()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO training_records(
                        request_hash,
                        conversation_hash,
                        created_at,
                        updated_at,
                        protocol,
                        trainable,
                        payload_ciphertext,
                        payload_bytes,
                        encrypted_bytes,
                        schema_version
                    ) VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        request_hash,
                        conversation_hash,
                        now,
                        now,
                        protocol,
                        ciphertext,
                        payload_bytes,
                        len(ciphertext),
                        SCHEMA_VERSION,
                    ),
                )
                return cursor.rowcount == 1

    def _merge_sync(
        self,
        token: str,
        update: Any,
        selected_model: str | None,
        endpoint_id: str | None,
        status_code: int | None,
        trainable: bool | None,
    ) -> None:
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT payload_ciphertext
                    FROM training_records
                    WHERE request_hash=?
                    """,
                    (token,),
                ).fetchone()
                if row is None:
                    raise TrainingArchiveUnavailableError(
                        "training archive request record is missing"
                    )
                payload = self._decrypt(bytes(row["payload_ciphertext"]))
                if str(payload.get("state", "")) in TERMINAL_STATES:
                    return
                update(payload)
                ciphertext, payload_bytes = self._encrypt(payload)
                connection.execute(
                    """
                    UPDATE training_records
                    SET updated_at=?,
                        status_code=COALESCE(?, status_code),
                        trainable=COALESCE(?, trainable),
                        selected_model=COALESCE(?, selected_model),
                        endpoint_id=COALESCE(?, endpoint_id),
                        payload_ciphertext=?,
                        payload_bytes=?,
                        encrypted_bytes=?
                    WHERE request_hash=?
                    """,
                    (
                        time.time(),
                        status_code,
                        int(trainable) if trainable is not None else None,
                        selected_model or None,
                        endpoint_id or None,
                        ciphertext,
                        payload_bytes,
                        len(ciphertext),
                        token,
                    ),
                )

    def _status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS records,
                    SUM(CASE WHEN trainable=1 THEN 1 ELSE 0 END) AS trainable,
                    SUM(CASE WHEN status_code IS NULL THEN 1 ELSE 0 END) AS incomplete,
                    SUM(payload_bytes) AS payload_bytes,
                    SUM(encrypted_bytes) AS encrypted_bytes
                FROM training_records
                """
            ).fetchone()
            backfill = connection.execute(
                """
                SELECT value FROM training_metadata
                WHERE key='legacy_backfill_completed_at'
                """
            ).fetchone()
        return {
            "enabled": True,
            "database_path": str(self.database_path),
            "records": int(row["records"] or 0),
            "trainable_records": int(row["trainable"] or 0),
            "incomplete_records": int(row["incomplete"] or 0),
            "payload_bytes": int(row["payload_bytes"] or 0),
            "encrypted_bytes": int(row["encrypted_bytes"] or 0),
            "database_bytes": (
                self.database_path.stat().st_size
                if self.database_path.exists()
                else 0
            ),
            "legacy_backfill_completed_at": (
                float(backfill["value"]) if backfill else None
            ),
        }

    def _needs_legacy_backfill(self) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM training_metadata
                WHERE key='legacy_backfill_completed_at'
                """
            ).fetchone()
        return row is None

    def _export_jsonl(
        self,
        output_path: Path,
        trainable_only: bool,
    ) -> int:
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output_path.parent, 0o700)
        temporary = output_path.with_suffix(output_path.suffix + ".new")
        query = """
            SELECT id, created_at, updated_at, protocol, status_code,
                   trainable, selected_model, endpoint_id, payload_ciphertext
            FROM training_records
        """
        if trainable_only:
            query += " WHERE trainable=1"
        query += " ORDER BY id"
        count = 0
        with closing(self._connect()) as connection, temporary.open(
            "w",
            encoding="utf-8",
        ) as handle:
            for row in connection.execute(query):
                value = {
                    "archive": {
                        "id": int(row["id"]),
                        "created_at": float(row["created_at"]),
                        "updated_at": float(row["updated_at"]),
                        "protocol": row["protocol"],
                        "status_code": row["status_code"],
                        "trainable": bool(row["trainable"]),
                        "selected_model": row["selected_model"],
                        "endpoint_id": row["endpoint_id"],
                    },
                    "payload": self._decrypt(
                        bytes(row["payload_ciphertext"])
                    ),
                }
                handle.write(
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                handle.write("\n")
                count += 1
        os.chmod(temporary, 0o600)
        os.replace(temporary, output_path)
        return count

    def _backfill_conversation_snapshots(
        self,
        states: list[dict[str, Any]],
        state_key: str,
    ) -> int:
        state_cipher = Fernet(state_key.encode("ascii"))
        prepared: list[tuple[Any, ...]] = []
        for state in states:
            capsule = state.get("encrypted_capsule")
            conversation_id = str(state.get("conversation_id", ""))
            if not capsule or not conversation_id:
                continue
            try:
                messages = json.loads(
                    state_cipher.decrypt(
                        str(capsule).encode("ascii")
                    )
                )
            except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
                continue
            synthetic_request_id = (
                f"backfill:{conversation_id}:"
                f"{state.get('boundary_hash', '')}"
            )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "conversation_snapshot",
                "source": "redis_active_conversation_backfill",
                "state": "completed",
                "trainable": True,
                "historical_completeness": "active_snapshot_only",
                "received_at": float(state.get("last_seen", time.time())),
                "completed_at": float(state.get("last_seen", time.time())),
                "request": {
                    "request_id": synthetic_request_id,
                    "conversation_id": conversation_id,
                    "protocol": "snapshot",
                    "messages": messages,
                },
                "routing": {
                    "public_model": state.get("public_model"),
                    "endpoint_id": state.get("endpoint_id"),
                    "deployment_id": state.get("deployment_id"),
                    "task": state.get("task"),
                    "migration_count": state.get("migration_count"),
                },
            }
            ciphertext, payload_bytes = self._encrypt(payload)
            timestamp = float(state.get("last_seen", time.time()))
            prepared.append(
                (
                    self._digest(f"request:{synthetic_request_id}"),
                    self._digest(f"conversation:{conversation_id}"),
                    timestamp,
                    timestamp,
                    "snapshot",
                    200,
                    1,
                    state.get("public_model"),
                    state.get("endpoint_id"),
                    ciphertext,
                    payload_bytes,
                    len(ciphertext),
                    SCHEMA_VERSION,
                )
            )
        inserted = 0
        with closing(self._connect()) as connection:
            with connection:
                already_done = connection.execute(
                    """
                    SELECT 1 FROM training_metadata
                    WHERE key='legacy_backfill_completed_at'
                    """
                ).fetchone()
                if already_done:
                    return 0
                for values in prepared:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO training_records(
                            request_hash,
                            conversation_hash,
                            created_at,
                            updated_at,
                            protocol,
                            status_code,
                            trainable,
                            selected_model,
                            endpoint_id,
                            payload_ciphertext,
                            payload_bytes,
                            encrypted_bytes,
                            schema_version
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    inserted += int(cursor.rowcount == 1)
                connection.execute(
                    """
                    INSERT INTO training_metadata(key, value)
                    VALUES('legacy_backfill_completed_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(time.time()),),
                )
        return inserted

    def _connect(self, *, initialize: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if initialize:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _encrypt(self, payload: dict[str, Any]) -> tuple[bytes, int]:
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        compressed = zlib.compress(plaintext, level=9)
        return self._cipher.encrypt(compressed), len(plaintext)

    def _decrypt(self, ciphertext: bytes) -> dict[str, Any]:
        compressed = self._cipher.decrypt(ciphertext)
        value = json.loads(zlib.decompress(compressed))
        if not isinstance(value, dict):
            raise TrainingArchiveUnavailableError(
                "training archive payload is invalid"
            )
        return value

    def _digest(self, value: str) -> str:
        return hmac.new(
            self._index_key,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _digest_optional(self, value: str | None) -> str | None:
        return self._digest(f"conversation:{value}") if value else None


def _response_value(payload: bytes | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "value": base64.b64encode(payload).decode("ascii"),
        }
    try:
        return {"encoding": "json", "value": json.loads(text)}
    except json.JSONDecodeError:
        return {"encoding": "utf-8", "value": text}
