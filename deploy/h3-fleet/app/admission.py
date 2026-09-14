"""Conservative capacity policy and host resource admission for Ivan H3."""
from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any



BUSY = ("reserved", "reconciling", "submitted", "running", "cancelling")
GIB = 1024**3
DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "config/capacity.json"


def cgroup_limit_bytes(snapshot: dict, limits: dict) -> int:
    """Clamp the reviewed ceiling to this host while preserving its reserve."""
    configured = limits.get("max_cgroup_gib", 72)
    floor = limits.get("min_available_ram_gib", 16)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (configured, floor)):
        raise ValueError("invalid node memory limits")
    ceiling = int(min(72, configured) * GIB)
    total = snapshot.get("memory_total_bytes")
    if total is not None:
        if type(total) is not int or total <= max(16, floor) * GIB:
            raise ValueError("physical memory cannot preserve host reserve")
        ceiling = min(ceiling, int(total - max(16, floor) * GIB))
    return ceiling


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
            if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:")):
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
        memory_started = time.time()
        before_current = int((cgroup / "memory.current").read_text())
        memory_stat = dict((key, int(value)) for key, value in
                           (line.split() for line in (cgroup / "memory.stat").read_text().splitlines()))
        after_current = int((cgroup / "memory.current").read_text())
        if time.time() - memory_started > 1:
            raise ValueError("memory stat observation window exceeded")
        return {
            "timestamp": time.time(),
            "ok": True,
            "memory_available_bytes": memory["MemAvailable"],
            "memory_total_bytes": memory["MemTotal"],
            "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
            "root_available_bytes": shutil.disk_usage("/").free,
            "offload_available_bytes": shutil.disk_usage(os.environ.get(
                "H3_OFFLOAD_ROOT", "/mnt/ivan-ext4-offload"
            )).free,
            "cgroup_current_bytes": max(before_current, after_current),
            "memory_stat": memory_stat,
            "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
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
        self.last_pswpin: int | None = None
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
        signature = json.dumps({
            "boot_id": snapshot["boot_id"],
            "cgroup_events": snapshot["cgroup_events"],
            "pswpout": snapshot["swap_io_pages"]["pswpout"],
            "pressure_totals": {key: value for key, value in snapshot["memory_pressure"].items()
                                if key.endswith("_total")},
        }, sort_keys=True)
        changed = self.last_signature is not None and signature != self.last_signature
        pswpin = snapshot["swap_io_pages"]["pswpin"]
        counter_reset = self.last_pswpin is not None and pswpin < self.last_pswpin
        pressure = any(value > 0 for key, value in snapshot["memory_pressure"].items()
                       if key.endswith("_avg10"))
        self.last_signature = signature
        self.last_pswpin = pswpin
        if not idle:
            self.candidate = None
            self.was_busy = True
        elif self.was_busy:
            self.candidate = None
            # Retain the previous round's guard until a new quiet window is
            # complete, even if swapped pages have since been reclaimed.
            self.was_busy = False
        if changed or counter_reset or pressure:
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
        self.data.setdefault("max_active_jobs", 3)
        if type(self.data["max_active_jobs"]) is not int or not 1 <= self.data["max_active_jobs"] <= 3:
            raise ValueError("max_active_jobs must be an integer between 1 and 3")
        # Raising these bounds requires a reviewed policy/code change with evidence.
        for profile, maximum in (("preview", 3), ("quality", 2)):
            rule = self.data["short"][profile]
            if not 1 <= rule["max_parallel"] <= maximum or rule["max_frames"] > 124:
                raise ValueError("capacity exceeds the reviewed short-job evidence boundary")
        if self.data["long"]["max_parallel"] != 1:
            raise ValueError("long-job concurrency has not been validated")
        studio = self.data.get("studio_preview")
        if studio is not None:
            if (studio.get("max_frames") != 362 or studio.get("steps") != 4
                    or type(studio.get("max_parallel")) is not int or not 1 <= studio["max_parallel"] <= 3
                    or studio.get("lanes") != ["fast", "main", "preview"]
                    or studio.get("memory_budget_gib", 0) < 12 or studio.get("disk_budget_gib", 0) < 1
                    or not studio.get("evidence")):
                raise ValueError("Studio preview capacity requires bounded full-duration evidence")
        if (self.data["resources"].get("swap_idle_stable_seconds", 60) < 60
                or self.data["resources"].get("swap_hard_limit_gib", 8) > 8):
            raise ValueError("swap recovery exceeds its reviewed safety boundary")

    def demand(self, payload: dict[str, Any], profile: str) -> dict[str, Any]:
        """Use executed graph dimensions, never a client's duration/profile claim alone."""
        if payload.get("extra_data", {}).get("h3", {}).get("studio") is True:
            return self.studio_demand(payload, profile)
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
        if experiment and experiment["profile"] == "mixed":
            return {**self.data["long"], "max_parallel": 2,
                    "lanes": ["fast"] if demand["profile"] == "quality" else ["main"]}
        if experiment:
            return {**self.data["long"], "max_parallel": experiment["max_parallel"],
                    "lanes": self.data["short"][experiment["profile"]]["lanes"]}
        if self.parallel_studio_preview(demand):
            return self.data["studio_preview"]
        return self.data["short"][demand["profile"]] if demand["class"] == "short" else self.data["long"]

    def parallel_studio_preview(self, demand: dict[str, Any]) -> bool:
        rule = self.data.get("studio_preview")
        return bool(rule and demand.get("known_shape") and demand.get("profile") == "preview"
                    and demand.get("conditioning_type") == "MiniMaxH3AudioConditioningT8"
                    and demand.get("task_type") == "T2VA" and demand.get("audio_mode") == "native"
                    and demand.get("steps") == rule["steps"]
                    and type(demand.get("frame_count")) is int and 0 < demand["frame_count"] <= rule["max_frames"]
                    and sorted((demand.get("width", 0), demand.get("height", 0))) == [480, 864])

    def studio_preview_capacity(self, active: list[dict[str, Any]], snapshot: dict[str, Any],
                                idle_lanes: list[str], external_reason: str | None = None) -> dict[str, Any]:
        demand = self.studio_demand({"prompt": {
            "conditioning": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
                "width": 480, "height": 864, "length": 362, "task_type": "T2VA", "audio_mode": "native"}},
            "sampler": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {"steps": 4}},
        }}, "preview")
        rule = self.rule(demand)
        maximum = min(rule["max_parallel"], self.data["max_active_jobs"])
        result = {"max_parallel": maximum, "available": 0,
                  "validated_parallel": maximum > 1 and self.parallel_studio_preview(demand), "reason": external_reason}
        if external_reason:
            return result
        reserved = list(active)
        for lane in rule["lanes"]:
            if lane not in idle_lanes:
                continue
            if (snapshot.get("swap_recovery", {}).get("ready") and lane != "fast"
                    and max(snapshot.get("swap_used_bytes", 0), snapshot.get("cgroup_swap_bytes", 0))
                    > self.data["resources"]["max_swap_gib"] * GIB):
                result["reason"] = "swap_recovery_fast_only"
                continue
            reason = self.blocked(demand, reserved, snapshot)
            if reason:
                result["reason"] = reason
                break
            result["available"] += 1
            reserved.append(demand)
        if not result["available"] and not result["reason"]:
            result["reason"] = "no_eligible_lane"
        return result

    def studio_demand(self, payload: dict[str, Any], profile: str) -> dict[str, Any]:
        graph = payload["prompt"]
        conditioning = [node for node in graph.values() if node.get("class_type") in {
            "MiniMaxH3AudioConditioningT8", "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}]
        samplers = [node for node in graph.values() if node.get("class_type") in {
            "MiniMaxH3DualClockSamplerT8", "BasicScheduler"}]
        if len(conditioning) != 1 or len(samplers) != 1:
            raise ValueError("Studio graph must contain one bounded H3 conditioning and sampler")
        values = conditioning[0]["inputs"]
        frames = values.get("length")
        dynamic = isinstance(frames, list)
        if dynamic:
            if len(frames) != 2 or frames[1] != 1:
                raise ValueError("Unsupported Studio duration link")
            window = graph.get(str(frames[0]), {})
            inputs = window.get("inputs", {})
            duration = inputs.get("scene_duration_seconds")
            if (window.get("class_type") != "MiniMaxH3AudioWindowT8"
                    or type(duration) not in {int, float} or not 0 < duration <= 362 / 24
                    or inputs.get("warmup_seconds", 0) != 0 or inputs.get("cooldown_seconds", 0) != 0):
                raise ValueError("Unbounded Studio audio window")
            frames = 362
        width, height = values.get("width"), values.get("height")
        steps = samplers[0]["inputs"].get("steps")
        if not all(type(value) is int and value > 0 for value in (frames, width, height, steps)) or frames > 362:
            raise ValueError("Studio graph has invalid dimensions or frame count")
        dimensions = sorted((width, height))
        if profile == "preview":
            if (dimensions != [480, 864] or steps not in {4, 6}
                    or samplers[0]["class_type"] != "MiniMaxH3DualClockSamplerT8"
                    or conditioning[0]["class_type"] != "MiniMaxH3AudioConditioningT8"):
                raise ValueError("Unsupported Studio turbo profile")
        elif profile != "quality" or dimensions not in ([480, 864], [768, 1344]) or steps != 14:
            raise ValueError("Unsupported Studio quality profile")
        standard_short = (not dynamic and frames <= 124 and (profile == "preview" or (
            dimensions == [768, 1344] and conditioning[0]["class_type"] == "MiniMaxH3ImageToVideo")))
        demand = {"profile": profile, "class": "short" if standard_short else "long", "known_shape": True,
                  "frame_count": frames, "width": width, "height": height, "steps": steps,
                  "conditioning_type": conditioning[0]["class_type"], "task_type": values.get("task_type"),
                  "audio_mode": values.get("audio_mode")}
        rule = self.rule(demand)
        memory_budget = rule.get("preview_memory_budget_gib", rule["memory_budget_gib"]) if profile == "preview" else rule["memory_budget_gib"]
        return {**demand,
                "memory_budget_bytes": memory_budget * GIB,
                "disk_budget_bytes": rule["disk_budget_gib"] * GIB}

    def blocked(self, demand: dict[str, Any], active: list[dict[str, Any]],
                snapshot: dict[str, Any], experiment: dict[str, Any] | None = None) -> str | None:
        if len(active) >= self.data["max_active_jobs"]:
            return "capacity_full"
        if experiment and any(not self.matches_experiment(item, experiment) for item in [demand, *active]):
            return "validation_workload_mismatch"
        mixed = bool(experiment and experiment["profile"] == "mixed")
        if mixed and any(item["profile"] == demand["profile"] for item in active):
            return "mixed_profile_slot_full"
        parallel_preview = not experiment and all(self.parallel_studio_preview(item) for item in [demand, *active])
        if not mixed and not parallel_preview and active and ((demand["class"] == "long" and not experiment) or any(
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
            (snapshot["cgroup_current_bytes"] + memory > cgroup_limit_bytes(snapshot, limits), "cgroup_headroom"),
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
        if experiment["profile"] == "mixed":
            dimensions = sorted((demand.get("width", 0), demand.get("height", 0)))
            return bool(demand.get("known_shape") and (
                (demand.get("profile") == "quality" and demand.get("frame_count") == experiment["frame_count"]
                 and dimensions == [768, 1344] and demand.get("steps") == 14)
                or (demand.get("profile") == "preview" and demand.get("frame_count") == experiment.get("preview_frame_count", 124)
                    and dimensions == [480, 864] and demand.get("steps") in {4, 6})))
        return bool(demand.get("known_shape") and demand.get("profile") == experiment["profile"]
                    and demand.get("frame_count") == experiment["frame_count"])

    @staticmethod
    def validate_experiment(experiment: dict[str, Any]) -> None:
        required = {"profile", "frame_count", "max_parallel"}
        if (not isinstance(experiment, dict) or not required <= set(experiment)
                or set(experiment) - required - {"preview_frame_count"}):
            raise ValueError("experimental capacity requires profile, frame_count and max_parallel")
        if experiment["profile"] not in {"preview", "quality", "mixed"}:
            raise ValueError("invalid experimental profile")
        if type(experiment["frame_count"]) is not int or not 125 <= experiment["frame_count"] <= 362:
            raise ValueError("experimental frame_count must be 125..362")
        maximum = 3 if experiment["profile"] == "preview" else 2
        if experiment["profile"] == "mixed" and experiment["max_parallel"] != 2:
            raise ValueError("mixed validation requires exactly one quality and one preview lane")
        if type(experiment["max_parallel"]) is not int or not 1 <= experiment["max_parallel"] <= maximum:
            raise ValueError("experimental concurrency exceeds available physical lanes")
        if "preview_frame_count" in experiment:
            frames = experiment["preview_frame_count"]
            if experiment["profile"] != "mixed" or type(frames) is not int or frames not in {124, 362}:
                raise ValueError("mixed preview must use a reviewed 124 or 362 frame shape")
