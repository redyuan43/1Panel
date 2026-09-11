from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
FLEET_ROOT = Path(os.environ.get("H3_RESIDUE_TEST_FLEET_ROOT", "/tmp/h3-multimodal-fleet-release-r5"))
GIB = 1024**3


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preparer = load("residue_release_preparer", ROOT / "scripts/prepare_multimodal_release.py")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real network, process or GPU access forbidden")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    original = {"app/" + name: (FLEET_ROOT / "app" / name).read_bytes()
                for name in ("main.py", "recipe_dispatch.py")}
    disk_module = FLEET_ROOT / "app/backend_residue.py"
    files = {**original, "app/backend_residue.py": disk_module.read_bytes()} if disk_module.is_file() else preparer.fleet_residue_overlay(original)
    package = ModuleType("residue_fixture")
    package.__path__ = [str(ROOT / "fleet")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    residue = load("residue_fixture.backend_residue", disk_module if disk_module.is_file() else ROOT / "fleet/backend_residue.py")
    monkeypatch.setattr(residue.BackendResidue, "poll_seconds", 0.03)
    monkeypatch.setattr(residue.BackendResidue, "poll_interval", 0.001)
    monkeypatch.setitem(sys.modules, residue.__name__, residue)
    admission = ModuleType("residue_fixture.admission")
    admission.BUSY = {"reserved", "running", "submitted", "reconciling", "cancelling"}
    monkeypatch.setitem(sys.modules, admission.__name__, admission)
    reconciliation = load("residue_reconciliation", FLEET_ROOT / "scripts/admission_reconciliation.py")
    dispatcher_module = ModuleType("residue_fixture.recipe_dispatch")
    dispatcher_module.__dict__.update(asyncio=asyncio, json=json, time=time, re=re, GIB=GIB,
        BUSY=admission.BUSY, finish=reconciliation.finish, observe=reconciliation.observe,
        record_execution_failure=reconciliation.record_execution_failure)
    dispatcher_ast = next(node for node in ast.parse(files["app/recipe_dispatch.py"]).body
                          if isinstance(node, ast.ClassDef) and node.name == "RecipeDispatcher")
    dispatcher_ast.body = [node for node in dispatcher_ast.body if getattr(node, "name", "") in {
        "audit", "control", "completed", "unload", "idle_cleanup", "cleanup_blocking_warm_backends"}]
    exec("from __future__ import annotations\n" + ast.unparse(dispatcher_ast), dispatcher_module.__dict__)
    monkeypatch.setitem(sys.modules, dispatcher_module.__name__, dispatcher_module)
    namespace = dict(__name__="residue_fixture.main", __package__="residue_fixture", asyncio=asyncio,
        sqlite3=sqlite3, json=json, time=time, Path=Path, HTTPException=HTTPException,
        TERMINAL_STATUSES={"completed", "error", "missing", "cancelled"})
    main_ast = ast.parse(files["app/main.py"])
    selected = [node for node in main_ast.body if getattr(node, "name", "") in {"JobStore", "_cancel_job_locked", "queue_contains"}]
    exec("from __future__ import annotations\n" + "\n".join(ast.unparse(node) for node in selected), namespace)
    store = namespace["JobStore"](tmp_path / "fleet.sqlite3")
    store.initialize()
    identity = {"pid": 123, "start_ticks": "456", "gpu_uuid": "GPU-owned", "cgroup_path": "/owned/cgroup",
                "cmdline_sha256": "a" * 64, "url": "http://127.0.0.1:18488", "runtime_version": "runtime-v1"}
    backend = {"id": "single-a4", "lane_id": "lane", **identity}
    lane = SimpleNamespace(id="lane", url=identity["url"], gpu_uuid=identity["gpu_uuid"])
    state = SimpleNamespace(running=True, posts=[], queues={}, identity=copy.deepcopy(identity),
        free_after=2, free_polls=0, used=8266 * 1024**2, response_lost=False, health_unknown=False, sample_invalid=False,
        omit_process_memory=False, old_ticks=identity["start_ticks"])
    monkeypatch.setattr(residue, "process_start_ticks", lambda process_id: state.old_ticks)

    def checked_identity(value):
        if any(state.identity.get(key) != value.get(key) for key in identity):
            raise ValueError("runtime_process_identity_changed")
        return copy.deepcopy(state.identity)

    dispatcher_module.backend_identity = checked_identity

    async def get(url, **kwargs):
        queue = {"queue_running": [[0, "upstream-original"]] if state.running and url.startswith(lane.url) else [], "queue_pending": []}
        return httpx.Response(200, json=state.queues.get(url, queue), request=httpx.Request("GET", url))

    async def post(url, **kwargs):
        state.posts.append(url)
        if url.endswith("/interrupt") or url.endswith("/queue"):
            state.running = False
        if url.endswith("/free") and state.response_lost:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    async def health(target):
        if any(url.endswith("/free") for url in state.posts):
            state.free_polls += 1
            if state.free_polls >= state.free_after:
                state.used = 512 * 1024**2
        return {"ok": not state.health_unknown, "vram_total": 16 * GIB, "vram_free": 16 * GIB - state.used}

    async def sample():
        return {"ok": not state.sample_invalid, "timestamp": time.time(), "cgroup_current_bytes": 3 * GIB,
            "memory_available_bytes": 64 * GIB, "worker_memory_bytes": {backend["id"]: GIB},
            "backend_identities": {backend["id"]: copy.deepcopy(state.identity)},
            "gpu_process_memory": {} if state.omit_process_memory else {str(state.identity["pid"]) + ":" + state.identity["gpu_uuid"]: state.used}}

    dispatcher = dispatcher_module.RecipeDispatcher()
    fleet = SimpleNamespace(store=store, recipes=dispatcher, lanes=[lane], lanes_by_id={lane.id: lane},
        assignment_lock=asyncio.Lock(), client=SimpleNamespace(get=get, post=post), lane_health=health,
        cleanup_job_inputs=lambda job: None, draining=False)
    dispatcher.fleet = fleet
    dispatcher.backends = {backend["id"]: backend}
    dispatcher.policy = {}
    dispatcher.enabled = True
    dispatcher.progress = {}
    dispatcher.sample = sample
    dispatcher.profile_key = lambda *args: "fixture-profile"
    dispatcher.lane_for = lambda job: lane
    dispatcher.lane_for_binding = lambda value: lane
    dispatcher.hard_protection = AsyncMock(return_value=False)
    dispatcher.decision = lambda *args: {"reasons": ["gpu_vram_headroom"] if state.used > GIB else []}
    dispatcher.snapshot = {}
    dispatcher.waiting = lambda *args: None
    namespace["fleet"] = fleet
    job = store.create(prompt_id="cancelled-original", upstream_prompt_id="upstream-original", execution_id="execution-original",
        request_digest="fixture", lane_id=lane.id, stage="preview", profile="preview", status="running")
    record = reconciliation.start_record({}, asyncio.run(sample()), "A4", {backend["id"]: GIB})
    binding = {"id": backend["id"], "runtime_version": identity["runtime_version"], "identity": identity,
               "lane": vars(lane)}
    store.update(job["prompt_id"], recipe_id="A4", backend_json=json.dumps(binding), reconciliation_json=json.dumps(record))
    listener = SimpleNamespace(close=AsyncMock(), receipt=lambda: {"events": [], "prompt_id": "upstream-original"})
    dispatcher.progress[job["prompt_id"]] = listener
    manager = residue.install(fleet)
    return SimpleNamespace(fleet=fleet, dispatcher=dispatcher, backend=backend, state=state, listener=listener,
        manager=manager, residue=residue, job=job, cancel=namespace["_cancel_job_locked"], files=files, original=original)


def cancel(runtime):
    return asyncio.run(runtime.cancel(runtime.job["prompt_id"], {}))


def free_count(runtime):
    return sum(url.endswith("/free") for url in runtime.state.posts)


def test_cancel_settles_listener_reconciliation_audit_and_not_warm(runtime):
    assert cancel(runtime)["status"] == "cancelled"
    job = runtime.fleet.store.get(runtime.job["prompt_id"])
    assert json.loads(job["reconciliation_json"])["state"] == "finished"
    runtime.listener.close.assert_awaited_once()
    assert not runtime.dispatcher.progress
    assert not runtime.dispatcher.control("warm:single-a4")
    assert runtime.manager.records()[0]["settled"]
    asyncio.run(runtime.dispatcher.completed(job, {}))
    with runtime.fleet.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM recipe_audit WHERE event='task_finished'").fetchone()[0] == 1
    assert free_count(runtime) == 0


def test_async_free_posts_once_and_preserves_existing_queued_execution(runtime):
    cancel(runtime)
    queued = runtime.fleet.store.create(prompt_id="waiting", upstream_prompt_id="", execution_id="existing-queued-execution",
        request_digest="queued", lane_id="", stage="preview", profile="preview", status="queued")
    assert asyncio.run(runtime.manager.cleanup())
    assert runtime.state.free_polls >= 2 and free_count(runtime) == 1
    assert runtime.manager.records()[0]["state"] == "released"
    assert runtime.fleet.store.get("waiting") == queued
    assert not runtime.dispatcher.control("warm:single-a4")


@pytest.mark.parametrize("failure", ["timeout", "response_lost", "unknown_health", "bad_sample"])
def test_unconfirmed_free_is_never_reposted_and_recovery_queries_first(runtime, failure):
    cancel(runtime)
    runtime.state.free_after = 100000 if failure in {"timeout", "response_lost"} else 1
    runtime.state.response_lost = failure == "response_lost"
    runtime.state.health_unknown = failure == "unknown_health"
    runtime.state.sample_invalid = failure == "bad_sample"
    assert not asyncio.run(runtime.manager.cleanup())
    assert runtime.manager.records()[0]["state"] != "released"
    restarted = runtime.residue.BackendResidue(runtime.fleet)
    assert not asyncio.run(restarted.cleanup())
    assert free_count(runtime) == 1
    runtime.state.used = 512 * 1024**2
    runtime.state.free_after = 1
    runtime.state.health_unknown = False
    runtime.state.sample_invalid = False
    assert asyncio.run(restarted.cleanup())
    assert free_count(runtime) == 1


@pytest.mark.parametrize("field,value", [("gpu_uuid", "GPU-other"), ("pid", 999), ("start_ticks", "reused"),
                                        ("cmdline_sha256", "changed"), ("url", "http://wrong")])
def test_changed_gpu_or_process_identity_never_frees(runtime, field, value):
    cancel(runtime)
    runtime.state.identity[field] = value
    assert not asyncio.run(runtime.manager.cleanup())
    assert free_count(runtime) == 0


@pytest.mark.parametrize("queue", [{"queue_running": [[0, "other"]], "queue_pending": []},
                                  {"queue_running": [], "queue_pending": [[0, "other"]]}, {}])
def test_other_same_gpu_backend_queue_must_be_empty_and_known(runtime, queue):
    cancel(runtime)
    peer = {**runtime.backend, "id": "single-b8", "url": "http://127.0.0.1:18489"}
    runtime.dispatcher.backends[peer["id"]] = peer
    runtime.state.queues[peer["url"] + "/queue"] = queue
    assert not asyncio.run(runtime.manager.cleanup())
    assert free_count(runtime) == 0


@pytest.mark.parametrize("status", ["reserved", "running", "submitted", "reconciling", "cancelling"])
def test_same_gpu_busy_job_blocks_free(runtime, status):
    cancel(runtime)
    runtime.fleet.store.create(prompt_id="busy", upstream_prompt_id="other", execution_id="busy-execution",
        request_digest="busy", lane_id="lane", stage="preview", profile="preview", status=status)
    assert not asyncio.run(runtime.manager.cleanup())
    assert free_count(runtime) == 0


def test_legacy_cancelled_observing_record_recovers_without_warm(runtime):
    runtime.state.running = False
    with runtime.fleet.store._connect() as connection:
        connection.execute("UPDATE jobs SET status='cancelled' WHERE prompt_id=?", (runtime.job["prompt_id"],))
    restarted = runtime.residue.BackendResidue(runtime.fleet)
    assert restarted.records()[0]["settled"] is False
    assert asyncio.run(restarted.cleanup())
    job = runtime.fleet.store.get(runtime.job["prompt_id"])
    assert json.loads(job["reconciliation_json"])["state"] == "finished"
    runtime.listener.close.assert_awaited_once()
    assert restarted.records()[0]["state"] == "released"
    assert not runtime.dispatcher.control("warm:single-a4")


def test_terminal_record_and_status_roll_back_together_on_receipt_failure(runtime):
    with runtime.fleet.store._connect() as connection:
        connection.execute("CREATE TRIGGER reject_residue BEFORE INSERT ON controls BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END")
    with pytest.raises(sqlite3.IntegrityError):
        runtime.fleet.store.update(runtime.job["prompt_id"], status="cancelled")
    assert runtime.fleet.store.get(runtime.job["prompt_id"])["status"] == "running"


def test_settlement_failure_blocks_free_until_reconciled(runtime, monkeypatch):
    runtime.fleet.store.update(runtime.job["prompt_id"], status="cancelled")
    monkeypatch.setattr(runtime.dispatcher, "completed", AsyncMock(side_effect=RuntimeError("settlement failed")))
    assert not asyncio.run(runtime.manager.cleanup())
    assert free_count(runtime) == 0


def test_overlay_changes_only_three_files_and_checks_anchors(runtime):
    assert set(runtime.files) == {"app/main.py", "app/recipe_dispatch.py", "app/backend_residue.py"}
    assert b"await fleet.recipes.completed(updated, {})" in runtime.files["app/main.py"]
    assert runtime.original["app/main.py"] == (FLEET_ROOT / "app/main.py").read_bytes()
    with pytest.raises(ValueError, match="anchor changed"):
        preparer.fleet_residue_overlay(runtime.files)


def test_admission_cleanup_discovers_non_warm_residue(runtime):
    cancel(runtime)
    result = asyncio.run(runtime.dispatcher.cleanup_blocking_warm_backends(
        [{"recipe_id": "A4"}], [runtime.backend], [], {runtime.backend["id"]: {}}))
    assert result and free_count(runtime) == 1


def test_idle_cleanup_discovers_non_warm_residue(runtime):
    cancel(runtime)
    asyncio.run(runtime.dispatcher.idle_cleanup())
    assert runtime.manager.records()[0]["state"] == "released"
    assert free_count(runtime) == 1


def test_missing_pid_memory_never_means_zero(runtime):
    cancel(runtime)
    runtime.state.omit_process_memory = True
    assert not asyncio.run(runtime.manager.cleanup())
    assert runtime.manager.records()[0]["state"] != "released"
    assert not asyncio.run(runtime.manager.cleanup())
    assert free_count(runtime) == 1


def test_cancel_recovery_does_not_sample_other_running_job(runtime, monkeypatch):
    job = runtime.fleet.store.get(runtime.job["prompt_id"])
    previous = json.loads(job["reconciliation_json"])
    runtime.fleet.store.update(job["prompt_id"], status="cancelled")
    runtime.fleet.store.create(prompt_id="other", upstream_prompt_id="other", execution_id="other-execution",
        request_digest="other", lane_id="lane", stage="preview", profile="preview", status="running")
    sampled = AsyncMock(side_effect=AssertionError("must not sample subsequent work"))
    monkeypatch.setattr(runtime.dispatcher, "sample", sampled)
    assert not asyncio.run(runtime.manager.cleanup())
    sampled.assert_not_awaited()
    finished = json.loads(runtime.fleet.store.get(job["prompt_id"])["reconciliation_json"])
    assert finished["state"] == "finished"
    assert finished["sample_count"] == previous["sample_count"]
    assert finished["last_observed_at"] == previous["last_observed_at"]
    assert free_count(runtime) == 0


@pytest.mark.parametrize("finished", [False, True])
def test_settlement_preserves_oom_first_error_and_finished_evidence(runtime, finished):
    job = runtime.fleet.store.get(runtime.job["prompt_id"])
    record = json.loads(job["reconciliation_json"])
    failure = {"cuda_oom_detected": True, "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]}
    record["execution_failures"].append(failure)
    record["cuda_oom_detected"] = True
    if finished:
        reconciliation = load("residue_finish_test", FLEET_ROOT / "scripts/admission_reconciliation.py")
        record = reconciliation.finish(record)
    runtime.fleet.store.update(job["prompt_id"], reconciliation_json=json.dumps(record), failure_reason="first-provider-error",
                               execution_seconds=12.5)
    cancel(runtime)
    settled = runtime.fleet.store.get(job["prompt_id"])
    result = json.loads(settled["reconciliation_json"])
    assert result["cuda_oom_detected"] and result["execution_failures"][0] == failure
    assert settled["failure_reason"] == "first-provider-error" and settled["execution_seconds"] == 12.5
    if finished:
        assert result == record


@pytest.mark.parametrize("old_ticks", [None, "new-generation"])
def test_old_worker_generation_is_retired_without_freeing_new_worker(runtime, old_ticks):
    cancel(runtime)
    runtime.state.old_ticks = old_ticks
    runtime.state.identity.update(pid=999, start_ticks="new-generation")
    runtime.backend.update(runtime.state.identity)
    runtime.fleet.backend_lifecycle = SimpleNamespace(before_dispatch=AsyncMock(return_value=True))
    restarted = runtime.residue.install(runtime.fleet)
    assert asyncio.run(runtime.fleet.backend_lifecycle.before_dispatch([{"execution_id": "same-queued"}]))
    assert restarted.records()[0]["state"] == "retired"
    assert free_count(runtime) == 0


def test_unreadable_old_process_identity_never_retires(runtime, monkeypatch):
    cancel(runtime)
    def denied(process_id):
        raise PermissionError("proc unavailable")
    monkeypatch.setattr(runtime.residue, "process_start_ticks", denied)
    assert not asyncio.run(runtime.manager.cleanup())
    assert runtime.manager.records()[0]["state"] == "pending"
    assert free_count(runtime) == 0


@pytest.mark.parametrize("kind", ["newer", "different_identity", "stale"])
def test_legacy_cancel_does_not_erase_newer_or_different_worker_warm(runtime, kind):
    job = runtime.fleet.store.get(runtime.job["prompt_id"])
    warm = {"recipe_id": "A4", "runtime_version": runtime.backend["runtime_version"],
            "idle_since": job["updated_at"] + (60 if kind == "newer" else -60)}
    if kind == "different_identity":
        warm["identity"] = {**runtime.state.identity, "start_ticks": "newer-worker"}
    with runtime.fleet.store._connect() as connection:
        connection.execute("UPDATE jobs SET status='cancelled' WHERE prompt_id=?", (job["prompt_id"],))
        connection.execute("INSERT INTO controls(name,value) VALUES (?,?)", ("warm:single-a4", json.dumps(warm)))
    restarted = runtime.residue.BackendResidue(runtime.fleet)
    assert restarted.records()
    assert runtime.dispatcher.control("warm:single-a4") == (None if kind == "stale" else warm)


def test_install_before_store_initialization_defers_recovery_and_scans_once(runtime, tmp_path, monkeypatch):
    store = type(runtime.fleet.store)(tmp_path / "uninitialized.sqlite3")
    fleet = SimpleNamespace(**{**vars(runtime.fleet), "store": store, "recipes": copy.copy(runtime.dispatcher)})
    fleet.recipes.fleet = fleet
    manager = runtime.residue.install(fleet)
    assert not store.path.exists()
    with pytest.raises(sqlite3.OperationalError, match="no such table: jobs"):
        manager.records()
    store.initialize()
    original = runtime.fleet.store.get(runtime.job["prompt_id"])
    job = store.create(prompt_id="legacy", upstream_prompt_id="upstream-legacy", execution_id="legacy-execution",
        request_digest="legacy", lane_id="lane", stage="preview", profile="preview", status="cancelled")
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET recipe_id=?,backend_json=?,reconciliation_json=? WHERE prompt_id=?",
            (original["recipe_id"], original["backend_json"], original["reconciliation_json"], job["prompt_id"]))
    statements = []
    connect = store._connect

    def traced_connect():
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    first = manager.records()
    assert len(first) == 1 and first[0]["prompt_id"] == "legacy"
    assert manager.records() == first
    assert sum(statement.startswith("SELECT prompt_id FROM jobs WHERE status IN") for statement in statements) == 1


def test_failed_legacy_recovery_propagates_and_can_retry_after_repair(runtime):
    with runtime.fleet.store._connect() as connection:
        connection.execute("UPDATE jobs SET status='cancelled' WHERE prompt_id=?", (runtime.job["prompt_id"],))
        connection.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON controls BEGIN SELECT RAISE(ABORT, 'recovery failed'); END")
    manager = runtime.residue.BackendResidue(runtime.fleet)
    with pytest.raises(sqlite3.IntegrityError, match="recovery failed"):
        manager.records()
    with runtime.fleet.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM controls WHERE name LIKE 'backend_residue:%'").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_recovery")
    assert len(manager.records()) == 1


