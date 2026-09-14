from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from scripts.progressive_resource_policy import parse_inventory


def command(*arguments: str) -> str:
    return subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=5).stdout


def host_swap() -> int:
    memory = dict((parts[0], int(parts[1]) * 1024) for parts in
                  (line.split() for line in Path("/proc/meminfo").read_text().splitlines())
                  if parts[0] in {"SwapTotal:", "SwapFree:"})
    return memory["SwapTotal:"] - memory["SwapFree:"]


def residual_sample(sample: dict) -> dict:
    before = host_swap()
    swaps = Path("/proc/swaps").read_text()
    devices = []
    for line in swaps.splitlines()[1:]:
        name = line.split()[0]
        if not re.fullmatch(r"/dev/zram[0-9]+", name):
            continue
        directory = Path("/sys/block") / Path(name).name
        devices.append({"path": name, "disksize_bytes": int((directory / "disksize").read_text()),
                        **{key: (directory / key).read_text() for key in ("backing_dev", "mm_stat", "bd_stat")}})
    group = Path(os.environ.get("H3_COMPUTE_CGROUP", "/sys/fs/cgroup/h3.slice/h3-compute.slice"))
    group_swap = int((group / "memory.swap.current").read_text())
    after = host_swap()
    if before != after or after != sample["swap_used_bytes"] or group_swap != sample["cgroup_swap_bytes"]:
        raise ValueError("swap_sample_changed_during_collection")
    sample["swap_inventory"] = {"observed_at": time.time(), "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
                                "proc_swaps": swaps, "devices": devices}
    parse_inventory(sample, time.time())
    raw = command("nvidia-smi", "--query-gpu=uuid,memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits")
    sample["gpus"] = [dict(zip(("uuid", "memory_used_mib", "utilization_percent", "temperature_c"),
                                [parts[0], *map(int, parts[1:])]))
                       for parts in ([value.strip() for value in line.split(",")] for line in raw.splitlines())]
    raw = command("systemctl", "show", *("comfyui-h3@" + lane + ".service" for lane in ("fast", "main", "preview")),
                  "-p", "Id", "-p", "ActiveState", "-p", "MainPID", "-p", "NRestarts")
    sample["services"] = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                          for block in raw.strip().split("\n\n")]
    return sample
