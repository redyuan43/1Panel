from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from test_main import FakeComfyClient, load_module, payload
from app.admission import CapacityPolicy, InstanceLock, SwapRecovery, GIB


def recovery_sample(timestamp=100, **updates):
    return {"ok": True, "timestamp": timestamp, "boot_id": "same-boot",
            "memory_available_bytes": 67 * GIB, "swap_used_bytes": int(1.03 * GIB),
            "cgroup_current_bytes": 25 * GIB, "cgroup_swap_bytes": int(1.01 * GIB),
            "root_available_bytes": 28 * GIB, "offload_available_bytes": 45 * GIB,
            "cgroup_events": {"high": 0, "max": 0, "oom": 0},
            "swap_io_pages": {"pswpin": 10, "pswpout": 20},
            "memory_pressure": {"host_some_total": 50, "host_full_total": 50,
                                "cgroup_some_total": 0, "cgroup_full_total": 0,
                                "host_some_avg10": 0, "host_full_avg10": 0,
                                "cgroup_some_avg10": 0, "cgroup_full_avg10": 0}, **updates}


def test_residual_swap_requires_full_quiet_window_then_single_known_workload(tmp_path):
    module = load_module(tmp_path)
    guard = SwapRecovery()
    assert not guard.observe(recovery_sample(100), idle=True)["swap_recovery"]["ready"]
    assert not guard.observe(recovery_sample(159), idle=True)["swap_recovery"]["ready"]
    ready = guard.observe(recovery_sample(160), idle=True)
    assert ready["swap_recovery"]["ready"]
    ready["timestamp"] = time.time()
    demand = module.fleet.policy.demand(payload("recovery"), "preview")
    assert module.fleet.policy.blocked(demand, [], ready) is None
    assert module.fleet.policy.blocked(demand, [demand], ready) == "swap_recovery_single_only"
    ready["memory_available_bytes"] = 20 * GIB
    assert module.fleet.policy.blocked(demand, [], ready) == "ram_headroom"


@pytest.mark.parametrize("updates", [
    {"swap_io_pages": {"pswpin": 11, "pswpout": 20}},
    {"swap_io_pages": {"pswpin": 10, "pswpout": 21}},
    {"memory_pressure": {"host_some_total": 51, "host_some_avg10": 0}},
    {"memory_pressure": {"host_some_total": 50, "host_some_avg10": 0.01}},
    {"cgroup_events": {"high": 1, "max": 0, "oom": 0}},
    {"boot_id": "new-boot"},
])
def test_swap_activity_pressure_or_counter_reset_invalidates_quiet_window(updates):
    guard = SwapRecovery()
    guard.observe(recovery_sample(100), idle=True)
    assert guard.observe(recovery_sample(160), idle=True)["swap_recovery"]["ready"]
    result = guard.observe(recovery_sample(165, **updates), idle=True)
    assert not result["swap_recovery"]["ready"]
    assert result["swap_recovery"]["reason"] == "active_swap_io_or_memory_pressure"


def test_swap_baseline_is_frozen_for_busy_and_unknown_work_then_reobserved(tmp_path):
    module = load_module(tmp_path)
    guard = SwapRecovery()
    guard.observe(recovery_sample(100), idle=True)
    ready = guard.observe(recovery_sample(160), idle=True)
    busy = guard.observe(recovery_sample(800, swap_used_bytes=3 * GIB), idle=False)
    assert busy["swap_recovery"]["baseline"] == ready["swap_recovery"]["baseline"]
    assert not busy["swap_recovery"]["ready"]
    busy["timestamp"] = time.time()
    demand = module.fleet.policy.demand(payload("growth"), "preview")
    assert module.fleet.policy.blocked(demand, [], busy) == "swap_growth_limit"
    assert not guard.observe(recovery_sample(900), idle=True)["swap_recovery"]["ready"]
    assert not SwapRecovery().observe(recovery_sample(1000), idle=False)["swap_recovery"]["ready"]


def test_reclaimed_swap_does_not_skip_the_next_idle_recovery_window(tmp_path):
    module = load_module(tmp_path)
    guard = SwapRecovery()
    guard.observe(recovery_sample(100), idle=True)
    guard.observe(recovery_sample(160), idle=True)
    guard.observe(recovery_sample(200), idle=False)
    idle = guard.observe(recovery_sample(300, swap_used_bytes=0, cgroup_swap_bytes=0), idle=True)
    idle["timestamp"] = time.time()
    demand = module.fleet.policy.demand(payload("next-round"), "preview")
    assert module.fleet.policy.blocked(demand, [], idle) == "swap_pressure"
    ready = guard.observe(recovery_sample(360, swap_used_bytes=0, cgroup_swap_bytes=0), idle=True)
    ready["timestamp"] = time.time()
    assert module.fleet.policy.blocked(demand, [], ready) is None


