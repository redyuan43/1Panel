from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import types
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request


CONNECTOR_PATH = Path(__file__).resolve().parents[1] / "studio" / "connector_api.py"
DEFAULT_STUDIO_ROOT = Path(__file__).resolve().parents[2] / "h3-video-studio"
STUDIO_ROOT = Path(os.environ.get(
    "H3_CONNECTOR_TEST_STUDIO_ROOT",
    str(DEFAULT_STUDIO_ROOT)))
SPEC = importlib.util.spec_from_file_location("h3_connector_under_test", CONNECTOR_PATH)
connector_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connector_api)
PREFIX = connector_api.API_PREFIX
OWNER = "client-test-a"
INTERNAL_SECRET = "isolated-test-internal-secret"


def forbidden(*args, **kwargs):
    raise AssertionError("real network, cloud, GPU or subprocess access is forbidden")


class FakeFleet:
    is_fleet = True

    def __init__(self):
        self.submissions = []
        self.waits = []
        self.uploads = []
        self.catalog_reads = 0
        self.version = "v1"
        self.catalog_enabled = True
        self.unknown_submit = False
        self.unknown_wait = False
        self.artifact = b"0123456789-synthetic-preview-video"

    def recipe_catalog(self):
        self.catalog_reads += 1
        return {"enabled": self.catalog_enabled, "recipes": [
            {"recipe_id": identifier, "version": self.version,
             "trigger": "r34l1sm\n" if identifier in {"A4_C0", "A4_C1"} else "",
             "people_lora_strength": 1.0 if identifier == "A4_C1" else 0.0,
             "sampling": {"steps": 8 if identifier == "B8" else 4}}
            for identifier in connector_api.RECIPE_IDS]}

    def capacity(self):
        return {"available": True, "sampled_at": 1700000000, "active": 1, "queued": 2, "resources_ok": True,
                "lanes": [{"id": f"lane-{number}", "status": "idle"} for number in range(3)],
                "secret_path": "/private/capacity", "recipe_capacity": {
                    identifier: {"available_slots": 0, "eligible_lanes": [], "recipe_version": self.version,
                                 "reasons": ["host_ram_floor_crossed"], "api_key": "provider-secret"}
                    for identifier in connector_api.RECIPE_IDS}}

    def upload_assets(self, assets):
        assert assets == {}
        self.uploads.append(assets)

    def submit_stage(self, graph, execution_id, stage_id, profile, *, recipe, prepared):
        assert stage_id == "preview" and graph == {}
        prepared({"fake_graph": True}, {"recipe_id": recipe["recipe_id"], "recipe_version": self.version,
                                       "private_path": "/private/provider-workflow", "api_key": "provider-secret"})
        self.submissions.append({"execution_id": execution_id, "recipe": copy.deepcopy(recipe)})
        if self.unknown_submit:
            raise self.SubmissionUnknown("private-provider-error /private/path provider-secret")
        return "fake-prompt-id"

    def wait_execution(self, execution_id, destination, *, progress, cancelled):
        self.waits.append(execution_id)
        if self.unknown_wait:
            raise self.SubmissionUnknown("private-provider-error /private/path provider-secret")
        if cancelled():
            raise RuntimeError("cancelled")
        progress({"progress": 60, "detail": "private-provider-error", "execution": {
            "recipe_id": self.submissions[0]["recipe"]["recipe_id"], "recipe_version": self.version,
            "gpu_uuid": "GPU-test-uuid", "backend_id": "backend-test", "runtime_version": "runtime-test",
            "execution_seconds": 1.5}, "admission_reason": "admission_waiting:host_ram_floor_crossed"})
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.artifact)
        return {"execution_id": execution_id}


class Studio:
    def __init__(self, module, contract, connector, fleet, pending):
        self.module = module
        self.contract = contract
        self.connector = connector
        self.fleet = fleet
        self.pending = pending
        self.client = TestClient(module.app)
        self.headers = {"Authorization": "Bearer " + INTERNAL_SECRET, "X-H3-Connector-Owner": OWNER}

    def call(self, tool, arguments=None, owner=OWNER):
        return self.client.post(PREFIX + "/call/" + tool, json=arguments if arguments is not None else {},
                                headers={**self.headers, "X-H3-Connector-Owner": owner})

    def ok(self, tool, arguments=None, owner=OWNER):
        response = self.call(tool, arguments, owner)
        assert response.status_code == 200, response.text
        return response.json()

    def draft(self, **changes):
        return self.ok("h3_save_draft", {"operation_id": uuid4().hex, "original_prompt": "原始中文提示词",
                                        "prompt": "十五秒竖版草稿，原生声音。", **changes})

    def arguments(self, task, **changes):
        return {"operation_id": uuid4().hex, "task_id": task["task_id"],
                "expected_revision": task["revision"], **changes}

    def get(self, task):
        return self.ok("h3_get_task", {"task_id": task["task_id"]})

    def confirm(self, task):
        return self.ok("h3_confirm_prompt", self.arguments(task, expected_output_id=task["context_output_id"]))

    def start(self, task):
        return self.ok("h3_start_preview", self.arguments(
            task, expected_output_id=task["context_output_id"], expected_run_id=task["preview"]["run_id"]))

    def finish(self, task):
        assert len(self.pending) == 1
        self.pending.pop(0)()
        return self.get(task)

    def video(self):
        return self.finish(self.start(self.confirm(self.draft())))


