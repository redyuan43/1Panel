"""CPU-only counterfactual scheduling replay; never collects or controls a runtime."""
from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.throughput import GIB, RECIPES, plan_assignments


def _number(value, name, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _bytes(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {name}")
    return value


def validate_trace(document):
    trace = copy.deepcopy(document)
    if not isinstance(trace, dict) or type(trace.get("version")) is not int or trace["version"] != 1:
        raise ValueError("trace version must be 1")
    if trace.get("trace_kind") not in {"synthetic", "measured"}:
        raise ValueError("trace_kind must explicitly be synthetic or measured")
    _identifier(trace.get("provenance"), "provenance")
    resources = trace.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("explicit resources required")
    _bytes(resources.get("effective_workingset_bytes"), "effective_workingset_bytes")
    resources.setdefault("running_remaining_bytes", 0)
    resources.setdefault("global_margin_bytes", 2 * GIB)
    resources.setdefault("cgroup_limit_bytes", 72 * GIB)
    _bytes(resources["running_remaining_bytes"], "running_remaining_bytes")
    _bytes(resources["global_margin_bytes"], "global_margin_bytes", minimum=2 * GIB)
    _bytes(resources["cgroup_limit_bytes"], "cgroup_limit_bytes", minimum=1)
    if resources["cgroup_limit_bytes"] > 72 * GIB:
        raise ValueError("cannot raise the 72 GiB ceiling")
    trace.setdefault("stable_seconds", 60)
    _number(trace["stable_seconds"], "stable_seconds", minimum=60)
    trace.setdefault("protected_seconds", 900)
    _number(trace["protected_seconds"], "protected_seconds", minimum=900)
    backends = trace.get("backends")
    jobs = trace.get("jobs")
    if not isinstance(backends, list) or not backends or not isinstance(jobs, list) or not jobs:
        raise ValueError("nonempty backends and jobs required")
    known = {}
    for backend in backends:
        if not isinstance(backend, dict):
            raise ValueError("invalid backend")
        identifier = _identifier(backend.get("id"), "backend id")
        _identifier(backend.get("gpu_uuid"), "gpu_uuid")
        if identifier in known or type(backend.get("enabled")) is not bool:
            raise ValueError("duplicate backend or missing explicit enabled flag")
        qualified = backend.get("qualified_recipes")
        if (not isinstance(qualified, list)
                or any(not isinstance(recipe, str) or recipe not in RECIPES for recipe in qualified)
                or len(set(qualified)) != len(qualified)):
            raise ValueError("explicit known backend qualification required")
        known[identifier] = backend
    if len({backend["gpu_uuid"] for backend in backends}) > 3:
        raise ValueError("at most three physical GPUs")
    identifiers = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("invalid job")
        identifier = _identifier(job.get("prompt_id"), "prompt_id")
        if identifier in identifiers:
            raise ValueError("duplicate prompt_id")
        identifiers.add(identifier)
        if not isinstance(job.get("recipe_id"), str) or job["recipe_id"] not in RECIPES:
            raise ValueError("unknown recipe")
        _number(job.get("created_at"), "relative created_at")
        _bytes(job.get("candidate_budget_bytes"), "candidate_budget_bytes", minimum=18 * GIB)
        eligible = job.get("eligible_backend_ids")
        if (not isinstance(eligible, list) or not eligible
                or any(not isinstance(identifier, str) or identifier not in known for identifier in eligible)):
            raise ValueError("unknown or missing eligible backend; no fallback")
        if len(set(eligible)) != len(eligible):
            raise ValueError("duplicate eligible backend")
        if any(job["recipe_id"] not in known[identifier]["qualified_recipes"] for identifier in eligible):
            raise ValueError("recipe not qualified on requested backend")
        durations = job.get("duration_seconds_by_backend")
        if not isinstance(durations, dict) or set(durations) != set(eligible):
            raise ValueError("explicit duration required for exactly every eligible backend")
        for duration in durations.values():
            _number(duration, "duration_seconds", minimum=0)
            if duration == 0:
                raise ValueError("duration_seconds must be positive")
        job["eligible_gpu_uuids"] = sorted({known[identifier]["gpu_uuid"] for identifier in eligible})
        job["eta_by_backend"] = dict(durations)
    return trace


def synthetic_fixture():
    backends = [
        {"id": "fast", "gpu_uuid": "synthetic-16gb", "enabled": True, "qualified_recipes": ["A4", "B8"]},
        {"id": "main", "gpu_uuid": "synthetic-12gb-main", "enabled": True, "qualified_recipes": ["A4"]},
        {"id": "preview", "gpu_uuid": "synthetic-12gb-preview", "enabled": True, "qualified_recipes": ["A4"]},
    ]
    jobs = []
    for identifier, recipe, eligible, duration in (
        ("001-b8", "B8", ["fast"], 600), ("002-b8", "B8", ["fast"], 600),
        ("003-a4", "A4", ["fast", "main", "preview"], 900),
        ("004-a4", "A4", ["fast", "main", "preview"], 900),
    ):
        jobs.append({"prompt_id": identifier, "recipe_id": recipe, "created_at": 0,
                     "candidate_budget_bytes": 18 * GIB, "eligible_backend_ids": eligible,
                     "duration_seconds_by_backend": {backend: duration for backend in eligible}})
    return {"version": 1, "trace_kind": "synthetic",
            "provenance": "Invented CPU fixture; durations, memory and qualifications are not hardware evidence.",
            "resources": {"effective_workingset_bytes": 16 * GIB}, "backends": backends, "jobs": jobs}


def _projected(resources, active, assignment):
    running = resources["running_remaining_bytes"] + sum(task["candidate_budget_bytes"] for task in active)
    candidate = sum(item["job"]["candidate_budget_bytes"] for item in assignment)
    projected = resources["effective_workingset_bytes"] + running + candidate + resources["global_margin_bytes"]
    return projected, running


def _fifo(jobs, backends, feasible):
    if not jobs:
        return []
    head = min(jobs, key=lambda job: (job["created_at"], job["prompt_id"]))
    for backend in backends:
        assignment = [{"job": head, "backend": backend}]
        if backend["id"] in head["eligible_backend_ids"] and feasible(assignment):
            return assignment
    return []


def _run(trace, policy):
    pending = copy.deepcopy(trace["jobs"])
    resources = trace["resources"]
    active, completed, events = [], [], []
    now, quiet_since, peak_parallel = 0, 0, 0
    while pending or active:
        finished = sorted((task for task in active if task["finished_at"] <= now),
                          key=lambda task: task["prompt_id"])
        for task in finished:
            active.remove(task)
            completed.append(task)
            events.append({"event": "complete", "at": now, "prompt_id": task["prompt_id"]})
        if finished:
            quiet_since = now
        ready = [job for job in pending if job["created_at"] <= now]
        occupied = {task["gpu_uuid"] for task in active}
        idle = [backend for backend in trace["backends"]
                if backend["enabled"] and backend["gpu_uuid"] not in occupied]

        def feasible(assignment):
            return (len(assignment) == 1 and now - quiet_since >= trace["stable_seconds"]
                    and _projected(resources, active, assignment)[0] <= resources["cgroup_limit_bytes"])

        assignment = []
        if ready and now - quiet_since >= trace["stable_seconds"]:
            if policy == "strict_fifo":
                assignment = _fifo(ready, idle, feasible)
            else:
                assignment = plan_assignments(ready, idle, now=now, feasible=feasible,
                                              protected_seconds=trace["protected_seconds"])
        if assignment:
            projected, reserved = _projected(resources, active, assignment)
            item = assignment[0]
            job, backend = item["job"], item["backend"]
            duration = job["duration_seconds_by_backend"][backend["id"]]
            finish = now + duration
            if not math.isfinite(finish) or finish <= now:
                raise ValueError("duration cannot advance the replay clock finitely")
            record = {"prompt_id": job["prompt_id"], "recipe_id": job["recipe_id"],
                      "backend_id": backend["id"], "gpu_uuid": backend["gpu_uuid"],
                      "created_at": job["created_at"], "started_at": now, "finished_at": finish,
                      "duration_seconds": duration, "queue_wait_seconds": now - job["created_at"],
                      "candidate_budget_bytes": job["candidate_budget_bytes"]}
            pending.remove(job)
            active.append(record)
            peak_parallel = max(peak_parallel, len(active))
            events.append({"event": "start", "at": now, "prompt_id": job["prompt_id"],
                           "backend_id": backend["id"], "projected_bytes": projected,
                           "reserved_running_bytes": reserved, "parallel": len(active),
                           "stable_seconds": now - quiet_since})
            quiet_since = now
        if not pending and not active:
            break
        future = [task["finished_at"] for task in active if task["finished_at"] > now]
        future.extend(job["created_at"] for job in pending if job["created_at"] > now)
        if pending and quiet_since + trace["stable_seconds"] > now:
            future.append(quiet_since + trace["stable_seconds"])
        if not future:
            break
        now = min(future)
    waits = [task["queue_wait_seconds"] for task in completed]
    success = len(completed) == len(trace["jobs"])
    return {"policy": policy, "status": "completed" if success else "blocked",
            "total_jobs": len(trace["jobs"]), "completed_jobs": len(completed),
            "makespan_seconds": now if success else None, "elapsed_seconds": now,
            "completed_per_hour": len(completed) * 3600 / now if success else None,
            "queue_wait": {"completed_mean_seconds": sum(waits) / len(waits) if waits else None,
                           "completed_max_seconds": max(waits) if waits else None,
                           "by_job_seconds": {task["prompt_id"]: task["queue_wait_seconds"] for task in completed}},
            "peak_parallel": peak_parallel, "jobs": sorted(completed, key=lambda task: task["prompt_id"]),
            "pending": [{"prompt_id": job["prompt_id"], "waited_seconds": max(0, now - job["created_at"]),
                         "reason": "no_future_event_can_change_memory_or_capability_or_fifo_head"} for job in pending],
            "events": events}


def replay(document):
    trace = validate_trace(document)
    results = {policy: _run(trace, policy) for policy in ("strict_fifo", "throughput")}
    old, new = results["strict_fifo"], results["throughput"]
    completed = old["status"] == new["status"] == "completed"
    return {"version": 1, "trace_kind": trace["trace_kind"], "provenance": trace["provenance"],
            "result_kind": "counterfactual_replay_not_live_benchmark", "trace": trace,
            "trace_sha256": hashlib.sha256(json.dumps(trace, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "planner_source_sha256": hashlib.sha256(Path(inspect.getfile(plan_assignments)).read_bytes()).hexdigest(),
            "assumptions": ["Both policies share arrivals, qualifications, duration matrix, budgets and resource model.",
                "Initial admission and every ownership change require a new continuous stable window; one dispatch per window.",
                "Effective working set excludes replay jobs; their full peak budgets remain reserved until completion.",
                "No partial-progress budget release, cache reclaim, swap/PSI telemetry, runtime start or inference is modeled.",
                "Durations are supplied per backend including any desired load/decode cost, and do not change under overlap.",
                "Even measured duration inputs produce counterfactual, not measured concurrent throughput."],
            "results": results, "comparison": {
                "makespan_saved_seconds": old["makespan_seconds"] - new["makespan_seconds"] if completed else None,
                "modeled_speedup_ratio": old["makespan_seconds"] / new["makespan_seconds"] if completed else None}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace", type=Path, help="Explicit synthetic or measured-duration JSON trace")
    source.add_argument("--synthetic", action="store_true", help="Use the explicitly invented built-in fixture")
    args = parser.parse_args()
    try:
        if args.synthetic:
            result = replay(synthetic_fixture())
        else:
            raw = args.trace.read_bytes()
            result = replay(json.loads(raw))
            result["input_file_sha256"] = hashlib.sha256(raw).hexdigest()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"replay rejected: {error}\n")


if __name__ == "__main__":
    main()
