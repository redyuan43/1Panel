"""Synthetic encrypted archive and content preservation checks; CPU only."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import zlib

from cryptography.fernet import Fernet

WORK = pathlib.Path(os.environ.get("CACHE_AUDIT_WORK", pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(WORK))
from ai_router.content_audit import ArchiveReader, ContentObservation
from ai_router.protocol import move_workbuddy_dynamic_context


SYNTHETIC_TEXT = "synthetic-sensitive-fixture-中文-only"


class SnapshotContractTests(unittest.TestCase):
    def test_capture_preserves_body_before_later_mutation(self):
        body = {"messages": [{"role": "user", "content": SYNTHETIC_TEXT}]}
        observer = ContentObservation()
        observer.capture("received", body)
        body["messages"][0]["content"] = "mutated"
        original = observer.archive()["bodies"][observer.stages[0]["sha256"]]
        self.assertEqual(original["messages"][0]["content"], SYNTHETIC_TEXT)

    def test_metadata_excludes_content_but_retains_stage_digest(self):
        observer = ContentObservation()
        observer.capture("received", {"prompt": SYNTHETIC_TEXT})
        metadata = observer.metadata()
        self.assertNotIn(SYNTHETIC_TEXT, json.dumps(metadata))
        self.assertEqual(metadata["stages"][0]["stage"], "received")
        self.assertEqual(len(metadata["stages"][0]["sha256"]), 64)

    def test_equal_bodies_are_deduplicated_without_losing_stages(self):
        observer = ContentObservation()
        for stage in ("received", "normalized", "routed"):
            observer.capture(stage, {"prompt": SYNTHETIC_TEXT})
        self.assertEqual(len(observer.archive()["bodies"]), 1)
        self.assertEqual(len(observer.stages), 3)

    def test_valid_workbuddy_reorder_retains_tool_history_and_image(self):
        before = {
            "model": "siyuan/qwen36-shared",
            "messages": [
                {"role": "system", "content": "stable instructions\n<workbuddy_dynamic_context>\nchanging fixture\n</workbuddy_dynamic_context>"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "fixture-call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "fixture-call", "content": "fixture-result"},
                {"role": "user", "content": [{"type": "text", "text": SYNTHETIC_TEXT}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}]},
            ],
        }
        saved = copy.deepcopy(before)
        move = move_workbuddy_dynamic_context(before, "chat", client_id="workbuddy-qwen36-shared")
        self.assertTrue(move.moved)
        observer = ContentObservation()
        observer.check_workbuddy(saved, move.body, move)
        self.assertTrue(observer.checks)
        self.assertTrue(all(x["status"] == "passed" for x in observer.checks), observer.checks)
        self.assertEqual(before, saved)

    def test_content_corruption_fails_preservation_check(self):
        before = {
            "model": "siyuan/qwen36-shared",
            "messages": [
                {"role": "system", "content": "stable\n<workbuddy_dynamic_context>\nchanging fixture\n</workbuddy_dynamic_context>"},
                {"role": "user", "content": SYNTHETIC_TEXT},
            ],
        }
        move = move_workbuddy_dynamic_context(before, "chat", client_id="workbuddy-qwen36-shared")
        self.assertTrue(move.moved)
        corrupted = copy.deepcopy(move.body)
        corrupted["messages"][-1]["content"] = corrupted["messages"][-1]["content"].replace(SYNTHETIC_TEXT, "lost")
        observer = ContentObservation()
        observer.check_workbuddy(before, corrupted, move)
        self.assertEqual(next(x["status"] for x in observer.checks if x["check"] == "user_content_and_images"), "failed")


class ArchiveReadOnlyContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "synthetic.sqlite3"
        self.key_path = self.root / "synthetic.key"
        self.key = Fernet.generate_key()
        self.key_path.write_bytes(self.key)
        self.observer = ContentObservation()
        self.body = {"messages": [{"role": "user", "content": SYNTHETIC_TEXT * 8}]}
        self.observer.capture("received", self.body)
        self.payload = {"request": {"received_body": self.body}, "routing_attempts": [], "pipeline": self.observer.archive()}
        self.write_payload(self.payload)

    def write_payload(self, payload):
        index = hmac.new(base64.urlsafe_b64decode(self.key), b"1panel-ai-router-training-index-v1", hashlib.sha256).digest()
        request_hash = hmac.new(index, b"request:synthetic-request", hashlib.sha256).hexdigest()
        encrypted = Fernet(self.key).encrypt(zlib.compress(json.dumps(payload).encode()))
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS training_records(request_hash TEXT PRIMARY KEY, payload_ciphertext BLOB)")
            db.execute("INSERT OR REPLACE INTO training_records VALUES(?,?)", (request_hash, encrypted))

    def file_state(self):
        return {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in self.root.iterdir() if p.is_file()}

    def test_read_only_reader_preserves_database_key_modes_and_mtime(self):
        previous = self.file_state()
        reader = ArchiveReader(self.path, self.key_path)
        self.assertEqual(reader.read("synthetic-request"), self.payload)
        self.assertIsNone(reader.read("missing-request"))
        self.assertEqual(self.file_state(), previous)

    def test_missing_archive_is_not_created(self):
        missing = self.root / "missing.sqlite3"
        reader = ArchiveReader(missing, self.key_path)
        with self.assertRaises(sqlite3.OperationalError):
            reader.read("synthetic-request")
        self.assertFalse(missing.exists())

    def test_metadata_does_not_decrypt_content_into_public_result(self):
        result = ArchiveReader(self.path, self.key_path).content("synthetic-request")
        self.assertFalse(result["legacy"])
        self.assertNotIn(SYNTHETIC_TEXT, json.dumps(result, ensure_ascii=False))
        self.assertNotIn("bodies", result)
        self.assertNotIn("text", result)

    def test_explicit_stage_pagination_reassembles_unicode_losslessly(self):
        reader = ArchiveReader(self.path, self.key_path)
        pieces, offset = [], 0
        while offset is not None:
            page = reader.content("synthetic-request", stage="received", offset=offset, limit=29)
            pieces.append(page["text"])
            self.assertLessEqual(len(page["text"]), 29)
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(pieces)), self.body)

    def test_unknown_stage_is_explicit_failure(self):
        with self.assertRaises(KeyError):
            ArchiveReader(self.path, self.key_path).content("synthetic-request", stage="never-recorded")

    def test_legacy_archive_is_labeled_without_fabricating_raw_stage(self):
        payload = copy.deepcopy(self.payload)
        payload.pop("pipeline")
        self.write_payload(payload)
        result = ArchiveReader(self.path, self.key_path).content("synthetic-request")
        self.assertTrue(result["legacy"])
        self.assertEqual(result["stages"][0]["stage"], "legacy_after_directives")
        self.assertNotIn("received", [x["stage"] for x in result["stages"]])

    def test_wrong_key_cannot_find_or_disclose_record(self):
        wrong = self.root / "wrong.key"
        wrong.write_bytes(Fernet.generate_key())
        self.assertIsNone(ArchiveReader(self.path, wrong).read("synthetic-request"))


if __name__ == "__main__":
    unittest.main()
