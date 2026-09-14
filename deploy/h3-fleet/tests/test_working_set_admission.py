import copy
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from working_set_admission import GIB, REQUIRED_STAT_FIELDS, conservative_working_set, projected_admission


def sample(current=64 * GIB, **stats):
    values = {"anon": 20 * GIB, "file": 40 * GIB, "inactive_file": 32 * GIB,
              "active_file": 8 * GIB, "file_dirty": 0, "file_writeback": 0, "unevictable": 0}
    values.update(stats)
    return {"memory_current": current, "memory_stat": values}


def test_clean_inactive_cache_discount_preserves_current_and_raw_counters():
    original = sample(workingset_refault_file=15, pgscan=200, pgsteal=180)
    before = copy.deepcopy(original)
    result = conservative_working_set(original)
    assert result["memory_current"] == 64 * GIB
    assert result["reclaimable_file"] == 32 * GIB
    assert result["effective_reclaimable"] == 16 * GIB
    assert result["effective_working_set"] == 48 * GIB
    assert result["reclaim_factor"] == result["applied_reclaim_factor"] == 0.5
    assert result["memory_stat"] == original["memory_stat"]
    assert result["admission"] == "allow" and result["reasons"] == []
    result["memory_stat"]["anon"] = 0
    assert original == before
    assert result["experimental"] and not result["reclaim_guaranteed"]


def test_dirty_writeback_and_unevictable_are_each_fully_subtracted():
    result = conservative_working_set(sample(file_dirty=4 * GIB, file_writeback=2 * GIB, unevictable=6 * GIB))
    assert result["reclaimable_file"] == 20 * GIB
    assert result["effective_reclaimable"] == 10 * GIB
    assert result["effective_working_set"] == 54 * GIB


def test_subtractions_clamp_to_zero_and_never_discount_anon_or_active_file():
    assert conservative_working_set(sample(file_dirty=40 * GIB))["reclaimable_file"] == 0
    result = conservative_working_set(sample(inactive_file=0, active_file=40 * GIB))
    assert result["effective_working_set"] == result["memory_current"]


def test_reclaimable_is_capped_by_file_even_if_inactive_counter_is_larger():
    result = conservative_working_set(sample(file=10 * GIB, inactive_file=20 * GIB))
    assert result["reclaimable_file"] == 10 * GIB


@pytest.mark.parametrize("factor,expected", [(0, 0), (0.25, 8 * GIB), (0.5, 16 * GIB), (1, 32 * GIB)])
def test_configurable_factor_including_zero_and_one(factor, expected):
    result = conservative_working_set(sample(), reclaim_factor=factor)
    assert result["effective_reclaimable"] == expected
    assert result["reclaim_factor"] == factor


def test_floor_is_conservative_even_for_counters_above_float_integer_precision():
    size = 2**56 + 3
    result = conservative_working_set(sample(current=size, file=size, inactive_file=size))
    assert result["effective_reclaimable"] == size // 2
    assert result["effective_working_set"] == size - size // 2


@pytest.mark.parametrize("swap,psi,reasons", [(True, True, ["swap_growing"]),
                                            (False, False, ["psi_not_stable"]),
                                            (True, False, ["swap_growing", "psi_not_stable"])])
def test_swap_or_psi_disables_discount_and_always_waits(swap, psi, reasons):
    result = conservative_working_set(sample(), swap_growing=swap, psi_stable=psi)
    assert result["reclaim_factor"] == 0.5 and result["applied_reclaim_factor"] == 0
    assert result["effective_reclaimable"] == 0
    assert result["effective_working_set"] == 64 * GIB
    assert result["admission"] == "wait" and result["reasons"] == reasons
    projected = projected_admission(sample(), 0, 0, 100 * GIB, swap_growing=swap, psi_stable=psi)
    assert projected["projected_within_limit"]
    assert projected["admission"] == "wait"


@pytest.mark.parametrize("field", REQUIRED_STAT_FIELDS)
def test_each_required_stat_is_required(field):
    current = sample()
    del current["memory_stat"][field]
    with pytest.raises(ValueError, match="memory_stat requires"):
        conservative_working_set(current)


