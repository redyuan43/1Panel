import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import httpx
import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import run_parallel_comparison as parallel


ADAPTER = '''def validate_workflow(graph, case=None):
    if set(graph) != {"sampler", "save"}:
        raise ValueError("unknown graph")
    if graph["sampler"]["class_type"] != "SamplerCustomAdvanced":
        raise ValueError("unknown sampler")
    if graph["save"]["class_type"] != "SaveVideo":
        raise ValueError("unknown output")
    return {"width":480,"height":864,"length":362,"fps":24,"save_node":"save"}
'''


@pytest.fixture
def prepared(tmp_path):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(ADAPTER)
    tasks = []
    for offset, case in enumerate(("B8", "new3060"), 1):
        graph = {"sampler": {"class_type": "SamplerCustomAdvanced", "inputs": {"seed": 7980473147387766288}},
                 "save": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "original", "video": ["sampler", 0]}}}
        workflow = tmp_path / (case + ".json")
        workflow.write_bytes(parallel.encoded(graph))
        tasks.append({"case": case, "endpoint": f"http://127.0.0.1:{19000 + offset}",
                      "unit": f"parallel-{case}.service", "gpu_uuid": "GPU-" + str(offset) * 8 + "-1111-1111-1111-111111111111",
                      "workflow": workflow.name, "workflow_sha256": parallel.file_sha256(workflow),
                      "memory_budget_gib": 4, "disk_budget_gib": 1,
                      "validator": "strict", "validator_sha256": parallel.file_sha256(adapter)})
    manifest = tmp_path / "batch.json"
    manifest.write_bytes(parallel.encoded({"version": 1, "run_id": "parallel-offline", "tasks": tasks}))
    args = parallel.parser().parse_args(["--manifest", str(manifest), "--manifest-sha256", parallel.file_sha256(manifest),
                                       "--output-dir", str(tmp_path / "output"), "--validator", f"strict={adapter}:validate_workflow",
                                       "--poll-interval", "0.001", "--cleanup-timeout", "0.015", "--timeout", "1"])
    return args


def test_prepare_has_no_api_telemetry_or_gpu_and_preserves_graph(prepared, monkeypatch):
    monkeypatch.setattr(parallel.httpx, "Client", lambda **kwargs: pytest.fail("no API clients in preparation"))
    monkeypatch.setattr(parallel, "command", lambda *args: pytest.fail("no telemetry in preparation"))
    assert parallel.run(prepared) == 0
    report = json.loads((prepared.output_dir / "report.json").read_bytes())
    for task in report["tasks"]:
        original = json.loads(Path(task["workflow"]).read_bytes())
        result = json.loads((prepared.output_dir / task["case"] / "workflow.json").read_bytes())
        original["save"]["inputs"]["filename_prefix"] = task["prefix"]
        assert original == result
        assert result["sampler"]["inputs"]["seed"] == 7980473147387766288
    assert len({task["prompt_id"] for task in report["tasks"]}) == 2
    assert len({task["prefix"] for task in report["tasks"]}) == 2
    assert parallel.run(prepared) == 0


@pytest.mark.parametrize("kind", ["workflow", "adapter", "manifest"])
def test_sha_mismatch_fails_before_run(prepared, kind):
    manifest = json.loads(prepared.manifest.read_bytes())
    if kind == "workflow":
        path = prepared.manifest.parent / manifest["tasks"][0]["workflow"]
    elif kind == "adapter":
        path = prepared.manifest.parent / "adapter.py"
    else:
        path = prepared.manifest
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        parallel.prepare(prepared)


@pytest.mark.parametrize("field", ["endpoint", "unit", "gpu_uuid", "case"])
def test_distinct_workers_required(prepared, field):
    manifest = json.loads(prepared.manifest.read_bytes())
    manifest["tasks"][1][field] = manifest["tasks"][0][field]
    prepared.manifest.write_bytes(parallel.encoded(manifest))
    prepared.manifest_sha256 = parallel.file_sha256(prepared.manifest)
    with pytest.raises(ValueError, match="distinct"):
        parallel.prepare(prepared)


def test_unknown_graph_rejected_by_pinned_adapter(prepared):
    manifest = json.loads(prepared.manifest.read_bytes())
    task = manifest["tasks"][0]
    path = prepared.manifest.parent / task["workflow"]
    graph = json.loads(path.read_bytes())
    graph["unknown"] = {"class_type": "UnauditedNode", "inputs": {}}
    path.write_bytes(parallel.encoded(graph))
    task["workflow_sha256"] = parallel.file_sha256(path)
    prepared.manifest.write_bytes(parallel.encoded(manifest))
    prepared.manifest_sha256 = parallel.file_sha256(prepared.manifest)
    with pytest.raises(ValueError, match="unknown graph"):
        parallel.prepare(prepared)


