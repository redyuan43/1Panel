"""Pure-data admission reconciliation; no collection, I/O, policy or calibration.

worker_baseline accepts worker_initial[case] bytes (or the full mapping).
Other baselines, when provided, come from admission.reservation.worker_initial_bytes.
Call observe BEFORE resource_check, so failing/OOM samples are preserved even
when sample.ok is false. This module does not invoke or replace the hard stop.
observe mutates/returns record; finish returns a detached finalized
snapshot and closes record. memory_stat is the aggregate memory.stat dictionary.
Page size comes from sample.page_size_bytes or swap_inventory.page_size_bytes;
never use this process's page size to interpret another host's counters.
Peaks are sampled high-water marks, not unsampled kernel memory.peak values.
All aggregate changes include concurrent work and cannot be attributed to case.
pgsteal deltas count cumulative reclaimed pages, potentially including repeated
reclaim of the same memory and anonymous pages; they are not net file-cache
release or unique bytes freed. Missing memory.stat is telemetry absence only.
"""
from __future__ import annotations

import copy
import math
import re


PRESSURE = ("host_some", "host_full", "cgroup_some", "cgroup_full")
EVENTS = ("high", "max", "oom", "oom_kill")


def integer(value):
    return type(value) is int and value >= 0


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def page_size(sample):
    direct = sample.get("page_size_bytes")
    inventory = sample.get("swap_inventory", {}).get("page_size_bytes")
    if direct is not None and inventory is not None and direct != inventory:
        return None
    value = direct if direct is not None else inventory
    return value if integer(value) and value > 0 else None


def reclaim_counter(sample):
    stat = sample.get("memory_stat") or {}
    if "pgsteal" in stat:
        return "pgsteal", stat["pgsteal"] if integer(stat["pgsteal"]) else None
    keys = ("pgsteal_kswapd", "pgsteal_direct")
    if all(integer(stat.get(key)) for key in keys):
        return "+".join(keys), sum(stat[key] for key in keys)
    return None, None


def gauge(value):
    value = value if integer(value) else None
    return {"baseline_bytes": value, "current_bytes": value, "peak_bytes": value,
            "delta_bytes": 0 if value is not None else None,
            "peak_delta_bytes": 0 if value is not None else None}


def update_gauge(metric, value):
    if not integer(value):
        metric["current_bytes"] = metric["delta_bytes"] = None
        return
    metric["current_bytes"] = value
    metric["peak_bytes"] = value if metric["peak_bytes"] is None else max(metric["peak_bytes"], value)
    if metric["baseline_bytes"] is not None:
        metric["delta_bytes"] = value - metric["baseline_bytes"]
        metric["peak_delta_bytes"] = max(0, metric["peak_bytes"] - metric["baseline_bytes"])


def counter(value):
    valid = integer(value)
    return {"baseline": value if valid else None, "last": value if valid else None,
            "delta": 0 if valid else None, "valid": valid}


def update_counter(metric, value):
    if not integer(value) or metric["last"] is None or value < metric["last"]:
        metric["valid"] = False
    metric["last"] = value if integer(value) else None
    metric["delta"] = value - metric["baseline"] if metric["valid"] else None


def validate_sample(sample):
    if sample.get("ok") is not True or not number(sample.get("timestamp")):
        raise ValueError("requires a valid timestamped sample")
    for field in ("cgroup_current_bytes", "memory_available_bytes"):
        if not integer(sample.get(field)):
            raise ValueError("missing/invalid " + field)


