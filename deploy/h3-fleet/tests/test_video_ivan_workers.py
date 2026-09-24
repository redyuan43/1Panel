"""Safety checks for the production Ivan worker pair."""

from scripts.serve_video_ivan_workers import GIB, safety_reason


def sample():
    return {
        "available": 42 * GIB,
        "root_free": 26 * GIB,
        "data_free": 45 * GIB,
        "swap_used": GIB // 5,
        "cgroup_swap_used": 0,
        "cgroup_events": {"oom": 0, "oom_kill": 0, "max": 0},
        "gpus": {"gpu": {"temperature_c": 54}},
    }


def test_other_services_swap_do_not_kill_healthy_workers():
    baseline = sample()
    current = {**baseline, "swap_used": baseline["swap_used"] + 2 * GIB}
    assert safety_reason(current, baseline) is None


def test_owned_memory_pressure_still_stops_workers():
    baseline = sample()
    assert safety_reason({**baseline, "cgroup_swap_used": GIB + 1}, baseline) == "worker_swap_growth"
    assert safety_reason({**baseline, "available": 16 * GIB - 1}, baseline) == "host_ram_floor"
    assert safety_reason({**baseline, "swap_used": 8 * GIB + 1}, baseline) == "host_swap_ceiling"
    assert safety_reason({**baseline, "cgroup_events": {"oom": 0, "oom_kill": 0, "max": 1}}, baseline) == "worker_memory_event"
