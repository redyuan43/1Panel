import copy
import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts/progressive_resource_policy.py"
SPEC = importlib.util.spec_from_file_location("progressive_resource_policy_under_test", PATH)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)
GIB = policy.GIB
RESIDUAL = 6 * GIB - 256 * 1024**2


def snapshot(stamp=100, host_swap=RESIDUAL, cgroup_swap=RESIDUAL):
    size = 16 * GIB
    return {"ok": True, "timestamp": stamp, "boot_id": "stable-boot",
            "memory_available_bytes": 32 * GIB, "cgroup_current_bytes": 40 * GIB,
            "root_available_bytes": 40 * GIB, "offload_available_bytes": 80 * GIB,
            "swap_used_bytes": host_swap, "cgroup_swap_bytes": cgroup_swap,
            "cgroup_events": {key: 0 for key in policy.EVENTS}, "swap_io_pages": {"pswpin": 10, "pswpout": 20},
            "memory_pressure": {key + suffix: 0 for key in policy.PRESSURE for suffix in ("_total", "_avg10")},
            "kernel_alerts": [], "gpus": [{"uuid": "GPU-tested", "temperature_c": 45}],
            "services": [{"Id": unit, "MainPID": str(1000 + index), "NRestarts": "0", "ActiveState": "active"}
                         for index, unit in enumerate(sorted(policy.UNITS))],
            "swap_inventory": {"observed_at": stamp, "page_size_bytes": 4096,
                               "proc_swaps": f"Filename Type Size Used Priority\n/dev/zram0 partition {(size - 4096) // 1024} {host_swap // 1024} 100\n",
                               "devices": [{"path": "/dev/zram0", "disksize_bytes": size, "backing_dev": "none\n",
                                            "mm_stat": f"{host_swap} {GIB} {2 * GIB} 0 {2 * GIB} 0 0 0 0\n",
                                            "bd_stat": "0 0 0\n"}]}}


def observe(guard, sample, active=False, **kwargs):
    return guard.observe(sample, active=active, now=sample["timestamp"], **kwargs)


def ready_guard():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        result = observe(guard, snapshot(stamp))
    assert result["admission"] == "allow" and not result["fatal"]
    return guard


def test_explicit_opt_in_only_generic_is_not_reinterpreted():
    for profile in (None, "generic", "strict", "residual", ""):
        with pytest.raises(ValueError, match="explicit opt-in"):
            policy.ProgressiveResourcePolicy(profile=profile)
    with pytest.raises(TypeError):
        policy.ProgressiveResourcePolicy()


def test_exact_60s_continuous_idle_baseline_and_recovery_pageins():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        sample = snapshot(stamp)
        sample["swap_io_pages"]["pswpin"] += stamp - 100
        original = copy.deepcopy(sample)
        result = observe(guard, sample)
        assert result["admission"] == ("allow" if stamp == 160 else "wait")
        assert result["stable_seconds"] == stamp - 100
        assert not result["fatal"] and sample == original
    assert result["baseline"]["timestamp"] == 100
    assert result["baseline"]["host_bytes"] == RESIDUAL
    assert result["inventory"]["physical_memory_bytes"] == 2 * GIB
    result["baseline"]["host_bytes"] = 0
    assert guard.baseline["host_bytes"] == RESIDUAL


def test_no_baseline_or_lease_hot_adoption_for_existing_running_task():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 201, 5):
        result = observe(guard, snapshot(stamp), active=True)
        assert result["admission"] == "wait" and result["baseline"] is None
        assert result["fatal"] is False


@pytest.mark.parametrize("source", ["pswpout", "host_some_total", "cgroup_full_total", "host_full_avg10", "swap_growth"])
def test_unstable_idle_window_waits_without_fatal(source):
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 131, 5):
        observe(guard, snapshot(stamp))
    sample = snapshot(135)
    if source == "pswpout":
        sample["swap_io_pages"][source] += 1
    elif source == "swap_growth":
        sample = snapshot(135, host_swap=RESIDUAL + 4096)
    else:
        sample["memory_pressure"][source] = 1
    result = observe(guard, sample)
    assert result["admission"] == "wait" and result["baseline"] is None
    assert not result["fatal"] and guard.candidate is None


