"""Independent synthetic CPU checks. Never contacts a model or reads live data."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest


WORK = pathlib.Path(os.environ.get("CACHE_AUDIT_WORK", pathlib.Path(__file__).resolve().parents[1]))
SPEC = importlib.util.spec_from_file_location("independent_cache_audit", WORK / "ai_router/cache_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def trace(request_id="synthetic-request", attempts=1, **updates):
    value = {
        "request_id": request_id,
        "started_at": 1000,
        "completed_at": 1002,
        "status": "succeeded",
        "selected_model": "synthetic/model",
        "endpoint_id": "synthetic-endpoint",
        "deployment_id": "synthetic-deployment",
        "client_id": "synthetic-client",
        "conversation_id": "synthetic-conversation",
        "attempts": [{"number": i, "steps": []} for i in range(1, attempts + 1)],
    }
    value.update(updates)
    return value


def operation(request_id="synthetic-request", attempt=1, operation_id="synthetic-operation", **updates):
    value = {
        "request_id": request_id,
        "operation_id": operation_id,
        "attempt": attempt,
        "kind": "foreground",
        "terminal": True,
        "status_code": 200,
        "deployment_id": "synthetic-deployment",
        "cache": {"event": "hot", "fixed_tokens": 100, "prime_tokens": 0},
        "timings": {"prompt_n": 20, "cache_n": 100},
    }
    value.update(updates)
    return value


class MetricContractTests(unittest.TestCase):
    def test_cold_preparation_is_charged_before_formal_prompt(self):
        value = operation(cache={"event": "miss_saved", "fixed_tokens": 100, "prime_tokens": 100})
        row = audit.metrics(trace(), [value])
        self.assertEqual(row["total_prefill_tokens"], 120)
        self.assertEqual(row["fixed_reuse_ratio"], 0)
        self.assertEqual(row["net_cache_ratio"], 0)
        self.assertEqual(row["input_tokens"], 120)

    def test_fixed_reuse_formula_boundary_matrix(self):
        for fixed, cached, prime, expected in [
            (100, 100, 0, 1), (100, 90, 0, .9),
            (100, 140, 20, .8), (100, 60, 20, .4),
            (100, 30, 50, 0), (100, 100, 150, 0),
            (100, 0, 0, 0),
        ]:
            with self.subTest(fixed=fixed, cached=cached, prime=prime):
                value = operation(cache={"fixed_tokens": fixed, "prime_tokens": prime},
                                  timings={"prompt_n": 25, "cache_n": cached})
                row = audit.metrics(trace(), [value])
                self.assertAlmostEqual(row["fixed_reuse_ratio"], expected)
                self.assertEqual(row["total_prefill_tokens"], prime + 25)

    def test_zero_fixed_denominator_is_unavailable(self):
        row = audit.metrics(trace(), [operation(cache={"fixed_tokens": 0, "prime_tokens": 0})])
        self.assertIsNone(row["fixed_reuse_ratio"])

    def test_missing_counters_do_not_become_zero(self):
        for group, key, unavailable in [
            ("cache", "prime_tokens", ("prime_tokens", "fixed_reuse_ratio", "total_prefill_tokens")),
            ("cache", "fixed_tokens", ("fixed_tokens", "fixed_reuse_ratio")),
            ("timings", "prompt_n", ("prompt_tokens", "total_prefill_tokens")),
            ("timings", "cache_n", ("cached_tokens", "fixed_reuse_ratio")),
        ]:
            with self.subTest(group=group, key=key):
                value = operation()
                del value[group][key]
                row = audit.metrics(trace(), [value])
                for field in unavailable:
                    self.assertIsNone(row[field], field)

    def test_measured_zero_counters_are_preserved(self):
        value = operation(cache={"fixed_tokens": 100, "prime_tokens": 0},
                          timings={"prompt_n": 0, "cache_n": 0})
        row = audit.metrics(trace(), [value])
        for field in ("prime_tokens", "prompt_tokens", "cached_tokens", "total_prefill_tokens", "fixed_reuse_ratio"):
            self.assertEqual(row[field], 0, field)

    def test_untrusted_numeric_values_are_unavailable(self):
        for value in (None, True, False, -1, float("nan"), float("inf"), "7", [], {}):
            with self.subTest(value=repr(value)):
                self.assertIsNone(audit.number(value))

    def test_hot_label_without_measurements_is_not_a_pass(self):
        row = audit.metrics(trace(), [operation(timings={})])
        self.assertIsNone(row["fixed_reuse_ratio"])
        self.assertEqual(row["measurement"], "unavailable")

    def test_explicit_zero_queue_is_preserved(self):
        item = trace(observation={"queue_wait_ms": 0})
        self.assertEqual(audit.metrics(item, []) ["queue_ms"], 0)

    def test_zero_capacity_wait_evidence_is_preserved(self):
        item = trace()
        item["attempts"][0]["steps"].append({"node_id": "capacity_wait", "evidence": {"queue_wait_ms": 0}})
        self.assertEqual(audit.metrics(item, []) ["queue_ms"], 0)

    def test_missing_queue_is_unavailable(self):
        self.assertIsNone(audit.metrics(trace(), []) ["queue_ms"])

    def test_retry_uses_final_attempt_not_arrival_order(self):
        earlier = operation(attempt=1, operation_id="first", cache={"fixed_tokens": 100, "prime_tokens": 100})
        final = operation(attempt=2, operation_id="second")
        for values in ([earlier, final], [final, earlier]):
            row = audit.metrics(trace(attempts=2), values)
            self.assertEqual(row["fixed_reuse_ratio"], 1)
            self.assertEqual(row["attempts"], 2)

    def test_missing_final_retry_cannot_borrow_previous_attempt(self):
        row = audit.metrics(trace(attempts=2), [operation(attempt=1)])
        for field in ("cached_tokens", "prime_tokens", "total_prefill_tokens", "fixed_reuse_ratio"):
            self.assertIsNone(row[field], field)

    def test_background_prewarm_never_replaces_foreground(self):
        foreground = operation()
        background = operation(attempt=99, operation_id="warm", kind="prewarm",
                               cache={"fixed_tokens": 900, "prime_tokens": 900})
        row = audit.metrics(trace(), [background, foreground])
        self.assertEqual(row["total_prefill_tokens"], 20)
        self.assertEqual(row["fixed_reuse_ratio"], 1)
        self.assertEqual(row["background_operations"], 1)

    def test_background_only_does_not_prove_request_reuse(self):
        row = audit.metrics(trace(), [operation(kind="prewarm")])
        self.assertIsNone(row["total_prefill_tokens"])
        self.assertIsNone(row["fixed_reuse_ratio"])

    def test_summary_excludes_unknown_and_failed_from_pass_denominator(self):
        rows = [
            audit.metrics(trace(request_id="known"), [operation(request_id="known")]),
            audit.metrics(trace(request_id="unknown"), []),
            audit.metrics(trace(request_id="failed", status="failed"), [operation(request_id="failed")]),
        ]
        value = audit.CacheAudit.summarize({"items": rows, "total": 3, "truncated": False, "since": 0, "until": 2000})
        self.assertEqual(value["total"], 3)
        self.assertEqual(value["succeeded"], 2)
        self.assertEqual(value["fixed_pass"], {"n": 1, "passed": 1})

    def test_summary_zero_is_a_measured_failure(self):
        row = audit.metrics(trace(), [operation(cache={"fixed_tokens": 100, "prime_tokens": 100})])
        value = audit.CacheAudit.summarize({"items": [row], "total": 1, "truncated": False, "since": 0, "until": 2000})
        self.assertEqual(value["fixed_pass"], {"n": 1, "passed": 0})
        self.assertEqual(value["metrics"]["fixed_reuse_ratio"], {"n": 1, "median": 0, "p95": 0})


class PersistenceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "synthetic.sqlite3"
        with sqlite3.connect(self.path) as db:
            audit.initialize(db)
            db.execute("CREATE TABLE route_traces(request_id TEXT PRIMARY KEY, started_at REAL, client_id TEXT, conversation_id TEXT, selected_model TEXT, status TEXT, payload_json TEXT)")
        self.store = audit.CacheAudit(self.path)

    def save_trace(self, item):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO route_traces VALUES(?,?,?,?,?,?,?)", (
                item["request_id"], item["started_at"], item["client_id"], item["conversation_id"],
                item["selected_model"], item["status"], json.dumps(item)))

    def test_repeated_operation_polls_are_idempotent(self):
        value = operation()
        asyncio.run(self.store.save_operation(value))
        asyncio.run(self.store.save_operation(value))
        result = asyncio.run(self.store.detail(trace()))
        self.assertEqual(len(result["operations"]), 1)

    def test_operation_id_collision_cannot_reassign_request(self):
        original = operation()
        foreign = operation(request_id="foreign-request", cache={"fixed_tokens": 999, "prime_tokens": 999})
        asyncio.run(self.store.save_operation(original))
        try:
            asyncio.run(self.store.save_operation(foreign))
        except (ValueError, sqlite3.IntegrityError):
            pass
        result = asyncio.run(self.store.detail(trace()))
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(result["operations"][0]["request_id"], original["request_id"])
        self.assertEqual(result["request"]["fixed_tokens"], 100)
        self.assertEqual(asyncio.run(self.store.detail(trace(request_id="foreign-request")))["operations"], [])

    def test_request_rows_do_not_double_count_retry_and_prewarm(self):
        item = trace(attempts=2)
        self.save_trace(item)
        for value in (operation(operation_id="one"), operation(operation_id="two", attempt=2), operation(operation_id="warm", kind="prewarm")):
            asyncio.run(self.store.save_operation(value))
        result = asyncio.run(self.store.query(since=1, until=2000))
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)

    def test_summary_filters_apply_before_measurements(self):
        self.save_trace(trace(request_id="one", client_id="first"))
        self.save_trace(trace(request_id="two", client_id="second"))
        result = asyncio.run(self.store.query(since=1, until=2000, client_id="first"))
        self.assertEqual([row["request_id"] for row in result["items"]], ["one"])

    def test_workbuddy_group_includes_public_and_pinned_accounts(self):
        self.save_trace(trace(request_id="public", client_id="workbuddy-public"))
        self.save_trace(trace(request_id="pinned", client_id="workbuddy-qwen36-shared"))
        self.save_trace(trace(request_id="other", client_id="another-client"))
        result = asyncio.run(self.store.query(since=1, until=2000, client_group="workbuddy"))
        self.assertEqual({row["request_id"] for row in result["items"]}, {"public", "pinned"})
        selected = asyncio.run(self.store.query(since=1, until=2000, client_group="workbuddy", client_id="another-client"))
        self.assertEqual([row["request_id"] for row in selected["items"]], ["other"])

    def test_provider_usage_does_not_invent_gateway_prefill(self):
        value = trace()
        value["attempts"][0]["steps"] = [{"node_id": "upstream_request", "evidence": {
            "input_tokens": 100, "cached_prompt_tokens": 80, "cache_measurement_source": "upstream_usage"}}]
        row = audit.metrics(value, [])
        self.assertEqual(row["cache_source"], "upstream_usage")
        self.assertEqual(row["reported_cache_ratio"], .8)
        self.assertIsNone(row["total_prefill_tokens"])
        self.assertIsNone(row["net_cache_ratio"])


if __name__ == "__main__":
    unittest.main()
