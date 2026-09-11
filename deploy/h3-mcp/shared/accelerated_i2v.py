from __future__ import annotations

import copy
import hashlib
import json
import re


PROFILE_ID = "H3_I2V_A4_C1_TURBO4"
ASSET_NODE = "20"
RECIPES = {
    "A4": ("H3_I2V_A4_TURBO4", "accelerated-i2v-a4.json", 4, "A4 · 首帧四步", ""),
    "A4_C0": ("H3_I2V_A4_C0_TURBO4", "accelerated-i2v-c0.json", 4, "A4＋写实触发词 · 首帧四步", "r34l1sm\n"),
    "A4_C1": (PROFILE_ID, "accelerated-i2v.json", 4, "A4＋人物写实增强 · 首帧四步", "r34l1sm\n"),
    "B8": ("H3_I2V_B8_VDN8", "accelerated-i2v-b8.json", 8, "B8 OpenVDN · 首帧八步", ""),
}
PROFILE_RECIPES = {settings[0]: recipe for recipe, settings in RECIPES.items()}


def execution_prompt(prompt, recipe_id):
    trigger = RECIPES[recipe_id][4]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt required")
    if trigger:
        if not prompt.startswith(trigger):
            prompt = trigger + prompt
        if prompt[len(trigger):].lstrip().startswith("r34l1sm"):
            raise ValueError("duplicate realism trigger")
    return prompt


def build(base_graph, prompt, seed, filename, prefix, recipe_id="A4_C1"):
    graph = copy.deepcopy(base_graph)
    if ASSET_NODE in graph or graph["6"]["inputs"]["task_type"] != "T2VA":
        raise ValueError("expected original pinned text template")
    prompt = execution_prompt(prompt, recipe_id)
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("invalid seed")
    if not re.fullmatch(r"asset_[a-f0-9]{32}\.(?:png|jpg|jpeg|webp)", filename):
        raise ValueError("immutable first frame required")
    graph[ASSET_NODE] = {"class_type": "LoadImage", "inputs": {"image": filename}}
    graph["6"]["inputs"].update(prompt=prompt, task_type="I2VA", first_frame=[ASSET_NODE, 0])
    graph["8"]["inputs"]["noise_seed"] = seed
    graph["13"]["inputs"]["filename_prefix"] = prefix
    return graph


def validate(graph, base, recipe_id="A4_C1"):
    source = base.get(recipe_id)
    inputs = graph.get("6", {}).get("inputs", {})
    original, _ = base.build(recipe_id, inputs.get("prompt", ""),
                             graph["8"]["inputs"]["noise_seed"], graph["13"]["inputs"]["filename_prefix"])
    expected = build(original, inputs["prompt"], graph["8"]["inputs"]["noise_seed"],
                     graph[ASSET_NODE]["inputs"]["image"], graph["13"]["inputs"]["filename_prefix"], recipe_id)
    if graph != expected or (recipe_id == "A4_C1" and source.get("people_lora_strength") != 1.0):
        raise ValueError("I2V must differ from pinned recipe only by first-frame binding and task type")
    return source


def profile(template, recipe_id="A4_C1"):
    identifier, filename, steps, label, _ = RECIPES[recipe_id]
    return {"profile_id": identifier, "version": hashlib.sha256(template).hexdigest(),
            "template": filename, "steps": steps, "family": "FL2VA",
            "mode": "i2v", "audio_policy": "native", "preview_recipe_id": recipe_id,
            "label": label, "width": 480, "height": 864}
