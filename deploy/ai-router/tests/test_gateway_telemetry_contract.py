"""Independent ring and real local-HTTP telemetry checks against fake backend."""
import copy
import http.client
import importlib.util
import json
import os
import pathlib
import time
import unittest
from unittest import mock

WORK = pathlib.Path(os.environ.get("CACHE_AUDIT_WORK", pathlib.Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location("gateway_fixture", WORK / "tests/test_qwen36_prefix_gateway.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
gateway = fixture.gateway


def event(index=1, **fields):
    return {"operation_id": format(index, "032x"), "request_id": "synthetic-request", "attempt": 1,
            "kind": "foreground", "terminal": False, **fields}


class RingContractTests(unittest.TestCase):
    def test_count_bound_evicts_oldest_operation(self):
        ring = gateway.TelemetryRing(max_records=2)
        for i in (1, 2, 3):
            ring.put(event(i))
        self.assertIsNone(ring.get(event(1)["operation_id"]))
        self.assertIsNotNone(ring.get(event(2)["operation_id"]))
        self.assertIsNotNone(ring.get(event(3)["operation_id"]))

    def test_expired_operation_is_not_returned(self):
        with mock.patch.object(gateway.time, "monotonic", return_value=100):
            ring = gateway.TelemetryRing(ttl=2)
            ring.put(event())
        with mock.patch.object(gateway.time, "monotonic", return_value=103):
            self.assertIsNone(ring.get(event()["operation_id"]))

    def test_byte_bound_does_not_retain_oversized_payload(self):
        ring = gateway.TelemetryRing(max_bytes=512)
        ring.put(event(padding="x" * 1024))
        self.assertIsNone(ring.get(event()["operation_id"]))

    def test_put_and_get_are_defensive_snapshots(self):
        ring = gateway.TelemetryRing()
        value = event(cache={"prime_tokens": 7})
        ring.put(value)
        value["cache"]["prime_tokens"] = 99
        saved = ring.get(value["operation_id"])
        self.assertEqual(saved["cache"]["prime_tokens"], 7)
        saved["cache"]["prime_tokens"] = 42
        self.assertEqual(ring.get(value["operation_id"])["cache"]["prime_tokens"], 7)

    def test_repeated_stage_updates_consume_one_record(self):
        ring = gateway.TelemetryRing(max_records=1)
        ring.put(event())
        ring.put(event(terminal=True))
        self.assertTrue(ring.get(event()["operation_id"])["terminal"])


class GatewayContractTests(unittest.TestCase):
    setUp = fixture.CacheTests.setUp
    body = fixture.CacheTests.body

    def request(self, raw=None, path="/v1/chat/completions", method="POST", authorization="Bearer cpu-test-secret", attempt=2, operation_id="0123456789abcdef0123456789abcdef"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers={
                "Authorization": authorization, "Content-Type": "application/json",
                "X-Request-ID": "synthetic-request", "X-1Panel-Attempt": str(attempt),
                "X-1Panel-Operation-ID": operation_id,
            })
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def telemetry(self):
        status, headers, raw = self.request(path="/cache/telemetry/0123456789abcdef0123456789abcdef", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return json.loads(raw)

    def test_json_metadata_is_correlated_and_response_is_unchanged(self):
        response = b'{"choices":[],"usage":{"prompt_tokens":120},"timings":{"cache_n":100,"prompt_n":20}}'
        self.state["response"] = response
        status, headers, raw = self.request(gateway.encode(self.body()))
        self.assertEqual((status, raw), (200, response))
        self.assertEqual(headers["X-Prefix-Telemetry"], "1")
        value = self.telemetry()
        self.assertEqual((value["request_id"], value["attempt"], value["operation_id"]),
                         ("synthetic-request", 2, "0123456789abcdef0123456789abcdef"))
        self.assertTrue(value["terminal"])
        self.assertEqual(value["timings"]["prompt_n"], 20)
        self.assertEqual(value["total_prefill_tokens"], value["cache"]["prime_tokens"] + 20)
        self.assertNotIn("fixed prefix", json.dumps(value))
        self.assertNotIn("cpu-test-secret", json.dumps(value))

    def test_sse_metadata_preserves_reasoning_and_done_bytes(self):
        response = b': heartbeat\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: {"choices":[{"delta":{"reasoning_content":"synthetic-reasoning"}}]}\n\ndata: {"choices":[{"delta":{"content":"synthetic-output"}}]}\n\ndata: {"timings":{"cache_n":100,"prompt_n":20}}\n\ndata: [DONE]\n\n'
        self.state.update(response=response, content_type="text/event-stream")
        status, _, raw = self.request(gateway.encode(self.body()))
        self.assertEqual((status, raw), (200, response))
        value = self.telemetry()
        self.assertEqual(value["timings"]["cache_n"], 100)
        self.assertGreaterEqual(value["first_text_ms"], value["ttft_ms"])
        self.assertNotIn("synthetic-reasoning", json.dumps(value))
        self.assertNotIn("synthetic-output", json.dumps(value))

    def test_missing_formal_timing_remains_unavailable(self):
        self.request(gateway.encode(self.body()))
        self.assertIsNone(self.telemetry()["total_prefill_tokens"])

    def test_prewarm_is_separate_kind_and_terminal(self):
        status, _, _ = self.request(gateway.encode(self.body()), path="/cache/prepare")
        self.assertEqual(status, 200)
        value = self.telemetry()
        self.assertEqual(value["kind"], "prewarm")
        self.assertTrue(value["terminal"])
        self.assertFalse(any(path == "/v1/chat/completions" for path, _, _ in self.state["calls"]))

    def test_queue_timeout_is_terminal_failure_without_backend_call(self):
        self.config["queue_timeout"] = .01
        with self.cache.lock:
            status, _, _ = self.request(gateway.encode(self.body()))
        self.assertEqual(status, 503)
        value = self.telemetry()
        self.assertTrue(value["terminal"])
        self.assertEqual(value["status"], "failed")
        self.assertEqual(self.state["calls"], [])

    def test_telemetry_endpoint_requires_auth_and_does_not_call_backend(self):
        status, _, _ = self.request(path="/cache/telemetry/0123456789abcdef0123456789abcdef", method="GET", authorization="")
        self.assertEqual(status, 401)
        self.assertEqual(self.state["calls"], [])

    def test_malformed_native_counter_cannot_leak_worker_lock(self):
        original = self.cache.post
        def malformed_counter(path, body):
            result = original(path, body)
            if path == "/completion":
                result["timings"]["prompt_n"] = "unknown"
            return result
        self.cache.post = malformed_counter
        self.state["response"] = b'{"choices":[],"timings":{"prompt_n":20,"cache_n":100}}'
        self.request(gateway.encode(self.body()))
        self.assertFalse(self.cache.lock.locked(), "malformed audit counter must not retain shared inference lock")
        value = self.telemetry()
        self.assertIsNone(value["total_prefill_tokens"])

    def test_telemetry_failure_cannot_leak_worker_lock(self):
        count = 0
        original = self.cache.telemetry.put
        def failed_publish(value):
            nonlocal count
            count += 1
            if count > 1:
                raise OSError("synthetic telemetry failure")
            original(value)
        self.cache.telemetry.put = failed_publish
        try:
            self.request(gateway.encode(self.body()))
        except (http.client.RemoteDisconnected, ConnectionResetError):
            pass
        self.assertFalse(self.cache.lock.locked(), "telemetry publication must not retain shared inference lock")


if __name__ == "__main__":
    unittest.main()
