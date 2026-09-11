from __future__ import annotations

import copy
import hashlib
import importlib
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_connector_api import OWNER, PREFIX, connector_api, forbidden, studio, studio_factory


PACKAGE = types.ModuleType("h3_fixture_import_source")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "studio")]
sys.modules[PACKAGE.__name__] = PACKAGE
sys.modules[PACKAGE.__name__ + ".connector_api"] = connector_api
fixture_import = importlib.import_module(PACKAGE.__name__ + ".fixture_import")
SYNTHETIC_BYTES = b"fixture-only synthetic bytes; not a playable video or generation evidence"


@pytest.fixture
def prepared(studio, tmp_path, monkeypatch):
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "false")
    monkeypatch.setenv("AI_ROUTER_H3_MCP_GENERATION_ENABLED", "false")
    task = studio.confirm(studio.draft(name="一次性导入验收草稿"))
    source = tmp_path / "explicit-authorized-source.mp4"
    source.write_bytes(SYNTHETIC_BYTES)
    arguments = {"owner": OWNER, "task_id": task["task_id"], "expected_revision": task["revision"],
                 "operation_id": "fixture-import-once", "source_path": source,
                 "source_sha256": hashlib.sha256(SYNTHETIC_BYTES).hexdigest(),
                 "authorization_reference": "acceptance-approval-test-001"}
    reads = studio.fleet.catalog_reads
    monkeypatch.setattr(studio.fleet, "recipe_catalog", forbidden)
    monkeypatch.setattr(studio.fleet, "capacity", forbidden)
    yield SimpleNamespace(studio=studio, source=source, task=task, arguments=arguments)
    assert studio.fleet.catalog_reads == reads
    assert studio.fleet.submissions == studio.fleet.uploads == studio.fleet.waits == []
    assert studio.pending == []


def run_import(prepared, **changes):
    return fixture_import.import_fixture(prepared.studio.connector, **{**prepared.arguments, **changes})


def snapshot(prepared):
    studio = prepared.studio
    with studio.contract.connect() as database:
        receipts = database.execute("SELECT * FROM router_operations ORDER BY operation_id").fetchall()
    return studio.module.STORE.get(prepared.task["task_id"]), receipts


def test_import_creates_independent_immutable_fixture_without_generation_evidence(prepared):
    source_stat = prepared.source.stat()
    result = run_import(prepared)
    stage = result["preview"]
    fixture = stage["fixture_import"]
    assert result["status"] == "awaiting_preview_approval"
    assert fixture["origin"] == "fixture_import" and fixture["generated_in_this_task"] is False
    assert fixture["label"] == "导入验收视频，非本次生成"
    assert fixture["media_validation"] == "not_performed"
    assert fixture["source_sha256"] == prepared.arguments["source_sha256"]
    assert fixture["source_bytes"] == len(SYNTHETIC_BYTES)
    assert fixture["authorization_reference"] == prepared.arguments["authorization_reference"]
    assert fixture["context_output_id"] == prepared.task["context_output_id"]
    assert fixture["run_id"] == stage["run_id"] and stage["run_id"].startswith("fixture_run_")
    assert fixture["output_id"] == stage["output_id"]
    for key in ("execution_id", "backend_id", "gpu_id", "gpu_uuid", "runtime_version", "execution_seconds",
                "elapsed_seconds", "queue_seconds", "started_at", "queued_at", "finished_at", "recipe_id", "recipe_version"):
        assert stage[key] is None
    project = prepared.studio.module.STORE.get(result["task_id"])
    artifact = project["router_outputs"][stage["output_id"]]
    target = Path(artifact["path"])
    assert target.read_bytes() == SYNTHETIC_BYTES == prepared.source.read_bytes()
    assert target.stat().st_ino != source_stat.st_ino
    assert target.stat().st_mode & 0o777 == 0o600
    assert prepared.source.stat().st_mtime_ns == source_stat.st_mtime_ns
    assert project["connector_fixture_import"]["source_path"] == str(prepared.source)
    assert project["connector_fixture_import"]["owner"] == OWNER
    assert "source_path" not in fixture and "owner" not in fixture
    assert str(prepared.source) not in str(result)
    assert artifact["fixture_import"] == fixture
    assert result["outputs"][-1]["fixture_import"] == fixture
    assert project["stages"]["local_768"]["status"] == "pending"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_fixture_uses_existing_range_and_exact_review_binding(prepared, decision):
    studio = prepared.studio
    imported = run_import(prepared)
    preview = imported["preview"]
    token_url = PREFIX + f"/tasks/{imported['task_id']}/outputs/{preview['output_id']}"
    browser_url = f"/api/projects/{imported['task_id']}/connector-outputs/{preview['output_id']}"
    response = studio.client.get(token_url, headers={**studio.headers, "Range": "bytes=2-8"})
    assert response.status_code == 206 and response.content == SYNTHETIC_BYTES[2:9]
    assert studio.client.get(browser_url, headers={"Range": "bytes=0-3"}).content == SYNTHETIC_BYTES[:4]
    arguments = studio.arguments(imported, output_id=preview["output_id"],
                                 expected_run_id=preview["run_id"], decision=decision)
    for change in ({"expected_revision": prepared.task["revision"]},
                   {"output_id": imported["context_output_id"]}, {"expected_run_id": "run_not_fixture"}):
        assert studio.call("h3_review_preview", {**arguments, **change}).status_code == 409
    reviewed = studio.ok("h3_review_preview", arguments)
    assert reviewed["status"] == ("completed" if decision == "approve" else "rejected")
    assert reviewed["preview"]["fixture_import"] == preview["fixture_import"]
    assert studio.ok("h3_review_preview", arguments) == reviewed
    assert studio.call("h3_start_preview", studio.arguments(reviewed,
        expected_output_id=reviewed["context_output_id"], expected_run_id=preview["run_id"])).status_code == 403


