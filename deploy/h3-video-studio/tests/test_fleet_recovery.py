from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.fleet import SubmissionUnknown
from app.batching import TIMEZONE
from test_batch_api import ready_project, setup_stores


def test_unknown_execution_can_only_reconcile(tmp_path, monkeypatch):
    from app import main

    store, _ = setup_stores(main, tmp_path, monkeypatch)
    project = ready_project(main, "recovery")
    project["stages"]["local_768"].update(status="failed", fleet_pending=True, execution_id="original-execution")
    store.save(project)
    spawned = []
    monkeypatch.setattr(main, "_spawn", lambda *args: spawned.append(args))
    client = TestClient(main.app)
    prefix = "/api/projects/recovery"
    assert client.delete(prefix).status_code == 409
    assert client.put(prefix + "/prompt", json={"prompt": "changed"}).status_code == 409
    assert client.post(prefix + "/stages/preview/start", json={}).status_code == 409
    assert client.post(prefix + "/stages/local_768/start", json={"new_seed": True}).status_code == 409
    assert client.post(prefix + "/stages/local_768/start", json={}).status_code == 200
    assert store.get("recovery")["stages"]["local_768"]["execution_id"] == "original-execution"
    assert len(spawned) == 1
    main.GPU_GATE.release_manual()


def test_concurrent_clicks_only_spawn_once(tmp_path, monkeypatch):
    from app import main

    store, _ = setup_stores(main, tmp_path, monkeypatch)
    store.save(ready_project(main, "concurrent"))
    spawned = []
    barrier = Barrier(2)
    monkeypatch.setattr(main, "_spawn", lambda *args: spawned.append(args))

    def start():
        barrier.wait()
        return TestClient(main.app).post("/api/projects/concurrent/stages/local_768/start", json={}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda unused: start(), range(2)))
    assert sorted(statuses) == [200, 409]
    assert len(spawned) == 1
    main.GPU_GATE.release_manual()


def test_existing_execution_never_uploads_or_submits(tmp_path, monkeypatch):
    from app import main

    store, _ = setup_stores(main, tmp_path, monkeypatch)
    project = ready_project(main, "existing")
    project["stages"]["local_768"].update(status="running", fleet_pending=True, execution_id="original")
    store.save(project)

    class ExistingFleet:
        is_fleet = True

        def wait_execution(self, execution_id, destination, **kwargs):
            assert execution_id == "original"
            raise SubmissionUnknown("still unknown")

    monkeypatch.setattr(main, "COMFY", ExistingFleet())
    main._run_stage("existing", "local_768")
    stage = store.get("existing")["stages"]["local_768"]
    assert stage["status"] == "failed" and stage["fleet_pending"]
    assert stage["execution_id"] == "original"
    spawned = []
    monkeypatch.setattr(main, "_spawn", lambda *args: spawned.append(args))
    main._mark_interrupted_stages()
    assert len(spawned) == 1
    main.GPU_GATE.release_manual()


def test_batch_unknown_execution_survives_pause_and_resume(tmp_path, monkeypatch):
    from app import main

    store, batches = setup_stores(main, tmp_path, monkeypatch)
    store.save(ready_project(main, "batch-recovery"))
    client = TestClient(main.app)
    future = datetime.now(TIMEZONE) + timedelta(days=1)
    schedule = client.post("/api/768-queue/schedules", json={"kind": "once", "project_ids": ["batch-recovery"],
        "once_local": future.strftime("%Y-%m-%dT%H:%M")}).json()
    schedule_id = schedule["id"]
    schedule["status"] = "waiting_for_gpu"
    schedule["items"][0]["status"] = "running"
    batches.save(schedule)
    store.update("batch-recovery", lambda project: project["stages"]["local_768"].update(
        status="running", execution_id="original-batch-execution", fleet_pending=True))

    class RecoveringFleet:
        is_fleet = True
        available = False
        observations = []

        def begin_batch(self, owner):
            assert owner == schedule_id

        def end_batch(self, owner):
            if not self.available:
                raise RuntimeError("execution remains active")

        def wait_execution(self, execution_id, destination, **kwargs):
            self.observations.append(execution_id)
            if not self.available:
                raise SubmissionUnknown("lost connection")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"synthetic test artifact")
            return {"execution_id": execution_id}

    fleet = RecoveringFleet()
    monkeypatch.setattr(main, "COMFY", fleet)
    monkeypatch.setattr(main, "_project_dir", lambda project_id: tmp_path / project_id)
    main._run_batch_schedule(schedule_id)
    assert batches.get(schedule_id)["status"] == "paused"
    assert batches.get(schedule_id)["items"][0]["status"] == "running"
    path = "/api/768-queue/schedules/" + schedule_id
    assert client.delete(path).status_code == 409
    assert client.patch(path, json={"project_ids": []}).status_code == 409
    assert client.post(path + "/resume").status_code == 200
    fleet.available = True
    main._run_batch_schedule(schedule_id)
    assert batches.get(schedule_id)["status"] == "completed"
    assert fleet.observations == ["original-batch-execution", "original-batch-execution"]
    assert store.get("batch-recovery")["stages"]["local_768"]["status"] == "awaiting_approval"


def test_recovered_fleet_execution_preserves_original_start(tmp_path, monkeypatch):
    from app import main
    from types import SimpleNamespace
    store, _ = setup_stores(main, tmp_path, monkeypatch)
    project = ready_project(main, "time-recovery")
    project["stages"]["local_768"].update(fleet_pending=True, execution_id="same-execution", started_at=123.0)
    store.save(project)
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(is_fleet=True))
    monkeypatch.setattr(main.time, "time", lambda: 456.0)
    monkeypatch.setattr(main, "_run_local_stage", lambda *args: None)
    main._run_stage("time-recovery", "local_768")
    stage=store.get("time-recovery")["stages"]["local_768"]
    assert stage["started_at"]==123.0 and stage["execution_id"]=="same-execution"


def test_new_fleet_execution_uses_new_start(tmp_path, monkeypatch):
    from app import main
    from types import SimpleNamespace
    store, _ = setup_stores(main, tmp_path, monkeypatch)
    project = ready_project(main, "time-new")
    project["stages"]["local_768"].update(fleet_pending=False, execution_id="previous-execution", started_at=123.0)
    store.save(project)
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(is_fleet=True))
    monkeypatch.setattr(main.time, "time", lambda: 456.0)
    monkeypatch.setattr(main, "_run_local_stage", lambda *args: None)
    main._run_stage("time-new", "local_768")
    assert store.get("time-new")["stages"]["local_768"]["started_at"]==456.0
