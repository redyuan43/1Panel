from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


spec = importlib.util.spec_from_file_location("lifecycle_under_test", Path(__file__).resolve().parents[1] / "fleet/backend_lifecycle.py")
lifecycle_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle_module)
GIB = 1024**3


@pytest.fixture
def lifecycle(tmp_path):
    path = tmp_path / "fleet.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE controls(name TEXT PRIMARY KEY, value TEXT)")
    rows, audit, waits, commands, candidates = [], [], [], [], []

    def control(name, default=None):
        with sqlite3.connect(path) as database:
            result = database.execute("SELECT value FROM controls WHERE name=?", (name,)).fetchone()
        return json.loads(result[0]) if result else default

    def decide(sample, candidate, tasks, **kwargs):
        candidates.append(candidate)
        return {"admission": "allow", "projected_bytes": 55 * GIB}

    def runner(*args):
        commands.append(args)
        if args[:2] == ("systemctl", "cat"):
            return "owned service definition"
        if args[:2] == ("systemctl", "show"):
            return "MainPID=0\nActiveState=inactive\nSubState=dead\nControlGroup=/h3.slice/h3-compute.slice/h3-single-a4.service"
        if args[0] == "nvidia-smi":
            return "16000"
        raise RuntimeError("backend_start_failed")

    dispatcher = SimpleNamespace(backends={"single-a4": {"id": "single-a4", "pid": 1, "gpu_uuid": "gpu", "recipes": {"A4": {}}, "url": "http://127.0.0.1:18488"}},
        policy={"static_budget_gib": 18}, control=control, qualifications=lambda *args: True,
        waiting=lambda tasks, reason: waits.append(reason), audit=lambda event, data: audit.append((event, data)),
        sample=AsyncMock(return_value={}), candidate=lambda *args, **kwargs: {"candidate_budget_bytes": 18 * GIB},
        memory=SimpleNamespace(decide=decide))
    fleet = SimpleNamespace(recipes=dispatcher, assignment_lock=asyncio.Lock(), draining=False,
        store=SimpleNamespace(_connect=lambda: sqlite3.connect(path), active=lambda: rows,
                              release_gate_reason=lambda _: None, validation_lease=lambda: None, studio_batch=lambda: None),
        inspect_queues=AsyncMock(return_value=[]), policy=SimpleNamespace(data={"resources": {}}))
    registry = {"single-a4": {"unit": "h3-single-a4.service", "unit_sha256": hashlib.sha256(b"owned service definition").hexdigest()}}
    result = lifecycle_module.BackendLifecycle(fleet, registry, runner)
    result.observed = SimpleNamespace(rows=rows, audit=audit, waits=waits, commands=commands, candidates=candidates)
    return result


def test_policy_has_cas_and_stable_operation_receipts(lifecycle):
    async def run():
        body = {"operation_id": "disable-once", "expected_revision": "initial", "enabled": False}
        changed = await lifecycle.set_enabled(body)
        assert await lifecycle.set_enabled(body) == changed
        with pytest.raises(HTTPException, match="conflict"):
            await lifecycle.set_enabled({**body, "enabled": True})
        with pytest.raises(HTTPException, match="changed"):
            await lifecycle.set_enabled({**body, "operation_id": "second"})
    asyncio.run(run())
    assert not lifecycle.policy()["enabled"]
    assert lifecycle.observed.commands == []


def test_disabled_keeps_healthy_jobs_and_does_not_launch(lifecycle):
    async def run():
        lifecycle.observed.rows.append({"status": "running"})
        await lifecycle.set_enabled({"operation_id": "disable", "expected_revision": "initial", "enabled": False})
        assert not await lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}])
        await lifecycle.drain_stopped()
    asyncio.run(run())
    assert lifecycle.observed.rows == [{"status": "running"}]
    assert lifecycle.observed.commands == []
    assert lifecycle.observed.waits == ["backend_disabled"]