@pytest.fixture
def studio_factory(tmp_path, monkeypatch):
    assert (STUDIO_ROOT / "app" / "main.py").is_file(), "set H3_CONNECTOR_TEST_STUDIO_ROOT to the Studio source"
    monkeypatch.setenv("H3_STUDIO_ROOT", str(STUDIO_ROOT))
    monkeypatch.setenv("H3_STUDIO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("MINIMAX_CREDENTIALS", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("MINIMAX_API_KEY", "")
    monkeypatch.delenv("H3_STUDIO_KEY_FILE", raising=False)
    monkeypatch.delenv("H3_STUDIO_TAILSCALE_USERS", raising=False)
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "true")
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "true")
    key_path = tmp_path / "connector-key"
    key_path.write_text(INTERNAL_SECRET + "\n")
    monkeypatch.setenv("H3_CONNECTOR_KEY_FILE", str(key_path))
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    instances = []
    packages = []

    def load(fleet=None):
        name = "isolated_h3_" + uuid4().hex
        package = types.ModuleType(name)
        package.__path__ = [str(STUDIO_ROOT / "app")]
        sys.modules[name] = package
        packages.append(name)
        fleet_module = importlib.import_module(name + ".fleet")
        fleet = fleet or FakeFleet()
        fleet.SubmissionUnknown = fleet_module.SubmissionUnknown
        monkeypatch.setattr(fleet_module, "configured_client", lambda: fleet)
        router_path = STUDIO_ROOT / "app" / "router_contract.py"
        router_source = router_path.read_text()
        native_connector = "from .connector_api import install_connector_api" in router_source
        guard_anchor = "            mutator(project)\n"
        assert router_source.count(guard_anchor) == 1
        if not native_connector:
            router_source = router_source.replace(guard_anchor, guard_anchor +
                "            validate_connector_execution(project)\n")
        router_module = types.ModuleType(name + ".router_contract")
        router_module.__file__ = str(router_path)
        router_module.__package__ = name
        router_module.validate_connector_execution = connector_api.validate_connector_execution
        sys.modules[router_module.__name__] = router_module
        exec(compile(router_source, str(router_path), "exec"), router_module.__dict__)
        original_install = router_module.install
        installed, pending = [], []
        native_instances = []
        if native_connector:
            native_module = importlib.import_module(name + ".connector_api")
            native_install = native_module.install_connector_api

            def capture_connector(module, contract):
                connector = native_install(module, contract)
                native_instances.append(connector)
                return connector

            monkeypatch.setattr(native_module, "install_connector_api", capture_connector)

        def install(module):
            module._spawn = lambda target, *args: pending.append(partial(target, *args))
            contract = original_install(module)
            if native_connector:
                assert len(native_instances) == 1
                connector = native_instances[0]
            else:
                connector = connector_api.install_connector_api(module, contract)
            installed.append((contract, connector))
            return contract

        monkeypatch.setattr(router_module, "install", install)
        module = importlib.import_module(name + ".main")
        contract, connector = installed[0]
        assert len(installed) == 1
        assert contract.original_connect.__self__ is module.STORE
        assert module.STORE._connect.__self__ is contract
        module.STORE.initialize()
        module.BATCH_STORE.initialize()
        contract.initialize()
        for attribute in ("context_ir", "regenerate_2k"):
            monkeypatch.setattr(module.MINIMAX, attribute, forbidden)
        monkeypatch.setattr(module, "build_workflow", forbidden)
        monkeypatch.setattr(module, "_run_cloud_768", forbidden)
        monkeypatch.setattr(module, "_run_2k", forbidden)
        instance = Studio(module, contract, connector, fleet, pending)
        instances.append(instance)
        return instance

    yield load
    for instance in instances:
        instance.client.close()
    for name in packages:
        for identifier in list(sys.modules):
            if identifier == name or identifier.startswith(name + "."):
                sys.modules.pop(identifier, None)


@pytest.fixture
def studio(studio_factory):
    return studio_factory()


@pytest.fixture
def api_setup(studio):
    return {"app": studio.module.app, "module": studio.module, "store": studio.module.STORE,
            "root": studio.module.SETTINGS.data_root, "source_root": STUDIO_ROOT,
            "contract": studio.contract, "connector": studio.connector, "client": studio.client,
            "headers": studio.headers, "fleet": studio.fleet, "pending": studio.pending, "studio": studio}


