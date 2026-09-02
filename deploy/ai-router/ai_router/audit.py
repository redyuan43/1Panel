from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": time.time(),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)

    def recent(
        self,
        limit: int = 100,
        *,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not self.path.exists():
            return []
        with self._lock:
            try:
                with self.path.open("rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    start = max(0, size - max_bytes)
                    handle.seek(start)
                    if start:
                        handle.readline()
                    lines = handle.readlines()
            except OSError:
                return []

        result: list[dict[str, Any]] = []
        for raw_line in reversed(lines):
            try:
                value = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                result.append(value)
            if len(result) >= limit:
                break
        return result
