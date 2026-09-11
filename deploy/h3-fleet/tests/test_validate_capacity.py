from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

from app.admission import CapacityPolicy, GIB


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_capacity.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("validate_capacity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sample():
    return {"ok": True, "memory_available_bytes": 70 * GIB, "cgroup_current_bytes": 20 * GIB,
            "swap_used_bytes": 0, "cgroup_swap_bytes": 0, "root_available_bytes": 30 * GIB,
            "offload_available_bytes": 45 * GIB, "cgroup_events": {"oom": 0, "oom_kill": 0, "max": 0},
            "kernel_alerts": [], "gpus": [{"temperature_c": 70}],
            "services": [{"Id": "fast", "ActiveState": "active", "MainPID": "100", "NRestarts": "0"}]}


@pytest.mark.parametrize("kind", ["oom", "oom_kill", "max", "xid", "swap", "ram", "cgroup", "disk", "restart", "temperature"])
def test_monitor_stops_on_resource_or_worker_failure(kind):
    baseline = sample()
    current = copy.deepcopy(baseline)
    if kind in {"oom", "oom_kill", "max"}:
        current["cgroup_events"][kind] = 1
    elif kind == "xid":
        current["kernel_alerts"] = ["NVRM: Xid 79"]
    elif kind == "swap":
        current["swap_used_bytes"] = 2 * GIB
    elif kind == "ram":
        current["memory_available_bytes"] = 2 * GIB
    elif kind == "cgroup":
        current["cgroup_current_bytes"] = 79 * GIB
    elif kind == "disk":
        current["offload_available_bytes"] = 2 * GIB
    elif kind == "restart":
        current["services"][0]["MainPID"] = "200"
    elif kind == "temperature":
        current["gpus"][0]["temperature_c"] = 90
    assert MODULE.safety_reason(current, baseline, CapacityPolicy().data["resources"])


def test_preflight_refuses_managed_or_untracked_production_queue():
    for value in ({"active": [{"execution_id": "customer"}], "queues": []},
                  {"active": [], "queues": [{"queued_or_running": 1}]}):
        with pytest.raises(RuntimeError, match="existing production"):
            MODULE.check_idle(value)


def test_maximum_uses_real_frames_and_never_promotes_long_concurrency():
    policy = CapacityPolicy().data
    assert MODULE.maximum_for(policy, "preview", 124) == 3
    assert MODULE.maximum_for(policy, "quality", 124) == 2
    assert MODULE.maximum_for(policy, "preview", 362) == 1
    assert MODULE.maximum_for(policy, "quality", 362) == 1


@pytest.mark.parametrize("experiment", [
    {"profile": "quality", "frame_count": 362, "max_parallel": 3},
    {"profile": "preview", "frame_count": 500, "max_parallel": 3},
    {"profile": "preview", "frame_count": 124, "max_parallel": 3},
    {"profile": "preview", "frame_count": 362, "max_parallel": 4},
])
def test_experimental_capacity_is_bounded_to_long_work_and_physical_lanes(experiment):
    with pytest.raises(ValueError):
        CapacityPolicy.validate_experiment(experiment)


def test_process_credential_is_selected_without_reporting_other_environment(monkeypatch):
    monkeypatch.setattr(MODULE, "command", lambda *args: "123\n")
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"UNRELATED_SECRET=must-not-be-returned\0H3_ROUTER_KEY=test-key\0")
    assert MODULE.key_from_service() == "test-key"


def test_process_credential_refuses_stopped_service(monkeypatch):
    monkeypatch.setattr(MODULE, "command", lambda *args: "0\n")
    with pytest.raises(RuntimeError, match="no running process"):
        MODULE.key_from_service()


def prior_report(tmp_path):
    configuration = {"profile": "preview", "duration": 15, "frame_count": 362,
                     "aspect_ratio": "16:9", "max_parallel": 1}
    artifact = tmp_path / "owned.mp4"
    artifact.write_bytes(b"test-output")
    execution = {"artifact": artifact.name, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                 "media_validation": {"ok": True}}
    report = {"status": "passed_evidence_only_no_capacity_promotion", "lease_released": True,
              "configuration": configuration, "baseline": {"gpus": [{"uuid": "gpu-fast"}]},
              "batches": [{"level": 1, "batch": number, "status": "passed", "peak_parallel_running": 1,
                           "executions": [execution]} for number in (1, 2)]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path, configuration, artifact


def test_continuation_verifies_completed_two_batch_evidence_and_artifact_hash(tmp_path):
    path, configuration, artifact = prior_report(tmp_path)
    assert MODULE.verified_prior_levels(path, configuration, {"gpu-fast"}) == {1}
    artifact.write_bytes(b"changed-output")
    with pytest.raises(ValueError, match="changed or lost"):
        MODULE.verified_prior_levels(path, configuration, {"gpu-fast"})


def test_continuation_rejects_changed_workload_or_topology(tmp_path):
    path, configuration, _ = prior_report(tmp_path)
    with pytest.raises(ValueError, match="workload differs"):
        MODULE.verified_prior_levels(path, {**configuration, "duration": 5}, {"gpu-fast"})
    with pytest.raises(ValueError, match="topology differs"):
        MODULE.verified_prior_levels(path, configuration, {"another-gpu"})


def test_continuation_rejects_incomplete_batches(tmp_path):
    path, configuration, _ = prior_report(tmp_path)
    report = json.loads(path.read_text())
    report["batches"].pop()
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="incomplete level"):
        MODULE.verified_prior_levels(path, configuration, {"gpu-fast"})


def test_cleanup_only_uses_persisted_owned_execution_ids():
    calls = []
    owner = "h3val_0123456789abcdef"
    identifier = owner + "_1_0_0"
    async def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"execution_id": identifier, "status": "running" if request.method == "GET" else "cancelled"})
    async def scenario():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="outside"):
                await MODULE.cancel_owned(client, owner, ["customer-operation"])
            assert not calls
            assert (await MODULE.cancel_owned(client, owner, [identifier]))[0]["status"] == "cancelled"
    asyncio.run(scenario())
    assert calls == [("GET", "/api/router/executions/" + identifier),
                     ("POST", "/api/router/executions/" + identifier + "/cancel")]


def test_cleanup_unknown_result_is_preserved_for_reconciliation():
    owner = "h3val_0123456789abcdef"
    identifier = owner + "_1_0_0"
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"execution_id": identifier, "status": "submitted"})
        return httpx.Response(409, json={"detail": "unknown upstream outcome"})
    async def scenario():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            result = await MODULE.cancel_owned(client, owner, [identifier])
            assert result[0]["status"] == "reconciliation_required"
    asyncio.run(scenario())


def test_cleanup_does_not_treat_404_after_submission_as_proven_absence():
    owner = "h3val_0123456789abcdef"
    async def handler(request):
        return httpx.Response(404, json={"detail": "execution not found"})
    async def scenario():
        async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
            result = await MODULE.cancel_owned(client, owner, [owner + "_uncertain"])
            assert result[0]["status"] == "reconciliation_required"
    asyncio.run(scenario())
