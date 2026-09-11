"""Conservative capacity policy and host resource admission for Ivan H3."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


BUSY = ("reserved", "reconciling", "submitted", "running", "cancelling")
GIB = 1024**3
DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "config/capacity.json"


class InstanceLock:
    """OS releases the persistent database's lock even after an unclean exit."""

    def __init__(self, database: Path) -> None:
        self.path = database.with_suffix(database.suffix + ".lock")
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RuntimeError("another H3 fleet process owns this database") from error
        self.handle = handle

    def release(self) -> None:
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def resource_snapshot() -> dict[str, Any]:
    """Missing telemetry closes admission; the queue can still be inspected."""
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemAvailable:", "SwapTotal:", "SwapFree:")):
                name, value, _ = line.split()
                memory[name.rstrip(":")] = int(value) * 1024
        cgroup = Path(os.environ.get(
            "H3_COMPUTE_CGROUP", "/sys/fs/cgroup/h3.slice/h3-compute.slice"
        ))
        events = dict(
            (key, int(value))
            for key, value in (line.split() for line in (cgroup / "memory.events").read_text().splitlines())
        )
        vmstat = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
        pressure = {}
        for source, path in (("host", Path("/proc/pressure/memory")),
                             ("cgroup", cgroup / "memory.pressure")):
            for line in path.read_text().splitlines():
                category, *values = line.split()
                fields = dict(value.split("=") for value in values)
                pressure[f"{source}_{category}_total"] = int(fields["total"])
                pressure[f"{source}_{category}_avg10"] = float(fields["avg10"])
        return {
            "timestamp": time.time(),
            "ok": True,
            "memory_available_bytes": memory["MemAvailable"],
            "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
            "root_available_bytes": shutil.disk_usage("/").free,
            "offload_available_bytes": shutil.disk_usage(os.environ.get(
                "H3_OFFLOAD_ROOT", "/mnt/ivan-ext4-offload"
            )).free,
            "cgroup_current_bytes": int((cgroup / "memory.current").read_text()),
            "cgroup_swap_bytes": int((cgroup / "memory.swap.current").read_text()),
            "cgroup_events": events,
            "swap_io_pages": {key: int(vmstat[key]) for key in ("pswpin", "pswpout")},
            "memory_pressure": pressure,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, ValueError, KeyError):
        return {"timestamp": time.time(), "ok": False, "reason": "resource_telemetry_unavailable"}


class SwapRecovery:
    """Observe a quiet idle window; never turn a busy residual into a new baseline.

    A process restart starts closed and must observe a new complete idle window.
    The baseline remains frozen throughout each round, including unknown outcomes.
    This does not cancel running work; it prevents subsequent admission.
    """

    def __init__(self, stable_seconds: int = 60) -> None:
        self.stable_seconds = stable_seconds
        self.candidate: dict[str, Any] | None = None
        self.baseline: dict[str, Any] | None = None
        self.last_signature: str | None = None
        self.was_busy = False

    def observe(self, snapshot: dict[str, Any], *, idle: bool) -> dict[str, Any]:
        result = {**snapshot}
        recovery: dict[str, Any] = {"ready": False, "exclusive_single": True,
                                  "stable_seconds_required": self.stable_seconds}
        result["swap_recovery"] = recovery
        required = {"swap_io_pages", "memory_pressure", "boot_id", "cgroup_events"}
        if not snapshot.get("ok") or not required <= snapshot.keys():
            self.candidate = None
            recovery["reason"] = "telemetry_unavailable"
            return result
        signature = json.dumps({key: snapshot[key] for key in required}, sort_keys=True)
        changed = self.last_signature is not None and signature != self.last_signature
        pressure = any(value > 0 for key, value in snapshot["memory_pressure"].items()
                       if key.endswith("_avg10"))
        self.last_signature = signature
        if not idle:
            self.candidate = None
            self.was_busy = True
        elif self.was_busy:
            self.candidate = None
            # Retain the previous round's guard until a new quiet window is
            # complete, even if swapped pages have since been reclaimed.
            self.was_busy = False
        if changed or pressure:
            self.candidate = None
            recovery["reason"] = "active_swap_io_or_memory_pressure"
        elif idle:
            if self.candidate is None:
                self.candidate = {"timestamp": snapshot["timestamp"],
                                  "host_bytes": snapshot["swap_used_bytes"],
                                  "cgroup_bytes": snapshot["cgroup_swap_bytes"]}
            quiet_seconds = snapshot["timestamp"] - self.candidate["timestamp"]
            recovery["stable_seconds_observed"] = max(0, quiet_seconds)
            if quiet_seconds >= self.stable_seconds:
                self.baseline = self.candidate.copy()
                recovery["ready"] = True
            else:
                recovery["reason"] = "awaiting_stable_idle_window"
        if self.baseline:
            recovery["baseline"] = self.baseline.copy()
            recovery["growth_bytes"] = max(0, snapshot["swap_used_bytes"] - self.baseline["host_bytes"],
                                             snapshot["cgroup_swap_bytes"] - self.baseline["cgroup_bytes"])
        return result


