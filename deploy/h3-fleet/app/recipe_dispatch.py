from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from scripts.admission_reconciliation import start_record, observe, finish, record_execution_failure
from scripts.progressive_resource_policy import ProgressiveResourcePolicy
from app.admission import cgroup_limit_bytes
from scripts.recipe_evidence import sampler_overlap
from .admission import BUSY
from .throughput import GIB, MemoryAdmission, candidate_budget, estimate_seconds, load_policy, plan_assignments
from .recipe_telemetry import residual_sample
from .recipe_progress import RecipeProgress
from .recipe_legacy_evidence import validate_legacy_single
from .unified_memory import validate_policy


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pinned_json(reference: dict) -> dict:
    path = Path(reference["path"])
    raw = path.read_bytes()
    if not path.is_absolute() or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("qualification evidence hash mismatch")
    return json.loads(raw)


def owns_listener(process: Path, port: int) -> bool:
    inodes = set()
    for descriptor in (process / "fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    for name in ("tcp", "tcp6"):
        for line in (process / "net" / name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port and fields[9] in inodes:
                return True
    return False


def backend_identity(backend: dict) -> dict:
    process = Path("/proc") / str(backend["pid"])
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[19] != str(backend["start_ticks"]):
        raise ValueError("runtime_process_identity_changed")
    environment = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
    if environment.get(b"CUDA_VISIBLE_DEVICES", b"").decode() != backend["gpu_uuid"]:
        raise ValueError("runtime_gpu_binding_mismatch")
    command_sha = hashlib.sha256((process / "cmdline").read_bytes()).hexdigest()
    if command_sha != backend["cmdline_sha256"]:
        raise ValueError("runtime_parameters_changed")
    cgroup = Path(backend["cgroup_path"])
    aggregate = Path(os.environ.get("H3_COMPUTE_CGROUP", "/sys/fs/cgroup/h3.slice/h3-compute.slice"))
    if cgroup == aggregate or not cgroup.is_relative_to(aggregate):
        raise ValueError("worker_cgroup_outside_protected_aggregate")
    membership = (process / "cgroup").read_text().strip()
    if membership != "0::/" + str(cgroup.relative_to("/sys/fs/cgroup")):
        raise ValueError("runtime_cgroup_binding_mismatch")
    port = urlsplit(backend["url"]).port
    if not owns_listener(process, port):
        raise ValueError("runtime_endpoint_not_owned_by_pinned_pid")
    return {"pid": backend["pid"], "start_ticks": backend["start_ticks"],
            "gpu_uuid": backend["gpu_uuid"], "cgroup_path": str(cgroup),
            "cmdline_sha256": command_sha,
            "url": backend["url"],
            "runtime_version": backend["runtime_version"]}


def verify_backend(backend: dict, catalog) -> None:
    validate_policy(backend)
    parsed = urlsplit(backend["url"])
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username):
        raise ValueError("recipe backend must use a same-host private origin")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", backend["id"]):
        raise ValueError("invalid backend id")
    files = backend["runtime_files"]
    if not files or digest(files) != backend["runtime_version"]:
        raise ValueError("runtime version must bind pinned runtime files")
    root = Path(backend["runtime_root"])
    if not root.is_absolute() or root != root.resolve() or not (root / "main.py").is_file():
        raise ValueError("independent runtime root is required")
    excluded = {".git", "__pycache__", ".pytest_cache", "node_modules", ".cache", "models", "output", "user",
                "input", "temp", "tmp", "venv", ".venv", "env"}
    nested_cache = {".git", "__pycache__", ".pytest_cache", "node_modules", ".cache"}
    required = {str(path) for path in root.rglob("*.py")
                if path.relative_to(root).parts[0] not in excluded
                and not nested_cache.intersection(path.relative_to(root).parts)}
    required.add(str(root / "extra_model_paths.yaml"))
    declared = {source["path"]: source for source in files}
    if len(declared) != len(files) or not required <= declared.keys():
        raise ValueError("runtime source manifest does not cover the complete executable tree")
    aliases = {"comfy_minimax_model.py": "comfy/ldm/minimax/model.py", "comfy_model_base.py": "comfy/model_base.py",
               "t8_sampling.py": "custom_nodes/h3_t8_baseline/sampling.py"}
    for recipe_id in backend["recipes"]:
        entry = catalog.get(recipe_id)
        if entry.get("runtime_version_required") and entry["runtime_version_required"] != backend["runtime_version"]:
            raise ValueError("multimodal_runtime_version_mismatch")
        if entry.get("profile_id") and not backend.get("input_root"):
            raise ValueError("multimodal_registered_input_directory_required")
        source = entry["runtime_source"]
        expected = {aliases[name]: checksum for name, checksum in source.get("source_sha256", {}).items()}
        expected.update({source["plugin_directory"] + "/" + name: checksum
                         for name, checksum in source.get("plugin_python_files", {}).items()})
        for relative, checksum in expected.items():
            if declared.get(str(root / relative), {}).get("sha256") != checksum:
                raise ValueError("required recipe node source does not match its catalog")
    for source in files:
        path = Path(source["path"])
        if not path.is_absolute() or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("runtime source mismatch")


def verify_weights(backend: dict, catalog, cache: dict) -> None:
    declared = {source["filename"]: source for source in backend.get("weight_files", [])}
    for recipe_id in backend["recipes"]:
        entry = catalog.get(recipe_id)
        for weight in [*entry["weights"], *entry.get("metadata_files", [])]:
            source = declared.get(weight["filename"])
            if not source or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")):
                raise ValueError("runtime must pin every recipe weight")
            if weight.get("sha256") and source["sha256"] != weight["sha256"]:
                raise ValueError("runtime weight differs from selected recipe")
            path = Path(source["path"])
            if not path.is_absolute():
                raise ValueError("runtime weight path must be absolute")
            key = (str(path), source["sha256"])
            if key not in cache:
                checksum = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        checksum.update(chunk)
                if checksum.hexdigest() != source["sha256"]:
                    raise ValueError("runtime weight hash mismatch")
                cache[key] = path.stat()


class RecipeDispatcher:
    def __init__(self, fleet, catalog, policy_path: Path) -> None:
        self.fleet = fleet
        self.catalog = catalog
        self.policy = load_policy(policy_path)
        self.enabled = self.policy["enabled"]
        residual = self.policy.get("resource_profile", "generic") == "residual_zram"
        if residual and not self.policy.get("experimental_only", True):
            raise ValueError("residual zram requires an independent experimental profile")
        self.memory = MemoryAdmission(reclaim_factor=self.policy.get("reclaim_factor", 0.5),
                                      stable_seconds=self.policy.get("stable_seconds", 60),
                                      global_margin_bytes=self.policy.get("global_margin_gib", 2) * GIB,
                                      residual_policy=ProgressiveResourcePolicy(profile="residual_zram") if residual else None)
        self.backends = {backend["id"]: backend for backend in self.policy["backends"]}
        if len(self.backends) != len(self.policy["backends"]):
            raise ValueError("duplicate recipe backend id")
        self.snapshot = {"ok": False}
        self.started_at = time.time()
        self.last_error = None
        self.weight_cache = {}
        self.progress = {}
        self.external_queue_reason = None
        if self.enabled:
            for backend in self.backends.values():
                verify_backend(backend, catalog)
                verify_weights(backend, catalog, self.weight_cache)
                lane = self.fleet.lanes_by_id[backend["lane_id"]]
                if lane.gpu_uuid != backend["gpu_uuid"]:
                    raise ValueError("recipe backend physical lane mismatch")

    def public(self) -> dict:
        public = self.catalog.public()
        for profile in public.get("multimodal_profiles", []):
            eligible = [backend for backend in self.backends.values() if self.qualifications(profile["profile_id"], backend)]
            profile["qualified"] = bool(eligible)
            profile["qualification"] = "runtime_evidence_accepted" if eligible else "runtime_not_qualified"
        return {**public, "enabled": self.enabled,
                "experimental_only": self.policy.get("experimental_only", True)}

    async def close(self) -> None:
        for listener in self.progress.values():
            await listener.close()
        self.progress.clear()

    def bind_progress(self, job: dict) -> None:
        listener = self.progress.get(job["prompt_id"])
        if listener and job.get("upstream_prompt_id") and not listener.prompt_id:
            listener.bind(job["upstream_prompt_id"])

    def lane_for(self, job: dict):
        raw = job.get("backend_json")
        if not raw:
            return self.fleet.lanes_by_id[job["lane_id"]]
        binding = json.loads(raw)
        lane_type = type(self.fleet.lanes[0])
        return lane_type(**binding["lane"])

    def control(self, name: str, default=None):
        with self.fleet.store._connect() as connection:
            row = connection.execute("SELECT value FROM controls WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def audit(self, event: str, details: dict) -> None:
        with self.fleet.store._connect() as connection:
            connection.execute("INSERT INTO recipe_audit(timestamp,event,details_json) VALUES (?,?,?)",
                               (time.time(), event, json.dumps(details)))
            from .backend_residue import record_settled
            record_settled(connection, event, details)

    def qualifications(self, recipe_id: str, backend: dict) -> bool:
        entry = backend.get("recipes", {}).get(recipe_id)
        if not entry or self.control("quarantine:" + self.profile_key(recipe_id, backend)):
            return False
        version = self.catalog.get(recipe_id)["version"]
        if entry.get("recipe_version") != version:
            return False
        if entry.get("qualification") == "trial":
            return bool(self.policy.get("experimental_only", True)
                        and recipe_id in self.policy.get("validation_cases", []))
        try:
            if entry.get("qualification") == "historical_single_completed":
                receipt = validate_legacy_single(entry, backend, self.catalog)
                return receipt["recipe_id"] == recipe_id and receipt["single_task_only"] is True
            report = pinned_json(entry["evidence"])
            expected = {"recipe_id": recipe_id, "recipe_version": version, "gpu_uuid": backend["gpu_uuid"],
                        "runtime_version": backend["runtime_version"], "status": "completed",
                        "recipe_digest": self.catalog.get(recipe_id)["recipe_digest"]}
            if not all(report.get(key) == value for key, value in expected.items()):
                return False
            graph = pinned_json(report["workflow"])
            self.catalog.validate(recipe_id, graph, version)
            history = pinned_json(report["history"])
            upstream_id = report["upstream_prompt_id"]
            record = history[upstream_id]
            messages = record["status"]["messages"]
            sampler = self.catalog.get(recipe_id)["sampling"]["sampler_node"]
            cached = {str(node) for kind, detail in messages if kind == "execution_cached" for node in detail.get("nodes", [])}
            progress = pinned_json(report["sampler_progress"])
            if (progress.get("prompt_id") != upstream_id or progress.get("cached") or not progress.get("sampler_progress_events")
                    or not progress.get("sampler_intervals") or any(interval.get("finished_at") is None
                                                                 for interval in progress["sampler_intervals"])):
                return False
            if (sampler in cached or record["status"].get("status_str") != "success"
                    or record["prompt"][2] != graph):
                return False
            media = pinned_json(report["media_probe"])
            if (media.get("ok") is not True or [media.get(field) for field in ("width", "height", "frame_count", "fps")]
                    != [480, 864, 362, 24] or not media.get("audio_codec")):
                return False
            artifact = Path(report["artifact"]["path"])
            checksum = hashlib.sha256()
            with artifact.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    checksum.update(chunk)
            return (artifact.is_absolute() and checksum.hexdigest() == report["artifact"]["sha256"]
                    and media.get("artifact_sha256") == checksum.hexdigest())
        except (OSError, KeyError, ValueError, TypeError):
            return False

    def profile_key(self, recipe_id: str, backend: dict) -> str:
        return digest({"recipe_id": recipe_id, "recipe_version": self.catalog.get(recipe_id)["version"],
                       "gpu_uuid": backend["gpu_uuid"], "runtime_version": backend["runtime_version"],
                       "cmdline_sha256": backend["cmdline_sha256"]})

    def active_tasks(self, rows: list[dict]) -> list[dict]:
        tasks = []
        for row in rows:
            if row["status"] not in BUSY:
                continue
            if not row.get("backend_json") or not row.get("admission_json"):
                raise ValueError("legacy_or_unknown_workload_active")
            binding = json.loads(row["backend_json"])
            admission = json.loads(row["admission_json"])
            tasks.append({**admission, "backend_id": binding["id"], "recipe_id": row["recipe_id"],
                          "gpu_uuid": binding["lane"]["gpu_uuid"], "runtime_version": binding["runtime_version"],
                          "prompt_id": row["prompt_id"], "status": row["status"],
                          "observed_worker_peak_bytes": row["worker_peak_bytes"] or admission["worker_baseline_bytes"]})
        return tasks

    def candidate(self, recipe_id: str, backend: dict, job=None) -> dict:
        entry = backend["recipes"][recipe_id]
        with self.fleet.store._connect() as connection:
            rows = connection.execute("SELECT admission_json,worker_peak_bytes FROM jobs WHERE recipe_id=? AND backend_json IS NOT NULL",
                                      (recipe_id,)).fetchall()
        floors = []
        model_floors = []
        for row in rows:
            if not row[0]:
                continue
            previous = json.loads(row[0])
            peak_delta = max(0, (row[1] or 0) - previous["worker_baseline_bytes"])
            floors.extend((previous["candidate_budget_bytes"], peak_delta + max(2 * GIB, (peak_delta + 9) // 10)))
            model_floors.append(previous.get("model_base_budget_bytes", max(previous["candidate_budget_bytes"], floors[-1])))
        if recipe_id in getattr(self.catalog, "entries", {}):
            floors.append(max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24)) * GIB)
        budget = candidate_budget(recipe_id, self.profile_key(recipe_id, backend),
                                  self.policy.get("static_budget_gib", 18) * GIB,
                                  entry.get("peak_history"), floors)
        from .input_memory import apply_input_budget, job_contract
        base = candidate_budget(recipe_id, self.profile_key(recipe_id, backend),
                                max(self.policy.get("static_budget_gib", 18),
                                    max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24))
                                    if recipe_id in getattr(self.catalog, "entries", {}) else 18) * GIB,
                                entry.get("peak_history"), model_floors)
        required = "reference_video" in self.catalog.get(recipe_id).get("asset_roles", {})
        budget = apply_input_budget(budget, job_contract(job), base["candidate_budget_bytes"], required=required)
        return {**budget, "recipe_id": recipe_id, "lane_id": backend["lane_id"], "backend_id": backend["id"],
                "gpu_uuid": backend["gpu_uuid"], "runtime_version": backend["runtime_version"],
                "disk_budget_bytes": GIB, "vram_budget_bytes": entry["vram_budget_bytes"],
                "worker_pid": backend.get("pid"), "unified_memory": backend.get("unified_memory")}

    def mixed_allowed(self, tasks: list[dict]) -> bool:
        if len(tasks) <= 1:
            return True
        signatures = sorted((task["recipe_id"], task["gpu_uuid"], task["runtime_version"]) for task in tasks)
        combinations = list(self.policy.get("qualified_combinations", []))
        if self.policy.get("experimental_only", True):
            combinations += self.policy.get("validation_combinations", [])
        for combination in combinations:
            members = [tuple(member) for member in combination["members"]]
            if all(signature in members for signature in signatures) and len(set(signatures)) == len(signatures):
                if combination.get("trial") is True and self.policy.get("experimental_only", True):
                    return True
                try:
                    evidence = pinned_json(combination["evidence"])
                    measured = sampler_overlap(evidence["tasks"])
                    observed_members = sorted((task["recipe_id"], task["backend"]["lane"]["gpu_uuid"],
                                               task["backend"]["runtime_version"]) for task in evidence["tasks"])
                    if (measured["parallel_validated"] and measured["peak_parallel"] >= len(members)
                            and observed_members == sorted(members)):
                        return True
                except (OSError, KeyError, ValueError, TypeError):
                    pass
        return False

    def sample_local(self, sample: dict) -> dict:
        sample = copy.deepcopy(sample)
        sample["worker_memory_bytes"] = {}
        sample["backend_identities"] = {}
        sample["backend_errors"] = {}
        observed_backends = dict(self.backends)
        for row in self.fleet.store.active():
            if row.get("backend_json") and row["status"] in BUSY:
                binding = json.loads(row["backend_json"])
                observed_backends.setdefault(binding["id"], {"id": binding["id"], **binding["identity"]})
        for backend in observed_backends.values():
            try:
                identity = backend_identity(backend)
                sample["backend_identities"][backend["id"]] = identity
                sample["worker_memory_bytes"][backend["id"]] = int((Path(backend["cgroup_path"]) / "memory.current").read_text())
            except (OSError, ValueError, KeyError) as error:
                sample["backend_errors"][backend["id"]] = str(error)
        try:
            allocation = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_gpu_memory", "--format=csv,noheader,nounits"],
                                        capture_output=True, text=True, timeout=5, check=True)
            sample["gpu_process_memory"] = {}
            sample["gpu_process_identities"] = []
            for line in allocation.stdout.splitlines():
                process_id, gpu_uuid, memory_mib = (value.strip() for value in line.split(","))
                sample["gpu_process_identities"].append((process_id, gpu_uuid))
                # Shared-memory allocation is not reusable discrete VRAM.
                if memory_mib.isdigit():
                    sample["gpu_process_memory"][process_id + ":" + gpu_uuid] = int(memory_mib) * 1024**2
                elif not any(b.get("unified_memory") and b["gpu_uuid"] == gpu_uuid for b in observed_backends.values()):
                    raise ValueError("unknown_gpu_memory_telemetry")
            if any(b.get("unified_memory") for b in observed_backends.values()):
                names = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name", "--format=csv,noheader"], capture_output=True, text=True, timeout=5, check=True)
                sample["gpu_names"] = dict(tuple(v.strip() for v in line.split(",", 1)) for line in names.stdout.splitlines())
            kernel = subprocess.run(["journalctl", "-k", "--since", "@" + str(int(self.started_at)),
                                     "--no-pager", "-o", "cat"], capture_output=True, text=True, timeout=5, check=True)
            sample["kernel_alerts"] = [line[-500:] for line in kernel.stdout.splitlines()
                                       if re.search(r"NVRM: Xid|oom-kill|Out of memory|Killed process", line)]
            if self.memory.residual_policy is not None:
                residual_sample(sample)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            sample["ok"] = False
            sample["reason"] = "kernel_telemetry_unavailable"
        return sample

    async def sample(self) -> dict:
        sample = await self.fleet.capacity_snapshot()
        sample = await asyncio.to_thread(self.sample_local, sample)
        rows = self.fleet.store.active()
        owners = tuple(sorted(row["prompt_id"] for row in rows if row["status"] in BUSY))
        self.snapshot = self.memory.observe(sample, owners, now=time.time())
        for row in rows:
            self.bind_progress(row)
            listener = self.progress.get(row["prompt_id"])
            if listener:
                self.fleet.store.update(row["prompt_id"], progress_json=json.dumps(listener.receipt()))
            if row.get("backend_json") and row.get("reconciliation_json"):
                record = json.loads(row["reconciliation_json"])
                if record["state"] != "observing":
                    continue
                try:
                    observe(record, self.snapshot)
                    worker = json.loads(row["backend_json"])["id"]
                    self.fleet.store.update(row["prompt_id"], reconciliation_json=json.dumps(record),
                                            worker_peak_bytes=max(row.get("worker_peak_bytes") or 0,
                                                                  self.snapshot["worker_memory_bytes"].get(worker, 0)))
                except (KeyError, ValueError, TypeError) as error:
                    self.last_error = "reconciliation_telemetry: " + str(error)
        return self.snapshot

    def decision(self, recipe_id: str, backend: dict, rows: list[dict], sample: dict, health: dict, job=None) -> dict:
        try:
            active = [row for row in rows if row["status"] in BUSY]
            if len(active) >= self.fleet.policy.data["max_active_jobs"]:
                raise ValueError("max_active_jobs_reached")
            historical = backend.get("recipes", {}).get(recipe_id, {}).get("qualification") == "historical_single_completed"
            if active and (historical or any(json.loads(row.get("admission_json") or "{}").get(
                    "qualification") == "historical_single_completed" for row in active)):
                raise ValueError("historical_single_only")
            if not self.qualifications(recipe_id, backend):
                raise ValueError("recipe_hardware_not_qualified_or_quarantined")
            if backend["id"] not in sample.get("backend_identities", {}):
                raise ValueError("backend_identity_unavailable")
            for (path, _), before in self.weight_cache.items():
                after = Path(path).stat()
                if (after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_ino, before.st_size, before.st_mtime_ns):
                    raise ValueError("runtime_weights_changed_after_verification")
            tasks = self.active_tasks(rows)
            candidate = self.candidate(recipe_id, backend, job=job)
            if any(task["gpu_uuid"] == backend["gpu_uuid"] for task in tasks):
                raise ValueError("physical_gpu_reserved")
            if any(task["status"] in {"reserved", "reconciling", "cancelling"} for task in tasks):
                raise ValueError("prior_submission_or_cancellation_unresolved")
            for task in tasks:
                listener = self.progress.get(task["prompt_id"])
                if listener is None or not listener.receipt()["ready"]:
                    raise ValueError("owned_non_cached_sampler_not_stable_for_60_seconds")
            if not self.mixed_allowed([*tasks, candidate]):
                raise ValueError("mixed_recipe_combination_unvalidated")
            if sample.get("kernel_alerts"):
                raise ValueError("kernel_oom_or_xid")
            vram_free = health.get("vram_free")
            warm = self.control("warm:" + backend["id"], {})
            reuse = 0
            if (warm.get("recipe_id") == recipe_id and warm.get("runtime_version") == backend["runtime_version"]
                    and backend["recipes"][recipe_id].get("qualification") not in {"trial", "historical_single_completed"}):
                reuse = sample.get("gpu_process_memory", {}).get(str(backend["pid"]) + ":" + backend["gpu_uuid"], 0)
            if type(vram_free) is int:
                vram_free = min(health.get("vram_total", vram_free), vram_free + reuse)
            decision = self.memory.decide(sample, candidate, tasks, limits=self.fleet.policy.data["resources"],
                                          now=time.time(), vram_free_bytes=vram_free)
            return {**candidate, **decision, "verified_warm_vram_reuse_bytes": reuse,
                    "qualification": backend["recipes"][recipe_id].get("qualification")}
        except (OSError, KeyError, ValueError, TypeError) as error:
            return {"admission": "wait", "reasons": [str(error)]}

    async def available_backends(self, rows: list[dict]) -> tuple[list[dict], dict]:
        occupied = {row["lane_id"] for row in rows if row["status"] in BUSY}
        backends, health = [], {}
        self.external_queue_reason = None
        for backend in self.backends.values():
            if backend["id"] not in self.snapshot.get("backend_identities", {}):
                continue
            lane = self.lane_for_binding(backend)
            try:
                response = await self.fleet.client.get(backend["url"] + "/queue", timeout=5)
                response.raise_for_status()
                queue = response.json()
                if not all(isinstance(queue.get(key), list) for key in ("queue_running", "queue_pending")):
                    self.external_queue_reason = "experimental_queue_telemetry_unavailable"
                    continue
                known = {row["upstream_prompt_id"] for row in rows if row.get("backend_json")
                         and json.loads(row["backend_json"])["id"] == backend["id"] and row["status"] in BUSY}
                records = queue["queue_running"] + queue["queue_pending"]
                if any(len(item) < 2 or str(item[1]) not in known for item in records):
                    self.external_queue_reason = "untracked_experimental_work"
                if records or backend["lane_id"] in occupied:
                    continue
                health[backend["id"]] = await self.fleet.lane_health(lane)
                if health[backend["id"]].get("ok"):
                    backends.append({**backend, "warm_recipe_id": self.control("warm:" + backend["id"], {}).get("recipe_id")})
            except Exception:
                self.external_queue_reason = "experimental_queue_telemetry_unavailable"
                continue
        return backends, health

    def lane_for_binding(self, backend: dict):
        lane = self.fleet.lanes_by_id[backend["lane_id"]]
        return type(lane)(**{**asdict(lane), "url": backend["url"]})

    async def submit(self, payload: dict) -> dict:
        if not self.enabled:
            raise HTTPException(409, "recipe_scheduler_not_enabled")
        metadata = payload["extra_data"]["h3"]
        if metadata.get("profile", "preview") != "preview" or metadata.get("stage", "preview") != "preview":
            raise HTTPException(400, "selected recipes only support T2V preview")
        recipe_id = metadata["recipe_id"]
        try:
            binding = self.catalog.validate(recipe_id, payload["prompt"], metadata.get("recipe_version"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        execution_id = metadata.get("execution_id")
        if not isinstance(execution_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
            raise HTTPException(400, "recipe execution requires a stable execution_id")
        payload = copy.deepcopy(payload)
        metadata = payload["extra_data"]["h3"]
        metadata["contract"] = {**metadata.get("contract", {}), **binding, "actual_duration": 362 / 24,
                                "frame_count": 362, "width": 480, "height": 864, "fps": 24,
                                "steps": self.catalog.get(recipe_id)["sampling"]["steps"]}
        if binding.get("profile_id"):
            from .multimodal_api import verify_assets, asset_root, verified_memory
            metadata["contract"].update({key: binding[key] for key in ("mode", "width", "height", "frame_count", "actual_duration", "fps", "steps")})
            try:
                await asyncio.to_thread(verify_assets, metadata["contract"]["assets"], binding, asset_root(self.fleet))
                metadata["contract"]["verified_input_memory"] = await asyncio.to_thread(
                    verified_memory, metadata["contract"]["assets"], asset_root(self.fleet))
                if not re.fullmatch(r"[a-f0-9]{64}", metadata["contract"]["input_sha256"]):
                    raise ValueError("input digest required")
            except (ValueError, KeyError, TypeError, OSError) as error:
                raise HTTPException(400, "multimodal input verification failed") from error
        else:
            metadata["contract"].pop("verified_input_memory", None)
        from .input_memory import demand_budget
        request_digest = digest(payload)
        async with self.fleet.assignment_lock:
            if self.fleet.draining:
                raise HTTPException(503, "H3 fleet is draining")
            if self.fleet.store.release_gate_reason(execution_id):
                raise HTTPException(503, "release_validation_gate")
            batch = self.fleet.store.studio_batch()
            if batch and not execution_id.startswith(batch["owner"] + "_"):
                raise HTTPException(409, "another Studio batch owns the execution window")
            lease = self.fleet.store.validation_lease()
            if lease and (lease["expires_at"] <= time.time() or not execution_id.startswith(lease["owner"] + "_")):
                raise HTTPException(409, "another validation owner controls the execution window")
            if self.policy.get("experimental_only", True):
                lease = self.fleet.store.validation_lease()
                if not lease or lease["expires_at"] <= time.time() or not execution_id.startswith(lease["owner"] + "_"):
                    raise HTTPException(409, "recipe experiment requires an owned validation lease")
            existing = self.fleet.store.get_by_execution(execution_id)
            if existing:
                if existing["request_digest"] != request_digest:
                    raise HTTPException(409, "execution_id already binds a different request")
                return self.submission_public(existing, replay=True)
            from .multimodal_qualification import check_submission
            check_submission(self, recipe_id, binding, execution_id)
            job = self.fleet.store.create(prompt_id=uuid.uuid4().hex, upstream_prompt_id="", execution_id=execution_id,
                                          request_digest=request_digest, lane_id="", stage="preview", profile="preview",
                                          status="queued", request_data=payload, execution_data=metadata["contract"])
            self.fleet.store.update(job["prompt_id"], recipe_id=recipe_id, recipe_version=binding["recipe_version"],
                                    admission_reason="admission_waiting:awaiting_continuous_stable_window",
                                    demand_json=json.dumps({"class": "long", "profile": "preview", "recipe_id": recipe_id,
                                                            "memory_budget_bytes": demand_budget(metadata["contract"], (max(24, self.fleet.policy.data.get("long", {}).get("memory_budget_gib", 24))
                                                                                    if binding.get("profile_id") else 18) * GIB), "disk_budget_bytes": GIB}))
        if not getattr(self.fleet, "backend_lifecycle", None):
            await self.tick()
        return self.submission_public(self.fleet.store.get(job["prompt_id"]))

    @staticmethod
    def submission_public(job: dict, replay=False) -> dict:
        binding = json.loads(job.get("backend_json") or "{}")
        return {"prompt_id": job["prompt_id"], "h3_lane": job["lane_id"] or None,
                "h3_gpu_uuid": binding.get("lane", {}).get("gpu_uuid"), "queued": job["status"] == "queued",
                "recipe_id": job["recipe_id"], "recipe_version": job["recipe_version"], "idempotent_replay": replay}

    async def tick(self) -> None:
        if not self.enabled:
            return
        async with self.fleet.assignment_lock:
            await self._tick_locked()

    async def fence_reason(self) -> str | None:
        if not self.policy.get("experimental_only", True):
            return None
        origin = self.policy.get("production_fence_url", "")
        parsed = urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "100.96.79.21", "ivan-ms-7b17.taild500c8.ts.net"} or not parsed.port
                or parsed.path or parsed.query or parsed.fragment or parsed.username):
            return "production_fence_not_configured"
        owned = self.fleet.store.validation_lease()
        if not owned:
            return "validation_lease_missing"
        try:
            response = await self.fleet.client.get(origin + "/api/router/capacity", timeout=5,
                headers={"Authorization": "Bearer " + os.environ.get("H3_ROUTER_KEY", "")})
            response.raise_for_status()
            state = response.json()
            lease = state.get("validation_lease")
            if not lease or lease.get("owner") != owned["owner"] or lease.get("expires_at", 0) <= time.time():
                return "production_validation_fence_lost"
            if state.get("active") or any(queue["queued_or_running"] for queue in state["queues"]):
                return "production_work_present"
        except Exception:
            return "production_fence_unverifiable"
        return None

    async def _tick_locked(self) -> None:
        rows = self.fleet.store.active()
        waiting = [row for row in rows if row["status"] == "queued" and row.get("recipe_id")]
        await self.sample()
        rows = self.fleet.store.active()
        if await self.hard_protection(rows):
            return
        if self.control("recipe_hard_stop"):
            self.waiting(waiting, "runtime_hard_stop_requires_operator_reconciliation")
            return
        if not waiting or self.fleet.draining:
            return
        held = [row for row in waiting if self.fleet.store.release_gate_reason(row.get("execution_id"))]
        self.waiting(held, "release_validation_gate")
        waiting = [row for row in waiting if row not in held]
        if not waiting:
            return
        fence = await self.fence_reason()
        if fence:
            self.waiting(waiting, fence)
            return
        lease = self.fleet.store.validation_lease()
        if self.policy.get("experimental_only", True) and (not lease or lease["expires_at"] <= time.time()):
            self.waiting(waiting, "validation_lease_missing_or_expired")
            return
        if lease:
            waiting = [row for row in waiting if (row.get("execution_id") or "").startswith(lease["owner"] + "_")]
        if not waiting:
            return
        queues = await self.fleet.inspect_queues()
        if any(item["untracked_count"] for item in queues):
            self.waiting(waiting, "untracked_upstream_work")
            return
        backends, health = await self.available_backends(rows)
        if self.external_queue_reason:
            self.waiting(waiting, self.external_queue_reason)
            return
        if not await self.cleanup_blocking_warm_backends(waiting, backends, rows, health):
            self.waiting(waiting, "previous_backend_unload_unconfirmed")
            return
        backends, health = await self.available_backends(self.fleet.store.active())
        if self.external_queue_reason:
            self.waiting(waiting, self.external_queue_reason)
            return
        now = time.time()
        protected = [row for row in rows if row["status"] == "queued" and now - row["created_at"] >= 900
                     and not self.fleet.store.release_gate_reason(row.get("execution_id"))]
        if any(not row.get("recipe_id") for row in protected):
            self.waiting(waiting, "protected_legacy_task_waiting")
            return
        candidates = []
        decisions = {}
        with self.fleet.store._connect() as connection:
            history_rows = [dict(row) for row in connection.execute(
                "SELECT recipe_id,backend_json,admission_json,execution_seconds FROM jobs WHERE status='completed' AND backend_json IS NOT NULL AND execution_seconds>0")]
        observations = []
        for row in history_rows:
            binding = json.loads(row["backend_json"])
            observations.append({"recipe_id": row["recipe_id"], "gpu_uuid": binding["lane"]["gpu_uuid"],
                                 "runtime_version": binding["runtime_version"],
                                 "cold": json.loads(row["admission_json"]).get("cold", True),
                                 "execution_seconds": row["execution_seconds"]})
        for row in waiting:
            qualified = [backend for backend in self.backends.values() if self.qualifications(row["recipe_id"], backend)]
            candidate = {**row, "eligible_backend_ids": [backend["id"] for backend in qualified],
                         "eligible_gpu_uuids": sorted({backend["gpu_uuid"] for backend in qualified}), "eta_by_backend": {}}
            for backend in backends:
                decision = self.decision(row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]], job=row)
                decisions[(row["prompt_id"], backend["id"])] = decision
                candidate["eta_by_backend"][backend["id"]] = estimate_seconds(
                    observations, row["recipe_id"], backend["gpu_uuid"], backend["runtime_version"],
                    backend.get("warm_recipe_id") != row["recipe_id"])
            candidates.append(candidate)

        def feasible(assignment):
            if len(assignment) != 1:
                return False
            item = assignment[0]
            if protected and item["job"]["prompt_id"] != protected[0]["prompt_id"]:
                return False
            return decisions[(item["job"]["prompt_id"], item["backend"]["id"])]["admission"] == "allow"

        assignments = plan_assignments(candidates, backends, now=now, feasible=feasible)
        if not assignments:
            for row in waiting:
                reasons = sorted({reason for (identifier, _), decision in decisions.items() if identifier == row["prompt_id"]
                                  for reason in decision["reasons"]})
                self.waiting([row], ";".join(reasons) or "no_qualified_idle_backend")
            return
        selected = assignments[0]
        job, backend = selected["job"], selected["backend"]
        decision = decisions[(job["prompt_id"], backend["id"])]
        if not await self.prepare_backend(backend, job["recipe_id"]):
            self.waiting([job], "previous_backend_unload_unconfirmed")
            return
        await self.sample()
        health_now = await self.fleet.lane_health(self.lane_for_binding(backend))
        fence = await self.fence_reason()
        if fence:
            self.waiting([job], fence)
            return
        with self.fleet.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = dict(connection.execute("SELECT * FROM jobs WHERE prompt_id=?", (job["prompt_id"],)).fetchone())
            if current["status"] != "queued":
                return
            reason = "fleet_draining" if self.fleet.draining else self.fleet.store.release_gate_reason(
                current.get("execution_id"), connection)
            if reason:
                connection.execute("UPDATE jobs SET admission_reason=? WHERE prompt_id=?", (reason, job["prompt_id"]))
                return
            active = [dict(row) for row in connection.execute("SELECT * FROM jobs WHERE status IN ('reserved','reconciling','submitted','running','cancelling')")]
            decision = self.decision(job["recipe_id"], backend, active, self.snapshot, health_now, job=current)
            if decision["admission"] != "allow":
                connection.execute("UPDATE jobs SET admission_reason=? WHERE prompt_id=?",
                                   ("admission_waiting:" + ";".join(decision["reasons"]), job["prompt_id"]))
                return
            identity = backend_identity(backend)
            baseline = self.snapshot["worker_memory_bytes"][backend["id"]]
            decision.update(worker_baseline_bytes=baseline, observed_at=self.snapshot["timestamp"],
                            cold=backend.get("warm_recipe_id") != job["recipe_id"])
            binding = {"id": backend["id"], "runtime_version": backend["runtime_version"],
                       "identity": identity, "lane": asdict(self.lane_for_binding(backend))}
            record = start_record(decision, self.snapshot, job["recipe_id"], {backend["id"]: baseline})
            connection.execute("UPDATE jobs SET status='reserved',lane_id=?,backend_json=?,admission_json=?,reconciliation_json=?,worker_peak_bytes=?,admission_reason=NULL,updated_at=? WHERE prompt_id=?",
                               (backend["lane_id"], json.dumps(binding), json.dumps(decision), json.dumps(record), baseline, time.time(), job["prompt_id"]))
        self.audit("admitted", {"prompt_id": job["prompt_id"], "recipe_id": job["recipe_id"], "backend": binding, "prediction": decision})
        reserved = self.fleet.store.get(job["prompt_id"])
        listener = RecipeProgress(backend["url"], job["prompt_id"], [self.catalog.get(job["recipe_id"])["sampling"]["sampler_node"]])
        try:
            await listener.start()
            fence = await self.fence_reason()
            if fence or self.control("recipe_hard_stop") or self.fleet.draining:
                raise ValueError(fence or "scheduler_stopped_before_submission")
            if self.fleet.store.release_gate_reason(job.get("execution_id")):
                raise ValueError("release_validation_gate")
            backend_identity(backend)
            payload = json.loads(reserved["request_json"])
            contract = payload["extra_data"]["h3"].get("contract", {})
            if contract.get("profile_id"):
                from .multimodal_api import materialize
                await asyncio.to_thread(materialize, self.fleet, backend, contract)
            if time.time() - decision["observed_at"] > 10:
                raise ValueError("admission_expired_during_websocket_handshake")
        except Exception as error:
            await listener.close()
            self.fleet.store.update(job["prompt_id"], status="queued", lane_id="", backend_json=None,
                                    admission_json=None, reconciliation_json=None,
                                    admission_reason="admission_waiting:pre_submission_check_failed:" + str(error))
            self.audit("progress_listener_failed_before_submission", {"prompt_id": job["prompt_id"], "error": str(error)})
            return
        self.progress[job["prompt_id"]] = listener
        await self.fleet.submit_reserved(reserved, json.loads(reserved["request_json"]), self.lane_for(reserved))

    async def hard_protection(self, rows: list[dict]) -> bool:
        owned = [row for row in rows if row.get("backend_json") and row["status"] in BUSY]
        if not owned:
            return False
        sample = self.snapshot
        reasons = []
        if sample.get("kernel_alerts"):
            reasons.append("kernel_oom_or_xid")
        if sample.get("cgroup_current_bytes", 0) > cgroup_limit_bytes(sample, self.fleet.policy.data["resources"]):
            reasons.append("raw_cgroup_hard_guard")
        if sample.get("memory_available_bytes", 16 * GIB) < 16 * GIB:
            reasons.append("host_ram_floor_crossed")
        if max(sample.get("swap_used_bytes", 0), sample.get("cgroup_swap_bytes", 0)) >= 8 * GIB:
            reasons.append("absolute_swap_hard_guard")
        residual = self.memory.residual_decision
        if residual and residual.get("fatal"):
            reasons.extend(residual["reasons"])
        for row in owned:
            record = json.loads(row.get("reconciliation_json") or "{}")
            if any(value.get("delta", 0) and value["delta"] > 0 for key, value in record.get("cgroup_events", {}).items()
                   if key in {"oom", "oom_kill", "max"}):
                reasons.append("cgroup_oom_or_max_event")
        if not reasons:
            return False
        self.audit("runtime_hard_protection", {"reasons": sorted(set(reasons)), "sample": sample,
                                               "owned_prompt_ids": [row["prompt_id"] for row in owned]})
        with self.fleet.store._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO controls VALUES ('recipe_hard_stop',?)",
                               (json.dumps({"reasons": sorted(set(reasons)), "timestamp": time.time()}),))
        self.last_error = "runtime_hard_protection:" + ";".join(sorted(set(reasons)))
        self.waiting([row for row in rows if row["status"] == "queued"], self.last_error)
        for row in owned:
            try:
                backend_identity(json.loads(row["backend_json"])["identity"])
                await self.fleet.cancel_owned_recipe_job(row["prompt_id"])
            except Exception as error:
                self.fleet.store.update(row["prompt_id"], status="reconciling", failure_reason="hard_stop_outcome_unknown")
                self.audit("runtime_hard_stop_unconfirmed", {"prompt_id": row["prompt_id"], "error": str(error)})
        return True

    def waiting(self, rows: list[dict], reason: str) -> None:
        for row in rows:
            value = "admission_waiting:" + reason
            if row.get("admission_reason") != value:
                self.fleet.store.update(row["prompt_id"], admission_reason=value)
                self.audit("admission_waiting", {"prompt_id": row["prompt_id"], "reason": reason})

    async def cleanup_blocking_warm_backends(self, waiting: list[dict], backends: list[dict],
                                           rows: list[dict], health: dict) -> bool:
        changed = False
        for backend in backends:
            if not any("gpu_vram_headroom" in self.decision(
                    row["recipe_id"], backend, rows, self.snapshot, health[backend["id"]], row)["reasons"]
                    for row in waiting):
                continue
            for previous in self.backends.values():
                if previous["gpu_uuid"] != backend["gpu_uuid"] or not self.control("warm:" + previous["id"], {}):
                    continue
                if not await self.unload(previous, reason="blocking_same_gpu_warm_backend"):
                    return False
                changed = True
        if changed:
            await self.sample()
            if await self.hard_protection(self.fleet.store.active()):
                return False
        return True

    async def prepare_backend(self, backend: dict, recipe_id: str) -> bool:
        for previous in self.backends.values():
            if previous["gpu_uuid"] != backend["gpu_uuid"]:
                continue
            warm = self.control("warm:" + previous["id"], {})
            if not warm or previous["id"] == backend["id"] and warm.get("recipe_id") == recipe_id:
                continue
            if not await self.unload(previous, reason="recipe_switch"):
                return False
        return True

    async def unload(self, backend: dict, *, reason: str) -> bool:
        if any(row["status"] in BUSY and (row["lane_id"] == backend["lane_id"] or
               self.fleet.lanes_by_id.get(row["lane_id"]) is None or
               self.fleet.lanes_by_id[row["lane_id"]].gpu_uuid == backend["gpu_uuid"])
               for row in self.fleet.store.active()):
            return False
        try:
            backend_identity(backend)
            response = await self.fleet.client.get(backend["url"] + "/queue", timeout=5)
            response.raise_for_status()
            queue = response.json()
            if any(not isinstance(queue.get(key), list) or queue[key] for key in ("queue_running", "queue_pending")):
                return False
            response = await self.fleet.client.post(backend["url"] + "/free", json={"unload_models": True, "free_memory": True}, timeout=15)
            response.raise_for_status()
            health = await self.fleet.lane_health(self.lane_for_binding(backend))
            confirmed = (health.get("ok") and type(health.get("vram_free")) is int
                         and health["vram_total"] - health["vram_free"] <= GIB)
            self.audit("lifecycle_unload", {"backend_id": backend["id"], "reason": reason, "confirmed": bool(confirmed)})
            if confirmed:
                with self.fleet.store._connect() as connection:
                    connection.execute("DELETE FROM controls WHERE name=?", ("warm:" + backend["id"],))
            return bool(confirmed)
        except Exception as error:
            self.audit("lifecycle_unload_failed", {"backend_id": backend["id"], "reason": str(error)})
            return False

    async def completed(self, job: dict, history: dict) -> None:
        if not job.get("backend_json"):
            return
        binding = json.loads(job["backend_json"])
        listener = self.progress.pop(job["prompt_id"], None)
        progress = listener.receipt() if listener else json.loads(job.get("progress_json") or "null")
        if listener:
            await listener.close()
        record = json.loads(job.get("reconciliation_json") or "null")
        if record and record["state"] == "observing" and history:
            sample = await self.sample()
            refreshed = self.fleet.store.get(job["prompt_id"])
            record = json.loads(refreshed["reconciliation_json"])
            if sample["timestamp"] > record["last_observed_at"]:
                observe(record, sample)
        messages = history.get("status", {}).get("messages", [])
        encoded = json.dumps(messages)
        failed_oom = bool((record or {}).get("cuda_oom_detected") or job.get("failure_reason") == "cuda_oom"
                          or re.search(r"CUDA.*out of memory|OutOfMemoryError|allocation on device", encoded, re.I))
        if record and record["state"] == "observing":
            if job["status"] in {"error", "cancelled", "missing"}:
                record_execution_failure(record, {"cuda_oom_detected": failed_oom, "messages": messages, "terminal_status": job["status"]})
            record = finish(record)
        if failed_oom:
            backend = self.backends.get(binding["id"])
            if backend:
                with self.fleet.store._connect() as connection:
                    connection.execute("INSERT OR REPLACE INTO controls VALUES (?,?)", (
                        "quarantine:" + self.profile_key(job["recipe_id"], backend),
                        json.dumps({"prompt_id": job["prompt_id"], "reason": "cuda_oom", "timestamp": time.time()})))
            self.audit("runtime_quarantined", {"prompt_id": job["prompt_id"], "backend": binding, "cuda_oom": True})
        stamps = {kind: detail.get("timestamp", 0) / 1000 for kind, detail in messages
                  if kind in {"execution_start", "execution_success", "execution_error"} and isinstance(detail, dict)}
        duration = max(0, stamps.get("execution_success", stamps.get("execution_error", 0)) - stamps.get("execution_start", 0))
        self.fleet.store.update(job["prompt_id"], reconciliation_json=json.dumps(record),
                                execution_seconds=duration or job.get("execution_seconds"), progress_json=json.dumps(progress),
                                failure_reason=job.get("failure_reason") or ("cuda_oom" if failed_oom else None))
        if job["status"] == "completed":
            with self.fleet.store._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO controls VALUES (?,?)", (
                    "warm:" + binding["id"], json.dumps({"recipe_id": job["recipe_id"], "idle_since": time.time(),
                                                        "runtime_version": binding["runtime_version"], "identity": binding["identity"],
                                                        "prompt_id": job["prompt_id"]})))
        self.audit("task_finished", {"prompt_id": job["prompt_id"], "prediction_and_actual": record,
                                     "sampler_progress": progress,
                                     "execution_seconds": duration or job.get("execution_seconds"), "cuda_oom": failed_oom,
                                     "quality_review": "pending_human_review"})

    async def idle_cleanup(self) -> None:
        if not self.enabled:
            return
        async with self.fleet.assignment_lock:
            rows = self.fleet.store.active()
            for backend in self.backends.values():
                warm = self.control("warm:" + backend["id"], {})
                if (warm and time.time() - warm["idle_since"] >= self.policy.get("idle_unload_seconds", 300)
                        and not any(row.get("recipe_id") == warm["recipe_id"] for row in rows)):
                    await self.unload(backend, reason="idle_300_seconds")

    async def capacity(self) -> dict:
        output = {}
        rows = self.fleet.store.active()
        lease = self.fleet.store.validation_lease()
        blocked = ("fleet_draining" if self.fleet.draining else "runtime_hard_stop_requires_operator_reconciliation"
                   if self.control("recipe_hard_stop") else "validation_lease_missing_or_expired"
                   if self.policy.get("experimental_only", True) and (not lease or lease["expires_at"] <= time.time()) else None)
        if self.fleet.store.release_validation_gate().get("enabled"):
            blocked = blocked or "release_validation_gate"
        backends, health = await self.available_backends(rows) if self.enabled else ([], {})
        blocked = blocked or self.external_queue_reason
        for recipe_id in ("A4", "A4_C0", "A4_C1", "B8", *getattr(self.catalog, "entries", {})):
            decisions = [self.decision(recipe_id, backend, rows, self.snapshot, health[backend["id"]]) for backend in backends]
            allowed = [decision["lane_id"] for decision in decisions if decision["admission"] == "allow"]
            if blocked:
                allowed = []
            output[recipe_id] = {"available_slots": min(1, len(allowed)), "eligible_lanes": sorted(set(allowed)),
                                 "recipe_version": self.catalog.get(recipe_id)["version"],
                                 "reasons": [blocked] if blocked else sorted({reason for decision in decisions for reason in decision["reasons"]})
                                 if decisions else ["no_qualified_idle_backend" if self.enabled else "recipe_scheduler_not_enabled"],
                                 "max_active_jobs": self.fleet.policy.data["max_active_jobs"],
                                 "progressive": self.fleet.policy.data["max_active_jobs"] > 1, "mixed_parallel_validated": False}
        return output