@pytest.mark.parametrize("change", ["missing", "disk_used", "backing", "backing_io", "mm_stat", "inventory_missing_device",
                                   "host_mismatch", "stale", "size_mismatch", "duplicate"])
def test_inventory_fail_closed_before_and_during_execution(change):
    guard = ready_guard()
    sample = snapshot(165)
    inventory = sample["swap_inventory"]
    if change == "missing":
        del sample["swap_inventory"]
    elif change == "disk_used":
        inventory["proc_swaps"] += "/swap.img file 8388608 1024 -2\n"
    elif change == "backing":
        inventory["devices"][0]["backing_dev"] = "8:1"
    elif change == "backing_io":
        inventory["devices"][0]["bd_stat"] = "0 1 0"
    elif change == "mm_stat":
        inventory["devices"][0]["mm_stat"] = "unknown"
    elif change == "inventory_missing_device":
        inventory["devices"] = []
    elif change == "host_mismatch":
        sample["swap_used_bytes"] += GIB
    elif change == "stale":
        inventory["observed_at"] = 140
    elif change == "size_mismatch":
        inventory["devices"][0]["disksize_bytes"] = GIB
    else:
        inventory["devices"].append(copy.deepcopy(inventory["devices"][0]))
    idle = policy.ProgressiveResourcePolicy(profile="residual_zram")
    assert observe(idle, sample)["fatal"] is False
    result = observe(guard, sample, active=True)
    assert result["admission"] == "wait" and result["fatal"] is True


@pytest.mark.parametrize("change", ["boot", "pid", "restart", "inactive", "oom", "oom_kill", "max", "xid",
                                   "counter_reset", "gap", "temperature", "cgroup_ceiling"])
def test_running_hard_hazards_are_latched_fatal(change):
    guard = ready_guard()
    sample = snapshot(165)
    if change == "boot":
        sample["boot_id"] = "new-boot"
    elif change == "pid":
        sample["services"][0]["MainPID"] = "9999"
    elif change == "restart":
        sample["services"][0]["NRestarts"] = "1"
    elif change == "inactive":
        sample["services"][0]["ActiveState"] = "inactive"
    elif change in {"oom", "oom_kill", "max"}:
        sample["cgroup_events"][change] = 1
    elif change == "xid":
        sample["kernel_alerts"] = ["NVRM: Xid 31"]
    elif change == "counter_reset":
        sample["swap_io_pages"]["pswpin"] = 9
    elif change == "gap":
        sample = snapshot(180)
    elif change == "temperature":
        sample["gpus"][0]["temperature_c"] = 90
    else:
        sample["cgroup_current_bytes"] = 72 * GIB + 1
    result = observe(guard, sample, active=True)
    assert result["fatal"] and result["admission"] == "wait"
    assert observe(guard, snapshot(185), active=True)["fatal"]
    assert guard.baseline["host_bytes"] == RESIDUAL


@pytest.mark.parametrize("resource", ["ram", "reserved_ram", "reserved_cgroup", "disk", "root"])
def test_admission_headroom_shortage_does_not_cancel_owned_running_task(resource):
    guard = ready_guard()
    sample = snapshot(165)
    options = {}
    if resource == "ram":
        sample["memory_available_bytes"] = 15 * GIB
    elif resource == "reserved_ram":
        options["additional_memory_bytes"] = 17 * GIB
    elif resource == "reserved_cgroup":
        sample["memory_available_bytes"] = 100 * GIB
        options["additional_memory_bytes"] = 33 * GIB
    elif resource == "disk":
        options["additional_disk_bytes"] = 41 * GIB
    else:
        sample["root_available_bytes"] = 24 * GIB
    result = observe(guard, sample, active=True, **options)
    assert result["admission"] == "wait" and not result["fatal"]
    assert all(reason.startswith("admission_") for reason in result["reasons"])
    assert observe(guard, snapshot(170), active=True)["admission"] == "allow"


