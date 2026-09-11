from __future__ import annotations

import copy
import hashlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from test_connector_api import PREFIX, OWNER, connector_api, studio, studio_factory


def picture():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 48), "pink").save(buffer, "PNG")
    return buffer.getvalue()


def upload(studio, kind="first_frame", content=None, operation=None, owner=None):
    content = picture() if content is None else content
    metadata = {"operation_id": operation or uuid4().hex, "kind": kind, "filename": "photo.png",
                "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    headers = {**studio.headers, "x-h3-upload-metadata": json.dumps(metadata)}
    if owner:
        headers["X-H3-Connector-Owner"] = owner
    return studio.client.post(PREFIX + "/assets/uploads", content=content, headers=headers)


def test_upload_is_owned_immutable_and_idempotent(studio):
    operation = uuid4().hex
    response = upload(studio, operation=operation)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "ready" and result["sha256"] == hashlib.sha256(picture()).hexdigest()
    assert "path" not in result and "owner" not in result
    assert upload(studio, operation=operation).json() == result
    assert upload(studio, operation=operation, kind="last_frame").status_code == 409
    assert studio.client.get(PREFIX + "/assets/uploads/" + operation, headers=studio.headers).json() == result
    assert studio.client.get(PREFIX + "/assets/" + result["asset_id"] + "/content", headers=studio.headers).content == picture()
    headers = {**studio.headers, "X-H3-Connector-Owner": "different-owner"}
    assert studio.client.get(PREFIX + "/assets/" + result["asset_id"] + "/content", headers=headers).status_code == 404


def test_upload_invalid_image_and_size_are_rejected(studio):
    assert upload(studio, content=b"not an image").status_code == 400
    assert upload(studio, content=b"").status_code == 413
    assert studio.fleet.submissions == []


def test_asset_mutation_is_detected(studio):
    result = upload(studio).json()
    store = studio.connector.assets
    assets = store.bind(OWNER, {"first_frame": result["asset_id"]})
    Path(assets["first_frame"]["path"]).write_bytes(b"changed")
    with pytest.raises(Exception, match="asset integrity"):
        store.bind(OWNER, {"first_frame": result["asset_id"]})


@pytest.mark.parametrize("mode,roles", [
    ("i2v", ["first_frame"]), ("l2v", ["last_frame"]), ("fl2v", ["first_frame", "last_frame"]),
    ("reference", ["reference_image"]), ("hybrid", ["first_frame", "last_frame", "reference_image"]),
])
def test_multimodal_draft_preserves_assets_and_input_revision(studio, monkeypatch, tmp_path, mode, roles):
    assets = {role: upload(studio, kind=role).json()["asset_id"] for role in roles}
    import importlib
    workflows = importlib.import_module(studio.module.__package__ + ".workflows")
    template = workflows.COMMUNITY_TEMPLATES["reference_image" if mode == "reference" else mode]
    root = tmp_path / "workflows"
    (root / template).parent.mkdir(parents=True)
    (root / template).write_text("{}")
    settings = SimpleNamespace(**vars(studio.module.SETTINGS))
    settings.workflow_root = root
    monkeypatch.setattr(studio.module, "SETTINGS", settings)
    arguments = {"operation_id": uuid4().hex, "original_prompt": "Pink room scene", "prompt": "Pink room scene",
                 "mode": mode, "assets": assets}
    response = studio.client.post(PREFIX + "/call/h3_save_draft", headers=studio.headers, json=arguments)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["mode"] == mode and draft["recipe_id"] is None
    assert {entry["kind"] for entry in draft["input_assets"]} == set(roles)
    assert draft["execution_profile"]["steps"] == 14
    assert draft["stages"]["context_ir"]["status"] == "awaiting_approval"
    assert draft["input_sha256"]
    confirm = {"operation_id": uuid4().hex, "task_id": draft["task_id"], "expected_revision": draft["revision"],
               "expected_output_id": draft["context_output_id"]}
    response = studio.client.post(PREFIX + "/call/h3_confirm_prompt", headers=studio.headers, json=confirm)
    assert response.status_code == 200, response.text
    approved = response.json()
    role = roles[0]
    replacement = upload(studio, kind=role).json()["asset_id"]
    response = studio.client.post(PREFIX + "/call/h3_save_draft", headers=studio.headers,
        json={**arguments, "operation_id": uuid4().hex, "task_id": draft["task_id"], "expected_revision": approved["revision"],
              "assets": {**assets, role: replacement}})
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["input_sha256"] != draft["input_sha256"] and edited["prompt_approved"] == ""
    assert len(edited["history"]) == 1
    assert studio.fleet.submissions == []


def test_t2v_rejects_an_uploaded_first_frame(studio):
    asset = upload(studio).json()["asset_id"]
    response = studio.client.post(PREFIX + "/call/h3_save_draft", headers=studio.headers, json={
        "operation_id": uuid4().hex, "original_prompt": "Scene", "prompt": "Scene", "assets": {"first_frame": asset}})
    assert response.status_code == 400


def test_graph_must_use_every_asset_in_output(studio):
    project = {"assets": {"first_frame": {"comfy_name": "source.png"}}}
    graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "source.png"}},
             "2": {"class_type": "SaveVideo", "inputs": {"video": ["1", 0]}}}
    connector_api.input_contract.check_graph_assets(graph, project)
    detached = copy.deepcopy(graph)
    detached["2"]["inputs"] = {}
    with pytest.raises(Exception, match="disconnected"):
        connector_api.input_contract.check_graph_assets(detached, project)