def test_import_does_not_read_or_adopt_source_project(prepared, tmp_path):
    studio = prepared.studio
    historical = copy.deepcopy(studio.module.STORE.get(prepared.task["task_id"]))
    historical.update(id="historical-unowned", router_managed=False)
    historical.pop("connector_owner")
    directory = studio.module.SETTINGS.data_root / "projects" / historical["id"] / "artifacts"
    directory.mkdir(parents=True)
    source = directory / "preview.mp4"
    source.write_bytes(SYNTHETIC_BYTES)
    historical["stages"]["preview"].update(status="approved", artifact=str(source), run_id="original-run")
    studio.module.STORE.save(historical)
    before = studio.module.STORE.get(historical["id"])
    result = run_import(prepared, source_path=source)
    assert studio.module.STORE.get(historical["id"]) == before
    assert source.read_bytes() == SYNTHETIC_BYTES
    assert result["preview"]["run_id"] != "original-run"
    assert studio.call("h3_get_task", {"task_id": historical["id"]}).status_code == 404


def test_replay_and_readonly_recovery_do_not_reopen_missing_source(prepared, monkeypatch):
    result = run_import(prepared)
    before = snapshot(prepared)
    monkeypatch.setattr(fixture_import, "_copy_source", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setenv("H3_CONNECTOR_WRITES_ENABLED", "false")
    assert run_import(prepared) == result
    found = prepared.studio.ok("h3_get_task", {"operation_id": prepared.arguments["operation_id"]})
    assert found["receipt"]["result"] == result
    assert found["preview"]["fixture_import"] == result["preview"]["fixture_import"]
    assert snapshot(prepared) == before


def test_import_receipt_survives_restart_with_same_store(prepared, studio_factory):
    result = run_import(prepared)
    restarted = studio_factory(prepared.studio.fleet)
    assert fixture_import.import_fixture(restarted.connector, **prepared.arguments) == result
    found = restarted.ok("h3_get_task", {"operation_id": prepared.arguments["operation_id"]})
    assert found["receipt"]["result"] == result


@pytest.mark.parametrize("change", [{"authorization_reference": "different-approval"},
    {"source_sha256": "0" * 64}, {"expected_revision": "0" * 64}, {"source_path": "/different/source.mp4"}])
def test_same_operation_with_different_arguments_conflicts(prepared, change):
    run_import(prepared)
    before = snapshot(prepared)
    with pytest.raises(HTTPException) as error:
        run_import(prepared, **change)
    assert error.value.status_code == 409
    assert snapshot(prepared) == before


def test_concurrent_identical_imports_create_one_output_and_receipt(prepared):
    barrier = Barrier(2)

    def invoke(unused):
        barrier.wait()
        return run_import(prepared)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert results[0] == results[1]
    output_directory = prepared.studio.module._project_dir(prepared.task["task_id"]) / "router-outputs"
    assert len(list(output_directory.glob("*.mp4"))) == 1
    assert len(snapshot(prepared)[1]) == 3


@pytest.mark.parametrize("change", [
    {"owner": "../other"}, {"task_id": "bad/task"}, {"operation_id": ""},
    {"expected_revision": 1}, {"source_sha256": "bad-hash"},
    {"authorization_reference": ""}, {"authorization_reference": "/private/approval.json"},
    {"authorization_reference": "approval "}, {"source_path": "relative.mp4"},
    {"source_path": "https://example.test/video.mp4"}, {"source_path": "/video.webm"},
    {"source_path": "/unused/../video.mp4"}, {"source_path": 1}])
def test_invalid_operator_arguments_reject_without_changes(prepared, change):
    before = snapshot(prepared)
    with pytest.raises(HTTPException) as error:
        run_import(prepared, **change)
    assert error.value.status_code == 400
    assert snapshot(prepared) == before


@pytest.mark.parametrize("owner", ["other-client", None])
def test_owner_checked_before_import_and_even_on_replay(prepared, owner):
    result = run_import(prepared)

    def mutate(project):
        if owner is None:
            project.pop("connector_owner")
        else:
            project["connector_owner"] = owner

    prepared.studio.module.STORE.update(result["task_id"], mutate)
    before = snapshot(prepared)
    for operation_id in (prepared.arguments["operation_id"], "new-import-operation"):
        with pytest.raises(HTTPException) as error:
            run_import(prepared, operation_id=operation_id)
        assert error.value.status_code == 404
    assert snapshot(prepared) == before


@pytest.mark.parametrize("setting,value", [
    ("H3_CONNECTOR_WRITES_ENABLED", "false"), ("H3_CONNECTOR_GENERATION_ENABLED", "true"),
    ("AI_ROUTER_H3_MCP_GENERATION_ENABLED", "1")])
def test_disabled_writes_or_enabled_generation_block_import(prepared, monkeypatch, setting, value):
    before = snapshot(prepared)
    monkeypatch.setenv(setting, value)
    with pytest.raises(HTTPException) as error:
        run_import(prepared)
    assert error.value.status_code == 403
    assert snapshot(prepared) == before


@pytest.mark.parametrize("stage_id,values", [
    ("preview", {"status": "queued"}), ("local_768", {"status": "running"}),
    ("proof", {"status": "scheduled"}), ("preview", {"fleet_pending": True}),
    ("preview", {"run_id": "old-run"}), ("preview", {"execution_id": "old-execution"}),
    ("preview", {"queued_at": 1}), ("preview", {"execution": {}}),
    ("preview", {"status": "failed"}), ("preview", {"status": "cancelled"}),
    ("preview", {"cancel_requested": True}), ("preview", {"submission_unknown": True}),
    ("preview", {"batch_schedule_id": "batch"}), ("local_768", {"output_id": "old-output"}),
    ("context_ir", {"status": "awaiting_approval"}), ("context_ir", {"approved_at": None})])
def test_only_confirmed_never_executed_inactive_draft_is_eligible(prepared, stage_id, values):
    studio = prepared.studio
    studio.module.STORE.update(prepared.task["task_id"], lambda project: project["stages"][stage_id].update(values))
    current = studio.get(prepared.task)
    before = snapshot(prepared)
    with pytest.raises(HTTPException) as error:
        run_import(prepared, expected_revision=current["revision"])
    assert error.value.status_code == 409
    assert snapshot(prepared) == before


@pytest.mark.parametrize("field,value", [
    ("connector_history", [{"old": True}]), ("stage_history", [{"old": True}]),
    ("connector_reviews", [{"old": True}]), ("prompt_approved", "modified approval"),
    ("prompt_processing", "cloud"), ("script_source", {"id": "old-script"}),
    ("assets", {"reference": {"id": "asset"}})])
def test_historical_or_changed_drafts_are_not_adopted(prepared, field, value):
    studio = prepared.studio
    if field == "assets":
        before = snapshot(prepared)
        with pytest.raises(RuntimeError, match="inputs changed outside a versioned edit"):
            studio.module.STORE.update(prepared.task["task_id"], lambda project: project.update({field: value}))
        assert snapshot(prepared) == before
        return
    studio.module.STORE.update(prepared.task["task_id"], lambda project: project.update({field: value}))
    project = studio.module.STORE.get(prepared.task["task_id"])
    before = snapshot(prepared)
    with pytest.raises(HTTPException) as error:
        run_import(prepared, expected_revision=connector_api._digest(project))
    assert error.value.status_code == 409
    assert snapshot(prepared) == before


def test_stale_revision_and_context_output_tampering_fail(prepared):
    with pytest.raises(HTTPException, match="stale task revision"):
        run_import(prepared, expected_revision="0" * 64)
    studio = prepared.studio
    studio.module.STORE.update(prepared.task["task_id"], lambda project:
        project["router_outputs"][prepared.task["context_output_id"]].update(text="other context"))
    current = studio.get(prepared.task)
    with pytest.raises(HTTPException, match="confirmed immutable context"):
        run_import(prepared, expected_revision=current["revision"])


@pytest.mark.parametrize("kind", ["absent", "empty", "directory", "fifo", "symlink", "parent_symlink", "oversize", "hash"])
def test_invalid_sources_do_not_leave_state_receipts_or_output_files(prepared, tmp_path, monkeypatch, kind):
    source = tmp_path / (kind + ".mp4")
    if kind == "empty":
        source.write_bytes(b"")
    elif kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "symlink":
        source.symlink_to(prepared.source)
    elif kind == "parent_symlink":
        link = tmp_path / "redirected"
        link.symlink_to(tmp_path, target_is_directory=True)
        source = link / prepared.source.name
    elif kind in {"oversize", "hash"}:
        source.write_bytes(b"different bytes")
        if kind == "oversize":
            monkeypatch.setattr(fixture_import, "MAX_FIXTURE_BYTES", 2)
    before = snapshot(prepared)
    with pytest.raises(HTTPException) as error:
        run_import(prepared, source_path=source)
    assert error.value.status_code in {400, 409}
    assert snapshot(prepared) == before
    output_directory = prepared.studio.module._project_dir(prepared.task["task_id"]) / "router-outputs"
    assert list(output_directory.glob("*.mp4")) == []


def test_output_directory_cannot_be_redirected(prepared, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output_directory = prepared.studio.module._project_dir(prepared.task["task_id"]) / "router-outputs"
    output_directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(HTTPException, match="must not be redirected"):
        run_import(prepared)
    assert list(outside.iterdir()) == []


def test_failure_after_state_update_rolls_back_receipt_state_and_own_file(prepared, monkeypatch):
    before = snapshot(prepared)
    original = prepared.studio.connector.public

    def fail_result(project):
        raise RuntimeError("injected receipt failure")

    monkeypatch.setattr(prepared.studio.connector, "public", fail_result)
    with pytest.raises(RuntimeError, match="receipt failure"):
        run_import(prepared)
    assert snapshot(prepared) == before
    output_directory = prepared.studio.module._project_dir(prepared.task["task_id"]) / "router-outputs"
    assert list(output_directory.glob("*.mp4")) == []
    assert prepared.source.read_bytes() == SYNTHETIC_BYTES
    monkeypatch.setattr(prepared.studio.connector, "public", original)
    assert run_import(prepared)["preview"]["fixture_import"]["origin"] == "fixture_import"


def test_gate_change_during_copy_rolls_back_only_new_output(prepared, monkeypatch):
    original = fixture_import._copy_source

    def copy_then_enable(*args):
        copied = original(*args)
        monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "true")
        return copied

    monkeypatch.setattr(fixture_import, "_copy_source", copy_then_enable)
    before = snapshot(prepared)
    with pytest.raises(HTTPException, match="generation disabled"):
        run_import(prepared)
    assert snapshot(prepared) == before
    directory = prepared.studio.module._project_dir(prepared.task["task_id"]) / "router-outputs"
    assert list(directory.glob("*.mp4")) == []


def test_second_import_refused_even_after_review_or_draft_reset(prepared, monkeypatch):
    studio = prepared.studio
    imported = run_import(prepared)
    with pytest.raises(HTTPException, match="without history"):
        run_import(prepared, operation_id="second-import", expected_revision=imported["revision"])
    monkeypatch.setattr(studio.fleet, "recipe_catalog", lambda: {
        "enabled": True, "recipes": [{"recipe_id": "A4", "version": "v1"}]})
    edited = studio.ok("h3_save_draft", studio.arguments(imported,
        original_prompt=imported["original_prompt"], prompt="编辑后的新草稿"))
    confirmed = studio.confirm(edited)
    fixture = imported["preview"]["fixture_import"]
    assert confirmed["history"][0]["stages"]["preview"]["fixture_import"] == fixture
    assert next(output for output in confirmed["outputs"] if output["id"] == fixture["output_id"])["fixture_import"] == fixture
    with pytest.raises(HTTPException, match="without history"):
        run_import(prepared, operation_id="third-import", expected_revision=confirmed["revision"])


def test_fixture_metadata_public_projection_excludes_private_audit_fields(prepared):
    imported = run_import(prepared)
    studio = prepared.studio

    def add_private(item):
        item["stages"]["preview"]["fixture_import"]["source_path"] = "/private/fixture.mp4"
        item["router_outputs"][imported["preview"]["output_id"]]["fixture_import"]["authorization_token"] = "private-token"

    studio.module.STORE.update(imported["task_id"], add_private)
    current = studio.get(imported)
    assert "/private/" not in str(current) and "private-token" not in str(current)
    assert "source_path" not in current["preview"]["fixture_import"]


def test_fixture_tasks_cannot_generate_even_after_draft_reset(prepared, monkeypatch):
    studio = prepared.studio
    imported = run_import(prepared)
    monkeypatch.setenv("H3_CONNECTOR_GENERATION_ENABLED", "true")
    response = studio.call("h3_start_preview", studio.arguments(imported,
        expected_output_id=imported["context_output_id"], expected_run_id=imported["preview"]["run_id"]))
    assert response.status_code == 409 and "cannot generate" in response.text
    monkeypatch.setattr(studio.fleet, "recipe_catalog", lambda: {
        "enabled": True, "recipes": [{"recipe_id": "A4", "version": "v1"}]})
    edited = studio.ok("h3_save_draft", studio.arguments(imported,
        original_prompt=imported["original_prompt"], prompt="新草稿也仍是验收项目"))
    confirmed = studio.confirm(edited)
    response = studio.call("h3_start_preview", studio.arguments(confirmed,
        expected_output_id=confirmed["context_output_id"], expected_run_id=None))
    assert response.status_code == 409 and "cannot generate" in response.text
    assert "fixture_import" not in confirmed["preview"]


def test_fixture_metadata_never_attaches_to_an_unrelated_run_or_output(prepared):
    imported = run_import(prepared)
    stage = prepared.studio.module.STORE.get(imported["task_id"])["stages"]["preview"]
    stage["run_id"] = "run_different"
    assert "fixture_import" not in prepared.studio.connector.stage_public("preview", stage, 1)
    stage["run_id"] = imported["preview"]["run_id"]
    stage["output_id"] = "out_different"
    assert "fixture_import" not in prepared.studio.connector.stage_public("preview", stage, 1)


def cli_arguments(prepared):
    arguments = ["--data-root", str(prepared.studio.module.SETTINGS.data_root)]
    for name, value in prepared.arguments.items():
        arguments.extend(["--" + name.replace("_", "-"), str(value)])
    return arguments


def test_cli_requires_execute_before_loading_studio(prepared, monkeypatch):
    monkeypatch.setattr(fixture_import.importlib, "import_module", forbidden)
    with pytest.raises(SystemExit) as error:
        fixture_import.main(cli_arguments(prepared))
    assert error.value.code == 2


def test_cli_uses_existing_single_contract_without_startup(prepared, monkeypatch, capsys):
    module = prepared.studio.module
    monkeypatch.setitem(sys.modules, fixture_import.__package__ + ".main", module)
    monkeypatch.setattr(module, "startup", forbidden)
    monkeypatch.setattr(type(prepared.studio.contract), "__init__", forbidden)
    monkeypatch.setattr(module.STORE, "initialize", forbidden)
    monkeypatch.setattr(prepared.studio.contract, "initialize", forbidden)
    assert fixture_import.existing_connector(module).contract is prepared.studio.contract
    assert fixture_import.main(["--execute", *cli_arguments(prepared)]) == 0
    output = capsys.readouterr().out
    assert '"origin": "fixture_import"' in output
    assert str(prepared.source) not in output
    assert module.STORE._connect.__self__ is prepared.studio.contract


def test_cli_refuses_loaded_studio_for_a_different_data_root(prepared, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, fixture_import.__package__ + ".main", prepared.studio.module)
    other = tmp_path / "other-offline-data"
    other.mkdir()
    (other / "studio.sqlite3").write_bytes(b"not the selected database")
    arguments = cli_arguments(prepared)
    arguments[1] = str(other)
    with pytest.raises(SystemExit) as error:
        fixture_import.main(["--execute", *arguments])
    assert error.value.code == 2
