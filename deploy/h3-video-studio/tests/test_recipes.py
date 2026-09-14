import copy
import hashlib
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.fleet import FleetClient, SubmissionUnknown
from app.recipes import RECIPE_IDS, confirmed_recipe, execution_info, recipe_scope
from test_batch_api import ready_project, setup_stores


def catalog(enabled=True):
    return {"enabled": enabled, "default_recipe_id": "A4", "recipes": [
        {"recipe_id": identifier, "version": "v1", "label": identifier} for identifier in RECIPE_IDS]}


def graph_response(identifier="B8"):
    graph = {"10": {"class_type": "VDNTestSampler", "inputs": {"steps": 8}}}
    digest = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()
    return {"enabled": True, "prompt": graph, "recipe_binding": {
        "recipe_id": identifier, "recipe_version": "v1", "graph_sha256": digest}}


@pytest.mark.parametrize("identifier", RECIPE_IDS)
def test_canonical_graph_is_submitted_without_rewriting_user_inputs(tmp_path, identifier):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    calls, prepared = [], []
    request = {"recipe_id": identifier, "prompt": " 原始人像\n不能换人物 ", "seed": 2**63 - 2,
               "filename_prefix": "video/h3-video-studio/example/preview"}
    original = copy.deepcopy(request)
    response = graph_response(identifier)

    def invoke(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        if path.startswith("/api/jobs/by-execution/"):
            raise urllib.error.HTTPError(path, 404, "", {}, None)
        if path == "/api/router/options":
            return {"recipe_catalog": catalog()}
        if path == "/api/router/recipe-workflow":
            assert payload == original
            return response
        assert path == "/prompt" and prepared
        assert payload["prompt"] == response["prompt"]
        assert payload["extra_data"]["h3"]["recipe_id"] == identifier
        assert payload["extra_data"]["h3"]["contract"]["recipe_id"] == identifier
        return {"prompt_id": "accepted"}

    fleet._request = invoke
    assert fleet.submit_stage({"legacy": {}}, "execution", "preview", "preview", recipe=request,
        prepared=lambda graph, binding: prepared.append((graph, binding))) == "accepted"
    assert request == original
    assert [path for _, path, _ in calls][-2:] == ["/api/router/recipe-workflow", "/prompt"]


@pytest.mark.parametrize("failure", ["disabled", "absent", "unreachable", "id", "version", "hash", "build_disabled", "build_timeout"])
def test_recipe_failure_never_submits_or_marks_unknown(tmp_path, failure):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    response = graph_response()
    calls = []
    if failure in {"id", "version", "hash"}:
        response["recipe_binding"][{"id": "recipe_id", "version": "recipe_version", "hash": "graph_sha256"}[failure]] = "wrong"
    if failure == "build_disabled":
        response["enabled"] = False

    def invoke(method, path, payload=None, **kwargs):
        calls.append(path)
        if path.startswith("/api/jobs/"):
            raise urllib.error.HTTPError(path, 404, "", {}, None)
        if path == "/api/router/options":
            if failure == "unreachable":
                raise TimeoutError()
            return {} if failure == "absent" else {"recipe_catalog": catalog(failure != "disabled")}
        if path == "/api/router/recipe-workflow":
            if failure == "build_timeout":
                raise TimeoutError()
            return response
        pytest.fail("must not submit a rejected graph")

    fleet._request = invoke
    with pytest.raises(RuntimeError, match="配方调度未就绪") as caught:
        fleet.submit_stage({}, "execution", "preview", "preview", recipe={"recipe_id": "B8"})
    assert not isinstance(caught.value, SubmissionUnknown)
    assert "/prompt" not in calls


@pytest.mark.parametrize("change", [{"duration": 5}, {"orientation": "landscape"}, {"mode": "i2v"},
                                    {"audio_policy": "lock_source"}])
def test_recipe_scope_does_not_expand(change):
    project = {"mode": "t2v", "duration": 15, "orientation": "portrait", "audio_policy": "native"}
    assert recipe_scope(project)
    assert not recipe_scope(project, "local_768")
    assert not recipe_scope(project, "proof")
    assert not recipe_scope({**project, **change})


@pytest.fixture
def studio(tmp_path, monkeypatch):
    from app import main

    store, _ = setup_stores(main, tmp_path, monkeypatch)
    monkeypatch.setattr(main, "SETTINGS", replace(main.SETTINGS, data_root=tmp_path))
    monkeypatch.setattr(main, "_spawn", lambda *args: None)
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    monkeypatch.setattr(fleet, "recipe_catalog", lambda: catalog())
    monkeypatch.setattr(main, "COMFY", fleet)
    yield main, store, TestClient(main.app), fleet


def test_new_project_defaults_but_legacy_requires_explicit_selection(studio, tmp_path):
    main, store, client, _ = studio
    created = client.post("/api/projects", data={"mode": "t2v", "prompt": "原提示词",
        "orientation": "portrait", "duration": 15, "prompt_processing": "manual"})
    assert created.status_code == 200
    assert created.json()["recipe_id"] == "A4"
    assert store.get(created.json()["id"])["prompt_original"] == "原提示词"
    project = ready_project(main, "legacy")
    project["orientation"] = "portrait"
    artifact = tmp_path / "old.mp4"
    artifact.write_bytes(b"old artifact")
    project["stages"]["preview"].update(artifact=str(artifact))
    store.save(project)
    endpoint = "/api/projects/legacy/stages/preview/start"
    assert client.post(endpoint, json={}).status_code == 409
    for retired in ("R0", "C0", "C1", "D4", "A8", "A4_C05", "unknown"):
        assert client.post(endpoint, json={"recipe_id": retired}).status_code == 409
    assert store.get("legacy")["stages"]["preview"]["artifact"] == str(artifact)
    response = client.post(endpoint, json={"recipe_id": "A4_C0"})
    assert response.status_code == 200
    updated = response.json()
    assert updated["recipe_id"] == "A4_C0"
    assert updated["prompt_original"] == project["prompt_original"]
    assert updated["prompt_approved"] == project["prompt_approved"]
    assert updated["seed"] == project["seed"]
    assert client.get(updated["stage_history"][0]["artifact_url"]).content == b"old artifact"


def test_old_fleet_blocks_before_queue_without_changing_artifact(studio):
    main, store, client, fleet = studio
    project = ready_project(main, "disabled")
    project.update(orientation="portrait", recipe_id="A4")
    store.save(project)
    fleet.recipe_catalog = lambda: catalog(False)
    response = client.post("/api/projects/disabled/stages/preview/start", json={})
    assert response.status_code == 409 and "配方调度未就绪" in response.text
    assert store.get("disabled")["stages"] == project["stages"]


def test_other_modes_do_not_accept_recipe(studio):
    _, _, client, _ = studio
    response = client.post("/api/projects", data={"mode": "t2v", "prompt": "prompt", "recipe_id": "A4"})
    assert response.status_code == 400
    response = client.post("/api/projects", data={"mode": "t2v", "prompt": "prompt"})
    assert response.status_code == 200 and "recipe_id" not in response.json()


def test_pending_recipe_reconciles_without_catalog_or_new_graph(studio):
    main, store, client, fleet = studio
    project = ready_project(main, "pending")
    project.update(orientation="portrait", recipe_id="B8")
    project["stages"]["preview"].update(status="failed", fleet_pending=True, execution_id="original")
    store.save(project)
    fleet.recipe_catalog = lambda: pytest.fail("reconciliation must not check new catalog")
    path = "/api/projects/pending/stages/preview/start"
    assert client.post(path, json={"recipe_id": "A4"}).status_code == 409
    assert client.post(path, json={}).status_code == 200
    assert store.get("pending")["stages"]["preview"]["execution_id"] == "original"


def test_fleet_stage_persists_canonical_graph_and_server_execution(studio, tmp_path, monkeypatch):
    main, store, _, fleet = studio
    project = ready_project(main, "canonical")
    project.update(orientation="portrait", recipe_id="B8")
    project["stages"]["preview"] = main._new_stage()
    project["prompt_approved"] = "共同 IR\n不改写"
    store.save(project)
    response = graph_response()
    monkeypatch.setattr(main, "build_workflow", lambda *args: pytest.fail("must not use local T8 graph"))

    def request(method, path, payload=None, **kwargs):
        if path.startswith("/api/jobs/"):
            raise urllib.error.HTTPError(path, 404, "", {}, None)
        if path == "/api/router/recipe-workflow":
            assert payload["prompt"] == project["prompt_approved"]
            assert payload["seed"] == project["seed"]
            assert payload["filename_prefix"] == "video/h3-video-studio/canonical/preview"
            return response
        if path == "/prompt":
            return {"prompt_id": "prompt-id"}
        pytest.fail(path)

    fleet._request = request

    def wait(execution_id, destination, **kwargs):
        assert destination.name == execution_id + ".mp4"
        destination.write_bytes(b"CPU fixture only")
        kwargs["progress"]({"execution": {"gpu_uuid": "server-gpu", "runtime_version": "server-version"}})
        return {"execution": {"execution_seconds": 789.079, "backend_id": "vdn"}}

    fleet.wait_execution = wait
    main._run_fleet_stage("canonical", "preview")
    stage = store.get("canonical")["stages"]["preview"]
    assert json.loads(Path(stage["workflow_path"]).read_text()) == response["prompt"]
    assert stage["execution"]["contract"] == response["recipe_binding"]
    assert stage["execution"]["gpu_uuid"] == "server-gpu"
    assert stage["execution"]["execution_seconds"] == 789.079
    assert stage["status"] == "awaiting_approval"


def test_execution_public_only_copies_reported_fields():
    result = execution_info({"execution": {"recipe_id": "B8", "gpu_uuid": "GPU-real",
        "backend_id": "vdn", "runtime_version": "r2", "execution_seconds": 789.079,
        "private_path": "/private"}, "gpu_uuid": None})
    assert result["gpu_uuid"] == "GPU-real"
    assert result["execution_seconds"] == 789.079
    assert "private_path" not in result
    assert execution_info({"lane_id": "fast"}) == {}


def test_recipe_job_reads_actual_public_execution(tmp_path):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    calls = []

    def request(method, path):
        calls.append(path)
        if path.startswith("/api/jobs/by-execution/"):
            return {"recipe_id": "B8", "status": "running", "prompt_id": "prompt"}
        return {"gpu_uuid": "actual-uuid", "runtime_version": "actual-core", "backend_id": "vdn"}

    fleet._request = request
    result = execution_info(fleet.execution("execution"))
    assert result["gpu_uuid"] == "actual-uuid"
    assert calls == ["/api/jobs/by-execution/execution", "/api/router/executions/execution"]


def test_recipe_capacity_is_forwarded_without_inventing_qualification(tmp_path):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    capacity = {"queues": [], "active": [], "resources": {}, "policy": {
        "short": {"preview": {"max_parallel": 3, "max_frames": 362}, "quality": {"max_parallel": 1}},
        "long": {"max_parallel": 1}}, "recipe_capacity": {"B8": {
            "available_slots": 0, "eligible_lanes": [], "reasons": ["not_qualified"],
            "recipe_version": "v1", "private_path": "hidden"}}}
    fleet._request = lambda method, path, **kwargs: {"lanes": []} if path == "/api/health" else capacity
    result = fleet.capacity()
    assert result["recipe_capacity"]["B8"] == {
        "available_slots": 0, "eligible_lanes": [], "reasons": ["not_qualified"], "recipe_version": "v1"}
    assert "A4" not in result["recipe_capacity"]


@pytest.mark.parametrize("enabled", [False, None, "true", 1])
def test_server_enabled_must_be_boolean_true(enabled):
    with pytest.raises(RuntimeError):
        confirmed_recipe(catalog(enabled), "A4")
