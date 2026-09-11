"""Bounded, owned-ID H3 capacity validation through the authenticated fleet contract.

Run on Ivan so host telemetry covers the GPUs actually executing the requests.
The default is a read-only preflight. --execute requires an idle validation window.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.admission import GIB, resource_snapshot
from app.workflow_builder import frames_for_duration
from deploy import private_executor_url
from validate_dual import router_key, validate_media


TERMINAL = {"completed", "failed", "cancelled"}


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("required host telemetry or media validation command failed: " + args[0])
    return result.stdout


def key_from_service() -> str:
    """Use the same local user's live private credential without writing it out."""
    pid = int(command("systemctl", "show", "h3-fleet.service", "-p", "MainPID", "--value").strip())
    if pid <= 0:
        raise RuntimeError("h3-fleet.service has no running process")
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        name, _, value = item.partition(b"=")
        if name == b"H3_ROUTER_KEY":
            secret = value.decode()
            if not secret or len(secret) > 256 or not all(character.isalnum() or character in "_-" for character in secret):
                raise RuntimeError("fleet process credential is invalid")
            return secret
    raise RuntimeError("fleet process credential is unavailable")


def sample_host(since: float) -> dict[str, Any]:
    sample = resource_snapshot()
    if not sample["ok"]:
        raise RuntimeError("host resource telemetry is unavailable")
    raw = command("nvidia-smi", "--query-gpu=uuid,memory.used,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits")
    sample["gpus"] = [dict(zip(("uuid", "memory_used_mib", "utilization_percent", "temperature_c"),
                                [parts[0], *map(int, parts[1:])]))
                       for parts in ([item.strip() for item in line.split(",")] for line in raw.splitlines())]
    kernel = command("journalctl", "-k", "--since", "@" + str(int(since)), "--no-pager", "-o", "cat")
    sample["kernel_alerts"] = [line[-500:] for line in kernel.splitlines()
                               if any(marker in line for marker in ("NVRM: Xid", "oom-kill", "Out of memory", "Killed process"))]
    units = ("comfyui-h3@fast.service", "comfyui-h3@main.service", "comfyui-h3@preview.service")
    raw = command("systemctl", "show", *units, "-p", "Id", "-p", "ActiveState", "-p", "MainPID", "-p", "NRestarts")
    sample["services"] = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                           for block in raw.strip().split("\n\n")]
    return sample


def safety_reason(sample: dict[str, Any], baseline: dict[str, Any], limits: dict[str, Any]) -> str | None:
    if not sample.get("ok"):
        return "resource telemetry unavailable"
    if sample.get("kernel_alerts"):
        return "new kernel OOM or NVIDIA Xid evidence"
    for key in ("oom", "oom_kill", "max"):
        if sample["cgroup_events"].get(key, 0) > baseline["cgroup_events"].get(key, 0):
            return "cgroup memory event: " + key
    if sample["memory_available_bytes"] < limits["min_available_ram_gib"] * GIB:
        return "host RAM floor crossed"
    if sample["cgroup_current_bytes"] > limits["max_cgroup_gib"] * GIB:
        return "aggregate cgroup memory ceiling crossed"
    if max(sample["swap_used_bytes"], sample["cgroup_swap_bytes"]) > limits["max_swap_gib"] * GIB:
        return "swap limit crossed"
    if sample["root_available_bytes"] < limits["min_root_free_gib"] * GIB:
        return "root disk floor crossed"
    if sample["offload_available_bytes"] < limits["min_offload_free_gib"] * GIB:
        return "offload disk floor crossed"
    if any(gpu["temperature_c"] >= 90 for gpu in sample["gpus"]):
        return "GPU temperature reached 90 C"
    before = {unit["Id"]: unit for unit in baseline["services"]}
    for unit in sample["services"]:
        if unit["ActiveState"] != "active" or unit["MainPID"] != before.get(unit["Id"], {}).get("MainPID"):
            return "GPU worker stopped or restarted"
        if unit["NRestarts"] != before.get(unit["Id"], {}).get("NRestarts"):
            return "GPU worker restart count changed"
    return None


