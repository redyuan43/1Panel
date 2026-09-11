from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("i2v", ("first_frame",), "native", False),
    ("l2v", ("last_frame",), "native", False),
    ("fl2v", ("first_frame", "last_frame"), "native", False),
    ("hybrid", ("first_frame", "last_frame", "reference_image"), "native", False),
    ("reference", ("reference_image",), "reference", False),
    ("reference", ("reference_image", "reference_audio"), "reference", False),
    ("reference", ("reference_video",), "reference", False),
    ("reference", ("reference_video",), "reference", True),
    ("i2v", ("first_frame", "reference_audio"), "lock_source", False),
    ("fl2v", ("first_frame", "last_frame", "reference_audio"), "lock_source", False),
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(studio, backend):
    workflows = load("catalog_source_workflows", studio / "app/workflows.py")
    contract = load("catalog_input_contract", ROOT / "studio/input_contract.py")
    catalog = load("catalog_validation", ROOT / "fleet/multimodal_catalog.py")
    available = {weight["filename"]: weight for weight in backend["weight_files"]}
    files, profiles, blocked = {}, [], []
    for mode, roles, audio, embedded in CASES:
        assets = {role: {"comfy_name": "asset_" + format(index + 1, "032x") + (".mp4" if role.endswith("video") else ".wav" if role.endswith("audio") else ".png")}
                  for index, role in enumerate(roles)}
        project = {"id": "catalog", "mode": mode, "audio_policy": audio, "use_embedded_video_audio": embedded,
                   "duration": 15, "orientation": "portrait", "prompt_approved": "Versioned multimodal workflow", "seed": 1, "assets": assets}
        variant = "_AUDIO_LOCK" if audio == "lock_source" else ""
        if mode == "reference":
            variant = ("_VIDEO" if "reference_video" in roles else "_IMAGE") + ("_AUDIO" if embedded or "reference_audio" in roles else "")
        identifier = "H3_" + mode.upper() + variant + "_QUALITY14"
        graph, _ = workflows.build_workflow(project, "proof", studio / "workflows")
        template = workflows.quality_profile(project)
        contract.bind_graph_assets(graph, project)
        contract.check_quality_controls(graph, project)
        weights = [node["inputs"][field] for node in graph.values() for field in ("unet_name", "clip_name", "vae_name") if field in node["inputs"]]
        missing = [filename for filename in weights if filename not in available]
        if missing:
            blocked.append({"profile_id": identifier, "mode": mode, "qualified": False, "reason": "required_weight_not_registered", "missing_weights": missing})
            continue
        if any(len(available[filename].get("sha256", "")) != 64 for filename in weights):
            raise ValueError("weights must have an inspected SHA256 manifest")
        loaders = {"LoadImage": "image", "LoadVideo": "file", "LoadAudio": "audio"}
        bound = {role: next(node_id for node_id, node in graph.items() if node["class_type"] in loaders
                           and node["inputs"][loaders[node["class_type"]]] == asset["comfy_name"]) for role, asset in assets.items()}
        sampler = [node_id for node_id, node in graph.items() if node["class_type"] == "SamplerCustomAdvanced"]
        if len(sampler) != 1:
            raise ValueError("exactly one full-model sampler is required")
        raw = (json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        filename = "multimodal/" + identifier + ".json"
        files[filename] = raw
        profiles.append({"profile_id": identifier, "mode": mode, "family": "REF2VA" if mode in {"reference", "hybrid"} else "FL2VA",
            "audio_policy": audio, "version": hashlib.sha256((studio / "workflows" / template).read_bytes()).hexdigest(),
            "template": filename, "template_sha256": hashlib.sha256(raw).hexdigest(), "structure_sha256": catalog.structure(graph),
            "sampling": {"steps": 14, "sampler_node": sampler[0]}, "asset_roles": bound,
            "weights": [{"filename": name, "sha256": available[name]["sha256"]} for name in weights],
            "runtime_source": {"plugin_directory": ".", "plugin_python_files": {}, "source_sha256": {}},
            "runtime_version_required": backend["runtime_version"], "qualification": "not_validated", "parallel_validated": False})
    files["multimodal-profiles.json"] = (json.dumps({"schema_version": 1, "profiles": profiles, "unavailable_profiles": blocked}, indent=2) + "\n").encode()
    return files


if __name__ == "__main__":
    studio, backend_file, destination = map(Path, sys.argv[1:])
    if destination.exists():
        raise SystemExit("catalog destination must be new")
    files = build(studio, json.loads(backend_file.read_text()))
    destination.mkdir(parents=True)
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(json.dumps({"destination": str(destination), "files": len(files), "deployed": False, "qualified": False}))
