"""Rebuildable, account-scoped history index; never stores plaintext search terms.

The archive remains authoritative. Callers must authorize account ownership and
source disclosure before indexing, and authorize the destination before search.
This module deliberately knows nothing about model calls or conversation state.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from cryptography.fernet import Fernet
from .phase_timing import phase


INDEX_VERSION = 1
MAX_QUERY_TERMS = 64
MAX_CANDIDATES = 64
SEARCH_INDEXES = (
    "CREATE INDEX IF NOT EXISTS memory_documents_corpus ON memory_documents(owner,length)",
    "CREATE INDEX IF NOT EXISTS memory_documents_search ON memory_documents(id,owner,cloud_allowed,conversation,created_at)",
)
STOP_WORDS = frozenset("the and for with from this that have what when where please about error history conversation remember 之前 以前 历史 记录 这个 那个 什么 如何 我们 可以 是否 错误 问题 帮我 一下".split())
LATIN_TERM = re.compile(r"[a-z0-9_][a-z0-9_./:\\-]{1,127}", re.I)
HAN_RUN = re.compile(r"[\u3400-\u9fff]+")


def search_terms(text: str) -> Counter[str]:
    """Identifiers plus CJK bi/trigrams, without a new tokenizer dependency."""
    text = unicodedata.normalize("NFKC", text).casefold()
    terms: list[str] = []
    for match in LATIN_TERM.finditer(text):
        value = match.group().strip("./:\\-")
        if value and value not in STOP_WORDS:
            terms.append(value)
            terms.extend(part for part in re.split(r"[./:\\_-]+", value)
                         if len(part) >= 3 and part != value and part not in STOP_WORDS)
    for match in HAN_RUN.finditer(text):
        value = match.group()
        for size in (2, 3):
            terms.extend(value[i:i + size] for i in range(len(value) - size + 1)
                         if value[i:i + size] not in STOP_WORDS)
    return Counter(terms)


def explicit_identifiers(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text[:8192]).casefold()
    values = (match.group().strip("./:\\-") for match in LATIN_TERM.finditer(normalized))
    return frozenset(value for value in values
                     if len(value) >= 5 and re.search(r"[./:_\\-]|\d", value))


@dataclass(frozen=True)
class MemorySource:
    client_id: str
    conversation_id: str
    request_id: str
    message_id: str
    role: str
    text: str
    created_at: float
    cloud_allowed: bool = False
    offset: int = 0
    cloud_unknown: bool = False


@dataclass(frozen=True)
class MemoryHit:
    source_id: str
    source: MemorySource
    score: float
    matched_terms: int


class MemoryIndex:
    def __init__(self, database_path: str | Path, key: str, *, read_only: bool = False):
        self.path = Path(database_path)
        self.read_only = read_only
        self.cipher = Fernet(key.encode("ascii"))
        self._key = hmac.new(base64.urlsafe_b64decode(key),
                             b"router-history-memory-index-v1", hashlib.sha256).digest()
        if read_only:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_documents (
                    owner TEXT NOT NULL,
                    id TEXT PRIMARY KEY,
                    conversation TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    cloud_allowed INTEGER NOT NULL,
                    length INTEGER NOT NULL,
                    ciphertext BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_documents_owner
                    ON memory_documents(owner, conversation, created_at);
                CREATE TABLE IF NOT EXISTS memory_terms (
                    owner TEXT NOT NULL,
                    term TEXT NOT NULL,
                    document TEXT NOT NULL REFERENCES memory_documents(id) ON DELETE CASCADE,
                    frequency INTEGER NOT NULL,
                    PRIMARY KEY(owner, term, document)
                );
                CREATE TABLE IF NOT EXISTS memory_exclusions (
                    owner TEXT NOT NULL, conversation TEXT NOT NULL,
                    PRIMARY KEY(owner, conversation)
                );
                CREATE TABLE IF NOT EXISTS memory_checkpoints (
                    owner TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_ingestion (
                    owner TEXT PRIMARY KEY,
                    backfill_cursor INTEGER NOT NULL,
                    boundary INTEGER NOT NULL,
                    live_cursor INTEGER NOT NULL,
                    observed_head INTEGER NOT NULL,
                    events_per_second REAL NOT NULL DEFAULT 0,
                    sample_at REAL NOT NULL DEFAULT 0,
                    sample_position INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS memory_ingestion_meta (
                    key TEXT PRIMARY KEY, value INTEGER NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    def _connect(self):
        db = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
              if self.read_only else sqlite3.connect(self.path, timeout=2))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _digest(self, purpose: str, *values: str) -> str:
        message = json.dumps([purpose, *values], ensure_ascii=False,
                             separators=(",", ":")).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _owner(self, client_id: str) -> str:
        if not client_id:
            raise ValueError("history owner is required")
        return self._digest("owner", client_id)

    def _document_id(self, source: MemorySource) -> str:
        return self._digest("document", source.client_id, source.conversation_id,
            source.message_id, str(source.offset), hashlib.sha256(source.text.encode()).hexdigest())

    def _decode(self, row, client_id: str) -> MemorySource:
        source = MemorySource(**json.loads(self.cipher.decrypt(row["ciphertext"])))
        if source.client_id != client_id or self._document_id(source) != row["id"]:
            raise ValueError("history provenance integrity mismatch")
        return replace(source, cloud_allowed=source.cloud_allowed is True and row["cloud_allowed"] == 1,
                       cloud_unknown=source.cloud_unknown is True and row["cloud_allowed"] == 2)

    def add(self, sources: Iterable[MemorySource]) -> int:
        """Deduplicate replayed messages, while retaining a verifiable source."""
        inserted = 0
        with closing(self._connect()) as db, db:
            for source in sources:
                owner = self._owner(source.client_id)
                if not source.conversation_id or not source.request_id or not source.message_id:
                    raise ValueError("history provenance is required")
                if source.role not in {"user", "assistant", "tool"}:
                    raise ValueError("only visible conversation content is indexable")
                if not source.text.strip():
                    continue
                terms = search_terms(source.text)
                if not terms:
                    continue
                source_id = self._document_id(source)
                conversation = self._digest("conversation", source.client_id, source.conversation_id)
                ciphertext = self.cipher.encrypt(json.dumps(asdict(source), ensure_ascii=False).encode())
                # 0: forbidden, 1: known export eligibility, 2: legacy unknown.
                # Unknown sources require a separate live grant, never a rewrite
                # of their historical provenance when the grant is toggled.
                cloud_state = 1 if source.cloud_allowed else 2 if source.cloud_unknown else 0
                cursor = db.execute("""INSERT OR IGNORE INTO memory_documents
                    (owner,id,conversation,created_at,cloud_allowed,length,ciphertext)
                    VALUES(?,?,?,?,?,?,?)""", (owner, source_id, conversation, source.created_at,
                        cloud_state, sum(terms.values()), ciphertext))
                if cursor.rowcount != 1:
                    # A later source restriction must take effect even on a replay.
                    if not source.cloud_allowed:
                        row = db.execute("SELECT * FROM memory_documents WHERE owner=? AND id=?", (owner, source_id)).fetchone()
                        existing = self._decode(row, source.client_id)
                        unknown = source.cloud_unknown and (existing.cloud_allowed or existing.cloud_unknown)
                        restricted = replace(existing, cloud_allowed=False, cloud_unknown=bool(unknown))
                        ciphertext = self.cipher.encrypt(json.dumps(asdict(restricted), ensure_ascii=False).encode())
                        db.execute("UPDATE memory_documents SET cloud_allowed=?,ciphertext=? WHERE owner=? AND id=?",
                                   (2 if unknown else 0, ciphertext, owner, source_id))
                    continue
                db.executemany("INSERT INTO memory_terms VALUES(?,?,?,?)",
                    ((owner, self._digest("term", source.client_id, term), source_id, count)
                     for term, count in terms.items()))
                inserted += 1
        return inserted

    def exclude(self, client_id: str, conversation_id: str, excluded: bool) -> None:
        owner = self._owner(client_id)
        conversation = self._digest("conversation", client_id, conversation_id)
        with closing(self._connect()) as db, db:
            if excluded:
                db.execute("INSERT OR IGNORE INTO memory_exclusions VALUES(?,?)", (owner, conversation))
            else:
                db.execute("DELETE FROM memory_exclusions WHERE owner=? AND conversation=?", (owner, conversation))

    def status(self, client_id: str) -> dict:
        owner = self._owner(client_id)
        with closing(self._connect()) as db:
            row = db.execute("""SELECT COUNT(*) AS chunks, COUNT(DISTINCT conversation) AS conversations,
                COALESCE(SUM(LENGTH(ciphertext)),0) AS encrypted_bytes FROM memory_documents WHERE owner=?""", (owner,)).fetchone()
            excluded = db.execute("SELECT COUNT(*) FROM memory_exclusions WHERE owner=?", (owner,)).fetchone()[0]
            cursor = db.execute("SELECT cursor FROM memory_checkpoints WHERE owner=?", (owner,)).fetchone()
        from .memory_ingestion import IngestionProgress
        return {**dict(row), "excluded_conversations": excluded,
                "archive_cursor": cursor[0] if cursor else 0, "index_version": INDEX_VERSION,
                "progress": IngestionProgress(self).status(client_id)}

    def checkpoint(self, client_id: str, cursor: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO memory_checkpoints VALUES(?,?) ON CONFLICT(owner)
                DO UPDATE SET cursor=MAX(memory_checkpoints.cursor,excluded.cursor)""",
                (self._owner(client_id), int(cursor)))

    def read(self, client_id: str, source_id: str, *, cloud: bool = True,
             legacy_cloud_approved: bool = False) -> MemorySource | None:
        with closing(self._connect()) as db:
            row = db.execute("""SELECT d.* FROM memory_documents d WHERE d.owner=? AND d.id=?
                AND (?=0 OR d.cloud_allowed=1 OR (?=1 AND d.cloud_allowed=2)) AND NOT EXISTS (SELECT 1 FROM memory_exclusions e
                WHERE e.owner=d.owner AND e.conversation=d.conversation)""",
                (self._owner(client_id), source_id, int(cloud), int(legacy_cloud_approved is True))).fetchone()
        if row is None:
            return None
        source = self._decode(row, client_id)
        if cloud and not (source.cloud_allowed or (legacy_cloud_approved is True and source.cloud_unknown)):
            return None
        return source

    def search(self, client_id: str, query: str, *, cloud: bool = True,
               legacy_cloud_approved: bool = False,
               conversation_id: str | None = None, limit: int = 6,
               exclude_message_ids: frozenset[str] = frozenset(),
               required_identifiers: frozenset[str] | None = None,
               cancel_event=None, deadline: float | None = None,
               diagnostics: dict | None = None) -> list[MemoryHit]:
        owner = self._owner(client_id)
        terms = list(search_terms(query[:8192]))[:MAX_QUERY_TERMS]
        identifiers = explicit_identifiers(query) if required_identifiers is None else required_identifiers
        if not terms:
            return []
        hashed = {self._digest("term", client_id, term): term for term in terms}
        placeholders = ",".join("?" for _ in hashed)
        started = time.monotonic()
        deadline = min(deadline, started + 2) if deadline is not None else started + 2
        diagnostics = diagnostics if diagnostics is not None else {}
        def interrupted():
            return time.monotonic() >= deadline or (cancel_event is not None and cancel_event.is_set())
        with closing(self._connect()) as db:
            # A cancelled optional lookup must not occupy a worker in a long busy wait.
            db.execute("PRAGMA busy_timeout=100")
            db.set_progress_handler(lambda: int(interrupted()), 1000)
            def fetch(stage, sql, args):
                diagnostics["stage"] = stage
                start = time.monotonic()
                try:
                    if interrupted():
                        raise sqlite3.OperationalError("interrupted")
                    with phase("history_search_" + stage):
                        return db.execute(sql, args).fetchall()
                except sqlite3.Error as error:
                    diagnostics["sqlite_errorcode"] = getattr(error, "sqlite_errorcode", None)
                    diagnostics["sqlite_errorname"] = getattr(error, "sqlite_errorname", None) or (
                        "SQLITE_INTERRUPT" if str(error) == "interrupted" else "SQLITE_ERROR")
                    raise
                finally:
                    diagnostics.setdefault("stages_ms", {})[stage] = round((time.monotonic()-start)*1000, 3)
            db.execute("BEGIN")
            corpus = fetch("corpus", "SELECT COUNT(*), COALESCE(AVG(length),1) FROM memory_documents WHERE owner=?", (owner,))[0]
            # Group the narrow posting rows first. Joining documents per term and
            # carrying ciphertext through GROUP BY sorted gigabytes before LIMIT.
            candidates = fetch("candidates", f"""WITH matches AS MATERIALIZED (
                SELECT document, COUNT(*) AS matches
                FROM memory_terms WHERE owner=? AND term IN ({placeholders}) GROUP BY document
                ) SELECT d.id, m.matches FROM matches m
                CROSS JOIN memory_documents d ON d.id=m.document AND d.owner=?
                WHERE (?=0 OR d.cloud_allowed=1 OR (?=1 AND d.cloud_allowed=2))
                AND NOT EXISTS (SELECT 1 FROM memory_exclusions e WHERE e.owner=d.owner AND e.conversation=d.conversation)
                ORDER BY m.matches DESC, d.created_at DESC, d.id DESC LIMIT ?""",
                (owner, *hashed, owner, int(cloud), int(legacy_cloud_approved is True), MAX_CANDIDATES))
            if not candidates:
                return []
            selected = {row["id"]: row for row in candidates}
            marks = ",".join("?" for _ in selected)
            documents = fetch("documents", f"SELECT * FROM memory_documents WHERE owner=? AND id IN ({marks})", (owner, *selected))
            matched = {identifier: [] for identifier in selected}
            for row in fetch("matches", f"SELECT document,term FROM memory_terms WHERE owner=? AND term IN ({placeholders}) AND document IN ({marks})", (owner, *hashed, *selected)):
                matched[row["document"]].append(row["term"])
            by_id = {row["id"]: dict(row) for row in documents}
            rows = [{**by_id[row["id"]], "matched": matched[row["id"]]} for row in candidates]
            frequencies = dict(fetch("frequencies", f"SELECT term,COUNT(*) FROM memory_terms WHERE owner=? AND term IN ({placeholders}) GROUP BY term", (owner, *hashed)))
        hits = []
        for row in rows:
            if interrupted():
                diagnostics["stage"] = "decode"
                diagnostics["sqlite_errorname"] = "SQLITE_INTERRUPT"
                raise sqlite3.OperationalError("interrupted")
            matched = [hashed[value] for value in row["matched"]]
            exact = any(re.search(r"[./:_\\-]|\d", term) and len(term) >= 5 for term in matched)
            # One generic word is not sufficient evidence for injecting history.
            if len(matched) < 2 and not exact:
                continue
            source = self._decode(row, client_id)
            # Generic surrounding words cannot substitute for an explicit ID.
            # Compare whole normalized identifiers, not substrings or split terms.
            if identifiers and identifiers.isdisjoint(explicit_identifiers(source.text)):
                continue
            if cloud and not (source.cloud_allowed or (legacy_cloud_approved is True and source.cloud_unknown)):
                continue
            if source.message_id in exclude_message_ids:
                continue
            weights = sum(math.log(1 + (corpus[0] - frequencies[self._digest("term", client_id, term)] + .5)
                                   / (frequencies[self._digest("term", client_id, term)] + .5)) for term in matched)
            score = weights / (0.75 + 0.25 * row["length"] / max(1, corpus[1]))
            if exact:
                score *= 1.5
            if source.conversation_id == conversation_id:
                score *= 1.1
            hits.append(MemoryHit(row["id"], source, score, len(matched)))
        hits.sort(key=lambda hit: (hit.score, hit.source.created_at), reverse=True)
        return hits[:max(0, min(6, limit))]