def test_swap_recovery_hard_limit_and_no_parallel_even_after_swap_reclaims(tmp_path):
    module = load_module(tmp_path)
    demand = module.fleet.policy.demand(payload("hard"), "preview")
    sample = recovery_sample(time.time(), cgroup_swap_bytes=8 * GIB)
    sample["swap_recovery"] = {"ready": True}
    assert module.fleet.policy.blocked(demand, [], sample) == "swap_hard_limit"
    sample.update(cgroup_swap_bytes=0, swap_used_bytes=0)
    sample["swap_recovery"]["baseline"] = {"host_bytes": 2 * GIB}
    assert module.fleet.policy.blocked(demand, [demand], sample) == "swap_recovery_single_only"


def test_recovered_admission_stays_atomic_and_fast_only(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        now = time.time()
        module.fleet.swap_recovery.observe(recovery_sample(now - 61), idle=True)
        module.resource_snapshot = lambda: recovery_sample(time.time())
        first = await module.submit_prompt(payload("residual-first"))
        second = await module.submit_prompt(payload("residual-second"))
        assert first["h3_lane"] == "fast"
        assert second["queued"]
        assert len(module.fleet.client.submissions) == 1
    asyncio.run(scenario())


def test_long_requests_are_exclusive_even_when_other_lanes_are_idle(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        long = payload("long")
        long["prompt"]["1"]["inputs"]["length"] = 362
        first = await module.submit_prompt(long)
        assert first["h3_lane"] == "fast"
        second = await module.submit_prompt(payload("short"))
        assert second["queued"] and second["h3_lane"] is None
        assert module.fleet.store.get(second["prompt_id"])["admission_reason"] == "exclusive_workload_active"
    asyncio.run(scenario())


def test_quality_and_preview_do_not_mix_without_evidence(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        await module.submit_prompt(payload("preview"))
        assert (await module.submit_prompt(payload("quality", "quality")))["queued"]
    asyncio.run(scenario())


def test_unknown_legacy_shape_is_exclusive_and_known_shape_cannot_lie(tmp_path):
    module = load_module(tmp_path)
    policy = module.fleet.policy
    assert policy.demand({"prompt": {}}, "preview")["class"] == "long"
    value = payload("forged")
    value["prompt"]["1"]["inputs"]["length"] = 362
    value["extra_data"]["h3"]["contract"] = {"frame_count": 124}
    with pytest.raises(ValueError, match="differs"):
        policy.demand(value, "preview")
    value["extra_data"]["h3"].pop("contract")
    value["prompt"]["1"]["inputs"]["width"] = 4096
    with pytest.raises(ValueError, match="dimensions"):
        policy.demand(value, "preview")


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("memory_available_bytes", 20 * GIB, "ram_headroom"),
    ("cgroup_current_bytes", 70 * GIB, "cgroup_headroom"),
    ("swap_used_bytes", 2 * GIB, "swap_pressure"),
    ("root_available_bytes", 20 * GIB, "root_disk_headroom"),
    ("offload_available_bytes", 40 * GIB, "offload_disk_headroom"),
    ("ok", False, "resource_telemetry_unavailable"),
    ("timestamp", 1, "resource_telemetry_unavailable"),
])
def test_resource_gate_keeps_execution_queued(tmp_path, field, value, reason):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        snapshot = module.resource_snapshot()
        snapshot[field] = value
        module.resource_snapshot = lambda: snapshot
        result = await module.submit_prompt(payload("resources"))
        assert result["queued"]
        assert module.fleet.store.get(result["prompt_id"])["admission_reason"] == reason
        assert not module.fleet.client.submissions
    asyncio.run(scenario())


def test_sqlite_reservation_competes_across_independent_connections(tmp_path):
    module = load_module(tmp_path)
    policy = module.fleet.policy
    for identifier in ("one", "two"):
        module.fleet.store.create(prompt_id=identifier, upstream_prompt_id="", execution_id=identifier,
                                 request_digest=identifier, lane_id="", stage="preview", profile="preview", status="queued")
        module.fleet.store.update(identifier, demand_json=json.dumps(policy.demand(payload(identifier), "preview")))
    barrier = threading.Barrier(2)
    def reserve(identifier):
        store = module.JobStore(module.fleet.store.path)
        barrier.wait()
        return store.reserve(identifier, "fast", policy, module.resource_snapshot())
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(reserve, ("one", "two"))) == 1
    assert len(module.fleet.store.active_for_lane("fast")) == 1


def test_instance_lock_is_released_only_by_its_owner(tmp_path):
    first = InstanceLock(tmp_path / "fleet.sqlite3")
    second = InstanceLock(tmp_path / "fleet.sqlite3")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


