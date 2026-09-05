from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
import types
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI


class FixtureStore:
    def __init__(self, path):
        self.database_path = path
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, value TEXT)")

    def _connect(self):
        return sqlite3.connect(self.database_path)

    def get(self, key):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM projects WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, value):
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO projects VALUES (?,?)", (value["id"], json.dumps(value)))
        return value

    def update(self, key, mutate):
        with self._lock:
            value = self.get(key)
            mutate(value)
            return self.save(value)

    def list(self, limit=50):
        with self._connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT value FROM projects LIMIT ?", (limit,))]


def fixture(tmp_path, monkeypatch):
    package = types.ModuleType("fixture_h3")
    package.__path__ = []
    workflows = types.ModuleType("fixture_h3.workflows")
    workflows.VALID_MODES = {"t2v"}
    workflows.VALID_STRATEGIES = {"fast"}
    workflows.VALID_AUDIO_POLICIES = {"native"}
    monkeypatch.setitem(sys.modules, "fixture_h3", package)
    monkeypatch.setitem(sys.modules, "fixture_h3.workflows", workflows)
    path = Path(__file__).parents[1] / "integrations/h3/router_contract.py"
    spec = importlib.util.spec_from_file_location("fixture_h3.router_contract", path)
    extension = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extension)
    m = types.SimpleNamespace(app=FastAPI(), STORE=FixtureStore(tmp_path / "h3.sqlite3"),
                              STAGE_IDS={"context_ir", "preview"})
    m._spawn = lambda *args: None
    m.MINIMAX = types.SimpleNamespace(regenerate_2k=lambda *args, **kwargs: {"task_id": "unit-fixture"})
    m._run_context_ir = lambda *args: None
    m._run_stage = lambda *args: None
    m._run_local_stage = lambda *args: None
    m.BatchInfrastructureError = type(
        "BatchInfrastructureError", (RuntimeError,), {}
    )
    m._require_project = m.STORE.get
    m._project_dir = lambda key: tmp_path / key
    m.pipeline_for = lambda value: [{"id": key, **stage} for key, stage in value["stages"].items()]

    def create_project(**body):
        key = uuid4().hex
        (tmp_path / key).mkdir()
        return m.STORE.save({
            "id": key, "prompt_ir": "", "prompt_approved": "", "actual_duration": 4.45,
            "stages": {name: {"status": "pending", "progress": 0} for name in ("context_ir", "preview")},
        })
    m.create_project = create_project

    def start_context(key):
        m.STORE.update(key, lambda value: value["stages"].update({"context_ir": {"status": "queued", "progress": 1}}))
        m._spawn(m._run_context_ir, key)
    m.start_context_ir = start_context

    def approve_context(key, body):
        def change(value):
            value["prompt_approved"] = body["prompt"]
            value["stages"]["context_ir"]["status"] = "approved"
        return m.STORE.update(key, change)
    m.approve_context_ir = approve_context
    m.start_stage = lambda key, stage, body: m.STORE.update(key, lambda value: value["stages"][stage].update(status="queued"))
    m.approve_stage = lambda key, stage: m.STORE.update(key, lambda value: value["stages"][stage].update(status="approved"))
    m.cancel_stage = lambda key, stage: m.STORE.update(key, lambda value: value["stages"][stage].update(status="cancelled"))
    contract = extension.install(m)
    contract.initialize()
    monkeypatch.setenv("H3_ROUTER_KEY", "test")
    return m, contract, extension


def test_local_768_requires_exclusive_gpu(tmp_path, monkeypatch):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    calls = []
    contract.original_run_local_stage = lambda *args: calls.append(args)

    def run(args, **kwargs):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout="3059\n", stderr="")
        return subprocess.CompletedProcess(
            args, 0,
            stdout="3059, /home/admin/github/comfyui/.venv/bin/python, 16047\n"
                   "1702458, VLLM::EngineCore, 100114\n",
            stderr="",
        )

    monkeypatch.setattr(extension.subprocess, "run", run)
    with pytest.raises(m.BatchInfrastructureError, match="exclusive GPU"):
        contract.run_local_stage("project", "local_768")
    assert calls == []

    def comfy_only(args, **kwargs):
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 0, stdout="3059\n", stderr="")
        return subprocess.CompletedProcess(
            args, 0,
            stdout="3059, /home/admin/github/comfyui/.venv/bin/python, 16047\n",
            stderr="",
        )

    monkeypatch.setattr(extension.subprocess, "run", comfy_only)
    contract.run_local_stage("project", "local_768")
    assert calls == [("project", "local_768")]


