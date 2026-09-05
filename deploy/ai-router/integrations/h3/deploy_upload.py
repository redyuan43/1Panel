"""Upgrade only the installed H3 extension, preserving jobs and adjacent services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import deploy_edge as d


def queue():
    with d.OPENER.open("http://127.0.0.1:8188/queue", timeout=15) as response:
        value = json.load(response)
    return {name: [str(item[1]) for item in value.get(name, [])]
            for name in ("queue_running", "queue_pending")}


def snapshot(database):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        projects = d.read_projects(db)
        return {"activity": d.activity(db), "queue": queue(),
                "projects_sha256": hashlib.sha256(json.dumps(projects, sort_keys=True).encode()).hexdigest(),
                "outputs": {item["id"]: {
                    key: {"sha256": d.digest(Path(value["path"])),
                          "bytes": Path(value["path"]).stat().st_size}
                    for key, value in item.get("router_outputs", {}).items() if value.get("path")}
                    for item in projects},
                "service": d.service_info(),
                "neighbors": {name: d.service_info(name) for name in d.NEIGHBORS},
                "head": d.command("git", "-C", str(d.ROOT), "rev-parse", "HEAD"),
                "dirty": d.command("git", "-C", str(d.ROOT), "status", "--short"),
                "main_sha256": d.digest(d.ROOT / "app/main.py"),
                "extension_sha256": d.digest(d.ROOT / "app/router_contract.py")}


def require_idle(value):
    if value["activity"]["projects"] or value["activity"]["batches"] or any(value["queue"].values()):
        raise RuntimeError("Active H3/Comfy work detected; no reload is permitted.")


def health(key, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = d.request("/api/router/options", key=key)
            if status == 200:
                return body
        except (OSError, TimeoutError):
            pass
        if d.service_info()["SubState"] == "auto-restart":
            raise RuntimeError("H3 entered auto-restart after upload-hook deployment.")
        time.sleep(1)
    raise RuntimeError("H3 did not become healthy before the recovery deadline.")


def verify_options(key):
    options = health(key)
    required = {
        "cloud_upload_metadata_clean": "upload-cleaning",
        "stage_heartbeat": "stage heartbeat",
        "local_768_gpu_exclusive": "local GPU exclusivity guard",
    }
    for field, label in required.items():
        if options.get(field) is not True:
            raise RuntimeError(f"The live H3 {label} capability is absent.")
    status, _ = d.request("/api/router/options")
    if status != 401:
        raise RuntimeError("H3 options authentication regressed.")
    return options, status


def deploy(payload):
    database = d.ROOT / "data/studio.sqlite3"
    extension = d.ROOT / "app/router_contract.py"
    target_extension_sha256 = hashlib.sha256(payload["extension"].encode()).hexdigest()
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "scope": "H3 extension only", "billable_calls": 0, "generation_started": False}
    report["before"] = before = snapshot(database)
    require_idle(before)
    if (before["head"] != payload["expected_head"]
            or before["main_sha256"] != payload["expected_main_sha256"]
            or set(before["dirty"].splitlines()) != {"M app/main.py", "?? app/router_contract.py"}):
        raise RuntimeError("Installed H3 source differs from the reviewed deployment baseline.")
    if before["extension_sha256"] not in {
        payload["expected_extension_sha256"], target_extension_sha256
    }:
        raise RuntimeError("Installed H3 extension differs from the reviewed deployment baseline.")
    env = d.environment()
    key = env.get("H3_ROUTER_KEY")
    if not key:
        raise RuntimeError("The existing private H3 Router key is unavailable.")
    if before["extension_sha256"] == target_extension_sha256:
        _, unauthenticated_status = verify_options(key)
        report["checks"] = [
            {"path": "/api/router/options", "authenticated": False,
             "status": unauthenticated_status},
            {"path": "/api/router/options", "authenticated": True, "status": 200,
             "cloud_upload_metadata_clean": True, "stage_heartbeat": True,
             "local_768_gpu_exclusive": True},
        ]
        report["after"] = after = snapshot(database)
        require_idle(after)
        report["invariants"] = {
            "projects_unchanged": before["projects_sha256"] == after["projects_sha256"],
            "original_outputs_unchanged": before["outputs"] == after["outputs"],
            "neighbors_unchanged": before["neighbors"] == after["neighbors"],
            "main_unchanged": before["main_sha256"] == after["main_sha256"],
            "extension_matches": after["extension_sha256"] == target_extension_sha256,
            "h3_pid_unchanged": before["service"]["MainPID"] == after["service"]["MainPID"],
        }
        if not all(report["invariants"].values()):
            raise RuntimeError("An already-current deployment preservation invariant failed.")
        report["status"] = "deployed_upload_hook_already_current"
        report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return report
    backup = Path.home() / ".local/state/h3-router-deploy" / (
        time.strftime("%Y%m%dT%H%M%S") + "-upload-" + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    database_copy = backup / "studio-before.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(database_copy) as target:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    database_copy.chmod(0o600)
    if integrity != "ok":
        raise RuntimeError("Online SQLite backup did not pass integrity_check.")
    original = extension.read_bytes()
    d.private_write(backup / "router_contract.py.before", original)
    d.private_write(backup / "main.py.before", (d.ROOT / "app/main.py").read_bytes())
    report["backup"] = {"directory": str(backup), "database": str(database_copy),
                        "database_sha256": d.digest(database_copy), "integrity_check": integrity,
                        "method": "sqlite3.Connection.backup while H3 was running"}
    report_path = backup / "upload-deployment.json"
    d.private_write(report_path, json.dumps(report, indent=2))
    stopped = False
    try:
        d.private_write(extension, payload["extension"], mode=0o644)
        preflight = subprocess.run([
            str(d.ROOT / ".venv/bin/python"), "-c",
            "import app.main as m; from app.router_contract import Contract; "
            "assert isinstance(m.MINIMAX.regenerate_2k.__self__, Contract); "
            "assert m.MINIMAX.regenerate_2k.__self__.original_regenerate_2k.__self__ is m.MINIMAX"
        ], cwd=d.ROOT, capture_output=True, text=True, timeout=60, env=env)
        if preflight.returncode:
            d.private_write(backup / "preflight.stderr.private", preflight.stderr)
            raise RuntimeError("Real Edge app import preflight failed; private evidence preserved.")
        with sqlite3.connect(database, timeout=30) as gate:
            gate.execute("BEGIN IMMEDIATE")
            gate_state = {"activity": d.activity(gate), "queue": queue()}
            report["idle_gate"] = gate_state
            require_idle(gate_state)
            # The write lock prevents stage start from racing the idle-only stop.
            subprocess.run(["systemctl", "--user", "stop", d.SERVICE], check=True, timeout=45,
                           capture_output=True)
            stopped = True
        subprocess.run(["systemctl", "--user", "start", d.SERVICE], check=True, timeout=30,
                       capture_output=True)
        _, status = verify_options(key)
        report["checks"] = [{"path": "/api/router/options", "authenticated": False, "status": status},
                            {"path": "/api/router/options", "authenticated": True, "status": 200,
                             "cloud_upload_metadata_clean": True, "stage_heartbeat": True,
                             "local_768_gpu_exclusive": True}]
        report["after"] = after = snapshot(database)
        invariants = {
            "projects_unchanged": before["projects_sha256"] == after["projects_sha256"],
            "original_outputs_unchanged": before["outputs"] == after["outputs"],
            "neighbors_unchanged": before["neighbors"] == after["neighbors"],
            "main_unchanged": before["main_sha256"] == after["main_sha256"],
            "extension_matches": after["extension_sha256"] == target_extension_sha256,
            "h3_pid_changed": before["service"]["MainPID"] != after["service"]["MainPID"],
        }
        report["invariants"] = invariants
        if not all(invariants.values()):
            raise RuntimeError("A post-deployment preservation invariant failed.")
        require_idle(after)
        report["status"] = "deployed_upload_hook_idle_preservation_passed"
    except Exception as error:
        report["first_fatal"] = {"type": type(error).__name__, "message": str(error)}
        d.private_write(extension, original, mode=0o644)
        if stopped:
            # Only roll back while idle. Never interrupt a newly started task.
            current = snapshot(database)
            require_idle(current)
            subprocess.run(["systemctl", "--user", "stop", d.SERVICE], timeout=45, capture_output=True)
            subprocess.run(["systemctl", "--user", "start", d.SERVICE], timeout=30, check=True,
                           capture_output=True)
            health(key)
            report["restored_service"] = d.service_info()
        report["status"] = "failed_original_extension_restored"
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    d.private_write(report_path, json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    payload = {
        "expected_head": d.EXPECTED_HEAD,
        "expected_extension_sha256": "f48d563786f811a35026b44dfad6682d1e381fefc3e05feb1ff40d94ce2a5944",
        "expected_main_sha256": "be3c21801e1fc6e56aa64b7d4b0f343e9ec4e64159d4790873bd59fae30030ba",
        "extension": (here / "router_contract.py").read_text(),
    }
    remote = ("import types,sys,json; d=types.ModuleType('deploy_edge'); "
              "sys.modules['deploy_edge']=d; exec(" + repr((here / "deploy_edge.py").read_text()) + ",d.__dict__); "
              "ns={'__name__':'h3_upload_deploy'}; exec(" + repr(Path(__file__).read_text()) + ",ns); "
              "print(json.dumps(ns['deploy'](json.load(sys.stdin))))")
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "edge", "python3 -c " + shlex.quote(remote)],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError("Remote upload-hook deployment failed: " + result.stderr[-2500:])
    report = json.loads(result.stdout)
    d.private_write(args.report, json.dumps(report, indent=2))
    print(json.dumps({"status": report["status"], "report": str(args.report),
                      "before_pid": report["before"]["service"]["MainPID"],
                      "after_pid": report.get("after", report.get("restored_service", {})).get("service", {}).get("MainPID"),
                      "invariants": report.get("invariants")}))
    return 0 if report["status"].startswith("deployed_") else 1


if __name__ == "__main__":
    sys.exit(main())
