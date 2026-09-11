import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "h3_accelerated_test"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "fleet"), str(ROOT / "shared")]
sys.modules[PACKAGE] = package


def load(name, path):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


accelerated = load("accelerated_i2v", ROOT / "shared/accelerated_i2v.py")
catalog_module = load("multimodal_catalog", ROOT / "fleet/multimodal_catalog.py")


class Base:
    def __init__(self):
        self.graph = {
            "4": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_int8_convrot.safetensors"}},
            "5": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {"model": ["4", 0], "lora_name": "a4v12_people10_fp32.safetensors", "strength_model": 1.0}},
            "6": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {"task_type": "T2VA", "prompt": "r34l1sm\nscene", "width": 480, "height": 864, "length": 362, "audio_mode": "native"}},
            "7": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {"steps": 4, "model": ["5", 0]}},
            "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
            "13": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "video/test", "video": ["6", 1]}},
        }

    def get(self, identifier):
        assert identifier == "A4_C1"
        return {"recipe_digest": "base-pinned", "people_lora_strength": 1.0}

    def build(self, identifier, prompt, seed, prefix):
        self.get(identifier)
        graph = copy.deepcopy(self.graph)
        graph["6"]["inputs"]["prompt"] = prompt
        graph["8"]["inputs"]["noise_seed"] = seed
        graph["13"]["inputs"]["filename_prefix"] = prefix
        return graph, {}

    def public(self):
        return {"recipes": [{"recipe_id": "A4_C1"}]}


@pytest.fixture
def case(tmp_path):
    base = Base()
    graph = accelerated.build(base.graph, "scene", 2696045020911358409, "asset_" + "a" * 32 + ".png", "video/test")
    raw = json.dumps(graph).encode()
    profile = accelerated.profile(raw)
    (tmp_path / profile["template"]).write_bytes(raw)
    entry = {**profile, "sampling": {"steps": 4}, "template_sha256": profile["version"],
             "structure_sha256": catalog_module.structure(graph), "base_recipe_digest": "base-pinned",
             "asset_roles": {"first_frame": "20"}, "weights": [{"filename": "pinned-lora"}]}
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema_version": 1, "profiles": [entry]}))
    return base, graph, entry, catalog_module.MultimodalCatalog(base, path)


def test_keeps_c1_and_consumes_first_frame(case):
    base, graph, entry, catalog = case
    result = catalog.validate(entry["profile_id"], graph, entry["version"])
    assert result["steps"] == 4 and result["frame_count"] == 362
    assert result["input_roles"] == {"first_frame": "asset_" + "a" * 32 + ".png"}
    assert graph["6"]["inputs"]["first_frame"] == ["20", 0]
    assert graph["8"]["inputs"]["noise_seed"] == 2696045020911358409
    assert base.graph["6"]["inputs"]["task_type"] == "T2VA"


@pytest.mark.parametrize("node,field,value", [("6", "task_type", "T2VA"), ("6", "first_frame", ["4", 0]),
    ("6", "width", 288), ("6", "length", 124), ("6", "audio_mode", "lock_source"),
    ("5", "strength_model", 0.5), ("5", "lora_name", "other.safetensors"), ("7", "steps", 14)])
def test_rejects_unapproved_workflow_changes(case, node, field, value):
    _, graph, entry, catalog = case
    graph[node]["inputs"][field] = value
    with pytest.raises(ValueError):
        catalog.validate(entry["profile_id"], graph, entry["version"])


def test_wrong_version_rejected(case):
    _, graph, entry, catalog = case
    with pytest.raises(ValueError):
        catalog.validate(entry["profile_id"], graph, "old")


def test_trigger_not_duplicated(case):
    base, _, _, _ = case
    graph = accelerated.build(base.graph, "r34l1sm\nscene", 1, "asset_" + "a" * 32 + ".png", "video/test")
    assert graph["6"]["inputs"]["prompt"] == "r34l1sm\nscene"
    with pytest.raises(ValueError):
        accelerated.build(base.graph, "r34l1sm\nr34l1sm scene", 1, "asset_" + "a" * 32 + ".png", "video/test")


def test_studio_freezes_acceleration_profile_and_original_input(case, tmp_path):
    _, graph, _, _ = case
    contract = load("input_contract", ROOT / "studio/input_contract.py")
    path = tmp_path / "accelerated-i2v.json"
    path.write_text(json.dumps(graph))
    module = SimpleNamespace(__file__=str(tmp_path / "main.py"), __package__=PACKAGE)
    project = {"id": "test", "mode": "i2v", "preview_recipe_id": "A4_C1", "audio_policy": "native",
               "duration": 15, "orientation": "portrait", "seed": 2696045020911358409,
               "prompt_approved": "original prompt", "assets": {"first_frame": {"comfy_name": "asset_" + "b" * 32 + ".png"}}}
    project["execution_profile"] = contract.profile(module, project)
    output, _ = contract.quality_graph(module, project)
    assert output["20"]["inputs"]["image"] == project["assets"]["first_frame"]["comfy_name"]
    assert output["6"]["inputs"]["prompt"] == "r34l1sm\noriginal prompt"
    assert project["prompt_approved"] == "original prompt"
    before = contract.snapshot(project)
    project["preview_recipe_id"] = "retired"
    assert before != contract.snapshot(project)
    with pytest.raises(Exception, match="480"):
        contract.profile(module, project)
