from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys


BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(studio, fleet, target, recipe_ids=("A4_C1",), runtime_pins=None):
    if target.exists():
        raise ValueError("candidate already exists; inspect instead of overwriting")
    shared = load("accelerated", BASE / "shared/accelerated_i2v.py")
    recipes = load("recipe_source", fleet / "app/recipes.py")
    catalog_source = load("multimodal_source", BASE / "fleet/multimodal_catalog.py")
    catalog = recipes.RecipeCatalog(fleet / "config/recipes.json")
    for kind, origin in (("studio", studio), ("fleet", fleet)):
        shutil.copytree(origin, target / kind, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        for path in [target / kind, *(target / kind).rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        shutil.copy2(BASE / "shared/accelerated_i2v.py", target / kind / "app/accelerated_i2v.py")
    for name in ("connector_api.py", "input_contract.py", "input_view.py"):
        shutil.copy2(BASE / "studio" / name, target / "studio/app" / name)
    preparer = load("release_preparer", BASE / "scripts/prepare_release.py")
    (target / "studio/app/connector_schema.json").write_bytes(preparer.connector_schema((BASE / "studio/connector_api.py").read_bytes()))
    shutil.copy2(BASE / "fleet/multimodal_catalog.py", target / "fleet/app/multimodal_catalog.py")
    manifest_path = target / "fleet/multimodal-profiles.json"
    manifest = json.loads(manifest_path.read_bytes())
    full = next(entry for entry in manifest["profiles"] if entry["profile_id"] == "H3_I2V_QUALITY14")
    entries = []
    for recipe_id in recipe_ids:
        source = catalog.get(recipe_id)
        graph, _ = catalog.build(recipe_id, "Pinned first-frame template", 0, "video/acceptance")
        graph = shared.build(graph, "Pinned first-frame template", 0, "asset_" + "0" * 32 + ".png", "video/acceptance", recipe_id)
        shared.validate(graph, catalog, recipe_id)
        raw = json.dumps(graph, ensure_ascii=False, indent=2).encode()
        profile = shared.profile(raw, recipe_id)
        if any(entry["profile_id"] == profile["profile_id"] for entry in manifest["profiles"]):
            raise ValueError("profile already exists; preserve existing evidence")
        if runtime_pins is None:
            if recipe_id != "A4_C1":
                raise ValueError("explicit current runtime pins required")
            combined = next(weight for weight in source["weights"] if weight.get("role") == "composed_lora")
            weights = [*full["weights"], {"filename": combined["filename"], "sha256": combined["sha256"]}]
            runtime_version = "2676b7bb553b9191eafcba2e3652c3b406220fa13acb764bf850a6ea11b0d13b"
        else:
            backend = next(item for item in runtime_pins.values() if recipe_id in item["recipes"])
            runtime_version = backend["runtime_version"]
            weights = []
            for weight in source["weights"]:
                matched = next(item for item in backend["weights"] if item["filename"] == weight["filename"])
                if weight.get("sha256") and matched["sha256"] != weight["sha256"]:
                    raise ValueError("runtime weight differs from frozen recipe")
                weights.append({"filename": weight["filename"], "sha256": matched["sha256"]})
        entry = {**full, **profile, "template": "multimodal/" + profile["template"],
                 "template_sha256": profile["version"], "structure_sha256": catalog_source.structure(graph),
                 "sampling": {"steps": profile["steps"], "sampler_node": "10"}, "asset_roles": {"first_frame": "20"},
                 "base_recipe_digest": source["recipe_digest"], "qualification": "not_validated",
                 "runtime_version_required": runtime_version, "runtime_source": source["runtime_source"], "weights": weights}
        manifest["profiles"].append(entry)
        entries.append(entry)
        (target / "studio/app" / profile["template"]).write_bytes(raw)
        (target / "fleet" / entry["template"]).write_bytes(raw)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    for kind, origin in (("studio", studio), ("fleet", fleet)):
        changed = {}
        for path in (target / kind).rglob("*"):
            if not path.is_file() or path.name == "candidate-manifest.json":
                continue
            relative = path.relative_to(target / kind)
            original = origin / relative
            if not original.exists() or path.read_bytes() != original.read_bytes():
                changed[str(relative)] = {"before": hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None,
                                          "after": hashlib.sha256(path.read_bytes()).hexdigest()}
        (target / (kind + "-changes.json")).write_text(json.dumps(changed, indent=2))
    (target / "profiles.json").write_text(json.dumps(entries, indent=2))
    if len(entries) == 1:
        (target / "profile.json").write_text(json.dumps(entries[0], indent=2))
    print(json.dumps({"candidate": str(target), "profiles": [entry["profile_id"] for entry in entries]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("studio", "fleet", "target"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--recipe-ids", nargs="+", default=["A4_C1"])
    parser.add_argument("--runtime-pins", type=Path)
    options = parser.parse_args()
    prepare(options.studio, options.fleet, options.target, options.recipe_ids,
            json.loads(options.runtime_pins.read_bytes()) if options.runtime_pins else None)