def test_cold_start_reserves_additional_budget_and_does_not_repeat_failure(lifecycle):
    async def run():
        jobs = [{"recipe_id": "A4", "execution_id": "test"}]
        assert not await lifecycle.before_dispatch(jobs)
        await asyncio.gather(*list(lifecycle.starting.values()))
        await lifecycle.before_dispatch(jobs)
    asyncio.run(run())
    assert lifecycle.observed.candidates[0]["candidate_budget_bytes"] == 36 * GIB
    assert lifecycle.observed.commands.count(("systemctl", "start", "h3-single-a4.service")) == 1
    assert lifecycle.dispatcher.control("lifecycle_quarantine:single-a4")["reason"] == "backend_start_failed"


def test_unknown_previous_start_is_not_reissued_after_restart(lifecycle):
    lifecycle.save_control("lifecycle_start:single-a4", {"state": "starting"})
    asyncio.run(lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}]))
    assert not any(command[:2] == ("systemctl", "start") for command in lifecycle.observed.commands)
    assert lifecycle.dispatcher.control("lifecycle_quarantine:single-a4")["reason"] == "backend_start_result_unknown_requires_reconciliation"


def test_missing_reference_evidence_waits_without_start_or_hardware_quarantine(lifecycle):
    def missing(*args, **kwargs):
        assert kwargs["job"]["execution_id"] == "test"
        raise ValueError("reference_video_memory_evidence_required")
    lifecycle.dispatcher.candidate = missing
    asyncio.run(lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}]))
    assert lifecycle.observed.waits[-1] == "reference_video_memory_evidence_required"
    assert not lifecycle.dispatcher.control("lifecycle_quarantine:single-a4")
    assert not any(command[:2] == ("systemctl", "start") for command in lifecycle.observed.commands)


def test_changed_service_definition_fails_closed(lifecycle):
    lifecycle.registry["single-a4"]["unit_sha256"] = "0" * 64
    asyncio.run(lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}]))
    assert len(lifecycle.observed.commands) == 1
    assert lifecycle.dispatcher.control("lifecycle_quarantine:single-a4")["reason"] == "backend_service_definition_changed"


def test_other_validation_owner_cannot_wake_services(lifecycle):
    lifecycle.fleet.store.validation_lease = lambda: {"owner": "another", "expires_at": 9999999999}
    asyncio.run(lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}]))
    assert lifecycle.observed.commands == []


def test_disable_is_responsive_before_background_start_and_preserves_queue(lifecycle):
    async def run():
        jobs = [{"recipe_id": "A4", "execution_id": "test"}]
        assert not await lifecycle.before_dispatch(jobs)
        tasks = list(lifecycle.starting.values())
        await lifecycle.set_enabled({"operation_id": "stop-before-wake", "expected_revision": "initial", "enabled": False})
        await asyncio.gather(*tasks)
        assert jobs == [{"recipe_id": "A4", "execution_id": "test"}]
    asyncio.run(run())
    assert not any(command[:2] == ("systemctl", "start") for command in lifecycle.observed.commands)
    assert lifecycle.dispatcher.control("lifecycle_start:single-a4")["state"] == "not_started"


def test_cold_start_requires_old_same_gpu_model_unload_confirmation(lifecycle):
    lifecycle.dispatcher.backends["single-b8"] = {"id": "single-b8", "gpu_uuid": "gpu"}
    lifecycle.save_control("warm:single-b8", {"recipe_id": "B8"})
    lifecycle.dispatcher.unload = AsyncMock(return_value=False)
    asyncio.run(lifecycle.before_dispatch([{"recipe_id": "A4", "execution_id": "test"}]))
    assert "previous_backend_unload_unconfirmed" in lifecycle.observed.waits
    assert not any(command[:2] == ("systemctl", "start") for command in lifecycle.observed.commands)


def test_service_control_uses_existing_noninteractive_privilege_only_for_whitelisted_units(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle_module.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(stdout=""))
    lifecycle_module.command("systemctl", "start", "h3-single-a4.service")
    assert calls == [("sudo", "-n", "/usr/bin/systemctl", "start", "h3-single-a4.service")]
    with pytest.raises(ValueError):
        lifecycle_module.command("systemctl", "stop", "unrelated-model.service")
    assert len(calls) == 1
