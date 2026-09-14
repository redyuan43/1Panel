from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import time

import httpx
import pytest

from app import recipe_dispatch
from app.throughput import GIB
from test_main import load_module
from test_throughput import sample


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    module = load_module(tmp_path)
    dispatcher = module.fleet.recipes
    class FakeProgress:
        def __init__(self, *args):
            self.prompt_id = None

        async def start(self):
            pass

        async def close(self):
            pass

        def bind(self, identifier):
            self.prompt_id = identifier

        def receipt(self):
            return {"ready": True, "sampler_intervals": [], "reasons": []}

    monkeypatch.setattr(recipe_dispatch, "RecipeProgress", FakeProgress)
    dispatcher.enabled = True
    dispatcher.policy.update(enabled=True, validation_cases=["A4", "A4_C0", "A4_C1", "B8"],
                             production_fence_url="http://127.0.0.1:8789")
    module.fleet.store.set_validation_lease("h3val_" + "a" * 16, 3600)
    backends = {}
    for lane in module.fleet.lanes:
        backends[lane.id] = {"id": lane.id, "lane_id": lane.id, "gpu_uuid": lane.gpu_uuid,
                             "runtime_version": "f" * 64, "url": lane.url.replace(":81", ":181"),
                             "pid": 1, "start_ticks": 1, "cgroup_path": "/unused", "cmdline_sha256": "a" * 64,
                             "recipes": {recipe: {"qualification": "trial", "recipe_version": "20260910.1",
                                                  "vram_budget_bytes": 8 * GIB}
                                         for recipe in ("A4", "A4_C0", "A4_C1", "B8")}}
    dispatcher.backends = backends
    monkeypatch.setattr(recipe_dispatch, "backend_identity", lambda backend: {key: backend[key] for key in
                                                                            ("pid", "start_ticks", "gpu_uuid", "runtime_version", "cgroup_path", "cmdline_sha256")})
    monkeypatch.setattr(module, "backend_identity", recipe_dispatch.backend_identity)
    current = sample(time.time())
    current.update(cgroup_current_bytes=2 * GIB, memory_stat={key: 0 for key in current["memory_stat"]},
                   memory_available_bytes=100 * GIB, kernel_alerts=[], page_size_bytes=4096,
                   worker_memory_bytes={lane.id: GIB for lane in module.fleet.lanes},
                   backend_identities={lane.id: {"pid": 1} for lane in module.fleet.lanes},
                   progressive={"ready": True, "reasons": [], "stable_seconds": 60})
    dispatcher.snapshot = current

    async def get_sample():
        dispatcher.snapshot["timestamp"] = time.time()
        return dispatcher.snapshot

    monkeypatch.setattr(dispatcher, "sample", get_sample)
    calls = []

    def respond(request):
        if request.url.path == "/api/router/capacity":
            return httpx.Response(200, json={"validation_lease": {"owner": "h3val_" + "a" * 16, "expires_at": time.time() + 3600},
                                             "active": [], "queues": []})
        if request.url.path == "/prompt":
            calls.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"prompt_id": "upstream_" + str(len(calls))})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"vram_total": 16 * GIB, "vram_free": 15 * GIB}]})
        if request.url.path.startswith("/history"):
            return httpx.Response(200, json={})
        if request.url.path == "/free":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    module.fleet.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return module, calls


def payload(module, number=1, recipe="A4"):
    graph, binding = module.fleet.recipes.catalog.build(recipe, "A dreamy pink bedroom", 12, "test/" + str(number))
    return {"prompt": graph, "extra_data": {"h3": {"recipe_id": recipe, "execution_id": "h3val_" + "a" * 16 + "_" + str(number),
                                                   "contract": binding}}}


def test_disabled_scheduler_does_not_submit(tmp_path):
    module = load_module(tmp_path)
    with pytest.raises(module.HTTPException, match="recipe_scheduler_not_enabled"):
        asyncio.run(module.submit_prompt(payload(module)))
    assert module.fleet.store.active() == []


def test_forged_recipe_or_extra_node_rejected_before_reservation(runtime):
    module, calls = runtime
    body = payload(module)
    body["prompt"]["injected"] = {"class_type": "UNETLoader", "inputs": {}}
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.submit_prompt(body))
    assert error.value.status_code == 400
    assert not calls


