from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.media import validate_2k_spec
from app.workflows import (
    FL2VA_MODEL,
    QUALITY_TEMPLATE,
    TURBO_TEMPLATE,
    build_workflow,
    frames_for_duration,
    pipeline_for,
    project_runtime_summary,
    runtime_profile,
    validate_project_config,
)


def project(**overrides) -> dict:
    value = {
        "id": "abc123",
        "mode": "t2v",
        "strategy": "fast",
        "duration": 15,
        "seed": 42,
        "audio_policy": "native",
        "watermark": False,
        "use_embedded_video_audio": False,
        "prompt_original": "A runner crosses a rooftop.",
        "prompt_approved": "integrated_multimodal_description: a runner.",
        "assets": {},
        "stages": {
            name: {"status": "pending"}
            for name in {
                "context_ir",
                "preview",
                "proof",
                "local_768",
                "cloud_768",
                "regenerate_2k",
            }
        },
    }
    value.update(overrides)
    return value


def test_frame_grid_matches_known_outputs() -> None:
    assert frames_for_duration(5) == 124
    assert frames_for_duration(15) == 362
    with pytest.raises(ValueError):
        frames_for_duration(3)


def test_safe_pipeline_adds_proof_only_for_turbo_modes() -> None:
    fast = [stage["id"] for stage in pipeline_for(project())]
    safe = [
        stage["id"]
        for stage in pipeline_for(project(strategy="safe"))
    ]
    reference = [
        stage["id"]
        for stage in pipeline_for(
            project(
                mode="reference",
                strategy="safe",
                assets={"reference_image": {"path": "x"}},
            )
        )
    ]
    assert fast == ["context_ir", "preview", "local_768", "regenerate_2k"]
    assert safe == [
        "context_ir",
        "preview",
        "proof",
        "local_768",
        "regenerate_2k",
    ]
    assert reference == ["context_ir", "preview", "local_768", "regenerate_2k"]


def test_reference_video_rejects_separate_audio() -> None:
    value = project(
        mode="reference",
        strategy="safe",
        assets={
            "reference_video": {"path": "video.mp4"},
            "reference_audio": {"path": "audio.wav"},
        },
    )
    with pytest.raises(ValueError, match="内置音频"):
        validate_project_config(value)


def test_turbo_workflow_is_patched(tmp_path: Path) -> None:
    template = {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "old.safetensors"},
        },
        "6": {
            "class_type": "MiniMaxH3AudioConditioningT8",
            "inputs": {"prompt": "", "width": 1, "height": 1, "length": 1},
        },
        "7": {
            "class_type": "MiniMaxH3DualClockSamplerT8",
            "inputs": {"steps": 4},
        },
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
        "13": {
            "class_type": "SaveVideo",
            "inputs": {"filename_prefix": "video/old"},
        },
    }
    (tmp_path / TURBO_TEMPLATE).write_text(json.dumps(template), encoding="utf-8")
    workflow, template_name = build_workflow(project(), "preview", tmp_path)
    assert template_name == TURBO_TEMPLATE
    assert workflow["4"]["inputs"]["unet_name"] == FL2VA_MODEL
    assert workflow["6"]["inputs"]["length"] == 362
    assert workflow["6"]["inputs"]["width"] == 864
    assert workflow["6"]["inputs"]["prompt"].startswith("integrated_")
    assert workflow["8"]["inputs"]["noise_seed"] == 42
    assert workflow["13"]["inputs"]["filename_prefix"].endswith("/abc123/preview")

    portrait, _ = build_workflow(project(orientation="portrait"), "preview", tmp_path)
    assert portrait["6"]["inputs"]["width"] == 480
    assert portrait["6"]["inputs"]["height"] == 864


def test_manual_prompt_and_invalid_orientation():
    value = project(prompt_processing="manual", orientation="portrait")
    validate_project_config(value)
    assert runtime_profile(value, "context_ir")["billing"] == "无 API 调用"
    assert pipeline_for(value)[0]["label"] == "原始提示词确认"
    with pytest.raises(ValueError, match="画面方向"):
        validate_project_config(project(orientation="square"))
    with pytest.raises(ValueError, match="竖版"):
        validate_project_config(project(orientation="portrait", strategy="cloud"))


def test_quality_template_uses_fourteen_steps(tmp_path: Path) -> None:
    template = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "old"}},
        "5": {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {"prompt": "", "width": 1, "height": 1, "length": 1},
        },
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
        "7": {"class_type": "BasicScheduler", "inputs": {"steps": 20}},
        "14": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "old"}},
    }
    (tmp_path / QUALITY_TEMPLATE).write_text(json.dumps(template), encoding="utf-8")
    workflow, _ = build_workflow(project(strategy="safe"), "proof", tmp_path)
    assert workflow["7"]["inputs"]["steps"] == 14
    assert workflow["5"]["inputs"]["width"] == 864
    assert workflow["5"]["inputs"]["height"] == 480


def test_2k_source_grid_validation() -> None:
    validate_2k_spec(
        {
            "width": 1344,
            "height": 768,
            "fps": 24.0,
            "frames": 362,
        }
    )
    with pytest.raises(ValueError, match="24fps"):
        validate_2k_spec(
            {
                "width": 1344,
                "height": 768,
                "fps": 30.0,
                "frames": 362,
            }
        )


def test_runtime_profiles_use_measured_ranges() -> None:
    value = project(strategy="safe")
    preview = runtime_profile(value, "preview")
    proof = runtime_profile(value, "proof")
    final = runtime_profile(value, "local_768")
    summary = project_runtime_summary(value)
    assert runtime_profile(value, "context_ir")["label"] == "30秒–1分30秒"
    assert preview["label"] == "6分钟–8分钟"
    assert proof["label"] == "14分钟–18分钟"
    assert final["label"] == "1小时–1小时10分"
    assert summary["low_seconds"] == 30 + 360 + 840 + 3600
    assert summary["dynamic_cloud_stages"] == 1
    with pytest.raises(ValueError, match="帧数"):
        validate_2k_spec(
            {
                "width": 1344,
                "height": 768,
                "fps": 24.0,
                "frames": 360,
            }
        )