def start_record(admission, sample, case, worker_baseline):
    validate_sample(sample)
    if integer(worker_baseline):
        baselines = copy.deepcopy(admission.get("reservation", {}).get("worker_initial_bytes", {}))
        baselines[case] = worker_baseline
        worker_baseline = baselines
    if not isinstance(case, str) or not case or not isinstance(worker_baseline, dict):
        raise ValueError("requires case and worker baseline mapping")
    if not all(isinstance(key, str) and integer(value) for key, value in worker_baseline.items()):
        raise ValueError("worker baseline must map case to nonnegative bytes")
    source, stolen = reclaim_counter(sample)
    size = page_size(sample)
    stat = sample.get("memory_stat") or {}
    record = {
        "schema": "h3.admission-reconciliation.v1", "case": case, "state": "observing",
        "admission": copy.deepcopy(admission), "prediction": {
            key: copy.deepcopy(admission.get(key)) for key in ("projected_cgroup_bytes", "projected_available_bytes", "reservation")},
        "automatic_calibration": False, "aggregate_attribution": "all activity in aggregate cgroup; not attributable to this task",
        "peak_semantics": "sampled high-water marks", "boot_id": sample.get("boot_id"),
        "started_at": sample["timestamp"], "last_observed_at": None, "sample_count": 0,
        "aggregate_cgroup": gauge(sample["cgroup_current_bytes"]),
        "workers": {key: gauge(value) for key, value in worker_baseline.items()},
        "min_host_available_bytes": sample["memory_available_bytes"],
        "psi_avg10_peak": {key: None for key in PRESSURE},
        "swap": {"host": gauge(sample.get("swap_used_bytes")), "cgroup": gauge(sample.get("cgroup_swap_bytes"))},
        "swap_io_pages": {key: counter(sample.get("swap_io_pages", {}).get(key)) for key in ("pswpin", "pswpout")},
        "swap_io_delta_bytes": {"pswpin": None, "pswpout": None},
        "cgroup_events": {key: counter(sample.get("cgroup_events", {}).get(key)) for key in EVENTS},
        "reclaim": {"scope": "aggregate_cgroup", "task_attributable": False, "source": source,
                    "measurement": "cumulative pgsteal page delta; may include repeated and anonymous reclaim; not net file-cache release",
                    "page_size_bytes": size, "counter": counter(stolen), "actual_reclaimed_bytes": None,
                    "component_last": {key: stat.get(key) for key in source.split("+")} if source else {},
                    "complete": source is not None and size is not None and stolen is not None},
        "file_drop_proxy": {"baseline_bytes": stat.get("file") if integer(stat.get("file")) else None,
                            "min_bytes": None, "max_drop_bytes": None, "is_actual_reclaim": False},
        "kernel_alerts": [], "baseline_kernel_alerts": copy.deepcopy(sample.get("kernel_alerts")),
        "oom_alerts": [], "xid_alerts": [], "missing_fields": [], "failure_samples": [],
        "execution_failures": [], "cuda_oom_detected": False,
        "_page_size_bytes": size, "_page_size_valid": size is not None,
    }
    return observe(record, sample)


def record_execution_failure(record, failure):
    if failure not in record["execution_failures"]:
        record["execution_failures"].append(copy.deepcopy(failure))
    record["cuda_oom_detected"] = record["cuda_oom_detected"] or failure["cuda_oom_detected"]


