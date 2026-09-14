#!/usr/bin/env python3
"""Generate transfer manifests, locked environments and inactive node units."""
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path


GIB = 1024**3
GPU_MAIN = "GPU-08c21842-c266-7f7d-6e5d-d494d4c20c4f"
GPU_SECOND = "GPU-b9ca94d5-6180-2d81-bb33-5ad04722f492"
GPU_FAST = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"
PROFILES = {"single-a4": ("a4", 1, ["A4"]), "single-realism": ("realism", 3, ["A4_C0", "A4_C1"]),
            "single-b8": ("b8", 6, ["B8"])}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def weight_target(item):
    source = Path(item["source"])
    for category in ("diffusion_models", "text_encoders", "vae", "loras"):
        if category in source.parts:
            return Path(*source.parts[source.parts.index(category):])
    if item["filename"] == "MODEL_MANIFEST.json":
        return Path("MODEL_MANIFEST.json")
    raise ValueError("unknown weight category: " + str(source))


def locked(packages):
    lines = []
    for package in sorted(packages, key=lambda p: p["name"].lower()):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", package["name"]) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", package["version"]):
            raise ValueError("invalid installed distribution")
        if package["name"] != "pip":
            lines.append(package["name"] + "==" + package["version"])
    return "\n".join(lines) + "\n"


