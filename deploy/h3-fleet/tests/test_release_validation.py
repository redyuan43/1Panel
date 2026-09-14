import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.admission import CapacityPolicy
from app import recipe_dispatch
from test_main import FakeComfyClient, load_module, payload as legacy_payload
from test_admission import studio_parallel_demand, studio_parallel_policy
from test_recipe_dispatch import runtime, payload


PREFIX = "studio_abcdef012345_"


def gate(module, enabled=True):
    return module.fleet.store.set_release_validation_gate({
        "enabled": enabled, "allow_execution_prefixes": [PREFIX] if enabled else []})


def queued(module, identifier, recipe=None):
    body = legacy_payload(identifier)
    job = module.fleet.store.create(prompt_id=identifier, upstream_prompt_id="", lane_id="",
                                   request_digest=identifier,
                                   execution_id=identifier, stage="preview", profile="preview",
                                   status="queued", request_data=body)
    return module.fleet.store.update(job["prompt_id"], recipe_id=recipe,
                                    demand_json=json.dumps(module.fleet.policy.demand(body, "preview")))


@pytest.mark.parametrize("maximum", [0, 4, True, "1", 1.0])
def test_max_active_configuration_rejects_invalid(tmp_path, maximum):
    data = load_module(tmp_path).fleet.policy.data
    data["max_active_jobs"] = maximum
    path = tmp_path / "capacity-test.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="max_active_jobs"):
        CapacityPolicy(path)


def test_default_and_serial_capacity(tmp_path):
    policy = studio_parallel_policy(tmp_path)
    assert policy.data["max_active_jobs"] == 3
    policy.data["max_active_jobs"] = 1
    demand = studio_parallel_demand(policy)
    assert policy.blocked(demand, [demand], {}) == "capacity_full"
    result = policy.studio_preview_capacity([], {"ok": False}, ["fast", "main", "preview"])
    assert result["max_parallel"] == 1
    assert result["validated_parallel"] is False


def test_reserve_atomic_global_limit_counts_recipe_jobs(tmp_path):
    module = load_module(tmp_path)
    module.fleet.policy.data["max_active_jobs"] = 1
    first, second = queued(module, "first", "A4"), queued(module, "second")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(module.fleet.store.reserve, row["prompt_id"], lane,
                                   module.fleet.policy, module.resource_snapshot())
                   for row, lane in [(first, "fast"), (second, "main")]]
        assert sum(future.result() for future in futures) == 1
    assert sum(row["status"] == "reserved" for row in module.fleet.store.active()) == 1


@pytest.mark.parametrize("prefix", ["studio_", "studio_abc_", "studio_abcdef012345", "studio_abcdef012345__",
                                   "studio_ABCDEF012345_", "studio_abcdef012345_.*", "h3val_abc_", None, 1])
def test_gate_prefix_validation(tmp_path, prefix):
    module = load_module(tmp_path)
    with pytest.raises(ValueError):
        module.fleet.store.set_release_validation_gate({"enabled": True, "allow_execution_prefixes": [prefix]})


def test_gate_persistence_disable_and_audit(tmp_path):
    module = load_module(tmp_path)
    gate(module)
    for identifier in (None, "other", PREFIX, "studio_abcdef0123456_preview"):
        assert module.fleet.store.release_gate_reason(identifier)
    assert module.fleet.store.release_gate_reason(PREFIX + "preview_abc") is None
    reloaded = load_module(tmp_path)
    assert reloaded.fleet.store.release_validation_gate()["enabled"] is True
    assert reloaded.fleet.store.release_gate_reason("other")
    gate(reloaded, False)
    assert reloaded.fleet.store.release_gate_reason("other") is None
    with reloaded.fleet.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM recipe_audit WHERE event='release_validation_gate_updated'").fetchone()[0] == 2


def test_gate_does_not_cancel_existing_and_allowed_project_bypasses_held_fifo(tmp_path):
    module = load_module(tmp_path)
    held = queued(module, "old-other")
    allowed = queued(module, PREFIX + "preview")
    gate(module)
    assert not module.fleet.store.reserve(held["prompt_id"], "fast", module.fleet.policy, module.resource_snapshot())
    assert module.fleet.store.reserve(allowed["prompt_id"], "fast", module.fleet.policy, module.resource_snapshot())
    gate(module, False)
    assert module.fleet.store.get(allowed["prompt_id"])["status"] == "reserved"
    assert module.fleet.store.get(held["prompt_id"])["status"] == "queued"