def test_router_project_exposes_provider_heartbeat(tmp_path, monkeypatch):
    m, _, _ = fixture(tmp_path, monkeypatch)
    project = m.create_project()
    m.STORE.update(project["id"], lambda value: value.update(
        router_managed=True, updated_at=123.5,
    ))

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=m.app),
            base_url="http://test",
            headers={"Authorization": "Bearer test"},
        ) as client:
            options = (await client.get("/api/router/options")).json()
            assert options["stage_heartbeat"] is True
            assert options["local_768_gpu_exclusive"] is True
            response = await client.get(f"/api/router/projects/{project['id']}")
            assert response.status_code == 200
            assert response.json()["updated_at"] == 123.5

    asyncio.run(scenario())


def test_h3_idempotent_creation_versioned_approval_and_legacy_guard(tmp_path, monkeypatch):
    m, contract, _ = fixture(tmp_path, monkeypatch)
    async def scenario():
        headers = {"Authorization": "Bearer test"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.app), base_url="http://test", headers=headers) as client:
            assert (await client.get("/api/router/options", headers={"Authorization": "bad"})).status_code == 401
            body = {"operation_id": "create-one", "mode": "t2v", "prompt": "scene"}
            project = (await client.post("/api/router/projects", data=body)).json()
            assert (await client.post("/api/router/projects", data=body)).json()["id"] == project["id"]
            assert len(m.STORE.list()) == 1
            assert (await client.post("/api/router/projects", data={**body, "prompt": "different"})).status_code == 409
            key = project["id"]
            route = f"/api/router/projects/{key}/stages/context_ir"
            started = (await client.post(route + "/start", json={"operation_id": "start", "expected_run_id": None})).json()
            run_id = started["pipeline"][0]["run_id"]
            def complete(value):
                value["prompt_ir"] = "version one"
                value["stages"]["context_ir"].update(status="awaiting_approval", progress=100)
            m.STORE.update(key, complete)
            output_id = m.STORE.get(key)["stages"]["context_ir"]["output_id"]
            stale = await client.post(route + "/approve", json={
                "operation_id": "bad", "expected_run_id": run_id, "expected_output_id": "stale", "prompt": "approved",
            })
            assert stale.status_code == 409
            approval = {"operation_id": "approve", "expected_run_id": run_id,
                        "expected_output_id": output_id, "prompt": "edited approved"}
            assert (await client.post(route + "/approve", json=approval)).status_code == 200
            assert (await client.post(route + "/approve", json=approval)).status_code == 200
            value = m.STORE.get(key)
            assert value["stages"]["preview"]["status"] == "pending"
            assert value["router_outputs"][output_id]["text"] == "version one"
            assert value["prompt_approved"] == "edited approved"
            assert (await client.post(f"/api/projects/{key}/context-ir/approve", json={"prompt": "unsafe"})).status_code == 409
    asyncio.run(scenario())


def test_h3_atomic_receipt_rollback_and_old_callback_fencing(tmp_path, monkeypatch):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    original = m.create_project()
    key = original["id"]
    def mark(value):
        value["router_managed"] = True
        value["stages"]["context_ir"].update(status="queued")
    m.STORE.update(key, mark)
    first_run = m.STORE.get(key)["stages"]["context_ir"]["run_id"]
    def bad_mutation():
        m.STORE.update(key, lambda value: value.update(prompt_ir="must rollback"))
        raise ValueError("transaction failed")
    with pytest.raises(ValueError):
        contract.operation("bad", "digest", key, bad_mutation)
    assert m.STORE.get(key)["prompt_ir"] == ""
    with contract.connect() as db:
        assert db.execute("SELECT count(*) FROM router_operations").fetchone()[0] == 0
    m.STORE.update(key, lambda value: value["stages"]["context_ir"].update(status="failed"))
    m.start_context_ir(key)
    assert m.STORE.get(key)["stages"]["context_ir"]["run_id"] != first_run
    contract.local.execution = (key, "context_ir", first_run)
    try:
        with pytest.raises(extension.StaleRun):
            m.STORE.update(key, lambda value: value.update(prompt_ir="late result"))
    finally:
        contract.local.execution = None
    assert m.STORE.get(key)["prompt_ir"] == ""
