from __future__ import annotations

import copy
import itertools
import json
import math
from pathlib import Path
from typing import Any, Callable

from scripts.measured_peak_history import load_history, resolve_budget
from scripts.working_set_admission import projected_admission
from app.admission import cgroup_limit_bytes
from app.unified_memory import admission_reasons

GIB = 1024**3
RECIPES = ("A4", "A4_C0", "A4_C1", "B8")
PRESSURE = ("host_some", "host_full", "cgroup_some", "cgroup_full")


def remaining_peak(active: list[dict], sample: dict) -> int:
    total = 0
    for task in active:
        budget = task["candidate_budget_bytes"]
        baseline = task["worker_baseline_bytes"]
        current = sample["worker_memory_bytes"][task["backend_id"]]
        peak = max(task["observed_worker_peak_bytes"], current, baseline)
        if any(type(value) is not int or value < 0 for value in (budget, baseline, current, peak)):
            raise ValueError("invalid worker memory accounting")
        observed_delta = max(0, peak - baseline)
        if observed_delta > budget:
            raise ValueError("measured_worker_peak_exceeds_reserved_budget")
        remaining = max(0, budget - observed_delta)
        regrowth = max(0, peak - current)
        total += remaining + regrowth
    return total


class MemoryAdmission:
    def __init__(self, *, reclaim_factor: float = 0.5, stable_seconds: int = 60,
                 global_margin_bytes: int = 2 * GIB, residual_policy=None) -> None:
        if not math.isfinite(reclaim_factor) or not 0 <= reclaim_factor <= 1:
            raise ValueError("invalid reclaim_factor")
        if stable_seconds < 60 or global_margin_bytes < 2 * GIB:
            raise ValueError("cannot lower stability or safety margins")
        self.reclaim_factor = reclaim_factor
        self.stable_seconds = stable_seconds
        self.global_margin_bytes = global_margin_bytes
        self.previous = None
        self.quiet_since = None
        self.owners = None
        self.baseline = None
        self.residual_policy = residual_policy
        self.residual_decision = None

    def observe(self, sample: dict, owners: tuple, *, now: float) -> dict:
        result = copy.deepcopy(sample)
        reasons = []
        previous = self.previous
        if self.residual_policy is not None:
            self.residual_decision = self.residual_policy.observe(sample, active=bool(owners), now=now)
            reasons.extend(self.residual_decision["reasons"])
        required = {"timestamp", "boot_id", "memory_pressure", "swap_io_pages", "cgroup_events",
                    "swap_used_bytes", "cgroup_swap_bytes", "cgroup_current_bytes", "memory_stat"}
        try:
            if (not sample.get("ok") or not required <= sample.keys()
                    or not 0 <= now - sample["timestamp"] <= 10):
                raise ValueError("resource_telemetry_unavailable")
            if previous and not 0 <= sample["timestamp"] - previous["timestamp"] <= 15:
                reasons.append("telemetry_continuity_lost")
            if previous and sample["boot_id"] != previous["boot_id"]:
                reasons.append("boot_identity_changed")
                self.baseline = None
            pressure = sample["memory_pressure"]
            for category in PRESSURE:
                average, counter = pressure[category + "_avg10"], pressure[category + "_total"]
                if (type(average) not in (int, float) or not math.isfinite(average) or not 0 <= average <= 100
                        or type(counter) is not int or counter < 0):
                    raise ValueError("memory_pressure_unavailable")
                if average or (previous and counter != previous["memory_pressure"][category + "_total"]):
                    reasons.append("psi_not_stable")
            for field in ("swap_used_bytes", "cgroup_swap_bytes"):
                if type(sample[field]) is not int or sample[field] < 0:
                    raise ValueError("swap_telemetry_unavailable")
                if previous and sample[field] > previous[field]:
                    reasons.append("swap_growing")
            for counter in ("pswpin", "pswpout"):
                value = sample["swap_io_pages"][counter]
                if type(value) is not int or value < 0:
                    raise ValueError("swap_io_unavailable")
                if previous and (value < previous["swap_io_pages"][counter]
                                 or counter == "pswpout" and value > previous["swap_io_pages"][counter]):
                    reasons.append("swap_io_not_stable")
            for counter in ("oom", "oom_kill", "max", "high"):
                value = sample["cgroup_events"][counter]
                if type(value) is not int or value < 0:
                    raise ValueError("cgroup_events_unavailable")
                if previous and value != previous["cgroup_events"][counter]:
                    reasons.append("cgroup_events_changed")
            projected_admission(sample, 0, 0, 72 * GIB, reclaim_factor=self.reclaim_factor)
        except (KeyError, TypeError, ValueError) as error:
            reasons.append(str(error))
            self.previous = None
            self.quiet_since = None
            result["progressive"] = {"ready": False, "reasons": reasons, "stable_seconds": 0}
            return result
        window_only = {"awaiting_60s_stable_baseline", "awaiting_60s_zero_psi_admission"}
        if owners != self.owners:
            self.baseline = None
        if owners != self.owners or set(reasons) - window_only or self.quiet_since is None:
            self.quiet_since = sample["timestamp"]
        self.owners = owners
        self.previous = copy.deepcopy(sample)
        stable = max(0, sample["timestamp"] - self.quiet_since)
        if not owners and not reasons and stable >= self.stable_seconds:
            self.baseline = self.baseline or copy.deepcopy(sample)
        if stable < self.stable_seconds:
            reasons.append("awaiting_continuous_stable_window")
        result["progressive"] = {"ready": not reasons, "reasons": sorted(set(reasons)),
                                 "stable_seconds": stable, "baseline": self.baseline}
        return result

    def decide(self, sample: dict, candidate: dict, active: list[dict], *,
               limits: dict, now: float, vram_free_bytes: int | None) -> dict:
        reasons = list(sample.get("progressive", {}).get("reasons", ["stability_not_observed"]))
        result: dict[str, Any] = {"admission": "wait", "reasons": reasons,
                                  "reclaim_factor": self.reclaim_factor}
        try:
            if not sample.get("ok") or not 0 <= now - sample["timestamp"] <= 10:
                raise ValueError("resource_telemetry_unavailable")
            reserved = remaining_peak(active, sample)
            calculation = projected_admission(
                sample, candidate["candidate_budget_bytes"], reserved,
                cgroup_limit_bytes(sample, limits),
                global_margin=self.global_margin_bytes, reclaim_factor=self.reclaim_factor,
                swap_growing=any(reason in reasons for reason in ("swap_growing", "swap_io_not_stable")),
                psi_stable="psi_not_stable" not in reasons,
            )
            result.update(calculation)
            reasons.extend(calculation["reasons"])
            additional = reserved + candidate["candidate_budget_bytes"] + self.global_margin_bytes
            available = sample["memory_available_bytes"] - additional
            result.update(projected_cgroup_bytes=calculation["projected"],
                          projected_available_bytes=available, reserved_running_bytes=reserved,
                          candidate_budget_bytes=candidate["candidate_budget_bytes"])
            if available < max(16, limits["min_available_ram_gib"]) * GIB:
                reasons.append("ram_headroom")
            if sample["cgroup_current_bytes"] > cgroup_limit_bytes(sample, limits):
                reasons.append("raw_cgroup_hard_guard")
            swap = max(sample["swap_used_bytes"], sample["cgroup_swap_bytes"])
            if swap >= min(8, limits.get("swap_hard_limit_gib", 8)) * GIB:
                reasons.append("swap_hard_limit")
            if swap > min(1, limits["max_swap_gib"]) * GIB and self.residual_policy is None:
                recovery = sample.get("swap_recovery", {})
                if active or not recovery.get("ready") or candidate.get("lane_id") != "fast":
                    reasons.append("swap_recovery_single_fast_only")
            if self.residual_policy is not None:
                if not self.residual_decision or self.residual_decision["admission"] != "allow":
                    reasons.append("residual_zram_policy_not_ready")
                result["residual_zram_policy"] = copy.deepcopy(self.residual_decision)
            baseline = sample.get("progressive", {}).get("baseline") or sample
            if any(sample[field] - baseline[field] > GIB for field in ("swap_used_bytes", "cgroup_swap_bytes")):
                reasons.append("swap_growth_limit")
            if sample["root_available_bytes"] < max(25, limits["min_root_free_gib"]) * GIB:
                reasons.append("root_disk_headroom")
            disk = candidate.get("disk_budget_bytes", GIB) + sum(task.get("disk_budget_bytes", GIB) for task in active)
            if sample["offload_available_bytes"] - disk < max(40, limits["min_offload_free_gib"]) * GIB:
                reasons.append("offload_disk_headroom")
            unified = admission_reasons(candidate.get("unified_memory"), sample, candidate, active)
            if unified is not None:
                reasons.extend(unified)
            elif (type(vram_free_bytes) is not int or vram_free_bytes < candidate["vram_budget_bytes"]):
                reasons.append("gpu_vram_headroom")
        except (KeyError, TypeError, ValueError) as error:
            reasons.append("unverifiable_admission: " + str(error))
        result.update(admission="wait" if reasons else "allow", reasons=sorted(set(reasons)))
        return result


