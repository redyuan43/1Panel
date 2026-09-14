"""Stage native 4060 runtimes and pin their files without starting inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


GPU = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"
PROFILES = (
    ("a4", "h3-comparison-runtime-20260909-r2", 18488, 1, ["A4"]),
    ("realism", "h3-comparison-runtime-20260909-r2", 18489, 3, ["A4_C0", "A4_C1"]),
    ("b8", "h3-comparison-runtime-B-20260910", 18490, 6, ["B8"]),
)
EXCLUDED = {".git", "__pycache__", ".pytest_cache", "node_modules", ".cache", "models",
            "output", "user", "input", "temp", "tmp", "venv", ".venv", "env"}
ANYWHERE_EXCLUDED = {".git", "__pycache__", ".pytest_cache", "node_modules", ".cache"}


def executable_path(relative):
    return relative.parts[0] not in EXCLUDED and not ANYWHERE_EXCLUDED.intersection(relative.parts)


def copy_ignore(source):
    def ignored(directory, names):
        excluded = EXCLUDED if Path(directory) == source else ANYWHERE_EXCLUDED
        return [name for name in names if name in excluded or name.endswith(".pyc")]
    return ignored


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, document):
    with path.open("x") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def prepare(root, catalog_path, resume=False):
    root = root.resolve()
    if root.exists() and (not resume or (root / "runtime-stage.json").exists()):
        raise RuntimeError("release directory already exists; inspect instead of overwriting")
    aggregate = Path("/sys/fs/cgroup/h3.slice/h3-compute.slice")
    if int((aggregate / "memory.high").read_text()) != 72 * 1024**3:
        raise RuntimeError("72 GiB aggregate protection mismatch")
    if shutil.disk_usage(root.parent).free < 42 * 1024**3:
        raise RuntimeError("insufficient staging disk headroom")
    root.mkdir(mode=0o700, exist_ok=resume)
    catalog = json.loads(catalog_path.read_bytes())
    entries = {entry["recipe_id"]: entry for entry in catalog["recipes"]}
    base = Path("/mnt/ivan-ext4-offload")
    weights_cache = {}
    backends = []
    for name, source_name, port, reserve, recipe_ids in PROFILES:
        saved = root / (name + "-stage.json")
        if resume and saved.is_file():
            backend = json.loads(saved.read_bytes())
            for item in backend["runtime_files"]:
                if checksum(Path(item["path"])) != item["sha256"]:
                    raise RuntimeError("staged runtime changed")
            for item in backend["weight_files"]:
                metadata = Path(item["path"]).stat()
                if max(metadata.st_ctime_ns, metadata.st_mtime_ns) > saved.stat().st_mtime_ns:
                    raise RuntimeError("weight changed since completed hash")
                weights_cache[item["path"]] = item["sha256"]
            backends.append(backend)
            continue
        source = base / source_name
        target = root / ("runtime-" + name)
        if not target.exists():
            shutil.copytree(source, target, symlinks=True, ignore=copy_ignore(source))
        for path in target.rglob("*.py"):
            relative = path.relative_to(target)
            if executable_path(relative) and checksum(path) != checksum(source / relative):
                raise RuntimeError("staged runtime differs from native source")
        runtime_files = [{"path": str(path), "sha256": checksum(path)}
                         for path in sorted(target.rglob("*.py"))
                         if executable_path(path.relative_to(target))]
        runtime_files.append({"path": str(target / "extra_model_paths.yaml"),
                              "sha256": checksum(target / "extra_model_paths.yaml")})
        runtime_version = hashlib.sha256(json.dumps(runtime_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        mappings = json.loads((source / "extra_model_paths.yaml").read_bytes())
        bases = [Path(value["base_path"]) for value in mappings.values()]
        required = {item["filename"]: item for recipe_id in recipe_ids
                    for item in [*entries[recipe_id]["weights"], *entries[recipe_id].get("metadata_files", [])]}
        weights = []
        for filename, item in required.items():
            candidates = []
            for directory in bases:
                candidates.append(directory / filename)
                if "/" in filename:
                    candidates.append(directory / filename)
                else:
                    for category in ("diffusion_models", "text_encoders", "vae", "loras"):
                        candidates.append(directory / category / filename)
            existing = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
            if len(existing) != 1:
                raise RuntimeError("weight resolution ambiguous/missing: " + filename)
            path = existing[0]
            if str(path) not in weights_cache:
                weights_cache[str(path)] = checksum(path)
                print(json.dumps({"hashed": filename, "bytes": path.stat().st_size}), flush=True)
            actual = weights_cache[str(path)]
            if item.get("sha256") and actual != item["sha256"]:
                raise RuntimeError("catalog weight hash mismatch: " + filename)
            weights.append({"filename": filename, "path": str(path), "sha256": actual})
        state = root / ("state-" + name)
        for kind in ("input", "output", "temp", "user"):
            (state / kind).mkdir(parents=True, mode=0o700, exist_ok=resume)
        argv = ["/usr/bin/python3", str(target / "main.py"), "--listen", "127.0.0.1", "--port", str(port),
                "--reserve-vram", str(reserve), "--disable-pinned-memory", "--disable-auto-launch", "--disable-api-nodes"]
        for kind in ("input", "output", "temp", "user"):
            argv.extend(["--" + kind + "-directory", str(state / kind)])
        backend = {"id": "single-" + name, "lane_id": "fast", "gpu_uuid": GPU,
                   "url": "http://127.0.0.1:" + str(port), "runtime_root": str(target),
                   "runtime_files": runtime_files, "runtime_version": runtime_version,
                   "weight_files": weights, "argv": argv, "recipe_ids": recipe_ids,
                   "source_runtime_root": str(source), "unit": "h3-single-" + name + ".service"}
        backends.append(backend)
        write_json(root / (name + "-stage.json"), backend)
    result = {"prepared_at": time.time(), "inference_started": False, "backends": backends}
    write_json(root / "runtime-stage.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    result = prepare(arguments.root, arguments.catalog, arguments.resume)
    print(json.dumps({"prepared_backends": [backend["id"] for backend in result["backends"]], "inference_started": False}))
