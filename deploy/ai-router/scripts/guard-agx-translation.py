#!/usr/bin/env python3
"""Root-only, temporary Hy-MT2 start guard; does not restart the shared TTS gateway."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
from uuid import uuid4


UNIT = "agx-hymt-translate.service"
DROPIN = Path("/run/systemd/system/agx-hymt-translate.service.d/90-cerebellum-mtp-maintenance.conf")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def release(path):
    state = json.loads(path.read_text())
    if state.get("released"):
        return state
    if DROPIN.exists():
        if DROPIN.read_text() != state["dropin"]:
            raise RuntimeError("guard was changed concurrently; refusing removal")
        DROPIN.unlink()
        run("systemctl", "daemon-reload")
    if state["was_active"]:
        run("systemctl", "start", UNIT)
    state["released"] = True
    state["released_at"] = time.time()
    path.write_text(json.dumps(state, indent=2))
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("acquire", "release"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.execute:
        parser.error("requires root and --execute")
    path = Path(args.state).resolve()
    if args.action == "release":
        print(json.dumps(release(path), indent=2))
        return
    if path.exists() or DROPIN.exists():
        raise RuntimeError("guard/state already exists")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://100.103.199.121:11435/_lazy/status", timeout=10) as response:
        status = json.load(response)
    if status["activeRequests"] != 0:
        raise RuntimeError("translation has active requests")
    token = uuid4().hex[:12]
    state = {
        "was_active": status["state"] == "active", "released": False,
        "gateway_pid": run("systemctl", "show", "agx-lazy-services-gateway.service", "-p", "MainPID", "--value"),
        "dropin": f"[Unit]\nConditionPathExists=/run/cerebellum-mtp-{token}.allow\n",
        "timer": f"agx-mtp-translation-recovery-{token}",
        "acquired_at": time.time(),
    }
    with path.open("x") as handle:
        json.dump(state, handle, indent=2)
    os.chmod(path, 0o600)
    try:
        run("systemd-run", "--unit=" + state["timer"], "--on-active=150m",
            "/usr/bin/python3", str(Path(__file__).resolve()), "release", "--state", str(path), "--execute")
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        with DROPIN.open("x") as handle:
            handle.write(state["dropin"])
        run("systemctl", "daemon-reload")
        run("systemctl", "stop", UNIT)
        run("systemctl", "start", UNIT)
        active = run("systemctl", "show", UNIT, "-p", "ActiveState", "--value")
        if active != "inactive":
            raise RuntimeError("translation guard did not prevent startup")
        if run("systemctl", "show", "agx-lazy-services-gateway.service", "-p", "MainPID", "--value") != state["gateway_pid"]:
            raise RuntimeError("shared gateway changed during guard setup")
    except BaseException:
        release(path)
        raise
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
