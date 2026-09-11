from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_connector_api import connector_api, STUDIO_ROOT


ROOT = STUDIO_ROOT / "workflows"
spec = importlib.util.spec_from_file_location("quality_workflows_under_test", STUDIO_ROOT / "app/workflows.py")
workflows = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflows)
contract = connector_api.input_contract


@pytest.mark.parametrize("mode,roles,audio,embedded", [
    ("i2v", ["first_frame"], "native", False),
    ("l2v", ["last_frame"], "native", False),
    ("fl2v", ["first_frame", "last_frame"], "native", False),
    ("hybrid", ["first_frame", "last_frame", "reference_image"], "native", False),
    ("reference", ["reference_image"], "reference", False),
    ("reference", ["reference_image", "reference_audio"], "reference", False),
    ("reference", ["reference_video"], "reference", False),
    ("reference", ["reference_video"], "reference", True),
    ("i2v", ["first_frame", "reference_audio"], "lock_source", False),
    ("fl2v", ["first_frame", "last_frame", "reference_audio"], "lock_source", False),
])
def test_real_quality_templates_consume_inputs_without_turbo_sampling(mode, roles, audio, embedded):
    project = {"id": "offline-inputs", "mode": mode, "duration": 15, "orientation": "portrait",
               "prompt_approved": "Preserve the approved scene", "seed": 12345, "audio_policy": audio,
               "use_embedded_video_audio": embedded,
               "assets": {role: {"comfy_name": role + "." + ("mp4" if role.endswith("video") else "wav" if role.endswith("audio") else "png")} for role in roles}}
    graph, _ = workflows.build_workflow(project, "proof", ROOT)
    contract.bind_graph_assets(graph, project)
    contract.check_quality_controls(graph, project)
    conditioning = next(entry["inputs"] for entry in graph.values() if entry["class_type"] in (
        "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AudioConditioningT8"))
    assert conditioning["prompt"] == project["prompt_approved"]
    assert (conditioning["width"], conditioning["height"]) == (480, 864)
    for role in ("first_frame", "last_frame"):
        if role in roles:
            assert graph[conditioning[role][0]]["inputs"]["image"] == project["assets"][role]["comfy_name"]
    tampered = copy.deepcopy(graph)
    next(entry for entry in tampered.values() if entry["class_type"] == "BasicScheduler")["inputs"]["steps"] = 4
    with pytest.raises(Exception, match="14 steps"):
        contract.check_quality_controls(tampered, project)


def test_asset_roles_not_loader_sort_order_define_first_and_last_frames():
    project = {"id": "offline", "mode": "fl2v", "duration": 15, "orientation": "portrait", "audio_policy": "native",
               "prompt_approved": "Scene", "seed": 1, "assets": {role: {"comfy_name": role + ".png"} for role in ("first_frame", "last_frame")}}
    graph, _ = workflows.build_workflow(project, "proof", ROOT)
    conditioning = next(entry["inputs"] for entry in graph.values() if entry["class_type"] == "MiniMaxH3ImageToVideo")
    conditioning["first_frame"], conditioning["last_frame"] = conditioning["last_frame"], conditioning["first_frame"]
    contract.bind_graph_assets(graph, project)
    assert graph[conditioning["first_frame"][0]]["inputs"]["image"] == "first_frame.png"
    assert graph[conditioning["last_frame"][0]]["inputs"]["image"] == "last_frame.png"
