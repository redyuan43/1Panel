import importlib.util
import io
import itertools
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_comparison_status.py"
SPEC = importlib.util.spec_from_file_location("sync_comparison_status_under_test", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

ROOT = "/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909"
ENDPOINTS = tuple(f"http://127.0.0.1:{port}" for port in (18188, 18189, 18190, 18191))
MATRIX = "h3-comparison-A8-C1-20260909.service"
FOLLOWUP = "h3-comparison-C1-after-B8-20260910.service"
RETRY = "h3-comparison-B8-after-C1-20260910.service"
FINALCASE = "h3-comparison-D4-after-B8-20260910.service"


def queue(identifier=None, state="running"):
    result = {"queue_running": [], "queue_pending": []}
    if identifier is not None:
        result["queue_" + state] = [[0, identifier, {}, {}, []]]
    return result


@pytest.fixture
def remote(tmp_path, monkeypatch, capsys):
    calls, system_calls = [], []
    responses = {endpoint + "/queue": queue() for endpoint in ENDPOINTS}
    services = {}

    class Opener:
        def open(self, url, timeout):
            assert timeout == 3
            assert any(url == endpoint + "/queue" or url.startswith(endpoint + "/history/") for endpoint in ENDPOINTS)
            calls.append(url)
            response = responses.get(url, urllib.error.URLError("unconfigured allowed endpoint"))
            if isinstance(response, Exception):
                raise response
            return io.StringIO(json.dumps(response))

    def build_opener(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], urllib.request.ProxyHandler)
        assert handlers[0].proxies == {}
        return Opener()

    def run(command, **kwargs):
        assert command[:2] == ["systemctl", "is-active"]
        assert len(command) == 3 and command[2] in {MATRIX, FOLLOWUP, RETRY, FINALCASE}
        assert kwargs == {"capture_output": True, "text": True, "timeout": 5}
        system_calls.append(command)
        return SimpleNamespace(stdout=services.get(command[2], "inactive") + "\n")

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(subprocess, "run", run)

    def write_report(directory="run", **overrides):
        report = {"case": "B8", "prompt_id": "same-prompt", "status": "running", "started_at": 100,
                  "submitted_at": 101, "run_id": "old-run", "isolated_url": ENDPOINTS[0]}
        report.update(overrides)
        path = tmp_path / directory / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report))
        return report

    def observe():
        assert sync.REMOTE.count(ROOT) == 1
        source = sync.REMOTE.replace(ROOT, str(tmp_path))
        exec(compile(source, str(SCRIPT) + "::REMOTE", "exec"), {})
        return json.loads(capsys.readouterr().out)

    return SimpleNamespace(write=write_report, observe=observe, responses=responses,
                           calls=calls, services=services, system_calls=system_calls, root=tmp_path)


def case_record(payload, case="B8"):
    records = [record for record in payload["cases"] if record["id"] == case]
    assert len(records) == 1
    return records[0]


def test_progressive_batch_maps_each_task_to_its_gpu_and_queue(remote):
    remote.write(case=None, tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "gpu_uuid": "gpu-fast",
         "prompt_id": "fast-job", "submit_attempted": True, "submitted_at": 101},
        {"case": "A4_C0", "endpoint": ENDPOINTS[1], "gpu_uuid": "gpu-main",
         "prompt_id": "main-job", "submit_attempted": False},
    ])
    remote.responses[ENDPOINTS[0] + "/queue"] = queue("fast-job")
    result = remote.observe()
    assert case_record(result, "A4_C1")["status"] == "running"
    assert case_record(result, "A4_C1")["gpu_uuid"] == "gpu-fast"
    assert case_record(result, "A4_C0")["status"] == "prepared"
    assert case_record(result, "A4_C0")["gpu_uuid"] == "gpu-main"


