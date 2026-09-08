from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SUPPORTED_MODES = {"t2v", "i2v", "l2v", "fl2v"}
IMAGE_ASSETS = {"first_frame", "last_frame"}


def frames_for_duration(duration: int) -> int:
    if duration < 4 or duration > 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    valid = list(range(107, 363, 17))
    target = duration * 24
    return min(valid, key=lambda value: (abs(value - target), value))


def actual_duration(duration: int) -> float:
    return frames_for_duration(duration) / 24


def _template(profile: str) -> Path:
    variable = "H3_TURBO_TEMPLATE" if profile == "preview" else "H3_QUALITY_TEMPLATE"
    default = (
        "/mnt/ivan-ext4-offload/h3-deploy/workflows/turbo4-api.json"
        if profile == "preview"
        else "/mnt/ivan-ext4-offload/h3-deploy/workflows/quality14-768-api.json"
    )
    path = Path(os.environ.get(variable, default))
    if not path.is_file():
        raise FileNotFoundError(f"{variable} is unavailable")
    return path


def _node(workflow: dict[str, Any], class_type: str) -> tuple[str, dict[str, Any]]:
    for node_id, value in workflow.items():
        if value.get("class_type") == class_type:
            return node_id, value
    raise ValueError(f"workflow template lacks {class_type}")


def _next_node_id(workflow: dict[str, Any]) -> int:
    return max((int(key) for key in workflow if str(key).isdigit()), default=0) + 1


def _load_image(
    workflow: dict[str, Any],
    *,
    filename: str,
    next_id: int,
) -> tuple[list[Any], int]:
    node_id = str(next_id)
    workflow[node_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": filename},
    }
    return [node_id, 0], next_id + 1


def build_workflow(
    *,
    execution_id: str,
    profile: str,
    mode: str,
    prompt: str,
    duration: int,
    seed: int,
    aspect_ratio: str,
    assets: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if profile not in {"preview", "quality"}:
        raise ValueError("profile must be preview or quality")
    if mode not in SUPPORTED_MODES:
        raise ValueError("managed Ivan workflows support t2v, i2v, l2v and fl2v")
    required = {
        "i2v": {"first_frame"},
        "l2v": {"last_frame"},
        "fl2v": {"first_frame", "last_frame"},
    }.get(mode, set())
    if not required <= assets.keys() or set(assets) - IMAGE_ASSETS:
        raise ValueError("managed workflow image anchors do not match the selected mode")
    if aspect_ratio not in {"16:9", "9:16"}:
        raise ValueError("aspect_ratio must be 16:9 or 9:16")
    if not prompt.strip():
        raise ValueError("prompt is required")
    frame_count = frames_for_duration(duration)
    if seed < 0:
        seed = int.from_bytes(hashlib.sha256(execution_id.encode()).digest()[:8], "big") % (2**63)
    path = _template(profile)
    workflow = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
    portrait = aspect_ratio == "9:16"
    width, height = (
        ((480, 864) if portrait else (864, 480))
        if profile == "preview"
        else ((768, 1344) if portrait else (1344, 768))
    )
    conditioning_type = (
        "MiniMaxH3AudioConditioningT8"
        if profile == "preview"
        else "MiniMaxH3ImageToVideo"
    )
    _, conditioning = _node(workflow, conditioning_type)
    conditioning_inputs = conditioning.setdefault("inputs", {})
    conditioning_inputs.update(
        prompt=prompt.strip(),
        width=width,
        height=height,
        length=frame_count,
    )
    if profile == "preview":
        conditioning_inputs["task_type"] = {
            "t2v": "T2VA",
            "i2v": "I2VA",
            "l2v": "L2VA",
            "fl2v": "FL2VA",
        }[mode]
        conditioning_inputs["audio_mode"] = "native"
        conditioning_inputs["add_source_as_reference"] = False
        conditioning_inputs["prompt_primary_audio_ordinal"] = 0
    next_id = _next_node_id(workflow)
    for name in ("first_frame", "last_frame"):
        if name not in assets:
            conditioning_inputs.pop(name, None)
            continue
        link, next_id = _load_image(
            workflow,
            filename=assets[name],
            next_id=next_id,
        )
        conditioning_inputs[name] = link
    _, noise = _node(workflow, "RandomNoise")
    noise.setdefault("inputs", {})["noise_seed"] = seed
    sampler_type = "MiniMaxH3DualClockSamplerT8" if profile == "preview" else "BasicScheduler"
    _, sampler = _node(workflow, sampler_type)
    sampler.setdefault("inputs", {})["steps"] = 6 if profile == "preview" else 14
    _, save = _node(workflow, "SaveVideo")
    save.setdefault("inputs", {})["filename_prefix"] = (
        "video/router/" + hashlib.sha256(execution_id.encode()).hexdigest()[:24]
    )
    return workflow, {
        "template": path.name,
        "width": width,
        "height": height,
        "steps": 6 if profile == "preview" else 14,
        "frame_count": frame_count,
        "actual_duration": frame_count / 24,
        "seed": seed,
    }