def test_swapout_without_psi_or_limit_breach_waits_but_does_not_cancel():
    guard = ready_guard()
    sample = snapshot(165)
    sample["swap_io_pages"]["pswpout"] += 1
    result = observe(guard, sample, active=True)
    assert result["admission"] == "wait" and not result["fatal"]
    assert "new_swapout" in result["reasons"]
    assert guard.baseline["timestamp"] == 100


def test_exact_growth_limit_and_absolute_limit_are_independent():
    guard = ready_guard()
    assert observe(guard, snapshot(165, RESIDUAL + GIB, RESIDUAL + GIB), active=True)["admission"] == "allow"
    result = observe(guard, snapshot(170, RESIDUAL + GIB + 4096, RESIDUAL + GIB + 4096), active=True)
    assert result["fatal"] and "swap_growth_limit" in result["reasons"]
    absolute = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        observe(absolute, snapshot(stamp, 7 * GIB, 7 * GIB))
    assert observe(absolute, snapshot(165, 8 * GIB, 8 * GIB), active=True)["admission"] == "allow"
    result = observe(absolute, snapshot(170, 8 * GIB + 4096, 8 * GIB + 4096), active=True)
    assert result["fatal"] and "absolute_swap_limit" in result["reasons"]


def test_cgroup_growth_limit_checked_even_when_host_total_unchanged():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        observe(guard, snapshot(stamp, 7 * GIB, GIB))
    result = observe(guard, snapshot(165, 7 * GIB, 2 * GIB + 4096), active=True)
    assert result["fatal"] and "swap_growth_limit" in result["reasons"]


def test_frozen_baseline_is_not_rebased_after_swap_decreases():
    guard = ready_guard()
    first = copy.deepcopy(guard.baseline)
    assert observe(guard, snapshot(165, 2 * GIB, 2 * GIB), active=True)["admission"] == "allow"
    assert observe(guard, snapshot(170, RESIDUAL, RESIDUAL), active=True)["admission"] == "allow"
    assert guard.baseline == first


def test_a_pair_of_samples_60s_apart_does_not_prove_stable_window():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    observe(guard, snapshot(100))
    result = observe(guard, snapshot(160))
    assert result["admission"] == "wait" and result["baseline"] is None
    assert not result["fatal"]


def test_profile_never_uses_production_swap_recovery_or_relaxes_hard_limits():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram", limits={"max_swap_gib": 100, "swap_hard_limit_gib": 100,
                                                                            "max_cgroup_gib": 100, "min_available_ram_gib": 1})
    assert guard.limits["max_cgroup_gib"] == 72
    assert guard.limits["min_available_ram_gib"] == 16
    sample = snapshot(100, 9 * GIB, 9 * GIB)
    sample["swap_recovery"] = {"ready": True, "baseline": {"host_bytes": 9 * GIB}}
    result = observe(guard, sample)
    assert result["absolute_swap_limit_bytes"] == 8 * GIB and result["swap_growth_limit_bytes"] == GIB
    assert result["admission"] == "wait" and result["baseline"] is None


def with_unused_disk(sample, path="/swap.img", kind="file", size_kib=8388608, used_kib=0, priority=-2):
    sample["swap_inventory"]["proc_swaps"] += f"{path} {kind} {size_kib} {used_kib} {priority}\n"
    return sample


@pytest.mark.parametrize("path,kind", [("/swap.img", "file"), ("/dev/nvme0n1p3", "partition")])
def test_configured_unused_disk_swap_is_allowed_without_claiming_no_disk_io(path, kind):
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        result = observe(guard, with_unused_disk(snapshot(stamp), path=path, kind=kind))
        assert not result["fatal"]
    assert result["admission"] == "allow"
    result = observe(guard, with_unused_disk(snapshot(165), path=path, kind=kind), active=True)
    assert result["admission"] == "allow" and not result["fatal"]
    assert result["inventory"]["configured_unused_disk_swap"] == [
        {"path": path, "type": kind, "size_bytes": 8 * GIB, "priority": -2, "used_bytes": 0}]
    assert result["inventory"]["disk_io_guaranteed_absent"] is False
    assert result["baseline"]["host_bytes"] == RESIDUAL


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("used_kib", [1, 4, 1024])
def test_any_disk_swap_use_is_rejected_even_below_page_rounding_tolerance(active, used_kib):
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        observe(guard, with_unused_disk(snapshot(stamp)))
    result = observe(guard, with_unused_disk(snapshot(165), used_kib=used_kib), active=active)
    assert result["admission"] == "wait" and result["fatal"] == active
    assert result["reasons"] == ["disk_swap_in_use"]
    if active:
        assert observe(guard, with_unused_disk(snapshot(170)), active=True)["fatal"]