def sample():
    return {"ok": True, "timestamp": time.time(), "boot_id": "boot", "kernel_alerts": [],
            "cgroup_events": {"oom": 0, "oom_kill": 0, "max": 0}, "memory_available_bytes": 64 * parallel.GIB,
            "cgroup_current_bytes": 8 * parallel.GIB, "swap_used_bytes": 0, "cgroup_swap_bytes": 0,
            "root_available_bytes": 100 * parallel.GIB, "offload_available_bytes": 100 * parallel.GIB,
            "gpus": [{"uuid": "gpu", "temperature_c": 40}],
            "services": [{"Id": f"comfyui-h3@{lane}.service", "MainPID": str(100 + offset),
                          "ActiveState": "active", "NRestarts": "0"} for offset, lane in enumerate(parallel.WORKERS)]}


class FakeHost:
    def __init__(self, tasks):
        self.current = sample()
        self.fleet = {"MainPID": "55", "start_ticks": "1"}
        self.workers = {task["case"]: {"MainPID": str(200 + offset), "start_ticks": "1"}
                        for offset, task in enumerate(tasks)}
        self.rows = []

    def sample(self, since):
        return copy.deepcopy(self.current)

    def fleet_identity(self):
        return copy.deepcopy(self.fleet)

    def production_identity(self):
        return {unit["Id"]: copy.deepcopy(unit) for unit in self.current["services"]}

    def worker_identity(self, task):
        return copy.deepcopy(self.workers[task["case"]])

    def allocations(self):
        return copy.deepcopy(self.rows)


@pytest.fixture
def execution(prepared, monkeypatch):
    report, graphs = parallel.prepare(prepared)
    report.update(status="preflight", started_at=time.time())
    host = FakeHost(report["tasks"])
    state = {"lease": None, "draining": False, "requests": [], "modes": {}, "submitted": set(),
             "terminal": set(), "history_calls": {}, "foreign": False, "resource_failure": False,
             "retain_vram": False, "cached": False, "overlap": True, "on_second_submit": None,
             "on_unknown_submit": None}
    tasks = {task["endpoint"]: task for task in report["tasks"]}

    def item(task):
        return [0, task["prompt_id"], graphs[task["case"]], {"client_id": task["client_id"]}, []]

    def transport(request):
        body = json.loads(request.content) if request.content else None
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        path, method = request.url.path, request.method
        state["requests"].append((method, base + path, body))
        if base == parallel.FLEET:
            assert request.headers.get("authorization") == "Bearer offline"
        else:
            assert "authorization" not in request.headers
        if path == parallel.CAPACITY:
            return httpx.Response(200, json={"active": [], "queues": [{"lane_id": lane, "queued_or_running": 0}
                                                                        for lane in parallel.WORKERS],
                                             "validation_lease": state["lease"], "policy": {"resources": parallel.LIMITS}})
        if path == parallel.OPTIONS:
            return httpx.Response(200, json={"draining": state["draining"]})
        if path == parallel.LEASE:
            if method == "DELETE":
                assert report["both_unloaded"]
                assert all(task.get("reconciliation") for task in report["tasks"])
                state["lease"] = None
            else:
                assert state["lease"] is None or state["lease"]["owner"] == body["owner"]
                state["lease"] = {"owner": body["owner"], "expires_at": time.time() + body["ttl_seconds"]}
            return httpx.Response(200, json={"ok": True})
        if path == "/api/router/drain":
            state["draining"] = True
            return httpx.Response(200, json={"draining": True})
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.34.0"}})
        task = tasks.get(base)
        case = task["case"] if task else None
        mode = state["modes"].get(case, "success")
        if path == "/queue" and method == "GET":
            running = []
            if task and case in state["submitted"] and case not in state["terminal"] and mode not in {"400", "unknown"}:
                running = [item(task)]
            if task and state["foreign"] and len(state["submitted"]) == 2:
                running = [[0, "foreign", {}, {}, []]]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if path == "/free":
            assert state["draining"] and state["lease"]
            if state["retain_vram"] and state["submitted"]:
                host.rows = [{"pid": host.workers[case]["MainPID"], "gpu_uuid": task["gpu_uuid"], "mib": 1025}]
            return httpx.Response(200)
        if path == "/prompt":
            assert task and state["draining"] and state["lease"]
            assert case not in state["submitted"]
            assert body["prompt_id"] == task["prompt_id"] and body["client_id"] == task["client_id"]
            assert body["prompt"] == graphs[case] and "h3" not in body["extra_data"]
            state["submitted"].add(case)
            if len(state["submitted"]) == 2 and state["resource_failure"]:
                host.current["cgroup_events"]["oom"] = 1
            if len(state["submitted"]) == 2 and state["on_second_submit"]:
                state["on_second_submit"]()
            if mode == "400":
                return httpx.Response(400, json={"error": {"type": "validation_failed"}})
            if mode == "unknown":
                if state["on_unknown_submit"]:
                    state["on_unknown_submit"]()
                raise httpx.ReadTimeout("unknown submission", request=request)
            return httpx.Response(200, json={"prompt_id": task["prompt_id"]})
        if path == "/interrupt":
            assert task and body == {"prompt_id": task["prompt_id"]}
            state["terminal"].add(case)
            return httpx.Response(200)
        if path == "/view":
            assert request.url.params["filename"].endswith(".mp4")
            return httpx.Response(200, content=b"offline-video")
        if path.startswith("/history/"):
            state["history_calls"][case] = state["history_calls"].get(case, 0) + 1
            if case not in state["submitted"] or mode in {"400", "unknown"}:
                return httpx.Response(200, json={})
            if mode == "running" and case not in state["terminal"]:
                return httpx.Response(200, json={})
            start = 1000 if state["overlap"] or case == "B8" else 4000
            state["terminal"].add(case)
            parent, prefix = task["prefix"].rsplit("/", 1)
            return httpx.Response(200, json={task["prompt_id"]: {
                "outputs": {"save": {"images": [{"filename": prefix + "_00001_.mp4", "subfolder": parent, "type": "output"}]}},
                "prompt": item(task), "status": {"completed": True, "status_str": "success", "messages": [
                    ["execution_start", {"timestamp": start}],
                    ["execution_cached", {"nodes": ["sampler"] if state["cached"] else []}],
                    ["execution_success", {"timestamp": start + 2000}]]}}})
        pytest.fail(f"unexpected request {method} {base}{path}")

    fleet = httpx.Client(transport=httpx.MockTransport(transport), headers={"Authorization": "Bearer offline"})
    comfy = httpx.Client(transport=httpx.MockTransport(transport))
    coordinator = parallel.Coordinator(prepared, report, graphs, fleet, comfy, host)
    monkeypatch.setattr(coordinator, "collect", lambda task, record: task.update(status="collected"))
    yield coordinator, state, host
    fleet.close()
    comfy.close()


