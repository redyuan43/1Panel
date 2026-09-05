from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def script():
    path = Path(__file__).parents[1] / "scripts/verify-h3-media-live.py"
    spec = importlib.util.spec_from_file_location("h3_live_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("phase", ["context", "preview", "continue", "coverage"])
def test_h3_live_generation_requires_explicit_release(script, phase):
    with pytest.raises(SystemExit):
        script.parse_args(["--phase", phase])


def test_h3_live_evidence_redacts_nested_and_relative_tickets(script, tmp_path):
    secret = "private-preview-ticket"
    value = {"stages": [{"output": {"content_url": "/v1/media/outputs/out/content?access=" + secret}}],
             "error": "GET /v1/media/outputs/out/content?access=" + secret,
             "url": "https://host/video?access=" + secret,
             "credential": "Bearer " + secret}
    path = tmp_path / "report.json"
    script.save(path, value)
    text = path.read_text()
    assert secret not in text
    assert "content_url" not in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_h3_live_env_never_accepts_public_credentials(script, tmp_path):
    path = tmp_path / "acceptance.env"
    path.write_text('MEDIA_CLIENT_KEY="local-test-value"\n')
    path.chmod(0o644)
    with pytest.raises(script.AcceptanceError, match="0600"):
        script.env_value(path, "MEDIA_CLIENT_KEY")
    path.chmod(0o600)
    assert script.env_value(path, "MEDIA_CLIENT_KEY") == "local-test-value"


def test_h3_live_stage_selection_uses_advertised_ids(script):
    job = {"stages": [{"id": "context_ir"}, {"id": "preview"}, {"id": "cloud_768"}]}
    assert script.stage_by_id(job, "cloud_768")["id"] == "cloud_768"
    with pytest.raises(script.AcceptanceError):
        script.stage_by_id(job, "local_768")


def test_h3_live_edge_probe_is_read_only(script):
    assert "mode=ro" in script.EDGE_PROBE
    assert "method=" not in script.EDGE_PROBE
    assert "restart" not in script.EDGE_PROBE
    assert "cancel" not in script.EDGE_PROBE.replace("cancel_requested", "").replace("cancelling", "")


def test_h3_live_first_fatal_is_not_replaced(script, tmp_path):
    runner = object.__new__(script.Runner)
    runner.root = tmp_path
    runner.path = tmp_path / "manifest.json"
    runner.state = {"events": 0, "jobs": {}}
    runner.args = type("Args", (), {"phase": "observe"})()
    runner.first_fatal(ValueError("first evidence"))
    runner.first_fatal(ValueError("later evidence"))
    assert json.loads((tmp_path / "first-fatal.json").read_text())["error"] == "first evidence"


def test_h3_live_delivery_phase_does_not_require_generation_release(script):
    assert script.parse_args(["--phase", "delivery"]).execute is False


@pytest.mark.parametrize("name", ["source_path", "source_sha256", "source_bytes", "source_unexpected"])
def test_h3_live_rejects_private_fields_in_public_json(script, name):
    with pytest.raises(script.AcceptanceError, match="private source"):
        script.assert_public_boundary({"stages": [{"output": {name: "private"}}]})


def test_h3_live_distinguishes_upstream_raw_and_clean_delivery(script, tmp_path):
    source, target = tmp_path / "raw", tmp_path / "clean"
    source.write_bytes(b"original container")
    target.write_bytes(b"clean container")
    output = {"id": "out_delivery", "output_id": "out_upstream", "content_type": "video/mp4",
              "metadata_stripped": True, "sha256": script.sha256(target), "bytes": target.stat().st_size}
    archive = {**output, "path": str(target), "source_path": str(source),
               "source_sha256": script.sha256(source), "source_bytes": source.stat().st_size}
    original = {"sha256": script.sha256(source), "bytes": source.stat().st_size}
    local, actual_source = script.archive_identity(output, [archive], original)
    assert actual_source == source
    assert local["id"] != local["output_id"]
    assert local["sha256"] != original["sha256"]
    with pytest.raises(script.AcceptanceError, match="raw artifact"):
        script.archive_identity(output, [archive], {**original, "sha256": "wrong"})
    with pytest.raises(script.AcceptanceError, match="distinct"):
        script.archive_identity({**output, "metadata_stripped": False}, [archive], original)
    with pytest.raises(script.AcceptanceError, match="delivery ID"):
        script.archive_identity(output, [{**archive, "id": "out_upstream"}], original)


def test_h3_live_text_retains_upstream_identity(script, tmp_path):
    path = tmp_path / "context.txt"
    path.write_text("real context")
    output = {"id": "out_context", "output_id": "out_context", "content_type": "text/plain",
              "sha256": script.sha256(path), "bytes": path.stat().st_size}
    archive = {**output, "path": str(path)}
    assert script.archive_identity(output, [archive], output) == (archive, path)


def test_h3_live_new_raw_ids_are_unavailable_and_legacy_ids_are_gone(script):
    assert script.rejected_raw_status([], "out_never_published") == 404
    assert script.rejected_raw_status([{"id": "out_legacy"}], "out_legacy") == 410


def test_h3_live_coverage_is_cloud_then_safe_and_never_extra_2k(script):
    runner = object.__new__(script.Runner)
    runner.state = {"jobs": {"fast": {"status": "completed"}}, "coverage": {}}
    runner.options = lambda: {"videos": {"strategy": ["fast", "safe", "cloud"]}}
    runner.persist = lambda: None
    calls = []
    runner.drive = lambda strategy, **options: calls.append((strategy, options))
    runner.coverage()
    assert calls == [("cloud", {"stop_after": "cloud_768"}), ("safe", {"stop_after": "proof"})]


def test_h3_live_missing_authorized_stop_cannot_fall_through_to_local_or_2k(script):
    runner = object.__new__(script.Runner)
    job = {"stages": [{"id": "context_ir", "status": "approved"}, {"id": "local_768", "status": "pending"},
                      {"id": "regenerate_2k", "status": "pending"}]}
    runner.create = lambda strategy: job
    runner.wait = lambda strategy, stage: job
    with pytest.raises(script.AcceptanceError, match="stop stage"):
        runner.drive("cloud", stop_after="cloud_768")