@pytest.mark.parametrize("change", ["path", "type", "size", "priority", "removed", "added"])
def test_unused_disk_inventory_identity_is_frozen(change):
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        observe(guard, with_unused_disk(snapshot(stamp)))
    if change == "path":
        sample = with_unused_disk(snapshot(165), path="/different.img")
    elif change == "type":
        sample = with_unused_disk(snapshot(165), kind="partition")
    elif change == "size":
        sample = with_unused_disk(snapshot(165), size_kib=16777216)
    elif change == "priority":
        sample = with_unused_disk(snapshot(165), priority=-3)
    elif change == "removed":
        sample = snapshot(165)
    else:
        sample = with_unused_disk(with_unused_disk(snapshot(165)), path="/other.img")
    result = observe(guard, sample, active=True)
    assert result["fatal"] and "boot_production_pid_or_swap_topology_drift" in result["reasons"]


def with_unused_backed_zram(sample, used_kib=0, reads=1):
    sample["swap_inventory"]["proc_swaps"] += f"/dev/zram1 partition {GIB // 1024 - 4} {used_kib} 50\n"
    sample["swap_inventory"]["devices"].append(
        {"path": "/dev/zram1", "disksize_bytes": GIB, "backing_dev": "8:1",
         "mm_stat": "0 0 0 0 0 0 0", "bd_stat": f"0 {reads} 0"})
    return sample


def test_unused_backed_zram_is_not_mistaken_for_current_swap_use():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        result = observe(guard, with_unused_backed_zram(snapshot(stamp)))
    assert result["admission"] == "allow"
    result = observe(guard, with_unused_backed_zram(snapshot(165), used_kib=4), active=True)
    assert result["fatal"] and "zram_backing_present_or_unknown" in result["reasons"]


def test_new_backing_io_is_fatal_even_if_current_zram_used_is_zero():
    guard = policy.ProgressiveResourcePolicy(profile="residual_zram")
    for stamp in range(100, 161, 5):
        observe(guard, with_unused_backed_zram(snapshot(stamp)))
    result = observe(guard, with_unused_backed_zram(snapshot(165), reads=2), active=True)
    assert result["fatal"] and "zram_backing_counter_changed" in result["reasons"]


def pressure_snapshot(stamp, metric="host_full", value=1.0, total=1):
    sample = snapshot(stamp)
    sample["memory_pressure"][metric + "_avg10"] = value
    sample["memory_pressure"][metric + "_total"] = total
    return sample


@pytest.mark.parametrize("metric", policy.PRESSURE)
def test_mild_psi_and_one_microsecond_total_growth_only_wait(metric):
    guard = ready_guard()
    for stamp in range(165, 226, 5):
        sample = pressure_snapshot(stamp, metric, 0.12)
        sample["swap_io_pages"]["pswpin"] += 54
        result = observe(guard, sample, active=True)
        assert result["admission"] == "wait" and not result["fatal"]
        assert result["psi_windows"] == {}
    assert guard.baseline["timestamp"] == 100


@pytest.mark.parametrize("metric", policy.PRESSURE)
def test_exact_threshold_requires_both_fifteen_seconds_and_four_samples(metric):
    guard = ready_guard()
    threshold = 1.0 if metric.endswith("_full") else 10.0
    for stamp in (165, 170, 175):
        result = observe(guard, pressure_snapshot(stamp, metric, threshold), active=True)
        assert result["admission"] == "wait" and not result["fatal"]
    result = observe(guard, pressure_snapshot(180, metric, threshold), active=True)
    assert result["fatal"]
    assert result["reasons"] == ["sustained_memory_psi_pressure:" + metric]
    assert result["psi_windows"][metric] == {"since": 165, "samples": 4, "duration_seconds": 15}