def writes(state, suffix=None):
    return [request for request in state["requests"] if request[0] in {"POST", "DELETE"}
            and (suffix is None or request[1].endswith(suffix))]


def test_two_tasks_share_one_lease_and_release_only_after_both_unload(execution):
    coordinator, state, _ = execution
    assert coordinator.execute() == 0
    assert len(writes(state, "/prompt")) == 2
    owners = {body["owner"] for _, _, body in writes(state, parallel.LEASE)}
    assert owners == {coordinator.report["owner"]}
    assert coordinator.report["peak_parallel_running"] == 2
    assert coordinator.report["execution_overlap_seconds"] == 2
    assert all(task["execution"]["cached_nodes"] == [] for task in coordinator.tasks)
    assert state["draining"] and state["lease"] is None
    assert not writes(state, "/api/router/resume")
    events = writes(state)
    assert events[-1][0] == "DELETE"
    assert sum(url.endswith("/free") for _, url, _ in events) == 4


def test_full_artifact_collection_offline(execution, monkeypatch):
    coordinator, _, _ = execution
    monkeypatch.setattr(coordinator, "collect", parallel.Coordinator.collect.__get__(coordinator))
    probe = {"streams": [{"codec_type": "video", "width": 480, "height": 864,
                          "avg_frame_rate": "24/1", "duration": str(362 / 24), "nb_frames": "362", "nb_read_frames": "362"},
                         {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
    monkeypatch.setattr(parallel, "command", lambda *args: json.dumps(probe) if args[0] == "ffprobe" else pytest.fail("unexpected subprocess"))
    assert coordinator.execute() == 0
    for task in coordinator.tasks:
        directory = coordinator.args.output_dir / task["case"]
        assert {"workflow.json", "input-workflow.json", "history.json", "video.mp4", "media-probe.json"} <= {path.name for path in directory.iterdir()}
        assert task["artifact_sha256"] == parallel.sha(b"offline-video")
        assert task["media"]["frame_count"] == 362
    assert (coordinator.args.output_dir / "metrics.jsonl").stat().st_size > 0


def test_active_a8_lease_causes_zero_writes(execution):
    coordinator, state, _ = execution
    state["lease"] = {"owner": "h3val_aaaaaaaaaaaaaaaa", "expires_at": time.time() + 120}
    assert coordinator.execute() == 1
    assert writes(state) == []
    assert state["lease"]["owner"] == "h3val_aaaaaaaaaaaaaaaa"


def test_existing_drain_not_adopted(execution):
    coordinator, state, _ = execution
    state["draining"] = True
    assert coordinator.execute() == 1
    assert writes(state) == []


@pytest.mark.parametrize("field", ["swap_used_bytes", "cgroup_swap_bytes"])
def test_absolute_swap_above_one_gib_never_uses_recovery(execution, field):
    coordinator, state, host = execution
    host.current[field] = parallel.GIB + 1
    host.current["swap_recovery"] = {"ready": True, "exclusive_single": True}
    assert coordinator.execute() == 1
    assert writes(state) == []


def test_exact_one_gib_swap_allowed(execution):
    coordinator, _, host = execution
    host.current["swap_used_bytes"] = parallel.GIB
    assert coordinator.execute() == 0


def test_combined_ram_reservation_before_any_writes(execution):
    coordinator, state, host = execution
    host.current["memory_available_bytes"] = 20 * parallel.GIB
    assert coordinator.execute() == 1
    assert writes(state) == []


def test_oom_cancels_only_both_owned_prompts(execution):
    coordinator, state, _ = execution
    state["resource_failure"] = True
    state["modes"] = {"B8": "running", "new3060": "running"}
    assert coordinator.execute() == 1
    assert "memory event" in coordinator.report["error"]
    interrupted = {body["prompt_id"] for _, _, body in writes(state, "/interrupt")}
    assert interrupted == {task["prompt_id"] for task in coordinator.tasks}
    assert coordinator.report["lease_released"]
    assert state["draining"]


def test_explicit_400_reconciles_other_owned_work(execution):
    coordinator, state, _ = execution
    state["modes"] = {"B8": "running", "new3060": "400"}
    assert coordinator.execute() == 1
    assert coordinator.report["lease_released"]
    assert len(writes(state, "/interrupt")) == 1
    assert writes(state, "/interrupt")[0][2]["prompt_id"] == coordinator.tasks[0]["prompt_id"]


@pytest.mark.parametrize("expire_after,expected_submissions", [("unknown_first", 1), ("submitted_second", 2)])
def test_unknown_submission_never_retried_keeps_lease_and_drain(execution, monkeypatch, expire_after, expected_submissions):
    coordinator, state, _ = execution
    state["modes"] = {"B8": "unknown", "new3060": "running"}
    clock = SimpleNamespace(now=100.0)

    def advance(seconds):
        clock.now += seconds

    def expire():
        clock.now = coordinator.deadline

    monkeypatch.setattr(parallel, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock.now, sleep=advance))
    coordinator.deadline = clock.now + 1
    state["on_unknown_submit" if expire_after == "unknown_first" else "on_second_submit"] = expire
    assert coordinator.execute() == 1
    submitted_ids = [body["prompt_id"] for _, _, body in writes(state, "/prompt")]
    assert submitted_ids == [task["prompt_id"] for task in coordinator.tasks[:expected_submissions]]
    assert len(submitted_ids) == len(set(submitted_ids))
    assert "deadline" in coordinator.report["error"]
    assert coordinator.tasks[0]["status"] == "submission_unknown"
    assert coordinator.tasks[1]["submit_attempted"] == (expected_submissions == 2)
    interrupted_ids = [body["prompt_id"] for _, _, body in writes(state, "/interrupt")]
    assert interrupted_ids == ([coordinator.tasks[1]["prompt_id"]] if expected_submissions == 2 else [])
    assert coordinator.report["status"] == "needs_reconciliation"
    assert coordinator.report["lease_released"] is False
    assert not any(method == "DELETE" for method, _, _ in writes(state))
    assert state["draining"] and state["lease"]["owner"] == coordinator.report["owner"]
    assert coordinator.report["retained_lease"]["owner"] == coordinator.report["owner"]


def test_foreign_queue_prevents_any_cancel_or_release(execution):
    coordinator, state, _ = execution
    state["foreign"] = True
    assert coordinator.execute() == 1
    assert coordinator.report["status"] == "needs_reconciliation"
    assert not writes(state, "/interrupt")
    assert not any(method == "DELETE" for method, _, _ in writes(state))


def test_unload_over_1024_retains_lease(execution):
    coordinator, state, _ = execution
    state["retain_vram"] = True
    assert coordinator.execute() == 1
    assert coordinator.report["status"] == "needs_reconciliation"
    assert not any(method == "DELETE" for method, _, _ in writes(state))


@pytest.mark.parametrize("flag", ["cached", "overlap"])
def test_sampler_cache_or_no_overlap_not_a_parallel_success(execution, flag):
    coordinator, state, _ = execution
    state[flag] = flag == "cached"
    assert coordinator.execute() == 1
    assert coordinator.report["status"] == "failed"
    assert coordinator.report["lease_released"]


@pytest.mark.parametrize("change", ["restart", "ram_growth", "swap_growth", "oom", "missing_worker"])
def test_runtime_resource_gates(change):
    baseline = sample()
    current = copy.deepcopy(baseline)
    if change == "restart":
        current["services"][0]["MainPID"] = "999"
    elif change == "missing_worker":
        current["services"].pop()
    elif change == "ram_growth":
        current["cgroup_current_bytes"] += 9 * parallel.GIB
    elif change == "swap_growth":
        current["cgroup_swap_bytes"] = parallel.GIB + 1
    else:
        current["cgroup_events"]["oom"] = 1
    with pytest.raises(RuntimeError):
        parallel.resource_check(current, baseline, parallel.LIMITS, 8 * parallel.GIB, 2 * parallel.GIB)


@pytest.mark.parametrize("field", ["endpoint", "unit", "submitted_workflow_sha256", "submit_attempted"])
def test_prepared_receipt_tampering_rejected(prepared, field):
    report, _ = parallel.prepare(prepared)
    report["tasks"][0][field] = True if field == "submit_attempted" else "changed"
    parallel.save(prepared.output_dir / "report.json", report)
    with pytest.raises(RuntimeError, match="changed"):
        parallel.prepare(prepared)


def test_existing_attempt_cannot_be_resubmitted(prepared):
    report, _ = parallel.prepare(prepared)
    report["status"] = "running"
    parallel.save(prepared.output_dir / "report.json", report)
    with pytest.raises(RuntimeError, match="never resubmit"):
        parallel.prepare(prepared)


@pytest.mark.parametrize("changed", ["fleet", "worker", "production"])
def test_identity_change_retains_lease_no_blind_interrupt(execution, changed):
    coordinator, state, host = execution
    state["modes"] = {"B8": "running", "new3060": "running"}

    def change():
        if changed == "fleet":
            host.fleet["MainPID"] = "888"
        elif changed == "worker":
            host.workers["B8"]["MainPID"] = "888"
        else:
            host.current["services"][0]["MainPID"] = "888"

    state["on_second_submit"] = change
    assert coordinator.execute() == 1
    assert coordinator.report["status"] == "needs_reconciliation"
    assert not writes(state, "/interrupt")
    assert not any(method == "DELETE" for method, _, _ in writes(state))


def test_renewal_keeps_same_owner_and_ttl(execution):
    coordinator, state, _ = execution
    coordinator.report["limits"] = parallel.LIMITS
    coordinator.monitor(baseline=True)
    state["lease"] = {"owner": coordinator.report["owner"], "expires_at": time.time() + 120}
    state["draining"] = True
    coordinator.renew()
    assert writes(state, parallel.LEASE)[-1][2] == {"owner": coordinator.report["owner"], "ttl_seconds": 120}
    state["draining"] = False
    with pytest.raises(RuntimeError, match="drain lost"):
        coordinator.renew()
    assert len(writes(state, parallel.LEASE)) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_heartbeat_interval_and_failure_signal(execution, monkeypatch, failure):
    coordinator, _, _ = execution
    waits, renewals = [], []

    def wait(seconds):
        waits.append(seconds)
        return len(waits) > 1

    def renew():
        renewals.append(True)
        if failure:
            raise RuntimeError("offline renewal failure")

    monkeypatch.setattr(coordinator.heartbeat_stop, "wait", wait)
    monkeypatch.setattr(coordinator, "renew", renew)
    coordinator.start_heartbeat()
    coordinator.heartbeat.join(timeout=1)
    assert not coordinator.heartbeat.is_alive()
    assert waits[0] == 40
    assert renewals == [True]
    assert coordinator.stop.is_set() == failure
