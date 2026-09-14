from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx

from validate_capacity import check_idle, command, file_sha256, key_from_service, sample_host, safety_reason, validate_media


WORKER_URLS = {"fast": "http://127.0.0.1:8188", "main": "http://127.0.0.1:8189", "preview": "http://127.0.0.1:8190"}


def start_progress_recorders(output, finished):
    try:
        from websockets.sync.client import connect
    except ImportError:
        return []

    def record(lane, url):
        with (output / (lane + ".progress.jsonl")).open("a") as handle:
            while not finished.is_set():
                try:
                    with connect(url.replace("http://", "ws://") + "/ws?clientId=" + uuid.uuid4().hex,
                                 open_timeout=5, close_timeout=2, proxy=None, max_size=8 * 1024**2) as websocket:
                        while not finished.is_set():
                            try:
                                message = websocket.recv(timeout=2)
                            except TimeoutError:
                                continue
                            if not isinstance(message, str):
                                continue
                            event = json.loads(message)
                            if event.get("type") in {"progress", "executing", "execution_start", "execution_success", "execution_error", "execution_cached"}:
                                handle.write(json.dumps({"timestamp": time.time(), "lane": lane, **event}) + "\n")
                                handle.flush()
                except Exception as error:
                    handle.write(json.dumps({"timestamp": time.time(), "collector_error": str(error)[:200]}) + "\n")
                    handle.flush()
                    finished.wait(5)

    threads = [threading.Thread(target=record, args=(lane, url), daemon=True) for lane, url in WORKER_URLS.items()]
    for thread in threads:
        thread.start()
    return threads


def memory_diagnostics(services, bandwidth=False):
    cgroup = Path(os.environ.get("H3_COMPUTE_CGROUP", "/sys/fs/cgroup/h3.slice/h3-compute.slice"))
    result = {"meminfo": Path("/proc/meminfo").read_text(),
              "vmstat": {name: int(value) for name, value in (line.split() for line in Path("/proc/vmstat").read_text().splitlines())
                         if name.startswith(("pgmajfault", "pgscan", "pgsteal", "allocstall", "pswp", "oom_kill", "compact"))},
              "cgroup_memory_stat": (cgroup / "memory.stat").read_text(), "workers": {}}
    for unit in services:
        process = Path("/proc") / unit["MainPID"]
        try:
            status = process.joinpath("status").read_text()
            result["workers"][unit["Id"]] = {"pid": unit["MainPID"], "memory": "\n".join(
                line for line in status.splitlines() if line.startswith(("Vm", "Rss", "Threads:")))}
        except OSError:
            result["workers"][unit["Id"]] = {"pid": unit["MainPID"], "unavailable": True}
    try:
        result["pcie_dmon"] = command("nvidia-smi", "dmon", "-s", "pucvmt", "-c", "1")
    except RuntimeError as error:
        result["pcie_error"] = str(error)
    if bandwidth:
        try:
            counters = subprocess.run(["sudo", "-n", "perf", "stat", "-a", "-x", ";", "-e",
                "uncore_imc/data_reads/,uncore_imc/data_writes/", "--", "sleep", "1"],
                capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
            result["memory_bandwidth"] = {"returncode": counters.returncode, "sample_seconds": 1,
                                          "perf_stat": counters.stderr[-4000:]}
        except (OSError, subprocess.TimeoutExpired) as error:
            result["memory_bandwidth"] = {"unavailable": True, "error": str(error)[:200]}
    return result


def prepare_workflow(template, identifier, orientation):
    workflow = copy.deepcopy(template)
    seed = int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:8], "big") % (2 ** 63)
    found_noise = False
    for node in workflow.values():
        inputs = node.setdefault("inputs", {})
        if node.get("class_type") == "RandomNoise":
            inputs["noise_seed"] = seed
            found_noise = True
        if node.get("class_type") == "SaveVideo":
            inputs["filename_prefix"] = "video/h3-validation/" + identifier
        if "width" in inputs and "height" in inputs:
            width, height = sorted((inputs["width"], inputs["height"]), reverse=orientation == "landscape")
            inputs.update(width=width, height=height)
    if not found_noise:
        raise ValueError("validation workflow has no independently seeded noise")
    return workflow, seed


