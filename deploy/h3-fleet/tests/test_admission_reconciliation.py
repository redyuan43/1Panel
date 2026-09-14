import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import admission_reconciliation as reconciliation


def sample(timestamp=1):
    return {"ok": True, "timestamp": timestamp, "boot_id": "boot",
            "cgroup_current_bytes": 1000, "memory_available_bytes": 9000,
            "worker_memory_bytes": {"first": 200, "second": 100},
            "swap_used_bytes": 50, "cgroup_swap_bytes": 20,
            "swap_io_pages": {"pswpin": 10, "pswpout": 20},
            "cgroup_events": {key: 0 for key in reconciliation.EVENTS},
            "memory_pressure": {key + "_avg10": 0 for key in reconciliation.PRESSURE},
            "swap_inventory": {"page_size_bytes": 4096},
            "memory_stat": {"file": 600, "pgsteal": 100, "pgsteal_kswapd": 60, "pgsteal_direct": 40},
            "kernel_alerts": []}


def start(initial=None):
    return reconciliation.start_record({"projected_cgroup_bytes": 1500, "projected_available_bytes": 8500,
                                        "reservation": {"additional_memory_bytes": 500}},
                                       initial or sample(), "second", {"first": 100, "second": 100})


def test_peaks_deltas_predictions_and_no_input_mutation():
    initial = sample()
    original = copy.deepcopy(initial)
    record = start(initial)
    current = sample(2)
    current.update(cgroup_current_bytes=1700, memory_available_bytes=8100,
                   worker_memory_bytes={"first": 400, "second": 600}, swap_used_bytes=80, cgroup_swap_bytes=40)
    current["memory_stat"].update(pgsteal=103, file=550)
    current["memory_pressure"]["host_full_avg10"] = 1.5
    current["swap_io_pages"].update(pswpin=12, pswpout=23)
    assert reconciliation.observe(record, current) is record
    final_sample = copy.deepcopy(current)
    final_sample.update(timestamp=3, cgroup_current_bytes=1100, swap_used_bytes=40)
    reconciliation.observe(record, final_sample)
    final = reconciliation.finish(record)
    assert final["aggregate_cgroup"]["peak_bytes"] == 1700
    assert final["aggregate_cgroup"]["peak_delta_bytes"] == 700
    assert final["workers"]["first"]["peak_delta_bytes"] == 300
    assert final["workers"]["second"]["peak_delta_bytes"] == 500
    assert final["min_host_available_bytes"] == 8100
    assert final["psi_avg10_peak"]["host_full"] == 1.5
    assert final["swap"]["host"]["delta_bytes"] == -10
    assert final["swap"]["host"]["peak_delta_bytes"] == 30
    assert final["swap_io_pages"]["pswpout"]["delta"] == 3
    assert final["swap_io_delta_bytes"]["pswpin"] == 8192
    assert final["reclaim"]["actual_reclaimed_bytes"] == 12288
    assert final["file_drop_proxy"]["max_drop_bytes"] == 50
    assert not final["file_drop_proxy"]["is_actual_reclaim"]
    assert final["comparison"]["aggregate_peak_minus_projected_bytes"] == 200
    assert not final["automatic_calibration"] and not final["reclaim"]["task_attributable"]
    assert initial == original
    json.dumps(final, allow_nan=False)
    final["admission"]["reservation"]["additional_memory_bytes"] = 999
    assert record["admission"]["reservation"]["additional_memory_bytes"] == 500


def test_pgsteal_preferred_without_double_counting():
    record = start()
    current = sample(2)
    current["memory_stat"].update(pgsteal=105, pgsteal_kswapd=900, pgsteal_direct=900)
    reconciliation.observe(record, current)
    assert record["reclaim"]["source"] == "pgsteal"
    assert record["reclaim"]["actual_reclaimed_bytes"] == 5 * 4096


def test_split_counters_and_remote_page_size():
    initial = sample()
    del initial["memory_stat"]["pgsteal"]
    initial["swap_inventory"]["page_size_bytes"] = 65536
    record = start(initial)
    current = copy.deepcopy(initial)
    current["timestamp"] = 2
    current["memory_stat"].update(pgsteal_kswapd=65, pgsteal_direct=43)
    reconciliation.observe(record, current)
    assert record["reclaim"]["actual_reclaimed_bytes"] == 8 * 65536


@pytest.mark.parametrize("kind", ["absent", "one_split", "invalid_total", "missing_page", "conflicting_page"])
def test_unavailable_reclaim_is_null_not_file_drop(kind):
    initial = sample()
    if kind == "absent":
        initial["memory_stat"] = {"file": 600}
    elif kind == "one_split":
        initial["memory_stat"] = {"file": 600, "pgsteal_direct": 10}
    elif kind == "invalid_total":
        initial["memory_stat"]["pgsteal"] = "100"
    elif kind == "missing_page":
        initial.pop("swap_inventory")
    else:
        initial["page_size_bytes"] = 8192
    record = start(initial)
    current = copy.deepcopy(initial)
    current["timestamp"] = 2
    current["memory_stat"]["file"] = 100
    reconciliation.observe(record, current)
    assert record["reclaim"]["actual_reclaimed_bytes"] is None
    assert record["file_drop_proxy"]["max_drop_bytes"] == 500