class LostResponseClient(FakeComfyClient):
    async def post(self, url, json=None, **kwargs):
        if url.endswith("/prompt"):
            await super().post(url, json=json, **kwargs)
            raise httpx.ReadTimeout("response lost after upstream accepted")
        return await super().post(url, json=json, **kwargs)

    async def get(self, url, **kwargs):
        if url.endswith("/queue"):
            records = [[0, identifier, {}, self.payloads[index]["extra_data"]]
                       for index, identifier in enumerate(self.submissions)
                       if identifier in self.pending]
            return httpx.Response(200, request=httpx.Request("GET", url),
                                  json={"queue_running": [], "queue_pending": records})
        if url.endswith("/history"):
            return httpx.Response(200, request=httpx.Request("GET", url), json=self.history)
        return await super().get(url, **kwargs)


def test_lost_response_reconciles_by_operation_without_resubmitting(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = LostResponseClient()
        module.fleet.client = fake
        result = await module.submit_prompt(payload("lost"))
        unknown = module.fleet.store.get(result["prompt_id"])
        assert unknown["status"] == "reconciling"
        assert module.fleet.store.active_for_lane("fast")
        recovered = await module.fleet.refresh_job(unknown)
        assert recovered["upstream_prompt_id"] == "upstream-1"
        assert recovered["status"] == "running"
        await module.submit_prompt(payload("lost"))
        assert len(fake.submissions) == 1
    asyncio.run(scenario())


def test_unknown_absence_and_cancel_keep_reservation(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = LostResponseClient()
        module.fleet.client = fake
        result = await module.submit_prompt(payload("lost"))
        fake.pending.clear()
        unknown = module.fleet.store.update(result["prompt_id"], created_at=time.time() - 3600)
        assert (await module.fleet.refresh_job(unknown))["status"] == "reconciling"
        with pytest.raises(module.HTTPException) as error:
            await module.cancel_job(result["prompt_id"], {})
        assert error.value.status_code == 409
        assert module.fleet.store.active_for_lane("fast")
        assert not fake.interrupts
    asyncio.run(scenario())


def test_lost_response_can_reconcile_a_completed_history_record(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = LostResponseClient()
        module.fleet.client = fake
        result = await module.submit_prompt(payload("history"))
        fake.pending.clear()
        fake.history["upstream-1"] = {
            "prompt": [0, "upstream-1", {}, fake.payloads[0]["extra_data"]],
            "outputs": {"1": {"videos": [{"filename": "recovered.mp4"}]}},
            "status": {"completed": True, "status_str": "success"},
        }
        recovered = await module.fleet.refresh_job(module.fleet.store.get(result["prompt_id"]))
        assert recovered["status"] == "completed"
        assert not module.fleet.store.active_for_lane("fast")
    asyncio.run(scenario())


def test_cancellation_refuses_to_interrupt_unrelated_running_work(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        result = await module.submit_prompt(payload("owned"))
        fake.pending.clear()
        fake.running.update({"upstream-1", "production-other"})
        with pytest.raises(module.HTTPException) as error:
            await module.cancel_job(result["prompt_id"], {})
        assert error.value.status_code == 409
        assert not fake.interrupts
    asyncio.run(scenario())


def test_long_capacity_cannot_be_raised_by_environment_policy(tmp_path):
    policy = CapacityPolicy().data
    policy["long"]["max_parallel"] = 2
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="not been validated"):
        CapacityPolicy(path)


def test_validation_lease_is_durable_and_blocks_foreign_submissions(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        owner = "h3val_0123456789abcdef"
        module.fleet.store.set_validation_lease(owner, 120)
        other_store = module.JobStore(module.fleet.store.path)
        assert other_store.validation_lease()["owner"] == owner
        with pytest.raises(module.HTTPException) as error:
            await module.submit_prompt(payload("production"))
        assert error.value.status_code == 503
        submitted = await module.submit_prompt(payload(owner + "_test"))
        assert submitted["h3_lane"] == "fast"
        with pytest.raises(module.HTTPException):
            module.fleet.store.release_validation_lease(owner)
        await module.cancel_job(submitted["prompt_id"], {})
        module.fleet.store.release_validation_lease(owner)
        assert module.fleet.store.validation_lease() is None
    asyncio.run(scenario())


def test_validation_window_refuses_existing_production_work(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        await module.submit_prompt(payload("customer"))
        with pytest.raises(module.HTTPException) as error:
            module.fleet.store.set_validation_lease("h3val_0123456789abcdef", 120)
        assert error.value.status_code == 409
    asyncio.run(scenario())


def test_untracked_work_on_another_lane_blocks_new_admission(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        fake.running.add("untracked-production")
        module.fleet.client = fake
        result = await module.submit_prompt(payload("new"))
        assert result["queued"]
        assert not fake.submissions
    asyncio.run(scenario())


def test_older_long_job_is_not_starved_by_new_short_requests(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        first = await module.submit_prompt(payload("first"))
        long = payload("older-long")
        long["prompt"]["1"]["inputs"]["length"] = 362
        waiting = await module.submit_prompt(long)
        later = await module.submit_prompt(payload("later-short"))
        await module.cancel_job(first["prompt_id"], {})
        await module.fleet.refresh_active()
        assert module.fleet.store.get(waiting["prompt_id"])["status"] == "submitted"
        assert module.fleet.store.get(later["prompt_id"])["status"] == "queued"
    asyncio.run(scenario())


def test_explicit_validation_lease_can_test_long_concurrency_without_promoting_policy(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        owner = "h3val_0123456789abcdef"
        experiment = {"profile": "preview", "frame_count": 362, "max_parallel": 3}
        module.fleet.store.set_validation_lease(owner, 120, experiment)
        results = []
        for index in range(3):
            value = payload(owner + "_" + str(index))
            value["prompt"]["1"]["inputs"]["length"] = 362
            results.append(await module.submit_prompt(value))
        assert [value["h3_lane"] for value in results] == ["fast", "main", "preview"]
        assert module.fleet.policy.data["long"]["max_parallel"] == 1
        with pytest.raises(module.HTTPException) as error:
            await module.submit_prompt(payload(owner + "_wrong_shape"))
        assert error.value.status_code == 400
        for result in results:
            await module.cancel_job(result["prompt_id"], {})
        module.fleet.store.release_validation_lease(owner)
        long = payload("production-long")
        long["prompt"]["1"]["inputs"]["length"] = 362
        await module.submit_prompt(long)
        assert (await module.submit_prompt(payload("production-short")))["queued"]
    asyncio.run(scenario())


def test_validation_experiment_does_not_bypass_resources_or_allow_mutation(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        owner = "h3val_0123456789abcdef"
        experiment = {"profile": "quality", "frame_count": 362, "max_parallel": 2}
        module.fleet.store.set_validation_lease(owner, 120, experiment)
        with pytest.raises(module.HTTPException):
            module.fleet.store.set_validation_lease(owner, 120, {**experiment, "max_parallel": 1})
        snapshot = module.resource_snapshot()
        snapshot["cgroup_current_bytes"] = 70 * GIB
        module.resource_snapshot = lambda: snapshot
        value = payload(owner + "_quality", "quality")
        value["prompt"]["1"]["inputs"]["length"] = 362
        assert (await module.submit_prompt(value))["queued"]
        assert not module.fleet.client.submissions
    asyncio.run(scenario())


def test_expired_validation_window_holds_active_work_and_stops_new_dispatch(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        owner = "h3val_0123456789abcdef"
        module.fleet.store.set_validation_lease(owner, -1, {"profile": "preview", "frame_count": 362, "max_parallel": 3})
        # Persisted work keeps an expired lease closed after a validator crash.
        module.fleet.store.create(prompt_id="held", upstream_prompt_id="", execution_id=owner + "_held",
                                 request_digest="held", request_data=payload(owner + "_held"), lane_id="",
                                 stage="preview", profile="preview", status="queued")
        with pytest.raises(module.HTTPException) as error:
            await module.submit_prompt(payload("production"))
        assert error.value.status_code == 503
        assert module.fleet.store.get("held")["status"] == "queued"
        assert not module.fleet.client.submissions
    asyncio.run(scenario())


def test_legacy_unknown_submission_without_timestamp_is_never_requeued(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = LostResponseClient()
        job = module.fleet.store.create(prompt_id="legacy", upstream_prompt_id="legacy-upstream",
                                       execution_id="legacy", request_digest="legacy", lane_id="fast",
                                       stage="preview", profile="preview", status="reconciling")
        job["updated_at"] = time.time() - 3600
        assert (await module.fleet.refresh_job(job))["status"] == "reconciling"
        assert module.fleet.store.active_for_lane("fast")
        assert not module.fleet.client.submissions
    asyncio.run(scenario())


def test_late_validation_submission_cannot_start_after_lease_release(tmp_path):
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        owner = "h3val_0123456789abcdef"
        module.fleet.store.set_validation_lease(owner, 120)
        module.fleet.store.release_validation_lease(owner)
        with pytest.raises(module.HTTPException) as error:
            await module.submit_prompt(payload(owner + "_late_arrival"))
        assert error.value.status_code == 409
        assert not module.fleet.store.active()
        assert not module.fleet.client.submissions
    asyncio.run(scenario())