def maximum_for(policy: dict[str, Any], profile: str, frames: int) -> int:
    rule = policy["short"][profile]
    return rule["max_parallel"] if frames <= rule["max_frames"] else policy["long"]["max_parallel"]


def check_idle(capacity: dict[str, Any]) -> None:
    if capacity["active"] or any(item["queued_or_running"] for item in capacity["queues"]):
        raise RuntimeError("existing production or upstream work is present; no validation was submitted")
    if capacity.get("validation_lease"):
        raise RuntimeError("another controlled validation window is present")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_prior_levels(path: Path, configuration: dict[str, Any], gpu_ids: set[str],
                          seen: set[Path] | None = None) -> set[int]:
    """Resume expensive staircases only from complete, unchanged local evidence."""
    path = path.resolve()
    seen = seen or set()
    if path in seen or len(seen) >= 3:
        raise ValueError("invalid previous-report chain")
    seen.add(path)
    prior = json.loads(path.read_text())
    if prior.get("status") != "passed_evidence_only_no_capacity_promotion" or prior.get("lease_released") is not True:
        raise ValueError("previous validation did not finish and release its owned work")
    if any(prior["configuration"].get(key) != configuration[key]
           for key in ("profile", "duration", "frame_count", "aspect_ratio")):
        raise ValueError("previous validation workload differs")
    if {gpu["uuid"] for gpu in prior["baseline"]["gpus"]} != gpu_ids:
        raise ValueError("previous validation GPU topology differs")
    verified = set()
    if prior.get("previous_report"):
        reference = prior["previous_report"]
        if file_sha256(Path(reference["path"])) != reference["sha256"]:
            raise ValueError("previous-report chain was changed")
        verified |= verified_prior_levels(Path(reference["path"]), configuration, gpu_ids, seen)
    levels: dict[int, set[int]] = {}
    for batch in prior["batches"]:
        level, number = batch["level"], batch["batch"]
        if batch.get("status") != "passed" or batch.get("peak_parallel_running", 0) < level:
            raise ValueError("previous batch lacks real parallel completion")
        if number not in {1, 2} or number in levels.setdefault(level, set()) or len(batch["executions"]) != level:
            raise ValueError("previous validation does not have exactly two batches")
        levels[level].add(number)
        for execution in batch["executions"]:
            name = execution.get("artifact", "")
            if not name or Path(name).name != name or not execution.get("media_validation", {}).get("ok"):
                raise ValueError("previous output lacks media validation")
            if file_sha256(path.parent / name) != execution.get("sha256"):
                raise ValueError("previous output was changed or lost")
    if any(batches != {1, 2} for batches in levels.values()):
        raise ValueError("previous validation has an incomplete level")
    verified |= set(levels)
    if verified != set(range(1, prior["configuration"]["max_parallel"] + 1)):
        raise ValueError("previous validation skipped a staircase level")
    return verified


async def read_execution(client: httpx.AsyncClient, identifier: str) -> dict[str, Any]:
    response = await client.get("/api/router/executions/" + identifier)
    if response.status_code == 404:
        return {"execution_id": identifier, "status": "not_found"}
    response.raise_for_status()
    return response.json()


