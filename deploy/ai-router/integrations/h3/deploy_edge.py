"""Explicit, idle-only H3 deployment with online backup and non-billable checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4


ROOT = Path("/home/admin/github/h3-video-studio")
SERVICE = "h3-video-studio.service"
NEIGHBORS = ("comfyui-edge.service", "qwen38-flash-next-vllm.service",
             "asr-benchmark-runner.service", "edge-external-audio-transcription.service")
EXPECTED_HEAD = "be1edf4d1eeed8762b98aba691bfe0d445db5964"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, **kwargs).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def private_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content if isinstance(content, bytes) else content.encode())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def service_info(name=SERVICE):
    raw = command("systemctl", "--user", "show", name, "-p", "MainPID",
                  "-p", "ActiveState", "-p", "SubState", "-p", "ActiveEnterTimestamp")
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def process_error(error):
    detail = getattr(error, "stderr", None) or str(error)
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    return f"{type(error).__name__}: {detail[-3000:]}"


def restore_optional(path, saved):
    if saved.is_file():
        shutil.copy2(saved, path)
    else:
        path.unlink(missing_ok=True)


def rollback_original(report, backup, env_file, dropin, *, home=None):
    errors = []
    home = home or Path.home()
    unit = home / ".config/systemd/user" / SERVICE
    try:
        command("systemctl", "--user", "stop", SERVICE, timeout=45)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        errors.append({"step": "stop_failed_deployment", "error": process_error(error)})
    try:
        shutil.copy2(backup / "main.py.before", ROOT / "app/main.py")
        (ROOT / "app/router_contract.py").unlink(missing_ok=True)
        shutil.copy2(backup / SERVICE, unit)
        restore_optional(env_file, backup / "router.env.before")
        restore_optional(dropin, backup / "90-router-contract.conf.before")
    except OSError as error:
        errors.append({"step": "restore_files", "error": process_error(error)})
    for step, args in (
        ("daemon_reload", ("systemctl", "--user", "daemon-reload")),
        ("start_original_service", ("systemctl", "--user", "start", SERVICE)),
    ):
        try:
            command(*args, timeout=45)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            errors.append({"step": step, "error": process_error(error)})
    try:
        report["after"] = {"service": service_info()}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        errors.append({"step": "inspect_original_service", "error": process_error(error)})
    if errors:
        report["rollback_errors"] = errors
    return not errors


def start_or_rollback(report, backup, env_file, dropin, *, home=None):
    try:
        command("systemctl", "--user", "start", SERVICE, timeout=45)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        report["startup_error"] = process_error(error)
        restored = rollback_original(report, backup, env_file, dropin, home=home)
        report["status"] = (
            "original_service_restored_after_start_exception"
            if restored else "original_files_restored_service_restart_failed"
        )
        return False


def environment():
    pid = service_info()["MainPID"]
    raw = Path("/proc") / pid / "environ"
    return dict(line.split("=", 1) for line in raw.read_bytes().decode().split("\0") if "=" in line)


def read_projects(db):
    return [json.loads(row[0]) for row in db.execute("SELECT data_json FROM projects")]


def activity(db):
    projects = read_projects(db)
    schedules = [json.loads(row[0]) for row in db.execute("SELECT data_json FROM batch_schedules")]
    active = []
    for project in projects:
        for stage_id, stage in project["stages"].items():
            if stage["status"] in {"queued", "running", "cancelling"}:
                active.append({"project_id": project["id"], "stage": stage_id,
                               "status": stage["status"], "run_id": stage.get("run_id")})
    batches = [{"id": item["id"], "status": item["status"],
                "current_item_id": item.get("current_item_id")}
               for item in schedules if item["status"] in {"waiting_for_gpu", "running", "pausing", "cancelling"}
               or any(part["status"] == "running" for part in item.get("items", []))]
    return {"projects": active, "batches": batches, "project_count": len(projects),
            "schedules": [{key: item.get(key) for key in ("id", "status", "next_run_at")} for item in schedules]}


def credential_availability(env):
    path = Path(env.get("MINIMAX_CREDENTIALS", "/home/admin/.config/minimax/credentials.env"))
    values = {}
    if path.is_file():
        for raw in path.read_text().splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update(env)
    names = [key for key, value in values.items() if value and ("DASHSCOPE" in key or "QWEN" in key)]
    maas = False
    for key, value in values.items():
        if not key.endswith(("BASE", "URL", "ENDPOINT")):
            continue
        try:
            host = urllib.parse.urlparse(value).hostname
            maas = maas or bool(host and host.endswith(".maas.aliyuncs.com"))
        except ValueError:
            pass
    return {"source": str(path), "context_ir_provider": "MiniMax",
            "minimax_key_available": bool(values.get("MINIMAX_API_KEY")),
            "dashscope_variable_names": names, "maas_workspace_base_available": maas,
            "qwen_env_exported": False}


def request(path, *, key=None, method="GET", body=None, form=None, raw=None):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        raw = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request("http://127.0.0.1:8789" + path, data=raw,
                                 headers=headers, method=method)
    try:
        response = OPENER.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    content = response.read()
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        value = None
    return response.code, value


def functional_checks(key, operation):
    checks = []

    def check(name, path, expected, **kwargs):
        status, body = request(path, **kwargs)
        checks.append({"name": name, "method": kwargs.get("method", "GET"),
                       "path": path, "status": status, "expected": expected})
        if status != expected:
            raise RuntimeError(f"{name}: expected HTTP {expected}, got {status}")
        return body

    check("missing authentication", "/api/router/options", 401)
    check("invalid authentication", "/api/router/options", 401, key="invalid")
    options = check("authenticated options", "/api/router/options", 200, key=key)
    if options["contract_version"] != 1 or options["stage_outputs"] != "immutable":
        raise RuntimeError("Unexpected H3 Router contract")
    form = {"operation_id": operation, "name": "Router deployment acceptance (pending, no generation)",
            "mode": "t2v", "strategy": "fast", "prompt": "A red cube on a white table.",
            "duration": 4, "seed": 20260904, "audio_policy": "native"}
    project = check("real pending project creation", "/api/router/projects", 200,
                    key=key, method="POST", form=form)
    project_id = project["id"]
    repeat = check("idempotent creation replay", "/api/router/projects", 200,
                   key=key, method="POST", form=form)
    if project_id != repeat["id"]:
        raise RuntimeError("Idempotent creation duplicated the project")
    check("idempotency conflict", "/api/router/projects", 409, key=key, method="POST",
          form={**form, "prompt": "Conflicting payload"})
    path = f"/api/router/projects/{project_id}"
    result = check("persisted pending project", path, 200, key=key)
    if not all(stage["status"] == "pending" for stage in result["pipeline"]):
        raise RuntimeError("A deployment check unexpectedly started generation")
    check("unapproved preview start rejected", path + "/stages/preview/start", 409,
          key=key, method="POST", body={"operation_id": operation + "_preview",
                                       "expected_run_id": None, "expected_output_id": "out_not_approved"})
    check("stale context approval rejected", path + "/stages/context_ir/approve", 409,
          key=key, method="POST", body={"operation_id": operation + "_approve",
                                       "expected_run_id": None, "expected_output_id": "out_not_generated",
                                       "prompt": "Not an approved output"})
    check("pending cancellation rejected", path + "/stages/context_ir/cancel", 409,
          key=key, method="POST", body={"operation_id": operation + "_cancel", "expected_run_id": None})
    check("malformed JSON rejected", path + "/stages/context_ir/start", 400,
          key=key, method="POST", raw=b"not JSON")
    check("legacy context start protected", f"/api/projects/{project_id}/context-ir", 409,
          method="POST", body={})
    escaped_id = "".join("\\u%04x" % ord(char) for char in project_id)
    check("escaped legacy batch enrollment protected", "/api/768-queue/schedules", 409,
          method="POST", raw=('{"project_ids":["' + escaped_id + '"]}').encode())
    check("unknown immutable output", path + "/outputs/out_missing", 404, key=key)
    check("legacy project remains readable", f"/api/projects/{project_id}", 200)
    return {"checks": checks, "options": options, "pending_project_id": project_id,
            "generation_started": False, "billable_calls": 0}


def verify(report, key, extension):
    operation = "h3_deploy_acceptance_" + hashlib.sha256(report["started_at"].encode()).hexdigest()[:24]
    report["functional"] = functional_checks(key, operation)
    report["after"] = {"service": service_info(),
                       "head": command("git", "-C", str(ROOT), "rev-parse", "HEAD"),
                       "dirty": command("git", "-C", str(ROOT), "status", "--short"),
                       "main_sha256": digest(ROOT / "app/main.py"),
                       "extension_sha256": digest(ROOT / "app/router_contract.py"),
                       "neighbors": {name: service_info(name) for name in NEIGHBORS}}
    with sqlite3.connect(f'file:{report["database"]}?mode=ro', uri=True) as db:
        report["after"]["activity"] = activity(db)
        after_projects = {item["id"]: item for item in read_projects(db)}
        report["after"]["operation_receipt_count"] = db.execute("SELECT count(*) FROM router_operations").fetchone()[0]
    with sqlite3.connect(f'file:{report["backup"]["database"]}?mode=ro', uri=True) as original:
        report["after"]["existing_projects_unchanged"] = all(
            after_projects.get(value["id"]) == value for value in read_projects(original))
    report["after"]["neighbors_unchanged"] = report["before"]["neighbors"] == report["after"]["neighbors"]
    report["after"]["extension_matches_local"] = report["after"]["extension_sha256"] == hashlib.sha256(
        extension.encode()).hexdigest()
    report["private_configuration"] = {"environment_file": str(Path.home() / ".config/h3-video-studio/router.env"),
                                       "directory_mode": "0700", "file_mode": "0600",
                                       "dropin": str(Path.home() / ".config/systemd/user" / (SERVICE + ".d") / "90-router-contract.conf")}
    if not all(report["after"][key] for key in
               ("existing_projects_unchanged", "neighbors_unchanged", "extension_matches_local")):
        raise RuntimeError("Deployment verification invariant failed")
    report["status"] = "deployed_nonbillable_checks_passed"
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    private_write(Path(report["backup"]["directory"]) / "deployment-report.json", json.dumps(report, indent=2))
    return report


def remote(payload):
    if payload.get("verify"):
        return verify(payload["previous_report"], payload["key"], payload["extension"])
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": command("hostname"),
              "scope": "H3 service only", "status": "inspected"}
    env = environment()
    database = Path(env.get("H3_STUDIO_DATA", str(ROOT / "data"))) / "studio.sqlite3"
    report["database"] = str(database)
    report["credentials"] = credential_availability(env)
    report["before"] = {"head": command("git", "-C", str(ROOT), "rev-parse", "HEAD"),
                        "dirty": command("git", "-C", str(ROOT), "status", "--short"),
                        "service": service_info(),
                        "main_sha256": digest(ROOT / "app/main.py"),
                        "extension_sha256": digest(ROOT / "app/router_contract.py"),
                        "neighbors": {name: service_info(name) for name in NEIGHBORS}}
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        report["before"]["activity"] = activity(db)
    if not payload.get("deploy"):
        return report
    active = report["before"]["activity"]
    if active["projects"] or active["batches"]:
        report["status"] = "deferred_active_tasks"
        return report
    if report["before"]["head"] != payload["expected_head"]:
        raise RuntimeError("Edge H3 HEAD changed; inspect before deploying")
    if report["before"]["dirty"]:
        raise RuntimeError("Edge H3 worktree changed; inspect before deploying")
    if report["before"]["extension_sha256"]:
        raise RuntimeError("An extension already exists; inspect before replacing")
    subprocess.run(["git", "-C", str(ROOT), "apply", "--check", "-"],
                   input=payload["patch"], text=True, capture_output=True, check=True)
    backup = Path.home() / ".local/state/h3-router-deploy" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    snapshot = backup / "studio-before.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    snapshot.chmod(0o600)
    if integrity != "ok":
        raise RuntimeError("Online SQLite backup failed integrity validation")
    shutil.copy2(ROOT / "app/main.py", backup / "main.py.before")
    shutil.copy2(Path.home() / ".config/systemd/user" / SERVICE, backup / SERVICE)
    report["backup"] = {"directory": str(backup), "database": str(snapshot),
                        "integrity_check": integrity, "database_sha256": digest(snapshot),
                        "method": "sqlite3.Connection.backup while service running"}
    private_write(backup / "router_contract.py", payload["extension"])
    private_write(backup / "main.patch", payload["patch"])
    credential_dir = Path.home() / ".config/h3-video-studio"
    credential_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential_dir.chmod(0o700)
    env_file = credential_dir / "router.env"
    if env_file.exists():
        shutil.copy2(env_file, backup / "router.env.before")
    private_write(env_file, "H3_ROUTER_KEY=" + payload["key"] + "\n")
    dropin = Path.home() / ".config/systemd/user" / (SERVICE + ".d") / "90-router-contract.conf"
    if dropin.exists():
        shutil.copy2(dropin, backup / "90-router-contract.conf.before")
    private_write(dropin, "[Service]\nEnvironmentFile=" + str(env_file) + "\n")
    with sqlite3.connect(database, timeout=30) as gate:
        gate.execute("BEGIN IMMEDIATE")
        last_activity = activity(gate)
        report["idle_gate"] = last_activity
        if last_activity["projects"] or last_activity["batches"]:
            report["status"] = "deferred_activity_at_restart_gate"
            return report
        # Every H3 start persists queued state first. Holding this write lock
        # closes the admission race between the last idle check and service stop.
        private_write(ROOT / "app/router_contract.py", payload["extension"], mode=0o644)
        subprocess.run(["git", "-C", str(ROOT), "apply", "-"], input=payload["patch"],
                       text=True, capture_output=True, check=True)
        preflight = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", "import app.main"],
                                   cwd=ROOT, capture_output=True, text=True)
        if preflight.returncode:
            shutil.copy2(backup / "main.py.before", ROOT / "app/main.py")
            report["status"] = "preflight_failed_original_service_preserved"
            report["preflight_error"] = preflight.stderr[-3000:]
            private_write(backup / "deployment-report.json", json.dumps(report, indent=2))
            return report
        report["preflight"] = "import app.main in actual H3 runtime passed"
        command("systemctl", "--user", "daemon-reload")
        command("systemctl", "--user", "stop", SERVICE, timeout=45)
    if not start_or_rollback(report, backup, env_file, dropin):
        private_write(backup / "deployment-report.json", json.dumps(report, indent=2))
        return report
    for _ in range(50):
        try:
            status, options = request("/api/router/options", key=payload["key"])
            if status == 200 and options.get("contract_version") == 1:
                break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    else:
        report["status"] = "reloaded_but_options_unavailable"
        report["after"] = {"service": service_info()}
        if report["after"]["service"]["SubState"] != "running":
            with sqlite3.connect(database, timeout=30) as gate:
                gate.execute("BEGIN IMMEDIATE")
                active = activity(gate)
            if not active["projects"] and not active["batches"]:
                restored = rollback_original(report, backup, env_file, dropin)
                report["status"] = (
                    "original_service_restored_after_startup_failure"
                    if restored else "original_files_restored_service_restart_failed"
                )
        private_write(backup / "deployment-report.json", json.dumps(report, indent=2))
        return report
    return verify(report, payload["key"], payload["extension"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--deploy", action="store_true")
    mode.add_argument("--verify", action="store_true", help="Verify an existing deployment without restarting")
    parser.add_argument("--host", default="edge")
    parser.add_argument("--expected-head", default=EXPECTED_HEAD)
    parser.add_argument("--key-file", type=Path, default=Path.home() / ".config/ai-router-media/h3-key")
    parser.add_argument("--report", type=Path, default=Path.home() / ".local/state/ai-router-acceptance/20260904-media-deploy/h3-deploy.json")
    args = parser.parse_args()
    payload = {"deploy": args.deploy, "verify": args.verify, "expected_head": args.expected_head}
    if args.verify:
        payload["previous_report"] = json.loads(args.report.read_text())
    if args.deploy or args.verify:
        if args.key_file.is_symlink() or (args.key_file.stat().st_mode & 0o777) != 0o600:
            raise RuntimeError("Expected a private 0600 key file")
        key = args.key_file.read_text().strip()
        if not key or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in key):
            raise RuntimeError("Invalid private key file")
        payload.update(key=key, extension=Path(__file__).with_name("router_contract.py").read_text(),
                       patch=Path(__file__).with_name("main.patch").read_text())
    source = Path(__file__).read_text()
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", args.host,
                             "python3 -c " + shlex.quote(source) + " --remote"],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=180)
    if result.returncode:
        # No request payload or process environment is included in diagnostics.
        raise RuntimeError(f"Remote deployment failed (exit {result.returncode}): {result.stderr[-3000:]}")
    report = json.loads(result.stdout)
    private_write(args.report, json.dumps(report, indent=2))
    print(json.dumps({"report": str(args.report), "status": report["status"],
                      "before": report["before"], "after": report.get("after"),
                      "credentials": report["credentials"]}, indent=2))


if __name__ == "__main__":
    if "--remote" in sys.argv:
        print(json.dumps(remote(json.load(sys.stdin))))
    else:
        main()
