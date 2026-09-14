"""Import existing completion reports into a serial-only release without inference."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipes import RecipeCatalog
from app.recipe_legacy_evidence import QUALIFICATION, file_sha256, validate_legacy_single


CASES = {"A4": "A4-r3", "A4_C0": "A4-working-set-v4/A4_C0",
         "A4_C1": "A4-working-set-v3/A4_C1", "B8": "B8-r3"}


def pinned(path):
    path = path.resolve(strict=True)
    return {"path": str(path), "sha256": file_sha256(path)}


def write(path, document):
    with path.open("x") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def configure(root, evidence, output=None, stage_path=None):
    output = output or root
    output.mkdir(exist_ok=True)
    catalog = RecipeCatalog()
    stage = json.loads((stage_path or root / "runtime-stage.json").read_bytes())
    receipts = {}
    for backend in stage["backends"]:
        backend["cmdline_sha256"] = hashlib.sha256(("\0".join(backend["argv"]) + "\0").encode()).hexdigest()
        backend["cgroup_path"] = "/sys/fs/cgroup/h3.slice/h3-compute.slice/" + backend["unit"]
        backend["recipes"] = {}
        for recipe_id in backend["recipe_ids"]:
            recipe = catalog.get(recipe_id)
            folder = evidence / CASES[recipe_id]
            source = folder.parent / "report.json" if recipe_id in {"A4_C0", "A4_C1"} else folder / "report.json"
            report = json.loads(source.read_bytes())
            task = next(task for task in report["tasks"] if task["case"] == recipe_id) if "tasks" in report else report
            artifact = folder / "video.mp4"
            if not artifact.is_file():
                artifact = Path(task["artifact"])
            document = {"qualification": QUALIFICATION, "verification_scope": "historical_report_reused",
                        "recipe_id": recipe_id, "recipe_version": recipe["version"], "recipe_digest": recipe["recipe_digest"],
                        "expected_runtime_version": backend["runtime_version"], "gpu_uuid": backend["gpu_uuid"],
                        "single_task_only": True, "parallel_validated": False, "ws_validated": False,
                        "upstream_prompt_id": task["prompt_id"], "source_report": pinned(source),
                        "workflow": pinned(folder / "workflow.json"), "history": pinned(folder / "history.json"),
                        "media_probe": pinned(folder / "media-probe.json"), "artifact": pinned(artifact)}
            receipts[recipe_id] = validate_legacy_single(document, backend, catalog)
            destination = output / ("historical-" + recipe_id + ".json")
            write(destination, document)
            backend["recipes"][recipe_id] = {"qualification": QUALIFICATION, "recipe_version": recipe["version"],
                                               "evidence": pinned(destination), "vram_budget_bytes": 14500 * 1024**2}
    policy = {"version": 1, "enabled": True, "experimental_only": False, "resource_profile": "generic",
              "reclaim_factor": 0.5, "stable_seconds": 60, "global_margin_gib": 2, "static_budget_gib": 18,
              "idle_unload_seconds": 300, "protected_wait_seconds": 900, "backends": stage["backends"],
              "qualified_combinations": [], "validation_combinations": [],
              "note": "4060 Ti serial-only. Historical completions reused; no new video tests or concurrency qualification."}
    capacity = json.loads((root / "backup/capacity.json").read_bytes())
    capacity["max_active_jobs"] = 1
    for rule in [*capacity["short"].values(), capacity["long"], capacity["studio_preview"]]:
        rule["max_parallel"] = 1
    capacity["promotion"] = "20260910 first release: all local video jobs serial; four formal recipes only on original 4060 Ti. Prior evidence retained."
    write(output / "recipe-policy-template.json", policy)
    write(output / "capacity-single.json", capacity)
    write(output / "historical-import-receipts.json", receipts)
    return receipts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    receipts = configure(args.root, args.evidence, args.output, args.stage)
    print(json.dumps({"historical_recipes_imported": list(receipts), "new_inference_runs": 0, "new_full_decodes": 0}))
