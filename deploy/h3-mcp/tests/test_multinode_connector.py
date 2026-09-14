"""Device selection must use the same browser/connector contract as preview."""
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from test_connector_api import STUDIO_ROOT, studio, studio_factory


def enable_node_selection(studio):
    studio.fleet.is_multifleet = True

    def validate(target):
        if target not in {"auto", "ivan", "ivan-u24", "edge"}:
            raise ValueError("unknown device")
        return target

    studio.fleet.validate_target = validate


def test_packaged_schema_matches_runtime_authority(studio):
    import pytest
    schema_path = STUDIO_ROOT / "app" / "connector_schema.json"
    if not schema_path.is_file():
        pytest.skip("schema is generated only in a prepared release")
    native = importlib.import_module(studio.module.__package__ + ".connector_api")
    assert json.loads(schema_path.read_text()) == native.TOOL_DEFINITIONS


@pytest.mark.parametrize("target", ["ivan-u24", "edge"])
def test_browser_explicit_device_passes_authoritative_schema(studio, monkeypatch, target):
    task = studio.confirm(studio.draft())
    enable_node_selection(studio)
    monkeypatch.setenv("H3_MCP_ADMIN_USERS", "admin@example.test")
    studio.module.app.state.mcp_management_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"writes_enabled": True, "generation_enabled": True}))
    args = studio.arguments(task, expected_output_id=task["context_output_id"],
                            expected_run_id=None, target_node=target)
    # No lifespan: a synthetic pending worker is captured, never run.
    client = TestClient(studio.module.app, client=("127.0.0.1", 5678))
    response = client.post("/api/h3-browser/call/h3_start_preview", json=args,
                           headers={"tailscale-user-login": "admin@example.test"})
    assert response.status_code == 200, response.text
    assert response.json()["stages"]["preview"]["target_node"] == target
    assert len(studio.pending) == 1 and not studio.fleet.submissions


def test_omitted_target_keeps_unknown_execution_device(studio):
    studio.fleet.unknown_wait = True
    unknown = studio.finish(studio.start(studio.confirm(studio.draft())))
    execution = unknown["preview"]["execution_id"]
    studio.module.STORE.update(unknown["task_id"], lambda item:
        item["stages"]["preview"].update(target_node="ivan-u24", node_id="ivan-u24"))
    enable_node_selection(studio)
    unknown = studio.get(unknown)
    resumed = studio.start(unknown)
    stage = studio.module.STORE.get(unknown["task_id"])["stages"]["preview"]
    assert stage["target_node"] == "ivan-u24" and stage["execution_id"] == execution
    assert len(studio.pending) == 1 and len(studio.fleet.submissions) == 1


def test_unknown_execution_cannot_switch_to_other_device(studio):
    studio.fleet.unknown_wait = True
    unknown = studio.finish(studio.start(studio.confirm(studio.draft())))
    studio.module.STORE.update(unknown["task_id"], lambda item:
        item["stages"]["preview"].update(target_node="ivan-u24", node_id="ivan-u24"))
    enable_node_selection(studio)
    unknown = studio.get(unknown)
    args = studio.arguments(unknown, expected_output_id=unknown["context_output_id"],
                            expected_run_id=unknown["preview"]["run_id"], target_node="ivan")
    response = studio.call("h3_start_preview", args)
    assert response.status_code == 409
    assert not studio.pending and len(studio.fleet.submissions) == 1