def test_schema_is_portable_and_has_fixed_tools():
    definitions = json.loads(json.dumps(connector_api.TOOL_DEFINITIONS))
    assert {entry["name"] for entry in definitions} == {
        "h3_capabilities", "h3_prompt_guidance", "h3_save_draft", "h3_confirm_prompt", "h3_start_preview",
        "h3_list_tasks", "h3_get_task", "h3_review_preview", "h3_cancel_task"}
    for entry in definitions:
        assert set(entry) == {"name", "description", "inputSchema", "annotations"}
        assert entry["inputSchema"]["additionalProperties"] is False
        assert set(entry["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        if not entry["annotations"]["readOnlyHint"]:
            assert "operation_id" in entry["inputSchema"]["required"]
    assert "mcp" not in connector_api.__dict__


def test_default_gates_allow_reads_but_not_writes_or_generation(studio, monkeypatch):
    monkeypatch.delenv("H3_CONNECTOR_WRITES_ENABLED")
    monkeypatch.delenv("H3_CONNECTOR_GENERATION_ENABLED")
    capabilities = studio.ok("h3_capabilities")
    assert not capabilities["writes_enabled"] and not capabilities["generation_enabled"]
    assert capabilities["inference_validated"] is False
    assert all(entry["catalog_confirmed"] for entry in capabilities["recipes"])
    assert studio.call("h3_save_draft", {"operation_id": "disabled", "original_prompt": "original",
                                         "prompt": "draft"}).status_code == 403
    assert studio.ok("h3_list_tasks")["tasks"] == []
    assert studio.pending == [] and studio.fleet.submissions == []


def test_draft_confirmation_without_generation_and_exact_verbatim(studio, monkeypatch):
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    exact = "  原始雨景\n完全不修改。\n "
    task = studio.draft(original_prompt=exact, prompt=exact, verbatim=True, seed=314,
                        skill_sources=[{"name": "local-skill", "source": "local_workbuddy"},
                                       {"name": "server-guidance-claim", "source": "server_guidance"}])
    assert task["original_prompt"] == task["prompt"] == exact
    assert task["status"] == "awaiting_prompt_approval" and task["preview"]["output_id"] is None
    assert task["context_output_id"].startswith("out_")
    assert len(task["revision"]) == 64
    assert task["recipe_id"] == "A4" and task["recipe_version"] == "v1"
    assert all(not entry["execution_verified"] and entry["reported_by"] == "client" for entry in task["skill_sources"])
    output_url = PREFIX + f"/tasks/{task['task_id']}/outputs/{task['context_output_id']}"
    assert studio.client.get(output_url, headers=studio.headers).text == exact
    confirmed = studio.confirm(task)
    assert confirmed["prompt_approved"] == exact
    assert confirmed["status"] == "ready" and confirmed["context_output_id"] == task["context_output_id"]
    assert studio.call("h3_start_preview", studio.arguments(confirmed,
        expected_output_id=confirmed["context_output_id"], expected_run_id=None)).status_code == 403
    assert studio.pending == studio.fleet.submissions == studio.fleet.uploads == []


@pytest.mark.parametrize("headers", [None, {}, {"Authorization": "Bearer studio-secret"},
    {"Authorization": "Bearer " + INTERNAL_SECRET},
    {"Authorization": "Bearer " + INTERNAL_SECRET, "X-H3-Connector-Owner": "../other"},
    {"Authorization": "Bearer " + INTERNAL_SECRET, "X-H3-Connector-Owner": "a" * 129},
    {"Authorization": "Bearer " + INTERNAL_SECRET, "X-H3-Connector-Owner": "bad owner"},
    [("Authorization", "Bearer " + INTERNAL_SECRET), ("Authorization", "Bearer " + INTERNAL_SECRET),
     ("X-H3-Connector-Owner", OWNER)],
    [("Authorization", "Bearer " + INTERNAL_SECRET), ("X-H3-Connector-Owner", OWNER),
     ("X-H3-Connector-Owner", OWNER)]])
def test_authentication_is_required_even_for_reads(studio, headers):
    response = studio.client.post(PREFIX + "/call/h3_capabilities", headers=headers, json={})
    assert response.status_code in {400, 401}
    assert studio.fleet.catalog_reads == 0


def test_exported_authorization_checks_only_rotatable_dedicated_secret(studio, monkeypatch, tmp_path):
    request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + INTERNAL_SECRET).encode())]})
    assert connector_api.connector_authorized(request) is True
    monkeypatch.setenv("H3_ROUTER_KEY", INTERNAL_SECRET)
    key = Path(os.environ["H3_CONNECTOR_KEY_FILE"])
    key.write_text("changed-internal-key")
    assert connector_api.connector_authorized(request) is False
    key.write_text("")
    assert connector_api.connector_authorized(request) is False
    monkeypatch.setenv("H3_CONNECTOR_KEY_FILE", str(tmp_path / "absent"))
    assert connector_api.connector_authorized(request) is False
    monkeypatch.delenv("H3_CONNECTOR_KEY_FILE")
    assert connector_api.connector_authorized(request) is False


