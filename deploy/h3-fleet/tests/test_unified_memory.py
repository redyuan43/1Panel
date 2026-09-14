import copy
import pytest

from app.unified_memory import EDGE_UUID, GIB, admission_reasons, validate_policy
from app.throughput import MemoryAdmission
from test_throughput import sample, LIMITS

POLICY = {"kind": "gb10", "minimum_available_bytes": 80 * GIB,
          "host_budget_bytes": 64 * GIB, "external_gpu_processes": "deny"}


def decision(available=100, external=False, unified=True, stale=False):
    control = MemoryAdmission(reclaim_factor=0)
    for timestamp in range(100, 161, 10):
        value = sample(timestamp)
        value.update(cgroup_current_bytes=GIB, memory_available_bytes=available * GIB,
                     gpu_names={EDGE_UUID: "NVIDIA GB10"},
                     gpu_process_identities=[("7", EDGE_UUID)] + ([("9", EDGE_UUID)] if external else []))
        value["memory_stat"] = dict(anon=GIB, file=0, inactive_file=0, active_file=0,
                                    file_dirty=0, file_writeback=0, unevictable=0)
        value = control.observe(value, (), now=timestamp)
    candidate = dict(candidate_budget_bytes=64 * GIB, vram_budget_bytes=16 * GIB,
                     lane_id="fast", gpu_uuid=EDGE_UUID, worker_pid=7,
                     unified_memory=POLICY if unified else None)
    return control.decide(value, candidate, [], limits={**LIMITS, "max_cgroup_gib": 96},
                          now=180 if stale else 160, vram_free_bytes=None)


def test_gb10_uses_host_memory_without_inventing_vram():
    assert decision()["admission"] == "allow"


def test_discrete_na_remains_closed():
    assert "gpu_vram_headroom" in decision(unified=False)["reasons"]


@pytest.mark.parametrize("available", [15, 25, 79])
def test_qwen_occupied_memory_is_not_admitted(available):
    result = decision(available)
    assert result["admission"] == "wait"
    assert "unified_memory_requires_80GiB_available" in result["reasons"]


def test_80gib_floor_does_not_replace_existing_projected_reserve():
    assert "ram_headroom" in decision(80)["reasons"]


def test_external_process_is_never_automatically_preempted():
    assert "external_gpu_workload_requires_handoff" in decision(external=True)["reasons"]


def test_telemetry_staleness_still_blocks():
    assert decision(stale=True)["admission"] == "wait"


def test_unsupported_gpu_or_weakened_policy_is_rejected():
    with pytest.raises(ValueError):
        validate_policy({"gpu_uuid": "other", "unified_memory": POLICY})
    weak = {**POLICY, "host_budget_bytes": GIB}
    with pytest.raises(ValueError):
        validate_policy({"gpu_uuid": EDGE_UUID, "unified_memory": weak})
