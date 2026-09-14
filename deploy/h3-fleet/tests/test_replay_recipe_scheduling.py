import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import replay_recipe_scheduling as replay_module
from scripts.replay_recipe_scheduling import GIB, replay, synthetic_fixture


def job_result(report, policy, identifier):
    return next(job for job in report["results"][policy]["jobs"] if job["prompt_id"] == identifier)


def test_two_fast_only_b8_jobs_do_not_block_light_jobs_in_new_plan():
    fixture = synthetic_fixture()
    original = copy.deepcopy(fixture)
    report = replay(fixture)
    assert fixture == original
    assert report["trace_kind"] == "synthetic"
    assert report["result_kind"] == "counterfactual_replay_not_live_benchmark"
    assert job_result(report, "strict_fifo", "003-a4")["started_at"] == 780
    assert job_result(report, "throughput", "003-a4")["started_at"] == 120
    assert job_result(report, "throughput", "003-a4")["backend_id"] in {"main", "preview"}
    assert report["results"]["strict_fifo"]["makespan_seconds"] == 1740
    assert report["results"]["throughput"]["makespan_seconds"] == 1320
    assert report["comparison"]["makespan_saved_seconds"] == 420


def test_all_b8_has_no_false_three_lane_gain():
    fixture = synthetic_fixture()
    for job in fixture["jobs"]:
        job.update(recipe_id="B8", eligible_backend_ids=["fast"], duration_seconds_by_backend={"fast": 600})
    report = replay(fixture)
    for result in report["results"].values():
        assert result["peak_parallel"] == 1
        assert {job["backend_id"] for job in result["jobs"]} == {"fast"}
        assert result["makespan_seconds"] == 2640
    assert report["comparison"]["modeled_speedup_ratio"] == 1


def test_insufficient_capacity_serializes_even_with_idle_qualified_gpus():
    fixture = synthetic_fixture()
    fixture["resources"]["effective_workingset_bytes"] = 51 * GIB
    report = replay(fixture)
    for result in report["results"].values():
        assert result["status"] == "completed"
        assert result["peak_parallel"] == 1
        for event in result["events"]:
            if event["event"] == "start":
                assert event["projected_bytes"] == 71 * GIB
    assert report["comparison"]["modeled_speedup_ratio"] == 1


def test_first_and_each_additional_lane_require_a_new_sixty_second_window():
    report = replay(synthetic_fixture())
    for result in report["results"].values():
        previous_change = 0
        for event in result["events"]:
            if event["event"] == "start":
                assert event["at"] - previous_change >= 60
                assert event["stable_seconds"] >= 60
            previous_change = event["at"]
    starts = [event["at"] for event in report["results"]["throughput"]["events"] if event["event"] == "start"]
    assert starts[:3] == [60, 120, 180]


def test_running_remaining_is_reserved_until_completion_not_released_by_elapsed_time():
    fixture = synthetic_fixture()
    fixture["resources"].update(effective_workingset_bytes=32 * GIB, running_remaining_bytes=2 * GIB)
    report = replay(fixture)
    result = report["results"]["throughput"]
    assert result["peak_parallel"] == 2
    starts = [event for event in result["events"] if event["event"] == "start"]
    assert starts[1]["reserved_running_bytes"] == 20 * GIB
    assert starts[1]["projected_bytes"] == 72 * GIB
    assert starts[2]["at"] >= 720
    assert all(event["projected_bytes"] <= 72 * GIB for event in starts)


@pytest.mark.parametrize("alter", [
    lambda fixture: fixture["jobs"][0]["eligible_backend_ids"].append("unknown"),
    lambda fixture: fixture["jobs"][0]["duration_seconds_by_backend"].update(unknown=1),
    lambda fixture: fixture["backends"][0].pop("qualified_recipes"),
    lambda fixture: fixture["backends"][0].update(qualified_recipes=["A4"]),
    lambda fixture: fixture["backends"][0].pop("enabled"),
])
def test_unknown_backend_or_qualification_fails_closed(alter):
    fixture = synthetic_fixture()
    alter(fixture)
    with pytest.raises(ValueError):
        replay(fixture)


