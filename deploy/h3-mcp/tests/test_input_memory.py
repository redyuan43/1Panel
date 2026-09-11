import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_reference_video_probe import synthesize


BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("input_memory_tests", BASE / "fleet/input_memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)
GIB = 1024**3


def contract(extra=5 * GIB):
    asset = {"asset_id": "asset_" + "a" * 32, "sha256": "b" * 64, "size": 100, "comfy_name": "asset_" + "a" * 32 + ".mp4"}
    return {"assets": {"reference_video": asset}, "verified_input_memory": {"reference_video": {
        **{key: asset[key] for key in ("asset_id", "sha256", "size")}, "inspector_version": "c" * 64,
        "metadata": {"is_cfr_24": True, "width": 480, "height": 864, "frame_count": 360}, "decode_budget_bytes": extra}}}


def test_input_decode_increment_preserves_model_and_history_floors_without_growth():
    previous = {"candidate_budget_bytes": 24 * GIB, "budget_bytes": 24 * GIB}
    once = memory.apply_input_budget(previous, contract(), 24 * GIB, required=True)
    assert once["candidate_budget_bytes"] == once["budget_bytes"] == 29 * GIB
    assert once["model_base_budget_bytes"] == 24 * GIB
    assert memory.apply_input_budget(once, contract(), 24 * GIB, required=True) == once
    history = memory.apply_input_budget({"candidate_budget_bytes": 40 * GIB}, contract(), 24 * GIB, required=True)
    assert history["candidate_budget_bytes"] == 40 * GIB
    assert memory.demand_budget(contract(), 24 * GIB) == 29 * GIB


@pytest.mark.parametrize("change", ["missing", "wrong_hash", "wrong_size", "negative", "bool", "non24", "no_asset"])
def test_missing_or_mismatched_input_evidence_fails_closed(change):
    value = contract()
    evidence = value["verified_input_memory"]["reference_video"]
    if change == "missing": value["verified_input_memory"] = {}
    if change == "wrong_hash": evidence["sha256"] = "f" * 64
    if change == "wrong_size": evidence["size"] += 1
    if change == "negative": evidence["decode_budget_bytes"] = -1
    if change == "bool": evidence["decode_budget_bytes"] = True
    if change == "non24": evidence["metadata"]["is_cfr_24"] = False
    if change == "no_asset": value["assets"] = {}
    with pytest.raises(ValueError):
        memory.apply_input_budget({"candidate_budget_bytes": 24 * GIB}, value, 24 * GIB, required=True)


def test_text_and_image_tasks_do_not_inherit_video_budget():
    assert memory.input_increment({"assets": {"first_frame": {}}}) == 0
    assert memory.demand_budget({}, 18 * GIB) == 18 * GIB
    with pytest.raises(ValueError):
        memory.apply_input_budget({"candidate_budget_bytes": 24 * GIB}, {}, 24 * GIB, required=True)


def test_server_probe_is_cached_only_for_exact_immutable_content_and_inspector(tmp_path, monkeypatch):
    asset = contract()["assets"]["reference_video"]
    path = tmp_path / asset["comfy_name"]
    path.write_bytes(b"fake-video-probed-by-test")
    asset.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
    calls = []
    def probe(source):
        calls.append(source)
        return {"is_cfr_24": True}, 5 * GIB
    monkeypatch.setattr(memory, "inspect_reference", probe)
    monkeypatch.setattr(memory, "inspector_version", lambda: "c" * 64)
    first = memory.verified_input_memory({"reference_video": asset}, tmp_path)
    assert first == memory.verified_input_memory({"reference_video": asset}, tmp_path)
    assert len(calls) == 1
    first["reference_video"]["decode_budget_bytes"] = 2 * GIB
    assert memory.verified_input_memory({"reference_video": asset}, tmp_path)["reference_video"]["decode_budget_bytes"] == 5 * GIB
    monkeypatch.setattr(memory, "inspector_version", lambda: "d" * 64)
    assert memory.verified_input_memory({"reference_video": asset}, tmp_path) != first
    assert len(calls) == 2
    path.write_bytes(b"changed-input-identity")
    with pytest.raises(ValueError, match="content_changed"):
        memory.verified_input_memory({"reference_video": asset}, tmp_path)
    assert len(calls) == 2


def test_editable_disk_metadata_is_not_an_admission_authority(tmp_path, monkeypatch):
    asset = contract()["assets"]["reference_video"]
    path = tmp_path / asset["comfy_name"]
    path.write_bytes(b"actual-video")
    asset.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
    cache = tmp_path / ".verified-metadata"
    cache.mkdir()
    (cache / (asset["sha256"] + ".json")).write_text(json.dumps({
        "identity": memory.file_identity(path), "inspector_version": "c" * 64,
        "evidence": {**asset, "metadata": {"is_cfr_24": True}, "decode_budget_bytes": 2 * GIB}}))
    monkeypatch.setattr(memory, "inspector_version", lambda: "c" * 64)
    monkeypatch.setattr(memory, "inspect_reference", lambda _: ({"is_cfr_24": False}, 80 * GIB))
    with pytest.raises(ValueError, match="24fps"):
        memory.verified_input_memory({"reference_video": asset}, tmp_path)


def test_non24_actual_input_is_not_silently_treated_as_24fps(tmp_path, monkeypatch):
    asset = contract()["assets"]["reference_video"]
    path = tmp_path / asset["comfy_name"]
    path.write_bytes(b"fake-video")
    asset.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=path.stat().st_size)
    monkeypatch.setattr(memory, "inspect_reference", lambda _: ({"is_cfr_24": False}, 5 * GIB))
    monkeypatch.setattr(memory, "inspector_version", lambda: "c" * 64)
    with pytest.raises(ValueError, match="24fps"):
        memory.verified_input_memory({"reference_video": asset}, tmp_path)


def test_released_dispatcher_threads_input_evidence_through_every_admission_gate():
    source = (BASE / "scripts/prepare_multimodal_release.py").read_text()
    assert 'job=row)' in source
    assert 'health[backend["id"]], row)["reasons"]' in source
    assert 'job=current)' in source
    assert 'verified_memory, metadata["contract"]["assets"], asset_root(self.fleet)' in source
    assert 'demand_budget(metadata["contract"]' in source
    assert '"model_base_budget_bytes"' in source
    lifecycle = ast.parse((BASE / "fleet/backend_lifecycle.py").read_text())
    calls = [node for node in ast.walk(lifecycle) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "candidate"]
    assert len(calls) == 1 and any(keyword.arg == "job" for keyword in calls[0].keywords)


@pytest.mark.parametrize("rate,allowed", [("24", True), ("30", False)])
def test_actual_upload_probe_and_studio_execution_agree_without_changing_original(synthesize, rate, allowed):
    from fastapi import HTTPException
    from test_connector_api import connector_api
    path = synthesize(rate=rate)
    original = path.read_bytes()
    metadata = connector_api.connector_assets.probe(path, "reference_video")
    assert metadata["is_cfr_24"] == allowed
    assert metadata["frame_count"] == int(rate) // 2
    project = {"mode": "reference", "audio_policy": "reference", "assets": {"reference_video": {"metadata": metadata}}}
    if allowed:
        connector_api.input_contract.validate_execution_media(project)
    else:
        with pytest.raises(HTTPException, match="24fps"):
            connector_api.input_contract.validate_execution_media(project)
    assert path.read_bytes() == original
