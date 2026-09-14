import copy
import json
from pathlib import Path

import pytest

from app.recipes import DEFAULT_PATH, DYNAMIC_INPUTS, FORMAL, RETIRED, RecipeCatalog


@pytest.fixture
def catalog():
    result = RecipeCatalog()
    assert result.error is None
    return result


@pytest.mark.parametrize("recipe_id", sorted(FORMAL))
def test_build_validate_roundtrip(catalog, recipe_id):
    prompt = "  Original prompt\n[Shot 1] Keep every word.\n"
    graph, binding = catalog.build(recipe_id, prompt, 2**64 - 1, "video/router/test_123")
    recipe = catalog.get(recipe_id)
    assert graph["6"]["inputs"]["prompt"] == recipe["trigger"] + prompt
    assert graph["8"]["inputs"]["noise_seed"] == 2**64 - 1
    assert graph["13"]["inputs"]["filename_prefix"] == "video/router/test_123"
    assert catalog.validate(recipe_id, graph, recipe["version"]) == binding
    assert binding["recipe_version"] == recipe["version"]
    assert binding["hardware_qualification"] == "not_evaluated"
    assert binding["recipe_digest"] == recipe["recipe_digest"]
    assert graph["6"]["inputs"]["length"] == 362
    assert graph["6"]["inputs"]["width"] == 480
    assert graph["6"]["inputs"]["height"] == 864
    assert graph["6"]["inputs"]["audio_mode"] == "native"
    assert graph["6"]["inputs"]["task_type"] == "T2VA"
    assert graph["12"]["inputs"]["fps"] == 24
    assert len(graph) == 13
    assert json.loads(json.dumps(binding, allow_nan=False)) == binding


def test_public_catalog_and_retired_history(catalog):
    public = catalog.public()
    assert public["default_recipe_id"] == "A4"
    assert public["catalog_status"] == "ready"
    assert [recipe["recipe_id"] for recipe in public["recipes"]] == ["A4", "A4_C0", "A4_C1", "B8"]
    assert set(public["retired_recipe_ids"]) == RETIRED
    for recipe_id in RETIRED:
        assert catalog.get(recipe_id)["history_preserved"] is True
        with pytest.raises(ValueError, match="retired"):
            catalog.build(recipe_id, "prompt", 0, "output")
        with pytest.raises(ValueError, match="retired"):
            catalog.validate(recipe_id, {})


@pytest.mark.parametrize("recipe_id", sorted(FORMAL))
def test_every_static_input_and_class_is_fixed(catalog, recipe_id):
    graph, _ = catalog.build(recipe_id, "prompt", 0, "output")
    for node_id, node in graph.items():
        for field in node["inputs"]:
            if (node_id, field) in DYNAMIC_INPUTS.values():
                continue
            changed = copy.deepcopy(graph)
            changed[node_id]["inputs"][field] = "tampered"
            with pytest.raises(ValueError, match="pinned recipe"):
                catalog.validate(recipe_id, changed)
        changed = copy.deepcopy(graph)
        changed[node_id]["class_type"] = "ClientDeclaredSafeSampler"
        with pytest.raises(ValueError, match="pinned recipe"):
            catalog.validate(recipe_id, changed)


@pytest.mark.parametrize("change", ["extra_node", "missing_node", "extra_input", "label", "shape", "legacy"])
def test_no_shape_label_or_original_studio_conversion(catalog, change):
    graph, _ = catalog.build("A4", "prompt", 0, "output")
    if change == "extra_node":
        graph["99"] = {"class_type": "LoadImage", "inputs": {"image": "unexpected.png"}}
    elif change == "missing_node":
        del graph["11"]
    elif change == "extra_input":
        graph["10"]["inputs"]["safe"] = True
    elif change == "label":
        graph["10"]["_meta"] = {"title": "A4 validated"}
    elif change == "shape":
        graph["shape"] = {"width": 480, "height": 864, "frames": 362}
    else:
        graph["5"]["inputs"]["lora_name"] = "t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors"
        graph["7"]["inputs"]["shift_video"] = 12.0
    with pytest.raises(ValueError):
        catalog.validate("A4", graph)
    with pytest.raises(ValueError):
        catalog.freeze("A4", graph)


def test_recipe_and_version_mismatch(catalog):
    graph, _ = catalog.build("B8", "prompt", 0, "output")
    with pytest.raises(ValueError):
        catalog.validate("A4", graph)
    with pytest.raises(ValueError, match="version"):
        catalog.validate("B8", graph, "old")
    with pytest.raises(ValueError, match="unknown"):
        catalog.build("arbitrary", "prompt", 0, "output")