def test_legacy_submit_gate_and_drain(tmp_path):
    module = load_module(tmp_path)
    module.fleet.client = FakeComfyClient()
    gate(module)

    async def run():
        with pytest.raises(module.HTTPException, match="release_validation_gate"):
            await module.submit_prompt(legacy_payload("other"))
        result = await module.submit_prompt(legacy_payload(PREFIX + "preview"))
        assert module.fleet.store.get(result["prompt_id"])["status"] == "submitted"
        module.fleet.draining = True
        with pytest.raises(module.HTTPException, match="draining"):
            await module.submit_prompt(legacy_payload(PREFIX + "second"))
        held = queued(module, PREFIX + "queued")
        assert (await module.fleet.dispatch_queued(held))["admission_reason"] == "fleet_draining"

    asyncio.run(run())


def test_gate_management_requires_router_auth(tmp_path):
    module = load_module(tmp_path)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            body = {"enabled": True, "allow_execution_prefixes": [PREFIX]}
            assert (await client.post("/api/router/release-validation-gate", json=body)).status_code == 401
            assert (await client.get("/api/router/release-validation-gate")).status_code == 401
            headers = {"Authorization": "Bearer test-router-key"}
            assert (await client.post("/api/router/release-validation-gate", json=body, headers=headers)).status_code == 200
            assert (await client.get("/api/router/release-validation-gate", headers=headers)).json()["enabled"]
            assert (await client.post("/api/router/release-validation-gate", json={"enabled": False}, headers=headers)).status_code == 400

    asyncio.run(run())


def test_recipe_gate_and_global_limit(runtime, monkeypatch):
    module, calls = runtime
    dispatcher = module.fleet.recipes
    module.fleet.store.release_validation_lease("h3val_" + "a" * 16)
    dispatcher.policy["experimental_only"] = False
    monkeypatch.setattr(dispatcher, "qualifications", lambda *args: True)
    gate(module)

    async def run():
        with pytest.raises(module.HTTPException, match="release_validation_gate"):
            await module.submit_prompt(payload(module))
        body = payload(module)
        body["extra_data"]["h3"]["execution_id"] = PREFIX + "preview"
        result = await module.submit_prompt(body)
        assert not result["queued"]
        assert module.fleet.store.get(result["prompt_id"])["status"] == "submitted"
        assert len(calls) == 1

    asyncio.run(run())
    module.fleet.policy.data["max_active_jobs"] = 1
    backend = dispatcher.backends["fast"]
    decision = dispatcher.decision("A4", backend, [{"status": "running"}], dispatcher.snapshot, {})
    assert decision["reasons"] == ["max_active_jobs_reached"]
    capacity = asyncio.run(dispatcher.capacity())
    assert all(item["available_slots"] == 0 and not item["progressive"] for item in capacity.values())


def test_historical_single_qualification_never_grants_mixed(runtime, monkeypatch):
    module, _ = runtime
    dispatcher = module.fleet.recipes
    backend = dispatcher.backends["fast"]
    backend["recipes"]["A4"]["qualification"] = "historical_single_completed"
    invoked = []

    def validate(entry, selected, catalog):
        invoked.append((entry, selected, catalog))
        return {"recipe_id": "A4", "single_task_only": True}

    monkeypatch.setattr(recipe_dispatch, "validate_legacy_single", validate)
    assert dispatcher.qualifications("A4", backend)
    assert len(invoked) == 1
    result = dispatcher.decision("A4", backend, [{"status": "running"}], dispatcher.snapshot, {})
    assert result["reasons"] == ["historical_single_only"]
    result = dispatcher.decision("B8", backend, [{"status": "running", "admission_json": json.dumps({
        "qualification": "historical_single_completed"})}], dispatcher.snapshot, {})
    assert result["reasons"] == ["historical_single_only"]

    def reject(*args):
        raise ValueError("bad evidence")

    monkeypatch.setattr(recipe_dispatch, "validate_legacy_single", reject)
    assert not dispatcher.qualifications("A4", backend)


