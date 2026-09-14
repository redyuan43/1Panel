"""Check/import pinned historical single-task evidence without copying video.

CLI: --entry DRAFT.json --backend BACKEND.json [--catalog CATALOG.json]
     [--execute --output /persistent/new-import-directory]
Or: --runtime-stage STAGE.json --evidence-root ROOT [--recipes A4 A4_C0 A4_C1 B8]
    [--source-manifest ORIGINAL-PREPARER.json] [--historical-weights WEIGHTS.json]
    [--historical-units UNITS.json]
    [--execute --output /persistent/new-import-root]
Stage schema: backends=[{recipe_ids:[ID,...],runtime_files,weight_files,argv,
runtime_root,runtime_version,gpu_uuid,...}], prepared_at. Optional historical_units
and historical_weight_files supply original receipts. Otherwise old unit files
are read locally; known SHA values come from the pinned catalog. Only the three
catalog-null shared text/VAE files permit current staged full hash + unchanged
ordinary file stat predating execution + identical model-path configuration.
This is explicitly metadata continuity inference, not a recorded historical SHA.
Both roots are filesystem paths on the executing host; no implicit SSH/SCP.
Without --execute, only validate the supplied complete evidence; no writes or
subprocesses. Execute adds artifact_validation via CPU ffprobe/full AV decode,
then validates ALL sources before publishing evidence.json and entry.json.
The output must be new and outside source directories. This never edits Fleet
configuration, sets trial qualification, contacts workers or calls model APIs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipe_legacy_evidence import (
    QUALIFICATION, decode_command, file_sha256, persistent_path, pinned_path,
    probe_command, read_json, require, source_task, validate_history,
    stat_snapshot, validate_legacy_single, validate_probe, validate_runtime,
)
from app.recipes import RecipeCatalog


LEGACY_CASES = {"A4": "A4-r3", "A4_C0": "A4-working-set-v4/A4_C0",
                "A4_C1": "A4-working-set-v3/A4_C1", "B8": "B8-r3"}


def reference(path):
    path = persistent_path(path)
    require(path.is_file(), "missing historical evidence file: " + str(path))
    return {"path": str(path), "sha256": file_sha256(path)}


def selector(source, *pointer):
    return {"reference": source, "pointer": list(pointer)}


def build_documents(stage_path, evidence_root, catalog, recipes=None, source_manifest=None, historical_weights=None,
                    historical_units=None, unit_root="/etc/systemd/system"):
    stage_ref = reference(stage_path)
    stage = read_json(Path(stage_ref["path"]).read_bytes())
    root = persistent_path(evidence_root)
    manifest_ref = reference(source_manifest or root / "runtime-prepare-r2.json")
    if historical_units:
        unit_ref = reference(historical_units)
        units = read_json(Path(unit_ref["path"]).read_bytes())
        unit_pointer = []
    else:
        unit_ref = stage_ref
        units = stage.get("historical_units", {})
        unit_pointer = ["historical_units"]
    if historical_weights:
        weight_ref = reference(historical_weights)
        weight_records = read_json(Path(weight_ref["path"]).read_bytes())
        weight_pointer = []
    else:
        weight_ref = stage_ref
        weight_records = stage.get("historical_weight_files")
        weight_pointer = ["historical_weight_files"]
    weight_records = [] if weight_records is None else weight_records
    require(isinstance(weight_records, list), "historical_weight_files must be a list")
    weight_indices = {item["filename"]: index for index, item in enumerate(weight_records)}
    require(len(weight_indices) == len(weight_records), "duplicate historical weight source")
    require(isinstance(stage.get("backends"), list), "runtime-stage requires a backends list")
    selected_recipes = list(LEGACY_CASES) if recipes is None else recipes
    require(bool(selected_recipes) and len(selected_recipes) == len(set(selected_recipes))
            and all(recipe_id in LEGACY_CASES for recipe_id in selected_recipes), "invalid/duplicate historical recipes")
    results = []
    for recipe_id in selected_recipes:
        candidates = [backend for backend in stage["backends"] if recipe_id in backend.get("recipe_ids", backend.get("recipes", {}))]
        require(len(candidates) == 1, "need exactly one staged backend for " + recipe_id)
        backend = dict(candidates[0])
        if "argv" in backend and "cmdline_sha256" not in backend:
            backend["cmdline_sha256"] = hashlib.sha256(("\0".join(backend["argv"]) + "\0").encode()).hexdigest()
        folder = root / LEGACY_CASES[recipe_id]
        report_path = folder.parent / "report.json" if recipe_id in {"A4_C0", "A4_C1"} else folder / "report.json"
        report_ref = reference(report_path)
        report = read_json(report_path.read_bytes())
        task, identity = source_task(report, recipe_id, allow_failed=recipe_id == "A4_C1")
        unit = identity["Id"]
        if isinstance(units.get(unit, {}).get("argv"), (list, str)):
            argv_source = selector(unit_ref, *unit_pointer, unit, "argv")
        else:
            unit_file = persistent_path(unit_root) / unit
            require(not unit_file.with_name(unit + ".d").exists(), "unit has drop-ins; supply captured resolved historical argv")
            argv_source = {"reference": reference(unit_file), "format": "systemd_unit_execstart"}
        recipe = catalog.get(recipe_id)
        graph_ref = reference(folder / "workflow.json")
        graph = read_json(Path(graph_ref["path"]).read_bytes())
        binding = catalog.validate(recipe_id, graph)
        identity_pointer = ["identities", "workers", recipe_id] if "tasks" in report else ["baseline", "isolated"]
        if "tasks" in report:
            task_pointer = ["tasks", report["tasks"].index(task)]
            gpu_pointer = [*task_pointer, "gpu_uuid"]
        else:
            gpu_pointer = ["isolated_gpu_uuid"]
        required = [*recipe["weights"], *recipe.get("metadata_files", [])]
        catalog_ref = reference(catalog.path)
        catalog_data = read_json(Path(catalog_ref["path"]).read_bytes())
        recipe_index = next(index for index, item in enumerate(catalog_data["recipes"]) if item["recipe_id"] == recipe_id)
        backend_index = stage["backends"].index(candidates[0])
        weight_items = []
        for item in required:
            filename = item["filename"]
            if filename in weight_indices:
                weight_items.append({"filename": filename, "sha256_source": selector(
                    weight_ref, *weight_pointer, weight_indices[filename], "sha256")})
            elif item.get("sha256"):
                field = "metadata_files" if item in recipe.get("metadata_files", []) else "weights"
                index = next(index for index, record in enumerate(catalog_data["recipes"][recipe_index][field])
                             if record["filename"] == filename)
                weight_items.append({"filename": filename, "sha256_source": selector(
                    catalog_ref, "recipes", recipe_index, field, index, "sha256")})
            else:
                matches = [(index, record) for index, record in enumerate(backend["weight_files"]) if record["filename"] == filename]
                require(len(matches) == 1, "missing/ambiguous staged shared weight: " + filename)
                index, current = matches[0]
                require(isinstance(current.get("path"), str), "missing historical continuity current file path: " + filename)
                weight_items.append({"filename": filename, "continuity": "unchanged_file_metadata_inference",
                                     "sha256_source": selector(stage_ref, "backends", backend_index, "weight_files", index, "sha256"),
                                     "stat_snapshot": stat_snapshot(current["path"]),
                                     "model_paths": reference(Path(backend["runtime_root"]) / "extra_model_paths.yaml")})
        document = {"schema_version": 1, "qualification": QUALIFICATION, "recipe_id": recipe_id,
                    "recipe_version": binding["recipe_version"], "recipe_digest": binding["recipe_digest"],
                    "expected_runtime_version": backend["runtime_version"], "gpu_uuid": backend["gpu_uuid"],
                    "upstream_prompt_id": task["prompt_id"], "parallel_validated": False, "ws_validated": False,
                    "single_task_only": True, "source_report": report_ref, "workflow": graph_ref,
                    "history": reference(folder / "history.json"), "media_probe": reference(folder / "media-probe.json"),
                    "artifact": reference(folder / "video.mp4"),
                    "allow_completed_from_failed_batch": recipe_id == "A4_C1",
                    "runtime": {"manifest": manifest_ref,
                                "argv": argv_source,
                                "gpu_uuid": selector(report_ref, *gpu_pointer),
                                "unit": selector(report_ref, *identity_pointer, "Id"),
                                "pid": selector(report_ref, *identity_pointer, "MainPID")},
                    "weights": weight_items}
        validate_runtime(document, backend, recipe, task, identity)
        validate_history(read_json(Path(document["history"]["path"]).read_bytes()), graph, task, recipe["sampling"]["sampler_node"])
        validate_probe(read_json(Path(document["media_probe"]["path"]).read_bytes()), graph, Path(document["artifact"]["path"]).stat().st_size)
        require(document["artifact"]["sha256"] == task["artifact_sha256"], "historical video SHA mismatch")
        results.append({"recipe_id": recipe_id, "document": document, "backend": backend})
    return results


def write_pinned(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def import_evidence(entry, backend, catalog, output=None, execute=False):
    if not execute:
        return validate_legacy_single(entry, backend, catalog)
    require("evidence" not in entry, "execute requires a draft evidence document, not a scheduling entry")
    target = persistent_path(output)
    require(not target.exists(), "refusing to overwrite existing import")
    artifact = pinned_path(entry["artifact"])
    require(target not in artifact.parents and artifact.parent not in target.parents,
            "import directory must be outside original artifact directory")
    started = time.time()
    proof = {"artifact": entry["artifact"], "started_at": started}
    for name, argv in (("ffprobe", probe_command(artifact)), ("full_decode", decode_command(artifact))):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=True)
        require(result.stderr == "", "media validation emitted errors")
        proof[name] = {"argv": argv, "returncode": result.returncode, "stderr": result.stderr}
        if name == "ffprobe":
            proof[name]["probe"] = read_json(result.stdout)
    proof["finished_at"] = time.time()
    require(file_sha256(artifact) == entry["artifact"]["sha256"], "artifact changed during decode")
    target.mkdir(parents=True, exist_ok=False)
    document = dict(entry, artifact_validation=write_pinned(target / "artifact-validation.json", proof))
    receipt = validate_legacy_single(document, backend, catalog)
    evidence = write_pinned(target / "evidence.json", document)
    scheduling_entry = {"qualification": QUALIFICATION, "recipe_version": document["recipe_version"], "evidence": evidence}
    write_pinned(target / "entry.json", scheduling_entry)
    return {"entry": scheduling_entry, "receipt": receipt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--entry", type=Path)
    inputs.add_argument("--runtime-stage", type=Path)
    parser.add_argument("--backend", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--historical-weights", type=Path)
    parser.add_argument("--historical-units", type=Path)
    parser.add_argument("--unit-root", type=Path, default=Path("/etc/systemd/system"))
    parser.add_argument("--recipes", nargs="+", choices=list(LEGACY_CASES))
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.execute and args.output is None:
            raise ValueError("--execute requires --output")
        catalog = RecipeCatalog(args.catalog)
        if args.runtime_stage:
            require(args.evidence_root is not None, "--runtime-stage requires --evidence-root")
            plans = build_documents(args.runtime_stage, args.evidence_root, catalog, args.recipes,
                                    args.source_manifest, args.historical_weights, args.historical_units, args.unit_root)
            if args.execute:
                require(not persistent_path(args.output).exists(), "refusing existing batch import root")
                result = {"imports": [import_evidence(plan["document"], plan["backend"], catalog,
                                      args.output / plan["recipe_id"], True) for plan in plans]}
            else:
                result = {"status": "sources_checked_pending_full_decode", "qualified": False, "plans": plans}
        else:
            require(args.backend is not None, "--entry requires --backend")
            entry = read_json(persistent_path(args.entry).read_bytes())
            backend = read_json(persistent_path(args.backend).read_bytes())
            result = import_evidence(entry, backend, catalog, args.output, args.execute)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