def test_working_set_three_task_batch_keeps_gpu_wait_reason_and_replica_identity(remote):
    remote.write("A4_C05", case="A4_C05", status="generated_pending_quality_review", started_at=50)
    remote.write("A4-working-set-v2", case=None, run_id="working-set-v2", tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "gpu_uuid": "gpu-fast",
         "prompt_id": "fast-job", "submit_attempted": True, "submitted_at": 101},
        {"case": "A4_C0", "endpoint": ENDPOINTS[1], "gpu_uuid": "gpu-main",
         "prompt_id": "main-job", "submit_attempted": False, "status": "admission_waiting",
         "admission_reason": "admission_cgroup_remaining_peak_budget"},
        {"case": "A4_C05", "endpoint": ENDPOINTS[2], "gpu_uuid": "gpu-preview",
         "prompt_id": "replica-job", "submit_attempted": False, "status": "admission_waiting",
         "admission_reason": "waiting_for_prior_task_admission"},
    ])
    remote.responses[ENDPOINTS[0] + "/queue"] = queue("fast-job")
    result = remote.observe()
    for case, lane, endpoint in zip(("A4_C1", "A4_C0", "A4_C05"), ("fast", "main", "preview"), ENDPOINTS):
        record = case_record(result, case)
        assert record["lane"] == lane and record["endpoint"] == endpoint
        assert record["gpu_uuid"] == "gpu-" + lane
        assert record["run_id"] == "working-set-v2"
        assert record["replica"] is (case == "A4_C05")
    assert case_record(result, "A4_C0")["status"] == "prepared"
    assert case_record(result, "A4_C0")["admission_reason"] == "admission_cgroup_remaining_peak_budget"
    assert case_record(result, "A4_C05")["admission_reason"] == "waiting_for_prior_task_admission"
    assert json.loads((remote.root / "A4_C05/report.json").read_text())["status"] == "generated_pending_quality_review"


@pytest.mark.parametrize("released,expected", [(False, "finalizing"), (True, "completed")])
def test_batch_completion_requires_cleanup(remote, released, expected):
    remote.write(case=None, lease_released=released, both_unloaded=released, tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "gpu_uuid": "gpu-fast",
         "prompt_id": "fast-job", "submit_attempted": True, "status": "collected"},
    ])
    assert case_record(remote.observe(), "A4_C1")["status"] == expected


def test_prepared_batch_without_started_at_supersedes_old_failed_run(remote):
    remote.write("old", case="A4_C1", status="failed", started_at=100)
    remote.write("A4-working-set-v2", case=None, started_at=None, run_id="working-set-v2", tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "gpu_uuid": "gpu-fast",
         "prompt_id": "new-job", "status": "prepared", "submit_attempted": False},
    ])
    record = case_record(remote.observe(), "A4_C1")
    assert record["status"] == "prepared"
    assert record["prompt_id"] == "new-job"
    assert record["started_at"] is None
    assert record["run_id"] == "working-set-v2"


def test_failed_batch_retains_completed_c1_while_new_single_c0_takes_over(remote):
    remote.write("A4-working-set-v3", case=None, status="failed", run_id="working-set-v3",
                 lease_released=True, both_unloaded=True, error="C0 sampler CUDA OOM", tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "gpu_uuid": "gpu-fast", "prompt_id": "good-c1",
         "submit_attempted": True, "status": "collected", "execution": {"execution_seconds": 551.179}},
        {"case": "A4_C0", "endpoint": ENDPOINTS[1], "gpu_uuid": "gpu-main", "prompt_id": "bad-c0",
         "submit_attempted": True, "status": "submitted"},
    ])
    records = remote.observe()
    retained = case_record(records, "A4_C1")
    assert retained["status"] == "completed" and retained["error"] is None
    assert retained["batch_status"] == "failed" and retained["batch_error"] == "C0 sampler CUDA OOM"
    assert retained["retained_from_failed_batch"] is True
    assert retained["worker_seconds"] == 551.179
    assert case_record(records, "A4_C0")["status"] == "failed"
    remote.write("A4-C0-v4", case="A4_C0", status="running", run_id="single-v4", started_at=200,
                 prompt_id="good-c0", isolated_url=ENDPOINTS[0])
    remote.responses[ENDPOINTS[0] + "/queue"] = queue("good-c0")
    records = remote.observe()
    assert case_record(records, "A4_C0")["status"] == "running"
    assert case_record(records, "A4_C0")["run_id"] == "single-v4"
    assert case_record(records, "A4_C1")["batch_status"] == "failed"


@pytest.mark.parametrize("field,value", [("lease_released", False), ("both_unloaded", False),
                                        ("reconciliation_error", "unknown"), ("cleanup_error", "unknown"),
                                        ("all_unloaded", False), ("cleanup_errors", ["unknown"]),
                                        ("retained_lease", {"owner": "old"}), ("lease_hold_error", "unknown")])
def test_failed_batch_with_unconfirmed_cleanup_never_marks_collected_task_retained(remote, field, value):
    fields = {"lease_released": True, "both_unloaded": True, field: value}
    remote.write(case=None, status="failed", error="OOM", tasks=[
        {"case": "A4_C1", "endpoint": ENDPOINTS[0], "status": "collected", "prompt_id": "good-c1"}], **fields)
    record = case_record(remote.observe(), "A4_C1")
    assert record["retained_from_failed_batch"] is False
    assert record["status"] != "completed"


