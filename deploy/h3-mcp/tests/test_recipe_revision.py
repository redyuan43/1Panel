from types import SimpleNamespace
from test_connector_api import studio, studio_factory
from test_input_assets import upload
import importlib

def test_explicit_firstframe_recipe_null_and_mode_change(studio, monkeypatch, tmp_path):
    root=tmp_path / "workflows"
    root.mkdir()
    contract=importlib.import_module(studio.module.__package__+".input_contract")
    workflows=importlib.import_module(studio.module.__package__+".workflows")
    for filename in ["accelerated-i2v-a4.json", workflows.COMMUNITY_TEMPLATES["i2v"]]:
        path=root / filename; path.parent.mkdir(parents=True,exist_ok=True); path.write_text("{}")
    settings=SimpleNamespace(**vars(studio.module.SETTINGS)); settings.workflow_root=root
    monkeypatch.setattr(studio.module,"SETTINGS",settings)
    asset=upload(studio).json()["asset_id"]
    draft=studio.draft(mode="i2v",assets={"first_frame":asset},preview_recipe_id="A4")
    assert draft["execution_profile"]["steps"]==4
    approved=studio.confirm(draft)
    edited=studio.ok("h3_save_draft",studio.arguments(approved,original_prompt="原始中文提示词",prompt="十五秒竖版草稿，原生声音。",preview_recipe_id=None))
    assert edited["execution_profile"]["steps"]==14
    assert edited["revision"]!=approved["revision"] and not edited["prompt_approved"]
    assert not studio.module.STORE.get(edited["task_id"]).get("preview_recipe_id")
    accelerated=studio.ok("h3_save_draft",studio.arguments(edited,original_prompt="原始中文提示词",prompt="十五秒竖版草稿，原生声音。",preview_recipe_id="A4"))
    changed=studio.ok("h3_save_draft",studio.arguments(accelerated,original_prompt="原始中文提示词",prompt="十五秒竖版草稿，原生声音。",mode="t2v",assets={},recipe_id="A4"))
    assert changed["mode"]=="t2v"
    assert not studio.module.STORE.get(changed["task_id"]).get("preview_recipe_id")
    assert studio.call("h3_save_draft",studio.arguments(changed,original_prompt="原始中文提示词",prompt="十五秒竖版草稿，原生声音。",preview_recipe_id="A4")).status_code==400
    assert not studio.fleet.submissions
