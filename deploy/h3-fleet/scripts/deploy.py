"""Idle-only Ivan H3 fleet deployment with private-key transport and rollback."""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = (
    Path.home()
    / ".local/state/ai-router-acceptance/20260908-h3-direct/ivan-fleet-deploy.json"
)
DEFAULT_KEY = Path.home() / ".config/ai-router-media/h3-key"
DEFAULT_EXECUTOR_URL = "http://100.96.79.21:8789"
REMOTE_ROOT = "/mnt/ivan-ext4-offload/h3-fleet"
MIN_ROOT_AVAILABLE = 25 * 1024**3
MIN_OFFLOAD_AVAILABLE = 40 * 1024**3


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".{os.getpid()}.part")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive() -> str:
    files = [
        "app/__init__.py",
        "app/main.py",
        "app/workflow_builder.py",
        "requirements.txt",
        "systemd/h3-compute.slice",
        "systemd/comfyui-h3@.service",
        "systemd/h3-fleet.service",
    ]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for relative in files:
            path = ROOT / relative
            if not path.is_file():
                raise RuntimeError(f"missing deployment file: {relative}")
            info = bundle.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                bundle.addfile(info, handle)
    return base64.b64encode(output.getvalue()).decode()


def key(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("H3 key file must be a regular 0600 file")
    value = path.read_text(encoding="utf-8").strip()
    if not value or len(value) > 256 or not all(character.isalnum() or character in "_-" for character in value):
        raise RuntimeError("H3 key file is invalid")
    return value


def private_executor_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("H3 executor URL must be a private HTTP origin")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    private = (
        parsed.hostname in {"127.0.0.1", "localhost"}
        or parsed.hostname.endswith(".taild500c8.ts.net")
        or bool(address and (
            address.is_loopback
            or address in ipaddress.ip_network("100.64.0.0/10")
        ))
    )
    if not private:
        raise RuntimeError("H3 executor URL must use loopback or the Tailscale private network")
    return value.rstrip("/")


REMOTE = r'''
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path("/mnt/ivan-ext4-offload/h3-fleet")
ENV = Path("/etc/h3-fleet/fleet.env")
SYSTEMD = Path("/etc/systemd/system")
MIN_ROOT = 25 * 1024**3
MIN_OFFLOAD = 40 * 1024**3
UNITS = (
    "comfyui-h3@fast.service",
    "comfyui-h3@main.service",
    "comfyui-h3@preview.service",
    "h3-fleet.service",
)

def command(*args, check=True, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {args[0]} {args[1] if len(args) > 1 else ''}")
    return result.stdout.strip()

def service(name):
    raw = command("systemctl", "show", name, "-p", "ActiveState", "-p", "SubState",
                  "-p", "MainPID", "-p", "NRestarts", "-p", "Slice")
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

def active_jobs():
    database = ROOT / "fleet.sqlite3"
    if not database.is_file():
        return []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        return [
            {"prompt_id": row[0], "lane_id": row[1], "status": row[2]}
            for row in db.execute(
                "SELECT prompt_id,lane_id,status FROM jobs "
                "WHERE status IN ('queued','reserved','submitted','running') "
                "ORDER BY created_at"
            )
        ]

def inventory():
    root = shutil.disk_usage("/")
    offload = shutil.disk_usage("/mnt/ivan-ext4-offload")
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemAvailable:", "SwapTotal:", "SwapFree:")):
            name, value, _ = line.split()
            meminfo[name.rstrip(":")] = int(value) * 1024
    kernel = command(
        "journalctl", "-k", "--since", "24 hours ago", "--no-pager",
        check=False, timeout=30,
    )
    alerts = [
        line[-500:]
        for line in kernel.splitlines()
        if any(marker in line for marker in ("NVRM: Xid", "oom-kill", "Out of memory"))
    ]
    return {
        "active_jobs": active_jobs(),
        "root_available_bytes": root.free,
        "offload_available_bytes": offload.free,
        "memory_available_bytes": meminfo.get("MemAvailable"),
        "swap_used_bytes": meminfo.get("SwapTotal", 0) - meminfo.get("SwapFree", 0),
        "kernel_alerts": alerts[-20:],
        "services": {name: service(name) for name in UNITS},
    }

def process_env(unit):
    pid = int(service(unit)["MainPID"])
    if pid <= 0:
        raise RuntimeError(f"{unit} has no running process")
    values = {}
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            values[name.decode()] = value.decode()
    return values

def lane_bindings():
    scheduler = process_env("h3-fleet.service")
    lanes = json.loads(scheduler.get("H3_FLEET_LANES", "[]"))
    if (
        {lane.get("id") for lane in lanes} != {"fast", "main", "preview"}
        or len({lane.get("url") for lane in lanes}) != 3
        or len({lane.get("gpu_uuid") for lane in lanes}) != 3
    ):
        raise RuntimeError("Ivan lane inventory is not three distinct physical lanes")
    bindings = {}
    for lane in lanes:
        lane_id = lane["id"]
        worker = process_env(f"comfyui-h3@{lane_id}.service")
        if worker.get("CUDA_VISIBLE_DEVICES") != lane["gpu_uuid"]:
            raise RuntimeError(f"Ivan {lane_id} worker GPU UUID does not match fleet config")
        if not lane["url"].endswith(":" + worker.get("H3_PORT", "")):
            raise RuntimeError(f"Ivan {lane_id} worker port does not match fleet config")
        bindings[lane_id] = {
            "gpu_uuid": lane["gpu_uuid"],
            "url": lane["url"],
            "preview_only": bool(lane.get("preview_only", False)),
        }
    if not bindings["preview"]["preview_only"]:
        raise RuntimeError("Ivan preview lane must remain preview-only")
    return bindings

def safe_extract(encoded, destination):
    raw = base64.b64decode(encoded, validate=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if member.issym() or member.islnk() or not target.is_relative_to(destination.resolve()):
                raise RuntimeError("unsafe deployment archive")
        bundle.extractall(destination)

def merge_env(text, updates):
    output = []
    seen = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        name = stripped.split("=", 1)[0] if "=" in stripped and not stripped.startswith("#") else None
        if name in updates:
            output.append(name + "=" + updates[name])
            seen.add(name)
        else:
            output.append(raw)
    for name, value in updates.items():
        if name not in seen:
            output.append(name + "=" + value)
    return "\n".join(output).rstrip() + "\n"

def install_file(source, target, mode, sudo=False):
    if sudo:
        temporary = source.parent / ("." + source.name + "." + uuid4().hex + ".install")
        shutil.copy2(source, temporary)
        os.chmod(temporary, mode)
        command("sudo", "-n", "install", "-m", oct(mode)[2:], str(temporary), str(target))
        temporary.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / ("." + target.name + "." + uuid4().hex + ".install")
        shutil.copy2(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)

def authenticated_options(base, secret):
    request = urllib.request.Request(
        base + "/api/router/options",
        headers={"Authorization": "Bearer " + secret},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

def health(base):
    with urllib.request.urlopen(base + "/api/health", timeout=15) as response:
        return json.load(response)

def post_router(base, path, secret):
    request = urllib.request.Request(
        base + path,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {"supported": True, "value": json.load(response)}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"supported": False}
        raise

def wait_healthy(base, secret, *, timeout_seconds=120):
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            live_health = health(base)
            options = authenticated_options(base, secret)
            healthy_lanes = [
                lane
                for lane in live_health.get("lanes", [])
                if lane.get("enabled") and lane.get("ok")
            ]
            if len(healthy_lanes) == 3:
                return live_health, options
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Ivan H3 direct contract did not become healthy")
        time.sleep(2)

def restore(backup, existed, before, payload):
    command("sudo", "-n", "systemctl", "stop", "h3-fleet.service", check=False)
    for unit in UNITS[:3]:
        command("sudo", "-n", "systemctl", "stop", unit, check=False)
    for relative in ("app/__init__.py", "app/main.py", "app/workflow_builder.py", "requirements.txt"):
        target = ROOT / relative
        saved = backup / "files" / relative
        if relative in existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        else:
            target.unlink(missing_ok=True)
    for name in ("h3-compute.slice", "comfyui-h3@.service", "h3-fleet.service"):
        saved = backup / "systemd" / name
        target = SYSTEMD / name
        if "systemd/" + name in existed:
            command("sudo", "-n", "install", "-m", "644", str(saved), str(target))
        else:
            command("sudo", "-n", "rm", "-f", str(target))
    if "fleet.env" in existed:
        command("sudo", "-n", "install", "-m", "600", str(backup / "fleet.env"), str(ENV))
    else:
        command("sudo", "-n", "rm", "-f", str(ENV))
    command("sudo", "-n", "systemctl", "daemon-reload")
    for unit in UNITS[:3]:
        if before["services"][unit]["ActiveState"] == "active":
            command("sudo", "-n", "systemctl", "start", unit)
    if before["services"]["h3-fleet.service"]["ActiveState"] == "active":
        command("sudo", "-n", "systemctl", "start", "h3-fleet.service")
        wait_healthy(payload["executor_url"], payload["key"])

def run(payload):
    before = inventory()
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "before": before,
              "scope": "Ivan h3-fleet direct Router contract"}
    if not payload.get("execute"):
        report["status"] = "inspected"
        return report
    if before["active_jobs"]:
        raise RuntimeError("Ivan H3 jobs are active")
    if before["root_available_bytes"] < MIN_ROOT or before["offload_available_bytes"] < MIN_OFFLOAD:
        raise RuntimeError("Ivan storage gate failed")
    if before["kernel_alerts"]:
        raise RuntimeError("Ivan kernel has recent Xid or OOM evidence")
    command("sudo", "-n", "true")
    backup = Path.home() / ".local/state/h3-fleet-deploy" / (
        time.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    )
    staged = backup / "staged"
    staged.mkdir(parents=True, mode=0o700)
    safe_extract(payload["archive"], staged)
    existed = set()
    for relative in ("app/__init__.py", "app/main.py", "app/workflow_builder.py", "requirements.txt"):
        target = ROOT / relative
        if target.is_file():
            existed.add(relative)
            saved = backup / "files" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    for name in ("h3-compute.slice", "comfyui-h3@.service", "h3-fleet.service"):
        target = SYSTEMD / name
        if target.is_file():
            existed.add("systemd/" + name)
            saved = backup / "systemd" / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            command("sudo", "-n", "cp", "--preserve=mode,timestamps", str(target), str(saved))
            command("sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(saved))
    if ENV.is_file():
        existed.add("fleet.env")
        command("sudo", "-n", "cp", "--preserve=mode,timestamps", str(ENV), str(backup / "fleet.env"))
        command("sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(backup / "fleet.env"))
    report["backup"] = str(backup)
    try:
        drain = post_router(
            payload["executor_url"],
            "/api/router/drain",
            payload["key"],
        )
        report["drain"] = drain
        if active_jobs():
            if drain["supported"]:
                post_router(
                    payload["executor_url"],
                    "/api/router/resume",
                    payload["key"],
                )
            raise RuntimeError("Ivan H3 jobs became active during the drain gate")
        if not drain["supported"]:
            command("sudo", "-n", "systemctl", "stop", "h3-fleet.service")
        for relative in ("app/__init__.py", "app/main.py", "app/workflow_builder.py", "requirements.txt"):
            install_file(staged / relative, ROOT / relative, 0o644)
        current_env = (
            (backup / "fleet.env").read_text()
            if "fleet.env" in existed
            else ""
        )
        updated_env = merge_env(current_env, {
            "H3_ROUTER_KEY": payload["key"],
            "H3_TURBO_TEMPLATE": "/mnt/ivan-ext4-offload/h3-deploy/workflows/turbo4-api.json",
            "H3_QUALITY_TEMPLATE": "/mnt/ivan-ext4-offload/h3-deploy/workflows/quality14-768-api.json",
        })
        private_env = staged / "fleet.env"
        private_env.write_text(updated_env)
        os.chmod(private_env, 0o600)
        command("sudo", "-n", "install", "-m", "600", str(private_env), str(ENV))
        for name in ("h3-compute.slice", "comfyui-h3@.service", "h3-fleet.service"):
            install_file(staged / "systemd" / name, SYSTEMD / name, 0o644, sudo=True)
        command(str(ROOT / "venv/bin/python"), "-m", "py_compile",
                str(ROOT / "app/main.py"), str(ROOT / "app/workflow_builder.py"))
        command("sudo", "-n", "systemd-analyze", "verify",
                str(SYSTEMD / "h3-compute.slice"),
                str(SYSTEMD / "comfyui-h3@.service"),
                str(SYSTEMD / "h3-fleet.service"))
        if active_jobs():
            raise RuntimeError("Ivan accepted a job after the drain gate")
        command("sudo", "-n", "systemctl", "stop", "h3-fleet.service")
        for unit in UNITS[:3]:
            command("sudo", "-n", "systemctl", "stop", unit)
        command("sudo", "-n", "systemctl", "daemon-reload")
        for unit in UNITS[:3]:
            command("sudo", "-n", "systemctl", "start", unit, timeout=120)
        command("sudo", "-n", "systemctl", "start", "h3-fleet.service", timeout=60)
        live_health, options = wait_healthy(
            payload["executor_url"],
            payload["key"],
        )
        if options.get("workflow_contract_version") != 2:
            raise RuntimeError("Ivan H3 direct contract version is incorrect")
        after = inventory()
        report["lane_bindings"] = lane_bindings()
        if any(after["services"][unit].get("Slice") != "h3-compute.slice"
               for unit in UNITS[:3]):
            raise RuntimeError("ComfyUI workers are outside h3-compute.slice")
        report["after"] = after
        report["options"] = {
            "contract_version": options.get("contract_version"),
            "workflow_contract_version": options.get("workflow_contract_version"),
            "execution_profiles": options.get("execution_profiles"),
        }
        report["status"] = "deployed_verified_no_generation"
    except Exception as error:
        report["first_fatal"] = {"type": type(error).__name__, "message": str(error)}
        try:
            restore(backup, existed, before, payload)
            report["rollback"] = "restored_verified"
        except Exception as rollback_error:
            report["rollback"] = {
                "type": type(rollback_error).__name__,
                "message": str(rollback_error),
            }
        report["status"] = "failed"
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return report

payload = json.load(sys.stdin)
print(json.dumps(run(payload)))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="ivan")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--executor-url", default=DEFAULT_EXECUTOR_URL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    payload = {
        "execute": args.execute,
        "executor_url": private_executor_url(args.executor_url),
    }
    if args.execute:
        payload.update(archive=archive(), key=key(args.key_file))
    result = subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            args.host,
            "python3 -c " + shlex.quote(REMOTE),
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError(
            f"Ivan fleet deployment command failed ({result.returncode}): "
            + result.stderr[-2000:]
        )
    report = json.loads(result.stdout)
    private_write(args.report, json.dumps(report, indent=2).encode())
    print(json.dumps({
        "status": report["status"],
        "report": str(args.report),
        "before": report.get("before"),
        "after": report.get("after"),
        "options": report.get("options"),
        "rollback": report.get("rollback"),
    }, indent=2))
    return 0 if report["status"] in {"inspected", "deployed_verified_no_generation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
