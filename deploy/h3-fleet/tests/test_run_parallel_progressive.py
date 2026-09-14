import copy
import json

import pytest

from test_run_parallel_comparison import prepared, sample
import run_parallel_comparison as parallel


def add_progressive(args, count=2):
    manifest = json.loads(args.manifest.read_bytes())
    if count == 1:
        manifest["tasks"] = manifest["tasks"][:1]
    if count == 3:
        manifest["tasks"].append(dict(manifest["tasks"][1], case="third", endpoint="http://127.0.0.1:19003",
                                     unit="parallel-third.service", gpu_uuid="GPU-33333333-1111-1111-1111-111111111111"))
    for owner in manifest["tasks"]:
        owner["vram_budget_mib"] = 11000
    args.manifest.write_bytes(parallel.encoded(manifest))
    args.manifest_sha256 = parallel.file_sha256(args.manifest)
    args.progressive = True
    args.timeout, args.poll_interval, args.cleanup_timeout = 1200, 5, 10
    return args


@pytest.mark.parametrize("count", [1, 2, 3])
def test_progressive_contract_and_default_legacy(prepared, count):
    assert prepared.fleet_url == parallel.FLEET
    assert not prepared.progressive
    add_progressive(prepared, count)
    report, _ = parallel.prepare(prepared)
    assert len(report["tasks"]) == count
    assert report["contract"]["sampler_observation_seconds"] == 60
    assert parallel.prepare(prepared)[0] == report
    prepared.progressive = False
    with pytest.raises(ValueError):
        parallel.prepare(prepared)


def test_working_set_configuration_and_static_budget_are_frozen(prepared):
    add_progressive(prepared)
    report, _ = parallel.prepare(prepared)
    assert report["contract"]["admission_config"]["reclaim_factor"] == 0.5
    assert report["contract"]["admission_config"]["global_safety_margin_bytes"] == 2 * parallel.GIB
    for owner in report["tasks"]:
        assert owner["peak_budget"]["fallback"]
        assert owner["peak_budget"]["candidate_budget_bytes"] == int(owner["memory_budget_gib"] * parallel.GIB)
        assert len(owner["profile_key"]) == 64
    prepared.reclaim_factor = 0
    with pytest.raises(RuntimeError, match="frozen contract changed"):
        parallel.prepare(prepared)


def test_history_file_must_be_pinned_and_runtime_explicit(prepared):
    add_progressive(prepared)
    prepared.peak_history = prepared.manifest
    with pytest.raises(ValueError, match="pinned SHA256"):
        parallel.prepare(prepared)
    prepared.peak_history_sha256 = prepared.manifest_sha256
    with pytest.raises(ValueError, match="runtime profile"):
        parallel.prepare(prepared)
    prepared.runtime_profile_id = "audited-core-and-weights-v1"
    prepared.peak_history_sha256 = "a" * 64
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        parallel.prepare(prepared)


@pytest.mark.parametrize("field,value", [("sampler_observation_seconds", 59.999), ("sampler_observation_seconds", float("nan")),
                                         ("poll_interval", 5.01), ("fleet_url", "http://127.0.0.1:8789/")])
def test_invalid_contract(prepared, field, value):
    add_progressive(prepared)
    setattr(prepared, field, value)
    with pytest.raises(ValueError):
        parallel.prepare(prepared)


@pytest.mark.parametrize("url", ["http://user:key@100.96.79.21:8789", "http://100.96.79.21:8789?key=secret",
                                "https://100.96.79.21:8789", "http://100.96.79.21:8789/path",
                                "http://100.96.79.22:8789", "http://127.0.0.1:8789#frag"])
def test_fleet_url_refuses_credentials_and_suffixes(url):
    with pytest.raises(ValueError):
        parallel.fleet_url(url)


def task(case="first", attempted=False):
    return {"case": case, "prompt_id": "owned-" + case, "shape": {"sampler_nodes": ["sampler"]},
            "gpu_uuid": case, "memory_budget_gib": 18, "disk_budget_gib": 0.2,
            "vram_budget_mib": 11000, "submit_attempted": attempted}