class CapacityPolicy:
    def __init__(self, path: Path | None = None) -> None:
        self.data = json.loads((path or Path(os.environ.get(
            "H3_CAPACITY_POLICY", str(DEFAULT_POLICY)
        ))).read_text())
        if self.data.get("version") != 1:
            raise ValueError("unsupported H3 capacity policy version")
        # Raising these bounds requires a reviewed policy/code change with evidence.
        for profile, maximum in (("preview", 3), ("quality", 2)):
            rule = self.data["short"][profile]
            if not 1 <= rule["max_parallel"] <= maximum or rule["max_frames"] > 124:
                raise ValueError("capacity exceeds the reviewed short-job evidence boundary")
        if self.data["long"]["max_parallel"] != 1:
            raise ValueError("long-job concurrency has not been validated")
        if (self.data["resources"].get("swap_idle_stable_seconds", 60) < 60
                or self.data["resources"].get("swap_hard_limit_gib", 8) > 8):
            raise ValueError("swap recovery exceeds its reviewed safety boundary")

    def demand(self, payload: dict[str, Any], profile: str) -> dict[str, Any]:
        """Use executed graph dimensions, never a client's duration/profile claim alone."""
        nodes = list(payload.get("prompt", {}).values())
        conditioning = [node for node in nodes if node.get("class_type") in {
            "MiniMaxH3AudioConditioningT8", "MiniMaxH3ImageToVideo"
        }]
        samplers = [node for node in nodes if node.get("class_type") in {
            "MiniMaxH3DualClockSamplerT8", "BasicScheduler"
        }]
        rule = self.data["short"][profile]
        demand = {"profile": profile, "class": "long", "known_shape": False}
        if (conditioning or samplers) and not len(conditioning) == len(samplers) == 1:
            raise ValueError("H3 workflow has ambiguous resource demand")
        if len(conditioning) == len(samplers) == 1:
            values = conditioning[0].get("inputs", {})
            shape = {"frame_count": values.get("length"), "width": values.get("width"),
                     "height": values.get("height"), "steps": samplers[0].get("inputs", {}).get("steps")}
            if not all(type(value) is int and value > 0 for value in shape.values()):
                raise ValueError("H3 workflow requires explicit frame_count, dimensions and steps")
            if all(type(value) is int and value > 0 for value in shape.values()):
                if shape["frame_count"] > 362:
                    raise ValueError("H3 frame_count exceeds the 15-second workflow boundary")
                maximum = (864, 480) if profile == "preview" else (1344, 768)
                if (sorted((shape["width"], shape["height"])) != sorted(maximum)
                        or shape["steps"] != rule["steps"]):
                    raise ValueError("H3 workflow dimensions or steps do not match its profile")
                expected = "MiniMaxH3AudioConditioningT8" if profile == "preview" else "MiniMaxH3ImageToVideo"
                if conditioning[0]["class_type"] != expected:
                    raise ValueError("H3 conditioning does not match its profile")
                contract = payload.get("extra_data", {}).get("h3", {}).get("contract") or {}
                if any(key in contract and contract[key] != value for key, value in shape.items()):
                    raise ValueError("H3 resource contract differs from the executed workflow")
                demand.update(shape, known_shape=True)
                if shape["frame_count"] <= rule["max_frames"]:
                    demand["class"] = "short"
        budget = rule if demand["class"] == "short" else self.data["long"]
        memory_budget = (budget.get("preview_memory_budget_gib", budget["memory_budget_gib"])
                         if profile == "preview" and demand["known_shape"] else budget["memory_budget_gib"])
        return {**demand, "memory_budget_bytes": memory_budget * GIB,
                "disk_budget_bytes": budget["disk_budget_gib"] * GIB}

    def rule(self, demand: dict[str, Any], experiment: dict[str, Any] | None = None) -> dict[str, Any]:
        if experiment:
            return {**self.data["long"], "max_parallel": experiment["max_parallel"],
                    "lanes": self.data["short"][experiment["profile"]]["lanes"]}
        return self.data["short"][demand["profile"]] if demand["class"] == "short" else self.data["long"]

    def blocked(self, demand: dict[str, Any], active: list[dict[str, Any]],
                snapshot: dict[str, Any], experiment: dict[str, Any] | None = None) -> str | None:
        if experiment and any(not self.matches_experiment(item, experiment) for item in [demand, *active]):
            return "validation_workload_mismatch"
        if active and ((demand["class"] == "long" and not experiment) or any(
            (item["class"] == "long" and not experiment) or item["profile"] != demand["profile"] for item in active
        )):
            return "exclusive_workload_active"
        if len(active) >= self.rule(demand, experiment)["max_parallel"]:
            return "capacity_full"
        if not snapshot.get("ok") or time.time() - snapshot.get("timestamp", 0) > 10:
            return "resource_telemetry_unavailable"
        limits = self.data["resources"]
        memory = demand["memory_budget_bytes"] + sum(item["memory_budget_bytes"] for item in active)
        disk = demand["disk_budget_bytes"] + sum(item["disk_budget_bytes"] for item in active)
        gates = (
            (snapshot["memory_available_bytes"] - memory < limits["min_available_ram_gib"] * GIB, "ram_headroom"),
            (snapshot["cgroup_current_bytes"] + memory > limits["max_cgroup_gib"] * GIB, "cgroup_headroom"),
            (max(snapshot["swap_used_bytes"], snapshot["cgroup_swap_bytes"]) >= limits.get("swap_hard_limit_gib", 8) * GIB, "swap_hard_limit"),
            (snapshot["root_available_bytes"] < limits["min_root_free_gib"] * GIB, "root_disk_headroom"),
            (snapshot["offload_available_bytes"] - disk < limits["min_offload_free_gib"] * GIB, "offload_disk_headroom"),
        )
        reason = next((reason for failed, reason in gates if failed), None)
        if reason:
            return reason
        recovery = snapshot.get("swap_recovery", {})
        if recovery.get("growth_bytes", 0) > limits["max_swap_gib"] * GIB:
            return "swap_growth_limit"
        baseline = recovery.get("baseline", {})
        if max(snapshot["swap_used_bytes"], snapshot["cgroup_swap_bytes"],
               baseline.get("host_bytes", 0), baseline.get("cgroup_bytes", 0)) > limits["max_swap_gib"] * GIB:
            if active:
                return "swap_recovery_single_only"
            if not demand.get("known_shape"):
                return "swap_recovery_known_shape_required"
            if not recovery.get("ready"):
                return "swap_pressure"
        return None

    @staticmethod
    def matches_experiment(demand: dict[str, Any], experiment: dict[str, Any]) -> bool:
        return bool(demand.get("known_shape") and demand.get("profile") == experiment["profile"]
                    and demand.get("frame_count") == experiment["frame_count"])

    @staticmethod
    def validate_experiment(experiment: dict[str, Any]) -> None:
        if not isinstance(experiment, dict) or set(experiment) != {"profile", "frame_count", "max_parallel"}:
            raise ValueError("experimental capacity requires profile, frame_count and max_parallel")
        if experiment["profile"] not in {"preview", "quality"}:
            raise ValueError("invalid experimental profile")
        if type(experiment["frame_count"]) is not int or not 125 <= experiment["frame_count"] <= 362:
            raise ValueError("experimental frame_count must be 125..362")
        maximum = 3 if experiment["profile"] == "preview" else 2
        if type(experiment["max_parallel"]) is not int or not 1 <= experiment["max_parallel"] <= maximum:
            raise ValueError("experimental concurrency exceeds available physical lanes")