def observe(record, sample):
    if record["state"] != "observing":
        raise ValueError("record is finalized")
    events = sample.get("cgroup_events", {})
    oom_growth = any(integer(events.get(key)) and record["cgroup_events"][key]["last"] is not None
                     and events[key] > record["cgroup_events"][key]["last"] for key in ("oom", "oom_kill"))
    if sample.get("ok") is not True or sample.get("kernel_alerts") or oom_growth:
        record["failure_samples"].append(copy.deepcopy(sample))
    if not number(sample.get("timestamp")):
        raise ValueError("sample timestamp is missing/invalid")
    if sample.get("boot_id") != record["boot_id"]:
        raise ValueError("boot identity changed; do not mix accounting epochs")
    previous = record["last_observed_at"]
    if previous is not None and sample["timestamp"] <= previous:
        raise ValueError("samples must have strictly increasing timestamps")
    record["last_observed_at"] = sample["timestamp"]
    record["sample_count"] += 1
    def missing(field):
        if field not in record["missing_fields"]:
            record["missing_fields"].append(field)

    if sample.get("ok") is not True:
        missing("sample.ok")
    update_gauge(record["aggregate_cgroup"], sample.get("cgroup_current_bytes"))
    if not integer(sample.get("cgroup_current_bytes")):
        missing("cgroup_current_bytes")
    if integer(sample.get("memory_available_bytes")):
        record["min_host_available_bytes"] = min(record["min_host_available_bytes"], sample["memory_available_bytes"])
    else:
        missing("memory_available_bytes")
    memory = sample.get("worker_memory_bytes", {})
    for worker in memory:
        if worker not in record["workers"]:
            record["workers"][worker] = gauge(None)
            missing("worker_baseline." + worker)
    for worker, metric in record["workers"].items():
        value = memory.get(worker)
        if not integer(value):
            missing("worker_memory_bytes." + worker)
        update_gauge(metric, value)
    for key in PRESSURE:
        value = sample.get("memory_pressure", {}).get(key + "_avg10")
        if not number(value) or not 0 <= value <= 100:
            missing("memory_pressure." + key + "_avg10")
            continue
        peak = record["psi_avg10_peak"][key]
        record["psi_avg10_peak"][key] = value if peak is None else max(peak, value)
    for scope, key in (("host", "swap_used_bytes"), ("cgroup", "cgroup_swap_bytes")):
        if not integer(sample.get(key)):
            missing(key)
        update_gauge(record["swap"][scope], sample.get(key))
        observed = [sample.get("swap_sampling_peak_" + scope + "_bytes"), sample.get("swap_initial_sample", {}).get(key)]
        metric = record["swap"][scope]
        for value in observed:
            if integer(value):
                metric["peak_bytes"] = value if metric["peak_bytes"] is None else max(metric["peak_bytes"], value)
        if metric["baseline_bytes"] is not None and metric["peak_bytes"] is not None:
            metric["peak_delta_bytes"] = max(0, metric["peak_bytes"] - metric["baseline_bytes"])
    size = page_size(sample)
    if size is None or size != record["_page_size_bytes"]:
        record["_page_size_valid"] = False
        missing("page_size_bytes")
    for group in ("swap_io_pages", "cgroup_events"):
        for key, metric in record[group].items():
            update_counter(metric, sample.get(group, {}).get(key))
            if not metric["valid"]:
                missing(group + "." + key)
            if group == "swap_io_pages":
                record["swap_io_delta_bytes"][key] = metric["delta"] * size if metric["valid"] and record["_page_size_valid"] else None
    source, stolen = reclaim_counter(sample)
    stat = sample.get("memory_stat") or {}
    reclaim = record["reclaim"]
    if source != reclaim["source"] or not record["_page_size_valid"]:
        reclaim["complete"] = False
    for key, before in reclaim["component_last"].items():
        current = stat.get(key)
        if not integer(before) or not integer(current) or current < before:
            reclaim["complete"] = False
        reclaim["component_last"][key] = current
    update_counter(reclaim["counter"], stolen)
    reclaim["complete"] = reclaim["complete"] and reclaim["counter"]["valid"]
    reclaim["actual_reclaimed_bytes"] = reclaim["counter"]["delta"] * size if reclaim["complete"] else None
    if not reclaim["complete"]:
        missing("memory_stat.pgsteal_or_split_counters")
    file_bytes = stat.get("file")
    proxy = record["file_drop_proxy"]
    if integer(file_bytes):
        proxy["min_bytes"] = file_bytes if proxy["min_bytes"] is None else min(proxy["min_bytes"], file_bytes)
        if proxy["baseline_bytes"] is not None:
            proxy["max_drop_bytes"] = max(0, proxy["baseline_bytes"] - proxy["min_bytes"])
    else:
        missing("memory_stat.file")
    alerts = sample.get("kernel_alerts")
    if not isinstance(alerts, list) or not all(isinstance(alert, str) for alert in alerts):
        missing("kernel_alerts")
    else:
        for alert in alerts:
            if alert not in record["kernel_alerts"]:
                record["kernel_alerts"].append(alert)
                if re.search(r"\boom\b|out of memory|killed process", alert, re.I):
                    record["oom_alerts"].append(alert)
                if re.search(r"\bxid\b", alert, re.I):
                    record["xid_alerts"].append(alert)
    return record


def finish(record):
    predicted = record["prediction"]
    projected = predicted["projected_cgroup_bytes"]
    available = predicted["projected_available_bytes"]
    record["comparison"] = {
        "aggregate_peak_minus_projected_bytes": record["aggregate_cgroup"]["peak_bytes"] - projected if number(projected) else None,
        "min_available_minus_projected_bytes": record["min_host_available_bytes"] - available if number(available) else None,
        "interpretation": "observed aggregate window versus frozen prediction; not a per-task causal estimate",
        "automatic_calibration": False,
    }
    record["state"] = "finished"
    result = copy.deepcopy(record)
    for key in tuple(result):
        if key.startswith("_"):
            del result[key]
    return result
