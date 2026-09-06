from __future__ import annotations

import copy
import hashlib
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RouterError


MATCH_MODE = "latest_user_contains"
MAX_RETIRED_PHRASES = 64


@dataclass(frozen=True)
class PromptDirective:
    id: str
    generation: int
    endpoint_id: str | None = None
    reset: bool = False


@dataclass(frozen=True)
class PromptDirectiveResult:
    body: dict[str, Any]
    directive: PromptDirective | None


def normalize_phrase(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def validate_prompt_directives(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("routing.prompt_directives must be an object")
    if str(value.get("match", MATCH_MODE)) != MATCH_MODE:
        raise ValueError(
            "routing.prompt_directives.match must be latest_user_contains"
        )
    if str(value.get("persistence", "conversation")) != "conversation":
        raise ValueError(
            "routing.prompt_directives.persistence must be conversation"
        )
    if str(value.get("fallback", "error")) != "error":
        raise ValueError(
            "routing.prompt_directives.fallback must be error"
        )
    if int(value.get("revision", 0)) < 1:
        raise ValueError(
            "routing.prompt_directives.revision must be positive"
        )
    routes = value.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError(
            "routing.prompt_directives.routes must be a non-empty object"
        )
    normalized: list[str] = []
    for directive_id, route in routes.items():
        if not str(directive_id).strip() or not isinstance(route, dict):
            raise ValueError(
                "routing.prompt_directives route entries must be objects"
            )
        phrase = normalize_phrase(route.get("phrase"))
        endpoint_id = str(route.get("endpoint_id", "")).strip()
        if not phrase or not endpoint_id:
            raise ValueError(
                "routing.prompt_directives routes require phrase and endpoint_id"
            )
        if len(str(route.get("phrase", ""))) > 120:
            raise ValueError(
                "routing.prompt_directives phrases must not exceed 120 characters"
            )
        if int(route.get("generation", 0)) < 1:
            raise ValueError(
                "routing.prompt_directives generations must be positive"
            )
        normalized.append(phrase)
    reset = value.get("reset")
    if not isinstance(reset, dict) or not normalize_phrase(reset.get("phrase")):
        raise ValueError(
            "routing.prompt_directives.reset requires a phrase"
        )
    if int(reset.get("generation", 0)) < 1:
        raise ValueError(
            "routing.prompt_directives reset generation must be positive"
        )
    normalized.append(normalize_phrase(reset.get("phrase")))
    if len(normalized) != len(set(normalized)):
        raise ValueError(
            "routing.prompt_directives active phrases must be unique"
        )
    retired = value.get("retired_phrases", [])
    if (
        not isinstance(retired, list)
        or len(retired) > MAX_RETIRED_PHRASES
        or any(not normalize_phrase(item) for item in retired)
    ):
        raise ValueError(
            "routing.prompt_directives.retired_phrases must be a bounded string list"
        )


def prepare_prompt_directive_update(
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    validate_prompt_directives(current)
    if not isinstance(proposed, dict):
        raise RouterError(
            "routing.prompt_directives must be an object",
            status_code=400,
            code="invalid_route_directive",
        )
    raw_revision = proposed.get("revision", 0)
    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
        raise RouterError(
            "routing.prompt_directives.revision must be an integer",
            status_code=400,
            code="invalid_route_directive",
        )
    expected_revision = raw_revision
    current_revision = int(current.get("revision", 0))
    if expected_revision != current_revision:
        raise RouterError(
            "prompt directive settings changed; reload before saving",
            status_code=409,
            code="prompt_directive_revision_conflict",
            details={
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            },
        )

    result = copy.deepcopy(current)
    changes: list[dict[str, str]] = []
    global_fields = ("enabled", "match", "persistence", "fallback")
    global_changed = False
    for field in global_fields:
        incoming = proposed.get(field, current.get(field))
        if incoming != current.get(field):
            result[field] = copy.deepcopy(incoming)
            global_changed = True

    proposed_routes = proposed.get("routes")
    if not isinstance(proposed_routes, dict):
        proposed_routes = {}
    retired = [
        str(item)
        for item in current.get("retired_phrases", [])
        if normalize_phrase(item)
    ]
    for directive_id, current_route in current["routes"].items():
        incoming = proposed_routes.get(directive_id, current_route)
        if not isinstance(incoming, dict):
            incoming = current_route
        updated = copy.deepcopy(current_route)
        phrase = str(incoming.get("phrase", current_route["phrase"])).strip()
        endpoint_id = str(
            incoming.get("endpoint_id", current_route["endpoint_id"])
        ).strip()
        changed_fields = []
        if phrase != str(current_route.get("phrase", "")):
            changed_fields.append("phrase")
            retired.append(str(current_route.get("phrase", "")))
        if endpoint_id != str(current_route.get("endpoint_id", "")):
            changed_fields.append("endpoint_id")
        updated["phrase"] = phrase
        updated["endpoint_id"] = endpoint_id
        if global_changed or changed_fields:
            updated["generation"] = int(
                current_route.get("generation", 1)
            ) + 1
            changes.append(
                {
                    "directive_id": str(directive_id),
                    "fields": ",".join(changed_fields or ["global"]),
                }
            )
        result["routes"][directive_id] = updated

    proposed_reset = proposed.get("reset")
    if not isinstance(proposed_reset, dict):
        proposed_reset = current["reset"]
    reset_phrase = str(
        proposed_reset.get("phrase", current["reset"]["phrase"])
    ).strip()
    reset_changed = reset_phrase != str(current["reset"].get("phrase", ""))
    if reset_changed:
        retired.append(str(current["reset"].get("phrase", "")))
    result["reset"]["phrase"] = reset_phrase
    if global_changed or reset_changed:
        result["reset"]["generation"] = int(
            current["reset"].get("generation", 1)
        ) + 1
        changes.append(
            {
                "directive_id": "reset",
                "fields": "phrase" if reset_changed else "global",
            }
        )

    compact_retired: list[str] = []
    seen: set[str] = set()
    active = {
        normalize_phrase(item.get("phrase"))
        for item in result["routes"].values()
    }
    active.add(normalize_phrase(result["reset"].get("phrase")))
    for phrase in reversed(retired):
        normalized = normalize_phrase(phrase)
        if not normalized or normalized in active or normalized in seen:
            continue
        seen.add(normalized)
        compact_retired.append(str(phrase).strip())
        if len(compact_retired) >= MAX_RETIRED_PHRASES:
            break
    result["retired_phrases"] = list(reversed(compact_retired))
    if changes or global_changed:
        result["revision"] = current_revision + 1
    validate_prompt_directives(result)
    return result, changes


def sanitize_prompt_directives(
    body: dict[str, Any],
    api_kind: str,
    settings: dict[str, Any],
) -> PromptDirectiveResult:
    validate_prompt_directives(settings)
    result = copy.deepcopy(body)
    configured: dict[str, tuple[str, PromptDirective]] = {}
    for directive_id, route in settings["routes"].items():
        configured[normalize_phrase(route["phrase"])] = (
            str(route["phrase"]),
            PromptDirective(
                id=str(directive_id),
                generation=int(route["generation"]),
                endpoint_id=str(route["endpoint_id"]),
            ),
        )
    reset = settings["reset"]
    configured[normalize_phrase(reset["phrase"])] = (
        str(reset["phrase"]),
        PromptDirective(
            id="reset",
            generation=int(reset["generation"]),
            reset=True,
        ),
    )
    active = (
        configured
        if bool(settings.get("enabled", False))
        else {}
    )
    cleanup = {
        normalize_phrase(item)
        for item in settings.get("retired_phrases", [])
        if normalize_phrase(item)
    }
    cleanup.update(configured)

    messages, assign = _message_view(result, api_kind)
    latest_user = max(
        (
            index
            for index, message in enumerate(messages)
            if _is_user_message(message)
        ),
        default=-1,
    )
    matches: dict[str, PromptDirective] = {}
    for index, message in enumerate(messages):
        activate = index == latest_user
        cleaned, found = _clean_message(
            message,
            cleanup,
            active if activate else {},
        )
        messages[index] = cleaned
        for normalized, directive in found.items():
            matches[normalized] = directive
    assign(messages)

    distinct = {
        (item.id, item.generation, item.endpoint_id, item.reset)
        for item in matches.values()
    }
    if len(distinct) > 1:
        raise RouterError(
            "the latest user message contains multiple route directives",
            status_code=400,
            code="invalid_route_directive",
        )
    directive = next(iter(matches.values()), None)
    if directive is not None and latest_user >= 0:
        if not _has_meaningful_content(messages[latest_user]):
            raise RouterError(
                "put the route directive on its own line and include a task",
                status_code=400,
                code="invalid_route_directive",
            )
    return PromptDirectiveResult(result, directive)


def resolve_conversation_directive(
    activated: PromptDirective | None,
    conversation: Any,
    settings: dict[str, Any],
) -> tuple[PromptDirective | None, bool]:
    if activated is not None:
        return (None, True) if activated.reset else (activated, False)
    if conversation is None or not getattr(conversation, "directive_id", None):
        return None, False
    route = settings.get("routes", {}).get(conversation.directive_id)
    if (
        not bool(settings.get("enabled", False))
        or not isinstance(route, dict)
        or int(route.get("generation", 0))
        != int(getattr(conversation, "directive_generation", 0))
        or str(route.get("endpoint_id", ""))
        != str(getattr(conversation, "directive_endpoint_id", ""))
    ):
        return None, True
    return (
        PromptDirective(
            id=str(conversation.directive_id),
            generation=int(route["generation"]),
            endpoint_id=str(route["endpoint_id"]),
        ),
        False,
    )


class PromptDirectiveStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()
        os.chmod(self.path, 0o600)

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN used_at IS NULL AND retired_at IS NULL
                           THEN 1 ELSE 0 END) AS available,
                  SUM(CASE WHEN used_at IS NOT NULL THEN 1 ELSE 0 END) AS used,
                  SUM(CASE WHEN retired_at IS NOT NULL THEN 1 ELSE 0 END) AS retired
                FROM phrase_pool
                """
            ).fetchone()
        return {
            "total": int(row[0] or 0),
            "available": int(row[1] or 0),
            "used": int(row[2] or 0),
            "retired": int(row[3] or 0),
        }

    def suggest(
        self,
        directive_ids: list[str],
        *,
        excluded: set[str] | None = None,
    ) -> dict[str, str]:
        ids = [str(item) for item in directive_ids]
        if not ids or len(ids) != len(set(ids)) or len(ids) > 5:
            raise RouterError(
                "directive_ids must contain one to five unique IDs",
                status_code=400,
                code="invalid_route_directive",
            )
        blocked = {
            normalize_phrase(item)
            for item in (excluded or set())
            if normalize_phrase(item)
        }
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT phrase FROM phrase_pool
                WHERE used_at IS NULL AND retired_at IS NULL
                """
            ).fetchall()
            choices = [
                str(row[0])
                for row in rows
                if normalize_phrase(row[0]) not in blocked
            ]
            if len(choices) < len(ids):
                raise RouterError(
                    "the prompt directive phrase pool is exhausted",
                    status_code=503,
                    code="prompt_directive_pool_exhausted",
                )
            selected = secrets.SystemRandom().sample(choices, len(ids))
            connection.executemany(
                "UPDATE phrase_pool SET used_at = ? WHERE phrase = ?",
                [(now, phrase) for phrase in selected],
            )
        return dict(zip(ids, selected, strict=True))

    def sync_active(self, phrases: list[str]) -> None:
        now = time.time()
        values = [
            str(item).strip()
            for item in phrases
            if normalize_phrase(item)
        ]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT INTO phrase_pool(phrase, source, used_at)
                VALUES (?, 'active', ?)
                ON CONFLICT(phrase) DO UPDATE SET used_at = COALESCE(used_at, ?)
                """,
                [(phrase, now, now) for phrase in values],
            )

    def record_changes(
        self,
        revision: int,
        changes: list[dict[str, str]],
        *,
        previous: dict[str, Any],
        current: dict[str, Any],
        source: str,
    ) -> None:
        if not changes:
            return
        now = time.time()
        rows = []
        for change in changes:
            directive_id = change["directive_id"]
            old_phrase = _configured_phrase(previous, directive_id)
            new_phrase = _configured_phrase(current, directive_id)
            rows.append(
                (
                    int(revision),
                    directive_id,
                    _phrase_hash(old_phrase),
                    _phrase_hash(new_phrase),
                    now,
                    source,
                )
            )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for change in changes:
                old_phrase = _configured_phrase(
                    previous,
                    change["directive_id"],
                )
                new_phrase = _configured_phrase(
                    current,
                    change["directive_id"],
                )
                if (
                    normalize_phrase(old_phrase)
                    and normalize_phrase(old_phrase)
                    != normalize_phrase(new_phrase)
                ):
                    connection.execute(
                        """
                        INSERT INTO phrase_pool(
                          phrase, source, used_at, retired_at
                        ) VALUES (?, 'retired', ?, ?)
                        ON CONFLICT(phrase) DO UPDATE SET
                          used_at = COALESCE(used_at, ?),
                          retired_at = COALESCE(retired_at, ?)
                        """,
                        (old_phrase, now, now, now, now),
                    )
            connection.executemany(
                """
                INSERT INTO phrase_rotations(
                  revision, directive_id, previous_hash, current_hash,
                  changed_at, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS phrase_pool(
                  phrase TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  used_at REAL,
                  retired_at REAL
                );
                CREATE TABLE IF NOT EXISTS phrase_rotations(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  revision INTEGER NOT NULL,
                  directive_id TEXT NOT NULL,
                  previous_hash TEXT NOT NULL,
                  current_hash TEXT NOT NULL,
                  changed_at REAL NOT NULL,
                  source TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO phrase_pool(phrase, source)
                VALUES (?, 'generated-v1')
                """,
                ((_generated_phrase(index),) for index in range(4096)),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def configured_phrases(settings: dict[str, Any]) -> list[str]:
    values = [
        str(route.get("phrase", ""))
        for route in settings.get("routes", {}).values()
        if isinstance(route, dict)
    ]
    reset = settings.get("reset", {})
    if isinstance(reset, dict):
        values.append(str(reset.get("phrase", "")))
    return [item for item in values if normalize_phrase(item)]


def _message_view(
    body: dict[str, Any],
    api_kind: str,
) -> tuple[list[dict[str, Any]], Any]:
    if api_kind == "chat":
        values = body.get("messages")
        messages = (
            [copy.deepcopy(item) for item in values]
            if isinstance(values, list)
            else []
        )

        def assign(items: list[dict[str, Any]]) -> None:
            body["messages"] = items

        return messages, assign
    value = body.get("input")
    if isinstance(value, str):
        messages = [{"role": "user", "content": value}]

        def assign(items: list[dict[str, Any]]) -> None:
            body["input"] = items[0].get("content", "") if items else ""

        return messages, assign
    if isinstance(value, dict):
        values = [value]
    else:
        values = value
    messages = (
        [copy.deepcopy(item) for item in values]
        if isinstance(values, list)
        else []
    )

    def assign(items: list[dict[str, Any]]) -> None:
        body["input"] = items

    return messages, assign


def _is_user_message(message: Any) -> bool:
    return isinstance(message, dict) and str(
        message.get("role", "")
    ).lower() == "user"


def _clean_message(
    message: Any,
    cleanup: set[str],
    active: dict[str, tuple[str, PromptDirective]],
) -> tuple[dict[str, Any], dict[str, PromptDirective]]:
    if not isinstance(message, dict) or not _is_user_message(message):
        return copy.deepcopy(message), {}
    result = copy.deepcopy(message)
    content = result.get("content")
    found: dict[str, PromptDirective] = {}
    if isinstance(content, str):
        result["content"], found = _clean_text(content, cleanup, active)
        return result, found
    if not isinstance(content, list):
        return result, found
    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(copy.deepcopy(part))
            continue
        part_type = str(part.get("type", ""))
        text_key = next(
            (
                key
                for key in ("text", "input_text", "content")
                if isinstance(part.get(key), str)
            ),
            None,
        )
        if text_key is None or part_type in {
            "image_url",
            "input_image",
            "input_audio",
        }:
            parts.append(copy.deepcopy(part))
            continue
        cleaned, part_found = _clean_text(
            str(part[text_key]),
            cleanup,
            active,
        )
        found.update(part_found)
        if cleaned.strip():
            updated = copy.deepcopy(part)
            updated[text_key] = cleaned
            parts.append(updated)
    result["content"] = parts
    return result, found


def _clean_text(
    text: str,
    cleanup: set[str],
    active: dict[str, tuple[str, PromptDirective]],
) -> tuple[str, dict[str, PromptDirective]]:
    kept: list[str] = []
    found: dict[str, PromptDirective] = {}
    for line in text.splitlines():
        normalized_line = normalize_phrase(line)
        matched_cleanup = [
            phrase for phrase in cleanup if phrase in normalized_line
        ]
        if matched_cleanup:
            for phrase in matched_cleanup:
                if phrase in active:
                    found[phrase] = active[phrase][1]
            continue
        kept.append(line)
    return "\n".join(kept).strip(), found


def _has_meaningful_content(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, str) and part.strip():
            return True
        if not isinstance(part, dict):
            continue
        if any(
            part.get(key)
            for key in ("image_url", "input_image", "input_audio", "file_id")
        ):
            return True
        if any(
            str(part.get(key, "")).strip()
            for key in ("text", "input_text", "content")
        ):
            return True
    return False


def _configured_phrase(settings: dict[str, Any], directive_id: str) -> str:
    if directive_id == "reset":
        return str(settings.get("reset", {}).get("phrase", ""))
    return str(
        settings.get("routes", {})
        .get(directive_id, {})
        .get("phrase", "")
    )


def _phrase_hash(value: str) -> str:
    return hashlib.sha256(normalize_phrase(value).encode()).hexdigest()


def _generated_phrase(index: int) -> str:
    first = (
        "霁川",
        "星垣",
        "澄海",
        "云岫",
        "月汐",
        "青屿",
        "远岚",
        "静渊",
        "明涧",
        "雪庭",
        "苍原",
        "玄浦",
        "晴峤",
        "清晖",
        "夜航",
        "疏影",
    )
    second = (
        "玄镜",
        "银梭",
        "星槎",
        "青简",
        "玉尺",
        "云钥",
        "霜轮",
        "明烛",
        "风铎",
        "月琴",
        "天衡",
        "澄钟",
        "远帆",
        "静弦",
        "光栅",
        "墨印",
    )
    suffix = (
        "规程",
        "约定",
        "章法",
        "序列",
        "准则",
        "路径",
        "节律",
        "方案",
        "流程",
        "指引",
        "格式",
        "协议",
        "模式",
        "规则",
        "纲要",
        "方法",
    )
    a = index // 256
    b = (index // 16) % 16
    c = index % 16
    return f"按{first[a]}{second[b]}{suffix[c]}处理"