def test_recipe_frozen_and_same_request_is_not_resubmitted(runtime):
    module, calls = runtime

    async def run():
        first = await module.submit_prompt(payload(module))
        replay = await module.submit_prompt(payload(module))
        assert replay["prompt_id"] == first["prompt_id"]
        assert replay["idempotent_replay"]
        job = module.fleet.store.get(first["prompt_id"])
        assert job["recipe_id"] == "A4"
        assert job["recipe_version"] == "20260910.1"
        assert json.loads(job["backend_json"])["lane"]["gpu_uuid"].startswith("GPU-")
        assert json.loads(job["admission_json"])["candidate_budget_bytes"] == 18 * GIB
        assert json.loads(job["reconciliation_json"])["prediction"]["projected_cgroup_bytes"] > 0

    asyncio.run(run())
    assert len(calls) == 1


def test_single_success_does_not_grant_mixed_qualification(runtime):
    module, calls = runtime

    async def run():
        first = await module.submit_prompt(payload(module))
        second = await module.submit_prompt(payload(module, 2, "B8"))
        assert not first["queued"]
        assert second["queued"]
        job = module.fleet.store.get(second["prompt_id"])
        assert "mixed_recipe_combination_unvalidated" in job["admission_reason"]

    asyncio.run(run())
    assert len(calls) == 1


def test_concurrent_requests_cannot_double_reserve_gpu(runtime):
    module, calls = runtime
    dispatcher = module.fleet.recipes
    dispatcher.backends = {"fast": dispatcher.backends["fast"]}

    async def run():
        await asyncio.gather(*(module.submit_prompt(payload(module, number)) for number in (1, 2, 3)))
        active = module.fleet.store.active()
        assert sum(job["status"] == "submitted" for job in active) == 1
        assert sum(job["status"] == "queued" for job in active) == 2

    asyncio.run(run())
    assert len(calls) == 1


def test_transaction_rechecks_legacy_physical_ownership(runtime, monkeypatch):
    module, calls = runtime

    async def inject_reservation(backend, recipe_id):
        module.fleet.store.create(prompt_id="competitor", upstream_prompt_id="other", execution_id="competing",
                                  request_digest=None, lane_id=backend["lane_id"], stage="preview", profile="preview")
        return True

    monkeypatch.setattr(module.fleet.recipes, "prepare_backend", inject_reservation)
    result = asyncio.run(module.submit_prompt(payload(module)))
    assert result["queued"]
    assert not calls


def test_unknown_submission_holds_gpu_and_does_not_repost(runtime):
    module, calls = runtime

    async def run():
        first = await module.submit_prompt(payload(module))
        module.fleet.store.update(first["prompt_id"], status="reconciling", upstream_prompt_id="")
        await module.fleet.refresh_job(module.fleet.store.get(first["prompt_id"]))
        second = await module.submit_prompt(payload(module, 2))
        assert second["queued"]
        assert module.fleet.store.get(first["prompt_id"])["status"] == "reconciling"

    asyncio.run(run())
    assert len(calls) == 1


def test_recovery_uses_frozen_backend_not_new_lane_url(runtime):
    module, _ = runtime
    result = asyncio.run(module.submit_prompt(payload(module)))
    job = module.fleet.store.get(result["prompt_id"])
    expected = json.loads(job["backend_json"])["lane"]["url"]
    assert expected != module.fleet.lanes_by_id[job["lane_id"]].url
    module.fleet.recipes.backends.clear()
    assert module.fleet.recipes.lane_for(job).url == expected


def test_changed_process_never_frees_reservation(runtime, monkeypatch):
    module, _ = runtime
    result = asyncio.run(module.submit_prompt(payload(module)))
    monkeypatch.setattr(module, "backend_identity", lambda backend: (_ for _ in ()).throw(ValueError("changed")))
    job = asyncio.run(module.fleet.refresh_job(module.fleet.store.get(result["prompt_id"])))
    assert job["status"] == "reconciling"
    assert job["failure_reason"] == "bound_runtime_identity_changed"


