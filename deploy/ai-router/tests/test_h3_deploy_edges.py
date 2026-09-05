from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import types

import httpx
import pytest
from fastapi import FastAPI

from test_h3_media_contract import fixture


DEPLOY_PATH = Path(__file__).resolve().parents[1] / "integrations" / "h3" / "deploy_edge.py"
DEPLOY_SPEC = importlib.util.spec_from_file_location("deploy_h3_edge", DEPLOY_PATH)
assert DEPLOY_SPEC and DEPLOY_SPEC.loader
deploy_edge = importlib.util.module_from_spec(DEPLOY_SPEC)
DEPLOY_SPEC.loader.exec_module(deploy_edge)


def managed(m):
    project = m.create_project()
    return m.STORE.update(project["id"], lambda value: value.update(router_managed=True))


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, ["systemctl", "start"], stderr="new service failed"),
    subprocess.TimeoutExpired(["systemctl", "start"], 45),
])
def test_h3_start_exception_restores_files_and_original_service(tmp_path, monkeypatch, failure):
    root = tmp_path / "h3"
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    app = root / "app"
    unit_dir = home / ".config/systemd/user"
    env_file = home / ".config/h3-video-studio/router.env"
    dropin = unit_dir / (deploy_edge.SERVICE + ".d") / "90-router-contract.conf"
    app.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    backup.mkdir()
    env_file.parent.mkdir(parents=True)
    dropin.parent.mkdir(parents=True)
    (app / "main.py").write_text("patched")
    (app / "router_contract.py").write_text("extension")
    (unit_dir / deploy_edge.SERVICE).write_text("changed unit")
    env_file.write_text("new key")
    dropin.write_text("new dropin")
    (backup / "main.py.before").write_text("original")
    (backup / deploy_edge.SERVICE).write_text("original unit")
    monkeypatch.setattr(deploy_edge, "ROOT", root)
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[:4] == ("systemctl", "--user", "start", deploy_edge.SERVICE) and calls.count(args) == 1:
            raise failure
        if args[:4] == ("systemctl", "--user", "show", deploy_edge.SERVICE):
            return "MainPID=123\nActiveState=active\nSubState=running\nActiveEnterTimestamp=now"
        return ""

    monkeypatch.setattr(deploy_edge, "command", command)
    report = {}
    assert not deploy_edge.start_or_rollback(
        report, backup, env_file, dropin, home=home,
    )
    assert report["status"] == "original_service_restored_after_start_exception"
    assert (app / "main.py").read_text() == "original"
    assert not (app / "router_contract.py").exists()
    assert (unit_dir / deploy_edge.SERVICE).read_text() == "original unit"
    assert not env_file.exists()
    assert not dropin.exists()
    assert calls.count(("systemctl", "--user", "start", deploy_edge.SERVICE)) == 2
    assert report["after"]["service"]["SubState"] == "running"


def test_h3_startup_registration_supports_fastapi_without_app_event_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(FastAPI, "add_event_handler", None, raising=False)
    m, contract, _ = fixture(tmp_path, monkeypatch)
    assert contract.initialize in m.app.router.on_startup


def test_h3_dispatch_only_after_commit_and_release_reservation_on_rollback(tmp_path, monkeypatch):
    m, contract, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    dispatched, released = [], []
    contract.original_spawn = lambda *args: dispatched.append(args)
    m.GPU_GATE = types.SimpleNamespace(release_manual=lambda: released.append(True))
    with pytest.raises(ValueError):
        with contract.connect():
            m.start_context_ir(key)
            m._spawn(m._run_stage, key, "preview", True)
            assert dispatched == []
            raise ValueError("receipt could not be saved")
    assert dispatched == []
    assert released == [True]
    assert m.STORE.get(key)["stages"]["context_ir"]["status"] == "pending"
    with contract.connect():
        m.start_context_ir(key)
        assert dispatched == []
    assert len(dispatched) == 1
    assert m.STORE.get(key)["stages"]["context_ir"]["status"] == "queued"


