"""Opt-in, pure-CPU residual-zram policy; never collects telemetry or changes state.

ProgressiveResourcePolicy(profile="residual_zram", limits=resource_limits)
  .observe(sample, active=False, additional_memory_bytes=..., additional_disk_bytes=...)
returns admission="allow"/"wait", fatal, reasons, stable_seconds and baseline.
active means tasks ALREADY OWNED by this coordinator, not another runner's C05.
Do not hot-adopt a running task/lease. Only an idle, continuously sampled 60s
window can establish the first baseline; that baseline never gets rebased.
Missing headroom delays admission but is NOT a reason to cancel existing work.

This module does not implement/replace generic policy. profile must explicitly
equal residual_zram. Absolute logical swap <=8GiB and net baseline growth <=1GiB
apply independently to host and cgroup; compressed RAM is not subtracted from
either swap or MemAvailable. Admission requires zero PSI avg10 and no PSI total
growth for 60s, including after a transient pressure event. Experimental fatal
PSI thresholds are per metric: full_avg10>=1% or some_avg10>=10%, sustained for
>=15s across >=4 distinct valid timestamps while active. Small/transient PSI
only delays admission. Duplicate timestamps do not advance pressure windows;
gaps reset them (existing active telemetry-gap protection remains fatal).
These thresholds add trial protection, not hardware certification or a change
to production policy. pswpin growth alone is harmless;
pswpout growth resets idle qualification / delays admission while active, but
without PSI or a hard-limit breach does not itself cancel an owned task.

sample uses validate_capacity.sample_host fields plus:
swap_inventory = {
  "observed_at": epoch, "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
  "proc_swaps": complete /proc/swaps text,
  "devices": [{"path": "/dev/zram0", "disksize_bytes": integer,
               "backing_dev": "none", "mm_stat": raw sysfs string,
               "bd_stat": raw sysfs string}]
}
Collector: read /proc/swaps, then only its /dev/zramN devices' sysfs
/sys/block/zramN/{disksize,backing_dev,mm_stat,bd_stat}; never execute shell
fragments from device paths. Enumerate every configured swap. Disk/file swaps
are allowed ONLY while Used=0 in the baseline AND every subsequent sample;
their path/type/size/priority are frozen as part of the inventory identity.
Any disk/file Used>0 rejects idle admission / is fatal for owned active work.
No swapoff, swappiness change, reset or permission to start disk swapping is
implied. Configured is not the same as in-use. Only in-use zram devices require
backing_dev=none and all-zero bd_stat; backing-counter increases are rejected
even on a currently unused device. Inventory devices lists ONLY /dev/zramN,
not disk/file devices; the complete proc_swaps text still lists all of them.
Reports expose configured_unused_disk_swap and disk_io_guaranteed_absent=False:
polling Used=0 cannot prove that no transient disk I/O occurred between samples.
No boolean "verified"
substitute is accepted. These are caller-supplied raw observations, not remote
cryptographic attestation. Collector errors must remain errors, not empty lists.
Sampling gaps >15s or evidence older than 10s invalidate continuity. Pass now
only for deterministic offline replay/tests; live callers leave it omitted.
"""
from __future__ import annotations

import copy
import math
import re
import time


GIB = 1024**3
STABLE_SECONDS = 60
MAX_GAP_SECONDS = 15
MAX_AGE_SECONDS = 10
ABSOLUTE_SWAP_BYTES = 8 * GIB
GROWTH_SWAP_BYTES = GIB
PSI_FULL_PERCENT = 1.0
PSI_SOME_PERCENT = 10.0
PSI_FATAL_SECONDS = 15
PSI_FATAL_SAMPLES = 4
UNITS = {f"comfyui-h3@{lane}.service" for lane in ("fast", "main", "preview")}
PRESSURE = ("host_some", "host_full", "cgroup_some", "cgroup_full")
EVENTS = ("high", "max", "oom", "oom_kill")
DEFAULT_LIMITS = {"min_available_ram_gib": 16, "max_cgroup_gib": 72,
                  "min_root_free_gib": 25, "min_offload_free_gib": 40}


class EvidenceError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


