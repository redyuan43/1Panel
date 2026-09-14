"""Offline, source-pinned measured memory budgets; no network or GPU operations.

See measured_peak_history.md for the local collector contract. A JSON assertion
of verification is not trusted: load_history reopens and hashes source files.
The pure resolver accepts only histories returned by this module's collectors.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


GIB = 1024**3
STATIC_FALLBACK = 18 * GIB
_VERIFIED = object()


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _integer(value, name):
    _require(type(value) is int and value >= 0, name + " must be nonnegative integer")
    return value


def _time(value):
    _require(type(value) in (float, int) and math.isfinite(value) and value > 0,
             "invalid timestamp")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    _require(isinstance(value, str) and len(value) == 64
             and all(character in "0123456789abcdef" for character in value), "invalid SHA256")
    return value


def _profile(value):
    if isinstance(value, str):
        return _json(_sha(value))
    _require(isinstance(value, dict), "profile_key must be an explicit object")
    for key in ("hardware", "precision", "weights"):
        _require(isinstance(value.get(key), dict) and value[key], "profile missing " + key)
    _require(bool(value["hardware"].get("gpu_uuid")), "profile missing GPU UUID")
    for key in ("width", "height", "frames", "fps"):
        _require(_integer(value.get(key), key) > 0, "invalid profile shape")
    _sha(value.get("input_workflow_sha256"))
    return _json(value)


def _budget(peak, margin_bytes, margin_ratio):
    _integer(margin_bytes, "margin_bytes")
    _require(type(margin_ratio) in (int, float) and math.isfinite(margin_ratio)
             and margin_ratio >= 0, "invalid margin_ratio")
    return peak + max(2 * GIB, (peak + 9) // 10, margin_bytes,
                      math.ceil(peak * margin_ratio))


@dataclass(frozen=True)
class VerifiedHistory:
    records: tuple[str, ...]
    errors: tuple[str, ...]
    _seal: object


def _read_source(descriptor, name):
    source = descriptor["files"][name]
    expected = _sha(source["sha256"])
    path = Path(source["path"])
    _require(path.is_absolute() and path.is_file(), name + " requires a local absolute file")
    digest = hashlib.sha256()
    chunks = []
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
            if name != "media":
                chunks.append(chunk)
    _require(total > 0 and digest.hexdigest() == expected, name + " source hash mismatch")
    return b"".join(chunks)


def _task(report, case):
    if "tasks" not in report:
        _require(report["case"] == case, "report case mismatch")
        return report
    matches = [task for task in report["tasks"] if task["case"] == case]
    _require(len(matches) == 1, "report requires exactly one matching case")
    return matches[0]


def _baseline(report, task):
    if "tasks" in report:
        reconciliation = task.get("resource_reconciliation", {})
        baseline = reconciliation.get("baseline_sample", task.get("admission_baseline"))
        _require(isinstance(baseline, dict), "missing task admission baseline; batch baseline forbidden")
        if "baseline_sample" in reconciliation and "admission_baseline" in task:
            _require(baseline == task["admission_baseline"], "conflicting task baselines")
        return baseline
    return report["baseline"]


def build_source_manifest(report_path, *, case, metrics_path, workflow_path, media_path):
    """Pin copied runner files locally; obtain binding/identity from the report.

    No invented profile or batch baseline. A remote path inside a report is
    never opened: every local input path is supplied explicitly by the caller.
    """
    paths = {"report": report_path, "metrics": metrics_path, "workflow": workflow_path, "media": media_path}
    files = {}
    for name, source_path in paths.items():
        path = Path(source_path).resolve(strict=True)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        files[name] = {"path": str(path), "sha256": digest.hexdigest()}
    report = json.loads(_read_source({"files": files}, "report"))
    task = _task(report, case)
    baseline = _baseline(report, task)
    multi = "tasks" in report
    isolated = baseline["perworker_subtree"][case]["identity"] if multi else baseline["isolated"]
    return {"schema_version": 1, "case": case, "run_id": report["run_id"], "prompt_id": task["prompt_id"],
            "profile_key": task["profile_key"], "profile_binding": task["profile_binding"],
            "worker_count": len(report["tasks"]) if multi else 1,
            "scope": "perworker_subtree" if multi else "single_worker_aggregate",
            "identity": {"boot_id": baseline["boot_id"], "isolated": isolated,
                         "cgroup_events": baseline["cgroup_events"]}, "files": files}


def _sample(raw, descriptor):
    sample = raw.get("sample", raw)
    identity = descriptor["identity"]
    _require(sample.get("boot_id") == identity["boot_id"], "sample boot identity mismatch")
    _require(sample.get("kernel_alerts") == [], "missing/unsafe kernel evidence")
    events = sample["cgroup_events"]
    for name in ("oom", "oom_kill"):
        _require(_integer(events.get(name), name) == identity["cgroup_events"][name],
                 "OOM event drift")
    if descriptor["scope"] == "single_worker_aggregate":
        _require(descriptor["worker_count"] == 1, "multiworker aggregate cannot be attributed")
        _require(sample.get("isolated") == identity["isolated"], "worker identity drift")
        current = sample["cgroup_current_bytes"]
    else:
        _require(descriptor["scope"] == "perworker_subtree", "unknown memory scope")
        worker = sample["perworker_subtree"][descriptor["case"]]
        _require(worker["identity"] == identity["isolated"], "subtree identity drift")
        _require(worker["case"] == descriptor["case"] and worker["prompt_id"] == descriptor["prompt_id"],
                 "subtree case/prompt mismatch")
        current = worker["memory_current_bytes"]
    return _time(sample["timestamp"]), _integer(current, "memory_current_bytes")


def collect_peak_run(descriptor, *, margin_bytes=2 * GIB, margin_ratio=0.1):
    """Verify pinned local files and return one immutable VerifiedHistory record.

    descriptor is the reviewed collector manifest, not a runner report. Unknown
    schemas raise ValueError; load_history converts unavailable sources to a
    resolver fallback. This function never synthesizes missing evidence.
    """
    try:
        _require(descriptor["schema_version"] == 1, "unknown source schema")
        profile_key = descriptor["profile_key"]
        _profile(profile_key)
        profile = profile_key
        if isinstance(profile_key, str):
            binding = descriptor["profile_binding"]
            _require(hashlib.sha256(_json(binding).encode()).hexdigest() == profile_key,
                     "profile binding digest mismatch")
            for name in ("runtime_profile_id", "case", "unit", "endpoint", "gpu_uuid"):
                _require(isinstance(binding.get(name), str) and binding[name], "binding missing " + name)
            _sha(binding["validator_sha256"])
            _require(binding["case"] == descriptor["case"], "binding case mismatch")
            isolated = descriptor["identity"]["isolated"]
            _require(binding["unit"] == isolated["Id"] and binding["endpoint"] == isolated["isolated_url"]
                     and binding["gpu_uuid"] == isolated["gpu_uuid"], "binding worker mismatch")
            for name in ("MainPID", "start_ticks"):
                value = isolated.get(name, isolated.get("process_start_ticks") if name == "start_ticks" else None)
                _require(str(value).isdigit() and int(value) > 0,
                         "missing runtime identity " + name)
            if "start_ticks" in isolated and "process_start_ticks" in isolated:
                _require(isolated["start_ticks"] == isolated["process_start_ticks"], "conflicting start ticks")
            shape = binding["shape"]
            profile = {"hardware": {"gpu_uuid": binding["gpu_uuid"]},
                       "input_workflow_sha256": binding["workflow_sha256"],
                       "width": shape["width"], "height": shape["height"],
                       "frames": shape["length"], "fps": shape["fps"]}
            for name in ("width", "height", "frames", "fps"):
                _require(_integer(profile[name], name) > 0, "invalid profile shape")
        for key in ("case", "run_id", "prompt_id"):
            _require(isinstance(descriptor[key], str) and descriptor[key], "missing " + key)
        count = _integer(descriptor["worker_count"], "worker_count")
        _require(count > 0, "worker_count must be positive")
        report = json.loads(_read_source(descriptor, "report"))
        selected_task = _task(report, descriptor["case"])
        expected_baseline = _baseline(report, selected_task)
        baseline = (json.loads(_read_source(descriptor, "baseline"))
                    if "baseline" in descriptor["files"] else expected_baseline)
        metrics = [json.loads(line) for line in _read_source(descriptor, "metrics").splitlines() if line.strip()]
        _read_source(descriptor, "media")
        json.loads(_read_source(descriptor, "workflow"))
        _require(descriptor["files"]["workflow"]["sha256"] == profile["input_workflow_sha256"],
                 "profile workflow mismatch (including trigger/weights)")
        _require(report["run_id"] == descriptor["run_id"], "report run mismatch")
        _require(report.get("lease_released") is True and not report.get("error")
                 and not report.get("reconciliation_error"), "unverified cleanup/lease")
        if "tasks" in report:
            _require(len(report["tasks"]) == count, "worker count mismatch")
            matches = [task for task in report["tasks"] if task["case"] == descriptor["case"]]
            _require(len(matches) == 1 and report.get("both_unloaded") is True,
                     "parallel cleanup/case mismatch")
            task = matches[0]
            accounting = task.get("resource_reconciliation", {})
            _require(accounting.get("state") == "finished" and accounting.get("case") == descriptor["case"]
                     and not accounting.get("observation_error") and not accounting.get("failure_samples"),
                     "unverified resource reconciliation/observation_error")
            _require(task.get("profile_key") == profile_key
                     and task.get("profile_binding") == descriptor.get("profile_binding"), "task profile mismatch")
            _require(report["status"] == "generated_pending_quality_review_drained",
                     "parallel run did not succeed")
            graph_sha = task["workflow_sha256"]
            gpu_uuid = task["gpu_uuid"]
        else:
            _require(count == 1 and report["status"] == "generated_pending_quality_review",
                     "single run did not succeed")
            _require(report.get("isolated_unload_confirmed") is True
                     and _integer(report.get("isolated_vram_mib_after_free"), "cleanup VRAM") <= 1024,
                     "unverified model unload")
            task = report
            graph_sha = report["input_workflow_sha256"]
            gpu_uuid = report["isolated_gpu_uuid"]
        _require(task["case"] == descriptor["case"] and task["prompt_id"] == descriptor["prompt_id"],
                 "report case/prompt mismatch")
        _require(bool(task.get("reconciliation")), "missing terminal reconciliation")
        _require(graph_sha == profile["input_workflow_sha256"]
                 and gpu_uuid == profile["hardware"]["gpu_uuid"], "report profile mismatch")
        _require(task["artifact_sha256"] == descriptor["files"]["media"]["sha256"], "media hash mismatch")
        media = task["media"]
        _require(media.get("ok") is True and bool(media.get("audio_codec")), "media not complete")
        for field, source in (("width", "width"), ("height", "height"), ("frames", "frame_count"), ("fps", "fps")):
            _require(media[source] == profile[field], "media shape mismatch")
        _require(abs(media["duration_seconds"] - profile["frames"] / profile["fps"]) <= 1 / profile["fps"],
                 "media duration mismatch")
        execution = task["execution"]
        started = _time(execution["started_at"])
        finished = _time(execution["finished_at"])
        submitted = _time(task["submitted_at"])
        cleanup = _time(report["finished_at"])
        _require(submitted <= started < finished <= cleanup, "execution/cleanup timestamps mismatch")
        _require(abs(execution["execution_seconds"] - (finished - started)) < 0.01,
                 "execution duration mismatch")
        _require(execution.get("sampler_nodes") and not set(execution["sampler_nodes"]) & set(execution["cached_nodes"]),
                 "sampler cached/missing")
        _require(baseline == expected_baseline, "baseline not bound to report")
        baseline_time, baseline_bytes = _sample(baseline, descriptor)
        if "tasks" in report:
            _require(accounting.get("started_at") == baseline_time
                     and accounting.get("boot_id") == baseline["boot_id"], "accounting baseline mismatch")
        _require(0 <= submitted - baseline_time <= 15, "baseline too old/after submission")
        _require(len(metrics) >= 2, "insufficient metrics")
        previous = baseline_time
        run_values = []
        last_run_time = None
        first_postcompletion = None
        for raw in metrics:
            raw_sample = raw.get("sample", raw)
            timestamp = _time(raw_sample["timestamp"])
            if timestamp < baseline_time:
                continue
            if timestamp == baseline_time:
                _require(raw_sample == baseline and previous == baseline_time, "duplicate/conflicting baseline")
                continue
            if previous > finished:
                break
            timestamp, current = _sample(raw, descriptor)
            _require(previous < timestamp <= cleanup and timestamp - previous <= 15,
                     "metrics timestamp duplicate/gap/outside run")
            previous = timestamp
            if submitted <= timestamp <= finished:
                run_values.append(current)
                last_run_time = timestamp
            elif timestamp > finished:
                first_postcompletion = {"timestamp": timestamp, "scope": descriptor["scope"],
                                        "memory_current_bytes": current, "delta_bytes": current - baseline_bytes}
        _require(run_values and last_run_time >= finished - 15 and previous >= finished,
                 "metrics do not cover complete execution")
        peak = max(run_values)
        _require(peak >= baseline_bytes, "negative peak delta cannot justify a smaller budget")
        delta = peak - baseline_bytes
        budget_delta = max(delta, first_postcompletion["delta_bytes"] if first_postcompletion else 0)
        accounting_peak = None
        if descriptor["scope"] == "perworker_subtree":
            accounting = task.get("resource_reconciliation", {})
            worker_peak = accounting.get("workers", {}).get(descriptor["case"], {})
            if "peak_delta_bytes" in worker_peak:
                measured = _integer(worker_peak["peak_delta_bytes"], "worker accounting peak delta")
                _require(worker_peak.get("baseline_bytes") == baseline_bytes,
                         "worker accounting baseline mismatch")
                _require(_integer(worker_peak.get("peak_bytes"), "worker accounting peak") == baseline_bytes + measured,
                         "worker accounting peak mismatch")
                observed_until = _time(accounting["last_observed_at"])
                _require(baseline_time <= observed_until <= cleanup, "worker accounting window mismatch")
                accounting_peak = {"scope": "perworker_subtree", "case": descriptor["case"],
                                   "started_at": baseline_time, "last_observed_at": observed_until,
                                   "peak_delta_bytes": measured}
                budget_delta = max(budget_delta, measured)
        successful_budget = 0
        if "peak_budget" in task:
            peak_budget = task["peak_budget"]
            _require(peak_budget.get("profile_key", profile_key) == profile_key, "successful budget profile mismatch")
            successful_budget = _integer(peak_budget["candidate_budget_bytes"], "successful candidate budget")
            _require(successful_budget > 0, "successful candidate budget must be positive")
        floor = max(_integer(descriptor.get("budget_floor_bytes", 0), "budget_floor_bytes"),
                    successful_budget, _budget(budget_delta, margin_bytes, margin_ratio))
        saved = json.loads(_json(descriptor))
        saved["budget_floor_bytes"] = floor
        record = {"case": descriptor["case"], "profile_key": profile_key, "run_id": descriptor["run_id"],
                  "prompt_id": descriptor["prompt_id"], "scope": descriptor["scope"],
                  "baseline_bytes": baseline_bytes, "peak_bytes": peak, "peak_delta_bytes": delta,
                  "budget_peak_delta_bytes": budget_delta, "successful_budget_floor_bytes": successful_budget,
                  "peak_evidence": {"during_execution": {"scope": descriptor["scope"], "started_at": submitted,
                                                        "finished_at": finished, "peak_delta_bytes": delta},
                                    "first_postcompletion": first_postcompletion, "worker_accounting": accounting_peak},
                  "baseline_timestamp": baseline_time, "execution_finished_at": finished,
                  "cleanup_finished_at": cleanup, "budget_floor_bytes": floor, "source": saved}
        return VerifiedHistory((_json(record),), (), _VERIFIED)
    except (KeyError, TypeError, OSError, UnicodeError) as error:
        raise ValueError("unverifiable source: " + str(error)) from error


def load_history(path):
    """Reverify every entry from disk; missing sources retain saved upper floors."""
    records, errors = [], []
    try:
        archive = json.loads(Path(path).read_text())
        _require(archive["schema_version"] == 1, "unknown history schema")
        for source in archive["sources"]:
            try:
                records.extend(collect_peak_run(source).records)
            except ValueError as error:
                errors.append(str(error))
                records.append(_json({"case": source.get("case"), "profile_key": source.get("profile_key"),
                                      "budget_floor_bytes": source.get("budget_floor_bytes", 0)}))
    except (ValueError, KeyError, TypeError, OSError) as error:
        errors.append(str(error))
    return VerifiedHistory(tuple(records), tuple(errors), _VERIFIED)


def resolve_budget(case, static_budget_bytes, history, profile_key, margin_bytes=2 * GIB, margin_ratio=0.1):
    """Pure budget resolution. Unknown/unverified evidence never reduces static."""
    fallback = _integer(static_budget_bytes, "static_budget_bytes")
    _require(fallback > 0, "static_budget_bytes must be positive")
    _budget(0, margin_bytes, margin_ratio)
    result = {"budget_bytes": fallback, "candidate_budget_bytes": fallback,
              "profile_key": profile_key, "provenance": [], "fallback": True,
              "reason": "no_verified_matching_history", "experimental": True, "hardware_certified": False}
    try:
        key = _profile(profile_key)
        _require(isinstance(case, str) and bool(case), "case is required")
        _require(isinstance(history, VerifiedHistory) and history._seal is _VERIFIED, "history_not_verified")
        matches = [json.loads(record) for record in history.records]
        matches = [record for record in matches if record["case"] == case and _json(record["profile_key"]) == key]
        floor = max([_integer(record["budget_floor_bytes"], "budget floor") for record in matches] + [0])
        result["budget_bytes"] = max(fallback, floor)
        _require(not history.errors, "source_verification_failed: " + "; ".join(history.errors))
        if not matches:
            result["candidate_budget_bytes"] = result["budget_bytes"]
            return result
        peak = max(record["peak_delta_bytes"] for record in matches)
        budget_peak = max(record["budget_peak_delta_bytes"] for record in matches)
        result.update(budget_bytes=max(floor, _budget(budget_peak, margin_bytes, margin_ratio)),
                      fallback=False, reason="verified_exact_case_profile", peak_delta_bytes=peak,
                      budget_peak_delta_bytes=budget_peak,
                      budget_floor_bytes=floor, provenance=matches)
    except (ValueError, KeyError, TypeError) as error:
        result["reason"] = str(error)
    result["candidate_budget_bytes"] = result["budget_bytes"]
    return result


def save_history(path, collected):
    """Append verified sources, keeping prior records and their nondecreasing floor.

    Single-writer offline CLI contract; parent owns locking/atomic distribution.
    """
    _require(isinstance(collected, VerifiedHistory) and collected._seal is _VERIFIED
             and not collected.errors, "cannot save unverified history")
    path = Path(path)
    archive = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "sources": []}
    _require(archive["schema_version"] == 1, "unknown history schema")
    for raw in collected.records:
        record = json.loads(raw)
        source = record["source"]
        for previous in archive["sources"]:
            if previous["case"] == record["case"] and previous["profile_key"] == record["profile_key"]:
                source["budget_floor_bytes"] = max(source["budget_floor_bytes"], previous["budget_floor_bytes"])
        archive["sources"].append(source)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("x") as handle:
        handle.write(_json(archive) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect", help="collect_peak_run from local pinned files only")
    sources = collect.add_mutually_exclusive_group(required=True)
    sources.add_argument("--from-local-files", type=Path, help="source manifest JSON")
    sources.add_argument("--from-report", type=Path, help="build manifest from copied runner report")
    for field in ("metrics", "workflow", "media"):
        collect.add_argument("--" + field, type=Path)
    collect.add_argument("--case")
    collect.add_argument("--history", type=Path, required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--history", type=Path, required=True)
    resolve.add_argument("--case", required=True)
    resolve.add_argument("--profile-key", type=Path, required=True)
    resolve.add_argument("--static-budget-bytes", type=int, default=STATIC_FALLBACK)
    for command in (collect, resolve):
        command.add_argument("--margin-bytes", type=int, default=2 * GIB)
        command.add_argument("--margin-ratio", type=float, default=0.1)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            if args.from_report:
                _require(all((args.case, args.metrics, args.workflow, args.media)),
                         "--from-report requires --case --metrics --workflow --media")
                descriptor = build_source_manifest(args.from_report, case=args.case, metrics_path=args.metrics,
                                                   workflow_path=args.workflow, media_path=args.media)
            else:
                descriptor = json.loads(args.from_local_files.read_text())
            history = collect_peak_run(descriptor,
                                       margin_bytes=args.margin_bytes, margin_ratio=args.margin_ratio)
            save_history(args.history, history)
            print(_json({"collected": True, "records": [json.loads(record) for record in history.records]}))
        else:
            print(_json(resolve_budget(args.case, args.static_budget_bytes, load_history(args.history),
                                       json.loads(args.profile_key.read_text()), args.margin_bytes, args.margin_ratio)))
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
