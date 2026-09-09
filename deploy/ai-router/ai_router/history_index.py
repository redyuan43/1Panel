"""Versioned routing evidence from completed, encrypted WorkBuddy archives."""
import asyncio
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .content_audit import ArchiveReader
from .history import verified_history_identity
from .prefix_break import stage_bodies

CLIENTS = {"workbuddy-public", "workbuddy-qwen36-shared"}


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
    if isinstance(output, list) and output and all(isinstance(m, dict) for m in output):
        aliases.append("wb-raw-v1:" + verified_history_identity([*messages, *output]))
    return tuple(dict.fromkeys(aliases))


async def index_completed(runtime, trace, archive_reader=None):
    branch = trace.get("branch_id")
    if trace.get("client_id") not in CLIENTS or not branch or trace.get("status") != "succeeded":
        return 0
    state = await runtime.conversations.get(branch)
    if state is None or state.conversation_id != trace.get("conversation_id"):
        return 0
    archive_reader = archive_reader or await asyncio.to_thread(reader)
    archive = await asyncio.to_thread(archive_reader.read, trace["request_id"])
    aliases = raw_aliases(archive, trace)
    await runtime.conversations.map_history(trace["client_id"], aliases, branch)
    return bool(aliases)


def recent_completed(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        rows = db.execute("SELECT request_id, client_id, conversation_id, payload_json FROM route_traces "
                          "WHERE status='succeeded' AND protocol='chat' AND started_at>? "
                          "AND client_id IN (?,?) ORDER BY started_at DESC LIMIT 2048",
                          (time.time()-86400, *sorted(CLIENTS))).fetchall()
    import json
    return [json.loads(row[3]) for row in rows]


async def rebuild_verified_history(runtime):
    token = uuid4().hex
    key = "router:verified-history:backfill-lock"
    acquired = False
    try:
        acquired = await runtime.store.acquire_lock(key, token, 600)
        if not acquired:
            return
        archive_reader = await asyncio.to_thread(reader)
        traces = await asyncio.to_thread(recent_completed, runtime.route_traces.database_path)
        count, failed = 0, 0
        for trace in traces:
            try:
                count += await index_completed(runtime, trace, archive_reader)
            except Exception:
                failed += 1
            await asyncio.sleep(0)
        runtime.audit.write("verified_history_backfill_completed", version=5, records=count, failed=failed)
    except Exception as error:
        runtime.audit.write("verified_history_backfill_unavailable", version=5, error_type=type(error).__name__)
    finally:
        if acquired:
            await runtime.store.release_lock(key, token)
