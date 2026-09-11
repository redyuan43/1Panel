import importlib
import io
import json
from pathlib import Path
import urllib.error

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from test_connector_api import studio, studio_factory
from test_multimodal_fleet import api_module, fleet_case


@pytest.fixture
def chain(studio, fleet_case, monkeypatch, tmp_path):
    case = fleet_case
    app = FastAPI()
    api_module.install(case.fleet, app, lambda request: None)
    jobs, submissions = {}, []

    @app.get("/api/jobs/by-execution/{identifier}")
    def get(identifier):
        from fastapi import HTTPException
        if identifier not in jobs:
            raise HTTPException(404)
        return jobs[identifier]

    @app.post("/prompt")
    async def submit(request: Request):
        body = await request.json()
        metadata = body["extra_data"]["h3"]
        binding = case.catalog.validate(metadata["recipe_id"], body["prompt"], metadata["recipe_version"])
        api_module.verify_assets(metadata["contract"]["assets"], binding, case.root)
        result = {"prompt_id": "simulated-owned-execution"}
        jobs[metadata["execution_id"]] = result
        submissions.append(body)
        return result

    module = importlib.import_module(studio.module.__package__ + ".fleet")
    key = tmp_path / "fixture-key"
    key.write_text("fixture-key-only")
    client = module.FleetClient("http://isolated-fleet.test", str(key))
    monkeypatch.setattr(studio.module, "COMFY", client)
    importlib.import_module(studio.module.__package__ + ".multimodal_client").install(studio.module)
    with TestClient(app) as transport:
        class Opener:
            lose_response = False

            def open(self, request, timeout):
                body = request.data
                if hasattr(body, "read"):
                    body = body.read()
                response = transport.request(request.method, request.full_url, headers=dict(request.header_items()), content=body)
                if request.full_url.endswith("/prompt") and self.lose_response:
                    raise urllib.error.URLError("simulated response loss after durable submission")
                if response.status_code >= 300:
                    raise urllib.error.HTTPError(request.full_url, response.status_code, "fixture", {}, io.BytesIO(response.content))
                return io.BytesIO(response.content)
        client.opener = Opener()
        yield client, case, submissions, jobs


def recipe(case):
    return {"profile_id": "H3_I2V_QUALITY14", "profile_version": "fixture-v1", "input_sha256": "a" * 64,
            "assets": {"first_frame": case.asset}}


def test_real_fleet_client_binds_assets_prepares_full_graph_and_submits_only_once(chain):
    client, case, submissions, _ = chain
    asset = {**case.asset, "path": str(case.root / case.asset["comfy_name"])}
    client.upload_assets({"first_frame": asset})
    prepared = []
    result = client.submit_stage(case.graph, "stable-execution", "preview", "preview", recipe=recipe(case), prepared=lambda graph, binding: prepared.append(binding))
    assert result == "simulated-owned-execution" and len(prepared) == 1
    assert prepared[0]["assets"]["first_frame"]["sha256"] == case.asset["sha256"]
    assert client.submit_stage(case.graph, "stable-execution", "preview", "preview", recipe=recipe(case)) == result
    assert len(submissions) == 1
    assert submissions[0]["extra_data"]["h3"]["recipe_id"] == "H3_I2V_QUALITY14"


def test_response_loss_retains_execution_identity_instead_of_resubmitting(chain, studio):
    client, case, submissions, jobs = chain
    client.opener.lose_response = True
    with pytest.raises(studio.module.SubmissionUnknown):
        client.submit_stage(case.graph, "stable-unknown", "preview", "preview", recipe=recipe(case))
    assert "stable-unknown" in jobs
    client.opener.lose_response = False
    assert client.submit_stage(case.graph, "stable-unknown", "preview", "preview", recipe=recipe(case)) == "simulated-owned-execution"
    assert len(submissions) == 1


def test_modified_asset_is_rejected_before_submission(chain):
    client, case, submissions, _ = chain
    (case.root / case.asset["comfy_name"]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="未提交生成"):
        client.submit_stage(case.graph, "tampered", "preview", "preview", recipe=recipe(case))
    assert not submissions