def candidate_budget(recipe_id: str, profile_key: str, static_bytes: int,
                     history_path: str | None, prior_floors: list[int]) -> dict:
    floor = max([18 * GIB, static_bytes, *prior_floors])
    history = load_history(history_path or "/nonexistent/h3-peak-history")
    result = resolve_budget(recipe_id, floor, history, profile_key)
    result["candidate_budget_bytes"] = result["budget_bytes"] = max(floor, result["candidate_budget_bytes"])
    return result


def estimate_seconds(observations: list[dict], recipe_id: str, gpu_uuid: str,
                     runtime_version: str, cold: bool, fallback: float = 1800) -> float:
    matching = [item["execution_seconds"] for item in observations
                if item.get("recipe_id") == recipe_id and item.get("gpu_uuid") == gpu_uuid
                and item.get("runtime_version") == runtime_version and item.get("cold") is cold
                and type(item.get("execution_seconds")) in (int, float)
                and math.isfinite(item["execution_seconds"]) and item["execution_seconds"] > 0]
    if not matching:
        return fallback
    if len(matching) < 5:
        return max(matching) * 1.2
    ordered = sorted(matching)
    return ordered[math.ceil(len(ordered) * 0.9) - 1]


def plan_assignments(jobs: list[dict], backends: list[dict], *, now: float,
                     feasible: Callable[[list[dict]], bool], protected_seconds: int = 900) -> list[dict]:
    if protected_seconds < 900:
        raise ValueError("waiting protection must be at least 15 minutes")
    ordered = sorted(jobs, key=lambda job: (job["created_at"], job["prompt_id"]))[:32]
    counts: dict[str, int] = {}
    candidates = []
    for job in ordered:
        recipe = job["recipe_id"]
        counts[recipe] = counts.get(recipe, 0) + 1
        if counts[recipe] <= 3:
            candidates.append(job)
    devices = sorted({backend["gpu_uuid"] for backend in backends})
    if len(devices) > 3:
        raise ValueError("scheduler is limited to three physical GPUs")
    options = []
    for gpu_uuid in devices:
        matches = []
        for job in candidates:
            for backend in backends:
                if backend["gpu_uuid"] == gpu_uuid and backend["id"] in job["eligible_backend_ids"]:
                    matches.append({"job": job, "backend": backend})
        options.append([None, *matches])
    best: list[dict] = []
    best_score = None
    protected = [job for job in candidates if now - job["created_at"] >= protected_seconds]
    for combination in itertools.product(*options):
        assignment = [item for item in combination if item is not None]
        identifiers = {item["job"]["prompt_id"] for item in assignment}
        if not assignment or len(identifiers) != len(assignment) or not feasible(assignment):
            continue
        protection = tuple(int(job["prompt_id"] in identifiers) for job in protected)
        scarce = sum(1 / max(1, len(item["job"].get("eligible_gpu_uuids", item["job"]["eligible_backend_ids"])))
                     for item in assignment)
        eta = max(item["job"].get("eta_by_backend", {}).get(item["backend"]["id"], 1800) for item in assignment)
        switches = sum(item["backend"].get("warm_recipe_id") != item["job"]["recipe_id"] for item in assignment)
        fifo = tuple(-candidates.index(item["job"]) for item in sorted(assignment, key=lambda item: candidates.index(item["job"])))
        score = (protection, scarce, len(assignment), -eta, -switches, fifo)
        if best_score is None or score > best_score:
            best, best_score = assignment, score
    return sorted(best, key=lambda item: (item["job"] not in protected,
                                          len(item["job"].get("eligible_gpu_uuids", item["job"]["eligible_backend_ids"])),
                                          item["job"]["created_at"], item["job"]["prompt_id"]))


def load_policy(path: Path) -> dict:
    value = json.loads(path.read_text())
    if value.get("version") != 1 or type(value.get("enabled")) is not bool:
        raise ValueError("unsupported recipe scheduling policy")
    if value.get("stable_seconds", 60) < 60 or value.get("idle_unload_seconds", 300) < 300:
        raise ValueError("cannot lower observation or idle lifecycle windows")
    if value.get("static_budget_gib", 18) < 18 or value.get("global_margin_gib", 2) < 2:
        raise ValueError("cannot lower recipe memory budget floors")
    return value