def test_h3_immutable_video_versions_survive_rerun(tmp_path, monkeypatch):
    m, _, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    source = tmp_path / key / "preview.mp4"
    source.write_bytes(b"first video")
    m.start_stage(key, "preview", {})
    m.STORE.update(key, lambda value: value["stages"]["preview"].update(
        status="awaiting_approval", artifact=str(source)))
    first = m.STORE.get(key)
    first_id = first["stages"]["preview"]["output_id"]
    first_run = first["stages"]["preview"]["run_id"]
    m.start_stage(key, "preview", {})
    assert "output_id" not in m.STORE.get(key)["stages"]["preview"]
    source.write_bytes(b"second video")
    m.STORE.update(key, lambda value: value["stages"]["preview"].update(
        status="awaiting_approval", artifact=str(source)))
    second = m.STORE.get(key)
    second_id = second["stages"]["preview"]["output_id"]
    assert first_id != second_id
    assert first_run != second["stages"]["preview"]["run_id"]
    assert len(second["router_outputs"]) == 2
    assert source.read_bytes() == b"second video"
    assert (tmp_path / key / "router-outputs" / (first_id + ".mp4")).read_bytes() == b"first video"
    assert (tmp_path / key / "router-outputs" / (second_id + ".mp4")).read_bytes() == b"second video"


@pytest.mark.parametrize("body", [
    "not JSON", [], {"operation_id": "start"}, {"operation_id": "start", "expected_run_id": []},
    {"operation_id": "start", "expected_run_id": None, "new_seed": "false"},
])
def test_h3_invalid_action_never_dispatches(tmp_path, monkeypatch, body):
    m, contract, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    dispatched = []
    contract.original_spawn = lambda *args: dispatched.append(args)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://test", headers={"Authorization": "Bearer test"}) as client:
            response = await client.post(f"/api/router/projects/{key}/stages/context_ir/start",
                                         content=body if isinstance(body, str) else json.dumps(body))
            assert response.status_code == 400
    asyncio.run(scenario())
    assert not dispatched
    assert m.STORE.get(key)["stages"]["context_ir"]["status"] == "pending"


def test_h3_escaped_batch_id_is_blocked_and_output_text_is_retrievable(tmp_path, monkeypatch):
    m, _, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    m.start_context_ir(key)

    def complete(value):
        value["prompt_ir"] = "immutable prompt"
        value["stages"]["context_ir"].update(status="awaiting_approval")
    project = m.STORE.update(key, complete)
    output_id = project["stages"]["context_ir"]["output_id"]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://test", headers={"Authorization": "Bearer test"}) as client:
            escaped_id = "".join("\\u%04x" % ord(char) for char in key)
            response = await client.post("/api/768-queue/schedules",
                                         content='{"project_ids":["' + escaped_id + '"]}')
            assert response.status_code == 409
            result = await client.get(f"/api/router/projects/{key}/outputs/{output_id}")
            assert result.status_code == 200
            assert result.text == "immutable prompt"
            assert result.headers["content-type"].startswith("text/plain")
    asyncio.run(scenario())


def test_h3_running_downstream_cannot_be_reset_by_new_start(tmp_path, monkeypatch):
    m, contract, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    m.STORE.update(key, lambda value: value["stages"]["preview"].update(status="running", run_id="live"))
    dispatched = []
    contract.original_spawn = lambda *args: dispatched.append(args)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://test", headers={"Authorization": "Bearer test"}) as client:
            response = await client.post(f"/api/router/projects/{key}/stages/context_ir/start",
                                         json={"operation_id": "new", "expected_run_id": None})
            assert response.status_code == 409
    asyncio.run(scenario())
    assert dispatched == []
    assert m.STORE.get(key)["stages"]["preview"]["run_id"] == "live"


def test_h3_null_context_approval_and_boolean_form_are_rejected(tmp_path, monkeypatch):
    m, _, _ = fixture(tmp_path, monkeypatch)
    key = managed(m)["id"]
    m.STORE.update(key, lambda value: value["stages"]["context_ir"].update(
        status="awaiting_approval", run_id="ir"))
    project = m.STORE.get(key)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app),
                                     base_url="http://test", headers={"Authorization": "Bearer test"}) as client:
            response = await client.post(f"/api/router/projects/{key}/stages/context_ir/approve",
                                         json={"operation_id": "bad", "expected_run_id": "ir",
                                               "expected_output_id": project["stages"]["context_ir"]["output_id"],
                                               "prompt": None})
            assert response.status_code == 400
            response = await client.post("/api/router/projects",
                                         data={"operation_id": "bad-form", "mode": "t2v",
                                               "prompt": "scene", "watermark": "yes"})
            assert response.status_code == 400
    asyncio.run(scenario())
    assert len(m.STORE.list()) == 1