def integer(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in {float, int} and math.isfinite(value)


def parse_inventory(sample, now):
    inventory = sample["swap_inventory"]
    stamp, page = inventory["observed_at"], inventory["page_size_bytes"]
    require(number(stamp) and 0 <= now - stamp <= MAX_AGE_SECONDS
            and abs(stamp - sample["timestamp"]) <= MAX_AGE_SECONDS, "inventory_stale")
    require(type(page) is int and page in {4096, 16384, 65536}, "inventory_page_size_unknown")
    lines = inventory["proc_swaps"].strip().splitlines()
    require(bool(lines) and lines[0].split() == ["Filename", "Type", "Size", "Used", "Priority"], "proc_swaps_header_invalid")
    swaps = {}
    for line in lines[1:]:
        fields = line.split()
        require(len(fields) == 5, "proc_swaps_row_invalid")
        path, kind, capacity, used, priority = fields
        is_zram = re.fullmatch(r"/dev/zram[0-9]+", path) is not None
        require(path.startswith("/") and kind in {"file", "partition"}
                and (not is_zram or kind == "partition"), "swap_device_type_unknown")
        require(path not in swaps and capacity.isdecimal() and used.isdecimal()
                and re.fullmatch(r"-?[0-9]+", priority) is not None, "proc_swaps_row_invalid")
        capacity, used = int(capacity) * 1024, int(used) * 1024
        require(0 <= used <= capacity and capacity > 0, "proc_swaps_sizes_invalid")
        require(is_zram or used == 0, "disk_swap_in_use")
        swaps[path] = {"capacity": capacity, "used": used, "type": kind, "priority": int(priority), "zram": is_zram}
    devices = inventory["devices"]
    zram_paths = {path for path, row in swaps.items() if row["zram"]}
    require(bool(zram_paths) and isinstance(devices, list) and len(devices) == len(zram_paths), "inventory_incomplete")
    seen, topology = set(), []
    backing_counters = {}
    physical = 0
    for device in devices:
        path, size = device["path"], device["disksize_bytes"]
        require(path in zram_paths and path not in seen, "inventory_devices_mismatch")
        seen.add(path)
        require(integer(size) and size > 0 and 0 <= size - swaps[path]["capacity"] <= page, "inventory_capacity_mismatch")
        backing = device["backing_dev"].strip()
        require(backing == "none" or re.fullmatch(r"[0-9]+:[0-9]+", backing) is not None, "zram_backing_unknown")
        stats = device["mm_stat"].split()
        backing_stats = device["bd_stat"].split()
        require(len(stats) >= 7 and all(value.isdecimal() for value in stats), "zram_mm_stat_invalid")
        require(len(backing_stats) == 3 and all(value.isdecimal() for value in backing_stats), "zram_backing_io_unknown")
        if swaps[path]["used"]:
            require(backing == "none", "zram_backing_present_or_unknown")
            require(all(int(value) == 0 for value in backing_stats), "zram_backing_io_present_or_unknown")
        backing_counters[path] = tuple(map(int, backing_stats))
        original, compressed, memory = map(int, stats[:3])
        require(original <= size and compressed <= memory, "zram_memory_accounting_invalid")
        physical += memory
        topology.append((path, size, backing))
    used_total = sum(row["used"] for row in swaps.values())
    require(abs(used_total - sample["swap_used_bytes"]) <= page * len(zram_paths), "host_swap_inventory_mismatch")
    require(sample["cgroup_swap_bytes"] <= sample["swap_used_bytes"] + page * len(zram_paths), "cgroup_swap_exceeds_host")
    configured = sorted((path, row["type"], row["capacity"], row["priority"]) for path, row in swaps.items())
    unused_disk = [{"path": path, "type": row["type"], "size_bytes": row["capacity"], "priority": row["priority"], "used_bytes": 0}
                   for path, row in sorted(swaps.items()) if not row["zram"]]
    return {"topology": {"configured": configured, "zram": sorted(topology)}, "logical_used_bytes": used_total,
            "physical_memory_bytes": physical, "backing_counters": backing_counters,
            "configured_unused_disk_swap": unused_disk, "disk_io_guaranteed_absent": False}


def inspect(sample, now):
    require(sample.get("ok") is True and number(sample["timestamp"])
            and number(now) and 0 <= now - sample["timestamp"] <= MAX_AGE_SECONDS, "telemetry_stale_or_unavailable")
    require(isinstance(sample["boot_id"], str) and bool(sample["boot_id"]), "boot_identity_missing")
    for key in ("memory_available_bytes", "cgroup_current_bytes", "swap_used_bytes", "cgroup_swap_bytes",
                "root_available_bytes", "offload_available_bytes"):
        require(integer(sample[key]), "memory_telemetry_invalid")
    for key in EVENTS:
        require(integer(sample["cgroup_events"][key]), "cgroup_event_telemetry_invalid")
    for key in ("pswpin", "pswpout"):
        require(integer(sample["swap_io_pages"][key]), "swap_counter_invalid")
    for key in PRESSURE:
        require(integer(sample["memory_pressure"][key + "_total"]), "psi_counter_invalid")
        average = sample["memory_pressure"][key + "_avg10"]
        require(number(average) and 0 <= average <= 100, "psi_average_invalid")
    services = sample["services"]
    require(isinstance(services, list) and len(services) == 3
            and {unit["Id"] for unit in services} == UNITS, "production_inventory_incomplete")
    identities = {}
    for unit in services:
        pid, restarts = str(unit["MainPID"]), str(unit["NRestarts"])
        require(unit["ActiveState"] == "active" and pid.isdecimal() and int(pid) > 0
                and restarts.isdecimal(), "production_worker_not_active")
        identities[unit["Id"]] = (pid, restarts)
    require(len({pid for pid, _ in identities.values()}) == 3, "production_pid_alias")
    require(isinstance(sample["kernel_alerts"], list), "kernel_telemetry_missing")
    require(isinstance(sample["gpus"], list) and bool(sample["gpus"]), "gpu_telemetry_missing")
    for gpu in sample["gpus"]:
        require(isinstance(gpu["uuid"], str) and bool(gpu["uuid"])
                and number(gpu["temperature_c"]) and gpu["temperature_c"] >= 0, "gpu_telemetry_invalid")
    inventory = parse_inventory(sample, now)
    return {"boot_id": sample["boot_id"], "production": identities, "swap_topology": inventory["topology"]}, inventory


class ProgressiveResourcePolicy:
    def __init__(self, *, profile, limits=None):
        if profile != "residual_zram":
            raise ValueError("residual_zram requires explicit opt-in; generic policy is unchanged")
        self.profile = profile
        self.limits = {}
        for key, default in DEFAULT_LIMITS.items():
            value = (limits or {}).get(key, default)
            if not number(value) or value <= 0:
                raise ValueError("invalid resource limit: " + key)
            self.limits[key] = min(value, default) if key.startswith("max_") else max(value, default)
        self.baseline = None
        self.identity = None
        self.previous = None
        self.previous_inventory = None
        self.candidate = None
        self.failure = None
        self.psi_windows = {}
        self.quiet_since = None

    def decision(self, reasons, *, fatal=False, stable=0, inventory=None):
        if fatal:
            self.failure = list(reasons)
        reasons = self.failure or reasons
        return {"profile": self.profile, "admission": "wait" if reasons else "allow",
                "fatal": bool(self.failure), "reasons": list(reasons), "stable_seconds": stable,
                "baseline": copy.deepcopy(self.baseline), "inventory": copy.deepcopy(inventory),
                "absolute_swap_limit_bytes": ABSOLUTE_SWAP_BYTES, "swap_growth_limit_bytes": GROWTH_SWAP_BYTES,
                "experimental": True, "hardware_certified": False,
                "policy": {"psi_admission": {"avg10_percent": 0, "total_growth_us": 0, "stable_seconds": STABLE_SECONDS},
                           "psi_fatal": {"full_avg10_percent": PSI_FULL_PERCENT, "some_avg10_percent": PSI_SOME_PERCENT,
                                         "sustained_seconds": PSI_FATAL_SECONDS, "minimum_unique_samples": PSI_FATAL_SAMPLES,
                                         "maximum_sample_gap_seconds": MAX_GAP_SECONDS, "per_metric": True},
                           "resource_limits": copy.deepcopy(self.limits)},
                "psi_windows": copy.deepcopy(self.psi_windows)}

    def pressure_window(self, sample, *, active, delta):
        if not active or (delta is not None and (delta < 0 or delta > MAX_GAP_SECONDS)):
            self.psi_windows.clear()
            return []
        if delta == 0:
            return []
        exceeded = []
        for key in PRESSURE:
            threshold = PSI_FULL_PERCENT if key.endswith("_full") else PSI_SOME_PERCENT
            if sample["memory_pressure"][key + "_avg10"] < threshold:
                self.psi_windows.pop(key, None)
                continue
            window = self.psi_windows.setdefault(key, {"since": sample["timestamp"], "samples": 0, "duration_seconds": 0})
            window["samples"] += 1
            window["duration_seconds"] = sample["timestamp"] - window["since"]
            if window["samples"] >= PSI_FATAL_SAMPLES and window["duration_seconds"] >= PSI_FATAL_SECONDS:
                exceeded.append("sustained_memory_psi_pressure:" + key)
        return exceeded

    def observe(self, sample, *, active, additional_memory_bytes=0, additional_disk_bytes=0, now=None):
        if type(active) is not bool or not integer(additional_memory_bytes) or not integer(additional_disk_bytes):
            raise ValueError("active must be bool; additional budgets must be nonnegative integer bytes")
        if self.failure:
            return self.decision(self.failure)
        now = time.time() if now is None else now
        try:
            identity, inventory = inspect(sample, now)
        except EvidenceError as error:
            self.candidate = None
            self.psi_windows.clear()
            self.quiet_since = None
            return self.decision([str(error)], fatal=active)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            self.candidate = None
            self.psi_windows.clear()
            self.quiet_since = None
            return self.decision(["unverifiable_telemetry:" + str(error)], fatal=active)
        previous = self.previous
        previous_inventory = self.previous_inventory
        self.previous = copy.deepcopy(sample)
        self.previous_inventory = copy.deepcopy(inventory)
        if self.identity is None:
            self.identity = identity
        hard, transient = [], []
        if identity != self.identity:
            hard.append("boot_production_pid_or_swap_topology_drift")
        if previous_inventory and inventory["backing_counters"] != previous_inventory["backing_counters"]:
            hard.append("zram_backing_counter_changed")
        if sample["kernel_alerts"]:
            hard.append("kernel_oom_or_xid")
        if any(gpu["temperature_c"] >= 90 for gpu in sample["gpus"]):
            hard.append("gpu_temperature_ceiling")
        pressure = any(sample["memory_pressure"][key + "_avg10"] > 0 for key in PRESSURE)
        delta = sample["timestamp"] - previous["timestamp"] if previous else None
        if previous:
            if delta == 0:
                transient.append("duplicate_sample_timestamp")
            elif not 0 < delta <= MAX_GAP_SECONDS:
                transient.append("sampling_continuity_lost")
                if active:
                    hard.append("sampling_continuity_lost")
            for key in EVENTS:
                if sample["cgroup_events"][key] != previous["cgroup_events"][key]:
                    hard.append("cgroup_event_changed:" + key)
            for key in PRESSURE:
                current, before = sample["memory_pressure"][key + "_total"], previous["memory_pressure"][key + "_total"]
                if current < before:
                    hard.append("psi_counter_reset")
                pressure = pressure or current > before
            for key in ("pswpin", "pswpout"):
                if sample["swap_io_pages"][key] < previous["swap_io_pages"][key]:
                    hard.append("swap_counter_reset:" + key)
            if sample["swap_io_pages"]["pswpout"] > previous["swap_io_pages"]["pswpout"]:
                transient.append("new_swapout")
            if not self.baseline and any(sample[key] > previous[key] for key in ("swap_used_bytes", "cgroup_swap_bytes")):
                transient.append("residual_swap_not_stable")
        if pressure:
            transient.append("memory_psi_pressure")
        hard.extend(self.pressure_window(sample, active=active, delta=delta))
        if pressure or any(reason != "duplicate_sample_timestamp" for reason in transient):
            self.quiet_since = None
        elif delta != 0 and self.quiet_since is None:
            self.quiet_since = sample["timestamp"]
        if max(sample["swap_used_bytes"], sample["cgroup_swap_bytes"]) > ABSOLUTE_SWAP_BYTES:
            hard.append("absolute_swap_limit")
        if self.baseline and max(sample["swap_used_bytes"] - self.baseline["host_bytes"],
                                 sample["cgroup_swap_bytes"] - self.baseline["cgroup_bytes"]) > GROWTH_SWAP_BYTES:
            hard.append("swap_growth_limit")
        if sample["cgroup_current_bytes"] > self.limits["max_cgroup_gib"] * GIB:
            hard.append("aggregate_cgroup_hard_ceiling")
        if hard:
            self.candidate = None
            self.quiet_since = None
            return self.decision(hard, fatal=active, inventory=inventory)
        stable = 0 if self.quiet_since is None else sample["timestamp"] - self.quiet_since
        if not self.baseline:
            if active or transient:
                self.candidate = None
                transient.append("baseline_requires_idle_stable_window")
            else:
                if self.candidate is None:
                    self.candidate = {"timestamp": sample["timestamp"], "host_bytes": sample["swap_used_bytes"],
                                      "cgroup_bytes": sample["cgroup_swap_bytes"], "identity": copy.deepcopy(identity)}
                stable = sample["timestamp"] - self.candidate["timestamp"]
                if stable >= STABLE_SECONDS:
                    self.baseline = copy.deepcopy(self.candidate)
                else:
                    transient.append("awaiting_60s_stable_baseline")
        if self.baseline and stable < STABLE_SECONDS:
            transient.append("awaiting_60s_zero_psi_admission")
        if sample["memory_available_bytes"] - additional_memory_bytes < self.limits["min_available_ram_gib"] * GIB:
            transient.append("admission_ram_headroom")
        if sample["cgroup_current_bytes"] + additional_memory_bytes > self.limits["max_cgroup_gib"] * GIB:
            transient.append("admission_cgroup_headroom")
        if sample["root_available_bytes"] < self.limits["min_root_free_gib"] * GIB:
            transient.append("admission_root_disk_headroom")
        if sample["offload_available_bytes"] - additional_disk_bytes < self.limits["min_offload_free_gib"] * GIB:
            transient.append("admission_offload_disk_headroom")
        return self.decision(list(dict.fromkeys(transient)), stable=stable, inventory=inventory)
