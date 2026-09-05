from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_h3_media_contract import fixture


@pytest.fixture
def encoded_source(tmp_path):
    metadata = tmp_path / "source.ffmeta"
    metadata.write_text(
        ";FFMETADATA1\n"
        "title=private workflow title\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\n"
        "title=private chapter /home/private/workflow.json\n"
    )
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000", "-f", "ffmetadata", "-i", str(metadata),
        "-map", "0:v", "-map", "1:a", "-map_metadata", "2", "-map_chapters", "2",
        "-t", "0.5", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-metadata", 'workflow={"api_key":"unit-test-secret","model":"/home/private/model.safetensors"}',
        "-metadata", "comment=private prompt", "-metadata:s:v", "handler_name=/home/private/video-handler",
        "-metadata:s:a", "handler_name=private audio handler", "-timecode", "12:34:56:00",
        "-movflags", "use_metadata_tags", str(source),
    ], capture_output=True, check=True, timeout=30)
    return source


def staged_project(m, source, *, managed=True, strategy="fast"):
    project = m.create_project()
    previous = "cloud_768" if strategy == "cloud" else "local_768"
    directory = m._project_dir(project["id"]) / "router-outputs"
    directory.mkdir()
    approved = directory / "out_approved.mp4"
    shutil.copyfile(source, approved)
    project.update(router_managed=managed, strategy=strategy, prompt_approved="Reviewed scene")
    project["stages"][previous] = {
        "status": "approved", "progress": 100, "run_id": "run_source",
        "output_id": "out_approved", "artifact": str(approved),
    }
    project["stages"]["regenerate_2k"] = {
        "status": "running", "progress": 3, "run_id": "run_regeneration", "cancel_requested": False,
    }
    project["router_outputs"] = {
        "out_approved": {"id": "out_approved", "stage": previous, "run_id": "run_source",
                         "path": str(approved), "content_type": "video/mp4"},
    }
    if not managed:
        project["stages"][previous].pop("run_id")
        project["stages"][previous].pop("output_id")
        project["stages"]["regenerate_2k"].pop("run_id")
        project.pop("router_outputs")
    m.STORE.save(project)
    return project, approved, previous


def test_h3_upload_remux_removes_private_tags_chapters_data_and_keeps_packets(tmp_path, monkeypatch, encoded_source):
    _, _, extension = fixture(tmp_path, monkeypatch)
    raw = encoded_source.read_bytes()
    before = extension._media_probe(encoded_source)
    assert before["chapters"]
    assert any(stream["codec_type"] == "data" for stream in before["streams"])
    target = tmp_path / "private-upload" / "clean.mp4"
    proof = extension.sanitize_upload(encoded_source, target)
    clean = extension._media_probe(target)
    extension.assert_clean_metadata(clean)
    assert encoded_source.read_bytes() == raw
    assert b"unit-test-secret" not in target.read_bytes()
    assert "/home/private" not in json.dumps(clean)
    assert extension.av_packet_proof(before) == extension.av_packet_proof(clean)
    assert extension.decoded_frame_proof(encoded_source) == extension.decoded_frame_proof(target)
    assert proof["source_sha256"] != proof["upload_sha256"]
    assert proof["transcoded"] is False
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (target.parent.stat().st_mode & 0o777) == 0o700
    assert "unit-test-secret" not in json.dumps(proof)


@pytest.mark.parametrize("strategy", ["fast", "cloud"])
def test_h3_upload_hook_uses_clean_copy_of_current_approved_version(tmp_path, monkeypatch, encoded_source, strategy):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    project, source, previous = staged_project(m, encoded_source, strategy=strategy)
    original_hash = extension._file_sha256(source)
    calls = []
    callback, cancelled = lambda value: None, lambda: False

    def provider(value, upload, destination, *, progress, cancelled):
        assert Path(upload) != source
        extension.assert_clean_metadata(extension._media_probe(upload))
        assert value["stages"][previous]["artifact"] == str(source)
        assert value["stages"][previous]["output_id"] == "out_approved"
        assert progress is callback
        calls.append(str(upload))
        return {"task_id": "unit-test-provider-receipt"}
    contract.original_regenerate_2k = provider
    result = m.MINIMAX.regenerate_2k(project, source, tmp_path / "result.mp4",
                                    progress=callback, cancelled=cancelled)
    assert result["task_id"] == "unit-test-provider-receipt"
    assert len(calls) == 1
    current = m.STORE.get(project["id"])
    proof = current["stages"]["regenerate_2k"].pop("router_upload_source")
    assert current["stages"] == project["stages"]
    assert proof["source_stage"] == previous
    assert proof["source_output_id"] == "out_approved"
    assert proof["source_run_id"] == "run_source"
    assert proof["generation_run_id"] == "run_regeneration"
    assert proof["source_sha256"] == original_hash == extension._file_sha256(source)