async def cancel_owned(client: httpx.AsyncClient, owner: str, identifiers: list[str]) -> list[dict[str, Any]]:
    """Never interrupt a whole fleet or cancel an ID discovered in a foreign queue."""
    if any(not identifier.startswith(owner + "_") for identifier in identifiers):
        raise ValueError("refusing to cancel a task outside this validation run")
    results = []
    for identifier in identifiers:
        try:
            current = await read_execution(client, identifier)
            if current["status"] == "not_found":
                results.append({"execution_id": identifier, "status": "reconciliation_required"})
                continue
            if current["status"] in TERMINAL:
                results.append(current)
                continue
            response = await client.post("/api/router/executions/" + identifier + "/cancel",
                                         json={"operation_id": identifier + "_cancel"})
            if not response.is_success:
                results.append({"execution_id": identifier, "status": "reconciliation_required", "http_status": response.status_code})
            else:
                results.append(response.json())
        except (httpx.HTTPError, ValueError):
            results.append({"execution_id": identifier, "status": "reconciliation_required"})
    return results


async def run(args: argparse.Namespace) -> dict[str, Any]:
    owner = "h3val_" + uuid.uuid4().hex[:16]
    output = args.output_dir.resolve() / owner
    if str(output).startswith(("/tmp/", "/var/tmp/")):
        raise ValueError("capacity evidence must use a persistent directory")
    output.mkdir(parents=True, mode=0o700)
    os.chmod(output, 0o700)
    report: dict[str, Any] = {"run_id": owner, "started_at": time.time(), "status": "preflight",
                              "configuration": {"profile": args.profile, "duration": args.duration,
                                                "frame_count": frames_for_duration(args.duration),
                                                "max_parallel": args.max_parallel, "batches_per_level": 2,
                                                "aspect_ratio": args.aspect_ratio}, "batches": [], "owned_ids": []}
    def save() -> None:
        target = output / "report.json"
        target.write_text(json.dumps(report, indent=2))
        os.chmod(target, 0o600)
    save()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    lease = False
    monitor_task = None
    deadline = time.monotonic() + args.timeout
    secret = key_from_service() if getattr(args, "key_from_service", False) else router_key(args.key_file)
    async with httpx.AsyncClient(base_url=private_executor_url(args.fleet_url),
                                 headers={"Authorization": "Bearer " + secret},
                                 timeout=httpx.Timeout(30, connect=5), trust_env=False, follow_redirects=False) as client:
        try:
            response = await client.get("/api/router/capacity")
            response.raise_for_status()
            capacity = response.json()
            # Keep foreign execution IDs and customer payloads out of the report.
            report["preflight"] = {"active_count": len(capacity["active"]), "queues": capacity["queues"],
                                    "resources": capacity["resources"], "policy": capacity["policy"]}
            check_idle(capacity)
            maximum = maximum_for(capacity["policy"], args.profile, frames_for_duration(args.duration))
            experiment = None
            if getattr(args, "experimental_long_concurrency", False):
                experiment = {"profile": args.profile, "frame_count": frames_for_duration(args.duration),
                              "max_parallel": args.max_parallel}
                from app.admission import CapacityPolicy
                CapacityPolicy.validate_experiment(experiment)
                report["experimental_capacity"] = experiment
            if args.max_parallel > maximum and not experiment:
                raise RuntimeError("requested concurrency exceeds the reviewed capacity boundary; no automatic promotion")
            baseline = await asyncio.to_thread(sample_host, report["started_at"])
            report["baseline"] = baseline
            response = await client.get("/api/health")
            response.raise_for_status()
            gpu_ids = {lane["gpu_uuid"] for lane in response.json()["lanes"] if lane["enabled"]}
            if not gpu_ids <= {gpu["uuid"] for gpu in baseline["gpus"]}:
                raise RuntimeError("run this validator on the Ivan host that owns the fleet GPUs")
            start_parallel = 1
            if getattr(args, "previous_report", None):
                levels = verified_prior_levels(args.previous_report, report["configuration"],
                                               {gpu["uuid"] for gpu in baseline["gpus"]})
                start_parallel = max(levels) + 1
                if start_parallel > args.max_parallel:
                    raise ValueError("previous report already covers the requested maximum")
                report["previous_report"] = {"path": str(args.previous_report.resolve()),
                                             "sha256": file_sha256(args.previous_report)}
            report["configuration"]["start_parallel"] = start_parallel
            reason = safety_reason(baseline, baseline, capacity["policy"]["resources"])
            if reason:
                raise RuntimeError(reason)
            if not args.execute:
                report["status"] = "inspected_no_generation"
                return report
            lease_payload = {"owner": owner, "ttl_seconds": 120}
            if experiment:
                lease_payload["experiment"] = experiment
            response = await client.post("/api/router/validation-lease", json=lease_payload)
            response.raise_for_status()
            lease = True
            report["status"] = "running"
            save()

            async def monitor() -> None:
                with (output / "metrics.jsonl").open("w") as handle:
                    while not stop.is_set():
                        try:
                            sample = await asyncio.to_thread(sample_host, report["started_at"])
                            handle.write(json.dumps(sample) + "\n")
                            handle.flush()
                            reason = safety_reason(sample, baseline, capacity["policy"]["resources"])
                            if reason:
                                raise RuntimeError(reason)
                            response = await client.post("/api/router/validation-lease", json={"owner": owner, "ttl_seconds": 120})
                            response.raise_for_status()
                        except Exception as error:
                            report.setdefault("first_fatal", {"type": type(error).__name__, "message": str(error)[:500]})
                            stop.set()
                            return
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            pass

            monitor_task = asyncio.create_task(monitor())
            for level in range(start_parallel, args.max_parallel + 1):
                for batch_index in range(2):
                    if stop.is_set() or time.monotonic() >= deadline:
                        raise RuntimeError("validation stopped or total wall-clock deadline reached")
                    identifiers = [f"{owner}_{level}_{batch_index}_{index}" for index in range(level)]
                    # Persist ownership before a submission with an uncertain outcome.
                    report["owned_ids"].extend(identifiers)
                    batch = {"level": level, "batch": batch_index + 1, "started_at": time.time(), "executions": []}
                    report["batches"].append(batch)
                    save()
                    async def submit(identifier: str, index: int) -> None:
                        fields = {"operation_id": identifier, "profile": args.profile, "mode": "t2v",
                                  "prompt": "A stable cinematic shot of a mountain river, flowing water and quiet natural ambience, no speech or music.",
                                  "duration": str(args.duration), "seed": str(2026090900 + index + batch_index * 3),
                                  "aspect_ratio": args.aspect_ratio, "audio_policy": "native", "watermark": "false"}
                        try:
                            response = await client.post("/api/router/executions", data=fields)
                            response.raise_for_status()
                        except httpx.HTTPError:
                            # Never repeat POST after an unknown side effect.
                            current = await read_execution(client, identifier)
                            if current["status"] == "not_found":
                                raise RuntimeError("submission outcome unknown; inspect owned operation ID before retry")
                    outcomes = await asyncio.gather(*(submit(identifier, index) for index, identifier in enumerate(identifiers)), return_exceptions=True)
                    if any(isinstance(value, BaseException) for value in outcomes):
                        raise RuntimeError("batch submission needs owned-ID reconciliation")
                    peak_parallel = 0
                    while True:
                        if stop.is_set() or time.monotonic() >= deadline:
                            raise RuntimeError("validation stopped or total wall-clock deadline reached")
                        current = await asyncio.gather(*(read_execution(client, identifier) for identifier in identifiers))
                        response = await client.get("/api/router/capacity")
                        response.raise_for_status()
                        live = response.json()
                        if any(not (item.get("execution_id") or "").startswith(owner + "_") for item in live["active"]):
                            raise RuntimeError("foreign work appeared during validation")
                        lanes = {item.get("lane_id") for item in current}
                        peak_parallel = max(peak_parallel, sum(
                            min(1, item["running_count"]) for item in live["queues"] if item["lane_id"] in lanes
                        ))
                        if any(item["status"] in {"failed", "cancelled", "not_found"} for item in current):
                            raise RuntimeError("validation execution did not complete successfully")
                        if all(item["status"] == "completed" for item in current):
                            batch["executions"] = current
                            break
                        await asyncio.sleep(3)
                    batch["peak_parallel_running"] = peak_parallel
                    if peak_parallel < level:
                        raise RuntimeError("the requested concurrency was not observed; serialized completion is not a capacity pass")
                    width, height = (864, 480) if args.profile == "preview" else (1344, 768)
                    if args.aspect_ratio == "9:16":
                        width, height = height, width
                    for identifier, execution in zip(identifiers, batch["executions"]):
                        artifact = output / (identifier + ".mp4")
                        async with client.stream("GET", "/api/router/executions/" + identifier + "/output") as response:
                            response.raise_for_status()
                            with artifact.open("wb") as handle:
                                async for chunk in response.aiter_bytes():
                                    if stop.is_set() or time.monotonic() >= deadline:
                                        raise RuntimeError("validation stopped or total wall-clock deadline reached")
                                    handle.write(chunk)
                        media = json.loads(await asyncio.to_thread(command, "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(artifact)))
                        execution["media_validation"] = validate_media(media, width=width, height=height, length=frames_for_duration(args.duration))
                        execution["artifact"] = artifact.name
                        execution["sha256"] = await asyncio.to_thread(file_sha256, artifact)
                    batch["elapsed_seconds"] = time.time() - batch["started_at"]
                    batch["status"] = "passed"
                    save()
            report["status"] = "passed_evidence_only_no_capacity_promotion"
        except Exception as error:
            report["status"] = "failed"
            report.setdefault("first_fatal", {"type": type(error).__name__, "message": str(error)[:500]})
        finally:
            stop.set()
            if monitor_task:
                await monitor_task
            if report.get("first_fatal"):
                report["status"] = "failed"
            if lease:
                try:
                    report["cleanup"] = await asyncio.wait_for(cancel_owned(client, owner, report["owned_ids"]), timeout=90)
                    if any(item["status"] == "reconciliation_required" for item in report["cleanup"]):
                        report["lease_released"] = False
                        report["status"] = "needs_reconciliation"
                    else:
                        response = await client.request("DELETE", "/api/router/validation-lease", json={"owner": owner})
                        report["lease_released"] = response.is_success
                        if not response.is_success:
                            report["status"] = "needs_reconciliation"
                except Exception:
                    report["lease_released"] = False
                    report["status"] = "needs_reconciliation"
            report["finished_at"] = time.time()
            report["elapsed_seconds"] = report["finished_at"] - report["started_at"]
            save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet-url", required=True)
    credential = parser.add_mutually_exclusive_group(required=True)
    credential.add_argument("--key-file", type=Path)
    credential.add_argument("--key-from-service", action="store_true",
                            help="read the local running fleet process credential in memory; never print or copy it")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--profile", choices=("preview", "quality"), default="preview")
    parser.add_argument("--duration", type=int, choices=range(4, 16), default=5)
    parser.add_argument("--aspect-ratio", choices=("16:9", "9:16"), default="16:9")
    parser.add_argument("--max-parallel", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--previous-report", type=Path,
                        help="continue above a fully passed report after verifying its workload, GPU topology and every artifact hash")
    parser.add_argument("--experimental-long-concurrency", action="store_true",
                        help="explicitly permit unvalidated long concurrency only inside this owned validation lease; never promote production capacity")
    args = parser.parse_args()
    if not 60 <= args.timeout <= 21600:
        parser.error("--timeout must be 60..21600 seconds for the complete staircase")
    os.umask(0o077)
    report = asyncio.run(run(args))
    print(json.dumps({"run_id": report["run_id"], "status": report["status"],
                      "report": str(args.output_dir.resolve() / report["run_id"] / "report.json")}))
    return 0 if report["status"] in {"inspected_no_generation", "passed_evidence_only_no_capacity_promotion"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
