from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_connector_api import STUDIO_ROOT, studio_factory


ROUTER_SOURCE = Path(__file__).resolve().parents[2] / "ai-router/integrations/h3/router_contract.py"


@pytest.fixture(params=["source", "studio_overlay"])
def source_studio(studio_factory, monkeypatch, request):
    source = ROUTER_SOURCE.read_text()
    if request.param == "studio_overlay":
        anchor = '                    if stage["status"] == "queued" and old["status"] != "queued":'
        replacement = next(line for line in source.splitlines()
                           if 'if stage["status"] == "queued" and old["status"]' in line)
        source = (STUDIO_ROOT / "app/router_contract.py").read_text()
        if anchor in source:
            assert source.count(anchor) == 1
            source = source.replace(anchor, replacement)
        else:
            assert source.count(replacement) == 1
    read_text = Path.read_text

    def source_override(path, *args, **kwargs):
        if path == STUDIO_ROOT / "app/router_contract.py":
            return source
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", source_override)
    return studio_factory()


def test_running_queued_running_keeps_run_callbacks_and_approval(source_studio, monkeypatch):
    studio = source_studio
    queued = studio.start(studio.confirm(studio.draft()))
    run_id = queued["preview"]["run_id"]
    observed = []

    def wait(execution_id, destination, *, progress, cancelled):
        stage = studio.module.STORE.get(queued["task_id"])["stages"]["preview"]
        observed.append((stage["status"], stage["run_id"]))
        for status in ("queued", "running", "queued", "running"):
            progress({"status": status, "progress": 35})
            stage = studio.module.STORE.get(queued["task_id"])["stages"]["preview"]
            observed.append((stage["status"], stage["run_id"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(studio.fleet.artifact)
        return {"execution_id": execution_id}

    monkeypatch.setattr(studio.fleet, "wait_execution", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(studio.pending.pop()).result(timeout=10)
    assert observed == [(status, run_id) for status in ("running", "queued", "running", "queued", "running")]
    completed = studio.get(queued)
    assert completed["preview"]["status"] == "awaiting_approval"
    assert completed["preview"]["run_id"] == run_id
    output_id = completed["preview"]["output_id"]
    output = studio.module.STORE.get(queued["task_id"])["router_outputs"][output_id]
    assert output["run_id"] == run_id
    approved = studio.ok("h3_review_preview", studio.arguments(completed,
        output_id=output_id, expected_run_id=run_id, decision="approve"))
    assert approved["status"] == "completed" and approved["preview"]["output_id"] == output_id
    assert len(studio.fleet.submissions) == 1 and not studio.pending


@pytest.mark.parametrize("terminal_status", ["awaiting_approval", "failed", "cancelled"])
def test_new_attempt_rotates_run_and_rejects_old_callback(source_studio, terminal_status):
    studio = source_studio
    queued = studio.start(studio.confirm(studio.draft()))
    old_run = queued["preview"]["run_id"]
    rejected = []

    def old_callback(project_id, stage_id):
        try:
            studio.module._set_stage(project_id, stage_id, status="failed", detail="obsolete callback")
        except Exception as error:
            rejected.append(type(error).__name__)
            raise
        pytest.fail("old callback changed the new attempt")

    studio.contract.spawn(old_callback, queued["task_id"], "preview")
    old_worker = studio.pending.pop()
    completed = studio.finish(queued)
    old_output = completed["preview"]["output_id"]
    if terminal_status != "awaiting_approval":
        studio.module._set_stage(queued["task_id"], "preview", status=terminal_status)
        completed = studio.get(queued)
    new_attempt = studio.start(completed)
    assert new_attempt["preview"]["run_id"] != old_run
    assert new_attempt["preview"]["output_id"] is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(old_worker).result(timeout=10)
    assert rejected == ["StaleRun"]
    assert studio.get(new_attempt)["revision"] == new_attempt["revision"]
    finished = studio.finish(new_attempt)
    assert finished["preview"]["status"] == "awaiting_approval"
    assert finished["preview"]["output_id"] != old_output
    assert studio.module.STORE.get(queued["task_id"])["router_outputs"][old_output]["run_id"] == old_run
    assert studio.call("h3_review_preview", studio.arguments(finished,
        output_id=old_output, expected_run_id=old_run, decision="approve")).status_code == 409
    assert len(studio.fleet.submissions) == 2


def test_cancellation_survives_active_queue_callbacks(source_studio, monkeypatch):
    studio = source_studio
    queued = studio.start(studio.confirm(studio.draft()))
    run_id = queued["preview"]["run_id"]
    observed = []

    def wait(execution_id, destination, *, progress, cancelled):
        progress({"status": "queued", "progress": 20})
        current = studio.get(queued)
        studio.ok("h3_cancel_task", studio.arguments(current, expected_run_id=run_id))
        progress({"status": "running", "progress": 21})
        observed.append(cancelled())
        raise RuntimeError("cancelled")

    monkeypatch.setattr(studio.fleet, "wait_execution", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(studio.pending.pop()).result(timeout=10)
    result = studio.get(queued)
    assert observed == [True]
    assert result["preview"]["status"] == "cancelled"
    assert result["preview"]["run_id"] == run_id
    assert result["preview"]["output_id"] is None
    assert len(studio.fleet.submissions) == 1


def test_reconciliation_of_same_execution_is_not_a_new_attempt(source_studio):
    studio = source_studio
    studio.fleet.unknown_wait = True
    queued = studio.start(studio.confirm(studio.draft()))
    unknown = studio.finish(queued)
    assert unknown["status"] == "needs_reconciliation"
    execution_id = unknown["preview"]["execution_id"]
    studio.fleet.unknown_wait = False
    resumed = studio.start(unknown)
    assert resumed["preview"]["run_id"] == queued["preview"]["run_id"]
    finished = studio.finish(resumed)
    assert finished["preview"]["status"] == "awaiting_approval"
    assert finished["preview"]["run_id"] == queued["preview"]["run_id"]
    assert finished["preview"]["execution_id"] == execution_id
    assert len(studio.fleet.submissions) == 1
