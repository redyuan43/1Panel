"""Immutable WorkBuddy history overlays; encrypted archives hold all content."""
import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing

from .content_audit import ArchiveReader
from .prefix_break import stage_bodies
from .protocol import move_workbuddy_dynamic_context, _prepend_workbuddy_dynamic_context, stabilize_workbuddy_tools

VERSION = 1
STAGE = "workbuddy_history_preserved"


def rendered_messages(body):
    # Transport UI fields may vary, but function-call identity and message name
    # are semantic even on templates that do not print them.
    keys = ("role", "content", "reasoning_content", "tool_calls", "tool_call_id", "name")
    return [{k:m[k] for k in keys if k in m} for m in body.get("messages", []) if isinstance(m,dict)]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def prepare(raw, client_id):
    move = move_workbuddy_dynamic_context(raw, "chat", client_id=client_id)
    if not move.moved:
        return None
    bare = copy.deepcopy(move.body)
    i = move.target_user_index
    original, moved = raw["messages"][i].get("content"), bare["messages"][i]["content"]
    if isinstance(original, list):
        block = moved[0]["text"]
    else:
        block = moved[:-(len(original) + 2)] if original else moved
    bare["messages"][i]["content"] = copy.deepcopy(original)
    return move, bare, block


def chain(body):
    digest = hashlib.sha256(b"workbuddy-history-v1").digest()
    result = []
    for message in rendered_messages(body):
        digest = hashlib.sha256(digest + encode(message)).digest()
        result.append(digest.hex())
    return result


def reconcile(raw, client_id, old_raw, old_body, old_positions=None):
    """Exact model-visible raw-prefix proof, never fuzzy/session-ID matching."""
    fresh, prior = prepare(raw, client_id), prepare(old_raw, client_id)
    if fresh is None or prior is None:
        return None
    move, bare, block = fresh
    _, old_bare, old_block = prior
    previous = rendered_messages(old_bare)
    current = rendered_messages(bare)
    n = len(previous)
    if n < 2 or current[:n] != previous or len(current) < n:
        return None
    positions = old_positions if old_positions is not None else list(range(n))
    history = old_body.get("messages", [])
    if (len(positions) != n or any(type(i) is not int for i in positions)
            or positions != sorted(set(positions)) or not positions or positions[0] < 0
            or positions[-1] >= len(history)):
        return None
    def has_snapshot(message):
        content = message.get("content")
        if isinstance(content, str):
            return content == old_block or content.startswith(old_block + "\n\n")
        return isinstance(content, list) and bool(content) and isinstance(content[0], dict) and content[0].get("text") == old_block
    if not any(has_snapshot(message) for message in history if message.get("role") == "user"):
        return None
    # Every original role/content/argument/image must still exist at its mapped
    # position; only server-owned dynamic prefixes may differ in content.
    for src, dst in enumerate(positions):
        a, b = old_bare["messages"][src], history[dst]
        if {k:v for k,v in rendered_messages({"messages":[a]})[0].items() if k != "content"} != {k:v for k,v in rendered_messages({"messages":[b]})[0].items() if k != "content"}:
            return None
        original, normalized = a.get("content"), b.get("content")
        if original != normalized:
            if a.get("role") != "user":
                return None
            if isinstance(original, list):
                if not isinstance(normalized,list) or normalized[1:] != original or not isinstance(normalized[0],dict) or not normalized[0].get("text", "").startswith("<workbuddy_dynamic_context>"):
                    return None
            elif not isinstance(original,str) or not isinstance(normalized,str) or (original and not normalized.endswith("\n\n" + original)) or not normalized.startswith("<workbuddy_dynamic_context>"):
                return None
    value = copy.deepcopy(bare)
    value["messages"] = copy.deepcopy(history)
    # Preserve the caller's current transport/UI metadata while retaining only
    # normalized content. Model-visible non-content fields were verified above.
    for src, dst in enumerate(positions):
        content = value["messages"][dst].get("content")
        value["messages"][dst] = copy.deepcopy(raw["messages"][src])
        if "content" in history[dst]:
            value["messages"][dst]["content"] = content
    result_positions = list(positions)
    for message in bare["messages"][n:]:
        result_positions.append(len(value["messages"]))
        value["messages"].append(copy.deepcopy(message))
    update = block != old_block
    if update:
        # A new user turn can carry the current snapshot. Tool continuations
        # receive a separate late user context message, preserving tool results.
        target = move.target_user_index
        if target >= n:
            _prepend_workbuddy_dynamic_context(value["messages"][result_positions[target]], block)
        else:
            value["messages"].append({"role":"user", "content":block})
    preserved = rendered_messages(value)[:len(history)] == rendered_messages(old_body)
    if not preserved:
        raise ValueError("historical_prefix_modified")
    return value, {"version": VERSION, "positions": result_positions,
        "status":"passed", "association":"exact_raw_prefix", "history_preserved":True,
        "preserved_messages":len(history), "raw_prefix_messages":n,
        "dynamic_update_appended":update,
        "model_history_prefix_unchanged":True,
        "tools_changed": stabilize_workbuddy_tools(old_body, "chat", client_id=client_id)[0].get("tools") != stabilize_workbuddy_tools(value, "chat", client_id=client_id)[0].get("tools")}


