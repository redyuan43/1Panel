"""One coordinator, isolated workers, one Fleet validation lease.

No service lifecycle, SSH, installation, model download or production-policy
override. --prepare is the default and performs only local CPU/file work.
--execute must run on the workers' host, after the existing A8->C1 chain ends.
The two Comfy servers must implement pinned 0.34.0 client prompt_id/targeted
interrupt semantics. Each MainPID must own its listening socket, expose only
the supplied full CUDA GPU UUID, and belong to h3-compute.slice.

Invocation:
  python3 run_parallel_comparison.py --manifest batch.json \
    --manifest-sha256 SHA --output-dir /persistent/batch \
    --validator native=/absolute/trusted_adapter.py:validate_workflow --prepare
Repeat the same arguments with --execute to execute a prepared run, once only.

Manifest contract (exactly two distinct cases/endpoints/units/GPUs):
  {"version":1,"run_id":"pink-parallel-1","tasks":[
    {"case":"B8","endpoint":"http://127.0.0.1:18188",
     "unit":"isolated-one.service","gpu_uuid":"GPU-<full UUID>",
     "workflow":"B8.json","workflow_sha256":"<SHA256>",
     "memory_budget_gib":20,"disk_budget_gib":1,
     "validator":"native","validator_sha256":"<adapter file SHA256>"},
    {"case":"new-direction", "...":"same required fields"}]}
Workflow paths are relative to the manifest. Endpoint ports are supplied, not
selected here; production ports and the Fleet port cannot be used as workers.

--validator binds a trusted, CPU-only local Python source file and callable.
Its exact source hash must match the manifest BEFORE execution/import. This is
an explicit local-code trust boundary, not a sandbox for untrusted Python.
The callable validate_workflow(graph, case=...) must reject unknown graphs,
never mutate its input, and return width/height/length/fps/save_node plus
sampler_nodes (optional only when standard SamplerCustomAdvanced is present).
Adapters can audit B/D nodes without weakening this coordinator. Imported
adapter dependencies must be pinned by the operator; no graph auto-rewriting
other than the single SaveVideo filename_prefix is allowed.

Both tasks retain 362 frames at 24fps, with unique IDs and prefixes. Memory
budgets mean ADDITIONAL host/cgroup bytes above the frozen preflight baseline.
The two budgets are reserved together. Swap is absolute <=1GiB, never the
single-worker swap-recovery policy. Unknown cancellation keeps drain and lease.
Only after BOTH workers reconcile AND unload to <=1024MiB can lease DELETE run.
Even successful execution leaves Fleet DRAINED for explicit operator resume;
there is intentionally no /resume call. Drain is not durable across Fleet
restart; this tool cannot provide persistent admission fencing after both
coordinator failure/lease expiry and a Fleet restart. A retained lease has an
explicit bounded expiry and requires operator reconciliation before expiry.

Opt-in --progressive accepts two or three tasks, each with vram_budget_mib
as a declared full peak (including decode), not hardware certification. Host
memory_budget_gib likewise includes decode. Each admission reserves current
effective working set + remaining peak budgets of submitted, not-yet-unloaded
workers + the candidate's full budget + a global safety margin. Only discounted
clean inactive file cache is excluded, never dirty/writeback/unevictable bytes.
Historical exact-profile peaks carry >=2GiB/10% margin and cannot lower the
manifest floor. Unknown history retains the original static budget. It does
NOT reserve all unsubmitted tasks up front or change the 72GiB hard guard.
Before adding a lane, owned prompt_id/sampler progress over >=60s is required
via websockets.sync.client (websockets 15 API, proxy=None), plus continuous
resource observation. Reconnect executing events without prompt_id and load
logs are not progress. Missing progress blocks joining, not existing work.
An explicit --progressive-resource-profile residual_zram uses the independent
experimental policy, never production CapacityPolicy or singlefast recovery.
Insufficient headroom writes task.admission_reason and admission.jsonl; it
does not cancel healthy active tasks. Completed tasks collect/unload first,
then queued candidates may run with a new quiet admission window. All videos
can succeed with parallel_validated=False; only legacy demands overlap.
--fleet-url permits the default or the authorized same-host Tailscale binding
http://100.96.79.21:8789. No lease adoption, service restart or cache reclaim.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from types import ModuleType
from urllib.parse import urlsplit
import uuid

import httpx

from validate_capacity import GIB, check_idle, command, file_sha256, key_from_service, sample_host, safety_reason
from validate_dual import validate_media
from working_set_admission import conservative_working_set
from measured_peak_history import load_history, resolve_budget
from admission_reconciliation import start_record, observe as observe_reconciliation, finish as finish_reconciliation, record_execution_failure


FLEET = "http://127.0.0.1:8789"
WORKERS = {"fast": "http://127.0.0.1:8188", "main": "http://127.0.0.1:8189", "preview": "http://127.0.0.1:8190"}
UNITS = {f"comfyui-h3@{lane}.service" for lane in WORKERS}
CGROUP = "/h3.slice/h3-compute.slice"
LEASE = "/api/router/validation-lease"
CAPACITY = "/api/router/capacity"
OPTIONS = "/api/router/options"
LOCK = Path.home() / ".local/state/h3-parallel-comparison/coordinator.lock"
LIMITS = {"min_available_ram_gib": 16, "max_cgroup_gib": 72, "max_swap_gib": 1,
          "min_root_free_gib": 25, "min_offload_free_gib": 40}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    raw = value if isinstance(value, bytes) else encoded(value)
    temporary = path.with_name(path.name + ".next")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_pinned(path, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("explicit lowercase SHA256 required")
    raw = path.read_bytes()
    if sha(raw) != expected:
        raise ValueError("SHA256 mismatch: " + str(path))
    return raw


def positive(value):
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def fleet_url(value):
    if value not in {FLEET, "http://100.96.79.21:8789"}:
        raise ValueError("Fleet URL must be the explicit authorized same-host endpoint")
    return value


def validate_shape(graph, result):
    if not isinstance(result, dict):
        raise ValueError("validator must return a shape receipt")
    shape = {key: result.get(key) for key in ("width", "height", "length", "fps", "save_node")}
    if (shape["width"], shape["height"]) not in {(480, 864), (768, 1344), (1344, 768)}:
        raise ValueError("unsupported resolution")
    if shape["length"] != 362 or shape["fps"] != 24:
        raise ValueError("full 362-frame 24fps validation required")
    outputs = [identifier for identifier, node in graph.items() if node.get("class_type") == "SaveVideo"]
    if outputs != [shape["save_node"]] or "filename_prefix" not in graph[outputs[0]].get("inputs", {}):
        raise ValueError("exactly one validated SaveVideo node is required")
    samplers = result.get("sampler_nodes", [identifier for identifier, node in graph.items()
                                            if node.get("class_type") == "SamplerCustomAdvanced"])
    if not isinstance(samplers, list) or not samplers or any(identifier not in graph for identifier in samplers):
        raise ValueError("validator must identify actual sampler nodes")
    shape["sampler_nodes"] = sorted(set(samplers))
    return shape


def load_contract(args):
    raw = read_pinned(args.manifest, args.manifest_sha256)
    manifest = json.loads(raw, object_pairs_hook=unique_object)
    if set(manifest) != {"version", "run_id", "tasks"} or manifest["version"] != 1:
        raise ValueError("unknown manifest schema")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", manifest["run_id"]):
        raise ValueError("unsafe run_id")
    if not isinstance(manifest["tasks"], list) or len(manifest["tasks"]) not in ({1, 2, 3} if args.progressive else {2}):
        raise ValueError("exactly two tasks required, or one to three with --progressive")
    bindings = {}
    for binding in args.validator:
        name, source = binding.split("=", 1)
        path, function = source.rsplit(":", 1)
        if name in bindings or not function.isidentifier():
            raise ValueError("duplicate validator or invalid callable")
        bindings[name] = (Path(path).resolve(), function)
    tasks, graphs = [], {}
    fields = {"case", "endpoint", "unit", "gpu_uuid", "workflow", "workflow_sha256",
              "memory_budget_gib", "disk_budget_gib", "validator", "validator_sha256"}
    if args.progressive:
        fields.add("vram_budget_mib")
    for original in manifest["tasks"]:
        if set(original) != fields:
            raise ValueError("unknown/missing task fields")
        task = copy.deepcopy(original)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task["case"]):
            raise ValueError("unsafe case")
        url = urlsplit(task["endpoint"])
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or task["endpoint"] != f"http://127.0.0.1:{url.port}"
                or task["endpoint"] in {*WORKERS.values(), FLEET} or url.port == 8789):
            raise ValueError("worker must be a separate explicit loopback endpoint")
        if task["unit"] in UNITS | {"h3-fleet.service"} or not re.fullmatch(r"[A-Za-z0-9_@.-]+\.service", task["unit"]):
            raise ValueError("worker must have an independent system unit")
        if not re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", task["gpu_uuid"]):
            raise ValueError("full GPU UUID required")
        if any(not positive(task[key]) for key in ("memory_budget_gib", "disk_budget_gib")):
            raise ValueError("explicit positive memory and disk budgets required")
        if args.progressive and not positive(task["vram_budget_mib"]):
            raise ValueError("progressive tasks require a full peak vram_budget_mib including decode")
        path = (args.manifest.resolve().parent / task["workflow"]).resolve()
        graph = json.loads(read_pinned(path, task["workflow_sha256"]), object_pairs_hook=unique_object)
        validator_path, function = bindings[task["validator"]]
        source = read_pinned(validator_path, task["validator_sha256"])
        module = ModuleType("parallel_validator_" + task["validator_sha256"])
        module.__file__ = str(validator_path)
        sys.modules[module.__name__] = module
        exec(compile(source, str(validator_path), "exec"), module.__dict__)
        candidate = copy.deepcopy(graph)
        result = getattr(module, function)(candidate, case=task["case"])
        if candidate != graph:
            raise ValueError("validator mutated the frozen graph")
        task.update(workflow=str(path), validator_path=str(validator_path), validator_function=function,
                    shape=validate_shape(graph, result))
        tasks.append(task)
        graphs[task["case"]] = graph
    admission_config = {
        "model": "effective_working_set_v1", "reclaim_factor": args.reclaim_factor,
        "global_safety_margin_bytes": int(args.global_safety_margin_gib * GIB),
        "peak_safety_margin_bytes": int(args.peak_safety_margin_gib * GIB),
        "peak_safety_margin_ratio": args.peak_safety_margin_ratio,
        "runtime_profile_id": args.runtime_profile_id, "peak_history_sha256": args.peak_history_sha256,
        "auto_calibration": False, "cgroup_limit_ceiling_bytes": 72 * GIB,
    }
    history = None
    if args.peak_history:
        read_pinned(args.peak_history, args.peak_history_sha256)
        history = load_history(args.peak_history)
        read_pinned(args.peak_history, args.peak_history_sha256)
    if args.progressive:
        for task in tasks:
            binding = {key: task[key] for key in ("case", "workflow_sha256", "validator_sha256", "gpu_uuid", "unit", "endpoint", "shape")}
            binding["runtime_profile_id"] = args.runtime_profile_id
            task["profile_binding"] = binding
            task["profile_key"] = sha(json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
            task["peak_budget"] = resolve_budget(task["case"], int(task["memory_budget_gib"] * GIB), history,
                                                 task["profile_key"], admission_config["peak_safety_margin_bytes"],
                                                 args.peak_safety_margin_ratio)
            task["peak_budget"]["candidate_budget_bytes"] = max(int(task["memory_budget_gib"] * GIB),
                                                                task["peak_budget"]["candidate_budget_bytes"])
            task["peak_budget"]["budget_bytes"] = task["peak_budget"]["candidate_budget_bytes"]
            task["peak_budget"]["static_floor_bytes"] = int(task["memory_budget_gib"] * GIB)
    for field in ("case", "endpoint", "unit", "gpu_uuid"):
        if len({task[field] for task in tasks}) != len(tasks):
            raise ValueError("workers require distinct " + field)
    return {"manifest_sha256": sha(raw), "run_id": manifest["run_id"], "tasks": tasks,
            "timeout": args.timeout, "poll_interval": args.poll_interval, "lease_ttl": args.lease_ttl,
            "cleanup_timeout": args.cleanup_timeout, "fleet_url": fleet_url(args.fleet_url),
            "progressive": args.progressive, "progressive_resource_profile": args.progressive_resource_profile,
            "sampler_observation_seconds": args.sampler_observation_seconds,
            "admission_config": admission_config}, graphs


def system_unit(unit):
    raw = command("systemctl", "show", unit, "-p", "Id", "-p", "ActiveState", "-p", "MainPID",
                  "-p", "NRestarts", "-p", "Slice", "-p", "ControlGroup")
    state = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if state.get("Id") != unit or state.get("ActiveState") != "active" or int(state.get("MainPID", 0)) <= 0:
        raise RuntimeError("required service is not active: " + unit)
    stat = (Path("/proc") / state["MainPID"] / "stat").read_text()
    state["start_ticks"] = stat.rsplit(")", 1)[1].split()[19]
    return state


class SwapSamplingError(RuntimeError):
    def __init__(self, error, attempts):
        super().__init__("swap sampling failed: " + type(error).__name__ + ": " + str(error))
        self.attempts = copy.deepcopy(attempts)


class Host:
    def sample(self, since):
        return sample_host(since)

    def fleet_identity(self):
        return system_unit("h3-fleet.service")

    def production_identity(self):
        return {unit: system_unit(unit) for unit in sorted(UNITS)}

    def worker_identity(self, task):
        state = system_unit(task["unit"])
        process = Path("/proc") / state["MainPID"]
        if state["Slice"] != "h3-compute.slice" or not state["ControlGroup"].startswith(CGROUP + "/"):
            raise RuntimeError("isolated worker outside aggregate cgroup")
        if "0::" + state["ControlGroup"] not in (process / "cgroup").read_text().splitlines():
            raise RuntimeError("isolated process cgroup mismatch")
        environment = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
        if environment.get(b"CUDA_VISIBLE_DEVICES", b"").decode() != task["gpu_uuid"]:
            raise RuntimeError("isolated process does not expose only its pinned GPU UUID")
        sockets = set()
        for descriptor in (process / "fd").iterdir():
            try:
                sockets.add(os.readlink(descriptor))
            except FileNotFoundError:
                continue
        port = urlsplit(task["endpoint"]).port
        listeners = [line.split() for line in (process / "net/tcp").read_text().splitlines()[1:]]
        if not any(int(parts[1].split(":")[1], 16) == port and parts[3] == "0A"
                   and "socket:[" + parts[9] + "]" in sockets for parts in listeners):
            raise RuntimeError("endpoint listening socket is not owned by isolated MainPID")
        return state

    def allocations(self):
        raw = command("nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits")
        rows = []
        for line in raw.splitlines():
            process, gpu, memory = (part.strip() for part in line.split(","))
            rows.append({"pid": process, "gpu_uuid": gpu, "mib": int(memory)})
        return rows

    def progressive_inventory(self, identities):
        swap_sample = self.coherent_swap_inventory()
        memory = {}
        for case, identity in identities.items():
            group = identity["ControlGroup"]
            if not group.startswith(CGROUP + "/") or ".." in Path(group).parts:
                raise RuntimeError("untrusted worker memory cgroup")
            memory[case] = int((Path("/sys/fs/cgroup" + group) / "memory.current").read_text())
        raw = command("nvidia-smi", "--query-gpu=uuid,memory.total,memory.free", "--format=csv,noheader,nounits")
        gpus = {}
        for line in raw.splitlines():
            gpu, total, free = (part.strip() for part in line.split(","))
            gpus[gpu] = {"total_mib": int(total), "free_mib": int(free)}
        try:
            stat_started = time.time()
            current_path = Path("/sys/fs/cgroup" + CGROUP) / "memory.current"
            before_stat = int(current_path.read_text())
            stat_raw = (Path("/sys/fs/cgroup" + CGROUP) / "memory.stat").read_text()
            after_stat = int(current_path.read_text())
            stats = dict((key, int(value)) for key, value in (line.split() for line in stat_raw.splitlines()))
            stat_error = None
        except (OSError, ValueError) as error:
            stat_raw, stats, stat_error, before_stat, after_stat = None, None, str(error), None, None
        stat_finished = time.time()
        if not 0 <= stat_finished - stat_started <= 1:
            stats, stat_error = None, "memory_stat sampling window exceeded one second"
        return {"memory_stat": stats, "memory_stat_raw": stat_raw, "memory_stat_error": stat_error,
                "memory_stat_sample_started_at": stat_started, "memory_stat_sample_finished_at": stat_finished,
                "cgroup_current_before_stat_bytes": before_stat, "cgroup_current_after_stat_bytes": after_stat,
                "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
                **swap_sample,
                "worker_memory_bytes": memory, "gpu_memory": gpus}

    def coherent_swap_inventory(self):
        attempts = []
        try:
            return self._coherent_swap_inventory(attempts)
        except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
            raise SwapSamplingError(error, attempts) from error

    def _coherent_swap_inventory(self, attempts):
        from progressive_resource_policy import EvidenceError, parse_inventory

        def used_swap():
            raw = Path("/proc/meminfo").read_text()
            values = {parts[0].rstrip(":"): int(parts[1]) * 1024 for parts in
                      (line.split() for line in raw.splitlines()) if parts[0] in {"SwapTotal:", "SwapFree:"}}
            return values["SwapTotal"] - values["SwapFree"], raw

        for attempt in range(3):
            evidence = {"attempt": attempt + 1, "stage": "reading_meminfo_before"}
            attempts.append(evidence)
            before, before_raw = used_swap()
            evidence.update(before_host_bytes=before, before_meminfo=before_raw, stage="reading_inventory")
            swaps = Path("/proc/swaps").read_text()
            evidence["proc_swaps"] = swaps
            devices = []
            for line in swaps.splitlines()[1:]:
                path = line.split()[0]
                if re.fullmatch(r"/dev/zram[0-9]+", path):
                    directory = Path("/sys/block") / Path(path).name
                    device = {"path": path, "disksize_bytes": int((directory / "disksize").read_text())}
                    device.update({key: (directory / key).read_text() for key in ("backing_dev", "mm_stat", "bd_stat")})
                    devices.append(device)
            cgroup_swap = int((Path("/sys/fs/cgroup" + CGROUP) / "memory.swap.current").read_text())
            after, after_raw = used_swap()
            vmstat = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
            stamp = time.time()
            snapshot = {"timestamp": stamp, "swap_used_bytes": after, "cgroup_swap_bytes": cgroup_swap,
                        "swap_io_pages": {key: int(vmstat[key]) for key in ("pswpin", "pswpout")},
                        "swap_inventory": {"observed_at": stamp, "page_size_bytes": os.sysconf("SC_PAGE_SIZE"),
                                           "proc_swaps": swaps, "devices": devices}}
            reason = None
            try:
                parse_inventory(snapshot, stamp)
            except EvidenceError as error:
                reason = str(error)
            proc_total = sum(int(line.split()[3]) * 1024 for line in swaps.splitlines()[1:]) if reason in {
                None, "host_swap_inventory_mismatch", "cgroup_swap_exceeds_host"} else None
            evidence.update(after_meminfo=after_raw, sample=copy.deepcopy(snapshot), error=reason,
                            proc_used_total_bytes=proc_total, stage="collected")
            if reason not in {None, "host_swap_inventory_mismatch"}:
                break
            if reason is None and before == after:
                break
            reason = reason or "swap_sample_changed_during_collection"
            evidence["error"] = reason
        return {"swap_used_bytes": after, "cgroup_swap_bytes": cgroup_swap, "swap_io_pages": snapshot["swap_io_pages"],
                "swap_inventory": snapshot["swap_inventory"], "swap_sampling_attempts": attempts,
                "swap_sampling_consistent": reason is None, "swap_sampling_error": reason,
                "swap_sampling_peak_host_bytes": max(max(item["before_host_bytes"], item["sample"]["swap_used_bytes"],
                                                           item["proc_used_total_bytes"] or 0)
                                                       for item in attempts),
                "swap_sampling_peak_cgroup_bytes": max(item["sample"]["cgroup_swap_bytes"] for item in attempts)}


def strict_limits(policy):
    result = {}
    for key, default in LIMITS.items():
        value = policy[key]
        if not positive(value):
            raise RuntimeError("invalid resource policy")
        result[key] = max(value, default) if key.startswith("min_") else min(value, default)
    return result


def resource_check(sample, baseline, limits, budget, disk_budget, reserve=False):
    if any(not {"oom", "oom_kill", "max"} <= set(value["cgroup_events"]) for value in (sample, baseline)):
        raise RuntimeError("incomplete cgroup event telemetry")
    services = sample["services"]
    if len(services) != 3 or {unit["Id"] for unit in services} != UNITS:
        raise RuntimeError("production service inventory changed")
    if len({unit["MainPID"] for unit in services}) != 3 or any(int(unit["MainPID"]) <= 0 for unit in services):
        raise RuntimeError("three stable production processes required")
    if sample["boot_id"] != baseline["boot_id"]:
        raise RuntimeError("host boot identity changed")
    reason = safety_reason(sample, baseline, limits)
    if reason:
        raise RuntimeError(reason)
    growth = max(0, sample["swap_used_bytes"] - baseline["swap_used_bytes"],
                 sample["cgroup_swap_bytes"] - baseline["cgroup_swap_bytes"])
    if growth > limits["max_swap_gib"] * GIB:
        raise RuntimeError("swap growth ceiling crossed")
    if max(sample["cgroup_current_bytes"] - baseline["cgroup_current_bytes"],
           baseline["memory_available_bytes"] - sample["memory_available_bytes"]) > budget:
        raise RuntimeError("combined RAM growth budget exceeded")
    if reserve and (sample["memory_available_bytes"] - budget < limits["min_available_ram_gib"] * GIB
                    or sample["cgroup_current_bytes"] + budget > limits["max_cgroup_gib"] * GIB
                    or sample["offload_available_bytes"] - disk_budget < limits["min_offload_free_gib"] * GIB):
        raise RuntimeError("insufficient combined reservation headroom")


class SamplerProgress:
    def __init__(self, task):
        self.task = task
        self.lock = threading.Lock()
        self.connected = False
        self.error = None
        self.node = None
        self.events = []
        self.value = 0
        self.maximum = None
        self.first = None
        self.last = None
        self.invalid = False

    def receive(self, message, now):
        with self.lock:
            data = message.get("data", {})
            if not isinstance(data, dict) or data.get("prompt_id") != self.task["prompt_id"]:
                return
            kind = message.get("type")
            node = data.get("node")
            if kind == "executing":
                if node != self.node:
                    self.first, self.last, self.value, self.maximum = None, None, 0, None
                self.node = node
            elif kind == "execution_cached" and set(map(str, data.get("nodes", []))) & set(self.task["shape"]["sampler_nodes"]):
                self.invalid = True
            elif kind == "progress":
                node = data.get("node", self.node)
                value, maximum = data.get("value"), data.get("max")
                if (node not in self.task["shape"]["sampler_nodes"] or type(value) is not int
                        or type(maximum) is not int or not 0 < value <= maximum):
                    return
                if self.node != node:
                    self.first, self.last, self.value, self.maximum = None, None, 0, None
                self.node = node
                if (self.maximum is not None and maximum != self.maximum) or value <= self.value:
                    self.invalid = True
                    return
                self.first = now if self.first is None else self.first
                self.last, self.value, self.maximum = now, value, maximum
                self.events.append({"received_monotonic": now, "prompt_id": data["prompt_id"],
                                    "node": node, "value": value, "max": maximum})

    def snapshot(self, now, window):
        with self.lock:
            reasons = []
            if not self.connected or self.error:
                reasons.append("sampler_websocket_unavailable")
            if self.invalid:
                reasons.append("sampler_progress_invalid_or_cached")
            if self.first is None or self.last is None or self.value >= (self.maximum or 0):
                reasons.append("no_active_owned_sampler_progress")
            elif self.last - self.first < window or now - self.last > window:
                reasons.append("sampler_peak_observation_window_incomplete_or_stale")
            return {"ready": not reasons, "reasons": reasons, "events": copy.deepcopy(self.events),
                    "node": self.node, "connected": self.connected, "error": self.error}


class SamplerFeed:
    def __init__(self, task):
        self.progress = SamplerProgress(task)
        self.task = task
        self.socket = None
        self.thread = None
        self.stop = threading.Event()

    def start(self):
        from websockets.sync.client import connect
        self.socket = connect(self.task["endpoint"].replace("http://", "ws://", 1)
                              + "/ws?clientId=" + self.task["client_id"],
                              proxy=None, open_timeout=10, close_timeout=2, max_size=2**20)
        self.progress.connected = True

        def receive():
            try:
                while not self.stop.is_set():
                    try:
                        raw = self.socket.recv(timeout=1)
                    except TimeoutError:
                        continue
                    if isinstance(raw, str):
                        message = json.loads(raw, object_pairs_hook=unique_object)
                        if isinstance(message, dict):
                            self.progress.receive(message, time.monotonic())
            except Exception as error:
                with self.progress.lock:
                    self.progress.error = type(error).__name__
            finally:
                self.progress.connected = False

        self.thread = threading.Thread(target=receive, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.socket:
            self.socket.close()
        if self.thread:
            self.thread.join(timeout=3)


def task_memory_budget(task):
    return max(int(task["memory_budget_gib"] * GIB), task.get("peak_budget", {}).get("candidate_budget_bytes", 0))


def remaining_reservation(tasks, candidate, memory, initial, observed_peaks=None):
    remaining = {}
    regrowth = {}
    for task in tasks:
        case = task["case"]
        if type(memory.get(case)) is not int or memory[case] < 0 or type(initial.get(case)) is not int:
            raise RuntimeError("worker memory accounting unavailable")
        if not task["submit_attempted"] or task.get("unloaded"):
            continue
        current_growth = memory[case] - initial[case]
        growth = max(0, current_growth, (observed_peaks or {}).get(case, 0))
        budget = task_memory_budget(task)
        if growth > budget:
            raise RuntimeError("declared worker peak memory budget exceeded: " + case)
        remaining[case] = budget - growth
        regrowth[case] = max(0, growth - current_growth)
    candidate_bytes = task_memory_budget(candidate) if candidate else 0
    return {"remaining_peak_bytes": remaining, "candidate_peak_bytes": candidate_bytes,
            "released_peak_regrowth_reserve_bytes": regrowth,
            "reserved_running_bytes": sum(remaining.values()) + sum(regrowth.values()),
            "additional_memory_bytes": sum(remaining.values()) + sum(regrowth.values()) + candidate_bytes,
            "includes_decode": True, "worker_initial_bytes": dict(initial), "worker_current_bytes": dict(memory),
            "worker_observed_peak_delta_bytes": dict(observed_peaks or {})}


def progressive_admission(tasks, candidate, sample, initial, policy, progress, now, window, observed_since,
                          *, observed_peaks=None, reclaim_factor=0.5, global_safety_margin=2 * GIB):
    if type(global_safety_margin) is not int or global_safety_margin < 2 * GIB:
        raise ValueError("global safety margin must retain at least 2 GiB")
    reservation = remaining_reservation(tasks, candidate, sample["worker_memory_bytes"], initial, observed_peaks)
    reasons = list(policy["reasons"])
    if policy.get("admission", "allow") != "allow":
        reasons.append("resource_policy_not_stable")
    limits = policy["limits"]
    additional = reservation["additional_memory_bytes"] + global_safety_margin
    try:
        working_set = conservative_working_set(
            {"memory_current": sample["cgroup_current_bytes"], "memory_stat": sample.get("memory_stat")},
            reclaim_factor=reclaim_factor, swap_growing=policy.get("swap_growing", True),
            psi_stable=policy.get("psi_stable", False))
        reasons.extend(working_set["reasons"])
    except ValueError as error:
        working_set = {"effective_working_set": sample["cgroup_current_bytes"], "effective_reclaimable": 0,
                       "reclaim_factor": reclaim_factor, "applied_reclaim_factor": 0, "error": str(error)}
        reasons.append("working_set_telemetry_unavailable")
    projected = working_set["effective_working_set"] + additional
    if projected > min(72, limits["max_cgroup_gib"]) * GIB:
        reasons.append("admission_cgroup_remaining_peak_budget")
    if sample["memory_available_bytes"] - additional < limits["min_available_ram_gib"] * GIB:
        reasons.append("admission_host_remaining_peak_budget")
    disk = sum(task["disk_budget_gib"] for task in tasks if task["submit_attempted"] or task is candidate) * GIB
    if sample["offload_available_bytes"] - disk < limits["min_offload_free_gib"] * GIB:
        reasons.append("admission_disk_budget")
    gpu = sample["gpu_memory"].get(candidate["gpu_uuid"], {})
    if (not positive(gpu.get("free_mib")) or not positive(gpu.get("total_mib"))
            or min(gpu["free_mib"], gpu["total_mib"]) < candidate["vram_budget_mib"]):
        reasons.append("candidate_full_vram_budget_unavailable")
    for task in tasks:
        if task["submit_attempted"] and not task.get("unloaded"):
            receipt = progress[task["case"]]
            reasons.extend(task["case"] + ":" + reason for reason in receipt["reasons"])
            if task.get("status") == "submission_unknown":
                reasons.append("prior_submission_unknown")
    if observed_since is None or now - observed_since < window:
        reasons.append("awaiting_continuous_resource_observation")
    return {"admission": "wait" if reasons else "allow", "reasons": list(dict.fromkeys(reasons)),
            "reservation": reservation, "observed_at": sample["timestamp"],
            "working_set": working_set, "global_safety_margin_bytes": global_safety_margin,
            "candidate_budget": candidate.get("peak_budget", {"source": "static_fallback"}),
            "projected_cgroup_bytes": projected,
            "projected_available_bytes": sample["memory_available_bytes"] - additional}


class Coordinator:
    def __init__(self, args, report, graphs, fleet, comfy, host=None):
        self.args, self.report, self.graphs = args, report, graphs
        self.tasks = report["tasks"]
        self.fleet_url = fleet_url(args.fleet_url)
        self.progressive = args.progressive
        self.fleet, self.comfy, self.host = fleet, comfy, host or Host()
        self.stop, self.heartbeat_stop = threading.Event(), threading.Event()
        self.heartbeat = None
        self.heartbeat_error = None
        self.deadline = time.monotonic() + args.timeout
        self.budget = sum(task_memory_budget(task) for task in self.tasks)
        self.disk_budget = sum(task["disk_budget_gib"] for task in self.tasks) * GIB
        self.feeds = {}
        self.resource_policy = None
        self.resource_observed_since = None
        self.last_progressive_sample = None
        self.worker_initial = None
        self.worker_observed_peaks = {}
        self.last_policy = None

    def save(self):
        save(self.args.output_dir / "report.json", self.report)

    def request(self, method, url, body=None):
        client = self.fleet if url.startswith(self.fleet_url + "/") else self.comfy
        response = client.request(method, url, **({"json": body} if body is not None else {}))
        response.raise_for_status()
        return response.json() if response.content else {}

    def lease_info(self):
        return self.request("GET", self.fleet_url + CAPACITY)

    def capacity(self, owned=True):
        capacity = self.lease_info()
        if owned:
            lease = capacity.get("validation_lease") or {}
            if lease.get("owner") != self.report["owner"] or lease.get("expires_at", 0) <= time.time() + 5:
                raise RuntimeError("validation lease lost or near expiry")
            capacity = dict(capacity, validation_lease=None)
        check_idle(capacity)
        if len(capacity["queues"]) != 3 or {row["lane_id"] for row in capacity["queues"]} != set(WORKERS):
            raise RuntimeError("incomplete production queue inventory")
        return capacity

    def owned(self, task, item):
        return (isinstance(item, (list, tuple)) and len(item) >= 4 and item[1] == task["prompt_id"]
                and item[2] == self.graphs[task["case"]] and isinstance(item[3], dict)
                and item[3].get("client_id") == task["client_id"])

    def queues(self, empty=False):
        result = {}
        for url in [*WORKERS.values(), *(task["endpoint"] for task in self.tasks)]:
            queue = self.request("GET", url + "/queue")
            if (not isinstance(queue, dict) or set(queue) != {"queue_running", "queue_pending"}
                    or any(not isinstance(value, list) for value in queue.values())):
                raise RuntimeError("unknown queue schema")
            items = queue["queue_running"] + queue["queue_pending"]
            if url in WORKERS.values():
                if items:
                    raise RuntimeError("production work appeared")
            else:
                task = next(task for task in self.tasks if task["endpoint"] == url)
                if ((empty and items) or len(items) > 1
                        or any(not task["submit_attempted"] or not self.owned(task, item) for item in items)):
                    raise RuntimeError("foreign/duplicate isolated work; destructive action refused")
                result[task["case"]] = queue
        return result

    def identities(self, baseline=False):
        identity = {"fleet": self.host.fleet_identity(),
                    "production": self.host.production_identity(),
                    "workers": {task["case"]: self.host.worker_identity(task) for task in self.tasks}}
        isolated = {value["MainPID"] for value in identity["workers"].values()}
        production = {unit["MainPID"] for unit in self.report["baseline"]["services"]}
        if (len(isolated) != len(self.tasks) or isolated & production
                or identity["fleet"]["MainPID"] in isolated | production):
            raise RuntimeError("isolated and production PIDs are not distinct")
        if set(identity["production"]) != UNITS or any(
                identity["production"][unit["Id"]]["MainPID"] != unit["MainPID"]
                or identity["production"][unit["Id"]]["NRestarts"] != unit["NRestarts"]
                for unit in self.report["baseline"]["services"]):
            raise RuntimeError("production PID/restart identity changed")
        if baseline:
            self.report["identities"] = identity
        elif identity != self.report["identities"]:
            raise RuntimeError("Fleet or isolated PID/unit/cgroup changed")
        return identity

    def monitor(self, baseline=False):
        if self.progressive:
            return self.progressive_monitor(baseline)
        sample = self.host.sample(self.report["started_at"])
        reference = sample if baseline else self.report["baseline"]
        resource_check(sample, reference, self.report["limits"], self.budget, self.disk_budget, reserve=baseline)
        if baseline:
            self.report["baseline"] = sample
        identity = self.identities(baseline)
        rows = self.host.allocations()
        for task in self.tasks:
            pid = identity["workers"][task["case"]]["MainPID"]
            if any(row["pid"] == pid and row["gpu_uuid"] != task["gpu_uuid"] for row in rows):
                raise RuntimeError("isolated process allocated memory on an unassigned GPU")
        with (self.args.output_dir / "metrics.jsonl").open("a") as handle:
            handle.write(encoded({"sample": sample, "identities": identity, "allocations": rows}).decode() + "\n")
        return sample

    def progressive_monitor(self, baseline=False):
        sample = self.host.sample(self.report["started_at"])
        initial_swap = {key: sample[key] for key in ("swap_used_bytes", "cgroup_swap_bytes")}
        if baseline:
            self.report["baseline"] = sample
        try:
            identity = self.identities(baseline)
            sample.update(self.host.progressive_inventory(identity["workers"]))
        except Exception as error:
            if isinstance(error, SwapSamplingError):
                sample["swap_sampling_attempts"] = error.attempts
            self.report["failed_resource_sample"] = dict(sample, inventory_error=str(error))
            self.record_resource_observation(sample)
            with (self.args.output_dir / "metrics.jsonl").open("a") as handle:
                handle.write(encoded({"sample": sample, "inventory_error": str(error)}).decode() + "\n")
            self.save()
            raise
        sample["cgroup_current_initial_sample_bytes"] = sample["cgroup_current_bytes"]
        sample["swap_initial_sample"] = initial_swap
        sample["cgroup_current_bytes"] = max(sample["cgroup_current_bytes"],
                                              sample.get("cgroup_current_before_stat_bytes") or 0,
                                              sample.get("cgroup_current_after_stat_bytes") or 0)
        sample["perworker_subtree"] = {
            task["case"]: {"case": task["case"], "prompt_id": task["prompt_id"],
                           "memory_current_bytes": sample["worker_memory_bytes"][task["case"]],
                           "identity": dict(identity["workers"][task["case"]],
                                            isolated_url=task["endpoint"], gpu_uuid=task["gpu_uuid"],
                                            process_start_ticks=identity["workers"][task["case"]].get("start_ticks"))}
            for task in self.tasks}
        self.record_resource_observation(sample)
        with (self.args.output_dir / "metrics.jsonl").open("a") as handle:
            handle.write(encoded({"sample": sample, "identities": identity}).decode() + "\n")
        self.save()
        active = any(task["submit_attempted"] and not task.get("unloaded") for task in self.tasks)
        limits = dict(self.report["limits"])
        if self.resource_policy is not None:
            limits["max_swap_gib"] = 8
        if sample.get("swap_sampling_consistent") is False:
            raise RuntimeError("incoherent or unsafe swap inventory: " + sample["swap_sampling_error"])
        peak_swap = {"swap_used_bytes": max(sample["swap_used_bytes"], initial_swap["swap_used_bytes"],
                                             sample.get("swap_sampling_peak_host_bytes", 0)),
                     "cgroup_swap_bytes": max(sample["cgroup_swap_bytes"], initial_swap["cgroup_swap_bytes"],
                                               sample.get("swap_sampling_peak_cgroup_bytes", 0))}
        budget = sum(task_memory_budget(task) for task in self.tasks if task["submit_attempted"])
        resource_check(dict(sample, **peak_swap), self.report["baseline"], limits, budget if active else self.budget, 0)
        if self.resource_policy is not None and max(peak_swap[key] - self.report["baseline"][key] for key in peak_swap) > GIB:
            raise RuntimeError("swap growth peak crossed during coherent sampling")
        previous = self.last_progressive_sample
        now = time.monotonic()
        if previous and not 0 < sample["timestamp"] - previous["timestamp"] <= 15:
            raise RuntimeError("progressive telemetry continuity lost")
        if self.resource_policy is not None:
            decision = self.resource_policy.observe(sample, active=active)
        else:
            pressure = sample["memory_pressure"]
            keys = ("host_some", "host_full", "cgroup_some", "cgroup_full")
            pressured = any(pressure[key + "_avg10"] > 0 for key in keys)
            if previous:
                pressured = pressured or any(pressure[key + "_total"] > previous["memory_pressure"][key + "_total"] for key in keys)
                if any(pressure[key + "_total"] < previous["memory_pressure"][key + "_total"] for key in keys):
                    raise RuntimeError("PSI counter reset")
            if pressured and active:
                raise RuntimeError("memory PSI pressure")
            swapped = previous is not None and sample["swap_io_pages"]["pswpout"] > previous["swap_io_pages"]["pswpout"]
            decision = {"admission": "wait" if pressured or swapped else "allow", "fatal": False,
                        "reasons": (["memory_psi_pressure"] if pressured else []) + (["new_swapout"] if swapped else [])}
        if decision["fatal"]:
            raise RuntimeError("progressive resource hard stop: " + ";".join(decision["reasons"]))
        window_reasons = {"awaiting_60s_stable_baseline", "awaiting_60s_zero_psi_admission"}
        if set(decision["reasons"]) - window_reasons:
            self.resource_observed_since = None
        elif self.resource_observed_since is None:
            self.resource_observed_since = now
        swap_growing = previous is None or any(
            peak_swap[key] > max(previous[key], previous.get("swap_initial_sample", {}).get(key, 0),
                                  previous.get("swap_sampling_peak_" + scope + "_bytes", 0))
            for scope, key in (("host", "swap_used_bytes"), ("cgroup", "cgroup_swap_bytes")))
        swap_growing = swap_growing or (previous is not None and
                                       sample["swap_io_pages"]["pswpout"] > previous["swap_io_pages"]["pswpout"])
        pressure = sample["memory_pressure"]
        pressure_keys = ("host_some", "host_full", "cgroup_some", "cgroup_full")
        psi_stable = previous is not None and all(
            pressure[key + "_avg10"] == 0 and pressure[key + "_total"] == previous["memory_pressure"][key + "_total"]
            for key in pressure_keys)
        if swap_growing or not psi_stable:
            self.resource_observed_since = None
        self.last_policy = dict(decision, limits=self.report["limits"], swap_growing=swap_growing, psi_stable=psi_stable)
        rows = self.host.allocations()
        for task in self.tasks:
            owned = [row for row in rows if row["pid"] == identity["workers"][task["case"]]["MainPID"]]
            if any(row["gpu_uuid"] != task["gpu_uuid"] for row in owned):
                raise RuntimeError("isolated process allocated memory on an unassigned GPU")
            if task["submit_attempted"] and sum(row["mib"] for row in owned) > task["vram_budget_mib"]:
                raise RuntimeError("declared worker VRAM peak budget exceeded: " + task["case"])
        if self.worker_initial is not None:
            for task in self.tasks:
                case = task["case"]
                if task["submit_attempted"] and not task.get("unloaded"):
                    self.worker_observed_peaks[case] = max(self.worker_observed_peaks.get(case, 0),
                                                         sample["worker_memory_bytes"][case] - self.worker_initial[case])
            remaining_reservation(self.tasks, None, sample["worker_memory_bytes"], self.worker_initial,
                                  self.worker_observed_peaks)
        self.last_progressive_sample = sample
        peak = self.report.setdefault("observed_resources", {})
        peak["cgroup_peak_bytes"] = max(sample["cgroup_current_bytes"], peak.get("cgroup_peak_bytes", 0))
        peak["available_min_bytes"] = min(sample["memory_available_bytes"], peak.get("available_min_bytes", sample["memory_available_bytes"]))
        self.report["resource_policy"] = self.last_policy
        self.report["last_allocations"] = rows
        return sample

    def record_resource_observation(self, sample):
        for task in self.tasks:
            record = task.get("resource_reconciliation")
            if record and record["state"] == "observing":
                try:
                    observe_reconciliation(record, sample)
                except ValueError as error:
                    record["observation_error"] = str(error)

    def fenced(self):
        self.capacity()
        self.identities()
        if self.request("GET", self.fleet_url + OPTIONS).get("draining") is not True:
            raise RuntimeError("production drain lost")

    def guard(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError(self.heartbeat_error or "stop/deadline reached")
        if self.progressive:
            self.monitor()
        self.fenced()
        if not self.progressive:
            self.monitor()
        queues = self.queues()
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError(self.heartbeat_error or "stop/deadline reached")
        return queues

    def start_heartbeat(self):
        def watch():
            while not self.heartbeat_stop.wait(self.args.lease_ttl / 3):
                try:
                    self.renew()
                except Exception as error:
                    self.heartbeat_error = str(error)
                    self.stop.set()
                    return
        self.heartbeat = threading.Thread(target=watch, daemon=True)
        self.heartbeat.start()

    def renew(self):
        self.capacity()
        if self.host.fleet_identity() != self.report["identities"]["fleet"]:
            raise RuntimeError("Fleet identity changed")
        if self.request("GET", self.fleet_url + OPTIONS).get("draining") is not True:
            raise RuntimeError("drain lost during renewal")
        self.request("POST", self.fleet_url + LEASE, {"owner": self.report["owner"], "ttl_seconds": self.args.lease_ttl})

    def history(self, task):
        history = self.request("GET", task["endpoint"] + "/history/" + task["prompt_id"])
        if not isinstance(history, dict) or set(history) - {task["prompt_id"]}:
            raise RuntimeError("foreign history response")
        record = history.get(task["prompt_id"])
        if record is not None:
            if not self.owned(task, record.get("prompt")):
                raise RuntimeError("history identity/workflow mismatch")
            save(self.args.output_dir / task["case"] / "history.json", history)
        return record

    def free(self, targets, deadline):
        for target in targets:
            self.fenced()
            self.queues(empty=True)
            self.report.setdefault("free_attempts", []).append(target)
            self.save()
            self.request("POST", target["endpoint"] + "/free", {"unload_models": True, "free_memory": True})
        while True:
            self.fenced()
            self.queues(empty=True)
            rows = self.host.allocations()
            usage = {target["pid"]: sum(row["mib"] for row in rows if row["pid"] == target["pid"]) for target in targets}
            self.report["last_unload_vram_mib"] = usage
            self.save()
            if all(value <= 1024 for value in usage.values()):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("unload not confirmed at <=1024 MiB")
            time.sleep(self.args.poll_interval)

    def clear_idle_models(self):
        rows = self.host.allocations()
        devices = {task["gpu_uuid"] for task in self.tasks}
        targets = []
        for unit in self.report["baseline"]["services"]:
            if any(row["pid"] == unit["MainPID"] and row["gpu_uuid"] in devices for row in rows):
                lane = next(lane for lane in WORKERS if unit["Id"] == f"comfyui-h3@{lane}.service")
                targets.append({"endpoint": WORKERS[lane], "pid": unit["MainPID"]})
        targets.extend(self.isolated_targets())
        self.free(targets, time.monotonic() + self.args.cleanup_timeout)

    def isolated_targets(self):
        return [{"endpoint": task["endpoint"], "pid": self.report["identities"]["workers"][task["case"]]["MainPID"]}
                for task in self.tasks]

    def submit(self, task):
        self.guard()
        if task["submit_attempted"]:
            raise RuntimeError("never resubmit an attempted prompt")
        if self.progressive:
            decision = self.admission_for(task)
            task["admission"] = decision
            task["admission_reason"] = ";".join(decision["reasons"]) or "admitted_with_remaining_peak_reservations"
            self.save()
            if decision["admission"] != "allow":
                return
            self.worker_initial[task["case"]] = self.last_progressive_sample["worker_memory_bytes"][task["case"]]
            self.worker_observed_peaks[task["case"]] = 0
            task["admission_baseline"] = copy.deepcopy(self.last_progressive_sample)
            task["resource_reconciliation"] = start_record(decision, self.last_progressive_sample,
                                                            task["case"], self.last_progressive_sample["worker_memory_bytes"])
        task.update(submit_attempted=True, submitted_at=time.time(), status="submitting")
        self.save()
        try:
            response = self.request("POST", task["endpoint"] + "/prompt", {
                "prompt": self.graphs[task["case"]], "prompt_id": task["prompt_id"], "client_id": task["client_id"],
                "extra_data": {"parallel_comparison": {"run_id": self.report["run_id"], "case": task["case"]}}})
            if response.get("prompt_id") != task["prompt_id"]:
                raise RuntimeError("server did not honor preallocated prompt_id")
            task["status"] = "submitted"
        except httpx.HTTPStatusError as error:
            try:
                rejection = error.response.json()
            except ValueError:
                rejection = None
            if error.response.status_code == 400 and isinstance(rejection, dict) and rejection.get("error") and not rejection.get("prompt_id"):
                task.update(rejected_http400=True, rejection=rejection)
            self.save()
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            task["status"] = "submission_unknown"
        self.save()

    def evidence(self, task, record):
        status = record.get("status", {})
        messages = status.get("messages", [])
        failures = []
        for event, body in messages:
            if event not in {"execution_error", "execution_interrupted"}:
                continue
            failure = {key: copy.deepcopy(body.get(key)) for key in (
                "prompt_id", "node_id", "node_type", "exception_type", "exception_message", "timestamp")}
            failure["event"] = event
            failure["cuda_oom_detected"] = (
                body.get("exception_type") == "torch.cuda.OutOfMemoryError"
                or re.search(r"cuda.*out of memory|out of memory.*cuda|allocation on device.*out of memory|ran out of memory on your gpu",
                             str(body.get("exception_message", "")), re.IGNORECASE | re.DOTALL) is not None)
            failures.append(failure)
        starts = [body["timestamp"] for event, body in messages if event == "execution_start"]
        ends = [body["timestamp"] for event, body in messages if event == "execution_success"]
        cached = {str(node) for event, body in messages if event == "execution_cached" for node in body.get("nodes", [])}
        if (failures or status.get("completed") is not True or status.get("status_str") != "success" or not starts or not ends
                or not any(event == "execution_cached" for event, _ in messages)
                or ends[-1] <= starts[0] or cached & set(task["shape"]["sampler_nodes"])):
            reason = "history does not prove successful non-cached sampling"
            primary = next((failure for failure in failures if failure["event"] == "execution_error"),
                           failures[0] if failures else {})
            failure = {**copy.deepcopy(primary), "prompt_id": task["prompt_id"],
                       "reported_prompt_id": primary.get("prompt_id"),
                       "status": {"status_str": status.get("status_str"), "completed": status.get("completed")},
                       "reason": reason, "events": failures,
                       "cuda_oom_detected": any(item["cuda_oom_detected"] for item in failures)}
            task.update(status="failed", execution_failure=failure, cuda_oom_detected=failure["cuda_oom_detected"])
            task.pop("execution", None)
            if task.get("resource_reconciliation") is not None:
                record_execution_failure(task["resource_reconciliation"], failure)
            self.save()
            detail = ": " + str(primary.get("exception_type")) + " at node " + str(primary.get("node_id")) if primary else ""
            raise RuntimeError(reason + detail)
        task["execution"] = {"started_at": starts[0] / 1000, "finished_at": ends[-1] / 1000,
                             "execution_seconds": (ends[-1] - starts[0]) / 1000,
                             "sampler_nodes": task["shape"]["sampler_nodes"], "cached_nodes": sorted(cached)}
        task["status"] = "sampled"

    def collect(self, task, record):
        artifacts = record.get("outputs", {}).get(task["shape"]["save_node"], {}).get("images", [])
        if len(artifacts) != 1:
            raise RuntimeError("expected exactly one video artifact")
        artifact = artifacts[0]
        parent, prefix = task["prefix"].rsplit("/", 1)
        filename = artifact.get("filename", "")
        if (artifact.get("type") != "output" or artifact.get("subfolder") != parent
                or not filename.startswith(prefix + "_") or not filename.endswith(".mp4")
                or any(character in filename for character in ("/", "\\", ":"))):
            raise RuntimeError("artifact does not belong to the frozen run prefix")
        path = self.args.output_dir / task["case"] / "video.mp4"
        with self.comfy.stream("GET", task["endpoint"] + "/view",
                               params={key: artifact[key] for key in ("filename", "subfolder", "type")}) as response:
            response.raise_for_status()
            next_sample = 0
            with path.open("xb") as handle:
                for chunk in response.iter_bytes():
                    if time.monotonic() >= next_sample:
                        self.guard()
                        next_sample = time.monotonic() + self.args.poll_interval
                    handle.write(chunk)
        probe = json.loads(command("ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)))
        save(path.with_name("media-probe.json"), probe)
        shape = task["shape"]
        media = validate_media(probe, width=shape["width"], height=shape["height"], length=362, fps=24)
        videos = [stream for stream in probe["streams"] if stream.get("codec_type") == "video"]
        if len(videos) != 1 or int(videos[0].get("nb_read_frames", 0)) != 362:
            raise RuntimeError("video is not exactly 362 frames")
        task.update(media=media, artifact_sha256=file_sha256(path), status="collected")
        self.save()

    def reconcile(self):
        deadline = time.monotonic() + self.args.cleanup_timeout
        while time.monotonic() < deadline:
            self.fenced()
            queues = self.queues()
            reconciled = []
            for task in self.tasks:
                queue = queues[task["case"]]
                record = self.history(task)
                if not any(queue.values()):
                    if not task["submit_attempted"] and record is None:
                        task["reconciliation"] = "never submitted; empty queue/history"
                    elif task.get("rejected_http400") and record is None:
                        task["reconciliation"] = "explicit HTTP400; empty queue/history"
                    elif record and record.get("status", {}).get("status_str") in {"success", "error"}:
                        if task.get("rejected_http400"):
                            raise RuntimeError("HTTP400 rejection contradicts terminal history")
                        task["reconciliation"] = "owned terminal history; empty queue"
                    if task.get("reconciliation"):
                        reconciled.append(task["case"])
                        continue
                if queue["queue_running"] and not task.get("interrupt_attempted"):
                    self.queues()
                    task["interrupt_attempted"] = True
                    self.save()
                    self.request("POST", task["endpoint"] + "/interrupt", {"prompt_id": task["prompt_id"]})
                if queue["queue_pending"] and not task.get("delete_attempted"):
                    self.queues()
                    task["delete_attempted"] = True
                    self.save()
                    self.request("POST", task["endpoint"] + "/queue", {"delete": [task["prompt_id"]]})
            self.save()
            if len(reconciled) == len(self.tasks):
                self.queues(empty=True)
                return
            time.sleep(self.args.poll_interval)
        raise RuntimeError("owned submission/cancellation outcome unknown")

    def finish(self):
        if not self.report.get("lease_attempted"):
            return
        try:
            self.capacity()
            if not self.report.get("drain_attempted"):
                if any(task["submit_attempted"] for task in self.tasks):
                    raise RuntimeError("submission without drain")
                self.queues(empty=True)
            else:
                self.reconcile()
                self.free(self.isolated_targets(), time.monotonic() + self.args.cleanup_timeout)
                self.report["both_unloaded"] = True
            self.heartbeat_stop.set()
            if self.heartbeat:
                self.heartbeat.join()
            self.capacity()
            self.identities()
            self.queues(empty=True)
            if self.report.get("drain_attempted"):
                self.fenced()
            self.request("DELETE", self.fleet_url + LEASE, {"owner": self.report["owner"]})
            lease = self.lease_info().get("validation_lease")
            if lease and lease.get("owner") == self.report["owner"]:
                raise RuntimeError("lease release unconfirmed")
            self.report["lease_released"] = True
        except Exception as error:
            self.report.update(status="needs_reconciliation", reconciliation_error=str(error)[:1000])
            self.heartbeat_stop.set()
            if self.heartbeat:
                self.heartbeat.join()
            try:
                self.capacity()
                self.request("POST", self.fleet_url + LEASE, {"owner": self.report["owner"], "ttl_seconds": 21600})
                self.report["retained_lease"] = self.lease_info().get("validation_lease")
            except Exception as hold_error:
                self.report["lease_hold_error"] = str(hold_error)[:1000]
        finally:
            self.heartbeat_stop.set()
            if self.heartbeat:
                self.heartbeat.join()
            self.report["automatic_resume"] = False

    def execute(self):
        if self.progressive:
            return self.execute_progressive()
        handlers = {signum: signal.signal(signum, lambda *_: self.stop.set()) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            capacity = self.capacity(owned=False)
            if self.request("GET", self.fleet_url + OPTIONS).get("draining") is not False:
                raise RuntimeError("existing/unknown drain belongs to another operation")
            self.report["limits"] = strict_limits(capacity["policy"]["resources"])
            self.queues(empty=True)
            self.monitor(baseline=True)
            for task in self.tasks:
                stats = self.request("GET", task["endpoint"] + "/system_stats")
                if stats.get("system", {}).get("comfyui_version") != "0.34.0" or self.history(task) is not None:
                    raise RuntimeError("unaudited server API or reused prompt ID")
            self.report["lease_attempted"] = True
            self.save()
            self.request("POST", self.fleet_url + LEASE, {"owner": self.report["owner"], "ttl_seconds": self.args.lease_ttl})
            self.capacity()
            self.report["drain_attempted"] = True
            self.save()
            self.request("POST", self.fleet_url + "/api/router/drain")
            self.fenced()
            self.start_heartbeat()
            self.guard()
            self.clear_idle_models()
            self.report["status"] = "running"
            for task in self.tasks:
                self.submit(task)
            records = {}
            while len(records) != 2:
                queues = self.guard()
                running = sum(bool(queue["queue_running"]) for queue in queues.values())
                self.report["peak_parallel_running"] = max(running, self.report.get("peak_parallel_running", 0))
                for task in self.tasks:
                    if task["case"] not in records:
                        record = self.history(task)
                        if record:
                            self.evidence(task, record)
                            records[task["case"]] = record
                self.save()
                if len(records) != 2:
                    self.stop.wait(self.args.poll_interval)
            overlap = min(task["execution"]["finished_at"] for task in self.tasks) - max(task["execution"]["started_at"] for task in self.tasks)
            self.report["execution_overlap_seconds"] = max(0, overlap)
            if overlap <= 0:
                raise RuntimeError("no measured execution overlap; not a parallel comparison")
            for task in self.tasks:
                self.guard()
                self.collect(task, records[task["case"]])
            self.report["status"] = "generated_pending_quality_review_drained"
        except Exception as error:
            self.report.update(status="failed", error=type(error).__name__ + ": " + str(error)[:1000])
        finally:
            self.finish()
            self.report["finished_at"] = time.time()
            self.save()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        return 0 if self.report["status"] == "generated_pending_quality_review_drained" and self.report["lease_released"] else 1

    def unload_completed(self, task):
        if task.get("status") != "collected" or task.get("unloaded"):
            raise RuntimeError("only collected owned tasks may unload progressively")
        queue = self.guard()[task["case"]]
        record = self.history(task)
        if any(queue.values()) or not record or record.get("status", {}).get("status_str") != "success":
            raise RuntimeError("completed worker is not terminal and empty")
        task["unload_attempted"] = True
        self.save()
        self.request("POST", task["endpoint"] + "/free", {"unload_models": True, "free_memory": True})
        deadline = time.monotonic() + self.args.cleanup_timeout
        while True:
            if any(self.guard()[task["case"]].values()):
                raise RuntimeError("work appeared during completed worker unload")
            pid = self.report["identities"]["workers"][task["case"]]["MainPID"]
            usage = sum(row["mib"] for row in self.host.allocations() if row["pid"] == pid)
            if usage <= 1024:
                task.update(unloaded=True, unloaded_at=time.time(), unload_vram_mib=usage)
                self.resource_observed_since = None
                self.save()
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("completed worker unload unconfirmed")
            self.stop.wait(self.args.poll_interval)

    def progress_receipts(self):
        now = time.monotonic()
        receipts = {case: feed.progress.snapshot(now, self.args.sampler_observation_seconds)
                    for case, feed in self.feeds.items()}
        for task in self.tasks:
            task["sampler_progress"] = receipts[task["case"]]
        return receipts

    def admission_for(self, candidate):
        progress = self.progress_receipts()
        decision = progressive_admission(self.tasks, candidate, self.last_progressive_sample,
                                          self.worker_initial, self.last_policy, progress,
                                          time.monotonic(), self.args.sampler_observation_seconds,
                                          self.resource_observed_since, observed_peaks=self.worker_observed_peaks,
                                          reclaim_factor=self.args.reclaim_factor,
                                          global_safety_margin=int(self.args.global_safety_margin_gib * GIB))
        feed = progress[candidate["case"]]
        if not feed["connected"] or feed["error"]:
            decision["reasons"].append("candidate_websocket_unavailable")
            decision["admission"] = "wait"
        return decision

    def progressive_schedule(self):
        records = {}
        self.worker_initial = dict(self.last_progressive_sample["worker_memory_bytes"])
        self.report.update(status="running", parallel_validated=False, peak_parallel=0)
        for task in self.tasks:
            task.update(status="admission_waiting", admission_reason="waiting_for_prior_task_admission")
        while len(records) < len(self.tasks):
            queues = self.guard()
            running = sum(bool(queue["queue_running"]) for queue in queues.values())
            self.report["peak_parallel_running"] = max(running, self.report.get("peak_parallel_running", 0))
            progress = self.progress_receipts()
            for task in self.tasks:
                if task["submit_attempted"] and task["case"] not in records:
                    record = self.history(task)
                    if record:
                        self.evidence(task, record)
                        self.report["peak_parallel"] = max(1, self.report["peak_parallel"])
                        self.collect(task, record)
                        records[task["case"]] = record
                        self.unload_completed(task)
                        task["resource_reconciliation"] = finish_reconciliation(task["resource_reconciliation"])
                        self.save()
            candidate = next((task for task in self.tasks if not task["submit_attempted"]), None)
            if candidate:
                self.guard()
                decision = self.admission_for(candidate)
                candidate["admission_reason"] = ";".join(decision["reasons"]) if decision["reasons"] else "admitted_with_remaining_peak_reservations"
                candidate["admission"] = decision
                with (self.args.output_dir / "admission.jsonl").open("a") as handle:
                    handle.write(encoded({"case": candidate["case"], **decision}).decode() + "\n")
                self.save()
                if decision["admission"] == "allow":
                    self.submit(candidate)
                    if candidate["submit_attempted"]:
                        self.resource_observed_since = None
            self.save()
            if len(records) < len(self.tasks):
                self.stop.wait(self.args.poll_interval)
        self.progress_receipts()
        intervals = []
        for task in self.tasks:
            if task["sampler_progress"].get("error") or self.feeds[task["case"]].progress.invalid:
                continue
            by_node = {}
            for event in task["sampler_progress"]["events"]:
                by_node.setdefault(event["node"], []).append(event["received_monotonic"])
            for times in by_node.values():
                if len(times) >= 2 and times[-1] > times[0]:
                    intervals.extend([(times[0], 1, task["case"]), (times[-1], -1, task["case"])])
        active = set()
        overlap = 0.0
        previous = None
        for stamp, change, case in sorted(intervals):
            if previous is not None and len(active) >= 2:
                overlap += stamp - previous
            if change > 0:
                active.add(case)
            else:
                active.discard(case)
            self.report["peak_parallel"] = max(len(active), self.report["peak_parallel"])
            previous = stamp
        self.report.update(sampler_overlap_seconds=overlap,
                           parallel_validated=overlap > 0 and self.report["peak_parallel"] == len(self.tasks),
                           execution_overlap_seconds=max(0, min(task["execution"]["finished_at"] for task in self.tasks)
                                                         - max(task["execution"]["started_at"] for task in self.tasks)),
                           status="generated_pending_quality_review_drained")

    def execute_progressive(self):
        handlers = {signum: signal.signal(signum, lambda *_: self.stop.set()) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            capacity = self.capacity(owned=False)
            if self.request("GET", self.fleet_url + OPTIONS).get("draining") is not False:
                raise RuntimeError("existing/unknown drain belongs to another operation")
            self.report["limits"] = strict_limits(capacity["policy"]["resources"])
            if self.args.progressive_resource_profile == "residual_zram":
                from progressive_resource_policy import ProgressiveResourcePolicy
                self.resource_policy = ProgressiveResourcePolicy(profile="residual_zram", limits=self.report["limits"])
            self.queues(empty=True)
            self.monitor(baseline=True)
            for task in self.tasks:
                stats = self.request("GET", task["endpoint"] + "/system_stats")
                if stats.get("system", {}).get("comfyui_version") != "0.34.0" or self.history(task) is not None:
                    raise RuntimeError("unaudited server API or reused prompt ID")
                feed = SamplerFeed(task)
                self.feeds[task["case"]] = feed
                feed.start()
            self.report["lease_attempted"] = True
            self.save()
            self.request("POST", self.fleet_url + LEASE, {"owner": self.report["owner"], "ttl_seconds": self.args.lease_ttl})
            self.capacity()
            self.report["drain_attempted"] = True
            self.save()
            self.request("POST", self.fleet_url + "/api/router/drain")
            self.fenced()
            self.start_heartbeat()
            self.clear_idle_models()
            self.guard()
            self.progressive_schedule()
        except Exception as error:
            self.report.update(status="failed", error=type(error).__name__ + ": " + str(error)[:1000])
        finally:
            self.finish()
            for task in self.tasks:
                record = task.get("resource_reconciliation")
                if record and record["state"] == "observing":
                    task["resource_reconciliation"] = finish_reconciliation(record)
            for feed in self.feeds.values():
                feed.close()
            self.report["finished_at"] = time.time()
            self.save()
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
        return 0 if self.report["status"] == "generated_pending_quality_review_drained" and self.report["lease_released"] else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--manifest-sha256", required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--validator", action="append", default=[], metavar="NAME=FILE:FUNCTION")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--execute", action="store_true")
    result.add_argument("--timeout", type=float, default=1800)
    result.add_argument("--poll-interval", type=float, default=5)
    result.add_argument("--lease-ttl", type=int, default=120)
    result.add_argument("--cleanup-timeout", type=float, default=120)
    result.add_argument("--fleet-url", default=FLEET)
    result.add_argument("--progressive", action="store_true")
    result.add_argument("--progressive-resource-profile", choices=("strict", "residual_zram"), default="strict")
    result.add_argument("--sampler-observation-seconds", type=float, default=60)
    result.add_argument("--reclaim-factor", type=float, default=0.5)
    result.add_argument("--global-safety-margin-gib", type=float, default=2)
    result.add_argument("--peak-safety-margin-gib", type=float, default=2)
    result.add_argument("--peak-safety-margin-ratio", type=float, default=0.1)
    result.add_argument("--peak-history", type=Path)
    result.add_argument("--peak-history-sha256")
    result.add_argument("--runtime-profile-id", default="unverified")
    return result


def prepare(args):
    fleet_url(args.fleet_url)
    if (not math.isfinite(args.reclaim_factor) or not 0 <= args.reclaim_factor <= 1
            or not math.isfinite(args.global_safety_margin_gib) or args.global_safety_margin_gib < 2
            or not math.isfinite(args.peak_safety_margin_gib) or args.peak_safety_margin_gib < 2
            or not math.isfinite(args.peak_safety_margin_ratio) or args.peak_safety_margin_ratio < 0.1):
        raise ValueError("invalid reclaim factor or reduced safety margin")
    if bool(args.peak_history) != bool(args.peak_history_sha256):
        raise ValueError("peak history requires both file and pinned SHA256")
    if args.peak_history and args.runtime_profile_id == "unverified":
        raise ValueError("history requires an explicit audited runtime profile identity")
    if (not positive(args.sampler_observation_seconds) or args.sampler_observation_seconds < 60
            or (args.progressive and args.poll_interval > 5)
            or (not args.progressive and args.progressive_resource_profile != "strict")):
        raise ValueError("progressive requires >=60s observation, <=5s polling and explicit profile")
    if (not 60 <= args.lease_ttl <= 600 or not 0 < args.poll_interval <= args.lease_ttl / 4
            or any(not positive(value) for value in (args.timeout, args.poll_interval, args.cleanup_timeout))):
        raise ValueError("unsafe time budgets/lease TTL")
    if os.environ.get("H3_COMPUTE_CGROUP", "/sys/fs/cgroup" + CGROUP) != "/sys/fs/cgroup" + CGROUP:
        raise ValueError("aggregate cgroup override refused")
    contract, graphs = load_contract(args)
    output = args.output_dir.resolve()
    if args.execute and (output == Path("/tmp") or Path("/tmp") in output.parents):
        raise ValueError("execution output must be persistent, not /tmp")
    args.output_dir = output
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_bytes())
        if (report.get("status") != "prepared" or report.get("contract") != contract
                or report.get("lease_attempted") or len(report.get("tasks", [])) != len(contract["tasks"])):
            raise RuntimeError("prior attempt or frozen contract changed; never resubmit")
        for task, original in zip(report["tasks"], contract["tasks"]):
            if (any(task.get(key) != value for key, value in original.items()) or task.get("submit_attempted")
                    or task.get("status") != "prepared"):
                raise RuntimeError("prepared task contract changed")
            if any(str(uuid.UUID(task[key])) != task[key] for key in ("prompt_id", "client_id")):
                raise RuntimeError("invalid prepared identity")
            expected_prefix = "video/h3-parallel/" + contract["run_id"] + "/" + task["case"] + "_" + task["prompt_id"]
            graph = graphs[task["case"]]
            graph[task["shape"]["save_node"]]["inputs"]["filename_prefix"] = expected_prefix
            if task.get("prefix") != expected_prefix or sha(encoded(graph)) != task["submitted_workflow_sha256"]:
                raise RuntimeError("prepared graph/prefix changed")
            path = output / task["case"] / "workflow.json"
            graphs[task["case"]] = json.loads(read_pinned(path, task["submitted_workflow_sha256"]))
        if (len({task["prompt_id"] for task in report["tasks"]}) != len(contract["tasks"])
                or len({task["client_id"] for task in report["tasks"]}) != len(contract["tasks"])):
            raise RuntimeError("duplicate prepared identities")
        return report, graphs
    if any(output.iterdir()):
        raise RuntimeError("nonempty run directory without report requires manual reconciliation")
    report = {"contract": contract, "run_id": contract["run_id"], "owner": "h3val_" + uuid.uuid4().hex[:16],
              "status": "prepared", "lease_released": False, "tasks": copy.deepcopy(contract["tasks"]),
              "fleet_url": args.fleet_url, "progressive": args.progressive}
    for task in report["tasks"]:
        task.update(prompt_id=str(uuid.uuid4()), client_id=str(uuid.uuid4()), submit_attempted=False, status="prepared")
        task["prefix"] = "video/h3-parallel/" + report["run_id"] + "/" + task["case"] + "_" + task["prompt_id"]
        graph = graphs[task["case"]]
        graph[task["shape"]["save_node"]]["inputs"]["filename_prefix"] = task["prefix"]
        task["submitted_workflow_sha256"] = sha(encoded(graph))
        directory = output / task["case"]
        directory.mkdir()
        save(directory / "workflow.json", graph)
        save(directory / "input-workflow.json", read_pinned(Path(task["workflow"]), task["workflow_sha256"]))
    save(output / "manifest.json", read_pinned(args.manifest, args.manifest_sha256))
    save(report_path, report)
    return report, graphs


def run(args):
    args.output_dir = args.output_dir.resolve()
    args.output_dir.parent.mkdir(parents=True, exist_ok=True)
    with args.output_dir.with_name(args.output_dir.name + ".lock").open("a") as batch_lock:
        fcntl.flock(batch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report, graphs = prepare(args)
        if not args.execute:
            print(encoded({"status": "prepared", "report": str(args.output_dir / "report.json"), "run_id": report["run_id"]}).decode())
            return 0
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("a") as coordinator_lock:
            fcntl.flock(coordinator_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report.update(status="preflight", started_at=time.time())
            save(args.output_dir / "report.json", report)
            with httpx.Client(headers={"Authorization": "Bearer " + key_from_service()}, trust_env=False,
                              follow_redirects=False, timeout=10) as fleet:
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as comfy:
                    return Coordinator(args, report, graphs, fleet, comfy).execute()


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
