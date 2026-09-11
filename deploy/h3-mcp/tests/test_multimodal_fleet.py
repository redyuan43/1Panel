from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from test_quality_workflows import ROOT, workflows, contract


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "fleet" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalog_module = load("multimodal_catalog")
api_module = load("multimodal_api")


@pytest.fixture
def fleet_case(tmp_path):
    source_bytes = b"verified-by-studio-decoder"
    identifier = "asset_" + "a" * 32
    filename = identifier + ".png"
    asset = {"asset_id": identifier, "comfy_name": filename, "sha256": hashlib.sha256(source_bytes).hexdigest(), "size": len(source_bytes)}
    project = {"id": "offline", "mode": "i2v", "duration": 15, "orientation": "portrait", "audio_policy": "native",
               "prompt_approved": "Approved prompt", "seed": 123, "assets": {"first_frame": asset}}
    graph, _ = workflows.build_workflow(project, "proof", ROOT)
    contract.bind_graph_assets(graph, project)
    template = tmp_path / "i2v.json"
    raw = json.dumps(graph).encode()
    template.write_bytes(raw)
    entry = {"profile_id": "H3_I2V_QUALITY14", "mode": "i2v", "family": "FL2VA", "audio_policy": "native", "version": "fixture-v1",
             "template": template.name, "template_sha256": hashlib.sha256(raw).hexdigest(), "structure_sha256": catalog_module.structure(graph),
             "sampling": {"steps": 14, "sampler_node": "10"}, "asset_roles": {"first_frame": "20"},
             "weights": [{"filename": "minimax_h3_fl2va_int8_convrot.safetensors", "sha256": "1" * 64}]}
    manifest = tmp_path / "profiles.json"
    manifest.write_text(json.dumps({"schema_version": 1, "profiles": [entry]}))
    base = SimpleNamespace(public=lambda: {"recipes": [{"recipe_id": "A4"}]}, get=lambda name: {"recipe_id": name})
    catalog = catalog_module.MultimodalCatalog(base, manifest)
    root = tmp_path / "input-assets"
    root.mkdir()
    (root / filename).write_bytes(source_bytes)
    backend_root = tmp_path / "runtime"
    (backend_root / "input").mkdir(parents=True)
    effective_input = tmp_path / "state/input"
    effective_input.mkdir(parents=True)
    backend = {"id": "native", "runtime_root": str(backend_root), "input_root": str(effective_input)}
    fleet = SimpleNamespace(recipes=SimpleNamespace(catalog=catalog, enabled=True, backends={"native": backend},
                            qualifications=lambda *args: True), store=SimpleNamespace(path=tmp_path / "fleet.sqlite3"),
                            policy=SimpleNamespace(data={"resources": {"min_offload_free_gib": 40}}))
    return SimpleNamespace(fleet=fleet, graph=graph, asset=asset, bytes=source_bytes, backend=backend, root=root, catalog=catalog)


def test_full_profile_keeps_text_catalog_and_freezes_actual_shape(fleet_case):
    case = fleet_case
    binding = case.catalog.validate("H3_I2V_QUALITY14", case.graph, "fixture-v1")
    assert binding["frame_count"] == 362 and binding["steps"] == 14
    assert binding["input_roles"] == {"first_frame": case.asset["comfy_name"]}
    assert case.catalog.public()["recipes"] == [{"recipe_id": "A4"}]
    assert case.catalog.get("A4")["recipe_id"] == "A4"
    landscape = copy.deepcopy(case.graph)
    landscape["5"]["inputs"].update(width=864, height=480, length=124)
    assert case.catalog.validate("H3_I2V_QUALITY14", landscape, "fixture-v1")["actual_duration"] == 124 / 24


@pytest.mark.parametrize("node,field,value", [
    ("1", "unet_name", "retired-T8.safetensors"), ("7", "steps", 4), ("8", "sampler_name", "euler"),
    ("5", "width", 1920), ("5", "length", 1000), ("20", "image", "../../private.png"),
    ("14", "filename_prefix", "/tmp/video"), ("6", "noise_seed", -1),
])
def test_profile_rejects_weight_node_spec_and_file_forgery(fleet_case, node, field, value):
    graph = copy.deepcopy(fleet_case.graph)
    graph[node]["inputs"][field] = value
    with pytest.raises(ValueError):
        fleet_case.catalog.validate("H3_I2V_QUALITY14", graph, "fixture-v1")


def test_materialized_input_is_verified_and_never_overwritten(fleet_case):
    case = fleet_case
    binding = case.catalog.validate("H3_I2V_QUALITY14", case.graph, "fixture-v1")
    binding["assets"] = {"first_frame": case.asset}
    api_module.materialize(case.fleet, case.backend, binding)
    target = Path(case.backend["input_root"]) / case.asset["comfy_name"]
    assert target.read_bytes() == case.bytes
    assert not (Path(case.backend["runtime_root"]) / "input" / case.asset["comfy_name"]).exists()
    api_module.materialize(case.fleet, case.backend, binding)
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        api_module.materialize(case.fleet, case.backend, binding)
    assert target.read_bytes() == b"changed"


def test_fleet_prepare_checks_roles_hash_and_qualification_without_submitting(fleet_case):
    case = fleet_case
    app = FastAPI()
    def authenticated(request):
        if request.headers.get("authorization") != "Bearer fixture":
            raise HTTPException(401, "authentication required")
    api_module.install(case.fleet, app, authenticated)
    body = {"profile_id": "H3_I2V_QUALITY14", "profile_version": "fixture-v1", "input_sha256": "a" * 64,
            "prompt": case.graph, "assets": {"first_frame": case.asset}}
    with TestClient(app) as client:
        url = "/api/router/multimodal-workflow"
        assert client.post(url, json=body).status_code == 401
        headers = {"Authorization": "Bearer fixture"}
        result = client.post(url, json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["binding"]["assets"]["first_frame"]["sha256"] == case.asset["sha256"]
        wrong_role = {**body, "assets": {"last_frame": case.asset}}
        assert client.post(url, json=wrong_role, headers=headers).status_code == 409
        case.fleet.recipes.qualifications = lambda *args: False
        assert client.post(url, json=body, headers=headers).status_code == 409


def test_fleet_upload_receipt_supports_response_loss_and_limits(fleet_case, monkeypatch):
    case = fleet_case
    app = FastAPI()
    api_module.install(case.fleet, app, lambda request: None)
    monkeypatch.setattr(api_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3))
    filename = "asset_" + "b" * 32 + ".png"
    url = "/api/router/input-assets/" + filename
    params = {"sha256": case.asset["sha256"], "size": case.asset["size"]}
    with TestClient(app) as client:
        assert client.get(url).status_code == 404
        result = client.put(url, params=params, content=case.bytes)
        assert result.status_code == 200, result.text
        assert client.get(url).json() == result.json()
        assert client.put(url, params=params, content=case.bytes).status_code == 409
        assert client.put(url + "x", params=params, content=case.bytes).status_code == 400
        missing = "/api/router/input-assets/asset_" + "c" * 32 + ".png"
        assert client.put(missing, params={**params, "size": 128 * 1024**2 + 1}, content=b"").status_code == 413
