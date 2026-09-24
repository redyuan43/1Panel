"""Pinned, bounded managed video recipe proven on Ivan's two RTX 3060s."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


QUALITY480_RECIPE = "h3-i2va-480p15-3060-v1"
QUALITY480_TEMPLATE = Path(__file__).resolve().parents[1] / "config/workflows/h3-i2va-480p15-3060-v1.api.json"
QUALITY480_TEMPLATE_SHA256 = "24940d290917ddb01e67eec30c0e9c0c8fe95749d2a32a4645b3c5ba54843298"


def build_quality480(execution_id: str, prompt: str, seed: int, first_frame: str) -> tuple[dict, dict]:
    if not execution_id or not prompt.strip() or not first_frame or seed < 0:
        raise ValueError("quality480 recipe requires execution id, prompt, seed and first frame")
    raw = QUALITY480_TEMPLATE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != QUALITY480_TEMPLATE_SHA256:
        raise ValueError("quality480 recipe template changed without a new version")
    graph = copy.deepcopy(json.loads(raw))
    graph["5"]["inputs"]["prompt"] = prompt.strip()
    graph["6"]["inputs"]["noise_seed"] = seed
    graph["20"]["inputs"]["image"] = first_frame
    graph["14"]["inputs"]["filename_prefix"] = (
        "video/router/" + hashlib.sha256(execution_id.encode()).hexdigest()[:24]
    )
    return graph, {"recipe_id": QUALITY480_RECIPE, "template": QUALITY480_TEMPLATE.name,
                   "width": 864, "height": 480, "steps": 14, "frame_count": 362,
                   "actual_duration": 362 / 24, "seed": seed}


def validate_quality480(payload: dict[str, Any]) -> dict[str, Any]:
    """Only the exact reviewed graph may use this two-lane admission class."""
    metadata = payload.get("extra_data", {}).get("h3", {})
    contract = metadata.get("contract") or {}
    graph = payload.get("prompt") or {}
    try:
        execution_id = metadata["execution_id"]
        prompt = graph["5"]["inputs"]["prompt"]
        seed = graph["6"]["inputs"]["noise_seed"]
        first_frame = graph["20"]["inputs"]["image"]
        if (not isinstance(execution_id, str) or not isinstance(prompt, str)
                or type(seed) is not int or not isinstance(first_frame, str)):
            raise ValueError("invalid quality480 recipe fields")
        expected, expected_contract = build_quality480(execution_id, prompt, seed, first_frame)
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid quality480 recipe graph") from error
    if graph != expected or any(contract.get(key) != value for key, value in expected_contract.items()):
        raise ValueError("quality480 graph differs from the qualified recipe")
    if contract.get("input_files") != [first_frame]:
        raise ValueError("quality480 input differs from the qualified recipe")
    return {"profile": "quality", "class": "long", "known_shape": True,
            "recipe_id": QUALITY480_RECIPE, "frame_count": 362, "width": 864,
            "height": 480, "steps": 14}
