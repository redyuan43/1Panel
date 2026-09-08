from __future__ import annotations

import json
from pathlib import Path

from app.workflow_builder import actual_duration, build_workflow, frames_for_duration


def write_template(path: Path, *, preview: bool) -> None:
    conditioning = "MiniMaxH3AudioConditioningT8" if preview else "MiniMaxH3ImageToVideo"
    sampler = "MiniMaxH3DualClockSamplerT8" if preview else "BasicScheduler"
    path.write_text(json.dumps({
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "h3.safetensors"}},
        "2": {"class_type": conditioning, "inputs": {}},
        "3": {"class_type": "RandomNoise", "inputs": {}},
        "4": {"class_type": sampler, "inputs": {}},
        "5": {"class_type": "SaveVideo", "inputs": {}},
    }), encoding="utf-8")


def templates(tmp_path: Path, monkeypatch) -> None:
    preview = tmp_path / "preview.json"
    quality = tmp_path / "quality.json"
    write_template(preview, preview=True)
    write_template(quality, preview=False)
    monkeypatch.setenv("H3_TURBO_TEMPLATE", str(preview))
    monkeypatch.setenv("H3_QUALITY_TEMPLATE", str(quality))


def test_preview_fl2v_uses_six_steps_and_shared_anchors(tmp_path, monkeypatch) -> None:
    templates(tmp_path, monkeypatch)
    workflow, contract = build_workflow(
        execution_id="preview-1",
        profile="preview",
        mode="fl2v",
        prompt="Connect the two approved anchors.",
        duration=5,
        seed=7,
        aspect_ratio="9:16",
        assets={"first_frame": "first.png", "last_frame": "last.png"},
    )
    conditioning = workflow["2"]["inputs"]
    assert conditioning["task_type"] == "FL2VA"
    assert conditioning["width"] == 480 and conditioning["height"] == 864
    assert conditioning["length"] == 124
    assert workflow[conditioning["first_frame"][0]]["inputs"]["image"] == "first.png"
    assert workflow[conditioning["last_frame"][0]]["inputs"]["image"] == "last.png"
    assert workflow["4"]["inputs"]["steps"] == 6
    assert contract["actual_duration"] == 124 / 24


def test_quality_fl2v_uses_fourteen_steps_and_768_portrait(tmp_path, monkeypatch) -> None:
    templates(tmp_path, monkeypatch)
    workflow, contract = build_workflow(
        execution_id="quality-1",
        profile="quality",
        mode="fl2v",
        prompt="Preserve identity and geometry.",
        duration=5,
        seed=-1,
        aspect_ratio="9:16",
        assets={"first_frame": "first.png", "last_frame": "last.png"},
    )
    assert workflow["2"]["inputs"]["width"] == 768
    assert workflow["2"]["inputs"]["height"] == 1344
    assert workflow["4"]["inputs"]["steps"] == 14
    assert contract["seed"] >= 0
    assert contract["frame_count"] == 124


def test_duration_uses_h3_frame_grid() -> None:
    assert frames_for_duration(5) == 124
    assert actual_duration(5) == 124 / 24
    assert frames_for_duration(15) == 362