def test_four_fast_samples_are_not_fifteen_seconds():
    guard = ready_guard()
    for stamp in (165, 166, 167, 168):
        result = observe(guard, pressure_snapshot(stamp), active=True)
        assert not result["fatal"]
    assert result["psi_windows"]["host_full"]["samples"] == 4
    assert result["psi_windows"]["host_full"]["duration_seconds"] == 3


def test_fifteen_seconds_with_two_samples_is_not_four_samples():
    guard = ready_guard()
    observe(guard, pressure_snapshot(165), active=True)
    result = observe(guard, pressure_snapshot(180), active=True)
    assert not result["fatal"] and result["psi_windows"]["host_full"]["samples"] == 2


def test_duplicate_timestamps_do_not_count_or_trigger_psi_fatal():
    guard = ready_guard()
    sample = pressure_snapshot(165)
    observe(guard, sample, active=True)
    for _ in range(20):
        result = observe(guard, copy.deepcopy(sample), active=True)
        assert not result["fatal"] and result["admission"] == "wait"
        assert result["psi_windows"]["host_full"]["samples"] == 1
    for stamp in (170, 175):
        assert not observe(guard, pressure_snapshot(stamp), active=True)["fatal"]
    assert observe(guard, pressure_snapshot(180), active=True)["fatal"]


def test_sampling_gap_resets_psi_window_but_retains_existing_telemetry_protection():
    guard = ready_guard()
    observe(guard, pressure_snapshot(165), active=True)
    observe(guard, pressure_snapshot(170), active=True)
    result = observe(guard, pressure_snapshot(190), active=True)
    assert result["fatal"] and result["psi_windows"] == {}
    assert "sampling_continuity_lost" in result["reasons"]
    assert not any(reason.startswith("sustained_memory_psi_pressure") for reason in result["reasons"])


def test_transient_high_psi_resets_duration_and_admission_needs_new_zero_window():
    guard = ready_guard()
    for stamp in (165, 170, 175):
        assert not observe(guard, pressure_snapshot(stamp), active=True)["fatal"]
    for stamp in range(180, 241, 5):
        result = observe(guard, pressure_snapshot(stamp, value=0), active=True)
        assert not result["fatal"] and result["psi_windows"] == {}
        assert result["admission"] == ("allow" if stamp == 240 else "wait")
        assert result["stable_seconds"] == stamp - 180
    assert guard.baseline["timestamp"] == 100
    result = observe(guard, pressure_snapshot(245), active=True)
    assert not result["fatal"] and result["psi_windows"]["host_full"]["samples"] == 1


def test_total_growth_with_zero_average_also_restarts_admission_window():
    guard = ready_guard()
    result = observe(guard, pressure_snapshot(165, value=0), active=True)
    assert result["admission"] == "wait" and not result["fatal"]
    for stamp in range(170, 231, 5):
        result = observe(guard, pressure_snapshot(stamp, value=0), active=True)
        assert result["admission"] == ("allow" if stamp == 230 else "wait")


def test_different_psi_metrics_cannot_pool_short_windows_into_fatal():
    guard = ready_guard()
    for stamp in (165, 170, 175, 180, 185):
        sample = snapshot(stamp)
        metric = "host_full" if stamp % 10 else "cgroup_full"
        sample["memory_pressure"][metric + "_avg10"] = 1
        result = observe(guard, sample, active=True)
        assert not result["fatal"]
        assert result["psi_windows"][metric]["samples"] == 1


def test_report_identifies_experimental_thresholds_not_hardware_certification():
    result = observe(ready_guard(), snapshot(165), active=True)
    assert result["experimental"] is True and result["hardware_certified"] is False
    assert result["policy"]["psi_fatal"] == {
        "full_avg10_percent": 1.0, "some_avg10_percent": 10.0, "sustained_seconds": 15,
        "minimum_unique_samples": 4, "maximum_sample_gap_seconds": 15, "per_metric": True}
    assert result["policy"]["psi_admission"] == {"avg10_percent": 0, "total_growth_us": 0, "stable_seconds": 60}
    assert result["absolute_swap_limit_bytes"] == 8 * GIB
    assert result["swap_growth_limit_bytes"] == GIB
