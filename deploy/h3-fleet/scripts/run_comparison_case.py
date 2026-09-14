from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import signal
import threading
import time
import uuid

import httpx

from validate_capacity import check_idle, file_sha256, key_from_service, sample_host, safety_reason
from validate_studio_matrix import WORKER_URLS, memory_diagnostics, start_progress_recorders, verify_execution


def validate_input(workflow):
    samplers = [node for node in workflow.values() if node.get("class_type") == "SamplerCustomAdvanced"]
    if len(samplers) != 1:
        raise ValueError("comparison requires exactly one explicit sampler")
    dimensions = [node["inputs"] for node in workflow.values()
                  if "width" in node.get("inputs", {}) and "height" in node.get("inputs", {})]
    if not dimensions or not any(inputs.get("length") == 362 for inputs in dimensions):
        raise ValueError("comparison must request the full 362-frame video")
    if not any(node.get("class_type") == "SaveVideo" for node in workflow.values()):
        raise ValueError("comparison must save a real video")


def run(args):
    workflow = json.loads(args.workflow.read_text())
    validate_input(workflow)
    if (args.output_dir / "report.json").exists():
        raise RuntimeError("existing run must be reconciled by execution ID, never resubmitted")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    owner = "h3val_" + uuid.uuid4().hex[:16]
    identifier = owner + "_" + args.case
    for node in workflow.values():
        if node.get("class_type") == "SaveVideo":
            node["inputs"]["filename_prefix"] = "video/h3-comparison/" + identifier
    encoded = json.dumps(workflow, sort_keys=True, ensure_ascii=False).encode()
    (args.output_dir / "workflow.json").write_bytes(encoded)
    report = {"case": args.case, "owner": owner, "execution_id": identifier,
              "started_at": time.time(), "status": "preflight", "workflow_sha256": hashlib.sha256(encoded).hexdigest(),
              "submitted": False, "lease_released": False}
    lock = threading.Lock()

    def save():
        with lock:
            temporary = args.output_dir / "report.json.next"
            temporary.write_text(json.dumps(report, indent=2))
            temporary.replace(args.output_dir / "report.json")

    save()
    if not args.execute:
        print(json.dumps(report))
        return 0
    stop = threading.Event()
    monitor_stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    failures = []
    lease = False
    monitor = None
    recorders = []
    headers = {"Authorization": "Bearer " + key_from_service()}
    with httpx.Client(base_url=args.fleet_url, headers=headers, trust_env=False, timeout=30) as client:
        def request(method, path, payload=None):
            response = client.request(method, path, json=payload) if payload is not None else client.request(method, path)
            response.raise_for_status()
            return response.json()

        def current_job():
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
            report.update(baseline=baseline, resource_limits=limits)
            request("POST", "/api/router/validation-lease", {"owner": owner, "ttl_seconds": 120})
            lease = True

            def observe():
                with httpx.Client(base_url=args.fleet_url, headers=headers, trust_env=False, timeout=20) as watcher:
                    with (args.output_dir / "metrics.jsonl").open("a") as handle:
                        while not monitor_stop.is_set():
                            try:
                                sample = sample_host(report["started_at"])
                                sample["memory_diagnostics"] = memory_diagnostics(sample["services"], bandwidth=True)
                                handle.write(json.dumps(sample) + "\n")
                                handle.flush()
                                reason = safety_reason(sample, baseline, limits)
                                if reason:
                                    raise RuntimeError(reason)
                                response = watcher.post("/api/router/validation-lease", json={"owner": owner, "ttl_seconds": 120})
                                response.raise_for_status()
                            except Exception as error:
                                failures.append(str(error)[:500])
                                stop.set()
                                return
                            monitor_stop.wait(5)

            monitor = threading.Thread(target=observe, daemon=True)
            monitor.start()
            recorders = start_progress_recorders(args.output_dir, monitor_stop)
            report.update(status="submitting", submitted=True, submitted_at=time.time())
            save()
            request("POST", "/prompt", {"prompt": workflow, "extra_data": {"h3": {
                "studio": True, "execution_id": identifier, "stage": "preview", "profile": "preview"}}})
            while True:
                if stop.is_set():
                    raise RuntimeError(failures[0] if failures else "controlled stop requested")
                if time.time() - report["submitted_at"] > args.timeout:
                    raise TimeoutError("comparison time budget exceeded")
                try:
                    current = current_job()
                except (httpx.TimeoutException, httpx.NetworkError) as error:
                    report["last_observation_error"] = {"at": time.time(), "type": type(error).__name__}
                    save()
                    stop.wait(3)
                    continue
                if current:
                    report.update(status=current["status"], job={key: current.get(key) for key in (
                        "prompt_id", "upstream_prompt_id", "lane_id", "status", "admission_reason", "error")})
                    save()
                    if current["status"] in {"error", "cancelled"}:
                        raise RuntimeError("generation ended: " + json.dumps(report["job"]))
                    if current["status"] == "completed":
                        break
                try:
                    live = request("GET", "/api/router/capacity")
                except (httpx.TimeoutException, httpx.NetworkError) as error:
                    report["last_observation_error"] = {"at": time.time(), "type": type(error).__name__}
                    save()
                    stop.wait(3)
                    continue
                if any(item.get("execution_id") != identifier for item in live["active"]):
                    raise RuntimeError("foreign execution appeared in experiment window")
                stop.wait(3)
            history_response = client.get(WORKER_URLS[current["lane_id"]] + "/history/" + current["upstream_prompt_id"],
                                          headers={"Authorization": ""})
            history_response.raise_for_status()
            history = history_response.json()
            (args.output_dir / "history.json").write_text(json.dumps(history))
            report["execution"] = verify_execution(history, current["upstream_prompt_id"], workflow)
            artifact = args.output_dir / "video.mp4"
            with client.stream("GET", "/view", params={"filename": current["output_filename"],
                               "subfolder": current["output_subfolder"], "type": current["output_type"]}) as response:
                response.raise_for_status()
                with artifact.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            report.update(status="generated_pending_media_review", artifact=str(artifact),
                          artifact_sha256=file_sha256(artifact), finished_at=time.time())
        except Exception as error:
            report.update(status="budget_stopped" if isinstance(error, TimeoutError) else "failed",
                          error=type(error).__name__ + ": " + str(error)[:1000], finished_at=time.time())
        finally:
            monitor_stop.set()
            if monitor:
                monitor.join(timeout=40)
            for recorder in recorders:
                recorder.join(timeout=10)
            reconciled = not report["submitted"]
            if report["submitted"]:
                try:
                    current = current_job()
                    if current and current["status"] not in {"completed", "error", "cancelled"}:
                        request("POST", "/api/jobs/" + current["prompt_id"] + "/cancel", {})
                        current = current_job()
                    reconciled = current is not None and current["status"] in {"completed", "error", "cancelled"}
                    report["terminal_job"] = current
                except Exception as error:
                    report["cleanup_error"] = str(error)[:500]
            if lease and reconciled:
                try:
                    request("DELETE", "/api/router/validation-lease", {"owner": owner})
                    report["lease_released"] = True
                except Exception as error:
                    report["lease_error"] = str(error)[:500]
            elif lease:
                report["needs_reconciliation"] = True
            save()
    print(json.dumps({key: report.get(key) for key in ("case", "status", "execution_id", "error", "lease_released")}))
    return 0 if report["status"] == "generated_pending_media_review" and report["lease_released"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--case", required=True, choices=["R0", "A4", "A8", "B8", "C0", "C1", "D4"])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fleet-url", default="http://ivan-ms-7b17.taild500c8.ts.net:8789")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--execute", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