def prepare(source, targets, output):
    if output.exists():
        raise ValueError("bundle already exists")
    if source.get("missing_requirements") or not source.get("fleet_packages"):
        raise ValueError("source dependency inventory incomplete")
    output.mkdir(parents=True, mode=0o700)
    weights, names = [], set()
    for item in source["weights"]:
        relative = weight_target(item)
        if (relative.is_absolute() or ".." in relative.parts or str(relative) in names
                or any(c in str(relative) for c in "\n\r\\")
                or not re.fullmatch("[0-9a-f]{64}", item["sha256"])):
            raise ValueError("ambiguous or unpinned model destination")
        names.add(str(relative))
        weights.append({**item, "relative_target": str(relative)})
    write_json(output / "models.json", weights)
    (output / "models.sha256").write_text("".join(item["sha256"] + "  " + item["relative_target"] + "\n" for item in weights))
    (output / "comfy-requirements.lock").write_text(locked(source["packages"]))
    (output / "fleet-requirements.lock").write_text(locked(source["fleet_packages"]))
    # Run this script on the source host after the destination tree is prepared.
    commands = ["#!/usr/bin/env bash", "set -euo pipefail", "# Run on Ivan. Does not delete files, install packages, or start services.",
                "destination=ivan@192.168.100.137", "root=/home/ivan/h3"]
    parents = sorted({"/home/ivan/h3/models/" + str(Path(item["relative_target"]).parent) for item in weights})
    commands.append('ssh "$destination" ' + shlex.quote("mkdir -p -- " + " ".join(shlex.quote(p) for p in parents)))
    for item in weights:
        commands.append("rsync -a --partial --protect-args --info=progress2 -- " + shlex.quote(item["source"]) +
                        ' "$destination":' + shlex.quote("/home/ivan/h3/models/" + item["relative_target"]))
    # Exclude only root weight/state trees; comfy/ldm/models is executable source.
    for runtime in source["runtimes"]:
        name = PROFILES[runtime["id"]][0]
        remote = "/home/ivan/h3/runtimes/runtime-" + name
        commands.append('ssh "$destination" ' + shlex.quote("mkdir -p -- " + shlex.quote(remote)))
        excludes = ["/models/", "/input/", "/output/", "/temp/", "/user/", "/venv/", "/.venv/", ".git/", "__pycache__/", "*.pyc"]
        commands.append("rsync -a --partial --protect-args " + " ".join("--exclude=" + shlex.quote(x) for x in excludes) +
                        " -- " + shlex.quote(runtime["runtime_root"] + "/") + ' "$destination:' + remote + '/"')
    commands.append('# After transfer, verify on Ivan-u24: cd /home/ivan/h3/models && sha256sum --check /path/to/models.sha256')
    (output / "copy-to-ivan-u24.sh").write_text("\n".join(commands) + "\n")
    reports = {}
    for node_id, inventory in targets.items():
        node_dir = output / node_id
        node_dir.mkdir()
        total = inventory["memory"]["MemTotal"]
        limit = min(72 * GIB, total - 16 * GIB)
        if limit <= 0:
            raise ValueError("host cannot preserve RAM reserve")
        expected = [GPU_MAIN, GPU_SECOND] if node_id == "ivan" else [GPU_FAST]
        if not set(expected) <= {gpu["uuid"] for gpu in inventory["gpus"]}:
            raise ValueError("physical GPU inventory mismatch")
        root = "/mnt/ivan-ext4-offload/h3-multinode" if node_id == "ivan" else "/home/ivan/h3"
        addresses = {"ivan": "100.96.79.21", "ivan-u24": "100.120.143.109"}
        lanes = []
        worker_specs = []
        for lane_index, gpu in enumerate(expected):
            lane_id = "main" if node_id == "ivan" and lane_index == 0 else "preview" if node_id == "ivan" else "fast"
            lanes.append({"id": lane_id, "url": "http://127.0.0.1:" + str(19488 + lane_index * 10),
                          "gpu_uuid": gpu, "device": next(g["name"] for g in inventory["gpus"] if g["uuid"] == gpu)})
            for runtime in source["runtimes"]:
                profile, reserve, recipes = PROFILES[runtime["id"]]
                identity = lane_id + "-" + profile
                port = 19488 + lane_index * 10 + list(PROFILES).index(runtime["id"])
                runtime_root = runtime["runtime_root"] if node_id == "ivan" else root + "/runtimes/runtime-" + profile
                python = "/usr/bin/python3" if node_id == "ivan" else root + "/comfy-venv/bin/python"
                state = root + "/state/" + identity
                argv = [python, runtime_root + "/main.py", "--listen", "127.0.0.1", "--port", str(port),
                        "--reserve-vram", str(reserve), "--disable-pinned-memory", "--disable-auto-launch", "--disable-api-nodes",
                        "--input-directory", state + "/input", "--output-directory", state + "/output",
                        "--temp-directory", state + "/temp", "--user-directory", state + "/user"]
                if node_id == "ivan":
                    argv.append("--lowvram")
                unit = ("[Unit]\nDescription=H3 " + node_id + " " + identity + " (qualification required)\nAfter=network-online.target\n\n"
                        "[Service]\nUser=ivan\nGroup=ivan\nSlice=h3-compute.slice\nWorkingDirectory=" + runtime_root + "\n"
                        "Environment=CUDA_VISIBLE_DEVICES=" + gpu + "\nEnvironment=PYTHONUNBUFFERED=1\n"
                        "Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True\nExecStart=" + " ".join(argv) + "\n"
                        "Restart=no\nKillSignal=SIGINT\nTimeoutStopSec=60\nMemoryMax=" + str(limit) + "\nMemorySwapMax=8G\nOOMPolicy=stop\n")
                (node_dir / ("h3-node-" + identity + ".service")).write_text(unit)
                worker_specs.append({"id": identity, "lane_id": lane_id, "gpu_uuid": gpu,
                    "url": "http://127.0.0.1:" + str(port), "runtime_root": runtime_root,
                    "input_root": state + "/input", "argv": argv, "recipe_ids": recipes,
                    "unit": "h3-node-" + identity + ".service", "qualification": "not_validated"})
        (node_dir / "h3-compute.slice").write_text("[Unit]\nDescription=H3 host memory guard\n\n[Slice]\nMemoryHigh=" + str(limit) + "\nMemoryMax=" + str(limit) + "\nMemorySwapMax=8G\n")
        environment = {"H3_FLEET_LISTEN": addresses[node_id], "H3_FLEET_PORT": "8789",
                       "H3_OFFLOAD_ROOT": root, "H3_FLEET_DATABASE": root + "/fleet.sqlite3",
                       "H3_CAPACITY_POLICY": root + "/config/capacity.json", "H3_RECIPE_POLICY": root + "/config/recipe-policy.json",
                       "H3_FLEET_LANES": json.dumps(lanes, separators=(",", ":"))}
        (node_dir / "fleet.env").write_text("\n".join(k + "=" + ("'" + v + "'" if k == "H3_FLEET_LANES" else v)
                                                      for k, v in environment.items()) + "\n")
        (node_dir / "h3-fleet.service").write_text("[Unit]\nDescription=H3 " + node_id + " Fleet\nAfter=network-online.target\n\n"
            "[Service]\nUser=ivan\nGroup=ivan\nWorkingDirectory=" + root + "/fleet\nEnvironmentFile=" + root + "/config/fleet.env\n"
            "EnvironmentFile=" + root + "/private/fleet-auth.env\nExecStart=" + root + "/fleet-venv/bin/python -m uvicorn app.main:app --host " +
            addresses[node_id] + " --port 8789\nRestart=on-failure\nMemoryMax=2G\n")
        policy = {"version": 1, "enabled": False, "experimental_only": False, "backends": [], "qualified_combinations": [],
                  "validation_combinations": [], "static_budget_gib": 18, "stable_seconds": 60, "global_margin_gib": 2,
                  "note": "No GPU or runtime qualifications are inherited from another host. Bind and validate before enabling."}
        write_json(node_dir / "recipe-policy.json", policy)
        capacity = json.loads(json.dumps(source["capacity_policy"]))
        capacity["max_active_jobs"] = 1
        for section in (capacity["short"]["preview"], capacity["short"]["quality"], capacity["long"], capacity.get("studio_preview", {})):
            section["max_parallel"] = 1
        capacity["resources"]["max_cgroup_gib"] = limit / GIB
        capacity["promotion"] = "Node qualification pending. No historical concurrency promotion."
        write_json(node_dir / "capacity.json", capacity)
        write_json(node_dir / "worker-specs.json", worker_specs)
        write_json(node_dir / "extra_model_paths.yaml", {"node_models": {"base_path": root + "/models",
            "diffusion_models": "diffusion_models", "text_encoders": "text_encoders", "vae": "vae", "loras": "loras"}})
        available = inventory["memory"]["MemAvailable"]
        root_free = inventory["storage"]["/"]["free"]
        reasons = []
        if root_free < 25 * GIB:
            reasons.append("root_disk_headroom")
        if available - 18 * GIB - 2 * GIB < 16 * GIB:
            reasons.append("ram_headroom")
        reports[node_id] = {"physical_memory_bytes": total, "cgroup_limit_bytes": limit,
            "available_bytes": available, "root_free_bytes": root_free, "resource_preflight_reasons": reasons,
            "inference_admission": "wait", "qualification": "not_validated", "gpu_inference_started": False,
            "max_parallel_target": len(expected), "active_policy_max_parallel": 1,
            "second_lane_policy": "qualified_A4_pair_and_60s_owned_sampler_progress" if node_id == "ivan" else None}
    write_json(output / "resource-dry-run.json", reports)
    write_json(output / "migration.json", {"model_bytes": sum(w["size"] for w in weights),
        "source_host": "ivan", "destination_host": "ivan-u24", "destination_root": "/home/ivan/h3",
        "source_hashes_from_pinned_policy": True, "hash_verified_now": False, "copied": False,
        "remaining_steps": ["transfer", "verify_target_hashes", "recreate_isolated_environments", "bind_runtime_identity",
                            "review_activation", "single_gpu_acceptance", "A4_pair_acceptance", "three_job_acceptance"]})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ivan-u24", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.source.read_text())
    print(json.dumps(prepare(source, {"ivan": source, "ivan-u24": json.loads(args.ivan_u24.read_text())}, args.output), indent=2))


if __name__ == "__main__":
    main()