@pytest.mark.parametrize("change", [
    {"duration": 14}, {"width": 864}, {"height": 480}, {"fps": 30}, {"mode": "i2v"},
    {"orientation": "landscape"}, {"audio_policy": "silent"}, {"recipe_id": "R0"}, {"recipe_id": "A8"},
    {"prompt_processing": "cloud"}, {"provider": "gpu0"}, {"stage": "local_768"},
    {"seed": True}, {"seed": 1.5}, {"seed": "1"}, {"seed": -2}, {"seed": 2**63},
    {"original_prompt": " "}, {"prompt": ""}, {"prompt": "x" * 20001}, {"prompt": "\x00bad"},
    {"operation_id": "bad/operation"}, {"operation_id": "bad\n"}, {"operation_id": "x" * 129},
    {"owner": "different"}, {"connector_owner": "different"}, {"verbatim": "true"}, {"verbatim": True},
    {"skill_sources": [{"name": "claimed", "source": "executed"}]},
    {"skill_sources": [{"name": "claimed", "source": "local_workbuddy", "executed": True}]},
    {"expected_revision": "0" * 64}, {"task_id": "existing"},
])
def test_invalid_drafts_reject_without_changes(studio, change):
    response = studio.call("h3_save_draft", {"operation_id": "invalid", "original_prompt": "original",
                                            "prompt": "draft", **change})
    assert response.status_code == 400, response.text
    assert studio.ok("h3_list_tasks")["tasks"] == []
    assert studio.pending == []


@pytest.mark.parametrize("raw", ['[]', 'null', '{"limit":NaN}', '{"limit":1,"limit":2}', '{', '"text"'])
def test_invalid_json_is_rejected(studio, raw):
    response = studio.client.post(PREFIX + "/call/h3_list_tasks", content=raw, headers=studio.headers)
    assert response.status_code == 400


def test_request_bound_and_tool_allowlist(studio):
    assert studio.client.post(PREFIX + "/call/h3_save_draft", content=b" " * (256 * 1024 + 1),
                              headers=studio.headers).status_code == 413
    for tool in ("start_stage", "h3_start_2k", "h3_delete_task"):
        assert studio.call(tool).status_code == 404


@pytest.mark.parametrize("recipe_id", connector_api.RECIPE_IDS)
def test_four_recipes_use_existing_preview_fleet_dispatch(studio, recipe_id):
    task = studio.draft(recipe_id=recipe_id, seed=27)
    queued = studio.start(studio.confirm(task))
    assert queued["preview"]["status"] == "queued" and queued["preview"]["run_id"]
    assert len(studio.pending) == 1 and studio.fleet.submissions == []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(studio.pending.pop()).result()
    completed = studio.get(task)
    assert completed["preview"]["status"] == "awaiting_approval"
    assert completed["status"] == "awaiting_preview_approval"
    assert completed["revision"] != queued["revision"]
    assert completed["preview"]["run_id"] == queued["preview"]["run_id"]
    assert completed["preview"]["recipe_id"] == recipe_id
    assert completed["preview"]["backend_id"] == "backend-test"
    assert completed["preview"]["runtime_version"] == "runtime-test"
    assert completed["preview"]["gpu_id"] == "GPU-test-uuid"
    assert completed["preview"]["execution_seconds"] == 1.5
    assert completed["preview"]["admission_reason"] == "admission_waiting:host_ram_floor_crossed"
    assert studio.fleet.submissions[0]["recipe"]["prompt"] == task["prompt"]
    assert studio.fleet.submissions[0]["recipe"]["seed"] == 27
    assert len(studio.fleet.submissions) == 1
    assert studio.module.STORE.get(task["task_id"])["stages"]["local_768"]["status"] == "pending"
    for private in ("private-provider-error", "provider-secret", "/private/", "private-gpu", "private-backend"):
        assert private not in json.dumps(completed)


def test_ownership_applies_to_all_task_routes_and_historical_unowned_projects(studio):
    task = studio.draft()
    identifier = task["task_id"]
    assert studio.call("h3_get_task", {"task_id": identifier}, "other-client").status_code == 404
    assert studio.ok("h3_list_tasks", owner="other-client")["tasks"] == []
    requests = {
        "h3_save_draft": {"original_prompt": task["original_prompt"], "prompt": "edited"},
        "h3_confirm_prompt": {"expected_output_id": task["context_output_id"]},
        "h3_start_preview": {"expected_output_id": task["context_output_id"], "expected_run_id": None},
        "h3_review_preview": {"output_id": "out_test", "expected_run_id": "run_test", "decision": "approve"},
        "h3_cancel_task": {"expected_run_id": None},
    }
    for tool, arguments in requests.items():
        assert studio.call(tool, studio.arguments(task, **arguments), "other-client").status_code == 404
    url = PREFIX + f"/tasks/{identifier}/outputs/{task['context_output_id']}"
    assert studio.client.get(url, headers={**studio.headers, "X-H3-Connector-Owner": "other-client"}).status_code == 404
    assert studio.client.get(url).status_code == 401
    studio.module.STORE.update(identifier, lambda project: project.pop("connector_owner"))
    unowned = studio.module.STORE.get(identifier)
    for tool, arguments in requests.items():
        assert studio.call(tool, studio.arguments(task, **arguments)).status_code == 404
    assert studio.module.STORE.get(identifier) == unowned
    assert studio.ok("h3_list_tasks")["tasks"] == []


