"""Encrypted stage snapshots and a strictly read-only archive reader."""
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
import time
import zlib
from collections import Counter
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet


def encoded(body):
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class ContentObservation:
    def __init__(self):
        self.started = time.monotonic()
        self.stages = []
        self.bodies = {}
        self.checks = []

    def capture(self, stage, body, *, archive_body=True):
        raw = encoded(body)
        digest = hashlib.sha256(raw).hexdigest()
        if archive_body:
            self.bodies.setdefault(digest, json.loads(raw))
        self.stages.append({"stage": stage, "sha256": digest, "bytes": len(raw),
                            "archived": archive_body,
                            "last_role": (body.get("messages") or [{}])[-1].get("role") if isinstance(body.get("messages"), list) and all(isinstance(m,dict) for m in body["messages"]) else None,
                            "offset_ms": round((time.monotonic()-self.started)*1000, 3)})

    def metadata(self):
        return {"stages": self.stages, "checks": self.checks}

    def archive(self):
        return {**self.metadata(), "bodies": self.bodies, "version": 1}

    def check_workbuddy(self, before, after, move):
        if not move.moved:
            self.checks = [{"check": "workbuddy_reorder", "status": "skipped", "reason": move.skip_reason}]
            return
        index = move.target_user_index
        a, b = before.get("messages", []), after.get("messages", [])
        findings = []
        def check(name, passed):
            findings.append({"check": name, "status": "passed" if passed else "failed"})
        if not all(isinstance(m, dict) for m in a + b) or not isinstance(index, int) or not 0 <= index < min(len(a), len(b)):
            check("message_order_and_tool_history", False)
            self.checks = findings
            return
        def preserved_message(i, old, new):
            if i == index or str(old.get("role", "")).lower() == "system":
                return {k: v for k, v in old.items() if k != "content"} == {k: v for k, v in new.items() if k != "content"}
            return old == new
        check("message_order_and_tool_history", len(a) == len(b) and all(
            preserved_message(i, old, new) for i, (old, new) in enumerate(zip(a, b))))
        old_user, new_user = a[index].get("content"), b[index].get("content")
        if isinstance(old_user, list) and isinstance(new_user, list):
            check("user_content_and_images", new_user[1:] == old_user)
            dynamic = new_user[0].get("text", "") if new_user else ""
        else:
            check("user_content_and_images", isinstance(new_user, str) and new_user.endswith("\n\n" + old_user) if old_user else True)
            dynamic = new_user[:-len(old_user)] if old_user else new_user
        old_tools, new_tools = copy.deepcopy(before.get("tools", [])), copy.deepcopy(after.get("tools", []))
        moved_descriptions = Counter()
        if isinstance(old_tools, list) and isinstance(new_tools, list) and len(old_tools) == len(new_tools):
            for old, new in zip(old_tools, new_tools):
                if not isinstance(old, dict) or not isinstance(new, dict):
                    continue
                of, nf = old.get("function", {}), new.get("function", {})
                if not isinstance(of, dict) or not isinstance(nf, dict):
                    continue
                if of.get("name") in {"Agent", "Skill", "ToolSearch"} and of.get("description") != nf.get("description"):
                    desc = of.get("description", "")
                    block = f'<workbuddy_tool_description name="{of["name"]}">\n{desc}\n</workbuddy_tool_description>'
                    moved_descriptions[block] += 1
                    of["description"] = nf.get("description")
        check("tool_definitions_and_parameters", old_tools == new_tools)
        check("dynamic_tool_content_once", all(isinstance(dynamic, str) and dynamic.count(block) == count for block, count in moved_descriptions.items()))
        from .protocol import _WORKBUDDY_MEMORY_HEADING, _WORKBUDDY_MEMORY_END, _WORKBUDDY_MEMORY_PLACEHOLDER
        memory_ok = True
        for old, new in zip(a, b):
            if str(old.get("role", "")).lower() != "system" or old == new: continue
            text = old.get("content", "")
            if "<workbuddy_dynamic_context>" in text:
                left, moved = text.split("<workbuddy_dynamic_context>", 1)
                moved = moved.split("</workbuddy_dynamic_context>", 1)[0].strip()
                memory_ok &= new.get("content") == left.rstrip() and dynamic.count(moved) == 1
            elif _WORKBUDDY_MEMORY_HEADING in text:
                left, rest = text.split(_WORKBUDDY_MEMORY_HEADING, 1)
                moved, right = rest.split(_WORKBUDDY_MEMORY_END, 1)
                block = "<workbuddy_workspace_memory>\n" + moved.strip() + "\n</workbuddy_workspace_memory>"
                expected = (left + _WORKBUDDY_MEMORY_HEADING).rstrip() + "\n\n" + _WORKBUDDY_MEMORY_PLACEHOLDER + "\n\n" + _WORKBUDDY_MEMORY_END + right
                memory_ok &= dynamic.count(block) == 1 and new.get("content") == expected
            else:
                memory_ok = False
        check("workspace_memory_and_stable_content", memory_ok)
        check("single_dynamic_block", isinstance(dynamic, str) and dynamic.count("<workbuddy_dynamic_context>") == 1 and dynamic.count("</workbuddy_dynamic_context>") == 1)
        self.checks = findings


class ArchiveReader:
    """No constructor initialization, chmod, migration, or write access."""
    def __init__(self, database_path, key_path):
        self.path = Path(database_path)
        key = Path(key_path).read_bytes().strip()
        self.cipher = Fernet(key)
        self.index_key = hmac.new(base64.urlsafe_b64decode(key), b"1panel-ai-router-training-index-v1", hashlib.sha256).digest()

    def read(self, request_id):
        digest = hmac.new(self.index_key, ("request:" + request_id).encode(), hashlib.sha256).hexdigest()
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute("SELECT payload_ciphertext FROM training_records WHERE request_hash=?", (digest,)).fetchone()
        if not row: return None
        return json.loads(zlib.decompress(self.cipher.decrypt(row[0])))

    def content(self, request_id, stage=None, offset=0, limit=16384):
        payload = self.read(request_id)
        if payload is None: return None
        pipeline = payload.get("pipeline")
        if not pipeline:
            pipeline = {"stages": [], "bodies": {}, "checks": []}
            for label, body in [("legacy_after_directives", payload["request"].get("received_body")), ("effective", payload["request"].get("effective_body"))] + [
                ("legacy_routed_" + str(a.get("attempt", i+1)), a.get("routed_body")) for i,a in enumerate(payload.get("routing_attempts", []))]:
                if body is not None:
                    digest = hashlib.sha256(encoded(body)).hexdigest()
                    pipeline["stages"].append({"stage": label, "sha256": digest})
                    pipeline["bodies"][digest] = body
        result = {"request_id": request_id, "stages": pipeline["stages"], "checks": pipeline.get("checks", []), "legacy": not bool(payload.get("pipeline"))}
        if stage:
            selected = next((x for x in reversed(pipeline["stages"]) if x["stage"] == stage), None)
            if selected is None or selected.get("archived") is False:
                raise KeyError(stage)
            body = pipeline["bodies"][selected["sha256"]]
            text = json.dumps(body, ensure_ascii=False, indent=2)
            result.update(stage=stage, text=text[offset:offset+limit], offset=offset, total_chars=len(text), next_offset=offset+limit if offset+limit < len(text) else None)
        return result