@pytest.mark.parametrize("submitted,reconciled,clean,expected", [
    (False, True, True, "not_run"), (True, True, True, "failed"),
    (False, False, True, "failed"), (False, True, False, "failed")])
def test_unsubmitted_replica_is_not_mislabeled_execution_failure(remote, submitted, reconciled, clean, expected):
    remote.write(case=None, status="failed", error="another task failed", lease_released=clean, both_unloaded=clean,
                 tasks=[{"case": "A4_C05", "endpoint": ENDPOINTS[2], "prompt_id": "replica", "status": "admission_waiting",
                         "submit_attempted": submitted,
                         "reconciliation": "never submitted; empty queue/history" if reconciled else None}])
    record = case_record(remote.observe(), "A4_C05")
    assert record["status"] == expected
    assert record["submit_attempted"] is submitted
    assert record["batch_status"] == "failed" and record["replica"] is True
    if expected == "not_run":
        assert record["error"] is None and record["started_at"] is None


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("queue_state,expected", [("running", "running"), ("pending", "queued")])
def test_matches_report_endpoint_queue(remote, endpoint, queue_state, expected):
    remote.write(isolated_url=endpoint)
    remote.responses[endpoint + "/queue"] = queue("same-prompt", queue_state)
    assert case_record(remote.observe())["status"] == expected
    assert not any("/history/" in url for url in remote.calls)


@pytest.mark.parametrize("target,other", list(itertools.permutations(ENDPOINTS, 2)))
@pytest.mark.parametrize("other_queue_state", ["running", "pending"])
def test_same_prompt_on_other_port_does_not_change_target_status(remote, target, other, other_queue_state):
    remote.write(isolated_url=target)
    remote.responses[other + "/queue"] = queue("same-prompt", other_queue_state)
    remote.responses[other + "/history/same-prompt"] = {"same-prompt": {"status": {"status_str": "success"}}}
    assert case_record(remote.observe())["status"] == "reconciling"
    assert target + "/history/same-prompt" in remote.calls
    assert other + "/history/same-prompt" not in remote.calls


@pytest.mark.parametrize("target,other", list(itertools.permutations(ENDPOINTS, 2)))
@pytest.mark.parametrize("report_status", ["running", "submitting"])
def test_target_offline_is_not_idle_or_completed_when_another_port_is_online(remote, target, other, report_status):
    remote.write(isolated_url=target, status=report_status)
    remote.responses[target + "/queue"] = urllib.error.URLError("offline target")
    remote.responses[other + "/queue"] = queue("same-prompt")
    remote.responses[other + "/history/same-prompt"] = {"same-prompt": {"status": {"status_str": "success"}}}
    record = case_record(remote.observe())
    assert record["status"] == "reconciling"
    assert record["status"] not in {"idle", "completed", "running", "finalizing"}
    assert not any("/history/" in url for url in remote.calls)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("terminal", ["generated_pending_quality_review", "generated_pending_media_review", "completed"])
def test_completed_video_stays_completed_with_worker_offline(remote, endpoint, terminal):
    remote.write(isolated_url=endpoint, status=terminal, execution={"execution_seconds": 541.772}, lease_released=True)
    remote.responses[endpoint + "/queue"] = urllib.error.URLError("worker offline after completion")
    for other in ENDPOINTS:
        if other != endpoint:
            remote.responses[other + "/queue"] = queue("same-prompt")
    record = case_record(remote.observe())
    assert record["status"] == "completed"
    assert record["worker_seconds"] == 541.772
    assert record["lease_released"] is True
    assert not any("/history/" in url for url in remote.calls)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("terminal,expected", [("success", "finalizing"), ("error", "failed")])
def test_history_is_queried_only_on_the_selected_online_endpoint(remote, endpoint, terminal, expected):
    remote.write(isolated_url=endpoint)
    remote.responses[endpoint + "/history/same-prompt"] = {"same-prompt": {"status": {"status_str": terminal}}}
    assert case_record(remote.observe())["status"] == expected
    assert [url for url in remote.calls if "/history/" in url] == [endpoint + "/history/same-prompt"]


def test_active_b8_retry_without_new_run_remains_scheduled_and_keeps_previous_error(remote):
    error = "previous run blocked: swap recovery window"
    remote.write(status="failed", error=error, run_id="pink20260909-B8-s1-r2")
    remote.services[RETRY] = "active"
    record = case_record(remote.observe())
    assert record == {"id": "B8", "status": "scheduled", "prompt_id": None, "started_at": None, "previous_error": error}


