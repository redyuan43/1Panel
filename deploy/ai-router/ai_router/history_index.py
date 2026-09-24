"""Versioned routing evidence from completed archives and encrypted history."""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .content_audit import ArchiveReader
from .compaction import CapsuleCipher, message_hash, extract_messages
from .history_identity import HISTORY_IDENTITY_VERSION, verified_history_identity, public_history_identity
from .prefix_break import stage_bodies

CLIENTS = {"workbuddy-public", "workbuddy-qwen36-shared"}
BACKFILL_PROGRESS_KEY = f"router:verified-history:v{HISTORY_IDENTITY_VERSION}:all-clients:backfill-progress"


class HistoryIndexEvidenceError(ValueError):
    """A live branch cannot be migrated; reason is a fixed metadata-only code."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def validate_archive(archive, trace):
    if not archive:
        raise HistoryIndexEvidenceError("archive_missing")
    request, response = archive.get("request", {}), archive.get("response", {})
    if any(request.get(key) != trace.get(key) for key in
           ("request_id", "client_id", "conversation_id", "protocol")):
        raise HistoryIndexEvidenceError("archive_identity_mismatch")
    if response.get("complete") is not True or response.get("status_code") != 200:
        raise HistoryIndexEvidenceError("archive_not_complete")


def capsule_aliases(runtime, state):
    # persist_history() originally indexed this exact encrypted message list.
    # Do not reconstruct it from provider JSON, re-normalize it, or relabel v5.
    if not state.encrypted_capsule or state.identity_only:
        raise HistoryIndexEvidenceError("persisted_history_missing")
    cipher = getattr(runtime, "history_cipher", None)
    if cipher is None:
        cipher = CapsuleCipher(os.environ["AI_ROUTER_STATE_KEY"])
    messages = cipher.decrypt(state.encrypted_capsule)
    if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
        raise HistoryIndexEvidenceError("persisted_history_invalid")
    if not state.boundary_hash or message_hash(messages[-1]) != state.boundary_hash:
        raise HistoryIndexEvidenceError("persisted_history_boundary_mismatch")
    return (verified_history_identity(messages),)


def reader():
    return ArchiveReader(os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3"),
                         os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key"))


def raw_aliases(archive, trace):
    if not archive or trace.get("client_id") not in CLIENTS or trace.get("status") != "succeeded":
        return ()
    request, response = archive.get("request", {}), archive.get("response", {})
    if (request.get("request_id") != trace.get("request_id") or request.get("client_id") != trace.get("client_id")
            or request.get("protocol") != "chat" or request.get("conversation_id") != trace.get("conversation_id")
            or response.get("complete") is not True
            or response.get("status_code") != 200):
        return ()
    raw = stage_bodies(archive).get("after_directives")
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        return ()
    messages = raw["messages"]
    aliases = ["wb-raw-v1:" + verified_history_identity(messages)]
    output = response.get("assistant_items")
    if output is None:
        # Non-streaming complete() stores the JSON response, not assistant_items.
        # Retain exact archived fields; this is the client's raw-history namespace.
        body = response.get("body") or {}
        value = body.get("value") if body.get("encoding") == "json" else None
        choices = value.get("choices") if isinstance(value, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                output = [{**message, "role": "assistant"}]
    if not isinstance(output, list) or not output or not all(isinstance(m, dict) for m in output):
        return ()
    aliases.append("wb-raw-v1:" + verified_history_identity([*messages, *output]))
    return tuple(dict.fromkeys(aliases))


async def index_completed(runtime, trace, archive_reader=None):
    branch = trace.get("branch_id")
    if not branch or trace.get("status") != "succeeded" or trace.get("protocol") not in {"chat", "responses"}:
        return 0
    state = await runtime.conversations.get(branch)
    if state is None:
        return 0
    if state.conversation_id != trace.get("conversation_id"):
        raise HistoryIndexEvidenceError("branch_identity_mismatch")
    archive_reader = archive_reader or await asyncio.to_thread(reader)
    archive = await asyncio.to_thread(archive_reader.read, trace["request_id"])
    validate_archive(archive, trace)
    if trace["protocol"] == "chat" and trace.get("client_id") in CLIENTS:
        aliases = await asyncio.to_thread(raw_aliases, archive, trace)
    else:
        aliases = await asyncio.to_thread(capsule_aliases, runtime, state)
    if not aliases:
        raise HistoryIndexEvidenceError("history_evidence_missing")
    public_output = archive["response"].get("public_assistant_items")
    public_body = stage_bodies(archive).get("history_identity_input")
    if public_output is not None:
        if (not isinstance(public_body, dict) or not isinstance(public_output, list)
                or not public_output or not all(isinstance(item, dict) for item in public_output)):
            raise HistoryIndexEvidenceError("public_history_evidence_invalid")
        aliases = tuple(dict.fromkeys((*aliases, public_history_identity(
            extract_messages(public_body, trace["protocol"]), public_output,
            client_id=trace["client_id"], protocol=trace["protocol"],
        ))))
    # Archive reads can be slow. Never recreate or renew an expired branch.
    state = await runtime.conversations.get(branch)
    if state is None:
        return 0
    if state.conversation_id != trace.get("conversation_id"):
        raise HistoryIndexEvidenceError("branch_identity_mismatch")
    if trace["protocol"] == "chat" and trace.get("client_id") in CLIENTS:
        from .workbuddy_history import WorkBuddyHistory
        await asyncio.to_thread(
            WorkBuddyHistory(runtime.route_traces.database_path).record, trace, archive=archive,
        )
    await runtime.conversations.map_history(trace["client_id"], aliases, branch)
    return bool(aliases)


def recent_completed(path, *, since, until, before=None, limit=128):
    """Stable keyset page; equal timestamps must not skip or duplicate rows."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    cursor_sql = " AND (started_at, request_id) < (?,?)" if before else ""
    parameters = [since, until, *(before or ()), limit]
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        return db.execute("SELECT started_at, request_id, payload_json FROM route_traces "
                          "WHERE status='succeeded' AND protocol IN ('chat','responses') AND started_at>? AND started_at<=? " + cursor_sql +
                          " ORDER BY started_at DESC, request_id DESC LIMIT ?", parameters).fetchall()


