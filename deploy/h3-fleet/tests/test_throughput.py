from __future__ import annotations

import copy
import json

import pytest

from app.throughput import GIB, MemoryAdmission, candidate_budget, estimate_seconds, plan_assignments, remaining_peak


LIMITS = {"max_cgroup_gib": 72, "min_available_ram_gib": 16, "max_swap_gib": 1,
          "swap_hard_limit_gib": 8, "min_root_free_gib": 25, "min_offload_free_gib": 40}


def sample(timestamp=100):
    return {"ok": True, "timestamp": timestamp, "boot_id": "boot",
            "cgroup_current_bytes": int(51.6 * GIB),
            "memory_stat": {"anon": 7 * GIB, "file": 44 * GIB, "inactive_file": 44 * GIB,
                            "active_file": 0, "file_dirty": 0, "file_writeback": 0, "unevictable": 0},
            "memory_available_bytes": 80 * GIB, "swap_used_bytes": 0, "cgroup_swap_bytes": 0,
            "swap_io_pages": {"pswpin": 0, "pswpout": 0},
            "cgroup_events": {"high": 0, "max": 0, "oom": 0, "oom_kill": 0},
            "memory_pressure": {key + suffix: 0 for key in ("host_some", "host_full", "cgroup_some", "cgroup_full")
                                for suffix in ("_total", "_avg10")},
            "root_available_bytes": 50 * GIB, "offload_available_bytes": 80 * GIB, "worker_memory_bytes": {}}


def ready(controller, owners=()):
    for timestamp in range(100, 161, 10):
        result = controller.observe(sample(timestamp), owners, now=timestamp)
    return result


def candidate():
    return {"candidate_budget_bytes": 18 * GIB, "vram_budget_bytes": 10 * GIB, "lane_id": "main"}


def decide(controller, current, active=None):
    return controller.decide(current, candidate(), active or [], limits=LIMITS,
                              now=current["timestamp"], vram_free_bytes=11 * GIB)


def test_44_gib_clean_cache_discounts_only_half():
    controller = MemoryAdmission()
    result = decide(controller, ready(controller))
    assert result["admission"] == "allow"
    assert result["effective_reclaimable"] == 22 * GIB
    assert result["projected"] == int(51.6 * GIB) - 22 * GIB + 20 * GIB


def test_dirty_writeback_unevictable_never_receive_credit():
    controller = MemoryAdmission()
    current = ready(controller)
    current["memory_stat"].update(file_dirty=10 * GIB, file_writeback=5 * GIB, unevictable=7 * GIB)
    result = decide(controller, current)
    assert result["effective_reclaimable"] == 11 * GIB


def test_zero_factor_preserves_raw_conservative_model():
    controller = MemoryAdmission(reclaim_factor=0)
    result = decide(controller, ready(controller))
    assert result["projected"] == int(51.6 * GIB) + 20 * GIB
    assert result["effective_working_set"] == result["memory_current"]


@pytest.mark.parametrize("change", ["swap_used_bytes", "pswpout", "host_some_avg10", "host_full_total"])
def test_pressure_or_swap_never_opens_extra_lane(change):
    controller = MemoryAdmission()
    ready(controller)
    current = sample(165)
    if change == "swap_used_bytes":
        current[change] = 4096
    elif change == "pswpout":
        current["swap_io_pages"][change] = 1
    else:
        current["memory_pressure"][change] = 1
    result = decide(controller, controller.observe(current, (), now=165))
    assert result["admission"] == "wait"
    assert result["effective_reclaimable"] == 0


def test_running_future_peak_and_regrowth_are_reserved():
    active = [{"backend_id": "worker", "candidate_budget_bytes": 18 * GIB,
               "worker_baseline_bytes": 2 * GIB, "observed_worker_peak_bytes": 8 * GIB}]
    assert remaining_peak(active, {"worker_memory_bytes": {"worker": 6 * GIB}}) == 14 * GIB
    assert remaining_peak(active, {"worker_memory_bytes": {"worker": GIB}}) == 19 * GIB


def test_exceeding_measured_budget_blocks_new_admissions():
    with pytest.raises(ValueError, match="exceeds_reserved"):
        remaining_peak([{"backend_id": "worker", "candidate_budget_bytes": 18 * GIB,
                         "worker_baseline_bytes": 0, "observed_worker_peak_bytes": 20 * GIB}],
                       {"worker_memory_bytes": {"worker": 20 * GIB}})


def test_unknown_history_retains_static_and_prior_version_floors():
    result = candidate_budget("A4", "0" * 64, 18 * GIB, None, [25 * GIB])
    assert result["fallback"] is True
    assert result["candidate_budget_bytes"] == 25 * GIB


@pytest.mark.parametrize(("current_gib", "allowed"), [(52, True), (53, False)])
def test_projected_must_not_exceed_72_gib(current_gib, allowed):
    controller = MemoryAdmission(reclaim_factor=0)
    current = ready(controller)
    current["cgroup_current_bytes"] = current_gib * GIB
    result = decide(controller, current)
    assert (result["admission"] == "allow") is allowed


def test_raw_limit_is_not_relaxed_by_large_cache():
    controller = MemoryAdmission()
    current = ready(controller)
    current["cgroup_current_bytes"] = 73 * GIB
    result = decide(controller, current)
    assert "raw_cgroup_hard_guard" in result["reasons"]