@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None, float("nan"), float("inf")])
def test_invalid_required_and_optional_counters_reject(value):
    for field in ("anon", "workingset_refault_file", "pgscan"):
        with pytest.raises(ValueError):
            conservative_working_set(sample(**{field: value}))
    with pytest.raises(ValueError):
        conservative_working_set(sample(current=value))


@pytest.mark.parametrize("value", [None, {}, {"memory_stat": {}}, {"memory_current": 1},
                                  {"memory_current": 1, "memory_stat": []}])
def test_missing_or_malformed_sample_rejects(value):
    with pytest.raises(ValueError):
        conservative_working_set(value)


@pytest.mark.parametrize("factor", [-0.1, 1.01, True, None, "0.5", float("nan"), float("inf"), 10**1000])
def test_invalid_factor_raises_value_error(factor):
    with pytest.raises(ValueError):
        conservative_working_set(sample(), reclaim_factor=factor)


@pytest.mark.parametrize("kwargs", [{"swap_growing": 0}, {"psi_stable": 1}, {"psi_stable": None}])
def test_stability_flags_must_be_actual_booleans(kwargs):
    with pytest.raises(ValueError):
        conservative_working_set(sample(), **kwargs)


def test_impossible_negative_working_set_rejects_instead_of_clamping():
    with pytest.raises(ValueError, match="inconsistent statistics"):
        conservative_working_set(sample(current=GIB))


def test_projected_includes_candidate_remaining_peak_and_default_global_margin():
    result = projected_admission(sample(), candidate_budget=18 * GIB, reserved_running=4 * GIB, limit=72 * GIB)
    assert result["global_margin"] == 2 * GIB
    assert result["projected"] == 72 * GIB
    assert result["raw_projected"] == 88 * GIB
    assert result["admission"] == "allow"
    assert result["projected_within_limit"]


@pytest.mark.parametrize("excess", [1, GIB])
def test_exceeding_limit_waits_with_exact_boundary(excess):
    result = projected_admission(sample(), 18 * GIB + excess, 4 * GIB, 72 * GIB)
    assert result["admission"] == "wait"
    assert "projected_working_set_exceeds_limit" in result["reasons"]


def test_factor_zero_matches_raw_projection_and_margin_can_be_explicit():
    result = projected_admission(sample(), 8 * GIB, 0, 72 * GIB, global_margin=0, reclaim_factor=0)
    assert result["projected"] == result["raw_projected"] == 72 * GIB
    assert result["admission"] == "allow"
    assert projected_admission(sample(), 8 * GIB, 0, 72 * GIB, reclaim_factor=0)["admission"] == "wait"


@pytest.mark.parametrize("field", ["candidate_budget", "reserved_running", "limit", "global_margin"])
@pytest.mark.parametrize("value", [-1, True, 1.0, "2", None])
def test_invalid_projection_inputs_raise_value_error(field, value):
    kwargs = {"candidate_budget": 0, "reserved_running": 0, "limit": 72 * GIB, "global_margin": 0}
    kwargs[field] = value
    with pytest.raises(ValueError):
        projected_admission(sample(), **kwargs)


def test_zero_limit_is_not_a_valid_capacity():
    with pytest.raises(ValueError):
        projected_admission(sample(), 0, 0, 0)


def test_parent_cgroup_input_and_byte_suffixed_output_contract():
    current = sample()
    current["cgroup_current_bytes"] = current.pop("memory_current")
    result = conservative_working_set(current, reclaim_factor=0.5, swap_growing=False, psi_stable=True)
    assert result["effective_working_set_bytes"] == result["effective_working_set"] == 48 * GIB
    assert result["effective_reclaimable_bytes"] == result["effective_reclaimable"] == 16 * GIB
    assert result["reasons"] == []
    assert projected_admission(current, 18 * GIB, 4 * GIB, 72 * GIB)["admission"] == "allow"
    current["memory_current"] = current["cgroup_current_bytes"]
    assert conservative_working_set(current) == result


@pytest.mark.parametrize("value", [63 * GIB, -1, True, None, "68719476736", float(64 * GIB)])
def test_conflicting_or_invalid_parent_current_rejects(value):
    current = sample()
    current["cgroup_current_bytes"] = value
    with pytest.raises(ValueError):
        conservative_working_set(current)