async def rebuild_verified_history(runtime, *, max_records=2048, max_seconds=120, page_size=128):
    """Bounded, resumable index repair, not lineage or directive restoration.

    Only authenticated completed archives may supply current evidence. A saved
    cursor resumes unfinished work on the next invocation; failures never skip a
    row. Counts distinguish expired/ineligible records from successful repairs.
    """
    if max_records < 1 or max_seconds <= 0 or page_size < 1:
        raise ValueError("backfill bounds must be positive")
    token = uuid4().hex
    key = "router:verified-history:backfill-lock"
    progress_key = BACKFILL_PROGRESS_KEY
    acquired = False
    report = dict(version=HISTORY_IDENTITY_VERSION, scanned=0, records=0, skipped=0, failed=0, complete=False)
    deadline = time.monotonic() + max_seconds
    try:
        acquired = await runtime.store.acquire_lock(key, token, 600)
        if not acquired:
            return {**report, "reason": "locked"}
        progress = await runtime.store.get_json(progress_key)
        if not progress:
            until = time.time()
            progress = {"since": until - 86400, "until": until, "before": None}
            await runtime.store.set_json(progress_key, progress, ttl_seconds=86400)
        archive_reader = await asyncio.to_thread(reader)
        while report["scanned"] < max_records and time.monotonic() < deadline:
            if not await runtime.store.renew_lock(key, token, 600):
                raise RuntimeError("backfill lock lost")
            rows = await asyncio.to_thread(recent_completed, runtime.route_traces.database_path,
                **progress, limit=min(page_size, max_records - report["scanned"]))
            if not rows:
                await runtime.store.delete(progress_key)
                report["complete"] = True
                break
            for started_at, request_id, payload in rows:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                indexed = await asyncio.wait_for(
                    index_completed(runtime, json.loads(payload), archive_reader), timeout=min(30, remaining))
                report["scanned"] += 1
                report["records"] += int(indexed)
                report["skipped"] += int(not indexed)
                progress["before"] = [started_at, request_id]
            await runtime.store.set_json(progress_key, progress, ttl_seconds=86400)
        runtime.audit.write("verified_history_backfill_completed" if report["complete"] else
                            "verified_history_backfill_pending", **report)
    except Exception as error:
        report["complete"] = False
        report["failed"] += 1
        if isinstance(error, HistoryIndexEvidenceError):
            report["failure_reason"] = error.reason
        runtime.audit.write("verified_history_backfill_unavailable", **report, error_type=type(error).__name__)
    finally:
        if acquired:
            await runtime.store.release_lock(key, token)
    return report


def main():
    """Explicit maintenance entrypoint. The default only prints a plan."""
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild versioned history indexes from trusted archives; never restore directives.")
    parser.add_argument("--apply", action="store_true", help="write Redis indexes; requires maintenance approval")
    parser.add_argument("--max-records", type=int, default=2048)
    parser.add_argument("--max-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.max_records < 1 or args.max_seconds <= 0:
        parser.error("backfill bounds must be positive")
    if not args.apply:
        print(json.dumps({"mode": "plan", "version": HISTORY_IDENTITY_VERSION,
                          "max_records": args.max_records, "max_seconds": args.max_seconds,
                          "window_hours": 24, "writes": False}))
        return 0

    async def apply():
        from types import SimpleNamespace
        from .config import Settings
        from .policy import ConversationRepository
        from .store import RedisStateStore
        store = RedisStateStore(os.environ["AI_ROUTER_REDIS_URL"])
        runtime = SimpleNamespace(store=store, conversations=ConversationRepository(store, Settings()),
            route_traces=SimpleNamespace(database_path=os.environ.get("AI_ROUTER_ROUTE_TRACE_DB_PATH", "/data/audit/route-traces.sqlite3")),
            audit=SimpleNamespace(write=lambda event, **fields: print(json.dumps({"event": event, **fields}))))
        try:
            report = await rebuild_verified_history(runtime, max_records=args.max_records, max_seconds=args.max_seconds)
            print(json.dumps(report))
            return 0 if report["complete"] else 2
        finally:
            await store.close()
    return asyncio.run(apply())


if __name__ == "__main__":
    raise SystemExit(main())