@pytest.mark.parametrize("fault", ["not_approved", "stale_output", "different_source", "cancelled", "stale_run"])
def test_h3_upload_hook_refuses_unapproved_or_stale_dispatch(tmp_path, monkeypatch, encoded_source, fault):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    project, source, previous = staged_project(m, encoded_source)
    current = copy.deepcopy(project)
    if fault == "not_approved":
        current["stages"][previous]["status"] = "pending"
    elif fault == "stale_output":
        current["stages"][previous]["output_id"] = "out_newer"
    elif fault == "different_source":
        source = encoded_source
    elif fault == "cancelled":
        current["stages"]["regenerate_2k"]["cancel_requested"] = True
    else:
        current["stages"]["regenerate_2k"]["run_id"] = "run_other"
    m.STORE.save(current)
    calls = []
    contract.original_regenerate_2k = lambda *a, **k: calls.append(True)
    with pytest.raises(RuntimeError):
        m.MINIMAX.regenerate_2k(project, source, tmp_path / "result.mp4",
                               progress=lambda value: None, cancelled=lambda: False)
    assert not calls


def test_h3_upload_rechecks_approval_after_remux(tmp_path, monkeypatch, encoded_source):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    project, source, previous = staged_project(m, encoded_source)
    real_sanitize = extension.sanitize_upload
    calls = []

    def change_version(*args):
        proof = real_sanitize(*args)
        value = m.STORE.get(project["id"])
        value["stages"][previous]["output_id"] = "out_changed_during_copy"
        m.STORE.save(value)
        return proof
    monkeypatch.setattr(extension, "sanitize_upload", change_version)
    contract.original_regenerate_2k = lambda *a, **k: calls.append(True)
    with pytest.raises(RuntimeError, match="version changed"):
        m.MINIMAX.regenerate_2k(project, source, tmp_path / "result.mp4",
                               progress=lambda value: None, cancelled=lambda: False)
    assert not calls


def test_h3_upload_validation_failure_has_no_raw_fallback(tmp_path, monkeypatch, encoded_source):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    project, source, _ = staged_project(m, encoded_source)
    original_hash = extension._file_sha256(source)
    calls = []
    contract.original_regenerate_2k = lambda *a, **k: calls.append(True)
    monkeypatch.setattr(extension, "decoded_frame_proof", lambda path: [{"path": str(path)}])
    with pytest.raises(RuntimeError, match="decoded frame"):
        m.MINIMAX.regenerate_2k(project, source, tmp_path / "result.mp4",
                               progress=lambda value: None, cancelled=lambda: False)
    assert not calls
    assert extension._file_sha256(source) == original_hash


def test_h3_upload_sanitizer_never_overwrites_original(tmp_path, monkeypatch, encoded_source):
    _, _, extension = fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="approved source"):
        extension.sanitize_upload(encoded_source, encoded_source)


def test_h3_legacy_upload_keeps_existing_approval_semantics(tmp_path, monkeypatch, encoded_source):
    m, contract, extension = fixture(tmp_path, monkeypatch)
    project, source, previous = staged_project(m, encoded_source, managed=False)
    calls = []
    contract.original_regenerate_2k = lambda value, upload, *a, **k: calls.append(str(upload)) or {"task_id": "legacy"}
    m.MINIMAX.regenerate_2k(project, source, tmp_path / "result.mp4",
                           progress=lambda value: None, cancelled=lambda: False)
    assert len(calls) == 1
    current = m.STORE.get(project["id"])
    assert current["stages"][previous]["status"] == "approved"
    assert not current.get("router_managed")
    assert "output_id" not in current["stages"][previous]
    extension.assert_clean_metadata(extension._media_probe(calls[0]))