@pytest.mark.parametrize("recipe_id", ["A4_C0", "A4_C1"])
def test_trigger_added_once_and_required(catalog, recipe_id):
    graph, binding = catalog.build(recipe_id, "r34l1sm\nprompt", 0, "output")
    assert graph["6"]["inputs"]["prompt"] == "r34l1sm\nprompt"
    assert catalog.build(recipe_id, "prompt", 0, "output")[1] == binding
    for invalid in ("prompt", "r34l1sm\n", "r34l1sm\nr34l1sm\nprompt"):
        graph["6"]["inputs"]["prompt"] = invalid
        with pytest.raises(ValueError, match="trigger"):
            catalog.validate(recipe_id, graph)


@pytest.mark.parametrize("field,value", [
    ("prompt", ""), ("prompt", " \n"), ("prompt", None), ("prompt", "bad\x00prompt"),
    ("seed", True), ("seed", -1), ("seed", 2**64), ("seed", 0.0),
    ("prefix", "../escape"), ("prefix", "/absolute"), ("prefix", "video/../escape"),
    ("prefix", "video\\escape"), ("prefix", "video//escape"), ("prefix", ""),
])
def test_dynamic_values_validated_for_build_and_validate(catalog, field, value):
    arguments = {"prompt": "prompt", "seed": 0, "prefix": "output"}
    arguments[field] = value
    with pytest.raises(ValueError):
        catalog.build("A4", **arguments)
    graph, _ = catalog.build("A4", "prompt", 0, "output")
    node, key = DYNAMIC_INPUTS[field]
    graph[node]["inputs"][key] = value
    with pytest.raises(ValueError):
        catalog.validate("A4", graph)


def test_detached_graphs_entries_and_binding_hashes(catalog):
    first, binding = catalog.build("A4", "prompt", 0, "output")
    frozen, frozen_binding = catalog.freeze("A4", first)
    assert frozen == first and frozen is not first and frozen_binding == binding
    catalog.get("A4")["sampling"]["steps"] = 100
    catalog.public()["recipes"][0]["sampling"]["steps"] = 100
    frozen["7"]["inputs"]["steps"] = 100
    for prompt, seed, prefix in [("other", 0, "output"), ("prompt", 1, "output"), ("prompt", 0, "other")]:
        _, changed = catalog.build("A4", prompt, seed, prefix)
        assert changed["graph_sha256"] != binding["graph_sha256"]
        assert changed["structure_sha256"] == binding["structure_sha256"]
        assert changed["recipe_digest"] == binding["recipe_digest"]
    assert catalog.build("A4", "prompt", 0, "output")[0] == first


@pytest.mark.parametrize("kind", ["missing", "invalid_json", "duplicate_key", "template_hash", "escape", "version"])
def test_unavailable_catalog_does_not_break_import_or_fall_back(tmp_path, kind):
    path = tmp_path / "recipes.json"
    document = json.loads(DEFAULT_PATH.read_bytes())
    if kind == "invalid_json":
        path.write_text("{")
    elif kind == "duplicate_key":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif kind != "missing":
        for entry in document["recipes"]:
            destination = tmp_path / entry["template"]
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes((DEFAULT_PATH.parent / entry["template"]).read_bytes())
        if kind == "template_hash":
            (tmp_path / document["recipes"][0]["template"]).write_text("{}")
        elif kind == "escape":
            document["recipes"][0]["template"] = str(DEFAULT_PATH.parent / "recipes/A4.json")
        else:
            document["recipes"][0]["version"] = None
        path.write_text(json.dumps(document))
    unavailable = RecipeCatalog(path)
    assert unavailable.error
    assert unavailable.public()["catalog_status"] == "unavailable"
    assert unavailable.public()["recipes"] == []
    with pytest.raises(ValueError, match="unavailable"):
        unavailable.build("A4", "prompt", 0, "output")


def test_delivery_is_local_and_unknown_weight_hashes_are_explicit(catalog, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("build must not read source evidence or remote templates")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    catalog.build("B8", "prompt", 0, "output")
    unknown = [weight["role"] for weight in catalog.get("A4")["weights"] if weight["sha256"] is None]
    assert unknown == ["text_encoder", "video_vae", "audio_vae"]
    assert catalog.get("A4_C1")["weights"][-1]["sha256"] == "5f7d9c69972d65c96438614af645b6d9e60f9a91daf1699179e2843e2de892fc"
    graph, _ = catalog.build("B8", "prompt", 0, "output")
    assert graph["5"]["inputs"]["verify_hashes"] is True
    assert graph["5"]["inputs"]["allow_structural_base"] is True
