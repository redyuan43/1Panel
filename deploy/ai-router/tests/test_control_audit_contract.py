"""New management endpoints exercised with real ASGI/auth and synthetic stores."""
import asyncio
import base64
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
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_cache_audit_contract import audit, trace, operation, WORK

sys.path.insert(0, str(WORK))
from ai_router.auth import AuthManager
from ai_router.control import create_app
from ai_router.content_audit import ContentObservation


class ControlAuditContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.path = root / "synthetic-traces.sqlite3"
        self.archive_path = root / "synthetic-archive.sqlite3"
        self.key_path = root / "synthetic.key"
        self.key = Fernet.generate_key()
        self.key_path.write_bytes(self.key)
        self.events = []
        self.traces = {"synthetic-request": trace()}
        with sqlite3.connect(self.path) as db:
            audit.initialize(db)
            db.execute("CREATE TABLE route_traces(request_id TEXT PRIMARY KEY, started_at REAL, client_id TEXT, conversation_id TEXT, selected_model TEXT, status TEXT, payload_json TEXT)")
            for i in range(3):
                item = trace(request_id="page-" + str(i))
                db.execute("INSERT INTO route_traces VALUES(?,?,?,?,?,?,?)", (item["request_id"], item["started_at"], item["client_id"], item["conversation_id"], item["selected_model"], item["status"], json.dumps(item)))
        self.runtime = SimpleNamespace(
            start=mock.AsyncMock(), auth=AuthManager(None, None),
            route_traces=SimpleNamespace(database_path=self.path, get=mock.AsyncMock(side_effect=lambda key: self.traces.get(key))),
            audit=SimpleNamespace(write=lambda event, **fields: self.events.append((event, fields))),
        )
        self.env_patch = mock.patch.dict(os.environ, {"AI_ROUTER_ADMIN_KEY": "synthetic-admin-key", "AI_ROUTER_TRAINING_DB_PATH": str(self.archive_path), "AI_ROUTER_TRAINING_KEY_PATH": str(self.key_path)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.client = TestClient(create_app(self.runtime))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = {"Authorization": "Bearer synthetic-admin-key"}

    def write_archive(self):
        observer = ContentObservation()
        observer.capture("received", {"prompt": "synthetic-private-content-中文"})
        payload = {"pipeline": observer.archive()}
        index = hmac.new(base64.urlsafe_b64decode(self.key), b"1panel-ai-router-training-index-v1", hashlib.sha256).digest()
        request_hash = hmac.new(index, b"request:synthetic-request", hashlib.sha256).hexdigest()
        ciphertext = Fernet(self.key).encrypt(zlib.compress(json.dumps(payload).encode()))
        with sqlite3.connect(self.archive_path) as db:
            db.execute("CREATE TABLE training_records(request_hash TEXT PRIMARY KEY, payload_ciphertext BLOB)")
            db.execute("INSERT INTO training_records VALUES(?,?)", (request_hash, ciphertext))

    def test_new_endpoints_require_admin(self):
        for path in ("/api/cache/summary", "/api/cache/requests", "/api/route-traces/synthetic-request/content"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer synthetic-client-key"}).status_code, 401)
        self.assertEqual(self.events, [])

    def test_cache_requests_pagination_and_summary_are_consistent(self):
        params = {"since": 1, "until": 2000, "limit": 2}
        first = self.client.get("/api/cache/requests", headers=self.auth, params=params)
        self.assertEqual(first.status_code, 200)
        data = first.json()
        self.assertEqual((data["total"], len(data["items"]), data["next_offset"]), (3, 2, 2))
        second = self.client.get("/api/cache/requests", headers=self.auth, params={**params, "offset": 2}).json()
        self.assertEqual((len(second["items"]), second["next_offset"]), (1, None))
        ids = [x["request_id"] for x in data["items"] + second["items"]]
        self.assertEqual(len(set(ids)), 3)
        summary = self.client.get("/api/cache/summary", headers=self.auth, params={"since": 1, "until": 2000}).json()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["fixed_pass"], {"n": 0, "passed": 0})

    def test_new_pagination_limits_reject_invalid_values(self):
        for path, params in (("/api/cache/requests", {"offset": -1}), ("/api/cache/requests", {"limit": 101}), ("/api/route-traces/synthetic-request/content", {"offset": -1}), ("/api/route-traces/synthetic-request/content", {"limit": 65537})):
            with self.subTest(path=path, params=params):
                self.assertEqual(self.client.get(path, headers=self.auth, params=params).status_code, 422)

    def test_content_is_opt_in_json_no_store_and_audited(self):
        self.write_archive()
        path = "/api/route-traces/synthetic-request/content"
        metadata = self.client.get(path, headers=self.auth)
        self.assertEqual(metadata.status_code, 200)
        self.assertNotIn("synthetic-private-content", metadata.text)
        content = self.client.get(path, headers=self.auth, params={"stage": "received", "limit": 65536})
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.headers["cache-control"], "no-store")
        self.assertIn("application/json", content.headers["content-type"])
        self.assertIn("synthetic-private-content", content.json()["text"])
        self.assertEqual([x[0] for x in self.events], ["admin_content_viewed", "admin_content_viewed"])
        self.assertNotIn("synthetic-private-content", json.dumps(self.events))

    def test_content_missing_states_are_explicit(self):
        missing = self.client.get("/api/route-traces/missing/content", headers=self.auth)
        self.assertEqual(missing.status_code, 404)
        unavailable = self.client.get("/api/route-traces/synthetic-request/content", headers=self.auth)
        self.assertEqual(unavailable.status_code, 503)
        self.assertFalse(self.archive_path.exists())
        self.write_archive()
        missing_stage = self.client.get("/api/route-traces/synthetic-request/content", headers=self.auth, params={"stage": "never-recorded"})
        self.assertEqual(missing_stage.status_code, 404)


if __name__ == "__main__":
    unittest.main()