def test_abandoned_upload_is_reconciled_without_recreating(studio):
    store = studio.connector.assets
    content = picture()
    operation = uuid4().hex
    metadata = {"operation_id": operation, "kind": "first_frame", "filename": "photo.png",
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    identifier, _ = store.reserve(OWNER, metadata)
    directory, target = store.paths(identifier, metadata)
    directory.mkdir(parents=True)
    (directory / "upload.png").write_bytes(content[:20])
    with store.contract.connect() as database:
        database.execute("UPDATE connector_assets SET updated_at=? WHERE asset_id=?", (time.time() - 601, identifier))
    response = studio.client.get(PREFIX + "/assets/uploads/" + operation, headers=studio.headers)
    assert response.status_code == 200
    assert response.json()["state"] == "failed"
    assert response.json()["recovery_reason"] == "upload_lease_expired"
    assert not list(directory.iterdir())
    assert upload(studio, operation=operation).status_code == 409


def test_cleanup_preserves_ready_and_in_progress_uploads(studio):
    ready = upload(studio).json()
    store = studio.connector.assets
    metadata = {"operation_id": uuid4().hex, "kind": "first_frame", "filename": "photo.png",
                "size": len(picture()), "sha256": hashlib.sha256(picture()).hexdigest()}
    identifier, _ = store.reserve(OWNER, metadata)
    assert store.recover_expired() == 0
    assert store.lookup(OWNER, asset_id=identifier)["state"] == "uploading"
    assert store.bind(OWNER, {"first_frame": ready["asset_id"]})["first_frame"]["sha256"] == ready["sha256"]


def test_declared_size_and_quota_are_not_trusted(studio, monkeypatch):
    monkeypatch.setitem(studio.connector.assets.reserve.__globals__, "OWNER_QUOTA", len(picture()))
    assert upload(studio).status_code == 200
    assert upload(studio).status_code == 413


@pytest.mark.parametrize("field,value", [("duration", 5), ("orientation", "landscape"), ("audio_policy", "reference")])
def test_multimodal_schema_does_not_relax_text_recipe_scope(studio, field, value):
    response = studio.client.post(PREFIX + "/call/h3_save_draft", headers=studio.headers, json={
        "operation_id": uuid4().hex, "original_prompt": "Scene", "prompt": "Scene", field: value})
    assert response.status_code == 400
    assert studio.fleet.submissions == []


def test_photo_draft_approval_and_simulated_execution_keep_identical_asset_binding(studio, monkeypatch):
    from test_quality_workflows import workflows, ROOT
    asset = upload(studio).json()
    draft = studio.draft(mode="i2v", assets={"first_frame": asset["asset_id"]})
    profile = draft["execution_profile"]
    original_catalog = studio.fleet.recipe_catalog
    monkeypatch.setattr(studio.fleet, "recipe_catalog", lambda: {**original_catalog(), "multimodal_profiles": [
        {"profile_id": profile["profile_id"], "version": profile["version"], "qualified": True}]})
    monkeypatch.setattr(studio.fleet, "upload_assets", lambda assets: studio.fleet.uploads.append(copy.deepcopy(assets)))

    def build(project, stage, root):
        graph, template = workflows.build_workflow(project, "proof", ROOT)
        connector_api.input_contract.bind_graph_assets(graph, project)
        return graph, template

    def submit(graph, execution_id, stage, profile_name, *, recipe, prepared):
        assert stage == "preview" and recipe["profile_id"] == profile["profile_id"]
        prepared(graph, {**recipe, "recipe_id": recipe["profile_id"], "recipe_version": recipe["profile_version"]})
        studio.fleet.submissions.append({"execution_id": execution_id, "recipe": {"recipe_id": recipe["profile_id"]}})
        return "fake-photo-prompt"

    monkeypatch.setattr(studio.module, "build_workflow", build)
    monkeypatch.setattr(studio.fleet, "submit_stage", submit)
    completed = studio.finish(studio.start(studio.confirm(draft)))
    assert completed["preview"]["status"] == "awaiting_approval", completed
    assert len(studio.fleet.submissions) == 1
    assert studio.fleet.uploads[0]["first_frame"]["sha256"] == asset["sha256"]
    assert completed["input_sha256"] == draft["input_sha256"]
    assert completed["input_assets"][0]["sha256"] == asset["sha256"]


@pytest.mark.parametrize("has_audio", [True, False, None])
def test_embedded_audio_selection_requires_a_decoded_audio_track(studio, monkeypatch, has_audio):
    content = b"isolated-video-fixture"
    metadata = {"operation_id": uuid4().hex, "kind": "reference_video", "filename": "original.mp4",
                "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    monkeypatch.setitem(studio.connector.assets.finish.__globals__, "probe", lambda *args: {
        "mime": "video/mp4", "duration": 5, "width": 480, "height": 864, "has_audio": has_audio})
    uploaded = studio.client.post(PREFIX + "/assets/uploads", content=content,
        headers={**studio.headers, "x-h3-upload-metadata": json.dumps(metadata)})
    assert uploaded.status_code == 200
    response = studio.call("h3_save_draft", {"operation_id": uuid4().hex, "original_prompt": "Keep the source audio",
        "prompt": "Keep the source audio", "mode": "reference", "audio_policy": "reference",
        "use_embedded_video_audio": True, "assets": {"reference_video": uploaded.json()["asset_id"]}})
    assert response.status_code == (200 if has_audio is True else 400), response.text
    assert not studio.fleet.submissions
    assert not studio.pending


def test_text_draft_cannot_silently_drop_embedded_audio_setting(studio):
    response = studio.call("h3_save_draft", {"operation_id": uuid4().hex, "original_prompt": "Keep the source audio",
        "prompt": "Keep the source audio", "use_embedded_video_audio": True})
    assert response.status_code == 400
    assert not studio.fleet.submissions
