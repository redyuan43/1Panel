from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path

import pytest

from app.fleet import FleetClient, SubmissionUnknown
from app.workflows import build_workflow, pipeline_for


ROOT = Path(__file__).resolve().parents[1]


def test_submit_is_idempotent_and_unknown_response_is_not_retried(tmp_path, monkeypatch):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    calls = []
    def request(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        if method == "GET":
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        raise TimeoutError("lost reply")
    monkeypatch.setattr(fleet, "_request", request)
    with pytest.raises(SubmissionUnknown):
        fleet.submit_stage({"1": {}}, "studio-operation", "preview", "preview")
    assert [method for method, *_ in calls] == ["GET", "POST"]
    monkeypatch.setattr(fleet, "execution", lambda identifier: {"prompt_id": "existing"})
    assert fleet.submit_stage({}, "studio-operation", "preview", "preview") == "existing"
    assert len(calls) == 2


def test_cancellation_targets_only_its_prompt(tmp_path, monkeypatch):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    jobs = iter([{"prompt_id": "owned", "status": "running"}, {"prompt_id": "owned", "status": "cancelled"}])
    monkeypatch.setattr(fleet, "execution", lambda identifier: next(jobs))
    calls = []
    monkeypatch.setattr(fleet, "_request", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr("app.fleet.time.sleep", lambda duration: None)
    with pytest.raises(RuntimeError, match="取消"):
        fleet.wait_execution("operation", tmp_path / "output.mp4", progress=lambda value: None, cancelled=lambda: True)
    assert calls == [("POST", "/api/jobs/owned/cancel", {})]


def test_all_source_modes_build_bounded_ivan_templates():
    import importlib.util
    admission_path = Path(os.environ.get("H3_FLEET_TEST_ROOT", str(ROOT.parent / "h3-fleet"))) / "app/admission.py"
    spec = importlib.util.spec_from_file_location("studio_capacity", admission_path)
    admission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admission)
    policy = admission.CapacityPolicy(admission_path.parents[1] / "config/capacity.json")
    combinations = [
        ("t2v", "native", {}), ("i2v", "native", {"first_frame": "image.png"}),
        ("l2v", "native", {"last_frame": "image.png"}),
        ("fl2v", "native", {"first_frame": "first.png", "last_frame": "last.png"}),
        ("reference", "native", {"reference_image": "image.png"}),
        ("reference", "reference", {"reference_image": "image.png", "reference_audio": "sound.wav"}),
        ("reference", "native", {"reference_video": "video.mp4"}),
        ("reference", "reference", {"reference_video": "video.mp4"}),
        ("hybrid", "native", {"first_frame": "first.png", "last_frame": "last.png", "reference_image": "image.png"}),
        ("i2v", "lock_source", {"first_frame": "image.png", "reference_audio": "sound.wav"}),
    ]
    for mode, audio, assets in combinations:
        for duration in (5, 15):
            project = {"id": "fixture", "mode": mode, "audio_policy": audio, "strategy": "safe",
                       "duration": duration, "seed": 7, "prompt_approved": "fixture",
                       "use_embedded_video_audio": audio == "reference", "stages": {name: {} for name in (
                           "context_ir", "preview", "proof", "local_768", "cloud_768", "regenerate_2k")},
                       "assets": {role: {"comfy_name": name} for role, name in assets.items()}}
            for stage in pipeline_for(project):
                if stage["id"] not in {"preview", "proof", "local_768"}:
                    continue
                graph, _ = build_workflow(project, stage["id"], ROOT / "workflows")
                profile = "preview" if any(node["class_type"] == "MiniMaxH3DualClockSamplerT8" for node in graph.values()) else "quality"
                demand = policy.demand({"prompt": graph, "extra_data": {"h3": {"studio": True}}}, profile)
                assert demand["known_shape"] and demand["frame_count"] <= 362
                if mode in {"reference", "hybrid"} or audio == "lock_source" or duration == 15:
                    assert demand["class"] == "long"


def test_upload_uses_immutable_names_and_no_global_free(tmp_path, monkeypatch):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    asset = tmp_path / "image.png"
    asset.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(fleet, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    fleet.upload_assets({"first_frame": {"path": str(asset), "comfy_name": "h3studio_unique.png"}})
    fleet.free()
    assert len(calls) == 1 and calls[0][0][1].endswith("?overwrite=false")
    assert b"fixture" in calls[0][1]["data"]