def progress_event(owner, value, node="sampler", maximum=4):
    return {"type": "progress", "data": {"prompt_id": owner["prompt_id"], "node": node, "value": value, "max": maximum}}


def test_progress_requires_exact_prompt_sampler_and_real_steps_over_60_seconds():
    owner = task()
    tracker = parallel.SamplerProgress(owner)
    tracker.connected = True
    tracker.receive({"type": "executing", "data": {"node": "sampler"}}, 0)
    tracker.receive(progress_event(task("foreign"), 1), 0)
    tracker.receive(progress_event(owner, 1, "loader"), 0)
    tracker.receive({"type": "progress", "data": {"value": 1, "max": 4}}, 0)
    assert not tracker.snapshot(100, 60)["ready"]
    tracker.receive(progress_event(owner, 1), 100)
    assert not tracker.snapshot(200, 60)["ready"]
    tracker.receive(progress_event(owner, 2), 160)
    assert tracker.snapshot(160, 60)["ready"]
    assert not tracker.snapshot(220.001, 60)["ready"]
    tracker.connected = False
    assert not tracker.snapshot(160, 60)["ready"]


@pytest.mark.parametrize("event", [
    {"type": "executing", "data": {"prompt_id": "owned-first", "node": "vae"}},
    {"type": "execution_cached", "data": {"prompt_id": "owned-first", "nodes": ["sampler"]}},
    progress_event(task(), 2), progress_event(task(), 4), progress_event(task(), 3, maximum=8),
])
def test_left_sampler_cached_complete_or_inconsistent_progress_cannot_admit(event):
    tracker = parallel.SamplerProgress(task())
    tracker.connected = True
    tracker.receive(progress_event(task(), 1), 0)
    tracker.receive(progress_event(task(), 2), 60)
    tracker.receive(event, 61)
    assert not tracker.snapshot(61, 60)["ready"]


def budget_sample(tasks, cgroup=55):
    return {**sample(), "cgroup_current_bytes": int(cgroup * parallel.GIB),
            "memory_stat": {key: 0 for key in ("anon", "file", "inactive_file", "active_file", "file_dirty", "file_writeback", "unevictable")},
            "worker_memory_bytes": {owner["case"]: parallel.GIB for owner in tasks},
            "gpu_memory": {owner["gpu_uuid"]: {"total_mib": 12288, "free_mib": 12000} for owner in tasks}}


def decide(tasks, candidate, current, initial=None, now=100, observed=0, receipts=None):
    initial = initial or {owner["case"]: parallel.GIB for owner in tasks}
    return parallel.progressive_admission(tasks, candidate, current, initial,
                                         {"reasons": [], "limits": parallel.LIMITS, "swap_growing": False,
                                          "psi_stable": True}, receipts or {}, now, 60, observed)


def test_reserves_only_candidate_not_all_three_but_keeps_remaining_peak():
    tasks = [task("first"), task("second"), task("third")]
    current = budget_sample(tasks, 44)
    assert decide(tasks, tasks[0], current)["admission"] == "allow"
    tasks[0]["submit_attempted"] = True
    current["cgroup_current_bytes"] = 59 * parallel.GIB
    current["worker_memory_bytes"]["first"] = 16 * parallel.GIB
    decision = decide(tasks, tasks[1], current, receipts={"first": {"reasons": []}})
    assert decision["reservation"]["remaining_peak_bytes"] == {"first": 3 * parallel.GIB}
    assert decision["projected_cgroup_bytes"] == 82 * parallel.GIB
    assert "admission_cgroup_remaining_peak_budget" in decision["reasons"]


def test_real_57_93_cgroup_waits_even_for_one_18_gib_candidate():
    tasks = [task("first"), task("second")]
    decision = decide(tasks, tasks[0], budget_sample(tasks, 57.93))
    assert decision["admission"] == "wait"
    assert decision["projected_cgroup_bytes"] > 72 * parallel.GIB


