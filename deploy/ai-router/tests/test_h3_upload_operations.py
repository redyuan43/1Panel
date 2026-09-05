from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def load(name, monkeypatch):
    directory = Path(__file__).parents[1] / "integrations/h3"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(name, directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("area", ["projects", "batches", "queue_running", "queue_pending"])
def test_h3_upgrade_defers_for_any_active_work(monkeypatch, area):
    helper = load("deploy_upload", monkeypatch)
    state = {"activity": {"projects": [], "batches": []},
             "queue": {"queue_running": [], "queue_pending": []}}
    helper.require_idle(state)
    if area in state["activity"]:
        state["activity"][area] = [{"id": "live-work"}]
    else:
        state["queue"][area] = ["live-prompt"]
    with pytest.raises(RuntimeError, match="no reload"):
        helper.require_idle(state)


def test_h3_upgrade_is_idempotent_when_target_is_already_live(
    monkeypatch, tmp_path
):
    helper = load("deploy_upload", monkeypatch)
    extension = "reviewed extension"
    extension_hash = helper.hashlib.sha256(extension.encode()).hexdigest()
    state = {
        "activity": {"projects": [], "batches": []},
        "queue": {"queue_running": [], "queue_pending": []},
        "projects_sha256": "projects",
        "outputs": {"project": {"output": {"sha256": "artifact", "bytes": 12}}},
        "service": {"MainPID": "123"},
        "neighbors": {"neighbor.service": {"MainPID": "456"}},
        "head": "reviewed-head",
        "dirty": "M app/main.py\n?? app/router_contract.py",
        "main_sha256": "reviewed-main",
        "extension_sha256": extension_hash,
    }
    monkeypatch.setattr(helper.d, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "snapshot", lambda database: state.copy())
    monkeypatch.setattr(
        helper.d, "environment", lambda: {"H3_ROUTER_KEY": "private"}
    )
    monkeypatch.setattr(
        helper, "health",
        lambda key: {
            "cloud_upload_metadata_clean": True,
            "stage_heartbeat": True,
            "local_768_gpu_exclusive": True,
        },
    )
    monkeypatch.setattr(
        helper.d, "request", lambda *args, **kwargs: (401, {})
    )
    monkeypatch.setattr(
        helper.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("already-current deploy restarted H3"),
    )

    report = helper.deploy({
        "expected_head": "reviewed-head",
        "expected_extension_sha256": "previous-extension",
        "expected_main_sha256": "reviewed-main",
        "extension": extension,
    })

    assert report["status"] == "deployed_upload_hook_already_current"
    assert report["invariants"]["h3_pid_unchanged"] is True


@pytest.mark.parametrize("field", ["api_key", "MINIMAX_API_KEY", "client-secret", "Authorization", "secret"])
def test_h3_metadata_scans_nested_json_without_returning_credentials(monkeypatch, field):
    helper = load("verify_upload", monkeypatch)
    assert helper.credential_fields({"comment": '{"nodes":[{"' + field + '":"private-fixture"}]}'}) is True
    assert helper.credential_fields({"comment": '{"nodes":[{"' + field + '":""}]}'}) is False
    assert helper.credential_fields({"workflow": {"model": "private model name"}}) is False


def test_h3_key_literal_scans_complete_file_and_chunk_boundaries(monkeypatch, tmp_path):
    helper = load("verify_upload", monkeypatch)
    path = tmp_path / "source.mp4"
    fixture = "unit-only-credential"
    path.write_bytes(b"0" * (1024 * 1024 - 5) + fixture.encode() + b"trailer")
    assert helper.contains_literal(path, fixture) is True
    assert helper.contains_literal(path, "different-test-value") is False
    assert helper.contains_literal(path, "0") is True
    with pytest.raises(RuntimeError, match="real credential"):
        helper.contains_literal(path, "")