@pytest.mark.parametrize("legacy_warm", [False, True])
def test_newer_completed_same_worker_supersedes_cancel_without_free_or_warm_deletion(runtime, legacy_warm):
    runtime.state.running = False
    old_job = runtime.fleet.store.get(runtime.job["prompt_id"])
    with runtime.fleet.store._connect() as connection:
        connection.execute("UPDATE jobs SET status='cancelled' WHERE prompt_id=?", (old_job["prompt_id"],))
    terminal_at = runtime.manager.records()[0]["terminal_at"]
    successor = runtime.fleet.store.create(prompt_id="healthy-successor", upstream_prompt_id="healthy-upstream",
        execution_id="healthy-execution", request_digest="healthy", lane_id="lane", stage="preview", profile="preview", status="completed")
    successor = runtime.fleet.store.update(successor["prompt_id"], recipe_id="A4", backend_json=old_job["backend_json"])
    asyncio.run(runtime.dispatcher.completed(successor, {}))
    warm = runtime.dispatcher.control("warm:single-a4")
    if legacy_warm:
        warm.pop("identity")
        warm.pop("prompt_id")
        with runtime.fleet.store._connect() as connection:
            connection.execute("UPDATE controls SET value=? WHERE name='warm:single-a4'", (json.dumps(warm),))
    assert asyncio.run(runtime.manager.cleanup())
    receipt = runtime.manager.records()[0]
    assert receipt["state"] == "superseded" and receipt["superseded_by"] == successor["prompt_id"]
    assert receipt["terminal_at"] == terminal_at == old_job["updated_at"]
    assert runtime.fleet.store.get(old_job["prompt_id"])["updated_at"] > terminal_at
    assert runtime.dispatcher.control("warm:single-a4") == warm
    assert free_count(runtime) == 0


def test_arbitrary_newer_warm_without_completed_success_cannot_supersede(runtime):
    cancel(runtime)
    warm = {"recipe_id": "A4", "runtime_version": runtime.backend["runtime_version"],
            "idle_since": time.time() + 60, "identity": runtime.state.identity, "prompt_id": "nonexistent"}
    with runtime.fleet.store._connect() as connection:
        connection.execute("INSERT INTO controls(name,value) VALUES ('warm:single-a4',?)", (json.dumps(warm),))
    assert asyncio.run(runtime.manager.cleanup())
    assert runtime.manager.records()[0]["state"] == "released"
    assert free_count(runtime) == 1
