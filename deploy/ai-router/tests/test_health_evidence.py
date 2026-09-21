"""Synthetic probe failures and persisted evidence; no live network or model calls."""
import asyncio
import json
import socket
import ssl
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ai_router.audit import AuditLog
from ai_router.health import HealthMonitor
from ai_router.health_evidence import HealthAuditWriter, health_snapshot, probe_failure
from ai_router.policy import RoutingPolicy
from ai_router.route_trace import DecisionTrace, RouteTraceStore
from ai_router.store import InMemoryStateStore
from ai_router.types import Endpoint, EndpointStatus, Evaluation, RequestCapabilities


def endpoint():
    return Endpoint(
        id="synthetic-cloud", public_model="synthetic/model", provider_model="synthetic/model",
        api_base="https://probe.invalid/v1", node="synthetic", role="responder", tier="cloud",
        tier_rank=1, modalities=("text",), tasks=("general",), safe_context_tokens=8192,
        configured_context_tokens=8192, max_concurrency=8, backend_type="openai",
        health_url="https://probe.invalid/v1/models", cloud=True,
    )


@pytest.mark.parametrize("error,category,phase", [
    (httpx.PoolTimeout, "pool_timeout", "pool"),
    (httpx.ConnectTimeout, "connect_timeout", "connect"),
    (httpx.ReadTimeout, "read_timeout", "read"),
    (httpx.WriteTimeout, "write_timeout", "write"),
    (httpx.ConnectError, "connect_error", "connect"),
    (httpx.ProxyError, "proxy_error", "proxy"),
    (httpx.RemoteProtocolError, "protocol_error", "protocol"),
    (ValueError, "unknown", "unknown"),
])
def test_exception_classification_never_copies_message(error, category, phase):
    failure = probe_failure(error=error("Bearer synthetic-secret https://private.invalid/?key=secret"))
    assert failure["category"] == category
    assert failure["phase"] == phase
    assert failure["exception_type"] == error.__name__
    assert "secret" not in json.dumps(failure)
    assert "private.invalid" not in json.dumps(failure)


@pytest.mark.parametrize("cause,category", [
    (socket.gaierror("private DNS detail"), "dns_error"),
    (ssl.SSLCertVerificationError("private TLS detail"), "tls_error"),
])
def test_transport_wrapper_preserves_dns_and_tls_cause(cause, category):
    outer = httpx.ConnectError("private wrapper detail")
    outer.__cause__ = cause
    failure = probe_failure(error=outer)
    assert failure["category"] == category
    assert failure["cause_types"] == [type(cause).__name__]
    assert "private" not in json.dumps(failure)


@pytest.mark.parametrize("code,category", [
    (401, "authentication"), (403, "authentication"), (429, "rate_limit"),
    (503, "server_error"), (404, "http_error"),
])
def test_http_failures_are_durable_and_recovery_is_linked(tmp_path, code, category):
    async def exercise():
        calls = []
        responses = iter([code, 200, 200])

        def handle(request):
            calls.append(request)
            return httpx.Response(next(responses), text="private upstream body")

        audit = AuditLog(tmp_path / "audit.jsonl")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monitor = HealthMonitor(InMemoryStateStore(), client=client, audit=audit,
                                    instance_id="instance-a", boot_id="boot-a")
            failed = await monitor.status(endpoint(), force_refresh=True)
            cached = await monitor.status(endpoint())
            assert not failed.healthy
            assert cached.detail["probe"]["probe_id"] == failed.detail["probe"]["probe_id"]
            assert len(calls) == 1, "No retry is added and cached reads do not probe again"
            healthy = await monitor.status(endpoint(), force_refresh=True)
            await monitor.status(endpoint(), force_refresh=True)
            assert healthy.healthy
            await monitor.close()
        events = [json.loads(line) for line in audit.path.read_text().splitlines()]
        assert [event["event"] for event in events] == ["health_probe_failed", "health_probe_recovered"]
        assert events[0]["failure"]["category"] == category
        assert events[0]["failure"]["http_status"] == code
        assert events[0]["probe_id"] == failed.detail["probe"]["probe_id"]
        assert events[1]["previous_failure_id"] == events[0]["probe_id"]
        assert events[1]["probe_id"] != events[0]["probe_id"]
        assert events[0]["instance_id"] == "instance-a"
        assert events[0]["boot_id"] == "boot-a"
        assert events[0]["elapsed_ms"] >= 0
        assert events[0]["completed_at"] >= events[0]["checked_at"]
        assert "private upstream" not in audit.path.read_text()
    asyncio.run(exercise())