def test_replay_is_owner_namespaced_and_rechecks_current_owner(studio):
    arguments = {"operation_id": "same-operation", "original_prompt": "original", "prompt": "draft"}
    first = studio.ok("h3_save_draft", arguments)
    assert studio.ok("h3_save_draft", arguments) == first
    assert studio.call("h3_save_draft", {**arguments, "prompt": "different"}).status_code == 409
    second = studio.ok("h3_save_draft", arguments, "other-client")
    assert second["task_id"] != first["task_id"]
    with studio.contract.connect() as database:
        keys = [row[0] for row in database.execute("SELECT operation_id FROM router_operations")]
    assert len(keys) == 2 and all(key.startswith("connector:") and len(key) == 74 for key in keys)
    studio.module.STORE.update(first["task_id"], lambda project: project.pop("connector_owner"))
    assert studio.call("h3_save_draft", arguments).status_code == 404


def test_start_replay_is_atomic_even_after_async_completion_and_pause(studio, monkeypatch):
    task = studio.confirm(studio.draft())
    arguments = studio.arguments(task, expected_output_id=task["context_output_id"], expected_run_id=None)
    barrier = Barrier(2)

    def start():
        barrier.wait()
        return studio.ok("h3_start_preview", arguments)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda unused: start(), range(2)))
    assert results[0] == results[1] and len(studio.pending) == 1
    completed = studio.finish(task)
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "false")
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    assert studio.ok("h3_start_preview", arguments) == results[0]
    assert studio.get(task)["revision"] == completed["revision"]
    assert len(studio.fleet.submissions) == 1 and not studio.pending
    assert studio.call("h3_start_preview", {**arguments, "expected_run_id": "run_other"}).status_code == 409
    studio.module.STORE.update(task["task_id"], lambda project: project.update(connector_owner="other-client"))
    assert studio.call("h3_start_preview", arguments).status_code == 404


def test_failed_transaction_keeps_no_state_receipt_or_dispatch(studio, monkeypatch):
    task = studio.confirm(studio.draft())
    before = studio.module.STORE.get(task["task_id"])
    original_public = studio.connector.public
    monkeypatch.setattr(studio.connector, "public", lambda project: (_ for _ in ()).throw(RuntimeError("fail receipt")))
    response = studio.call("h3_start_preview", studio.arguments(
        task, expected_output_id=task["context_output_id"], expected_run_id=None))
    assert response.status_code == 503
    assert studio.module.STORE.get(task["task_id"]) == before
    assert studio.pending == [] and studio.fleet.submissions == []
    monkeypatch.setattr(studio.connector, "public", original_public)
    assert studio.start(task)["status"] == "queued"


def test_draft_edits_preserve_history_and_invalidate_all_approvals(studio):
    completed = studio.video()
    reviewed = studio.ok("h3_review_preview", studio.arguments(completed,
        output_id=completed["preview"]["output_id"], expected_run_id=completed["preview"]["run_id"], decision="approve"))
    edited = studio.ok("h3_save_draft", studio.arguments(reviewed, original_prompt=reviewed["original_prompt"],
        prompt="新的草稿", recipe_id="B8"))
    assert edited["original_prompt"] == reviewed["original_prompt"]
    assert edited["prompt"] == "新的草稿" and edited["prompt_approved"] == ""
    assert edited["preview"]["status"] == "pending"
    assert edited["context_output_id"] != reviewed["context_output_id"]
    assert edited["history"][0]["context_output_id"] == reviewed["context_output_id"]
    assert edited["history"][0]["stages"]["preview"]["output_id"] == completed["preview"]["output_id"]
    assert edited["reviews"][0]["decision"] == "approve"
    for output in completed["outputs"]:
        response = studio.client.get(PREFIX + f"/tasks/{edited['task_id']}/outputs/{output['id']}", headers=studio.headers)
        assert response.status_code == 200
    assert studio.call("h3_confirm_prompt", studio.arguments(edited,
        expected_output_id=reviewed["context_output_id"])).status_code == 409
    assert studio.call("h3_save_draft", studio.arguments(edited,
        original_prompt="changed original", prompt="draft")).status_code == 409
    current = studio.confirm(edited)
    assert studio.call("h3_start_preview", studio.arguments(current,
        expected_output_id=reviewed["context_output_id"], expected_run_id=None)).status_code == 409
    assert len(studio.fleet.submissions) == 1 and not studio.pending


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_binds_current_output_run_and_revision_without_autoretry(studio, decision):
    completed = studio.video()
    arguments = studio.arguments(completed, output_id=completed["preview"]["output_id"],
                                 expected_run_id=completed["preview"]["run_id"], decision=decision, feedback="人工审核")
    for change in ({"expected_revision": "0" * 64}, {"output_id": completed["context_output_id"]},
                   {"expected_run_id": "run_other"}):
        assert studio.call("h3_review_preview", {**arguments, **change}).status_code == 409
    reviewed = studio.ok("h3_review_preview", arguments)
    assert studio.ok("h3_review_preview", arguments) == reviewed
    assert reviewed["status"] == ("completed" if decision == "approve" else "rejected")
    assert reviewed["preview"]["output_id"] == completed["preview"]["output_id"]
    assert len(reviewed["reviews"]) == 1
    assert len(studio.fleet.submissions) == 1 and not studio.pending


