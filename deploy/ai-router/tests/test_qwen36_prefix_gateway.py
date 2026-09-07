"""CPU-only protocol and persistence checks; never connects to model services."""
import concurrent.futures
import http.client
import importlib.util
import json
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "scripts/qwen36_prefix_gateway.py"
spec = importlib.util.spec_from_file_location("prefix_gateway", SOURCE)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond(200, b'{"status":"ok"}')

    def respond(self, status, data, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Backend-Proof", "unchanged")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        state = self.server.state
        state["calls"].append((self.path, raw, dict(self.headers)))
        body = json.loads(raw)
        if self.path == "/apply-template":
            rendered = state.get("rendered")
            if rendered is None:
                rendered = "".join("<|im_start|>" + m["role"] + "\n" + (m.get("content") or "") + "<|im_end|>\n" for m in body["messages"])
            return self.respond(200, gateway.encode({"prompt": rendered}))
        if self.path == "/tokenize":
            return self.respond(200, gateway.encode({"tokens": list(body["content"].encode())}))
        if self.path.startswith("/slots/"):
            action = self.path.split("action=")[-1]
            if state.get(action + "_status"):
                return self.respond(state[action + "_status"], b'{"error":"injected"}')
            if action == "save":
                for suffix in ("", ".draft", ".checkpoints"):
                    (state["data"] / (body["filename"] + suffix)).write_bytes(b"state" + suffix.encode())
            return self.respond(200, b'{"ok":true}')
        if self.path == "/completion":
            return self.respond(200, gateway.encode({"timings": {"prompt_n": len(body["prompt"])}}))
        with state["guard"]:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(state.get("delay", 0))
            self.respond(state.get("status", 200), state["response"], state.get("content_type", "application/json"))
        finally:
            with state["guard"]:
                state["active"] -= 1


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        secret = self.root / "key"
        secret.write_text("cpu-test-secret")
        self.state = {"calls": [], "guard": threading.Lock(), "active": 0, "peak": 0,
                      "response": b'{ "choices" : [], "usage":{"prompt_tokens":42} }\n'}
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        self.backend.state = self.state
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.addCleanup(self.backend.server_close)
        self.addCleanup(self.backend.shutdown)
        self.config = {"cache_directory": str(self.root / "cache"), "api_key_file": str(secret),
                       "backend_url": "http://127.0.0.1:" + str(self.backend.server_port),
                       "minimum_prefix_tokens": 1, "max_snapshots": 2, "max_disk_bytes": 10000,
                       "queue_timeout": 2, "backend_timeout": 2}
        self.cache = gateway.PrefixCache(self.config)
        self.state["data"] = self.cache.data
        self.cache.runtime = lambda: "verified-runtime"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        self.server.cache = self.cache
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def body(self, system="fixed prefix", user="unique current question"):
        return {"model": "qwen-test", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}

    def request(self, raw=None, path="/v1/chat/completions", authorization="Bearer cpu-test-secret", method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers={"Authorization": authorization, "Content-Type": "application/json", "X-Client-Proof": "preserved"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_json_raw_body_and_reply_unchanged(self):
        raw = json.dumps(self.body(), indent=2).encode() + b"\n"
        status, headers, payload = self.request(raw)
        self.assertEqual((status, payload), (200, self.state["response"]))
        forwarded = [call for call in self.state["calls"] if call[0] == "/v1/chat/completions"]
        self.assertEqual(forwarded[0][1], raw)
        self.assertEqual(forwarded[0][2]["X-Client-Proof"], "preserved")
        self.assertEqual(headers["X-Backend-Proof"], "unchanged")
        self.assertEqual(headers["X-Prefix-Cache"], "miss_saved")

    def test_sse_bytes_usage_reasoning_and_done_unchanged(self):
        self.state.update(content_type="text/event-stream", response=b': heartbeat\r\n\r\ndata: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\ndata: {"usage":{"prompt_tokens":7},"timings":{"prompt_n":3}}\n\ndata: [DONE]\n\n')
        body = self.body()
        body["stream"] = True
        status, headers, payload = self.request(gateway.encode(body))
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.state["response"])
        self.assertEqual(headers["Content-Type"], "text/event-stream")

    def test_sse_unusual_metadata_does_not_truncate_stream(self):
        self.state.update(content_type="text/event-stream", response=b'data: {"choices":null}\n\ndata: [DONE]\n\n')
        self.assertEqual(self.request(gateway.encode(self.body()))[2], self.state["response"])

    def test_http_error_transparent_and_invalidates_hot(self):
        self.state.update(status=422, response=b'{ "error": "model mismatch" }')
        status, _, payload = self.request(gateway.encode(self.body()))
        self.assertEqual((status, payload), (422, self.state["response"]))
        self.assertIsNone(self.cache.active)

    def test_auth_and_health(self):
        status, _, _ = self.request(gateway.encode(self.body()), authorization="Bearer wrong")
        self.assertEqual(status, 401)
        self.assertFalse(self.state["calls"])
        self.assertEqual(self.request(path="/health", method="GET", authorization="")[0], 200)
        self.assertEqual(self.request(path="/v1/models", method="GET", authorization="")[0], 401)

    def test_rejected_auth_closes_unread_post(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/chat/completions", body=b"unread", headers={"Authorization": "invalid"})
        response = conn.getresponse()
        response.read()
        self.assertTrue(response.will_close)

    def test_incomplete_body_and_ambiguous_framing_rejected(self):
        requests = [
            b"Content-Length: 99\r\n\r\n{}",
            b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        ]
        for suffix in requests:
            with self.subTest(suffix=suffix), socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
                sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer cpu-test-secret\r\n" + suffix)
                sock.shutdown(socket.SHUT_WR)
                response = http.client.HTTPResponse(sock)
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertTrue(response.will_close)
                response.read()
        self.assertFalse(self.state["calls"])

    def test_concurrent_generation_serialized(self):
        self.state["delay"] = 0.08
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.request(gateway.encode(self.body())), range(4)))
        self.assertTrue(all(result[0] == 200 for result in results))
        self.assertEqual(self.state["peak"], 1)
        self.assertEqual(sum(call[0] == "/completion" for call in self.state["calls"]), 1)

    def test_queue_timeout_does_not_invoke_backend(self):
        self.config["queue_timeout"] = 0.01
        with self.cache.lock:
            self.assertEqual(self.request(gateway.encode(self.body()))[0], 503)
        self.assertFalse(self.state["calls"])

    def test_hot_then_gateway_restart_uses_valid_disk(self):
        self.assertEqual(self.cache.prepare(self.body())["event"], "miss_saved")
        self.assertEqual(self.cache.prepare(self.body())["event"], "hot")
        restarted = gateway.PrefixCache(self.config)
        restarted.runtime = lambda: "verified-runtime"
        self.assertIsNone(restarted.active)
        self.assertEqual(restarted.prepare(self.body())["event"], "disk")

    def test_config_change_and_corrupt_checksum_reprime(self):
        self.cache.prepare(self.body())
        self.cache.active = None
        data = next(self.cache.data.glob("*.draft"))
        data.write_bytes(b"corruption")
        self.assertEqual(self.cache.prepare(self.body())["event"], "miss_saved")
        self.cache.active = None
        self.cache.runtime = lambda: "changed-runtime"
        self.assertEqual(self.cache.prepare(self.body())["event"], "miss_saved")

    def test_restore_rejection_primes_and_save_failure_keeps_memory(self):
        self.cache.prepare(self.body())
        self.cache.active = None
        self.state["restore_status"] = 400
        self.state["save_status"] = 500
        self.assertEqual(self.cache.prepare(self.body())["event"], "miss_memory_only")
        self.assertEqual(self.cache.prepare(self.body())["event"], "hot")

    def test_bypass_ambiguous_and_multimodal(self):
        cases = []
        ambiguous = self.body(system="unique current question")
        cases.append(ambiguous)
        multimodal = self.body()
        multimodal["messages"][-1]["content"] = [{"type": "text", "text": "question"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
        cases.append(multimodal)
        disabled = self.body()
        disabled["cache_prompt"] = False
        cases.append(disabled)
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.cache.prepare(body)["event"], "bypass")
        self.assertFalse(any(call[0] == "/completion" for call in self.state["calls"]))

    def test_snapshot_count_and_orphan_cleanup(self):
        for index in range(4):
            self.cache.prepare(self.body(system="fixed prefix " + str(index)))
        self.assertEqual(len(list(self.cache.manifests.glob("*.json"))), 2)
        self.assertEqual(len(list(self.cache.data.iterdir())), 6)
        orphan = self.cache.data / ("prefix-" + "a" * 32 + ".bin")
        orphan.write_bytes(b"unfinished")
        self.cache.prune(None)
        self.assertFalse(orphan.exists())

    def test_oversize_snapshot_not_retained(self):
        self.config["max_disk_bytes"] = 1
        self.cache.prepare(self.body())
        self.assertLessEqual(sum(path.stat().st_size for path in self.cache.data.iterdir()), 1)

    def test_disk_disabled_retains_memory_only(self):
        self.config["max_snapshots"] = 0
        self.assertEqual(self.cache.prepare(self.body())["event"], "miss_memory_only")
        self.assertFalse(list(self.cache.manifests.iterdir()))
        self.assertFalse(list(self.cache.data.iterdir()))

    def test_malformed_manifest_recomputed_and_unmanaged_files_preserved(self):
        result = self.cache.prepare(self.body())
        manifest = self.cache.manifests / (result["prefix_sha256"] + ".json")
        for invalid in ([], {"files": None}, {"version": 1, "runtime": "verified-runtime", "prefix_sha256": result["prefix_sha256"], "prefix_tokens": result["fixed_tokens"], "files": [None, None, None]}):
            manifest.write_text(json.dumps(invalid))
            self.cache.active = None
            self.assertEqual(self.cache.prepare(self.body())["event"], "miss_saved")
        unmanaged = self.cache.data / "operator-file.txt"
        unmanaged.write_text("keep")
        malformed = self.cache.manifests / "corrupt.json"
        malformed.write_text("[]")
        self.cache.prune(None)
        self.assertTrue(unmanaged.exists())
        self.assertFalse(malformed.exists())

    def test_integrity_failure_fails_closed(self):
        self.cache.runtime = mock.Mock(side_effect=RuntimeError("unvalidated_backend_library"))
        self.assertEqual(self.request(gateway.encode(self.body()))[0], 502)
        self.assertFalse(self.state["calls"])


class RuntimeTests(unittest.TestCase):
    def test_artifacts_environment_process_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            key = root / "key"
            key.write_text("cpu-only")
            impl = root / "libserver.so"
            impl.write_bytes(b"validated-library")
            model = root / "model.gguf"
            model.write_bytes(b"test-model")
            config = {"cache_directory": str(root / "cache"), "api_key_file": str(key),
                      "runtime_files": {"server_impl": str(impl), "model": str(model)},
                      "expected_server_impl_sha256": gateway.file_digest(impl), "backend_unit": "cpu-test"}
            cache = gateway.PrefixCache(config)
            for pid in ("123", "456"):
                proc = root / pid
                proc.mkdir()
                (proc / "cmdline").write_bytes(b"server\0--model\0test\0")
                (proc / "environ").write_bytes(b"LLAMA_LAB_KEEP_USER_CHECKPOINT=1\0LLAMA_LAB_DISK_CHECKPOINTS=1\0")
                (proc / "maps").write_text(str(impl))
            real_path = pathlib.Path
            def redirected(value):
                return root if value == "/proc" else real_path(value)
            with mock.patch.object(gateway, "Path", side_effect=redirected), mock.patch.object(gateway.subprocess, "check_output", return_value="123\n") as get_pid:
                fingerprint = cache.runtime()
                cache.active = ("prefix", fingerprint)
                self.assertEqual(cache.runtime(), fingerprint)
                self.assertIsNotNone(cache.active)
                get_pid.return_value = "456\n"
                self.assertEqual(cache.runtime(), fingerprint)
                self.assertIsNone(cache.active)
                cache.active = ("prefix", fingerprint)
                model.write_bytes(b"changed-model")
                self.assertNotEqual(cache.runtime(), fingerprint)
                self.assertIsNone(cache.active)
                (root / "456" / "environ").write_bytes(b"LLAMA_LAB_KEEP_USER_CHECKPOINT=1\0")
                with self.assertRaisesRegex(RuntimeError, "backend_cache_features_disabled"):
                    cache.runtime()
                (root / "456" / "environ").write_bytes(b"LLAMA_LAB_KEEP_USER_CHECKPOINT=1\0LLAMA_LAB_DISK_CHECKPOINTS=1\0")
                (root / "456" / "maps").write_text("other-library.so")
                with self.assertRaisesRegex(RuntimeError, "backend_loaded_library_mismatch"):
                    cache.runtime()
                (root / "456" / "maps").write_text(str(impl))
                impl.write_bytes(b"unvalidated-library")
                with self.assertRaisesRegex(RuntimeError, "unvalidated_backend_library"):
                    cache.runtime()


if __name__ == "__main__":
    unittest.main()
