from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from pathlib import Path

import httpx

from validate_capacity import key_from_service, sample_host, safety_reason
from validate_studio_matrix import WORKER_URLS, memory_diagnostics, start_progress_recorders


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fleet-url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=2700)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    identifiers = [record["execution_id"] for record in manifest["projects"]]
    for record in manifest["projects"]:
        if not record["execution_id"].startswith("studio_" + record["id"] + "_preview_"):
            raise ValueError("Cannot observe or cancel an unrelated execution")
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"status": "observing", "started_at": time.time(), "executions": [], "peak_parallel": 0}
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    threads = start_progress_recorders(args.output_dir, stop)
    with httpx.Client(base_url=args.fleet_url, headers={"Authorization": "Bearer " + key_from_service()},
                      trust_env=False, timeout=30) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        baseline = sample_host(manifest["started_at"])
        report["baseline"] = baseline
        limits = get("/api/router/capacity")["policy"]["resources"]
        try:
            with (args.output_dir / "metrics.jsonl").open("a") as metrics:
                while True:
                    jobs = [get("/api/jobs/by-execution/" + identifier) for identifier in identifiers]
                    report["executions"] = [{key: job.get(key) for key in (
                        "execution_id", "prompt_id", "upstream_prompt_id", "lane_id", "status", "admission_reason")}
                        for job in jobs]
                    sample = sample_host(manifest["started_at"])
                    sample["memory_diagnostics"] = memory_diagnostics(sample["services"], bandwidth=True)
                    metrics.write(json.dumps(sample) + "\n")
                    metrics.flush()
                    capacity = get("/api/router/capacity")
                    lanes = {job["lane_id"] for job in jobs if job["status"] == "running"}
                    parallel = sum(min(1, queue["running_count"]) for queue in capacity["queues"] if queue["lane_id"] in lanes)
                    report["peak_parallel"] = max(report["peak_parallel"], parallel)
                    reason = safety_reason(sample, baseline, limits)
                    if stop.is_set() or time.time() - report["started_at"] > args.timeout:
                        reason = reason or "observer stopped or timed out"
                    if reason:
                        raise RuntimeError(reason)
                    (args.output_dir / "observer.json").write_text(json.dumps(report, indent=2))
                    if all(job["status"] in {"completed", "error", "cancelled"} for job in jobs):
                        report["status"] = "completed" if all(job["status"] == "completed" for job in jobs) else "failed"
                        break
                    stop.wait(5)
        except Exception as error:
            report.update(status="failed", error=str(error))
            for identifier in identifiers:
                try:
                    job = get("/api/jobs/by-execution/" + identifier)
                    if job["status"] not in {"completed", "error", "cancelled"}:
                        response = client.post("/api/jobs/" + job["prompt_id"] + "/cancel", json={})
                        response.raise_for_status()
                except Exception as cleanup_error:
                    report.setdefault("cleanup_errors", []).append(str(cleanup_error))
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=10)
            for record in report["executions"]:
                if not record.get("upstream_prompt_id") or record.get("lane_id") not in WORKER_URLS:
                    continue
                response = client.get(WORKER_URLS[record["lane_id"]] + "/history/" + record["upstream_prompt_id"],
                                      headers={"Authorization": ""})
                if response.is_success:
                    (args.output_dir / (record["execution_id"] + ".history.json")).write_text(response.text)
            report["finished_at"] = time.time()
            (args.output_dir / "observer.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
