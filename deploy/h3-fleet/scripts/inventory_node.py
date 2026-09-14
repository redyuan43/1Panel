#!/usr/bin/env python3
"""Read-only inventory; never imports torch or emits credentials."""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return result.stdout.strip() if result.returncode == 0 else None


def inventory():
    memory = dict((line.split(":")[0], int(line.split()[1]) * 1024)
                  for line in Path("/proc/meminfo").read_text().splitlines()
                  if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:")))
    value = {"schema_version": 1, "observed_at": time.time(), "hostname": platform.node(),
             "architecture": platform.machine(), "python": platform.python_version(), "memory": memory,
             "gpus": [], "weights": [], "runtimes": [], "packages": [], "hash_verified_now": False}
    gpu = command("nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits")
    for line in (gpu or "").splitlines():
        uuid, name, total, used, driver = [field.strip() for field in line.split(",")]
        value["gpus"].append({"uuid": uuid, "name": name, "vram_total_bytes": int(total) * 1024**2,
                              "vram_used_bytes": int(used) * 1024**2, "driver": driver})
    pid = command("systemctl", "show", "h3-fleet.service", "--value", "-p", "MainPID")
    env = {}
    if pid and pid != "0":
        env = dict(item.split("=", 1) for item in Path("/proc", pid, "environ").read_text().split("\0") if "=" in item)
    value["fleet_running"] = bool(env)
    if env.get("H3_CAPACITY_POLICY"):
        value["capacity_policy"] = json.loads(Path(env["H3_CAPACITY_POLICY"]).read_text())
    if env:
        executable = Path("/proc", pid, "cmdline").read_bytes().split(b"\0")[0].decode()
        interpreter = Path(executable).parent / "python"
        if interpreter.is_file():
            packages = command(str(interpreter), "-c", "import importlib.metadata as m,json; print(json.dumps([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()]))")
            value["fleet_packages"] = json.loads(packages) if packages else []
    value["fleet_source"] = command("systemctl", "show", "h3-fleet.service", "--value", "-p", "WorkingDirectory")
    weights = {}
    policy_path = env.get("H3_RECIPE_POLICY")
    requirements = set()
    if policy_path:
        policy = json.loads(Path(policy_path).read_text())
        for backend in policy.get("backends", []):
            runtime = Path(backend["runtime_root"])
            value["runtimes"].append({key: backend[key] for key in ("id", "runtime_root", "runtime_version", "gpu_uuid", "argv", "recipe_ids") if key in backend})
            for item in backend.get("weight_files", []):
                path = Path(item["path"]).resolve(strict=True)
                stat = path.stat()
                weights[str(path)] = {"source": str(path), "filename": item["filename"], "sha256": item["sha256"],
                                      "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            files = [runtime / "requirements.txt", *runtime.glob("custom_nodes/*/requirements.txt")]
            for path in files:
                if path.exists():
                    requirements.update(line.strip() for line in path.read_text().splitlines()
                                        if line.strip() and not line.lstrip().startswith("#"))
    value["weights"] = list(weights.values())
    value["model_bytes"] = sum(item["size"] for item in weights.values())
    # Resolve only the H3 dependency closure, not unrelated user Python packages.
    try:
        from packaging.requirements import Requirement
        queue = list(requirements | {"triton"})
        collected, missing = {}, set()
        while queue:
            raw = queue.pop()
            req = Requirement(raw)
            name = re.sub(r"[-_.]+", "-", req.name).lower()
            if req.marker and not req.marker.evaluate():
                continue
            if name in collected or name in missing:
                continue
            try:
                dist = metadata.distribution(req.name)
            except metadata.PackageNotFoundError:
                missing.add(name)
                continue
            collected[name] = {"name": name, "version": dist.version}
            for dependency in dist.requires or []:
                dep = Requirement(dependency)
                if not dep.marker or any(dep.marker.evaluate({"extra": extra}) for extra in (req.extras or {""})):
                    dep.marker = None
                    queue.append(str(dep))
        value["packages"] = sorted(collected.values(), key=lambda item: item["name"])
        value["missing_requirements"] = sorted(missing)
    except (ImportError, ValueError) as error:
        value["package_inventory_error"] = type(error).__name__
    value["storage"] = {}
    for name in ("/", "/home/ivan", "/mnt/ivan-ext4-offload"):
        if Path(name).exists():
            usage = shutil.disk_usage(name)
            value["storage"][name] = {"total": usage.total, "free": usage.free}
    return value


if __name__ == "__main__":
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