def test_collected_without_unload_keeps_reservation_and_ram_floor_is_prospective():
    tasks = [task("first", True), task("second")]
    tasks[0]["status"] = "collected"
    current = budget_sample(tasks, 30)
    current["memory_available_bytes"] = 40 * parallel.GIB
    decision = decide(tasks, tasks[1], current, receipts={"first": {"reasons": []}})
    assert "admission_host_remaining_peak_budget" in decision["reasons"]
    tasks[0]["unloaded"] = True
    assert decide(tasks, tasks[1], current)["admission"] == "allow"


def test_cannot_fit_4060_peak_on_12gb_or_skip_new_observation_window():
    tasks = [task("first"), task("second")]
    tasks[0]["vram_budget_mib"] = 13689
    assert "candidate_full_vram_budget_unavailable" in decide(tasks, tasks[0], budget_sample(tasks, 30))["reasons"]
    tasks[0]["vram_budget_mib"] = 11000
    assert decide(tasks, tasks[0], budget_sample(tasks, 30), now=59.999)["admission"] == "wait"
    assert decide(tasks, tasks[0], budget_sample(tasks, 30), now=60)["admission"] == "allow"


def test_peak_budget_overrun_is_fatal_not_smaller_remaining_budget():
    tasks = [task("first", True)]
    with pytest.raises(RuntimeError, match="peak memory budget exceeded"):
        parallel.remaining_reservation(tasks, None, {"first": 20 * parallel.GIB}, {"first": parallel.GIB})


def test_observed_peak_does_not_fall_with_current_usage():
    owners = [task("first", True), task("second")]
    result = parallel.remaining_reservation(owners, owners[1], {"first": 2 * parallel.GIB, "second": parallel.GIB},
                                            {"first": parallel.GIB, "second": parallel.GIB},
                                            {"first": 12 * parallel.GIB})
    assert result["remaining_peak_bytes"] == {"first": 6 * parallel.GIB}
    assert result["released_peak_regrowth_reserve_bytes"] == {"first": 11 * parallel.GIB}
    assert result["additional_memory_bytes"] == 35 * parallel.GIB


def test_released_peak_cannot_be_used_to_allow_unsafe_regrowth():
    owners = [task("first", True), task("second")]
    current = budget_sample(owners, 50)
    current["worker_memory_bytes"]["first"] = 2 * parallel.GIB
    decision = parallel.progressive_admission(
        owners, owners[1], current, {owner["case"]: parallel.GIB for owner in owners},
        {"reasons": [], "limits": parallel.LIMITS, "swap_growing": False, "psi_stable": True},
        {"first": {"reasons": []}}, 100, 60, 0, observed_peaks={"first": 16 * parallel.GIB})
    assert decision["reservation"]["remaining_peak_bytes"]["first"] == 2 * parallel.GIB
    assert decision["reservation"]["released_peak_regrowth_reserve_bytes"]["first"] == 15 * parallel.GIB
    assert decision["projected_cgroup_bytes"] == 87 * parallel.GIB
    assert decision["admission"] == "wait"


def test_below_baseline_release_keeps_full_absolute_future_peak():
    owners = [task("first", True), task("second")]
    current = budget_sample(owners, 32)
    current["worker_memory_bytes"]["first"] = 2 * parallel.GIB
    decision = parallel.progressive_admission(
        owners, owners[1], current, {"first": 10 * parallel.GIB, "second": parallel.GIB},
        {"reasons": [], "limits": parallel.LIMITS, "swap_growing": False, "psi_stable": True},
        {"first": {"reasons": []}}, 100, 60, 0, observed_peaks={"first": 16 * parallel.GIB})
    assert decision["reservation"]["reserved_running_bytes"] == 26 * parallel.GIB
    assert decision["projected_cgroup_bytes"] == 78 * parallel.GIB
    assert decision["admission"] == "wait"


