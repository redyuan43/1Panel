import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("recipe_batch_validation", ROOT / "scripts/validate_recipe_batch.py")
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def task(start, end, *, cached=False, status="completed", media=True):
    return {"status": status, "media_validation": {"ok": media},
            "sampler_progress": {"cached": cached, "sampler_intervals": [{"started_at": start, "finished_at": end}]}}


def test_serial_results_are_never_parallel_success():
    result = validator.sampler_overlap([task(1, 10), task(10, 20), task(20, 30)])
    assert result["peak_parallel"] == 1
    assert result["parallel_validated"] is False


def test_real_sampler_spans_not_whole_job_envelopes_define_overlap():
    result = validator.sampler_overlap([task(1, 30), task(10, 30), task(20, 30)])
    assert result["peak_parallel"] == 3
    assert result["parallel_validated"] is True


def test_cached_or_missing_media_does_not_qualify_parallel():
    assert not validator.sampler_overlap([task(1, 20), task(1, 20, cached=True)])["parallel_validated"]
    assert not validator.sampler_overlap([task(1, 20), task(1, 20, media=False)])["parallel_validated"]


def test_dry_run_has_no_network_gpu_or_filesystem_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(validator.httpx, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError("network forbidden")))
    output = tmp_path / "dry-run"
    report = validator.run(argparse.Namespace(batch="single", output_dir=output, execute=False))
    assert report["status"] == "dry_run"
    assert report["recipes"] == ["A4_C0"]
    assert not report["parallel_validated"]
    assert not output.exists()


