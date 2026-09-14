import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import admission_reconciliation as reconciliation
import run_parallel_comparison as parallel
from test_admission_reconciliation import start


@pytest.fixture
def evidence_target(tmp_path):
    task = {"case": "A4_C0", "prompt_id": "owned-prompt", "status": "submitted",
            "shape": {"sampler_nodes": ["10"]}, "resource_reconciliation": start()}
    coordinator = parallel.Coordinator.__new__(parallel.Coordinator)
    coordinator.args = SimpleNamespace(output_dir=tmp_path)
    coordinator.report = {"tasks": [task]}
    coordinator.tasks = [task]
    return coordinator, task


def history(exception_type="torch.OutOfMemoryError", message="Allocation on device 0 would exceed allowed memory. (out of memory)\nFree (according to CUDA): 52.50 MiB"):
    return {"status": {"completed": False, "status_str": "error", "messages": [
        ["execution_start", {"timestamp": 1000, "prompt_id": "owned-prompt"}],
        ["execution_cached", {"nodes": [], "prompt_id": "owned-prompt"}],
        ["execution_error", {"timestamp": 20250, "prompt_id": "owned-prompt", "node_id": "10",
                             "node_type": "SamplerCustomAdvanced", "exception_type": exception_type,
                             "exception_message": message, "traceback": ["original traceback remains in history"]}]]},
        "outputs": {}}


def test_cuda_oom_persisted_without_kernel_or_cgroup_oom_and_history_unchanged(evidence_target):
    coordinator, task = evidence_target
    record = history()
    original = copy.deepcopy(record)
    directory = coordinator.args.output_dir / task["case"]
    directory.mkdir()
    path = directory / "history.json"
    raw = json.dumps({task["prompt_id"]: record}, indent=2).encode()
    path.write_bytes(raw)
    task["execution"] = {"stale_success": True}
    with pytest.raises(RuntimeError, match="torch.OutOfMemoryError at node 10"):
        coordinator.evidence(task, record)
    assert path.read_bytes() == raw and record == original
    saved = json.loads((coordinator.args.output_dir / "report.json").read_bytes())["tasks"][0]
    assert saved["status"] == "failed" and "execution" not in saved
    assert saved["cuda_oom_detected"] is True
    failure = saved["execution_failure"]
    assert failure["exception_type"] == "torch.OutOfMemoryError"
    assert failure["exception_message"] == record["status"]["messages"][-1][1]["exception_message"]
    assert failure["node_id"] == "10" and failure["node_type"] == "SamplerCustomAdvanced"
    assert failure["prompt_id"] == failure["reported_prompt_id"] == task["prompt_id"]
    assert failure["status"] == {"completed": False, "status_str": "error"}
    accounting = saved["resource_reconciliation"]
    assert accounting["cuda_oom_detected"] is True
    assert accounting["execution_failures"] == [failure]
    assert accounting["oom_alerts"] == []
    assert accounting["cgroup_events"]["oom"]["delta"] == 0
    assert accounting["cgroup_events"]["oom_kill"]["delta"] == 0
    final = reconciliation.finish(task["resource_reconciliation"])
    assert final["cuda_oom_detected"] and final["execution_failures"] == [failure]


@pytest.mark.parametrize("exception_type,message,expected", [
    ("torch.cuda.OutOfMemoryError", "allocation failed", True),
    ("RuntimeError", "CUDA error: out of memory", True),
    ("MemoryError", "CPU allocation failed: out of memory", False),
    ("torch.OutOfMemoryError", "DefaultCPUAllocator: out of memory", False),
    ("RuntimeError", "shape mismatch", False),
])
def test_cuda_oom_classification_does_not_convert_other_failures_to_success(evidence_target, exception_type, message, expected):
    coordinator, task = evidence_target
    with pytest.raises(RuntimeError, match="history does not prove"):
        coordinator.evidence(task, history(exception_type, message))
    assert task["status"] == "failed"
    assert task["cuda_oom_detected"] is expected
    assert task["resource_reconciliation"]["cuda_oom_detected"] is expected
    assert task["execution_failure"]["exception_message"] == message


def test_interruption_is_not_cuda_oom(evidence_target):
    coordinator, task = evidence_target
    record = history()
    record["status"]["messages"][-1] = ["execution_interrupted", {"node_id": "10", "node_type": "SamplerCustomAdvanced", "prompt_id": task["prompt_id"]}]
    with pytest.raises(RuntimeError):
        coordinator.evidence(task, record)
    assert task["status"] == "failed" and not task["cuda_oom_detected"]
    assert task["execution_failure"]["event"] == "execution_interrupted"
    assert task["execution_failure"]["exception_type"] is None


def test_success_status_cannot_override_execution_error_event(evidence_target):
    coordinator, task = evidence_target
    record = history()
    record["status"].update(completed=True, status_str="success")
    record["status"]["messages"].append(["execution_success", {"timestamp": 30000}])
    with pytest.raises(RuntimeError):
        coordinator.evidence(task, record)
    assert task["status"] == "failed" and task["cuda_oom_detected"]


def test_cached_sampler_rejection_has_failure_evidence_but_not_cuda_oom(evidence_target):
    coordinator, task = evidence_target
    record = history()
    record["status"].update(completed=True, status_str="success")
    record["status"]["messages"][-1] = ["execution_success", {"timestamp": 30000}]
    record["status"]["messages"][1][1]["nodes"] = ["10"]
    with pytest.raises(RuntimeError):
        coordinator.evidence(task, record)
    assert task["status"] == "failed" and not task["cuda_oom_detected"]
    assert task["execution_failure"]["events"] == []


def test_valid_history_and_legacy_task_remain_supported(evidence_target):
    coordinator, task = evidence_target
    task.pop("resource_reconciliation")
    record = history()
    record["status"].update(completed=True, status_str="success")
    record["status"]["messages"][-1] = ["execution_success", {"timestamp": 30000}]
    coordinator.evidence(task, record)
    assert task["status"] == "sampled" and task["execution"]["execution_seconds"] == 29
    assert "execution_failure" not in task
    with pytest.raises(RuntimeError):
        coordinator.evidence(task, history())
    assert task["status"] == "failed" and task["cuda_oom_detected"]
    assert "execution" not in task


def test_accounting_failure_evidence_is_detached_idempotent_and_sticky():
    record = start()
    failure = {"cuda_oom_detected": True, "exception_message": "CUDA out of memory"}
    reconciliation.record_execution_failure(record, failure)
    reconciliation.record_execution_failure(record, failure)
    assert len(record["execution_failures"]) == 1
    failure["exception_message"] = "changed"
    assert record["execution_failures"][0]["exception_message"] == "CUDA out of memory"
    reconciliation.record_execution_failure(record, {"cuda_oom_detected": False})
    assert record["cuda_oom_detected"] is True