def test_edits_reject_active_runs_and_cancel_binds_exact_run_while_paused(studio, monkeypatch):
    queued = studio.start(studio.confirm(studio.draft()))
    assert studio.call("h3_save_draft", studio.arguments(queued,
        original_prompt=queued["original_prompt"], prompt="cannot edit")).status_code == 409
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "false")
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    arguments = studio.arguments(queued, expected_run_id=queued["preview"]["run_id"])
    assert studio.call("h3_cancel_task", {**arguments, "expected_run_id": "run_other"}).status_code == 409
    cancelled = studio.ok("h3_cancel_task", arguments)
    assert cancelled["preview"]["status"] == "cancelled"
    assert studio.ok("h3_cancel_task", arguments) == cancelled
    assert studio.finish(queued)["preview"]["status"] == "cancelled"
    assert studio.fleet.submissions == []


def test_local_draft_cancel_never_dispatches_and_cannot_cancel_new_preview(studio):
    task = studio.draft()
    cancelled = studio.ok("h3_cancel_task", studio.arguments(task, stage_id="context_ir",
        expected_run_id=task["stages"]["context_ir"]["run_id"]))
    assert cancelled["status"] == "cancelled" and not studio.pending
    assert studio.call("h3_confirm_prompt", studio.arguments(cancelled,
        expected_output_id=cancelled["context_output_id"])).status_code == 409
    edited = studio.ok("h3_save_draft", studio.arguments(cancelled,
        original_prompt=cancelled["original_prompt"], prompt="fresh"))
    queued = studio.start(studio.confirm(edited))
    completed = studio.finish(queued)
    next_run = studio.start(completed)
    assert next_run["preview"]["run_id"] != queued["preview"]["run_id"]
    assert studio.call("h3_cancel_task", studio.arguments(next_run,
        expected_run_id=queued["preview"]["run_id"])).status_code == 409


def test_restart_reuses_receipt_and_unknown_execution_without_resubmission(studio_factory):
    studio = studio_factory()
    task = studio.confirm(studio.draft())
    arguments = studio.arguments(task, expected_output_id=task["context_output_id"], expected_run_id=None)
    queued = studio.ok("h3_start_preview", arguments)
    studio.fleet.unknown_submit = True
    studio.fleet.unknown_wait = True
    unknown = studio.finish(queued)
    assert unknown["status"] == "needs_reconciliation"
    execution_id = unknown["preview"]["execution_id"]
    assert execution_id and len(studio.fleet.submissions) == 1
    assert "private-provider-error" not in json.dumps(unknown)
    assert studio.call("h3_save_draft", studio.arguments(unknown,
        original_prompt=unknown["original_prompt"], prompt="cannot discard unknown")).status_code == 409
    recovered = studio_factory(studio.fleet)
    assert recovered.get(unknown)["revision"] == unknown["revision"]
    assert recovered.ok("h3_start_preview", arguments) == queued
    assert recovered.pending == []
    recovered.module._mark_interrupted_stages()
    assert len(recovered.pending) == 1
    recovered.fleet.unknown_wait = False
    completed = recovered.finish(unknown)
    assert completed["preview"]["output_id"]
    assert completed["preview"]["execution_id"] == execution_id
    assert recovered.fleet.waits == [execution_id, execution_id]
    assert len(recovered.fleet.submissions) == 1 and len(recovered.fleet.uploads) == 1


def test_explicit_reconciliation_uses_existing_execution_without_catalog(studio):
    studio.fleet.unknown_wait = True
    unknown = studio.finish(studio.start(studio.confirm(studio.draft())))
    studio.fleet.catalog_enabled = False
    studio.fleet.unknown_wait = False
    previous_reads = studio.fleet.catalog_reads
    reconciled = studio.finish(studio.start(unknown))
    assert reconciled["status"] == "awaiting_preview_approval"
    assert reconciled["preview"]["execution_id"] == unknown["preview"]["execution_id"]
    assert len(studio.fleet.submissions) == 1 and studio.fleet.catalog_reads == previous_reads


def test_recipe_not_ready_or_changed_fails_without_queue(studio):
    task = studio.confirm(studio.draft())
    studio.fleet.version = "v2"
    assert studio.call("h3_start_preview", studio.arguments(task,
        expected_output_id=task["context_output_id"], expected_run_id=None)).status_code == 409
    studio.fleet.catalog_enabled = False
    assert studio.call("h3_save_draft", {"operation_id": "not-ready", "original_prompt": "original",
                                         "prompt": "draft"}).status_code == 409
    assert not studio.pending


def test_pagination_filters_owner_before_limiting(studio):
    first, second = studio.draft(name="first"), studio.draft(name="second")
    for number in range(3):
        studio.ok("h3_save_draft", {"operation_id": f"other-{number}", "original_prompt": "original",
                                    "prompt": "other draft"}, "other-client")
    page = studio.ok("h3_list_tasks", {"limit": 1})
    assert [task["task_id"] for task in page["tasks"]] == [second["task_id"]]
    assert page["next_offset"] == 1
    last = studio.ok("h3_list_tasks", {"limit": 1, "offset": page["next_offset"]})
    assert [task["task_id"] for task in last["tasks"]] == [first["task_id"]]
    assert last["next_offset"] is None