def test_active_b8_retry_without_any_report_is_scheduled(remote):
    remote.services[RETRY] = "active"
    record = case_record(remote.observe())
    assert record["status"] == "scheduled"
    assert record["previous_error"] is None


@pytest.mark.parametrize("new_status,expected", [("prepared", "prepared"), ("running", "running"),
                                                ("generated_pending_quality_review", "completed")])
def test_new_b8_run_report_supersedes_scheduled_and_old_error(remote, new_status, expected):
    remote.write("old", status="failed", run_id="pink20260909-B8-s1-r2", error="old failure", started_at=100)
    remote.write("new", status=new_status, run_id="pink20260909-B8-s1-r3", prompt_id="new-prompt",
                 started_at=200, submitted_at=201, isolated_url=ENDPOINTS[1])
    remote.services[RETRY] = "active"
    if new_status == "running":
        remote.responses[ENDPOINTS[1] + "/queue"] = queue("new-prompt")
    record = case_record(remote.observe())
    assert record["status"] == expected
    assert record["status"] != "scheduled"
    assert record["prompt_id"] == "new-prompt"
    assert record["error"] is None
    assert "previous_error" not in record


@pytest.mark.parametrize("old_report", [False, True])
def test_active_d4_unit_without_target_run_is_scheduled(remote, old_report):
    if old_report:
        remote.write("old-d4", case="D4", status="failed", run_id="previous-D4-run", error="old failure")
    remote.services[FINALCASE] = "active"
    record = case_record(remote.observe(), "D4")
    assert record == {"id": "D4", "status": "scheduled", "prompt_id": None, "started_at": None}


@pytest.mark.parametrize("new_status,expected", [("prepared", "prepared"), ("running", "running"),
                                                ("generated_pending_quality_review", "completed"), ("failed", "failed")])
def test_d4_target_run_report_supersedes_scheduled_on_18191(remote, new_status, expected):
    remote.write("old-d4", case="D4", status="failed", run_id="previous-D4-run", started_at=100)
    remote.write("new-d4", case="D4", status=new_status, run_id="pink20260909-D4-s1",
                 prompt_id="d4-new-prompt", started_at=200, isolated_url=ENDPOINTS[3])
    remote.services[FINALCASE] = "active"
    if new_status == "running":
        remote.responses[ENDPOINTS[3] + "/queue"] = queue("d4-new-prompt")
    record = case_record(remote.observe(), "D4")
    assert record["status"] == expected
    assert record["status"] != "scheduled"
    assert record["prompt_id"] == "d4-new-prompt"


def test_inactive_d4_unit_does_not_invent_scheduled_case(remote):
    assert all(record["id"] != "D4" for record in remote.observe()["cases"])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_unconfigured_allowed_endpoint_is_unreachable_not_idle(remote, endpoint):
    remote.write(isolated_url=endpoint)
    remote.responses.pop(endpoint + "/queue")
    record = case_record(remote.observe())
    assert record["status"] == "reconciling"
    assert endpoint + "/queue" in remote.calls
    assert not any("/history/" in url for url in remote.calls)


@pytest.mark.parametrize("endpoint", ["http://example.invalid:18188", "http://169.254.169.254/latest/meta-data",
                                     "http://127.0.0.1:8188", "http://127.0.0.1:18192",
                                     "http://127.0.0.1:18188/", "file:///etc/passwd"])
def test_report_cannot_expand_endpoint_allowlist(remote, endpoint):
    remote.write(isolated_url=endpoint)
    assert case_record(remote.observe())["status"] == "reconciling"
    assert set(remote.calls) == {allowed + "/queue" for allowed in ENDPOINTS}


def test_legacy_report_defaults_to_18188_without_cross_endpoint_matching(remote):
    report = remote.write(isolated_url=ENDPOINTS[2])
    report.pop("isolated_url")
    (remote.root / "run/report.json").write_text(json.dumps(report))
    remote.responses[ENDPOINTS[2] + "/queue"] = queue("same-prompt")
    assert case_record(remote.observe())["status"] == "reconciling"
    assert ENDPOINTS[0] + "/history/same-prompt" in remote.calls


def test_legacy_endpoint_does_not_imply_current_gpu_lane(remote):
    remote.write(case="B8", status="completed", isolated_url=ENDPOINTS[2],
                 baseline={"isolated": {"gpu_uuid": "gpu-historical-fast"}})
    record = case_record(remote.observe())
    assert record["lane"] is None
    assert record["gpu_uuid"] == "gpu-historical-fast"


def test_remote_executes_only_mocked_status_commands(remote):
    remote.observe()
    assert {command[2] for command in remote.system_calls} == {MATRIX, FOLLOWUP, RETRY, FINALCASE}