def test_finished_prediction_actual_and_oom_quarantine_persist(runtime):
    module, _ = runtime

    async def run():
        result = await module.submit_prompt(payload(module))
        job = module.fleet.store.update(result["prompt_id"], status="error", worker_peak_bytes=21 * GIB)
        module.fleet.recipes.snapshot["cgroup_current_bytes"] = 28 * GIB
        module.fleet.recipes.snapshot["worker_memory_bytes"][job["lane_id"]] = 21 * GIB
        await module.fleet.recipes.completed(job, {"status": {"messages": [
            ["execution_start", {"timestamp": 1000}],
            ["execution_error", {"timestamp": 21000, "exception_message": "CUDA out of memory"}]]}})
        finished = module.fleet.store.get(job["prompt_id"])
        report = json.loads(finished["reconciliation_json"])
        assert report["state"] == "finished"
        assert report["prediction"]["projected_cgroup_bytes"] == 22 * GIB
        assert report["aggregate_cgroup"]["peak_bytes"] == 28 * GIB
        assert finished["execution_seconds"] == 20
        assert finished["failure_reason"] == "cuda_oom"
        backend = module.fleet.recipes.backends[job["lane_id"]]
        assert not module.fleet.recipes.qualifications("A4", backend)
        assert module.fleet.recipes.candidate("A4", backend)["candidate_budget_bytes"] == 22 * GIB
        with module.fleet.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM recipe_audit WHERE event='task_finished'").fetchone()[0] == 1

    asyncio.run(run())


def test_capacity_gpu_idle_but_memory_gate_closed(runtime):
    module, _ = runtime
    module.fleet.recipes.snapshot["cgroup_current_bytes"] = 65 * GIB
    result = asyncio.run(module.fleet.recipes.capacity())
    assert result["A4"]["available_slots"] == 0
    assert "projected_working_set_exceeds_limit" in result["A4"]["reasons"]


def test_expired_lease_cannot_dispatch_queued_work(runtime):
    module, calls = runtime
    module.fleet.recipes.snapshot["progressive"]["reasons"] = ["not_stable"]
    result = asyncio.run(module.submit_prompt(payload(module)))
    assert result["queued"]
    with module.fleet.store._connect() as connection:
        connection.execute("UPDATE controls SET value=? WHERE name='validation_lease'", (
            json.dumps({"owner": "h3val_" + "a" * 16, "expires_at": 1}),))
    module.fleet.recipes.snapshot["progressive"]["reasons"] = []
    asyncio.run(module.fleet.recipes.tick())
    assert not calls
    assert "expired" in module.fleet.store.get(result["prompt_id"])["admission_reason"]


def test_pid_must_own_actual_listening_socket():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert recipe_dispatch.owns_listener(Path("/proc") / str(os.getpid()), port)
    assert not recipe_dispatch.owns_listener(Path("/proc") / str(os.getpid()), port)


def test_hard_guard_cancels_only_owned_work_even_without_waiting(runtime, monkeypatch):
    module, _ = runtime
    cancelled = []

    async def cancel(identifier):
        cancelled.append(identifier)
        return module.fleet.store.update(identifier, status="cancelled")

    monkeypatch.setattr(module.fleet, "cancel_owned_recipe_job", cancel)

    async def run():
        result = await module.submit_prompt(payload(module))
        module.fleet.store.create(prompt_id="production", upstream_prompt_id="foreign", execution_id="foreign",
                                  request_digest=None, lane_id="fast", stage="preview", profile="preview")
        module.fleet.recipes.snapshot["cgroup_current_bytes"] = 80 * GIB
        await module.fleet.recipes.tick()
        assert cancelled == [result["prompt_id"]]
        assert module.fleet.store.get("production")["status"] == "submitted"
        assert module.fleet.recipes.control("recipe_hard_stop")

    asyncio.run(run())


def test_forecast_headroom_wait_does_not_cancel_healthy_task(runtime, monkeypatch):
    module, _ = runtime

    async def cancel(identifier):
        pytest.fail("healthy task must not be cancelled by admission waiting")

    monkeypatch.setattr(module.fleet, "cancel_owned_recipe_job", cancel)

    async def run():
        first = await module.submit_prompt(payload(module))
        module.fleet.recipes.snapshot["cgroup_current_bytes"] = 65 * GIB
        second = await module.submit_prompt(payload(module, 2))
        assert second["queued"]
        assert module.fleet.store.get(first["prompt_id"])["status"] == "submitted"

    asyncio.run(run())


def test_thin_boolean_qualification_cannot_promote_hardware(runtime, tmp_path):
    module, _ = runtime
    backend = module.fleet.recipes.backends["fast"]
    document = {"recipe_id": "A4", "recipe_version": "20260910.1", "gpu_uuid": backend["gpu_uuid"],
                "runtime_version": backend["runtime_version"], "status": "completed",
                "media_validation": {"ok": True}, "non_cached_sampler": True}
    path = tmp_path / "fake.json"
    path.write_text(json.dumps(document))
    import hashlib
    backend["recipes"]["A4"].update(qualification="single_completed", evidence={"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert not module.fleet.recipes.qualifications("A4", backend)
