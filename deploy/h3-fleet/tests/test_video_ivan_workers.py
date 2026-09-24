"""Safety checks for the production Ivan worker pair."""

from scripts.serve_video_ivan_workers import GIB, safety_reason, supervise_workers


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


def test_failed_lane_restarts_without_touching_sibling():
    class Process:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

        def wait(self):
            return self.code

    main = Process(None)
    preview = Process(1)
    replacement = Process(None)
    processes = {"main": main, "preview": preview}
    restart_at, failures = {}, {"main": [], "preview": []}
    starts = []

    def launch(name):
        starts.append(name)
        return replacement

    supervise_workers(processes, restart_at, failures, launch, 100)
    assert processes == {"main": main, "preview": None}
    assert restart_at == {"preview": 115} and starts == []
    supervise_workers(processes, restart_at, failures, launch, 115)
    assert processes == {"main": main, "preview": replacement}
    assert starts == ["preview"]


def test_repeated_failed_lane_is_quarantined_without_stopping_sibling():
    class Process:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

        def wait(self):
            return self.code

    main = Process(None)
    processes = {"main": main, "preview": Process(1)}
    restart_at, failures = {}, {"main": [], "preview": []}
    starts = []

    def launch(name):
        starts.append(name)
        return Process(1)

    for now in (100, 115, 116, 131, 132, 147):
        supervise_workers(processes, restart_at, failures, launch, now)
    assert processes == {"main": main, "preview": None}
    assert restart_at == {"preview": float("inf")}
    assert starts == ["preview", "preview"]