@pytest.fixture
def smoke_runtime(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(validator, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now,
                                                         sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    original_relative = Path.is_relative_to
    monkeypatch.setattr(Path, "is_relative_to", lambda path, other: False if str(other) in {"/tmp", "/var/tmp"}
                        else original_relative(path, other))
    monkeypatch.setattr(validator, "router_key", lambda path: "offline-key")
    monkeypatch.setattr(validator, "check_backend_identity", lambda binding: None)
    monkeypatch.setattr(validator, "probe_video", lambda path: pytest.fail("smoke must not decode/download a video"))
    state = {"leases": {8791: None, 8789: None}, "jobs": {}, "calls": [], "submitted": 0,
             "lab_lease_failure": False, "unknown_at": None, "wrong_identity": False,
             "cancel_failure": False, "backend_busy": False, "unload_failure": False,
             "sampler_cached": False, "foreign_progress": False, "oom": False, "xid": False,
             "missing_counters": False, "job_error": False, "completed_early": False}
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Original 15 second prompt.\n")
    args = argparse.Namespace(batch="single", output_dir=tmp_path / "evidence", execute=True,
                              fleet_url="http://127.0.0.1:8791", production_fleet_url="http://127.0.0.1:8789",
                              key_file=tmp_path / "key", prompt_file=prompt, seed=123,
                              timeout=180, smoke_seconds=60, cleanup_timeout=15)
    graph = {"6": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
        "width": 480, "height": 864, "length": 362, "audio_mode": "native", "task_type": "T2VA", "prompt": prompt.read_text()}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
        "12": {"class_type": "CreateVideo", "inputs": {"fps": 24}}}

    def read_job(job):
        result = copy.deepcopy(job)
        if state["wrong_identity"]:
            result["execution_id"] = "another_operation"
        if state["job_error"]:
            job["status"] = result["status"] = "error"
        if state["completed_early"]:
            job["status"] = result["status"] = "completed"
        upstream = job["upstream_prompt_id"]
        elapsed = clock.now - job["started_at"]
        result["progress_json"] = json.dumps({"prompt_id": upstream, "ready": elapsed >= 60,
            "cached": state["sampler_cached"], "error": None, "observed_at": clock.now,
            "continuous_sampler_seconds": elapsed, "sampler_progress_events": [
                {"prompt_id": "foreign" if state["foreign_progress"] else upstream,
                 "confirmed_sampler_progress": True, "node": "10", "value": 1, "max": 4}]})
        counters = {key: {"valid": True, "delta": int(state["oom"])} for key in ("oom", "oom_kill")}
        if state["missing_counters"]:
            counters = {}
        result["reconciliation_json"] = json.dumps({"last_observed_at": clock.now, "cgroup_events": counters,
            "kernel_alerts": [], "oom_alerts": [], "xid_alerts": ["Xid 79"] if state["xid"] else [],
            "missing_fields": []})
        return result

    def transport(request):
        port, path, method = request.url.port, request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        state["calls"].append((port, method, path, body))
        if path == "/api/router/capacity":
            active = [job for job in state["jobs"].values() if job["status"] not in validator.TERMINAL] if port == 8791 else []
            return httpx.Response(200, json={"active": active, "queues": [], "validation_lease": state["leases"][port]})
        if path == "/api/router/validation-lease":
            if method == "POST":
                if port == 8791 and state["lab_lease_failure"]:
                    return httpx.Response(503, json={"error": "lab refused lease"})
                state["leases"][port] = {"owner": body["owner"], "expires_at": clock.now + 21600}
            else:
                assert state["leases"][port]["owner"] == body["owner"]
                state["leases"][port] = None
            return httpx.Response(200, json={"ok": True})
        if path == "/api/router/recipe-workflow":
            assert body["prompt"] == prompt.read_text()
            return httpx.Response(200, json={"prompt": graph, "recipe_binding": {
                "recipe_id": body["recipe_id"], "recipe_version": "20260910.1"}})
        if path == "/prompt":
            assert body["prompt"] == graph
            state["submitted"] += 1
            if state["unknown_at"] == state["submitted"]:
                raise httpx.ReadTimeout("submission outcome unknown", request=request)
            identifier = "fleet_" + str(state["submitted"])
            metadata = body["extra_data"]["h3"]
            backend = {"id": "worker_" + identifier, "identity": {}, "runtime_version": "pinned",
                       "lane": {"url": "http://127.0.0.1:" + str(18187 + state["submitted"]), "gpu_uuid": "GPU-offline"}}
            state["jobs"][identifier] = {"prompt_id": identifier, "execution_id": metadata["execution_id"],
                "recipe_id": metadata["recipe_id"], "upstream_prompt_id": "upstream_" + identifier,
                "status": "running", "backend_json": json.dumps(backend), "started_at": clock.now}
            return httpx.Response(200, json={"prompt_id": identifier})
        if path.startswith("/api/jobs/"):
            return httpx.Response(200, json=read_job(state["jobs"][path.rsplit("/", 1)[1]]))
        if path.startswith("/api/router/executions/") and path.endswith("/cancel"):
            execution_id = path.split("/")[-2]
            job = next(job for job in state["jobs"].values() if job["execution_id"] == execution_id)
            assert execution_id.startswith(state["leases"][8791]["owner"] + "_")
            assert set(body) == {"operation_id"}
            if state["cancel_failure"]:
                return httpx.Response(503, json={"error": "cancellation unconfirmed"})
            job["status"] = "cancelled"
            return httpx.Response(200, json={"execution_id": execution_id, "status": "cancelled"})
        if path == "/queue" and port >= 18188:
            return httpx.Response(200, json={"queue_running": [[1, "foreign"]] if state["backend_busy"] else [], "queue_pending": []})
        if path == "/free":
            assert port >= 18188, "global Fleet free is forbidden"
            assert not state["backend_busy"]
            assert body == {"unload_models": True, "free_memory": True}
            assert all(job["status"] in validator.TERMINAL for job in state["jobs"].values())
            return httpx.Response(200, json={})
        if path == "/system_stats" and port >= 18188:
            return httpx.Response(200, json={"devices": [{"vram_total": 16 * validator.GIB,
                "vram_free": (14 if state["unload_failure"] else 16) * validator.GIB}]})
        pytest.fail("unexpected API: " + str(request.url))

    original_client = httpx.Client
    monkeypatch.setattr(validator.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))
    return args, state, clock


def test_smoke_pass_cancels_and_unloads_before_releasing_production(smoke_runtime):
    args, state, clock = smoke_runtime
    report = validator.run(args)
    assert report["status"] == "startup_smoke_passed"
    assert not report["full_video_validated"] and not report["parallel_validated"]
    assert report["lease_released"] and report["all_task_backends_unloaded"]
    assert report["tasks"][0]["status"] == "cancelled"
    assert report["tasks"][0]["smoke_observation"]["sampler_progress"]["continuous_sampler_seconds"] >= 60
    assert "not all stages" in report["no_oom_claim_scope"]
    assert not list(args.output_dir.rglob("qualification-candidate.json"))
    assert not list(args.output_dir.rglob("video.mp4"))
    paths = [(port, method, path) for port, method, path, _ in state["calls"]]
    assert paths.index((18188, "POST", "/free")) < paths.index((8791, "DELETE", "/api/router/validation-lease"))
    assert paths.index((8791, "DELETE", "/api/router/validation-lease")) < paths.index((8789, "DELETE", "/api/router/validation-lease"))
    assert clock.now == 1060
    assert json.loads((args.output_dir / "report.json").read_bytes())["status"] == "startup_smoke_passed"


