"""Supervise the qualified 4060 Ti ComfyUI worker with bounded resource checks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


GIB = 1024 ** 3
ROOT = Path.home() / ".local/state/siyuan-video-u24-production"
DATA = ROOT / "data"
RUNTIME = Path("/home/ivan/h3/runtimes/runtime-a4")
PYTHON = "/home/ivan/h3/comfy-venv/bin/python"
GPU_UUID = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"
STOP = False


def stop(_signal, _frame):
    global STOP
    STOP = True


def meminfo(name):
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(name + ":"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("missing memory metric")


def sample():
    relative = Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip().lstrip("/")
    group = Path("/sys/fs/cgroup") / relative
    events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
    output = subprocess.check_output([
        "nvidia-smi", "--query-gpu=uuid,memory.used,temperature.gpu",
        "--format=csv,noheader,nounits",
    ], text=True, timeout=8)
    gpu = next((line.split(",") for line in output.splitlines()
                if line.split(",", 1)[0].strip() == GPU_UUID), None)
    if gpu is None:
        raise RuntimeError("qualified 4060 Ti is missing")
    return {
        "available": meminfo("MemAvailable"),
        "swap_used": meminfo("SwapTotal") - meminfo("SwapFree"),
        "cgroup_swap": int((group / "memory.swap.current").read_text()),
        "events": {key: int(value) for key, value in events.items()},
        "root_free": shutil.disk_usage("/").free,
        "data_free": shutil.disk_usage(DATA).free,
        "gpu_used_mib": int(gpu[1]), "temperature_c": int(gpu[2]),
    }


def safety_reason(now, baseline):
    if now["available"] < 16 * GIB:
        return "host_ram_floor"
    if now["root_free"] < 25 * GIB or now["data_free"] < 40 * GIB:
        return "disk_headroom"
    if now["swap_used"] > 8 * GIB or now["cgroup_swap"] - baseline["cgroup_swap"] > GIB:
        return "swap_guard"
    if any(now["events"].get(key, 0) > baseline["events"].get(key, 0)
           for key in ("oom", "oom_kill", "max")):
        return "worker_memory_event"
    if now["temperature_c"] >= 90:
        return "gpu_temperature"
    return None


def main():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    baseline = sample()
    if (baseline["available"] < 28 * GIB or baseline["swap_used"] > GIB
            or baseline["gpu_used_mib"] > 1500 or safety_reason(baseline, baseline)):
        raise RuntimeError("u24 worker preflight resources unavailable")
    base = ROOT / "worker-fast"
    files = DATA / "fast"
    for path in (base / "custom_nodes", base / "user", files / "input", files / "output", files / "temp"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for plugin in (RUNTIME / "custom_nodes").iterdir():
        target = base / "custom_nodes" / plugin.name
        if target.is_symlink() and target.resolve() != plugin.resolve():
            raise RuntimeError("u24 custom node link differs from qualified runtime")
        if target.exists() and not target.is_symlink():
            raise RuntimeError("u24 custom node path is not a runtime link")
        if not target.exists():
            target.symlink_to(plugin)
    command = [PYTHON, str(RUNTIME / "main.py"), "--base-directory", str(base),
               "--listen", "127.0.0.1", "--port", "19388",
               "--input-directory", str(files / "input"),
               "--output-directory", str(files / "output"),
               "--temp-directory", str(files / "temp"),
               "--user-directory", str(base / "user"),
               "--database-url", "sqlite:///" + str(base / "user/comfyui.db"),
               "--reserve-vram", "1", "--disable-pinned-memory", "--disable-auto-launch",
               "--cache-none", "--enable-dynamic-vram", "--extra-model-paths-config",
               str(RUNTIME / "extra_model_paths.yaml")]
    environment = {**os.environ, "CUDA_VISIBLE_DEVICES": GPU_UUID,
                   "TMPDIR": str(files / "temp"), "XDG_CACHE_HOME": str(files / "temp/cache")}
    with (ROOT / "worker-fast.log").open("a") as log:
        process = subprocess.Popen(command, cwd=RUNTIME, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while not STOP:
                now = sample()
                reason = safety_reason(now, baseline)
                if reason:
                    raise RuntimeError("u24 worker safety threshold crossed: " + reason)
                if process.poll() is not None:
                    raise RuntimeError("u24 ComfyUI worker exited")
                time.sleep(2)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    main()
