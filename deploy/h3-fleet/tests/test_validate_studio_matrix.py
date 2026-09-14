from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_studio_matrix import matrix_cases, prepare_workflow, verify_execution
from app.admission import CapacityPolicy


def graph():
    return {"1": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
            "2": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {"width": 864, "height": 480}},
            "3": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
            "4": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "old"}}}


def history(cached):
    return {"upstream": {"status": {"completed": True, "status_str": "success", "messages": [
        ["execution_start", {"timestamp": 1000}],
        ["execution_cached", {"nodes": cached}],
        ["execution_success", {"timestamp": 101000}],
    ]}}}


def test_each_execution_invalidates_sampling_without_mutating_template():
    template = graph()
    original = copy.deepcopy(template)
    first, seed = prepare_workflow(template, "owned_1", "portrait")
    second, other_seed = prepare_workflow(template, "owned_2", "portrait")
    assert template == original
    assert seed != other_seed
    assert first["1"]["inputs"]["noise_seed"] == seed
    assert first["4"]["inputs"]["filename_prefix"] != second["4"]["inputs"]["filename_prefix"]
    assert first["2"]["inputs"] == {"width": 480, "height": 864}
    assert prepare_workflow(template, "owned_1", "portrait") == (first, seed)


def test_model_cache_is_allowed_but_sampler_cache_is_not():
    evidence = verify_execution(history(["1", "2"]), "upstream", graph())
    assert evidence == {"started_at": 1, "finished_at": 101, "execution_seconds": 100,
                        "sampler_nodes": ["3"], "cached_nodes": ["1", "2"]}
    with pytest.raises(ValueError, match="sampler output was cached"):
        verify_execution(history(["1", "2", "3", "4"]), "upstream", graph())


def test_missing_or_unsuccessful_history_is_not_a_benchmark():
    with pytest.raises(ValueError, match="successful execution"):
        verify_execution({}, "upstream", graph())
    invalid = history([])
    invalid["upstream"]["status"]["status_str"] = "error"
    with pytest.raises(ValueError, match="successful execution"):
        verify_execution(invalid, "upstream", graph())


@pytest.fixture
def b8_graph():
    path = Path(__file__).resolve().parents[1] / "experiments/optimization-20260909/b_vdn/workflow.json"
    return json.loads(path.read_bytes())


def test_b8_execution_distinguishes_sampler_from_cached_setup(b8_graph):
    evidence = verify_execution(history(["4", "5", "7"]), "upstream", b8_graph)
    assert evidence == {"started_at": 1, "finished_at": 101, "execution_seconds": 100,
                        "sampler_nodes": ["10"], "cached_nodes": ["4", "5", "7"],
                        "recipe": "B8", "model_composer_nodes": ["5"],
                        "execution_plan_nodes": ["7"], "expected_steps": 8}


def test_b8_cached_sampler_is_not_inference(b8_graph):
    with pytest.raises(ValueError, match="sampler output was cached"):
        verify_execution(history(["10"]), "upstream", b8_graph)


@pytest.mark.parametrize("change", ["missing_composer", "missing_plan", "stage", "wiring"])
def test_b8_execution_requires_audited_graph(b8_graph, change):
    if change == "missing_composer":
        del b8_graph["5"]
    elif change == "missing_plan":
        del b8_graph["7"]
    elif change == "stage":
        b8_graph["5"]["inputs"]["stage"] = "stage_dmd_4nfe"
    else:
        b8_graph["10"]["inputs"]["sampler"] = ["4", 0]
    with pytest.raises(ValueError):
        verify_execution(history([]), "upstream", b8_graph)


@pytest.mark.parametrize("change", ["missing", "error", "timing"])
def test_b8_requires_successful_timed_execution(b8_graph, change):
    record = history([])
    if change == "missing":
        record = {}
    elif change == "error":
        record["upstream"]["status"]["status_str"] = "error"
    else:
        record["upstream"]["status"]["messages"][-1][1]["timestamp"] = 1000
    with pytest.raises(ValueError):
        verify_execution(record, "upstream", b8_graph)


def test_default_business_cases_use_full_duration_and_valid_leases():
    cases = matrix_cases(["preview_2", "preview_3", "mixed"])
    assert [len(workloads) for _, workloads, _ in cases] == [2, 3, 2]
    for _, workloads, experiment in cases:
        assert "short_preview" not in workloads
        CapacityPolicy.validate_experiment(experiment)
    assert cases[-1][2]["preview_frame_count"] == 362


def test_full_duration_mixed_lease_requires_explicit_shape():
    experiment = {"profile": "mixed", "frame_count": 362, "max_parallel": 2, "preview_frame_count": 362}
    demand = {"known_shape": True, "profile": "preview", "frame_count": 362, "width": 480, "height": 864, "steps": 4}
    assert CapacityPolicy.matches_experiment(demand, experiment)
    legacy = {key: value for key, value in experiment.items() if key != "preview_frame_count"}
    assert not CapacityPolicy.matches_experiment(demand, legacy)
    for value in (True, 125, 363, "362"):
        with pytest.raises(ValueError):
            CapacityPolicy.validate_experiment({**experiment, "preview_frame_count": value})
    with pytest.raises(ValueError):
        CapacityPolicy.validate_experiment({**experiment, "profile": "preview"})
