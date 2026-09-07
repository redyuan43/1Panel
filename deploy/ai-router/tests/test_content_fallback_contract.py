"""Synthetic CPU checks for authenticated archive fallback and WAL sidecars."""
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

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

WORK = pathlib.Path(os.environ.get("CACHE_AUDIT_WORK", pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(WORK))
import ai_router.api as api
import ai_router.control as control
from ai_router.auth import AuthManager
from ai_router.content_audit import ArchiveReader, ContentObservation

SECRET_TEXT = "synthetic-private-archive-only-中文"
ADMIN = "Bearer synthetic-admin-key"


class ContentFallbackContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.archive_dir = self.root / "archive"
        self.archive_dir.mkdir()
        self.path = self.archive_dir / "synthetic.sqlite3"
        self.key_path = self.root / "synthetic.key"
        self.key = Fernet.generate_key()
        self.key_path.write_bytes(self.key)
        self.body = {"messages": [{"role": "user", "content": SECRET_TEXT}]}
        observation = ContentObservation()
        observation.capture("received", self.body)
        index = hmac.new(base64.urlsafe_b64decode(self.key), b"1panel-ai-router-training-index-v1", hashlib.sha256).digest()
        request_hash = hmac.new(index, b"request:synthetic-request", hashlib.sha256).hexdigest()
        encrypted = Fernet(self.key).encrypt(zlib.compress(json.dumps({"pipeline": observation.archive()}).encode()))
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE training_records(request_hash TEXT PRIMARY KEY,payload_ciphertext BLOB)")
        db.execute("INSERT INTO training_records VALUES(?,?)", (request_hash, encrypted))
        db.commit()
        db.close()
        self.events = []
        self.runtime = SimpleNamespace(
            start=mock.AsyncMock(), auth=AuthManager(None, None),
            route_traces=SimpleNamespace(get=mock.AsyncMock(side_effect=lambda key: {"request_id": key} if key == "synthetic-request" else None)),
            settings=SimpleNamespace(section=lambda key: {}), registry=SimpleNamespace(endpoints=[]),
            internal_client=SimpleNamespace(get=mock.AsyncMock(side_effect=AssertionError("unexpected network or model call"))),
            audit=SimpleNamespace(write=lambda event, **fields: self.events.append((event, fields))),
        )
        patcher = mock.patch.dict(os.environ, {"AI_ROUTER_ADMIN_KEY": "synthetic-admin-key", "AI_ROUTER_TRAINING_DB_PATH": str(self.path), "AI_ROUTER_TRAINING_KEY_PATH": str(self.key_path), "AI_ROUTER_TAILSCALE_IP": "100.64.0.99"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, module):
        value = TestClient(module.create_app(self.runtime), raise_server_exceptions=False)
        value.__enter__()
        self.addCleanup(value.__exit__, None, None, None)
        return value

    def get_internal(self, client, **kwargs):
        return client.get("/internal/request-content/synthetic-request", headers={"Authorization": ADMIN}, **kwargs)

    def get_control(self, client, **kwargs):
        return client.get("/api/route-traces/synthetic-request/content", headers={"Authorization": ADMIN}, **kwargs)

    def test_internal_endpoint_rejects_unauthorized_before_archive_or_trace(self):
        client = self.client(api)
        with mock.patch.object(api, "ArchiveReader") as reader:
            for authorization in (None, "Bearer synthetic-client-key", "Bearer wrong"):
                headers = {} if authorization is None else {"Authorization": authorization}
                response = client.get("/internal/request-content/synthetic-request", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(SECRET_TEXT, response.text)
            reader.assert_not_called()
        self.runtime.route_traces.get.assert_not_awaited()
        self.runtime.internal_client.get.assert_not_awaited()

    def test_internal_explicit_slice_is_admin_json_no_store_and_no_model_call(self):
        client = self.client(api)
        previous = hashlib.sha256(self.path.read_bytes()).hexdigest()
        response = self.get_internal(client, params={"stage": "received", "offset": 7, "limit": 31})
        self.assertEqual(response.status_code, 200)
        captured_body = json.loads(json.dumps(self.body, sort_keys=True))
        self.assertEqual(response.json()["text"], json.dumps(captured_body, ensure_ascii=False, indent=2)[7:38])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), previous)
        self.assertEqual([x[0] for x in self.events], ["admin_content_read_internal"])
        self.assertNotIn(SECRET_TEXT, json.dumps(self.events, ensure_ascii=False))
        self.runtime.internal_client.get.assert_not_awaited()

    def test_internal_metadata_omits_plaintext(self):
        response = self.get_internal(self.client(api))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("text", response.json())
        self.assertNotIn("bodies", response.json())
        self.assertNotIn(SECRET_TEXT, response.text)
        self.runtime.internal_client.get.assert_not_awaited()

    def test_internal_missing_request_and_stage_are_explicit(self):
        client = self.client(api)
        with mock.patch.object(api, "ArchiveReader") as reader:
            response = client.get("/internal/request-content/missing", headers={"Authorization": ADMIN})
            self.assertEqual(response.status_code, 404)
            reader.assert_not_called()
        response = self.get_internal(client, params={"stage": "missing"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET_TEXT, response.text)

    def test_internal_invalid_pagination_never_reads_archive(self):
        client = self.client(api)
        with mock.patch.object(api, "ArchiveReader") as reader:
            for params in ({"limit": 65537}, {"offset": -1}, {"limit": 0}):
                self.assertEqual(self.get_internal(client, params=params).status_code, 422)
            reader.assert_not_called()

    def test_local_read_success_never_enters_fallback(self):
        response = self.get_control(self.client(control), params={"stage": "received"})
        self.assertEqual(response.status_code, 200)
        self.runtime.internal_client.get.assert_not_awaited()

    def test_control_rejects_client_before_fallback(self):
        client = self.client(control)
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")) as reader:
            response = client.get("/api/route-traces/synthetic-request/content", headers={"Authorization": "Bearer synthetic-client-key"})
            self.assertEqual(response.status_code, 401)
            reader.assert_not_called()
        self.runtime.internal_client.get.assert_not_awaited()

    def test_wal_operational_error_falls_back_with_exact_auth_and_slice(self):
        value = {"request_id": "synthetic-request", "stage": "received", "text": SECRET_TEXT, "offset": 9, "next_offset": None}
        self.runtime.internal_client.get = mock.AsyncMock(return_value=httpx.Response(200, json=value))
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")):
            response = self.get_control(self.client(control), params={"stage": "received", "offset": 9, "limit": 33})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), value)
        call = self.runtime.internal_client.get.await_args
        self.assertEqual(call.args, ("http://127.0.0.1:4000/internal/request-content/synthetic-request",))
        self.assertEqual(call.kwargs["params"], {"stage": "received", "offset": 9, "limit": 33})
        self.assertEqual(call.kwargs["headers"], {"Authorization": ADMIN})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(SECRET_TEXT, json.dumps(self.events, ensure_ascii=False))

    def test_second_local_api_is_used_after_first_transport_failure(self):
        value = {"request_id": "synthetic-request", "stages": [], "checks": []}
        self.runtime.internal_client.get = mock.AsyncMock(side_effect=[httpx.ConnectError("synthetic unavailable"), httpx.Response(200, json=value)])
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")):
            response = self.get_control(self.client(control))
        self.assertEqual(response.status_code, 200)
        urls = [call.args[0] for call in self.runtime.internal_client.get.await_args_list]
        self.assertEqual(urls, ["http://127.0.0.1:4000/internal/request-content/synthetic-request", "http://100.64.0.99:4000/internal/request-content/synthetic-request"])

    def test_control_to_internal_api_fallback_end_to_end_in_memory(self):
        internal_app = api.create_app(self.runtime)
        internal_app.state.runtime = self.runtime
        transport = httpx.ASGITransport(app=internal_app)
        internal_client = httpx.AsyncClient(transport=transport)
        self.addCleanup(lambda: asyncio.run(internal_client.aclose()))
        self.runtime.internal_client = internal_client
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")):
            response = self.get_control(self.client(control), params={"stage": "received", "limit": 65536})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.json()["text"]), self.body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual([x[0] for x in self.events], ["admin_content_read_internal", "admin_content_viewed"])
        self.assertNotIn(SECRET_TEXT, json.dumps(self.events, ensure_ascii=False))

    def test_non_sqlite_read_failures_do_not_use_fallback(self):
        client = self.client(control)
        for error, status in ((KeyError("missing"), 404), (ValueError("synthetic invalid ciphertext"), 503), (FileNotFoundError("synthetic missing key"), 503)):
            with self.subTest(error=type(error).__name__), mock.patch.object(control, "ArchiveReader", side_effect=error):
                response = self.get_control(client)
                self.assertEqual(response.status_code, status)
        self.runtime.internal_client.get.assert_not_awaited()

    def test_both_api_failures_are_unavailable_without_payload_or_key_leak(self):
        self.runtime.internal_client.get = mock.AsyncMock(return_value=httpx.Response(503, text=SECRET_TEXT))
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")):
            response = self.get_control(self.client(control))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.runtime.internal_client.get.await_count, 2)
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertNotIn(ADMIN, response.text)
        self.assertEqual(self.events, [])

    def test_fallback_not_found_remains_not_found(self):
        self.runtime.internal_client.get = mock.AsyncMock(return_value=httpx.Response(404, text=SECRET_TEXT))
        with mock.patch.object(control, "ArchiveReader", side_effect=sqlite3.OperationalError("synthetic read-only WAL")):
            response = self.get_control(self.client(control), params={"stage": "received"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET_TEXT, response.text)
        self.assertEqual(self.runtime.internal_client.get.await_count, 1)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions are required to reproduce WAL sidecar failure")
    def test_wal_without_sidecars_needs_writable_directory_but_not_database_write(self):
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())
        self.assertFalse(self.path.with_name(self.path.name + "-shm").exists())
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.archive_dir.chmod(0o500)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ArchiveReader(self.path, self.key_path).content("synthetic-request", "received")
        finally:
            self.archive_dir.chmod(0o700)
        result = ArchiveReader(self.path, self.key_path).content("synthetic-request", "received")
        self.assertIn(SECRET_TEXT, result["text"])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