@pytest.mark.parametrize("alter", [
    lambda fixture: fixture.update(stable_seconds=59),
    lambda fixture: fixture.update(protected_seconds=899),
    lambda fixture: fixture["resources"].update(cgroup_limit_bytes=73 * GIB),
    lambda fixture: fixture["resources"].update(global_margin_bytes=GIB),
    lambda fixture: fixture["jobs"][0].update(candidate_budget_bytes=17 * GIB),
    lambda fixture: fixture["resources"].pop("effective_workingset_bytes"),
    lambda fixture: fixture["resources"].update(effective_workingset_bytes=True),
    lambda fixture: fixture["jobs"][0].update(duration_seconds_by_backend={}),
    lambda fixture: fixture["jobs"][0].update(duration_seconds_by_backend={"fast": float("nan")}),
    lambda fixture: fixture["jobs"][0].update(created_at=-1),
])
def test_missing_inputs_and_lowered_safety_floors_are_rejected(alter):
    fixture = synthetic_fixture()
    alter(fixture)
    with pytest.raises(ValueError):
        replay(fixture)


def test_two_backends_of_the_same_physical_gpu_are_not_parallel_capacity():
    fixture = synthetic_fixture()
    for backend in fixture["backends"]:
        backend["gpu_uuid"] = "one-physical-gpu"
    report = replay(fixture)
    assert all(result["peak_parallel"] == 1 for result in report["results"].values())


@pytest.mark.parametrize("memory_blocked", [True, False])
def test_impossible_work_terminates_without_reporting_complete_throughput(memory_blocked):
    fixture = synthetic_fixture()
    if memory_blocked:
        fixture["resources"]["effective_workingset_bytes"] = 60 * GIB
    else:
        for backend in fixture["backends"]:
            backend["enabled"] = False
    report = replay(fixture)
    for result in report["results"].values():
        assert result["status"] == "blocked"
        assert result["makespan_seconds"] is None
        assert result["completed_per_hour"] is None
        assert len(result["pending"]) == 4
    assert report["comparison"]["modeled_speedup_ratio"] is None


def test_durations_drive_metrics_and_no_gain_is_hardcoded():
    fixture = synthetic_fixture()
    for job in fixture["jobs"]:
        if job["recipe_id"] == "A4":
            job["duration_seconds_by_backend"] = {backend: 100 for backend in job["eligible_backend_ids"]}
    report = replay(fixture)
    assert report["comparison"]["modeled_speedup_ratio"] == 1
    assert report["comparison"]["makespan_saved_seconds"] == 0
    for result in report["results"].values():
        assert result["completed_per_hour"] == pytest.approx(4 * 3600 / result["makespan_seconds"])
        waits = [job["started_at"] - job["created_at"] for job in result["jobs"]]
        assert result["queue_wait"]["completed_mean_seconds"] == sum(waits) / len(waits)


def test_arrivals_are_not_scheduled_before_their_timestamp():
    fixture = synthetic_fixture()
    fixture["jobs"][3]["created_at"] = 3000
    report = replay(fixture)
    for result in report["results"].values():
        assert all(job["started_at"] >= job["created_at"] for job in result["jobs"])
        assert result["makespan_seconds"] >= 3900


def test_replay_calls_actual_planner_and_is_deterministic(monkeypatch):
    original = replay_module.plan_assignments
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs["now"])
        return original(*args, **kwargs)

    monkeypatch.setattr(replay_module, "plan_assignments", tracked)
    first = replay(synthetic_fixture())
    second = replay(synthetic_fixture())
    assert calls and first == second


def test_explicit_measured_inputs_remain_counterfactual_not_live_benchmark():
    fixture = synthetic_fixture()
    fixture.update(trace_kind="measured", provenance="CPU test for measured-input parsing, NOT real measured data")
    report = replay(fixture)
    assert report["trace_kind"] == "measured"
    assert report["result_kind"] == "counterfactual_replay_not_live_benchmark"
    fixture.pop("provenance")
    with pytest.raises(ValueError):
        replay(fixture)


def test_cli_is_explicit_and_reads_local_trace(tmp_path):
    script = Path(replay_module.__file__)
    result = subprocess.run([sys.executable, str(script), "--synthetic"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["trace_kind"] == "synthetic"
    source = tmp_path / "trace.json"
    source.write_text(json.dumps(synthetic_fixture()))
    result = subprocess.run([sys.executable, str(script), "--trace", str(source)], capture_output=True, text=True, check=True)
    assert len(json.loads(result.stdout)["input_file_sha256"]) == 64
    rejected = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert rejected.returncode == 2