def test_other_backend_same_gpu_cleanup_resamples_before_admission(runtime, monkeypatch):
    module, _ = runtime
    dispatcher = module.fleet.recipes
    target = dispatcher.backends["fast"]
    previous = {**copy.deepcopy(target), "id": "old", "url": "http://127.0.0.1:18191"}
    dispatcher.backends["old"] = previous
    with module.fleet.store._connect() as connection:
        connection.execute("INSERT INTO controls VALUES (?,?)", ("warm:old", json.dumps({"recipe_id": "B8"})))
    events = []
    monkeypatch.setattr(dispatcher, "decision", lambda *args: {"reasons": ["gpu_vram_headroom"]})

    async def unload(backend, *, reason):
        events.append((backend["id"], reason))
        return True

    async def sample():
        events.append("sample")
        return dispatcher.snapshot

    async def protection(rows):
        events.append("protection")
        return False

    monkeypatch.setattr(dispatcher, "unload", unload)
    monkeypatch.setattr(dispatcher, "sample", sample)
    monkeypatch.setattr(dispatcher, "hard_protection", protection)
    assert asyncio.run(dispatcher.cleanup_blocking_warm_backends([{"recipe_id": "A4"}], [target], [], {"fast": {}}))
    assert events == [("old", "blocking_same_gpu_warm_backend"), "sample", "protection"]


@pytest.mark.parametrize("queue", [{}, {"queue_running": None, "queue_pending": []},
                                  {"queue_running": [[1, "external"]], "queue_pending": []}])
def test_cleanup_never_frees_unknown_or_busy_queue(runtime, queue):
    module, _ = runtime
    frees = []

    def respond(request):
        if request.method == "POST":
            frees.append(str(request.url))
        return httpx.Response(200, json=queue)

    module.fleet.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    assert not asyncio.run(module.fleet.recipes.unload(module.fleet.recipes.backends["fast"], reason="test"))
    assert not frees


def test_recipe_transaction_rechecks_global_limit_after_sampling(runtime, monkeypatch):
    module, calls = runtime
    dispatcher = module.fleet.recipes
    module.fleet.policy.data["max_active_jobs"] = 1
    observations = []

    async def sample():
        observations.append(True)
        if len(observations) == 2:
            job = queued(module, "late-legacy")
            module.fleet.store.update(job["prompt_id"], status="running", lane_id="main")
        return dispatcher.snapshot

    monkeypatch.setattr(dispatcher, "sample", sample)
    result = asyncio.run(module.submit_prompt(payload(module)))
    assert result["queued"]
    assert not calls
    assert "max_active_jobs_reached" in module.fleet.store.get(result["prompt_id"])["admission_reason"]


def test_cleanup_failure_does_not_continue_or_submit(runtime, monkeypatch):
    module, calls = runtime
    dispatcher = module.fleet.recipes
    target = dispatcher.backends["fast"]
    dispatcher.backends["old"] = {**target, "id": "old"}
    with module.fleet.store._connect() as connection:
        connection.execute("INSERT INTO controls VALUES (?,?)", ("warm:old", json.dumps({"recipe_id": "B8"})))
    monkeypatch.setattr(dispatcher, "decision", lambda *args: {"reasons": ["gpu_vram_headroom"]})

    async def refuse(*args, **kwargs):
        return False

    async def forbidden():
        raise AssertionError("must not continue after an unconfirmed unload")

    monkeypatch.setattr(dispatcher, "unload", refuse)
    monkeypatch.setattr(dispatcher, "sample", forbidden)
    assert not asyncio.run(dispatcher.cleanup_blocking_warm_backends([{"recipe_id": "A4"}], [target], [], {"fast": {}}))
    assert not calls


def test_cleanup_preserves_running_task_on_same_gpu(runtime):
    module, calls = runtime
    job = queued(module, "existing")
    module.fleet.store.update(job["prompt_id"], status="running", lane_id="fast")
    assert not asyncio.run(module.fleet.recipes.unload(module.fleet.recipes.backends["fast"], reason="test"))
    assert module.fleet.store.get(job["prompt_id"])["status"] == "running"
    assert not calls


def test_enabled_empty_gate_blocks_all_and_does_not_cancel(tmp_path):
    module = load_module(tmp_path)
    job = queued(module, "running")
    module.fleet.store.update(job["prompt_id"], status="running", lane_id="fast")
    module.fleet.store.set_release_validation_gate({"enabled": True, "allow_execution_prefixes": []})
    assert module.fleet.store.release_gate_reason(PREFIX + "preview")
    assert module.fleet.store.get(job["prompt_id"])["status"] == "running"