@pytest.mark.parametrize("factor,expected", [(0.5, "allow"), (0, "wait")])
def test_clean_cache_with_running_reservation_and_all_other_gates(factor, expected):
    owners = [task("first", True), task("second")]
    current = budget_sample(owners, 51.6)
    current["memory_stat"].update(file=44 * parallel.GIB, inactive_file=44 * parallel.GIB)
    decision = parallel.progressive_admission(
        owners, owners[1], current, {owner["case"]: parallel.GIB for owner in owners},
        {"reasons": [], "limits": parallel.LIMITS, "swap_growing": False, "psi_stable": True},
        {"first": {"reasons": []}}, 100, 60, 0, reclaim_factor=factor)
    assert decision["admission"] == expected
    assert decision["reservation"]["remaining_peak_bytes"]["first"] == 18 * parallel.GIB
    assert decision["projected_cgroup_bytes"] == int(51.6 * parallel.GIB) - int(44 * factor * parallel.GIB) + 38 * parallel.GIB


@pytest.mark.parametrize("change,reason", [({"swap_growing": True}, "swap_growing"),
                                         ({"psi_stable": False}, "psi_not_stable"),
                                         ({"admission": "wait"}, "resource_policy_not_stable")])
def test_swap_or_psi_cannot_be_bypassed_by_cache(change, reason):
    owners = [task()]
    current = budget_sample(owners, 51.6)
    current["memory_stat"].update(file=44 * parallel.GIB, inactive_file=44 * parallel.GIB)
    policy = {"reasons": [], "limits": parallel.LIMITS, "swap_growing": False, "psi_stable": True, **change}
    decision = parallel.progressive_admission(owners, owners[0], current, {"first": parallel.GIB},
                                               policy, {}, 100, 60, 0)
    assert decision["admission"] == "wait"
    assert reason in decision["reasons"]
    if reason != "resource_policy_not_stable":
        assert decision["working_set"]["effective_reclaimable"] == 0


def test_missing_stat_fails_closed_without_exception_or_cache_discount():
    owners = [task()]
    current = budget_sample(owners, 51.6)
    del current["memory_stat"]
    result = decide(owners, owners[0], current)
    assert result["admission"] == "wait"
    assert "working_set_telemetry_unavailable" in result["reasons"]
    assert result["working_set"]["effective_reclaimable"] == 0


@pytest.mark.parametrize("flag,value", [("reclaim_factor", -0.1), ("reclaim_factor", 1.01),
                                       ("reclaim_factor", float("nan")), ("global_safety_margin_gib", 1.99),
                                       ("peak_safety_margin_gib", 1.99), ("peak_safety_margin_ratio", 0.099)])
def test_cannot_reduce_safety_margins_or_use_invalid_factor(prepared, flag, value):
    add_progressive(prepared)
    setattr(prepared, flag, value)
    with pytest.raises(ValueError):
        parallel.prepare(prepared)

class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Stop:
    def __init__(self, clock):
        self.clock, self.stopped = clock, False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.clock.sleep(seconds)
        return self.stopped