def test_new_owner_requires_another_60_second_window():
    controller = MemoryAdmission()
    ready(controller)
    result = controller.observe(sample(165), ("first-job",), now=165)
    assert not result["progressive"]["ready"]
    for timestamp in range(175, 226, 10):
        result = controller.observe(sample(timestamp), ("first-job",), now=timestamp)
    assert result["progressive"]["ready"]


def test_completed_owner_replaces_stale_swap_baseline_after_new_stable_window():
    controller = MemoryAdmission()
    idle = ready(controller)
    assert idle["progressive"]["baseline"]["swap_used_bytes"] == 0
    running = sample(170)
    running["swap_used_bytes"] = 3 * GIB
    controller.observe(running, ("finished-job",), now=170)
    for timestamp in range(180, 241, 10):
        current = sample(timestamp)
        current["swap_used_bytes"] = 3 * GIB
        result = controller.observe(current, (), now=timestamp)
    assert result["progressive"]["ready"]
    assert result["progressive"]["baseline"]["swap_used_bytes"] == 3 * GIB
    assert "swap_growth_limit" not in decide(controller, result)["reasons"]


def test_gap_cannot_count_as_continuous_observation():
    controller = MemoryAdmission()
    ready(controller)
    assert not controller.observe(sample(190), (), now=190)["progressive"]["ready"]


def test_missing_stat_fail_closed():
    controller = MemoryAdmission()
    current = sample()
    del current["memory_stat"]["inactive_file"]
    assert not controller.observe(current, (), now=100)["progressive"]["ready"]


def test_memory_reservation_blocks_even_when_gpu_idle():
    controller = MemoryAdmission(reclaim_factor=0)
    current = ready(controller)
    current["worker_memory_bytes"] = {"first": 2 * GIB}
    result = decide(controller, current, [{"backend_id": "first", "candidate_budget_bytes": 18 * GIB,
                                          "worker_baseline_bytes": 2 * GIB, "observed_worker_peak_bytes": 2 * GIB}])
    assert result["reserved_running_bytes"] == 18 * GIB
    assert result["admission"] == "wait"


def backend(identifier, warm=None):
    return {"id": identifier, "gpu_uuid": identifier, "warm_recipe_id": warm}


def job(identifier, recipe, eligible, created=100):
    return {"prompt_id": identifier, "recipe_id": recipe, "created_at": created,
            "eligible_backend_ids": eligible, "eligible_gpu_uuids": eligible}


def test_heavy_head_does_not_block_light_on_idle_3060():
    jobs = [job("older", "B8", ["fast"], 90), job("newer", "A4", ["fast", "main"], 100)]
    result = plan_assignments(jobs, [backend("main")], now=110, feasible=lambda assignment: True)
    assert result[0]["job"]["prompt_id"] == "newer"


def test_fast_is_not_permanently_reserved():
    result = plan_assignments([job("a4", "A4", ["fast", "main"])], [backend("fast")],
                              now=110, feasible=lambda assignment: True)
    assert result[0]["backend"]["id"] == "fast"


def test_scarce_heavy_recipe_gets_fast_and_light_fills_3060():
    jobs = [job("a4", "A4", ["fast", "main"], 90), job("b8", "B8", ["fast"], 100)]
    result = plan_assignments(jobs, [backend("fast"), backend("main")], now=110, feasible=lambda assignment: True)
    assert {(item["job"]["recipe_id"], item["backend"]["id"]) for item in result} == {("B8", "fast"), ("A4", "main")}


def test_waiting_protection_overrides_scarcity_and_warm_affinity():
    jobs = [job("a4", "A4", ["fast", "main"], 100), job("b8", "B8", ["fast"], 900)]
    result = plan_assignments(jobs, [backend("fast", "B8")], now=1001, feasible=lambda assignment: True)
    assert result[0]["job"]["recipe_id"] == "A4"


def test_two_ports_of_same_gpu_are_not_two_slots():
    backends = [backend("first"), {"id": "second", "gpu_uuid": "first"}]
    jobs = [job("one", "A4", ["first", "second"]), job("two", "A4", ["first", "second"])]
    assert len(plan_assignments(jobs, backends, now=110, feasible=lambda assignment: True)) == 1


def test_maximum_three_per_recipe_and_oldest_32_window():
    jobs = [job(str(index), "A4", ["fast"], index) for index in range(40)]
    visited = set()

    def feasible(assignment):
        visited.update(item["job"]["prompt_id"] for item in assignment)
        return False

    assert plan_assignments(jobs, [backend("fast")], now=100, feasible=feasible) == []
    assert visited == {"0", "1", "2"}


def test_eta_cold_warm_and_runtime_are_separate():
    observations = [{"recipe_id": "A4", "gpu_uuid": "fast", "runtime_version": "v1", "cold": True,
                     "execution_seconds": duration} for duration in (500, 550)]
    assert estimate_seconds(observations, "A4", "fast", "v1", True) == 660
    assert estimate_seconds(observations, "A4", "fast", "v1", False) == 1800
    assert estimate_seconds(observations, "A4", "fast", "v2", True) == 1800


def test_prediction_and_observed_peak_survive_json_report():
    from scripts.admission_reconciliation import start_record, observe, finish

    controller = MemoryAdmission()
    current = ready(controller)
    prediction = decide(controller, current)
    record = start_record(prediction, current, "A4", {})
    actual = copy.deepcopy(current)
    actual.update(timestamp=165, cgroup_current_bytes=60 * GIB)
    observe(record, actual)
    saved = json.loads(json.dumps(finish(record)))
    assert saved["prediction"]["projected_cgroup_bytes"] == prediction["projected"]
    assert saved["aggregate_cgroup"]["peak_bytes"] == 60 * GIB