def test_probe_exception_and_audit_failure_do_not_change_health_decision(tmp_path):
    async def exercise():
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("private credential", request=request)
            return httpx.Response(200)
        audit = Mock()
        audit.write.side_effect = OSError("private storage path")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            monitor = HealthMonitor(InMemoryStateStore(), client=client, audit=audit)
            failure = await monitor.status(endpoint(), force_refresh=True)
            assert not failure.healthy
            assert failure.load_headroom == 0
            assert failure.detail["probe"]["failure"]["category"] == "read_timeout"
            recovered = await monitor.status(endpoint(), force_refresh=True)
            assert recovered.healthy
            assert len(calls) == 2
            await monitor.close()
            assert audit.write.call_count == 2
    asyncio.run(exercise())


def test_blocked_audit_does_not_block_probes_and_backlog_is_bounded():
    async def exercise():
        entered, release = threading.Event(), threading.Event()
        events = []
        def write(event, **fields):
            entered.set()
            release.wait(5)
            events.append(event)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
            monitor = HealthMonitor(InMemoryStateStore(), client=client, audit=SimpleNamespace(write=write))
            monitor._audit_writer = HealthAuditWriter(capacity=2)
            try:
                failed = await asyncio.wait_for(monitor.status(endpoint(), force_refresh=True), 0.5)
                assert not failed.healthy
                assert await asyncio.to_thread(entered.wait, 1)
                for _ in range(10):
                    assert not (await asyncio.wait_for(monitor.status(endpoint(), force_refresh=True), 0.5)).healthy
                assert monitor._audit_writer.pending.qsize() == 2
                assert monitor._audit_writer.dropped == 8
                await asyncio.wait_for(monitor._audit_writer.close(timeout=0.01), 0.5)
                assert monitor._audit_writer.thread.is_alive()
            finally:
                release.set()
                await monitor.close()
            assert len(events) == 3
            assert not monitor._audit_writer.thread.is_alive()
    asyncio.run(exercise())


def test_expired_success_is_not_reported_as_failed_probe():
    status = EndpointStatus("synthetic", True, 100, detail={"probe": {"probe_id": "ok-probe"}})
    snapshot = health_snapshot(status, 120, 15)
    assert snapshot["healthy"] is True
    assert snapshot["stale"] is True
    assert snapshot["failure"] is None
    assert snapshot["age_seconds"] == 20
    assert health_snapshot(status, 110, 15)["stale"] is False


def test_legacy_failure_is_unknown_and_backend_stall_is_not_http_failure():
    status = EndpointStatus("synthetic", False, 100, detail={"error": "private old message"})
    snapshot = health_snapshot(status, 110, 15)
    assert snapshot["failure"] is None
    assert snapshot["probe_id"] is None
    assert snapshot["stale"] is False
    assert "private" not in json.dumps(snapshot)
    status.detail = {"reason": "backend_no_progress", "status_code": 200}
    assert probe_failure(status=status)["category"] == "backend_no_progress"


def test_request_keeps_probe_evidence_after_cache_replacement(tmp_path, monkeypatch):
    import ai_router.policy as policy_module
    monkeypatch.setattr(policy_module, "time", SimpleNamespace(time=lambda: 110.0))
    status = EndpointStatus(endpoint().id, False, 100, detail={
        "error": "private response", "probe": {"probe_id": "failed-probe", "instance_id": "instance-a",
        "boot_id": "boot-a", "elapsed_ms": 3000, "failure": {
            **probe_failure(error=httpx.ReadTimeout("private response")), "body": "private body"}}})
    policy = object.__new__(RoutingPolicy)
    policy.settings = SimpleNamespace(section=lambda name: {"stale_after_seconds": 15})
    candidate = policy._trace_candidate(endpoint(), status, evaluation=Evaluation("general", None, 1, "test"),
        prompt_tokens=10, output_reserve_tokens=20, required_capabilities=RequestCapabilities(protocol="chat"),
        rejection_reason="unhealthy")
    assert candidate["healthy"] is False
    assert candidate["fresh"] is False
    assert candidate["health_evidence"]["stale"] is False, "Failed and expired are separate facts"
    trace = DecisionTrace(request_id="health-request", client_id="test", key_id="test", protocol="chat",
                          requested_model="auto", excerpt={}, instance_id="instance-b", boot_id="boot-b",
                          settings_hash="settings", registry_hash="registry")
    trace.record(1, "candidate_scope", "passed", reason="candidates", evidence={"candidates": [candidate]})
    store = RouteTraceStore(tmp_path / "traces.sqlite3")
    asyncio.run(store.save(trace))
    status.detail.clear()
    saved = asyncio.run(store.get("health-request"))
    step = next(item for item in saved["attempts"][0]["steps"] if item["node_id"] == "candidate_scope")
    stored = step["evidence"]["candidates"][0]["health_evidence"]
    assert stored["probe_id"] == "failed-probe"
    assert stored["instance_id"] == "instance-a"
    assert stored["failure"]["category"] == "read_timeout"
    assert "private" not in json.dumps(saved)
