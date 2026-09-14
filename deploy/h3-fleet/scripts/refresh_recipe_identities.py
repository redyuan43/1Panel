"""Rebind only verified process identities before Fleet starts; never resubmit jobs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipe_dispatch import backend_identity


def identify(backend, pid, *, proc_root=Path("/proc"), verify=backend_identity):
    result = copy.deepcopy(backend)
    if type(pid) is not int or pid <= 0:
        raise ValueError("backend service is not running")
    process = proc_root / str(pid)
    command = (process / "cmdline").read_bytes()
    expected = ("\0".join(backend["argv"]) + "\0").encode()
    if command != expected or hashlib.sha256(command).hexdigest() != backend["cmdline_sha256"]:
        raise ValueError("backend launch parameters changed")
    result["pid"] = pid
    result["start_ticks"] = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
    if Path(os.readlink(process / "cwd")).resolve() != Path(backend["runtime_root"]).resolve():
        raise ValueError("backend runtime working directory changed")
    verify(result)
    return result


def refresh(template, output, audit):
    policy = json.loads(template.read_bytes())
    before = json.loads(output.read_bytes()) if output.exists() else None
    for index, backend in enumerate(policy["backends"]):
        pid = int(subprocess.check_output(["systemctl", "show", backend["unit"], "-p", "MainPID", "--value"], text=True).strip())
        policy["backends"][index] = identify(backend, pid)
    entry = {"timestamp": time.time(), "event": "prestart_identity_rebind", "template": str(template),
             "template_sha256": hashlib.sha256(template.read_bytes()).hexdigest(),
             "before": [{key: item.get(key) for key in ("id", "pid", "start_ticks")} for item in (before or {}).get("backends", [])],
             "after": [{key: item[key] for key in ("id", "pid", "start_ticks", "gpu_uuid", "runtime_version")}
                       for item in policy["backends"]], "existing_job_bindings_modified": False}
    with audit.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary = output.with_name(output.name + "." + str(os.getpid()))
    with temporary.open("x") as handle:
        json.dump(policy, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return entry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("template", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(refresh(args.template, args.output, args.audit)))