def test_three_recipe_smoke_cancels_only_its_own_execution_ids(smoke_runtime):
    args, state, _ = smoke_runtime
    args.batch = "people"
    report = validator.run(args)
    assert report["status"] == "startup_smoke_passed" and len(report["tasks"]) == 3
    cancellations = [path for _, method, path, _ in state["calls"] if method == "POST" and path.endswith("/cancel")]
    assert len(cancellations) == 3
    assert all(any(task["execution_id"] in path for task in report["tasks"]) for path in cancellations)
    assert len(report["backend_unloads"]) == 3
    assert not list(args.output_dir.rglob("qualification-candidate.json"))


@pytest.mark.parametrize("failure", ["sampler_cached", "foreign_progress", "missing_counters", "oom", "xid", "job_error", "completed_early"])
def test_no_false_smoke_success_and_known_work_is_reconciled(smoke_runtime, failure):
    args, state, _ = smoke_runtime
    state[failure] = True
    args.timeout = 65
    report = validator.run(args)
    assert report["status"] != "startup_smoke_passed"
    assert report["lease_released"]
    assert not report["full_video_validated"] and not report["parallel_validated"]
    assert all(job["status"] in validator.TERMINAL for job in state["jobs"].values())
    assert not list(args.output_dir.rglob("qualification-candidate.json"))


def test_timeout_cancels_known_work_without_waiting_for_video(smoke_runtime):
    args, state, _ = smoke_runtime
    args.timeout = 10
    report = validator.run(args)
    assert report["status"] == "requires_reconciliation"
    assert report["lease_released"]
    assert state["jobs"]["fleet_1"]["status"] == "cancelled"


def test_unknown_submission_keeps_both_leases_even_when_capacity_is_empty(smoke_runtime):
    args, state, _ = smoke_runtime
    state["unknown_at"] = 1
    report = validator.run(args)
    assert report["status"] == "requires_reconciliation" and not report["lease_released"]
    assert state["leases"][8791] and state["leases"][8789]
    assert not any(method == "DELETE" or path.endswith("/cancel") for _, method, path, _ in state["calls"])


def test_later_unknown_submission_still_cancels_earlier_known_task(smoke_runtime):
    args, state, _ = smoke_runtime
    args.batch = "people"
    state["unknown_at"] = 2
    report = validator.run(args)
    assert not report["lease_released"]
    assert state["jobs"]["fleet_1"]["status"] == "cancelled"
    assert not any(method == "DELETE" for _, method, _, _ in state["calls"])


def test_production_lease_is_cleaned_when_lab_lease_acquisition_fails(smoke_runtime):
    args, state, _ = smoke_runtime
    state["lab_lease_failure"] = True
    report = validator.run(args)
    assert report["status"] == "requires_reconciliation" and report["lease_released"]
    assert state["leases"] == {8791: None, 8789: None}
    assert state["submitted"] == 0
    assert not any(port == 8791 and method == "DELETE" for port, method, _, _ in state["calls"])


@pytest.mark.parametrize("failure", ["wrong_identity", "cancel_failure", "backend_busy", "unload_failure"])
def test_uncertain_cleanup_never_releases_fence(smoke_runtime, failure):
    args, state, _ = smoke_runtime
    state[failure] = True
    report = validator.run(args)
    assert report["status"] == "requires_reconciliation"
    assert not report["lease_released"]
    assert state["leases"][8791] and state["leases"][8789]
    assert not any(method == "DELETE" for _, method, _, _ in state["calls"])
    if failure in {"wrong_identity", "backend_busy"}:
        assert not any(path == "/free" for _, _, path, _ in state["calls"])
    if failure == "wrong_identity":
        assert not any(path.endswith("/cancel") for _, _, path, _ in state["calls"])


def test_backend_identity_failure_refuses_free_and_release(smoke_runtime, monkeypatch):
    args, state, _ = smoke_runtime
    monkeypatch.setattr(validator, "check_backend_identity", lambda binding: (_ for _ in ()).throw(RuntimeError("PID changed")))
    report = validator.run(args)
    assert not report["lease_released"]
    assert not any(path == "/free" or method == "DELETE" for _, method, path, _ in state["calls"])


@pytest.mark.parametrize("seconds", [0, 5, 59, True])
def test_smoke_cannot_silently_shorten_the_60_second_window(tmp_path, seconds):
    with pytest.raises(ValueError, match="exactly 60"):
        validator.run(argparse.Namespace(batch="single", output_dir=tmp_path, execute=False, smoke_seconds=seconds))
