import importlib
from types import SimpleNamespace
from test_connector_api import studio, studio_factory, STUDIO_ROOT
from test_input_assets import upload


def test_native_hybrid_save_confirm_build_binds_all_assets(studio, monkeypatch):
    settings = SimpleNamespace(**vars(studio.module.SETTINGS))
    settings.workflow_root = STUDIO_ROOT / "workflows"
    monkeypatch.setattr(studio.module, "SETTINGS", settings)
    roles = ("first_frame", "last_frame", "reference_image")
    assets = {role: upload(studio, kind=role).json()["asset_id"] for role in roles}
    task = studio.draft(mode="hybrid", assets=assets, seed=66125663, duration=15,
                        orientation="portrait", original_prompt="原文三素材不改写", prompt="原文三素材不改写")
    assert task["execution_profile"]["steps"] == 14
    approved = studio.confirm(task)
    project = studio.module.STORE.get(approved["task_id"])
    workflows = importlib.import_module(studio.module.__package__ + ".workflows")
    contract = importlib.import_module(studio.module.__package__ + ".input_contract")
    graph, _ = workflows.build_workflow(project, "preview", settings.workflow_root)
    contract.bind_graph_assets(graph, project)
    contract.check_quality_controls(graph, project)
    assert graph["5"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert graph["5"]["inputs"]["prompt"] == "原文三素材不改写"
    assert graph["7"]["inputs"]["steps"] == 14
    assert graph["6"]["inputs"]["noise_seed"] == 66125663
    for node, role in zip(("20", "21", "22"), roles):
        assert graph[node]["inputs"]["image"] == project["assets"][role]["comfy_name"]
    assert graph["9"]["inputs"]["conditioning"] == ["42", 0]
    assert not any(n["class_type"] == "MiniMaxH3AudioConditioningT8" for n in graph.values())
    assert not studio.fleet.submissions