@pytest.mark.parametrize("kind", ["reset", "missing", "source_change", "page_change"])
def test_counter_discontinuity_stays_invalid(kind):
    record = start()
    current = sample(2)
    if kind == "reset":
        current["memory_stat"]["pgsteal"] = 1
    elif kind == "missing":
        current["memory_stat"] = {}
    elif kind == "source_change":
        del current["memory_stat"]["pgsteal"]
    else:
        current["swap_inventory"]["page_size_bytes"] = 8192
    reconciliation.observe(record, current)
    current = sample(3)
    current["memory_stat"]["pgsteal"] = 110
    reconciliation.observe(record, current)
    assert record["reclaim"]["actual_reclaimed_bytes"] is None


def test_oom_xid_and_swap_counter_reset():
    record = start()
    current = sample(2)
    current["kernel_alerts"] = ["kernel: Out of memory: Killed process 123", "NVRM: Xid 79", "NVRM: Xid 79"]
    current["cgroup_events"]["oom_kill"] = 1
    current["swap_io_pages"]["pswpout"] = 1
    reconciliation.observe(record, current)
    assert record["cgroup_events"]["oom_kill"]["delta"] == 1
    assert len(record["kernel_alerts"]) == 2
    assert len(record["oom_alerts"]) == len(record["xid_alerts"]) == 1
    assert record["swap_io_delta_bytes"]["pswpout"] is None


def test_missing_worker_baseline_does_not_invent_delta():
    record = start()
    current = sample(2)
    current["worker_memory_bytes"] = {"new": 100}
    reconciliation.observe(record, current)
    assert record["workers"]["new"]["peak_delta_bytes"] is None
    assert record["workers"]["first"]["current_bytes"] is None
    assert "worker_baseline.new" in record["missing_fields"]


def test_reject_epoch_time_drift_and_finalized_observation():
    record = start()
    for current in (sample(), {**sample(2), "boot_id": "new"}):
        with pytest.raises(ValueError):
            reconciliation.observe(record, current)
    final = reconciliation.finish(record)
    assert reconciliation.finish(record) == final
    with pytest.raises(ValueError):
        reconciliation.observe(record, sample(2))


def test_parent_scalar_baseline_interface_and_reservation_baselines():
    admission = {"projected_cgroup_bytes": 1500,
                 "reservation": {"worker_initial_bytes": {"first": 100, "second": 100}}}
    record = reconciliation.start_record(admission, sample(), "second", 80)
    assert record["workers"]["first"]["peak_delta_bytes"] == 100
    assert record["workers"]["second"]["peak_delta_bytes"] == 20
    assert admission["reservation"]["worker_initial_bytes"]["second"] == 100
    without_mapping = reconciliation.start_record({}, sample(), "second", 80)
    assert without_mapping["workers"]["first"]["peak_delta_bytes"] is None


def test_failed_oom_sample_recorded_before_parent_hard_stop():
    record = reconciliation.start_record({}, sample(), "second", 100)
    failed = sample(2)
    failed["ok"] = False
    failed["kernel_alerts"] = ["OOM killed process 1", "NVRM: Xid 79"]
    failed["cgroup_events"]["oom_kill"] = 1
    failed.pop("memory_available_bytes")
    reconciliation.observe(record, failed)
    final = reconciliation.finish(record)
    assert final["failure_samples"] == [failed]
    assert final["cgroup_events"]["oom_kill"]["delta"] == 1
    assert final["oom_alerts"] == ["OOM killed process 1"]
    assert final["xid_alerts"] == ["NVRM: Xid 79"]
    assert "sample.ok" in final["missing_fields"]
    failed["kernel_alerts"].clear()
    assert final["failure_samples"][0]["kernel_alerts"]


def test_split_component_reset_cannot_be_hidden_by_total_growth():
    initial = sample()
    del initial["memory_stat"]["pgsteal"]
    record = start(initial)
    current = copy.deepcopy(initial)
    current["timestamp"] = 2
    current["memory_stat"].update(pgsteal_kswapd=1, pgsteal_direct=1000)
    reconciliation.observe(record, current)
    assert record["reclaim"]["actual_reclaimed_bytes"] is None


@pytest.mark.parametrize("missing_at_start", [True, False])
def test_none_memory_stat_is_unavailable_not_an_exception(missing_at_start):
    initial = sample()
    if missing_at_start:
        initial["memory_stat"] = None
    record = start(initial)
    current = sample(2)
    current["memory_stat"] = None
    current["cgroup_current_bytes"] = 1200
    reconciliation.observe(record, current)
    assert record["aggregate_cgroup"]["peak_bytes"] == 1200
    assert record["reclaim"]["actual_reclaimed_bytes"] is None
    assert "memory_stat.pgsteal_or_split_counters" in record["missing_fields"]
    assert "memory_stat.file" in record["missing_fields"]
    later = sample(3)
    later["memory_stat"]["pgsteal"] = 200
    reconciliation.observe(record, later)
    final = reconciliation.finish(record)
    assert final["reclaim"]["actual_reclaimed_bytes"] is None
    assert "not net file-cache release" in final["reclaim"]["measurement"]


def test_pgsteal_can_exceed_net_file_drop():
    record = start()
    current = sample(2)
    current["memory_stat"].update(pgsteal=1000, file=800)
    reconciliation.observe(record, current)
    assert record["reclaim"]["actual_reclaimed_bytes"] == 900 * 4096
    assert record["file_drop_proxy"]["max_drop_bytes"] == 0
    assert not record["reclaim"]["task_attributable"]
