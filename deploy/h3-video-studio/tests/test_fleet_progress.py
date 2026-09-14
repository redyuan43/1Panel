import copy
import json
from pathlib import Path

import pytest

from app.fleet_progress import owned_sampler_progress, execution_detail


@pytest.fixture
def completed_job():
    return json.loads((Path(__file__).parent / "fixtures/owned-sampler-completed.json").read_text())


def test_real_completed_receipt_retains_four_steps_during_decode(completed_job):
    job = completed_job
    assert job["prompt_id"] != job["upstream_prompt_id"]
    receipt = json.loads(job["progress_json"])
    assert receipt["ready"] is False
    assert "no_active_owned_sampler_progress" in receipt["reasons"]
    steps = owned_sampler_progress(job)
    assert (steps["completed"], steps["total"], steps["node"]) == (4, 4, "10")
    label = execution_detail({**job, "node_id": "ivan-u24"},
                             {"phase": "decoding", "sampler_progress": steps})
    assert "ivan-u24：解码中" in label and "4/4 步" in label and "%" not in label


@pytest.mark.parametrize("mutation", [
    lambda r, j: r.update(prompt_id=j["prompt_id"]),
    lambda r, j: r.update(prompt_id="different-run"),
    lambda r, j: r.update(cached=True),
    lambda r, j: r.update(error="listener_error"),
    lambda r, j: r["sampler_progress_events"][-1].update(prompt_id="different-run"),
    lambda r, j: r["sampler_progress_events"][-1].update(confirmed_sampler_progress=False),
    lambda r, j: r["sampler_progress_events"][-1].update(value=1),
    lambda r, j: r["sampler_progress_events"][-1].update(max=True),
])
def test_invalid_or_unowned_observations_are_not_shown(completed_job, mutation):
    job = copy.deepcopy(completed_job)
    receipt = json.loads(job["progress_json"])
    mutation(receipt, job)
    job["progress_json"] = receipt
    assert owned_sampler_progress(job) is None


def test_missing_receipt_does_not_use_fleet_placeholder_percent():
    assert owned_sampler_progress({"progress": 50}) is None
    assert execution_detail({"status": "running"}, {}) == "执行节点：执行中；完成后提供视频"
