from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configuration(original, observations, catalog):
    policy = copy.deepcopy(original)
    if {backend["id"] for backend in policy["backends"]} != {"single-a4", "single-realism", "single-b8"}:
        raise ValueError("inspect current release backends before freezing")
    registry = {}
    for backend in policy["backends"]:
        observed = observations[backend["id"]]
        unit = "h3-" + backend["id"] + ".service"
        if (observed["unit"] != unit or not re.fullmatch(r"[a-f0-9]{64}", observed["unit_sha256"])
                or observed["pid"] != backend["pid"] or observed["runtime_version"] != backend["runtime_version"]
                or observed["cmdline_sha256"] != backend["cmdline_sha256"]):
            raise ValueError("backend observation does not match release identity")
        if not Path(observed["input_root"]).is_absolute():
            raise ValueError("observed effective input directory is required")
        backend["input_root"] = observed["input_root"]
        registry[backend["id"]] = {"unit": unit, "unit_sha256": observed["unit_sha256"],
                                  "cold_start_budget_bytes": max(18, policy.get("static_budget_gib", 18)) * 1024**3}
        if backend["id"] == "single-a4":
            for profile in catalog["profiles"]:
                if profile["runtime_version_required"] != backend["runtime_version"]:
                    raise ValueError("multimodal runtime version mismatch")
                backend["recipes"][profile["profile_id"]] = {"recipe_version": profile["version"],
                    "qualification": "not_validated", "vram_budget_bytes": max(14 * 1024**3, backend["recipes"]["A4"]["vram_budget_bytes"])}
    policy["lifecycle_backends"] = registry
    return policy


def prepare(studio, base, policy_file, observations_file, destination):
    root = Path(__file__).parent
    builder = load("frozen_catalog_builder", root / "build_multimodal_catalog.py")
    release = load("frozen_release_builder", root / "prepare_multimodal_release.py")
    original = json.loads(policy_file.read_bytes())
    backend = next(item for item in original["backends"] if item["id"] == "single-a4")
    catalog_files = builder.build(studio, backend)
    catalog = json.loads(catalog_files["multimodal-profiles.json"])
    policy = configuration(original, json.loads(observations_file.read_bytes()), catalog)
    files = {path.relative_to(base).as_posix(): path.read_bytes() for path in base.rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc" and path.name != "candidate-manifest.json"}
    files = release.fleet_overlay(files)
    files.update(catalog_files)
    files["recipe-policy-multimodal.json"] = (json.dumps(policy, indent=2) + "\n").encode()
    result = release.preparer.write_candidate(destination, files, {"kind": "fleet-multimodal",
        "base_provenance": {"source": str(base), "policy_sha256": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
                            "observations_sha256": hashlib.sha256(observations_file.read_bytes()).hexdigest()},
        "profiles_registered_not_qualified": len(catalog["profiles"]), "missing_weight_profiles": len(catalog["unavailable_profiles"])})
    return {"candidate": str(destination), "files": len(result["files_sha256"]), "deployed": False, "new_modes_qualified": False}


if __name__ == "__main__":
    print(json.dumps(prepare(*map(Path, sys.argv[1:]))))
