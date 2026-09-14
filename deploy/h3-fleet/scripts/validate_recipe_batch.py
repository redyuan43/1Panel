from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import httpx

from deploy import private_executor_url
from validate_dual import router_key, validate_media
from recipe_evidence import sampler_overlap


BATCHES = {"single": ["A4_C0"], "b8": ["B8", "A4_C0", "A4"], "people": ["A4_C1", "A4", "A4_C0"]}
TERMINAL = {"completed", "error", "cancelled"}
GIB = 1024**3


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def probe_video(path: Path) -> dict:
    raw = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True, timeout=60)
    result = validate_media(json.loads(raw.stdout), width=480, height=864, length=362, fps=24)
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                   capture_output=True, check=True, timeout=120)
    result["full_decode_verified"] = True
    result["artifact_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def reference(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def request(client, method, path, deadline, **kwargs):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("bounded validation/cleanup deadline reached")
    return client.request(method, path, timeout=min(30, remaining), **kwargs).raise_for_status()


def idle(state):
    return (isinstance(state.get("active"), list) and not state["active"]
            and isinstance(state.get("queues"), list)
            and all(type(queue.get("queued_or_running")) is int and queue["queued_or_running"] == 0
                    for queue in state["queues"]))


def read_job(client, task, deadline):
    response = request(client, "GET", "/api/jobs/" + task["prompt_id"], deadline).json()
    job = response.get("job", response)
    if (job.get("prompt_id") != task["prompt_id"] or job.get("execution_id") != task["execution_id"]
            or job.get("recipe_id") != task["recipe_id"]):
        raise RuntimeError("task identity mismatch; refusing cancellation or cleanup")
    backend = json.loads(job.get("backend_json") or "null")
    if task.get("backend") and task["backend"] != backend:
        raise RuntimeError("task backend changed; manual reconciliation required")
    task.update(status=job["status"], upstream_prompt_id=job.get("upstream_prompt_id"),
                admission_reason=job.get("admission_reason"), execution_seconds=job.get("execution_seconds"),
                backend=backend, resource_reconciliation=json.loads(job.get("reconciliation_json") or "null"),
                sampler_progress=json.loads(job.get("progress_json") or "null"))
    return job


def smoke_ready(task, seconds):
    progress = task.get("sampler_progress") or {}
    reconciliation = task.get("resource_reconciliation") or {}
    upstream = task.get("upstream_prompt_id")
    events = progress.get("sampler_progress_events")
    if (not upstream or progress.get("prompt_id") != upstream or progress.get("cached") is not False
            or progress.get("ready") is not True or progress.get("error")
            or not isinstance(events, list) or not events
            or any(event.get("prompt_id") != upstream or event.get("confirmed_sampler_progress") is not True for event in events)):
        return False
    elapsed = progress.get("continuous_sampler_seconds")
    observed = progress.get("observed_at")
    last = reconciliation.get("last_observed_at")
    if (type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not elapsed >= seconds
            or type(observed) not in (int, float) or not 0 <= time.time() - observed <= 15
            or type(last) not in (int, float) or not 0 <= time.time() - last <= 15
            or reconciliation.get("observation_error")):
        return False
    for key in ("kernel_alerts", "oom_alerts", "xid_alerts"):
        if reconciliation.get(key) != []:
            return False
    for key in ("oom", "oom_kill"):
        counter = reconciliation.get("cgroup_events", {}).get(key, {})
        if counter.get("valid") is not True or type(counter.get("delta")) is not int or counter["delta"] != 0:
            return False
    if any(field == "kernel_alerts" or field.startswith("cgroup_events.") or field == "sample.ok"
           for field in reconciliation.get("missing_fields", [])):
        return False
    return not any(failure.get("cuda_oom_detected") for failure in reconciliation.get("execution_failures", []))


def cancel_task(client, task, deadline):
    read_job(client, task, deadline)
    if task["status"] not in TERMINAL:
        task.setdefault("cancel_operation_id", "cancel_" + hashlib.sha256(task["execution_id"].encode()).hexdigest()[:32])
        request(client, "POST", "/api/router/executions/" + task["execution_id"] + "/cancel", deadline,
                json={"operation_id": task["cancel_operation_id"]})
        while True:
            read_job(client, task, deadline)
            if task["status"] in TERMINAL:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("owned cancellation outcome unconfirmed")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    task["terminal_reconciled"] = True


def check_backend_identity(binding):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.recipe_dispatch import backend_identity
    identity = binding["identity"]
    if (identity["url"] != binding["lane"]["url"] or identity["gpu_uuid"] != binding["lane"]["gpu_uuid"]
            or identity["runtime_version"] != binding["runtime_version"]):
        raise RuntimeError("backend cleanup identity mismatch")
    observed = backend_identity(identity)
    if any(observed.get(key) != value for key, value in identity.items()):
        raise RuntimeError("backend process identity changed")


def empty_backend(client, deadline):
    queue = request(client, "GET", "/queue", deadline).json()
    if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
        raise RuntimeError("backend queue is not confirmed empty; refusing free")


def unload_backend(binding, deadline):
    check_backend_identity(binding)
    with httpx.Client(base_url=private_executor_url(binding["lane"]["url"]), trust_env=False, timeout=30) as backend:
        empty_backend(backend, deadline)
        check_backend_identity(binding)
        request(backend, "POST", "/free", deadline, json={"unload_models": True, "free_memory": True})
        while True:
            check_backend_identity(binding)
            empty_backend(backend, deadline)
            devices = request(backend, "GET", "/system_stats", deadline).json().get("devices", [])
            if len(devices) != 1:
                raise RuntimeError("backend GPU telemetry unavailable")
            total, free = devices[0].get("vram_total"), devices[0].get("vram_free")
            if type(total) is not int or type(free) is not int or not 0 <= free <= total or total <= 0:
                raise RuntimeError("invalid backend GPU memory accounting")
            if total - free <= GIB:
                return {"backend_id": binding["id"], "url": binding["lane"]["url"],
                        "unload_confirmed": True, "vram_used_bytes": total - free}
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def cleanup(client, production, report, output, timeout):
    deadline = time.monotonic() + timeout
    errors = []
    for task in report["tasks"]:
        if not task.get("submit_attempted"):
            continue
        if not task.get("prompt_id"):
            errors.append("unknown submission: " + task["execution_id"])
            continue
        try:
            cancel_task(client, task, deadline)
        except Exception as error:
            errors.append(task["execution_id"] + ": " + str(error))
        write_json(output / "report.json", report)
    try:
        if errors:
            raise RuntimeError("; ".join(errors))
        for connection in (client, production):
            state = request(connection, "GET", "/api/router/capacity", deadline).json()
            if not idle(state) or (state.get("validation_lease") and state["validation_lease"].get("owner") != report["owner"]):
                raise RuntimeError("Fleet work/lease identity is not reconciled")
        bindings = {}
        for task in report["tasks"]:
            if task.get("backend"):
                binding = task["backend"]
                previous = bindings.setdefault(binding["lane"]["url"], binding)
                if previous != binding:
                    raise RuntimeError("conflicting backend identities in task report")
        report["backend_unloads"] = []
        for binding in bindings.values():
            report["backend_unloads"].append(unload_backend(binding, deadline))
            write_json(output / "report.json", report)
        report["all_task_backends_unloaded"] = True
        for name, connection in (("lab", client), ("production", production)):
            state = request(connection, "GET", "/api/router/capacity", deadline).json()
            if not idle(state):
                raise RuntimeError("Fleet work appeared before lease release")
            lease = state.get("validation_lease")
            if lease:
                if lease.get("owner") != report["owner"]:
                    raise RuntimeError("refusing to release another owner's lease")
                request(connection, "DELETE", "/api/router/validation-lease", deadline, json={"owner": report["owner"]})
            state = request(connection, "GET", "/api/router/capacity", deadline).json()
            if state.get("validation_lease") is not None or not idle(state):
                raise RuntimeError(name + " lease release unconfirmed")
            report[name + "_lease_released"] = True
        report["lease_released"] = True
    except Exception as error:
        report.update(status="requires_reconciliation", cleanup_error=str(error), lease_released=False)


def run(args) -> dict:
    recipes = BATCHES[args.batch]
    output = args.output_dir.resolve()
    smoke_seconds = getattr(args, "smoke_seconds", None)
    if smoke_seconds not in (None, 60) or type(smoke_seconds) is bool:
        raise ValueError("startup smoke requires exactly 60 seconds")
    report = {"schema_version": 1, "recipes": recipes, "status": "dry_run", "peak_parallel": 0,
              "parallel_validated": False, "quality_review": "pending_human_review",
              "full_video_validated": False, "smoke_seconds": smoke_seconds,
              "validation_scope": "startup_sampler_window_only" if smoke_seconds else "full_video",
              "specification": {"width": 480, "height": 864, "frames": 362, "fps": 24, "audio": "native"}}
    if not args.execute:
        return report
    if not 0 < args.timeout <= 18000:
        raise ValueError("timeout must fit within the six-hour lease")
    if output.is_relative_to("/tmp") or output.is_relative_to("/var/tmp"):
        raise ValueError("validation evidence must use a persistent directory")
    output.mkdir(parents=True, exist_ok=False)
    owner = "h3val_" + uuid.uuid4().hex[:16]
    report.update(status="running", owner=owner, started_at=time.time(), tasks=[], lease_released=False,
                  no_oom_claim_scope="observed startup window only; not all stages" if smoke_seconds else "observed validation run")
    secret = router_key(args.key_file)
    headers = {"Authorization": "Bearer " + secret}
    with httpx.Client(base_url=private_executor_url(args.fleet_url), headers=headers, trust_env=False, timeout=60) as client:
        with httpx.Client(base_url=private_executor_url(args.production_fleet_url), headers=headers, trust_env=False, timeout=60) as production:
            deadline = time.monotonic() + args.timeout
            try:
                for connection in (client, production):
                    state = request(connection, "GET", "/api/router/capacity", deadline).json()
                    if not idle(state) or state.get("validation_lease"):
                        raise RuntimeError("Fleet must be idle without another validation lease")
                for name, connection in (("production", production), ("lab", client)):
                    report[name + "_lease_attempted"] = True
                    write_json(output / "report.json", report)
                    request(connection, "POST", "/api/router/validation-lease", deadline,
                            json={"owner": owner, "ttl_seconds": 21600})
                prompt = args.prompt_file.read_text()
                for position, recipe_id in enumerate(recipes):
                    execution_id = owner + "_" + str(position) + "_" + recipe_id
                    built = request(client, "POST", "/api/router/recipe-workflow", deadline, json={"recipe_id": recipe_id, "prompt": prompt,
                        "seed": args.seed, "filename_prefix": owner + "/" + recipe_id}).json()
                    directory = output / recipe_id
                    directory.mkdir()
                    write_json(directory / "workflow.json", built["prompt"])
                    task = {"recipe_id": recipe_id, "execution_id": execution_id, "status": "submission_unknown",
                            "submitted_at": time.time(), "recipe_binding": built["recipe_binding"], "submit_attempted": True}
                    report["tasks"].append(task)
                    write_json(output / "report.json", report)
                    receipt = request(client, "POST", "/prompt", deadline, json={"prompt": built["prompt"], "extra_data": {"h3": {
                        "recipe_id": recipe_id, "recipe_version": built["recipe_binding"]["recipe_version"],
                        "execution_id": execution_id, "stage": "preview", "profile": "preview"}}}).json()
                    identifier = receipt.get("prompt_id")
                    if not isinstance(identifier, str) or not identifier or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in identifier):
                        raise RuntimeError("submission returned no safe known Fleet prompt ID")
                    task.update(prompt_id=identifier, status="queued")
                    write_json(output / "report.json", report)
                while time.monotonic() < deadline:
                    for task in report["tasks"]:
                        if task["status"] in TERMINAL:
                            continue
                        job = read_job(client, task, deadline)
                        if smoke_seconds:
                            reconciliation = task.get("resource_reconciliation") or {}
                            counters = reconciliation.get("cgroup_events", {})
                            if (reconciliation.get("oom_alerts") or reconciliation.get("xid_alerts")
                                    or reconciliation.get("kernel_alerts")
                                    or any(type(counters.get(key, {}).get("delta")) is int and counters[key]["delta"] > 0
                                           for key in ("oom", "oom_kill"))
                                    or (task.get("sampler_progress") or {}).get("error")):
                                raise RuntimeError("startup telemetry reported OOM/Xid or sampler failure")
                            if smoke_ready(task, smoke_seconds):
                                task["smoke_observation"] = copy.deepcopy({key: task[key] for key in ("sampler_progress", "resource_reconciliation")})
                                task["smoke_passed"] = True
                                write_json(output / "report.json", report)
                                cancel_task(client, task, deadline)
                                if task["status"] == "error":
                                    raise RuntimeError("task failed during smoke cancellation")
                            elif task["status"] in TERMINAL:
                                raise RuntimeError("task ended before the owned startup smoke gate: " + task["recipe_id"])
                            continue
                        if job["status"] in {"error", "cancelled"}:
                            raise RuntimeError("recipe execution failed: " + task["recipe_id"])
                        if job["status"] == "completed":
                            directory = output / task["recipe_id"]
                            media = request(client, "GET", "/api/router/executions/" + task["execution_id"] + "/output", deadline)
                            path = directory / "video.mp4"
                            path.write_bytes(media.content)
                            task["media_validation"] = probe_video(path)
                            history = request(client, "GET", "/history/" + task["prompt_id"], deadline).json()
                            write_json(directory / "history.json", {job["upstream_prompt_id"]: history[task["prompt_id"]]})
                            write_json(directory / "media-probe.json", task["media_validation"])
                            write_json(directory / "sampler-progress.json", task["sampler_progress"])
                            backend = task["backend"]
                            qualification = {**task["recipe_binding"], "status": "completed",
                                "gpu_uuid": backend["lane"]["gpu_uuid"], "runtime_version": backend["runtime_version"],
                                "upstream_prompt_id": job["upstream_prompt_id"], "workflow": reference(directory / "workflow.json"),
                                "history": reference(directory / "history.json"), "artifact": reference(path),
                                "media_probe": reference(directory / "media-probe.json"), "quality_review": "pending_human_review",
                                "sampler_progress": reference(directory / "sampler-progress.json"),
                                "automatic_promotion": False}
                            write_json(directory / "qualification-candidate.json", qualification)
                    if not smoke_seconds:
                        report.update(sampler_overlap(report["tasks"]))
                    write_json(output / "report.json", report)
                    if all(task["status"] in {"completed", "error", "cancelled"} for task in report["tasks"]):
                        break
                    time.sleep(5)
                else:
                    raise RuntimeError("bounded validation deadline reached")
                if smoke_seconds:
                    report["status"] = "startup_smoke_passed" if all(task.get("smoke_passed") for task in report["tasks"]) else "failed"
                else:
                    report["full_video_validated"] = all(task["status"] == "completed" for task in report["tasks"])
                    report["status"] = "completed_pending_human_review" if report["full_video_validated"] else "failed"
            except Exception as error:
                report.update(status="requires_reconciliation", error=str(error))
            finally:
                cleanup(client, production, report, output, getattr(args, "cleanup_timeout", 120))
                report.update(finished_at=time.time(), elapsed_seconds=time.time() - report["started_at"])
                write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Bounded recipe verification through the single experimental Fleet authority")
    parser.add_argument("--fleet-url", required=True)
    parser.add_argument("--production-fleet-url", default="http://ivan-ms-7b17.taild500c8.ts.net:8789")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch", choices=BATCHES, default="single")
    parser.add_argument("--seed", type=int, default=2026090901)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--smoke-seconds", type=int, choices=(60,), help="cancel after 60s owned sampler startup; never creates qualification evidence")
    parser.add_argument("--cleanup-timeout", type=int, default=120)
    parser.add_argument("--execute", action="store_true")
    result = run(parser.parse_args())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["status"] in {"dry_run", "startup_smoke_passed", "completed_pending_human_review"} else 1)


if __name__ == "__main__":
    main()