def test_immutable_video_ranges_browser_auth_and_history(studio, monkeypatch, tmp_path):
    completed = studio.video()
    identifier, output_id = completed["task_id"], completed["preview"]["output_id"]
    token_url = PREFIX + f"/tasks/{identifier}/outputs/{output_id}"
    browser_url = f"/api/projects/{identifier}/connector-outputs/{output_id}"
    response = studio.client.get(token_url, headers={**studio.headers, "Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == studio.fleet.artifact[2:6]
    assert response.headers["content-range"] == f"bytes 2-5/{len(studio.fleet.artifact)}"
    assert studio.client.get(token_url, headers={**studio.headers, "Range": "bytes=99999-"}).status_code == 416
    assert studio.client.head(token_url, headers=studio.headers).status_code == 200
    studio_secret = tmp_path / "studio-key"
    studio_secret.write_text("browser-studio-secret")
    monkeypatch.setenv("H3_STUDIO_KEY_FILE", str(studio_secret))
    assert studio.client.get(browser_url).status_code == 401
    assert studio.client.get(browser_url, headers=studio.headers).status_code == 401
    browser_headers = {"Authorization": "Bearer browser-studio-secret", "Range": "bytes=0-3"}
    assert studio.client.get(browser_url, headers=browser_headers).content == studio.fleet.artifact[:4]
    project = studio.module.STORE.get(identifier)
    mutable = tmp_path / "mutable-preview.mp4"
    mutable.write_bytes(b"replacement-mutable-file")
    studio.module.STORE.update(identifier, lambda item: item["stages"]["preview"].update(artifact=str(mutable)))
    assert studio.client.get(browser_url, headers=browser_headers).content == studio.fleet.artifact[:4]
    assert str(tmp_path) not in json.dumps(studio.connector.public(studio.module.STORE.get(identifier)))
    artifact = project["router_outputs"][output_id]
    studio.module.STORE.update(identifier, lambda item: item["router_outputs"][output_id].update(path=str(mutable)))
    assert studio.client.get(browser_url, headers=browser_headers).status_code == 404
    studio.module.STORE.update(identifier, lambda item: item["router_outputs"].update({output_id: artifact}))
    studio.module.STORE.update(identifier, lambda item: item.pop("connector_owner"))
    assert studio.client.get(browser_url, headers=browser_headers).status_code == 404


def test_provider_http_errors_are_not_exposed(studio, monkeypatch):
    task = studio.confirm(studio.draft())

    def failed_start(*args, **kwargs):
        raise HTTPException(409, "provider-secret /private/path https://provider.invalid")

    monkeypatch.setattr(studio.module, "start_stage", failed_start)
    response = studio.call("h3_start_preview", studio.arguments(task,
        expected_output_id=task["context_output_id"], expected_run_id=None))
    assert response.status_code == 409
    assert response.json() == {"detail": "Studio rejected the operation"}
    assert not studio.pending


def test_capacity_uses_recipe_admission_not_idle_gpu_count(studio):
    capabilities = studio.ok("h3_capabilities")
    capacity = capabilities["capacity"]
    assert capacity["available"] and len(capacity["lanes"]) == 3
    assert capacity["active"] == 1 and capacity["queued"] == 2
    for recipe in capabilities["recipes"]:
        assert recipe["available_slots"] == 0
        assert recipe["waiting_reasons"] == ["host_ram_floor_crossed"]
    recipes = {recipe["recipe_id"]: recipe for recipe in capabilities["recipes"]}
    assert recipes["A4"]["trigger"] == "" and recipes["A4_C0"]["trigger"] == "r34l1sm\n"
    assert recipes["A4_C0"]["people_lora_strength"] == 0 and recipes["A4_C1"]["people_lora_strength"] == 1
    assert recipes["B8"]["sampling"]["steps"] == 8
    assert "/private/" not in json.dumps(capabilities) and "provider-secret" not in json.dumps(capabilities)


def test_capacity_missing_and_version_mismatch_fail_closed(studio, monkeypatch):
    capacity = studio.fleet.capacity()
    capacity["recipe_capacity"]["A4"].update(available_slots=3, recipe_version="different")
    monkeypatch.setattr(studio.fleet, "capacity", lambda: capacity)
    assert studio.ok("h3_capabilities")["recipes"][0]["available_slots"] == 0
    monkeypatch.setattr(studio.fleet, "capacity", lambda: (_ for _ in ()).throw(RuntimeError("private error")))
    result = studio.ok("h3_capabilities")
    assert result["capacity"]["available"] is False
    assert all(recipe["available_slots"] == 0 for recipe in result["recipes"])


def test_guidance_reuses_existing_skill_rules_without_models_or_executables(studio):
    result = studio.ok("h3_prompt_guidance")
    source = studio.connector.skills
    assert result["ruleset_sha256"] == source.RULESET_HASH
    assert result["model_calls"] == 0 and result["execution_verified"] is False
    assert {entry["id"] for entry in result["selected_skills"]} == set(source.BASE_SKILLS + ["tvc-video"])
    assert result["rules"]["tvc-video"] == source.rules_for(source.selected_skills(["tvc-video"]))["tvc-video"]
    assert result["rules"]["h3-prompt-writing"]["base_section_order"] == [
        "integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"]
    assert set(result["rules"]["h3-prompt-writing"]["supported_modes"]) >= {"t2va", "i2va", "fl2va", "ref2va"}
    assert {entry["execution_kind"] for entry in result["available_skills"]} == {"distilled_prompt_guidance"}
    assert all("sources" not in entry for entry in result["available_skills"])
    selected = studio.ok("h3_prompt_guidance", {"skill_ids": ["ecommerce-video", "short-drama-video"]})
    assert set(selected["rules"]) == set(source.BASE_SKILLS + ["ecommerce-video", "short-drama-video"])
    assert not studio.pending and not studio.fleet.submissions and studio.fleet.catalog_reads == 0


@pytest.mark.parametrize("skill_ids", [["image-to-video"], ["first-last-frame-to-video"],
    ["image-reference-to-video"], ["minimax-h3-video-generation-and-editing"], ["missing"],
    ["tvc-video"] * 2, ["tvc-video"] * 7, [1]])
def test_guidance_rejects_unsupported_asset_modes_and_invalid_selections(studio, skill_ids):
    assert studio.call("h3_prompt_guidance", {"skill_ids": skill_ids}).status_code == 400
    assert not studio.pending


def test_verbatim_rejects_implicit_recipe_trigger_insertion(studio):
    response = studio.call("h3_save_draft", {"operation_id": "verbatim-trigger", "recipe_id": "A4_C0",
                                            "original_prompt": "exact input", "prompt": "exact input", "verbatim": True})
    assert response.status_code == 400
    exact = "r34l1sm\nexact input"
    task = studio.draft(original_prompt=exact, prompt=exact, verbatim=True, recipe_id="A4_C0")
    studio.finish(studio.start(studio.confirm(task)))
    assert studio.fleet.submissions[0]["recipe"]["prompt"] == exact


def test_readonly_operation_recovery_preserves_receipt_and_current_state(studio, monkeypatch):
    created = studio.draft(operation_id="lost-create-response")
    confirmed = studio.confirm(created)
    args = studio.arguments(confirmed, operation_id="lost-start-response",
                            expected_output_id=confirmed["context_output_id"], expected_run_id=None)
    queued = studio.ok("h3_start_preview", args)
    completed = studio.finish(queued)
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "false")
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    found = studio.ok("h3_get_task", {"operation_id": "lost-create-response"})
    assert found["task_id"] == created["task_id"] and found["revision"] == completed["revision"]
    assert found["receipt"]["result"] == created
    assert found["receipt"]["result_revision"] == created["revision"]
    found = studio.ok("h3_get_task", {"operation_id": "lost-start-response"})
    assert found["receipt"]["result"] == queued
    assert found["preview"]["output_id"] == completed["preview"]["output_id"]
    assert studio.call("h3_get_task", {"operation_id": "lost-create-response"}, "other-client").status_code == 404
    assert studio.call("h3_get_task", {"operation_id": "unknown"}).status_code == 404
    assert studio.call("h3_get_task", {}).status_code == 400
    assert studio.call("h3_get_task", {"task_id": created["task_id"], "operation_id": "lost-create-response"}).status_code == 400
    studio.module.STORE.update(created["task_id"], lambda project: project.pop("connector_owner"))
    assert studio.call("h3_get_task", {"operation_id": "lost-create-response"}).status_code == 404
    assert len(studio.fleet.submissions) == 1


def test_dispatch_recipe_version_change_fails_before_fleet_submission(studio):
    queued = studio.start(studio.confirm(studio.draft()))
    studio.fleet.version = "v2"
    failed = studio.finish(queued)
    assert failed["status"] == "failed"
    assert not studio.fleet.submissions
    assert not studio.fleet.waits
    assert not failed["preview"]["output_id"]


@pytest.mark.parametrize("binding", [{"recipe_id": "B8", "recipe_version": "v1"},
    {"recipe_id": "A4"}, {}, "invalid", None])
def test_execution_guard_rejects_wrong_or_missing_frozen_binding(studio, binding):
    queued = studio.start(studio.confirm(studio.draft()))
    project = studio.module.STORE.get(queued["task_id"])
    project["stages"]["preview"]["execution"] = {"contract": binding}
    with pytest.raises(RuntimeError, match="recipe changed"):
        connector_api.validate_connector_execution(project)


def test_running_cancel_remains_allowed_before_execution_id_while_paused(studio, monkeypatch):
    queued = studio.start(studio.confirm(studio.draft()))
    studio.module._set_stage(queued["task_id"], "preview", status="running")
    running = studio.get(queued)
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "false")
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    response = studio.ok("h3_cancel_task", studio.arguments(running,
        expected_run_id=running["preview"]["run_id"]))
    assert response["preview"]["status"] == "cancelling"
    assert response["preview"]["run_id"] == running["preview"]["run_id"]