def validate_pinned_workflow(workflow, case):
    groups = {"B8": "b_vdn", "D4": "d_fasth3"}
    if case not in groups:
        raise ValueError("no audited validator for recipe")
    path = Path(__file__).resolve().parents[1] / "experiments/optimization-20260909" / groups[case] / "validate_runner_graph.py"
    spec = importlib.util.spec_from_file_location(groups[case] + "_runner_graph", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_workflow(workflow, case)


def verify_execution(history, upstream_id, workflow):
    composers = {identifier for identifier, node in workflow.items()
                 if node.get("class_type") == "MiniMaxH3VDNModelComposerT8Advanced"}
    plans = {identifier for identifier, node in workflow.items()
             if node.get("class_type") == "MiniMaxH3VDNExecutionPlanT8Advanced"}
    recipe = {}
    if composers or plans:
        shape = validate_pinned_workflow(workflow, "B8")
        recipe = {"recipe": "B8", "model_composer_nodes": sorted(composers),
                  "execution_plan_nodes": sorted(plans), "expected_steps": shape["steps"]}
    record = history.get(upstream_id, {})
    status = record.get("status", {})
    messages = status.get("messages", [])
    cached = {str(node) for event, payload in messages if event == "execution_cached" for node in payload.get("nodes", [])}
    samplers = {identifier for identifier, node in workflow.items() if node.get("class_type") == "SamplerCustomAdvanced"}
    starts = [payload["timestamp"] for event, payload in messages if event == "execution_start"]
    ends = [payload["timestamp"] for event, payload in messages if event == "execution_success"]
    if not status.get("completed") or status.get("status_str") != "success" or not starts or not ends:
        raise ValueError("worker history does not prove successful execution")
    if not samplers or samplers & cached:
        raise ValueError("sampler output was cached; this is not an inference benchmark")
    if ends[-1] <= starts[0]:
        raise ValueError("worker execution timing is invalid")
    return {"started_at": starts[0] / 1000, "finished_at": ends[-1] / 1000,
            "execution_seconds": (ends[-1] - starts[0]) / 1000,
            "sampler_nodes": sorted(samplers), "cached_nodes": sorted(cached), **recipe}


def matrix_cases(names):
    cases = {}
    for count in (2, 3):
        cases["preview_" + str(count)] = (["long_preview"] * count,
            {"profile": "preview", "frame_count": 362, "max_parallel": count})
    cases["mixed"] = (["long_quality", "long_preview"],
        {"profile": "mixed", "frame_count": 362, "preview_frame_count": 362, "max_parallel": 2})
    return [(name, *cases[name]) for name in names]


def run(args):
    owner = "h3val_" + uuid.uuid4().hex[:16]
    output = args.output_dir / owner
    output.mkdir(parents=True, mode=0o700)
    report = {"owner": owner, "status": "preflight", "started_at": time.time(), "batches": [], "owned_ids": [],
              "production_capacity_changed": False, "orientation": args.orientation,
              "repetitions": args.repetitions, "cases": args.case}
    stop = threading.Event()
    deadline = time.monotonic() + args.timeout
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    headers = {"Authorization": "Bearer " + key_from_service()}
    lease = False
    monitor = None
    failure = []
    finished = threading.Event()
    progress_threads = []

    def save():
        report["updated_at"] = time.time()
        temporary = output / "report.part"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(output / "report.json")

    def guard():
        if stop.is_set() or time.monotonic() >= deadline:
            raise RuntimeError(failure[0] if failure else "validation stopped or deadline reached")

    with httpx.Client(base_url=args.fleet_url, headers=headers, trust_env=False,
                      follow_redirects=False, timeout=30) as client:
        def request(method, path, body=None):
            response = client.request(method, path, json=body) if body is not None else client.request(method, path)
            response.raise_for_status()
            return response.json()

        def job(identifier):
            response = client.get("/api/jobs/by-execution/" + identifier)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

        try:
            capacity = request("GET", "/api/router/capacity")
            check_idle(capacity)
            baseline = sample_host(report["started_at"])
            limits = capacity["policy"]["resources"]
            reason = safety_reason(baseline, baseline, limits)
            if reason:
                raise RuntimeError(reason)
            report["baseline"] = baseline
            report["policy"] = capacity["policy"]
            request("POST", "/api/router/validation-lease", {"owner": owner, "ttl_seconds": 120})
            lease = True
            report["status"] = "running"
            progress_threads = start_progress_recorders(output, finished)
            report["progress_collection"] = bool(progress_threads)
            save()

            def watch():
                with httpx.Client(base_url=args.fleet_url, headers=headers, trust_env=False, timeout=15) as watcher:
                    with (output / "metrics.jsonl").open("a") as handle:
                        while not stop.is_set():
                            try:
                                sample = sample_host(report["started_at"])
                                sample["memory_diagnostics"] = memory_diagnostics(sample["services"], args.memory_bandwidth)
                                handle.write(json.dumps(sample) + "\n")
                                handle.flush()
                                reason = safety_reason(sample, baseline, limits)
                                if reason:
                                    raise RuntimeError(reason)
                                response = watcher.post("/api/router/validation-lease", json={"owner": owner, "ttl_seconds": 120})
                                response.raise_for_status()
                            except Exception as error:
                                failure.append(str(error)[:500])
                                stop.set()
                                return
                            stop.wait(5)

            def start_monitor():
                nonlocal monitor
                monitor = threading.Thread(target=watch, daemon=True)
                monitor.start()

            for case_name, workloads, experiment in matrix_cases(args.case):
                if monitor:
                    stop.set()
                    monitor.join(timeout=40)
                    if monitor.is_alive() or failure:
                        raise RuntimeError("safety monitor failed to finish cleanly")
                    stop.clear()
                guard()
                request("DELETE", "/api/router/validation-lease", {"owner": owner})
                lease = False
                payload = {"owner": owner, "ttl_seconds": 120}
                if experiment:
                    payload["experiment"] = experiment
                request("POST", "/api/router/validation-lease", payload)
                lease = True
                start_monitor()
                for repetition in range(1, args.repetitions + 1):
                    guard()
                    batch = {"case": case_name, "repetition": repetition, "status": "running",
                             "started_at": time.time(), "executions": [], "peak_parallel": 0}
                    report["batches"].append(batch)
                    workflows = {}
                    for index, workload in enumerate(workloads):
                        guard()
                        identifier = f"{owner}_{case_name}_{repetition}_{index}"
                        template = json.loads((args.workflows / (workload + ".json")).read_text())
                        template, seed = prepare_workflow(template, identifier, args.orientation)
                        workflows[identifier] = template
                        (output / (identifier + ".json")).write_text(json.dumps(template, ensure_ascii=False))
                        profile = "quality" if workload == "long_quality" else "preview"
                        payload = {"prompt": template, "extra_data": {"h3": {
                            "studio": True, "execution_id": identifier, "stage": profile, "profile": profile}}}
                        batch["executions"].append({"id": identifier, "workload": workload, "seed": seed,
                                                    "submitted_at": time.time()})
                        report["owned_ids"].append(identifier)
                        save()
                        request("POST", "/prompt", payload)
                    while True:
                        guard()
                        if time.time() - batch["started_at"] > args.batch_timeout:
                            raise RuntimeError("batch deadline reached")
                        jobs = [job(item["id"]) for item in batch["executions"]]
                        live = request("GET", "/api/router/capacity")
                        if any(not (item.get("execution_id") or "").startswith(owner + "_") for item in live["active"]):
                            raise RuntimeError("foreign work appeared in validation window")
                        lanes = {item.get("lane_id") for item in jobs if item}
                        overlap = sum(min(1, item["running_count"]) for item in live["queues"] if item["lane_id"] in lanes)
                        batch["peak_parallel"] = max(batch["peak_parallel"], overlap)
                        for record, current in zip(batch["executions"], jobs):
                            if current:
                                record.update({key: current.get(key) for key in ("status", "lane_id", "prompt_id", "upstream_prompt_id", "admission_reason")})
                        save()
                        if any(item and item["status"] in {"error", "cancelled"} for item in jobs):
                            raise RuntimeError("validation task failed or was cancelled")
                        if all(item and item["status"] == "completed" for item in jobs):
                            break
                        stop.wait(3)
                    for record, current in zip(batch["executions"], jobs):
                        guard()
                        history_url = WORKER_URLS[current["lane_id"]] + "/history/" + current["upstream_prompt_id"]
                        history_response = client.get(history_url, headers={"Authorization": ""})
                        history_response.raise_for_status()
                        history = history_response.json()
                        (output / (record["id"] + ".history.json")).write_text(json.dumps(history, ensure_ascii=False))
                        record["execution"] = verify_execution(history, current["upstream_prompt_id"], workflows[record["id"]])
                        record["queue_seconds"] = max(0, record["execution"]["started_at"] - record["submitted_at"])
                        artifact = output / (record["id"] + ".mp4")
                        with client.stream("GET", "/view", params={"filename": current["output_filename"],
                                           "subfolder": current["output_subfolder"], "type": current["output_type"]}) as response:
                            response.raise_for_status()
                            with artifact.open("wb") as handle:
                                for chunk in response.iter_bytes():
                                    guard()
                                    handle.write(chunk)
                        media = json.loads(command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(artifact)))
                        width, height = (1344, 768) if record["workload"] == "long_quality" else (864, 480)
                        if args.orientation == "portrait":
                            width, height = height, width
                        frames = 124 if record["workload"] == "short_preview" else 362
                        record["media"] = validate_media(media, width=width, height=height, length=frames)
                        record["sha256"] = file_sha256(artifact)
                        record["artifact"] = artifact.name
                        save()
                    overlap = min(record["execution"]["finished_at"] for record in batch["executions"]) - max(
                        record["execution"]["started_at"] for record in batch["executions"])
                    batch["execution_overlap_seconds"] = max(0, overlap)
                    if len(workloads) > 1 and (batch["peak_parallel"] < len(workloads) or overlap <= 0):
                        raise RuntimeError("outputs completed but simultaneous execution was not proven")
                    batch.update(status="passed", elapsed_seconds=time.time() - batch["started_at"])
                    save()
            report["status"] = "passed_evidence_only_no_capacity_promotion"
        except Exception as error:
            report["status"] = "failed"
            report["error"] = str(error)[:500]
            for batch in report["batches"]:
                if batch["status"] == "running":
                    batch.update(status="failed", error=report["error"], elapsed_seconds=time.time() - batch["started_at"])
        finally:
            stop.set()
            finished.set()
            for thread in progress_threads:
                thread.join(timeout=10)
            if monitor:
                monitor.join(timeout=40)
            if failure:
                report.update(status="failed", error=failure[0])
            if lease:
                try:
                    for identifier in report["owned_ids"]:
                        current = job(identifier)
                        if current and current["status"] not in {"completed", "cancelled", "error"}:
                            request("POST", "/api/jobs/" + current["prompt_id"] + "/cancel", {})
                    for attempt in range(30):
                        live = request("GET", "/api/router/capacity")
                        if not live["active"]:
                            request("DELETE", "/api/router/validation-lease", {"owner": owner})
                            report["lease_released"] = True
                            break
                        time.sleep(2)
                except Exception as error:
                    report["cleanup_error"] = str(error)[:500]
                if not report.get("lease_released"):
                    report.update(status="failed", cleanup_pending=True)
            report["finished_at"] = time.time()
            for lane in WORKER_URLS:
                try:
                    logs = command("journalctl", "-u", "comfyui-h3@" + lane + ".service", "--since",
                                   "@" + str(int(report["started_at"])), "--no-pager", "-o", "cat")
                    (output / (lane + ".log")).write_text(logs)
                except RuntimeError as error:
                    report.setdefault("log_errors", []).append(str(error))
            save()
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}))
    return 0 if report["status"].startswith("passed") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fleet-url", required=True)
    parser.add_argument("--workflows", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=21600)
    parser.add_argument("--batch-timeout", type=int, default=7200)
    parser.add_argument("--repetitions", type=int, choices=(1, 2), default=1)
    parser.add_argument("--orientation", choices=("portrait", "landscape"), default="portrait")
    parser.add_argument("--memory-bandwidth", action="store_true")
    parser.add_argument("--case", action="append", choices=("preview_2", "preview_3", "mixed"))
    arguments = parser.parse_args()
    arguments.case = arguments.case or ["preview_2", "preview_3", "mixed"]
    raise SystemExit(run(arguments))