class WorkBuddyHistory:
    def __init__(self, database_path, archive_path=None, key_path=None):
        self.database_path = str(database_path)
        self.archive_path = archive_path or os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3")
        self.key_path = key_path or os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key")

    def reader(self):
        return ArchiveReader(self.archive_path, self.key_path)

    @staticmethod
    def scope(client_id, model):
        return hashlib.sha256(encode([client_id, model, VERSION])).hexdigest()

    def record(self, trace):
        if trace.get("status") != "succeeded" or trace.get("protocol") != "chat":
            return
        client = trace.get("client_id")
        if client not in {"workbuddy-public", "workbuddy-qwen36-shared"}:
            return
        archived = self.reader().read(trace["request_id"])
        bodies = stage_bodies(archived)
        raw = bodies.get("after_directives")
        normalized = bodies.get(STAGE) or bodies.get("tools_stabilized")
        if not raw or not normalized or not prepare(raw, client):
            return
        # Legacy snapshots must pass the same conservation proof as new ones.
        checks = (archived.get("pipeline") or {}).get("checks", [])
        if not any(x.get("check") == "message_order_and_tool_history" and x.get("status") == "passed" for x in checks) or any(x.get("status") == "failed" for x in checks):
            return
        history_check = next((c for c in checks if c.get("check") == "workbuddy_history"), {})
        if history_check.get("reorder_bypassed"):
            return
        hashes = chain(prepare(raw, client)[1])
        if len(hashes) < 2:
            return
        with closing(sqlite3.connect(self.database_path, timeout=2)) as db, db:
            db.execute("INSERT OR REPLACE INTO workbuddy_history VALUES(?,?,?,?,?,?)", (
                trace["request_id"], self.scope(client, raw.get("model")), hashes[-1],
                len(hashes), trace.get("completed_at") or time.time(), VERSION))
            db.execute("DELETE FROM workbuddy_history WHERE created_at<?", (time.time()-86400,))
            db.execute("DELETE FROM workbuddy_history WHERE request_id IN (SELECT request_id FROM workbuddy_history ORDER BY created_at DESC LIMIT -1 OFFSET 2048)")

    def restore(self, raw, client_id, *, reset=False):
        prepared = prepare(raw, client_id)
        if prepared is None or reset:
            return None, {"status":"skipped", "association":"unconfirmed", "reason":"reset" if reset else "not_applicable"}
        _, bare, _ = prepared
        hashes = chain(bare)
        with closing(sqlite3.connect(self.database_path, timeout=2)) as db:
            # Small metadata-only index; no archive scans on the request path.
            rows = db.execute("SELECT request_id,prefix_hash,message_count FROM workbuddy_history WHERE scope=? AND created_at>? AND version=? ORDER BY message_count DESC,created_at DESC LIMIT 256", (self.scope(client_id, raw.get("model")), time.time()-86400, VERSION)).fetchall()
        candidates = [row for row in rows if 2 <= row[2] <= len(hashes) and hashes[row[2]-1] == row[1]]
        reader = None
        for request_id, _, _ in candidates[:3]:
            reader = reader or self.reader()
            archived = reader.read(request_id)
            bodies = stage_bodies(archived)
            old_raw = bodies.get("after_directives")
            old_body = bodies.get(STAGE) or bodies.get("tools_stabilized")
            if not old_raw or not old_body:
                continue
            report = next((c for c in (archived.get("pipeline") or {}).get("checks", []) if c.get("check") == "workbuddy_history"), {})
            result = reconcile(raw, client_id, old_raw, old_body, report.get("positions"))
            if result:
                value, evidence = result
                evidence["previous_request_id"] = request_id
                return value, evidence
        return None, {"status":"skipped", "association":"unconfirmed", "reason":"no_verified_raw_prefix"}

    async def apply(self, raw, client_id, *, reset=False):
        try:
            return await asyncio.to_thread(self.restore, raw, client_id, reset=reset)
        except Exception as error:
            # Explicit fail-open to the caller's original input; never silently
            # perform the known destructive full-history relocation on failure.
            return copy.deepcopy(raw), {"status":"failed", "association":"unconfirmed", "reason":type(error).__name__, "reorder_bypassed":True}
