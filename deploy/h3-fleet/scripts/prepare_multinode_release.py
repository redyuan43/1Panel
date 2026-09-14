#!/usr/bin/env python3
"""Build a reviewable candidate by merging only this change into live sources."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path


EXCLUDED = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "data", "node_modules"}
SOURCE_DIRS = {"app", "scripts", "frontend", "config", "multimodal", "workflows", "deploy", "systemd"}
GENERATED_MANIFEST = "multinode-manifest.json"


def source_file(path, root):
    relative = path.relative_to(root)
    return (path.is_file() and not EXCLUDED.intersection(relative.parts) and not path.is_symlink()
            and (len(relative.parts) == 1 or relative.parts[0] in SOURCE_DIRS)
            and path.suffix not in {".pyc", ".sqlite3", ".db", ".key"}
            and not path.name.endswith(".env") and path.name not in {".env", GENERATED_MANIFEST})


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapt_live_connector(output):
    """The deployed connector is newer than the checkout; preserve its full body."""
    source = output / "app/connector_api.py"
    if not source.exists():
        return []
    changes = []
    original = checksum(source)
    value = source.read_text()
    legacy_options = 'options = {"new_seed": False}'
    if value.count(legacy_options) == 1:
        value = value.replace(legacy_options,
            'options = {"new_seed": False, "target_node": arguments.get("target_node", preview.get("target_node", "auto"))}')
    elif '"target_node": arguments.get("target_node",' not in value:
        raise ValueError("connector target forwarding requires review")
    old_enum = '"enum": ["auto", "ivan", "ivan-u24"]'
    new_enum = '"enum": ["auto", "ivan", "ivan-u24", "edge"]'
    if old_enum in value:
        value = value.replace(old_enum, new_enum)
    elif new_enum not in value:
        anchor = '"expected_run_id": RUN_ID}'
        if value.count(anchor) != 1:
            raise ValueError("connector target schema requires review")
        value = value.replace(anchor, '"expected_run_id": RUN_ID, "target_node": {"type": "string", ' + new_enum + '}}')
    if value != source.read_text():
        source.write_text(value)
        changes.append({"path": "app/connector_api.py", "adaptation": "optional_node_selection",
                        "before": original, "candidate": checksum(source)})
    frontend = output / "frontend/app.js"
    value = frontend.read_text()
    before = 'expected_run_id: state.project.stages.preview.run_id || null}'
    after = 'expected_run_id: state.project.stages.preview.run_id || null, target_node: selectedTargetNode(stageId)}'
    if before in value:
        if value.count(before) != 1:
            raise ValueError("connector frontend adaptation requires review")
        original = checksum(frontend)
        frontend.write_text(value.replace(before, after))
        changes.append({"path": "frontend/app.js", "adaptation": "optional_node_selection",
                        "before": original, "candidate": checksum(frontend)})
    elif 'target_node: selectedTargetNode(stageId)' not in value:
        raise ValueError("connector frontend target forwarding requires review")
    # The same audited schema-only generator serves Studio and Control. Never
    # overwrite packaged JSON independently of the runtime authority again.
    helper_path = Path(__file__).resolve().parents[2] / "h3-mcp/scripts/prepare_release.py"
    spec = importlib.util.spec_from_file_location("h3_multinode_schema_preparer", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    schema = output / "app/connector_schema.json"
    original = checksum(schema) if schema.exists() else None
    content = helper.connector_schema(source.read_bytes())
    if not schema.exists() or schema.read_bytes() != content:
        schema.write_bytes(content)
        changes.append({"path": "app/connector_schema.json", "adaptation": "runtime_authoritative_schema",
                        "before": original, "candidate": checksum(schema)})
    return changes


def prepare(workspace, baseline, live, output):
    if output.exists():
        raise ValueError("candidate already exists; refusing overwrite")
    output.mkdir(parents=True, mode=0o700)
    changes = []
    for source in live.rglob("*"):
        if source_file(source, live):
            target = output / source.relative_to(live)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for current in sorted(workspace.rglob("*")):
        if not source_file(current, workspace):
            continue
        relative = current.relative_to(workspace)
        old, deployed, target = baseline / relative, live / relative, output / relative
        if old.is_file() and checksum(current) == checksum(old):
            continue
        if not old.exists() and deployed.exists() and checksum(current) != checksum(deployed):
            raise ValueError("new file conflicts with live source: " + str(relative))
        if old.exists() and deployed.exists():
            merged = subprocess.run(["git", "merge-file", "-p", str(deployed), str(old), str(current)], capture_output=True)
            if merged.returncode:
                raise ValueError("live source merge requires review: " + str(relative))
            content = merged.stdout
        elif old.exists() and not deployed.exists():
            raise ValueError("changed source missing from live release: " + str(relative))
        else:
            content = current.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        changes.append({"path": str(relative), "before": checksum(old) if old.exists() else None,
                        "live": checksum(deployed) if deployed.exists() else None, "candidate": checksum(target)})
    adaptations = adapt_live_connector(output) if (output / "app/multifleet.py").exists() else []
    if (workspace / "tests").is_dir():
        shutil.copytree(workspace / "tests", output / "tests",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    manifest = {str(p.relative_to(output)): checksum(p) for p in sorted(output.rglob("*")) if p.is_file()}
    report = {"schema_version": 1, "workspace": str(workspace), "baseline": str(baseline),
              "live": str(live), "changes": changes, "adaptations": adaptations, "files": manifest,
              "deployed": False, "gpu_validated": False}
    (output / GENERATED_MANIFEST).write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "baseline", "live", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(**{key: value.resolve() for key, value in vars(args).items()})
    print(json.dumps({"changed_files": len(report["changes"]), "candidate_files": len(report["files"]),
                      "output": str(args.output), "deployed": False}))


if __name__ == "__main__":
    main()