@pytest.fixture
def simulated(prepared, monkeypatch):
    import httpx
    from test_run_parallel_comparison import FakeHost

    def build(count=2, cgroup=8, fleet_url=parallel.FLEET, duration=310, progress_enabled=True):
        args = add_progressive(prepared, count)
        args.fleet_url = fleet_url
        report, graphs = parallel.prepare(args)
        clock = Clock()
        monkeypatch.setattr(parallel, "time", clock)
        report.update(started_at=clock.time(), status="preflight")
        state = {"lease": None, "draining": False, "requests": [], "submitted": {}, "terminal": set(),
                 "feeds": {}, "sample_hook": None, "unknown": False}
        host = FakeHost(report["tasks"])
        host.current["cgroup_current_bytes"] = int(cgroup * parallel.GIB)
        tasks = {owner["endpoint"]: owner for owner in report["tasks"]}

        class Feed:
            def __init__(self, owner):
                self.progress = parallel.SamplerProgress(owner)
                self.owner = owner
                state["feeds"][owner["case"]] = self

            def start(self):
                self.progress.connected = True

            def close(self):
                self.progress.connected = False

        def sample_host(since):
            clock.now += 0.01
            current = copy.deepcopy(host.current)
            current["timestamp"] = clock.time()
            current["memory_pressure"] = {key + suffix: 0 for key in ("host_some", "host_full", "cgroup_some", "cgroup_full")
                                          for suffix in ("_total", "_avg10")}
            current["swap_io_pages"] = {"pswpin": 0, "pswpout": 0}
            for case, started in state["submitted"].items():
                if progress_enabled and case not in state["terminal"]:
                    feed = state["feeds"][case]
                    value = min(4, int((clock.time() - started) // 75) + 1)
                    if value > feed.progress.value:
                        feed.progress.receive(progress_event(feed.owner, value), clock.monotonic())
            if state["sample_hook"]:
                state["sample_hook"](current)
            return current

        def inventory(identities):
            return {"swap_inventory": {}, "worker_memory_bytes": {case: parallel.GIB for case in identities},
                    "memory_stat": budget_sample([])["memory_stat"],
                    "gpu_memory": {owner["gpu_uuid"]: {"total_mib": 16384, "free_mib": 12000} for owner in report["tasks"]}}

        host.sample, host.progressive_inventory = sample_host, inventory
        monkeypatch.setattr(parallel, "SamplerFeed", Feed)

        def item(owner):
            return [0, owner["prompt_id"], graphs[owner["case"]], {"client_id": owner["client_id"]}, []]

        def transport(request):
            base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
            path, method = request.url.path, request.method
            body = json.loads(request.content) if request.content else None
            state["requests"].append((method, base + path, body))
            assert (request.headers.get("authorization") == "Bearer offline") == (base == fleet_url)
            owner = tasks.get(base)
            case = owner["case"] if owner else None
            if path == parallel.CAPACITY:
                value = {"active": [], "queues": [{"lane_id": lane, "queued_or_running": 0} for lane in parallel.WORKERS],
                         "validation_lease": state["lease"], "policy": {"resources": parallel.LIMITS}}
            elif path == parallel.OPTIONS:
                value = {"draining": state["draining"]}
            elif path == parallel.LEASE:
                if method == "DELETE":
                    assert report["both_unloaded"]
                    state["lease"] = None
                else:
                    assert state["lease"] is None or state["lease"]["owner"] == body["owner"]
                    state["lease"] = {"owner": body["owner"], "expires_at": clock.time() + body["ttl_seconds"]}
                value = {}
            elif path == "/api/router/drain":
                state["draining"] = True
                value = {}
            elif path == "/system_stats":
                value = {"system": {"comfyui_version": "0.34.0"}}
            elif path == "/queue":
                running = owner and case in state["submitted"] and case not in state["terminal"] and not state["unknown"]
                value = {"queue_running": [item(owner)] if running else [], "queue_pending": []}
            elif path == "/prompt":
                assert state["lease"] and state["draining"] and case not in state["submitted"]
                state["submitted"][case] = clock.time()
                if state["unknown"]:
                    raise httpx.ReadTimeout("unknown submission", request=request)
                value = {"prompt_id": owner["prompt_id"]}
            elif path.startswith("/history/"):
                if case not in state["submitted"] or state["unknown"] or (clock.time() - state["submitted"][case] < duration and case not in state["terminal"]):
                    value = {}
                else:
                    state["terminal"].add(case)
                    value = {owner["prompt_id"]: {"prompt": item(owner), "status": {"completed": True, "status_str": "success",
                              "messages": [["execution_start", {"timestamp": state["submitted"][case] * 1000}],
                                           ["execution_cached", {"nodes": []}], ["execution_success", {"timestamp": clock.time() * 1000}]]}}}
            elif path == "/free":
                assert state["lease"] and (case not in state["submitted"] or case in state["terminal"])
                value = {}
            elif path == "/interrupt":
                assert body == {"prompt_id": owner["prompt_id"]}
                state["terminal"].add(case)
                value = {}
            else:
                pytest.fail(f"unexpected request: {method} {base}{path}")
            return httpx.Response(200, json=value)

        fleet = httpx.Client(transport=httpx.MockTransport(transport), headers={"Authorization": "Bearer offline"})
        comfy = httpx.Client(transport=httpx.MockTransport(transport))
        coordinator = parallel.Coordinator(args, report, graphs, fleet, comfy, host)
        coordinator.stop = Stop(clock)
        monkeypatch.setattr(coordinator, "start_heartbeat", lambda: None)
        original_capacity = coordinator.capacity

        def capacity(owned=True):
            if owned and state["lease"] and state["lease"]["owner"] == report["owner"]:
                state["lease"]["expires_at"] = clock.time() + 120
            return original_capacity(owned)

        monkeypatch.setattr(coordinator, "capacity", capacity)
        monkeypatch.setattr(coordinator, "collect", lambda owner, record: owner.update(status="collected"))
        return coordinator, state, host, clock
    return build


@pytest.mark.parametrize("count", [2, 3])
def test_progressive_two_three_step_windows_one_lease_no_early_submit(simulated, count):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated(count)
    assert coordinator.execute() == 0, coordinator.report.get("error")
    submitted = list(state["submitted"].values())
    assert len(submitted) == count
    assert all(after - before >= 60 for before, after in zip(submitted, submitted[1:]))
    assert coordinator.report["peak_parallel"] == count
    assert coordinator.report["parallel_validated"] is True
    assert coordinator.report["both_unloaded"] and coordinator.report["lease_released"]
    assert {body["owner"] for _, _, body in writes(state, parallel.LEASE)} == {coordinator.report["owner"]}
    assert not writes(state, "/interrupt")
    saved = json.loads((coordinator.args.output_dir / "report.json").read_text())
    for owner in saved["tasks"]:
        accounting = owner["resource_reconciliation"]
        assert accounting["state"] == "finished"
        assert accounting["prediction"]["projected_cgroup_bytes"] == owner["admission"]["projected_cgroup_bytes"]
        assert accounting["aggregate_cgroup"]["peak_bytes"] > 0
        assert accounting["min_host_available_bytes"] > 0
        assert "aggregate_peak_minus_projected_bytes" in accounting["comparison"]
        assert not accounting["automatic_calibration"]


@pytest.mark.parametrize("reason", ["no_steps", "budget"])
def test_falls_back_to_queue_without_claiming_parallel_or_losing_outputs(simulated, reason):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated(progress_enabled=reason != "no_steps")
    if reason == "budget":
        coordinator.budget = 36 * parallel.GIB
        for owner in coordinator.tasks:
            owner["memory_budget_gib"] = 18
        state["sample_hook"] = lambda current: current.update(
            cgroup_current_bytes=(50 if not state["submitted"] or all(owner.get("unloaded") for owner in coordinator.tasks
                                                                     if owner["submit_attempted"]) else 51) * parallel.GIB)
    assert coordinator.execute() == 0, coordinator.report.get("error")
    assert len(state["submitted"]) == 2
    assert coordinator.report["parallel_validated"] is False
    assert coordinator.report["sampler_overlap_seconds"] == 0
    assert all(owner["status"] == "collected" for owner in coordinator.tasks)
    assert not writes(state, "/interrupt")


def test_first_budget_block_observable_without_submission(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated(cgroup=57.93)
    for owner in coordinator.tasks:
        owner["memory_budget_gib"] = 18
    coordinator.deadline = 1080
    assert coordinator.execute() == 1
    assert not writes(state, "/prompt")
    assert "admission_cgroup_remaining_peak_budget" in coordinator.tasks[0]["admission_reason"]
    assert (coordinator.args.output_dir / "admission.jsonl").stat().st_size > 0
    assert coordinator.report["lease_released"]


def test_old_single_lease_not_adopted_and_fleet_url_instance_scoped(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated(fleet_url="http://100.96.79.21:8789")
    state["lease"] = {"owner": "old-C05", "expires_at": 99999}
    assert coordinator.execute() == 1
    assert not writes(state)
    assert parallel.FLEET == "http://127.0.0.1:8789"
    assert coordinator.report["fleet_url"] == "http://100.96.79.21:8789"


def test_hard_oom_interrupts_only_owned_task_and_stops_admission(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated()
    state["sample_hook"] = lambda current: current["cgroup_events"].update(oom=int(bool(state["submitted"])))
    assert coordinator.execute() == 1
    assert len(writes(state, "/prompt")) == 1
    assert len(writes(state, "/interrupt")) == 1
    assert coordinator.report["lease_released"]
    accounting = coordinator.tasks[0]["resource_reconciliation"]
    assert accounting["cgroup_events"]["oom"]["delta"] == 1
    assert accounting["state"] == "finished"


def test_recovered_swap_sampling_peak_still_enforces_original_hard_limit(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, host, _ = simulated()
    original = host.progressive_inventory

    def inventory(identities):
        result = original(identities)
        result["swap_sampling_peak_host_bytes"] = 2 * parallel.GIB if state["submitted"] else 0
        return result

    host.progressive_inventory = inventory
    assert coordinator.execute() == 1
    assert len(writes(state, "/prompt")) == 1
    assert "swap limit crossed" in coordinator.report["error"]
    assert coordinator.tasks[0]["resource_reconciliation"]["swap"]["host"]["peak_bytes"] == 2 * parallel.GIB


def test_oom_and_lost_worker_identity_preserve_failed_sample(simulated, monkeypatch):
    coordinator, state, _, _ = simulated()
    original = coordinator.identities

    def identity(baseline=False):
        if state["submitted"]:
            raise RuntimeError("worker identity lost after OOM")
        return original(baseline)

    monkeypatch.setattr(coordinator, "identities", identity)
    state["sample_hook"] = lambda current: current["cgroup_events"].update(oom_kill=int(bool(state["submitted"])))
    assert coordinator.execute() == 1
    saved = json.loads((coordinator.args.output_dir / "report.json").read_text())
    assert saved["failed_resource_sample"]["cgroup_events"]["oom_kill"] == 1
    assert saved["tasks"][0]["resource_reconciliation"]["cgroup_events"]["oom_kill"]["delta"] == 1


def test_missing_cache_stats_only_waits_then_retries_after_completion(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, host, _ = simulated()
    inventory = host.progressive_inventory

    def sometimes_missing(identities):
        result = inventory(identities)
        if state["submitted"] and not all(owner.get("unloaded") for owner in coordinator.tasks if owner["submit_attempted"]):
            result["memory_stat"] = None
        return result

    host.progressive_inventory = sometimes_missing
    assert coordinator.execute() == 0, coordinator.report.get("error")
    assert len(state["submitted"]) == 2
    assert not writes(state, "/interrupt")
    assert coordinator.report["peak_parallel"] == 1
    assert coordinator.report["parallel_validated"] is False
    decisions = (coordinator.args.output_dir / "admission.jsonl").read_text()
    assert "working_set_telemetry_unavailable" in decisions


def test_ambiguous_submission_does_not_retry_add_lane_or_release_unknown_work(simulated):
    from test_run_parallel_comparison import writes
    coordinator, state, _, _ = simulated()
    state["unknown"] = True
    coordinator.deadline = 1200
    assert coordinator.execute() == 1
    assert len(writes(state, "/prompt")) == 1
    assert coordinator.report["status"] == "needs_reconciliation"
    assert not coordinator.report["lease_released"]


@pytest.mark.parametrize("stat_duration", [0.01, 1.01])
def test_raw_inventory_includes_unused_swapfile_and_only_reads_zram_sysfs(monkeypatch, stat_duration):
    from pathlib import Path
    from types import SimpleNamespace
    timestamps = iter([9, 10, 10 + stat_duration])
    monkeypatch.setattr(parallel, "time", SimpleNamespace(time=lambda: next(timestamps)))
    paths = {
        "/proc/meminfo": "SwapTotal: 16777216 kB\nSwapFree: 10747904 kB\n",
        "/proc/vmstat": "pswpin 10\npswpout 20\n",
        "/proc/swaps": "Filename Type Size Used Priority\n/dev/zram0 partition 16777212 6029312 100\n/swap.img file 8388608 0 -2\n",
        "/sys/block/zram0/disksize": str(16 * parallel.GIB),
        "/sys/block/zram0/backing_dev": "none\n",
        "/sys/block/zram0/mm_stat": "1 1 1 0 1 0 0\n",
        "/sys/block/zram0/bd_stat": "0 0 0\n",
        "/sys/fs/cgroup" + parallel.CGROUP + "/trial.service/memory.current": "1024",
        "/sys/fs/cgroup" + parallel.CGROUP + "/memory.current": "2048",
        "/sys/fs/cgroup" + parallel.CGROUP + "/memory.swap.current": "0",
        "/sys/fs/cgroup" + parallel.CGROUP + "/memory.stat": "anon 1024\nfile 0\ninactive_file 0\nactive_file 0\nfile_dirty 0\nfile_writeback 0\nunevictable 0\n",
    }
    monkeypatch.setattr(Path, "read_text", lambda path: paths[str(path)])
    monkeypatch.setattr(parallel, "command", lambda *args: "GPU-test, 12288, 11000\n")
    inventory = parallel.Host().progressive_inventory({"first": {"ControlGroup": parallel.CGROUP + "/trial.service"}})
    assert "/swap.img file" in inventory["swap_inventory"]["proc_swaps"]
    assert [device["path"] for device in inventory["swap_inventory"]["devices"]] == ["/dev/zram0"]
    assert inventory["worker_memory_bytes"] == {"first": 1024}
    assert inventory["gpu_memory"]["GPU-test"]["free_mib"] == 11000
    assert inventory["cgroup_current_before_stat_bytes"] == 2048
    assert inventory["cgroup_current_after_stat_bytes"] == 2048
    assert (inventory["memory_stat"] is not None) == (stat_duration <= 1)


def test_stat_bracket_uses_maximum_of_initial_before_and_after(simulated):
    coordinator, _, host, _ = simulated()
    original = host.progressive_inventory

    def bracket(identities):
        return dict(original(identities), cgroup_current_before_stat_bytes=9 * parallel.GIB,
                    cgroup_current_after_stat_bytes=10 * parallel.GIB)

    host.progressive_inventory = bracket
    coordinator.report["limits"] = parallel.LIMITS
    sampled = coordinator.progressive_monitor(baseline=True)
    assert sampled["cgroup_current_initial_sample_bytes"] == 8 * parallel.GIB
    assert sampled["cgroup_current_bytes"] == 10 * parallel.GIB


def test_residual_profile_integrates_real_policy_and_allows_only_quiet_admission(simulated, monkeypatch):
    import progressive_resource_policy
    from test_progressive_resource_policy import snapshot
    coordinator, state, host, clock = simulated()
    coordinator.args.progressive_resource_profile = "residual_zram"
    monkeypatch.setattr(progressive_resource_policy, "time", clock)
    original_inventory = host.progressive_inventory

    def inventory(identities):
        result = original_inventory(identities)
        result["swap_inventory"] = snapshot(clock.time())["swap_inventory"]
        result["swap_inventory"]["proc_swaps"] += "/swap.img file 8388608 0 -2\n"
        return result

    def residual(current):
        values = snapshot(clock.time())
        for key in ("cgroup_events", "swap_used_bytes", "cgroup_swap_bytes"):
            current[key] = values[key]
        if state["submitted"]:
            elapsed = clock.time() - min(state["submitted"].values())
            if 10 < elapsed < 15:
                current["memory_pressure"]["host_some_avg10"] = 0.12

    host.progressive_inventory = inventory
    state["sample_hook"] = residual
    assert coordinator.execute() == 0, coordinator.report.get("error")
    assert coordinator.report["resource_policy"]["baseline"]["host_bytes"] > 5 * parallel.GIB
    assert coordinator.report["resource_policy"]["inventory"]["disk_io_guaranteed_absent"] is False
    assert len(state["submitted"]) == 2
    assert all(after - before >= 60 for before, after in zip(list(state["submitted"].values()), list(state["submitted"].values())[1:]))


def test_vram_budget_missing_is_not_inferred_from_other_card(prepared):
    add_progressive(prepared)
    manifest = json.loads(prepared.manifest.read_bytes())
    del manifest["tasks"][1]["vram_budget_mib"]
    prepared.manifest.write_bytes(parallel.encoded(manifest))
    prepared.manifest_sha256 = parallel.file_sha256(prepared.manifest)
    with pytest.raises(ValueError, match="missing task fields"):
        parallel.prepare(prepared)
