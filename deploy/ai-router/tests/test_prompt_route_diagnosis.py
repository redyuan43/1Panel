"""请求路由诊断必须只读、脱敏，并将真实短测限制为一次请求。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prompt_route_diagnosis",
    ROOT / "scripts/diagnose-prompt-route.py",
)
diagnosis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnosis)


def trace_payload(**changes) -> dict:
    value = {
        "request_id": "router-id",
        "client_request_id": "client-id",
        "started_at": 1.0,
        "protocol": "chat",
        "task": "general",
        "requested_model": "siyuan/auto",
        "selected_model": "deepseek/deepseek-v4-flash",
        "endpoint_id": "cloud-deepseek-v4-flash",
        "deployment_id": "cloud-deepseek-v4-flash",
        "route_selected": True,
        "status": "succeeded",
        "status_code": 200,
        "request": {
            "prompt_tokens": 100,
            "output_reserve_tokens": 32,
            "required_context_tokens": 132,
            "modalities": ["text"],
            "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
        },
        "credential": "PRIVATE_CREDENTIAL",
        "attempts": [
            {
                "attempt": 1,
                "selection": {
                    "endpoint_id": "cloud-deepseek-v4-flash",
                    "selected_model": "deepseek/deepseek-v4-flash",
                    "reason": "remote_profile_fallback",
                    "affinity": "new",
                },
                "steps": [
                    {
                        "node_id": "candidate_scope",
                        "evidence": {
                            "candidates": [
                                {
                                    "endpoint_id": "edge-qwen38-flash",
                                    "node": "edge",
                                    "rejection_reason": "context",
                                    "required_context_tokens": 132,
                                    "safe_context_tokens": 64,
                                    "raw_error": "PRIVATE_UPSTREAM_ERROR",
                                },
                                {
                                    "endpoint_id": "cloud-deepseek-v4-flash",
                                    "node": "cloud-deepseek",
                                    "rejection_reason": None,
                                    "safe_context_tokens": 1048576,
                                },
                            ]
                        },
                    }
                ],
            }
        ],
    }
    value.update(changes)
    return value


def create_database(path: Path, payload: dict) -> None:
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE route_traces("
            "request_id TEXT, client_request_id TEXT, started_at REAL, "
            "payload_json TEXT)"
        )
        database.execute(
            "INSERT INTO route_traces VALUES (?,?,?,?)",
            (
                payload["request_id"],
                payload["client_request_id"],
                payload["started_at"],
                json.dumps(payload),
            ),
        )


def test_existing_request_diagnosis_is_read_only_and_filtered(tmp_path) -> None:
    path = tmp_path / "route-traces.sqlite3"
    payload = trace_payload()
    create_database(path, payload)
    before = path.read_bytes()

    trace = diagnosis.read_trace(path, "client-id")
    report = diagnosis.build_report(
        trace,
        identifier="client-id",
        settings=diagnosis.Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "absent.yaml",
        ),
        registry=diagnosis.Registry(ROOT / "config/registry.yaml"),
    )

    serialized = json.dumps(report)
    assert path.read_bytes() == before
    assert report["outcome"]["route_selected"] is True
    assert report["outcome"]["endpoint_id"] == "cloud-deepseek-v4-flash"
    assert report["candidates"][0]["rejection_reason"] == "context"
    assert report["explanation"]["verdict"]
    assert "PRIVATE_PROMPT" not in serialized
    assert "PRIVATE_CREDENTIAL" not in serialized
    assert "PRIVATE_UPSTREAM_ERROR" not in serialized


def test_missing_database_is_not_created(tmp_path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        diagnosis.read_trace(path, "missing")
    assert not path.exists()


def test_router_request_id_takes_priority_over_newer_client_id(tmp_path) -> None:
    path = tmp_path / "route-traces.sqlite3"
    exact = trace_payload(
        request_id="shared-id",
        client_request_id="exact-client",
        started_at=1,
    )
    collision = trace_payload(
        request_id="other-router-id",
        client_request_id="shared-id",
        started_at=2,
    )
    create_database(path, exact)
    with sqlite3.connect(path) as database:
        database.execute(
            "INSERT INTO route_traces VALUES (?,?,?,?)",
            (
                collision["request_id"],
                collision["client_request_id"],
                collision["started_at"],
                json.dumps(collision),
            ),
        )

    assert diagnosis.read_trace(path, "shared-id")["request_id"] == (
        "shared-id"
    )


def test_pre_route_tpm_rejection_is_not_described_as_model_shortage(
    tmp_path,
) -> None:
    trace = trace_payload(
        route_selected=False,
        status="failed",
        status_code=413,
        endpoint_id=None,
        selected_model=None,
        attempts=[],
        error={"code": "request_exceeds_tpm_limit"},
    )
    report = diagnosis.build_report(
        trace,
        identifier="router-id",
        settings=diagnosis.Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "absent.yaml",
        ),
        registry=diagnosis.Registry(ROOT / "config/registry.yaml"),
    )

    assert "尚未进入候选筛选" in report["explanation"]["verdict"]
    assert report["explanation"]["causal_chain"][1]["detail"] == "未执行"
    assert "没有合格" not in report["explanation"]["verdict"]


def test_running_trace_is_not_described_as_failed(tmp_path) -> None:
    trace = trace_payload(
        route_selected=False,
        status="running",
        status_code=None,
        endpoint_id=None,
        selected_model=None,
        attempts=[],
    )
    report = diagnosis.build_report(
        trace,
        identifier="router-id",
        settings=diagnosis.Settings(
            ROOT / "config/defaults.yaml",
            tmp_path / "absent.yaml",
        ),
        registry=diagnosis.Registry(ROOT / "config/registry.yaml"),
    )

    assert report["explanation"]["verdict"] == (
        "请求仍在处理中，尚未进入候选筛选。"
    )
    assert "失败" not in report["explanation"]["verdict"]


@pytest.mark.parametrize("status", [429, 503])
def test_live_failure_sends_once_and_does_not_export_error(status) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            headers={"X-Request-ID": "router-id"},
            json={"error": {"message": "PRIVATE_UPSTREAM_ERROR"}},
        )

    result = asyncio.run(
        diagnosis.live_short(
            "http://router.test",
            "PRIVATE_KEY",
            transport=httpx.MockTransport(handler),
        )
    )
    assert len(calls) == 1
    assert "PRIVATE" not in json.dumps(result)
    failed = trace_payload(
        client_request_id=result["client_request_id"],
        status="failed",
        route_selected=False,
    )
    assert diagnosis.live_verdict(result, failed) == "router_rejected"


def test_live_success_requires_a_selected_route_and_matching_audit() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            headers={"X-Request-ID": "router-id"},
            json={
                "choices": [
                    {
                        "message": {"content": diagnosis.MARKER},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    result = asyncio.run(
        diagnosis.live_short(
            "http://router.test",
            "key",
            transport=httpx.MockTransport(handler),
        )
    )
    assert len(calls) == 1
    trace = trace_payload(client_request_id=result["client_request_id"])
    assert diagnosis.live_verdict(result, trace) == "inference_verified"
    assert diagnosis.live_verdict(
        result,
        {**trace, "route_selected": False},
    ) == "route_not_confirmed"
    assert diagnosis.live_verdict(
        result,
        {**trace, "client_request_id": "wrong"},
    ) == "audit_identity_mismatch"


def test_live_timeout_is_not_retried() -> None:
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.Event().wait()

    result = asyncio.run(
        diagnosis.live_short(
            "http://router.test",
            "key",
            timeout=0.05,
            transport=httpx.MockTransport(handler),
        )
    )
    assert len(calls) == 1
    assert result["transport_error"] == "TimeoutError"


def test_report_is_private_and_never_overwritten(tmp_path) -> None:
    path = tmp_path / "report.json"
    diagnosis.save_report(path, {"first": True})
    with pytest.raises(FileExistsError):
        diagnosis.save_report(path, {"second": True})
    assert json.loads(path.read_text()) == {"first": True}
    assert path.stat().st_mode & 0o777 == 0o600
