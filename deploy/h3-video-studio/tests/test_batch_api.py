from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.batching import LocalGpuGate, TIMEZONE
from app.storage import BatchStore, ProjectStore


def ready_project(main, project_id: str) -> dict:
    stages = {stage: main._new_stage() for stage in main.STAGE_IDS}
    stages["context_ir"]["status"] = "approved"
    stages["preview"]["status"] = "approved"
    return {
        "id": project_id,
        "name": f"Ready {project_id}",
        "mode": "t2v",
        "strategy": "fast",
        "duration": 15,
        "actual_duration": 362 / 24,
        "seed": 1,
        "audio_policy": "native",
        "watermark": False,
        "use_embedded_video_audio": False,
        "prompt_original": "prompt",
        "prompt_ir": "optimized",
        "prompt_approved": "optimized",
        "assets": {},
        "stages": stages,
        "created_at": 1.0,
        "updated_at": 1.0,
    }


def setup_stores(main, tmp_path, monkeypatch):
    database = tmp_path / "studio.sqlite3"
    project_store = ProjectStore(database)
    batch_store = BatchStore(database)
    project_store.initialize()
    batch_store.initialize()
    monkeypatch.setattr(main, "STORE", project_store)
    monkeypatch.setattr(main, "BATCH_STORE", batch_store)
    monkeypatch.setattr(main, "GPU_GATE", LocalGpuGate())
    return project_store, batch_store


def test_schedule_create_and_delete_restores_project(tmp_path, monkeypatch) -> None:
    from app import main

    project_store, _ = setup_stores(main, tmp_path, monkeypatch)
    project = ready_project(main, "ready768")
    project_store.save(project)
    future = datetime.now(TIMEZONE) + timedelta(days=1)

    with TestClient(main.app) as client:
        candidates = client.get("/api/768-queue/candidates")
        assert candidates.status_code == 200
        assert [item["id"] for item in candidates.json()["candidates"]] == [
            "ready768"
        ]

        created = client.post(
            "/api/768-queue/schedules",
            json={
                "name": "Night batch",
                "kind": "once",
                "once_local": future.strftime("%Y-%m-%dT%H:%M"),
                "project_ids": ["ready768"],
            },
        )
        assert created.status_code == 200, created.text
        schedule = created.json()
        assert schedule["status"] == "scheduled"
        assert schedule["summary"]["pending_count"] == 1
        project_after = project_store.get("ready768")
        assert project_after["stages"]["local_768"]["status"] == "scheduled"

        deleted = client.delete(
            f"/api/768-queue/schedules/{schedule['id']}"
        )
        assert deleted.status_code == 200
        restored = project_store.get("ready768")
        assert restored["stages"]["local_768"]["status"] == "pending"


def test_batch_runs_serially_and_leaves_768_for_review(tmp_path, monkeypatch) -> None:
    from app import main
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(health=lambda: {}))

    project_store, batch_store = setup_stores(main, tmp_path, monkeypatch)
    project_store.save(ready_project(main, "first"))
    project_store.save(ready_project(main, "second"))
    future = datetime.now(TIMEZONE) + timedelta(days=1)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/768-queue/schedules",
            json={
                "name": "Serial batch",
                "kind": "once",
                "once_local": future.strftime("%Y-%m-%dT%H:%M"),
                "project_ids": ["first", "second"],
            },
        )
        assert created.status_code == 200, created.text
        schedule_id = created.json()["id"]

        order: list[str] = []

        def fake_local(project_id: str, stage_id: str) -> None:
            order.append(project_id)
            main._set_stage(
                project_id,
                stage_id,
                status="awaiting_approval",
                progress=100,
                detail="fake complete",
                finished_at=10.0,
            )

        monkeypatch.setattr(main, "_run_local_stage", fake_local)
        monkeypatch.setattr(main, "check_disk_space", lambda path: None)
        monkeypatch.setattr(main.COMFY, "health", lambda: {})
        batch_store.update(
            schedule_id,
            lambda item: item.update(status="waiting_for_gpu"),
        )
        main._run_batch_schedule(schedule_id)

        schedule = batch_store.get(schedule_id)
        assert order == ["first", "second"]
        assert schedule["status"] == "completed"
        assert [item["status"] for item in schedule["items"]] == [
            "completed",
            "completed",
        ]
        assert (
            project_store.get("first")["stages"]["local_768"]["status"]
            == "awaiting_approval"
        )
        assert (
            project_store.get("first")["stages"]["regenerate_2k"]["status"]
            == "pending"
        )


def test_invalid_item_is_skipped_and_next_item_continues(
    tmp_path,
    monkeypatch,
) -> None:
    from app import main
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(health=lambda: {}))

    project_store, batch_store = setup_stores(main, tmp_path, monkeypatch)
    project_store.save(ready_project(main, "invalid"))
    project_store.save(ready_project(main, "valid"))
    future = datetime.now(TIMEZONE) + timedelta(days=1)
    with TestClient(main.app) as client:
        created = client.post(
            "/api/768-queue/schedules",
            json={
                "name": "Skip batch",
                "kind": "once",
                "once_local": future.strftime("%Y-%m-%dT%H:%M"),
                "project_ids": ["invalid", "valid"],
            },
        )
        schedule_id = created.json()["id"]

        invalid = project_store.get("invalid")
        invalid["stages"]["preview"]["status"] = "pending"
        project_store.save(invalid)
        order: list[str] = []

        def fake_local(project_id: str, stage_id: str) -> None:
            order.append(project_id)
            main._set_stage(
                project_id,
                stage_id,
                status="awaiting_approval",
                progress=100,
            )

        monkeypatch.setattr(main, "_run_local_stage", fake_local)
        monkeypatch.setattr(main, "check_disk_space", lambda path: None)
        monkeypatch.setattr(main.COMFY, "health", lambda: {})
        batch_store.update(
            schedule_id,
            lambda item: item.update(status="waiting_for_gpu"),
        )
        main._run_batch_schedule(schedule_id)

        schedule = batch_store.get(schedule_id)
        assert order == ["valid"]
        assert [item["status"] for item in schedule["items"]] == [
            "skipped",
            "completed",
        ]
        assert schedule["status"] == "completed"


def test_infrastructure_error_pauses_remaining_batch(
    tmp_path,
    monkeypatch,
) -> None:
    from app import main
    monkeypatch.setattr(main, "COMFY", SimpleNamespace(health=lambda: {}))

    project_store, batch_store = setup_stores(main, tmp_path, monkeypatch)
    project_store.save(ready_project(main, "offline-first"))
    project_store.save(ready_project(main, "offline-second"))
    future = datetime.now(TIMEZONE) + timedelta(days=1)
    with TestClient(main.app) as client:
        created = client.post(
            "/api/768-queue/schedules",
            json={
                "name": "Infrastructure pause",
                "kind": "once",
                "once_local": future.strftime("%Y-%m-%dT%H:%M"),
                "project_ids": ["offline-first", "offline-second"],
            },
        )
        schedule_id = created.json()["id"]
        monkeypatch.setattr(main, "check_disk_space", lambda path: None)

        def offline():
            raise RuntimeError("connection refused")

        monkeypatch.setattr(main.COMFY, "health", offline)
        batch_store.update(
            schedule_id,
            lambda item: item.update(status="waiting_for_gpu"),
        )
        main._run_batch_schedule(schedule_id)

        schedule = batch_store.get(schedule_id)
        assert schedule["status"] == "paused"
        assert [item["status"] for item in schedule["items"]] == [
            "failed",
            "pending",
        ]
        assert "异常" in schedule["detail"]
