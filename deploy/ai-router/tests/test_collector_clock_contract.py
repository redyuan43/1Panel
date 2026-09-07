"""CPU-only collector reconciliation and event timing tests using fake clients."""
import asyncio
import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_cache_audit_contract import audit, operation


class CollectorContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = []
        self.events = []
        self.calls = []
        self.responses = []

        async def get(url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

        async def save(value):
            self.saved.append(value)

        self.runtime = SimpleNamespace(
            internal_client=SimpleNamespace(get=get),
            route_traces=SimpleNamespace(database_path="unused-synthetic-path"),
            audit=SimpleNamespace(write=lambda *args, **kwargs: self.events.append((args, kwargs))),
        )
        self.collector = audit.TelemetryCollector(self.runtime)
        patcher = mock.patch.object(audit, "CacheAudit", return_value=SimpleNamespace(save_operation=save))
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, value=None, status=200):
        return SimpleNamespace(status_code=status, json=lambda: value)

    async def collect(self):
        await self.collector._collect("http://synthetic.invalid", "synthetic-key", "synthetic-request", "synthetic-operation", 1, "synthetic-deployment", "foreground")

    async def test_exact_identity_is_required_before_persistence(self):
        for field, wrong in (("request_id", "other"), ("operation_id", "other"), ("attempt", 2), ("kind", "prewarm")):
            with self.subTest(field=field):
                self.responses = [self.response(operation(**{field: wrong}))]
                await self.collect()
        self.assertEqual(self.saved, [])

    async def test_terminal_record_is_saved_once_and_polling_stops(self):
        self.responses = [self.response(operation())]
        await self.collect()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["deployment_id"], "synthetic-deployment")

    async def test_active_then_terminal_updates_keep_same_identity(self):
        self.responses = [self.response(operation(terminal=False)), self.response(operation())]
        with mock.patch.object(audit.asyncio, "sleep", new=mock.AsyncMock()):
            await self.collect()
        self.assertEqual([x["terminal"] for x in self.saved], [False, True])
        self.assertEqual({x["operation_id"] for x in self.saved}, {"synthetic-operation"})

    async def test_repeated_missing_record_has_bounded_polling(self):
        self.responses = [self.response(status=404)] * 8
        with mock.patch.object(audit.asyncio, "sleep", new=mock.AsyncMock()):
            await self.collect()
        self.assertLessEqual(len(self.calls), 4)
        self.assertEqual(self.saved, [])

    async def test_shutdown_cancels_pending_io_and_releases_tasks(self):
        entered = asyncio.Event()
        async def blocking_get(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.runtime.internal_client.get = blocking_get
        self.collector.collect(base_url="http://synthetic.invalid/v1", key="synthetic-key", request_id="synthetic-request", operation_id="synthetic-operation", attempt=1, deployment_id="synthetic-deployment")
        await entered.wait()
        await self.collector.close()
        self.assertFalse(self.collector.tasks)


class OutputClockContractTests(unittest.TestCase):
    def test_role_heartbeat_and_empty_deltas_do_not_start_clock(self):
        clock = audit.OutputClock("chat", time.monotonic())
        clock.feed(b': keepalive\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: {"choices":[{"delta":{"content":""}}]}\n\n')
        self.assertEqual(clock.values, {})

    def test_reasoning_precedes_first_visible_text(self):
        clock = audit.OutputClock("chat", 100)
        with mock.patch.object(audit.time, "monotonic", return_value=101):
            clock.feed(b'data: {"choices":[{"delta":{"reasoning_content":"fixture"}}]}\n\n')
        with mock.patch.object(audit.time, "monotonic", return_value=103):
            clock.feed(b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n')
        self.assertEqual(clock.values, {"ttft_ms": 1000, "first_text_ms": 3000})

    def test_chunked_utf8_crlf_event_is_observed_once(self):
        clock = audit.OutputClock("chat", 100)
        frame = 'data: {"choices":[{"delta":{"content":"中"}}]}\r\n\r\n'.encode()
        with mock.patch.object(audit.time, "monotonic", return_value=102):
            for byte in frame:
                clock.feed(bytes([byte]))
        self.assertEqual(clock.values, {"ttft_ms": 2000, "first_text_ms": 2000})

    def test_responses_tool_output_counts_as_first_output_not_text(self):
        clock = audit.OutputClock("responses", 100)
        with mock.patch.object(audit.time, "monotonic", return_value=101):
            clock.feed(b'data: {"type":"response.function_call_arguments.delta","delta":"{}"}\n\n')
        self.assertEqual(clock.values, {"ttft_ms": 1000})

    def test_unusual_sse_values_do_not_break_later_output(self):
        clock = audit.OutputClock("chat", 100)
        with mock.patch.object(audit.time, "monotonic", return_value=101):
            clock.feed(b'data: {"choices":null}\n\ndata: null\n\ndata: [DONE]\n\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
        self.assertEqual(clock.values, {"ttft_ms": 1000, "first_text_ms": 1000})


if __name__ == "__main__":
    unittest.main()
