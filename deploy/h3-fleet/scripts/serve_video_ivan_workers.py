"""Run the qualified Ivan RTX 3060 pair with bounded resource monitoring."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


GIB = 1024 ** 3
CASE = "siyuan-video-production"
STOP = False


def mark_stop(_signal, _frame):
    global STOP
    STOP = True


def kib(name):
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(name + ":"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("missing meminfo " + name)


def cgroup():
    relative = Path("/proc/self/cgroup").read_text().split("::", 1)[1].strip().lstrip("/")
    return Path("/sys/fs/cgroup") / relative


def counters(path):
    return {line.split()[0]: int(line.split()[1]) for line in path.read_text().splitlines()}


def sample(data, gpu_ids):
    group = cgroup()
    output = subprocess.check_output([
        "nvidia-smi", "--query-gpu=uuid,memory.used,temperature.gpu",
        "--format=csv,noheader,nounits"], text=True, timeout=8)
    gpus = {}
    for line in output.splitlines():
        uuid, used, temperature = [part.strip() for part in line.split(",")]
        if uuid in gpu_ids:
            gpus[uuid] = {"used_mib": int(used), "temperature_c": int(temperature)}
    return {"time": time.time(), "available": kib("MemAvailable"),
            "swap_used": kib("SwapTotal") - kib("SwapFree"),
            "cgroup_swap_used": int((group / "memory.swap.current").read_text()),
            "root_free": shutil.disk_usage("/").free, "data_free": shutil.disk_usage(data).free,
            "cgroup_current": int((group / "memory.current").read_text()),
            "cgroup_events": counters(group / "memory.events"), "gpus": gpus}


def safety_reason(item, baseline):
    # Host-wide swap can grow because of unrelated services. This worker owns
    # only its cgroup; retain an absolute host ceiling and the RAM/OOM guards.
    if item["available"] < 16 * GIB:
        return "host_ram_floor"
    if item["root_free"] < 25 * GIB or item["data_free"] < 40 * GIB:
        return "disk_headroom"
    if item["swap_used"] > 8 * GIB:
        return "host_swap_ceiling"
    if item["cgroup_swap_used"] - baseline["cgroup_swap_used"] > GIB:
        return "worker_swap_growth"
    if any(item["cgroup_events"].get(key, 0) > baseline["cgroup_events"].get(key, 0)
           for key in ("oom", "oom_kill", "max")):
        return "worker_memory_event"
    if any(gpu["temperature_c"] >= 90 for gpu in item["gpus"].values()):
        return "gpu_temperature"
    return None


def supervise_workers(processes, restart_at, failures, launch, now):
    """Restart a failed GPU lane without stopping its healthy sibling."""
    for name, process in list(processes.items()):
        if process is not None and process.poll() is not None:
            code = process.wait()
            processes[name] = None
            failures[name] = [stamp for stamp in failures[name] if now - stamp < 600] + [now]
            restart_at[name] = now + 15 if len(failures[name]) < 3 else float("inf")
            print(f"worker {name} exited ({code}); other lanes remain untouched", file=sys.stderr, flush=True)
        if processes[name] is None and now >= restart_at[name]:
            try:
                processes[name] = launch(name)
                restart_at.pop(name)
            except Exception as error:
                failures[name] = [stamp for stamp in failures[name] if now - stamp < 600] + [now]
                restart_at[name] = now + 15 if len(failures[name]) < 3 else float("inf")
                print(f"worker {name} restart failed: {type(error).__name__}", file=sys.stderr, flush=True)
    if all(process is None for process in processes.values()):
        if all(restart_at[name] == float("inf") for name in processes):
            raise RuntimeError("all worker lanes failed repeatedly")


def prepare(base, data, name, models, plugins):
    for path in (base, data / name / "input", data / name / "output", data / name / "temp",
                 base / "user", base / "custom_nodes"):
        path.mkdir(parents=True, exist_ok=True)
    if models and not (base / "models").exists():
        (base / "models").symlink_to(models)
    for plugin in plugins:
        target = base / "custom_nodes" / Path(plugin).name
        if not target.exists():
            target.symlink_to(plugin)
    return data / name


def main(role):
    root = Path.home() / ".local/state" / CASE
    core = Path("/mnt/ivan-ext4-offload/ComfyUI")
    python = "/home/ivan/.local/state/siyuan-media-validation/backend-venv/bin/python"
    data = Path("/media/ivan/44A2C0D9A2C0D11A") / CASE
    targets = [
        ("preview", "GPU-b9ca94d5-6180-2d81-bb33-5ad04722f492", 19389),
        ("main", "GPU-08c21842-c266-7f7d-6e5d-d494d4c20c4f", 19388),
    ]
    models = core / "models"
    plugins = [root / "plugins/community-memory"]
    extra = []
    reserve_vram = "3"
    if not plugins[0].is_dir():
        raise RuntimeError("qualified community-memory plugin is missing")
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    baseline = sample(data, {gpu for _, gpu, _ in targets})
    if (baseline["available"] < 28 * GIB or baseline["root_free"] < 25 * GIB
            or baseline["data_free"] < 40 * GIB or baseline["swap_used"] > 1 * GIB
            or any(item["used_mib"] > 1500 for item in baseline["gpus"].values())):
        raise RuntimeError("worker preflight resources unavailable")
    (root / "baseline.json").write_text(json.dumps(baseline, indent=2))
    targets_by_name = {name: (gpu, port) for name, gpu, port in targets}

    def launch(name):
        gpu, port = targets_by_name[name]
        base = root / ("worker-" + name)
        files = prepare(base, data, name, models, plugins)
        command = [python, str(core / "main.py"), "--base-directory", str(base),
                   "--listen", "127.0.0.1", "--port", str(port),
                   "--input-directory", str(files / "input"),
                   "--output-directory", str(files / "output"),
                   "--temp-directory", str(files / "temp"),
                   "--user-directory", str(base / "user"),
                   "--database-url", "sqlite:///" + str(base / "user/comfyui.db"),
                   "--reserve-vram", reserve_vram, "--disable-pinned-memory",
                   "--disable-auto-launch", "--cache-none", "--enable-dynamic-vram", *extra]
        command.append("--use-sage-attention")
        environment = {**os.environ, "CUDA_VISIBLE_DEVICES": gpu,
                       "TMPDIR": str(files / "temp"),
                       "XDG_CACHE_HOME": str(files / "temp" / "cache")}
        return subprocess.Popen(command, cwd=core, env=environment,
                                stderr=subprocess.STDOUT, start_new_session=True)

    processes = {}
    restart_at = {}
    failures = {name: [] for name in targets_by_name}
    try:
        for name in targets_by_name:
            processes[name] = launch(name)
        with (root / "metrics.jsonl").open("a", buffering=1) as metrics:
            while not STOP:
                item = sample(data, {gpu for _, gpu, _ in targets})
                metrics.write(json.dumps(item) + "\n")
                reason = safety_reason(item, baseline)
                if reason:
                    raise RuntimeError("worker safety threshold crossed: " + reason)
                supervise_workers(processes, restart_at, failures, launch, time.monotonic())
                time.sleep(2)
    finally:
        for process in processes.values():
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes.values():
            if process is None:
                continue
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, mark_stop)
    signal.signal(signal.SIGINT, mark_stop)
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["ivan"])
    args = parser.parse_args()
    try:
        main(args.role)
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr, flush=True)
        raise
