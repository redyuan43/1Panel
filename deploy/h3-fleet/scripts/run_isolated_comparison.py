"""CPU preparation by default; --execute is an explicitly authorized Ivan-only run.

Contract: the operator starts the isolated worker at --isolated-url
(default http://127.0.0.1:18188), with
CUDA_VISIBLE_DEVICES set to the full supplied RTX 4060 Ti UUID, in
h3-compute.slice. Production workers remain running. Requires ComfyUI 0.34.0
commit 250b2e9551a7bc7a8ebb5beb07e0fecd2983e04a (client prompt_id and targeted
interrupt), T8 v1.3.3, existing Fleet, httpx, nvidia-smi, systemctl and ffprobe.
Pinned API: https://github.com/Comfy-Org/ComfyUI/blob/
250b2e9551a7bc7a8ebb5beb07e0fecd2983e04a/server.py

Supply --workflow, --workflow-sha256, --case, --run-id, --output-dir,
--isolated-unit and --isolated-gpu-uuid. Reuse a prepared directory with
--execute; any attempted execution is non-resumable and must be reconciled
by its persisted prompt_id, never re-submitted. Only SaveVideo's prefix is
changed. The loopback origin is frozen in report.isolated_url; its listening
socket must belong to the isolated unit MainPID in the same network namespace.
Prepared reports without that origin require a new preparation directory.
Native T8 362-frame graphs (4/8 steps, 480x864 or 768x1344/1344x768)
with zero/one single Bypass are accepted, plus the strictly audited B8 VDN
and D4 FastH3 graphs (480x864, 362 frames, 24 fps, 8/4 NFE respectively).
Other VDN/VSA/custom graphs are rejected. D4 uses video/h3-comparison/ prefixes.
The A4/A8 and exact A4_C0/A4_C05/A4_C1 recipes freeze shifts 6/3;
the three A4_C variants require four steps. Other recipes retain 12/3. Other B/D cases
and 12 GiB GPU execution are not supported by this driver.
No production studio_demand override, worker lifecycle or model synthesis.
--idle-wait-timeout changes only the stable-idle wait budget (default 180,
maximum 1800 seconds); Fleet stability windows and memory limits remain intact.

Cancellation that cannot be proven safe retains (does not DELETE) the lease,
requests a bounded six-hour hold if ownership remains, and exits nonzero.
The report records its expiry; operator reconciliation is mandatory before
that expiry. SIGKILL/host failure cannot guarantee lease renewal.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import threading
import time
import uuid

import httpx

from validate_capacity import check_idle, command, file_sha256, key_from_service, sample_host, safety_reason
from validate_dual import validate_media
from validate_studio_matrix import memory_diagnostics, validate_pinned_workflow, verify_execution


ISOLATED = "http://127.0.0.1:18188"
FLEET = "http://ivan-ms-7b17.taild500c8.ts.net:8789"
WORKERS = {"fast": "http://127.0.0.1:8188", "main": "http://127.0.0.1:8189",
           "preview": "http://127.0.0.1:8190"}
UNITS = {f"comfyui-h3@{lane}.service" for lane in WORKERS}
CGROUP = "/h3.slice/h3-compute.slice"
LEASE = "/api/router/validation-lease"
CAPACITY = "/api/router/capacity"
A4_REALISM_CASES = frozenset({"A4_C0", "A4_C05", "A4_C1"})
A_CASE_STEPS = {"A4": 4, "A8": 8, **{case: 4 for case in A4_REALISM_CASES}}
A4_LORA = "minimax_h3_fl2v_turbo_4step_v1.2_768p_comfyui_bf16.safetensors"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def persist(path, value):
    temporary = path.with_name(path.name + ".next")
    with temporary.open("wb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def relative_name(value):
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and "\\" not in value and ":" not in value
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def validate_isolated_url(value):
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", value) if isinstance(value, str) else None
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError("isolated URL must be exactly http://127.0.0.1:<port>, port 1..65535")
    if value in WORKERS.values():
        raise ValueError("isolated URL must not alias a production worker")
    return value


def validate_workflow(graph, case=None):
    if isinstance(case, str) and case.startswith("A4_C") and case not in A4_REALISM_CASES:
        raise ValueError("only exact A4_C0/A4_C05/A4_C1 realism recipes are accepted")
    if case in {"B8", "D4"}:
        return validate_pinned_workflow(graph, case)
    if not isinstance(graph, dict) or not graph:
        raise ValueError("expected an API workflow object")
    by_class = {}
    for identifier, node in graph.items():
        if not isinstance(identifier, str) or not isinstance(node, dict):
            raise ValueError("invalid graph node")
        if set(node) - {"class_type", "inputs", "_meta"} or not isinstance(node.get("inputs"), dict):
            raise ValueError("unknown node fields")
        by_class.setdefault(node.get("class_type"), []).append(identifier)
    single = {"CLIPLoader", "UNETLoader", "MiniMaxH3AudioConditioningT8",
              "MiniMaxH3DualClockSamplerT8", "RandomNoise", "BasicGuider",
              "SamplerCustomAdvanced", "MiniMaxH3AVDecodeT8", "CreateVideo", "SaveVideo"}
    if set(by_class) - single - {"VAELoader", "LoraLoaderBypassModelOnly"}:
        raise ValueError("unknown/unaudited node class")
    if any(len(by_class.get(kind, [])) != 1 for kind in single):
        raise ValueError("expected exactly one node of each native role")
    if len(by_class.get("VAELoader", [])) != 2 or len(by_class.get("LoraLoaderBypassModelOnly", [])) > 1:
        raise ValueError("requires two VAEs and at most one Bypass")
    identifiers = {kind: by_class[kind][0] for kind in single}

    def inputs(kind):
        return graph[identifiers[kind]]["inputs"]

    def link(kind, output=0):
        return [identifiers[kind], output]

    conditioning = inputs("MiniMaxH3AudioConditioningT8")
    clock = inputs("MiniMaxH3DualClockSamplerT8")
    width, height = conditioning.get("width"), conditioning.get("height")
    if (width, height) not in {(480, 864), (768, 1344), (1344, 768)}:
        raise ValueError("unaudited resolution")
    if type(clock.get("steps")) is not int or clock["steps"] not in {4, 8}:
        raise ValueError("only 4/8 steps accepted")
    shift = clock.get("shift_video")
    if shift not in {6, 12} or (case is not None and shift != (6 if case in A_CASE_STEPS else 12)):
        raise ValueError("recipe video shift mismatch: audited A recipes require 6; others require 12")
    if case in A_CASE_STEPS and clock["steps"] != A_CASE_STEPS[case]:
        raise ValueError("A recipe step count does not match recipe")
    seed = inputs("RandomNoise").get("noise_seed")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("invalid uint64 seed")
    if not isinstance(conditioning.get("prompt"), str) or not conditioning["prompt"].strip():
        raise ValueError("prompt text is required, never rewritten")
    video_vae, audio_vae = conditioning.get("video_vae"), conditioning.get("audio_vae")
    if (not isinstance(video_vae, list) or not isinstance(audio_vae, list)
            or len(video_vae) != 2 or len(audio_vae) != 2
            or video_vae[1] != 0 or audio_vae[1] != 0
            or {video_vae[0], audio_vae[0]} != set(by_class["VAELoader"])):
        raise ValueError("invalid VAE wiring")
    expected = {
        "CLIPLoader": {"clip_name": inputs("CLIPLoader").get("clip_name"), "type": "minimax", "device": "default"},
        "UNETLoader": {"unet_name": inputs("UNETLoader").get("unet_name"), "weight_dtype": "default"},
        "MiniMaxH3AudioConditioningT8": {
            "prompt": conditioning["prompt"], "width": width, "height": height, "length": 362,
            "task_type": "T2VA", "audio_mode": "native", "audio_denoise_strength": 1,
            "add_source_as_reference": False, "prompt_primary_audio_ordinal": 0,
            "strict_prompt_tags": True, "ref_image_size": "match", "reference_video_policy": "official_2_to_15s",
            "clip": link("CLIPLoader"), "video_vae": video_vae, "audio_vae": audio_vae},
        "MiniMaxH3DualClockSamplerT8": {
            "steps": clock["steps"], "shift_video": shift, "shift_audio": 3,
            "model": link("UNETLoader"), "av_latent": link("MiniMaxH3AudioConditioningT8", 1),
            "sampler_name": "dual_clock_euler", "scheduler": "native_flow"},
        "RandomNoise": {"noise_seed": seed},
        "BasicGuider": {"model": link("MiniMaxH3DualClockSamplerT8"), "conditioning": link("MiniMaxH3AudioConditioningT8")},
        "SamplerCustomAdvanced": {"noise": link("RandomNoise"), "guider": link("BasicGuider"),
                                  "sampler": link("MiniMaxH3DualClockSamplerT8", 1),
                                  "sigmas": link("MiniMaxH3DualClockSamplerT8", 2),
                                  "latent_image": link("MiniMaxH3AudioConditioningT8", 1)},
        "MiniMaxH3AVDecodeT8": {"av_latent": link("SamplerCustomAdvanced"), "video_vae": video_vae, "audio_vae": audio_vae},
        "CreateVideo": {"images": link("MiniMaxH3AVDecodeT8"), "audio": link("MiniMaxH3AVDecodeT8", 1), "fps": 24, "bit_depth": 8},
        "SaveVideo": {"video": link("CreateVideo"), "filename_prefix": inputs("SaveVideo").get("filename_prefix"),
                      "format": "auto", "codec": "auto"},
    }
    if by_class.get("LoraLoaderBypassModelOnly"):
        identifier = by_class["LoraLoaderBypassModelOnly"][0]
        lora = graph[identifier]["inputs"]
        if lora != {"model": link("UNETLoader"), "lora_name": lora.get("lora_name"), "strength_model": 1}:
            raise ValueError("single Bypass must have strength 1 and direct base input")
        if not relative_name(lora.get("lora_name")) or not lora["lora_name"].endswith(".safetensors"):
            raise ValueError("invalid LoRA filename")
        expected["MiniMaxH3DualClockSamplerT8"]["model"] = [identifier, 0]
    for kind, wanted in expected.items():
        if inputs(kind) != wanted:
            raise ValueError("unknown inputs or wiring: " + kind)
    for identifier in by_class["VAELoader"]:
        value = graph[identifier]["inputs"]
        if set(value) != {"vae_name"}:
            raise ValueError("unknown VAE inputs")
    for node in graph.values():
        for key in ("vae_name", "clip_name", "unet_name", "filename_prefix"):
            if key in node["inputs"] and not relative_name(node["inputs"][key]):
                raise ValueError("unsafe artifact name")
    if case in A4_REALISM_CASES:
        if (width, height) != (480, 864) or len(by_class.get("LoraLoaderBypassModelOnly", [])) != 1:
            raise ValueError("A4 realism requires common 480x864 canvas and one Bypass")
        prompt = conditioning["prompt"]
        if (not prompt.startswith("r34l1sm\n") or not prompt[len("r34l1sm\n"):].strip()
                or prompt[len("r34l1sm\n"):].lstrip().startswith("r34l1sm")):
            raise ValueError("A4 realism requires one r34l1sm newline trigger and unchanged common prompt")
        if (inputs("UNETLoader")["unet_name"] != "minimax_h3_fl2va_int8_convrot.safetensors"
                or inputs("CLIPLoader")["clip_name"] != "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
                or graph[video_vae[0]]["inputs"]["vae_name"] != "minimax_h3_video_vae_fp16.safetensors"
                or graph[audio_vae[0]]["inputs"]["vae_name"] != "minimax_h3_audio_vae_fp32.safetensors"):
            raise ValueError("A4 realism must retain the original A4 base/encoder/VAEs")
        lora_name = graph[by_class["LoraLoaderBypassModelOnly"][0]]["inputs"]["lora_name"]
        if case == "A4_C0" and lora_name != A4_LORA:
            raise ValueError("A4_C0 must retain the original A4 v1.2 LoRA")
        if case in {"A4_C05", "A4_C1"} and PurePosixPath(lora_name).name in {
                A4_LORA, "t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors",
                "h3-realism-people-t2v-i2v-r2v.safetensors", "c_realism_ema_people_fp32.safetensors"}:
            raise ValueError("A4_C05/A4_C1 require a separately verified A4+People composition, not an original/EMA artifact")
    return {"width": width, "height": height, "length": 362, "fps": 24,
            "steps": clock["steps"], "shift_video": shift, "shift_audio": 3,
            "save_node": identifiers["SaveVideo"]}


def production_services(sample):
    services = sample["services"]
    if len(services) != 3 or {unit["Id"] for unit in services} != UNITS:
        raise RuntimeError("production service set changed")
    if len({unit["MainPID"] for unit in services}) != 3 or any(int(unit["MainPID"]) <= 0 for unit in services):
        raise RuntimeError("three distinct production PIDs required")


def listener_evidence(process, isolated_url):
    validate_isolated_url(isolated_url)
    port = int(isolated_url.rsplit(":", 1)[1])
    address = f"0100007F:{port:04X}"
    if os.readlink(process / "ns/net") != os.readlink("/proc/self/ns/net"):
        raise RuntimeError("isolated PID is outside the runner network namespace")
    listeners = set()
    for line in (process / "net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == address and fields[3] == "0A":
            listeners.add(fields[9])
    owned = set()
    for descriptor in (process / "fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            owned.add(target[8:-1])
    matched = sorted(listeners & owned)
    if len(matched) != 1:
        raise RuntimeError("isolated MainPID does not uniquely own the configured URL listener")
    start_ticks = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
    return {"isolated_url": isolated_url, "listener_inode": matched[0], "process_start_ticks": start_ticks}


def isolated_snapshot(unit, gpu_uuid, isolated_url=ISOLATED):
    raw = command("systemctl", "show", unit, "-p", "Id", "-p", "ActiveState", "-p", "MainPID",
                  "-p", "NRestarts", "-p", "Slice", "-p", "ControlGroup")
    state = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    if state.get("Id") != unit or state.get("ActiveState") != "active" or int(state.get("MainPID", 0)) <= 0:
        raise RuntimeError("isolated service is not active")
    if state.get("Slice") != "h3-compute.slice" or not state.get("ControlGroup", "").startswith(CGROUP + "/"):
        raise RuntimeError("isolated service outside aggregate h3-compute.slice")
    process = Path("/proc") / state["MainPID"]
    if "0::" + state["ControlGroup"] not in (process / "cgroup").read_text().splitlines():
        raise RuntimeError("isolated PID cgroup does not match systemd")
    environment = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
    if environment.get(b"CUDA_VISIBLE_DEVICES", b"").decode() != gpu_uuid:
        raise RuntimeError("isolated worker must expose only the explicit full GPU UUID")
    inventory = command("nvidia-smi", "--query-gpu=uuid,name", "--format=csv,noheader,nounits")
    matches = [line.split(",", 1)[1].strip() for line in inventory.splitlines() if line.split(",", 1)[0].strip() == gpu_uuid]
    if len(matches) != 1 or "RTX 4060 Ti" not in matches[0]:
        raise RuntimeError("supplied UUID is not an RTX 4060 Ti")
    state["gpu_uuid"] = gpu_uuid
    state.update(listener_evidence(process, isolated_url))
    return state


def fast_vram(pid, gpu_uuid):
    raw = command("nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits")
    total = 0
    for line in raw.splitlines():
        process, device, memory = (part.strip() for part in line.split(","))
        if process == str(pid) and (gpu_uuid is None or device == gpu_uuid):
            total += int(memory)
    return total


def isolated_safety_reason(sample, baseline, limits, recovery=None):
    if not recovery:
        return safety_reason(sample, baseline, limits)
    if limits.get("swap_recovery_mode") != "stable_idle_exclusive_fast_only":
        return "unapproved swap recovery policy"
    if max(sample["swap_used_bytes"], sample["cgroup_swap_bytes"]) > limits["swap_hard_limit_gib"] * 1024**3:
        return "swap hard ceiling crossed"
    adjusted = dict(sample)
    adjusted["swap_used_bytes"] = max(0, sample["swap_used_bytes"] - recovery["host_bytes"])
    adjusted["cgroup_swap_bytes"] = max(0, sample["cgroup_swap_bytes"] - recovery["cgroup_bytes"])
    return safety_reason(adjusted, baseline, limits)


class Driver:
    def __init__(self, args, report, workflow, fleet, comfy):
        self.args, self.report, self.workflow = args, report, workflow
        self.isolated_url = validate_isolated_url(args.isolated_url)
        if report.get("isolated_url") != self.isolated_url:
            raise ValueError("isolated URL differs from frozen report")
        self.fleet, self.comfy = fleet, comfy
        self.stop = threading.Event()
        self.heartbeat_stop = threading.Event()
        self.heartbeat = None
        self.heartbeat_error = None
        self.lease_attempted = False
        self.deadline = time.monotonic() + args.timeout

    def save(self):
        persist(self.args.output_dir / "report.json", self.report)

    def request(self, method, url, body=None):
        client = self.fleet if url.startswith(FLEET + "/") else self.comfy
        response = client.request(method, url, **({"json": body} if body is not None else {}))
        response.raise_for_status()
        return response.json() if response.content else {}

    def capacity(self, owned=True):
        capacity = self.request("GET", FLEET + CAPACITY)
        if owned:
            lease = capacity.get("validation_lease") or {}
            if lease.get("owner") != self.report["owner"] or lease.get("expires_at", 0) <= time.time() + 5:
                raise RuntimeError("exclusive validation lease lost or near expiry")
            capacity = dict(capacity, validation_lease=None)
        check_idle(capacity)
        if {item["lane_id"] for item in capacity["queues"]} != set(WORKERS):
            raise RuntimeError("incomplete production queue inventory")
        return capacity

    def owned_item(self, item):
        return (isinstance(item, (list, tuple)) and len(item) >= 4
                and item[1] == self.report["prompt_id"] and item[2] == self.workflow
                and isinstance(item[3], dict) and item[3].get("client_id") == self.report["client_id"])

    def queues(self, empty=False):
        for url in WORKERS.values():
            queue = self.request("GET", url + "/queue")
            if set(queue) != {"queue_running", "queue_pending"} or any(queue.values()):
                raise RuntimeError("production queue not empty or unknown queue schema")
        queue = self.request("GET", self.isolated_url + "/queue")
        if set(queue) != {"queue_running", "queue_pending"} or any(not isinstance(value, list) for value in queue.values()):
            raise RuntimeError("unknown isolated queue schema")
        items = queue["queue_running"] + queue["queue_pending"]
        if (empty and items) or len(items) > 1 or any(not self.owned_item(item) for item in items):
            raise RuntimeError("unknown or duplicate isolated queue item; no destructive action allowed")
        return queue

    def observe(self, baseline=False):
        sample = sample_host(self.report["started_at"])
        production_services(sample)
        isolated = isolated_snapshot(self.args.isolated_unit, self.args.isolated_gpu_uuid, self.isolated_url)
        if isolated["MainPID"] in {unit["MainPID"] for unit in sample["services"]}:
            raise RuntimeError("isolated PID aliases a production worker")
        sample["isolated"] = isolated
        sample["memory_diagnostics"] = memory_diagnostics([*sample["services"], isolated], bandwidth=True)
        with (self.args.output_dir / "metrics.jsonl").open("a") as handle:
            handle.write(canonical(sample).decode() + "\n")
            handle.flush()
        reference = sample if baseline else self.report["baseline"]
        reason = isolated_safety_reason(sample, reference, self.report["resource_limits"],
                                        self.report.get("swap_recovery_baseline"))
        if reason or isolated != reference["isolated"]:
            raise RuntimeError(reason or "isolated PID/unit/cgroup changed")
        if baseline:
            self.report["baseline"] = sample
            self.save()
        return sample

    def await_idle_swap(self, capacity):
        limits = self.report["resource_limits"]
        deadline = time.monotonic() + self.args.idle_wait_timeout
        while True:
            snapshot = capacity["resources"]
            used = max(snapshot["swap_used_bytes"], snapshot["cgroup_swap_bytes"])
            if used <= limits["max_swap_gib"] * 1024**3:
                return
            if (limits.get("swap_recovery_mode") != "stable_idle_exclusive_fast_only"
                    or used > limits["swap_hard_limit_gib"] * 1024**3):
                raise RuntimeError("swap recovery not permitted or hard ceiling crossed")
            recovery = snapshot.get("swap_recovery", {})
            if (recovery.get("ready") is True and recovery.get("exclusive_single") is True
                    and recovery.get("stable_seconds_observed", 0) >= limits["swap_idle_stable_seconds"]):
                self.report["swap_recovery_baseline"] = {
                    "host_bytes": snapshot["swap_used_bytes"], "cgroup_bytes": snapshot["cgroup_swap_bytes"],
                    "timestamp": snapshot["timestamp"], "fleet_evidence": recovery,
                    "policy": "stable_idle_exclusive_fast_only"}
                self.save()
                return
            self.report.update(status="waiting_stable_idle_swap", swap_recovery_observation=recovery)
            self.save()
            if self.stop.is_set() or time.monotonic() >= deadline:
                raise RuntimeError(f"no stable idle swap window within {self.args.idle_wait_timeout:g} seconds; no submission")
            self.stop.wait(self.args.poll_interval)
            capacity = self.capacity(owned=False)
            self.queues(empty=True)

    def renew(self, ttl=None):
        self.capacity()
        self.request("POST", FLEET + LEASE, {"owner": self.report["owner"], "ttl_seconds": ttl or self.args.lease_ttl})

    def start_heartbeat(self):
        def watch():
            while not self.heartbeat_stop.wait(self.args.lease_ttl / 3):
                try:
                    self.renew()
                except Exception as error:
                    self.heartbeat_error = type(error).__name__ + ": " + str(error)
                    self.stop.set()
                    return
        self.heartbeat = threading.Thread(target=watch, daemon=True)
        self.heartbeat.start()

    def guard(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError(self.heartbeat_error or "stop requested or execution deadline reached")
        self.capacity()
        self.observe()
        return self.queues()

    def history(self):
        history = self.request("GET", self.isolated_url + "/history/" + self.report["prompt_id"])
        if not isinstance(history, dict) or set(history) - {self.report["prompt_id"]}:
            raise RuntimeError("foreign history response")
        record = history.get(self.report["prompt_id"])
        if record is not None:
            if not self.owned_item(record.get("prompt")):
                raise RuntimeError("history identity/workflow mismatch")
            persist(self.args.output_dir / "history.json", history)
        return history, record

    def collect(self, history, record):
        for event, payload in record.get("status", {}).get("messages", []):
            if event == "execution_error":
                self.report["upstream_error"] = {
                    key: str(payload.get(key, ""))[:1500]
                    for key in ("node_id", "node_type", "exception_type", "exception_message")
                }
                failure = self.report["upstream_error"]
                raise RuntimeError(f"{failure['exception_type']} at node {failure['node_id']} "
                                   f"({failure['node_type']}): {failure['exception_message']}")
        self.report["execution"] = verify_execution(history, self.report["prompt_id"], self.workflow)
        outputs = record.get("outputs", {}).get(self.report["shape"]["save_node"], {})
        artifacts = outputs.get("images", [])
        if len(artifacts) != 1:
            raise RuntimeError("expected exactly one SaveVideo artifact")
        artifact = artifacts[0]
        filename, subfolder = artifact.get("filename"), artifact.get("subfolder")
        prefix = PurePosixPath(self.report["prefix"])
        if (artifact.get("type") != "output" or not relative_name(filename) or "/" in filename
                or subfolder != str(prefix.parent) or not filename.startswith(prefix.name + "_")
                or not filename.endswith(".mp4")):
            raise RuntimeError("output does not belong to the frozen run prefix")
        path = self.args.output_dir / "video.mp4"
        self.guard()
        with self.comfy.stream("GET", self.isolated_url + "/view", params={key: artifact[key] for key in ("filename", "subfolder", "type")}) as response:
            response.raise_for_status()
            with path.open("xb") as handle:
                for chunk in response.iter_bytes():
                    if self.stop.is_set() or time.monotonic() >= self.deadline:
                        raise RuntimeError("artifact collection stopped")
                    handle.write(chunk)
        probe = json.loads(command("ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)))
        persist(self.args.output_dir / "media-probe.json", probe)
        shape = self.report["shape"]
        media = validate_media(probe, width=shape["width"], height=shape["height"], length=362, fps=24)
        videos = [stream for stream in probe["streams"] if stream.get("codec_type") == "video"]
        if not media["ok"] or len(videos) != 1 or int(videos[0].get("nb_read_frames", 0)) != 362:
            raise RuntimeError("full 362-frame audiovisual media validation failed")
        self.report.update(media=media, artifact=str(path), artifact_sha256=file_sha256(path),
                           status="generated_pending_quality_review")

    def reconcile(self):
        deadline = time.monotonic() + self.args.cancel_timeout
        interrupted = False
        deleted = False
        while time.monotonic() < deadline:
            self.capacity()
            isolated = isolated_snapshot(self.args.isolated_unit, self.args.isolated_gpu_uuid, self.isolated_url)
            if isolated != self.report["baseline"]["isolated"]:
                raise RuntimeError("isolated identity changed; cancellation refused")
            queue = self.queues(empty=not self.report["submit_attempted"])
            if not self.report["submit_attempted"]:
                return "no submission; all queues empty"
            _, record = self.history()
            if not any(queue.values()):
                if self.report.get("submit_rejected_http400"):
                    if record is not None:
                        raise RuntimeError("HTTP400 rejection contradicts owned history")
                    self.queues(empty=True)
                    return "explicit HTTP400 rejection; no history and all queues empty"
                terminal = record and record.get("status", {}).get("status_str") in {"success", "error"}
                if terminal:
                    self.queues(empty=True)
                    return "owned terminal history and empty queues"
            if queue["queue_running"] and not interrupted:
                self.report["interrupt_attempted"] = True
                self.save()
                self.request("POST", self.isolated_url + "/interrupt", {"prompt_id": self.report["prompt_id"]})
                interrupted = True
            if queue["queue_pending"] and not deleted:
                self.report["delete_attempted"] = True
                self.save()
                self.request("POST", self.isolated_url + "/queue", {"delete": [self.report["prompt_id"]]})
                deleted = True
            time.sleep(self.args.poll_interval)
        raise RuntimeError("submission/cancellation remains ambiguous; lease must not be released")

    def unload_isolated(self):
        deadline = time.monotonic() + self.args.free_timeout
        baseline = self.report["baseline"]["isolated"]

        def idle_identity():
            self.capacity()
            if isolated_snapshot(self.args.isolated_unit, self.args.isolated_gpu_uuid, self.isolated_url) != baseline:
                raise RuntimeError("isolated identity changed; free/release refused")
            self.queues(empty=True)

        idle_identity()
        self.report["isolated_free_attempted"] = True
        self.save()
        self.request("POST", self.isolated_url + "/free", {"unload_models": True, "free_memory": True})
        while True:
            idle_identity()
            used = fast_vram(baseline["MainPID"], None)
            self.report["isolated_vram_mib_after_free"] = used
            self.save()
            if used <= 1024:
                self.report["isolated_unload_confirmed"] = True
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("isolated PID VRAM remains above 1024 MiB; lease retained")
            time.sleep(self.args.poll_interval)

    def execute(self):
        old_handlers = {signum: signal.signal(signum, lambda *_: self.stop.set()) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            capacity = self.capacity(owned=False)
            self.report["resource_limits"] = capacity["policy"]["resources"]
            self.queues(empty=True)
            self.await_idle_swap(capacity)
            self.observe(baseline=True)
            stats = self.request("GET", self.isolated_url + "/system_stats")
            if stats.get("system", {}).get("comfyui_version") != "0.34.0":
                raise RuntimeError("unaudited ComfyUI server API version")
            if self.history()[1] is not None:
                raise RuntimeError("preallocated prompt ID already exists")
            self.report.update(status="acquiring_lease", lease_attempted=True)
            self.save()
            self.lease_attempted = True
            self.request("POST", FLEET + LEASE, {"owner": self.report["owner"], "ttl_seconds": self.args.lease_ttl})
            self.capacity()
            self.start_heartbeat()
            self.guard()
            self.queues(empty=True)
            self.report.update(status="freeing_fast_cache", free_attempted=True)
            self.save()
            self.request("POST", WORKERS["fast"] + "/free", {"unload_models": True, "free_memory": True})
            free_deadline = time.monotonic() + self.args.free_timeout
            fast_pid = next(unit["MainPID"] for unit in self.report["baseline"]["services"] if unit["Id"] == "comfyui-h3@fast.service")
            while True:
                self.guard()
                self.queues(empty=True)
                used = fast_vram(fast_pid, self.args.isolated_gpu_uuid)
                self.report["fast_vram_mib_after_free"] = used
                if used <= self.args.max_fast_vram_mib:
                    break
                if time.monotonic() >= free_deadline:
                    raise RuntimeError("fast cache did not free VRAM within budget")
                self.stop.wait(self.args.poll_interval)
            self.guard()
            self.queues(empty=True)
            self.report.update(status="submitting", submit_attempted=True, submitted_at=time.time())
            self.save()
            payload = {"prompt": self.workflow, "prompt_id": self.report["prompt_id"], "client_id": self.report["client_id"],
                       "extra_data": {"isolated_comparison": {"run_id": self.args.run_id, "case": self.args.case}}}
            try:
                response = self.request("POST", self.isolated_url + "/prompt", payload)
                if response.get("prompt_id") != self.report["prompt_id"]:
                    raise RuntimeError("server did not honor preallocated prompt_id")
                self.report["submit_acknowledged"] = True
                self.report["status"] = "running"
            except httpx.HTTPStatusError as error:
                try:
                    rejection = error.response.json()
                except ValueError:
                    rejection = None
                if (error.response.status_code == 400 and isinstance(rejection, dict)
                        and rejection.get("error") and not rejection.get("prompt_id")):
                    self.report["submit_rejected_http400"] = True
                    self.report["submit_rejection"] = rejection
                    self.save()
                raise
            except (httpx.TimeoutException, httpx.NetworkError):
                self.report["submit_acknowledged"] = False
            self.save()
            while True:
                self.guard()
                history, record = self.history()
                if record:
                    self.collect(history, record)
                    break
                self.stop.wait(self.args.poll_interval)
        except Exception as error:
            self.report.update(status="failed", error=type(error).__name__ + ": " + str(error)[:1000])
        finally:
            try:
                if self.lease_attempted:
                    self.report["reconciliation"] = self.reconcile()
                    self.unload_isolated()
                    self.heartbeat_stop.set()
                    if self.heartbeat:
                        self.heartbeat.join()
                    self.capacity()
                    self.queues(empty=True)
                    self.request("DELETE", FLEET + LEASE, {"owner": self.report["owner"]})
                    lease = self.request("GET", FLEET + CAPACITY).get("validation_lease")
                    if lease and lease.get("owner") == self.report["owner"]:
                        raise RuntimeError("lease release not confirmed")
                    self.report["lease_released"] = True
            except Exception as error:
                self.report.update(status="needs_reconciliation", reconciliation_error=str(error)[:1000])
                self.heartbeat_stop.set()
                if self.heartbeat:
                    self.heartbeat.join()
                try:
                    self.renew(ttl=21600)
                    self.report["retained_lease"] = self.request("GET", FLEET + CAPACITY).get("validation_lease")
                except Exception as hold_error:
                    self.report["lease_hold_error"] = str(hold_error)[:1000]
            self.heartbeat_stop.set()
            if self.heartbeat:
                self.heartbeat.join()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
            self.report["finished_at"] = time.time()
            self.save()
        return 0 if self.report["status"] == "generated_pending_quality_review" and self.report["lease_released"] else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--workflow", type=Path, required=True)
    result.add_argument("--workflow-sha256", required=True)
    result.add_argument("--case", required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--isolated-unit", required=True)
    result.add_argument("--isolated-url", type=validate_isolated_url, default=ISOLATED,
                        help="exact loopback HTTP origin; must be owned by the isolated unit MainPID")
    result.add_argument("--isolated-gpu-uuid", required=True)
    result.add_argument("--execute", action="store_true")
    result.add_argument("--timeout", type=float, default=3600)
    result.add_argument("--idle-wait-timeout", type=float, default=180,
                        help="stable-idle wait budget in seconds, >0 and <=1800; does not change safety thresholds")
    result.add_argument("--lease-ttl", type=int, default=120)
    result.add_argument("--poll-interval", type=float, default=5)
    result.add_argument("--cancel-timeout", type=float, default=120)
    result.add_argument("--free-timeout", type=float, default=90)
    result.add_argument("--max-fast-vram-mib", type=int, default=1024)
    return result


def run(args):
    if not re.fullmatch(r"[0-9a-f]{64}", args.workflow_sha256):
        raise ValueError("explicit lowercase workflow SHA256 required")
    raw = args.workflow.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.workflow_sha256:
        raise ValueError("frozen workflow SHA256 mismatch")
    graph = json.loads(raw, object_pairs_hook=unique_object)
    if args.case.startswith(("B", "D")) and args.case not in {"B8", "D4"}:
        raise ValueError("only audited B8/D4 are supported among B/D recipes")
    shape = validate_workflow(graph, args.case)
    isolated_url = validate_isolated_url(args.isolated_url)
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value) for value in (args.case, args.run_id)):
        raise ValueError("unsafe case/run ID")
    if args.isolated_unit in UNITS or not re.fullmatch(r"[A-Za-z0-9_@.-]+\.service", args.isolated_unit):
        raise ValueError("explicit independent system unit required")
    if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", args.isolated_gpu_uuid):
        raise ValueError("explicit full GPU UUID required")
    if not 60 <= args.lease_ttl <= 600 or not 0 < args.poll_interval <= args.lease_ttl / 4:
        raise ValueError("unsafe lease TTL/poll interval")
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout, args.poll_interval, args.cancel_timeout, args.free_timeout)):
        raise ValueError("finite positive time budgets required")
    if not math.isfinite(args.idle_wait_timeout) or not 0 < args.idle_wait_timeout <= 1800:
        raise ValueError("idle wait timeout must be finite, >0 and <=1800 seconds")
    if not 0 <= args.max_fast_vram_mib <= 1024:
        raise ValueError("fast residual VRAM ceiling may only be tightened")
    if os.environ.get("H3_COMPUTE_CGROUP", "/sys/fs/cgroup" + CGROUP) != "/sys/fs/cgroup" + CGROUP:
        raise ValueError("aggregate telemetry cgroup override is not permitted")
    output = args.output_dir.resolve()
    if output == Path("/tmp") or Path("/tmp") in output.parents:
        raise ValueError("formal run output must be persistent, not /tmp")
    args.output_dir = output
    namespace = "video/h3-comparison/" if args.case == "D4" else "video/h3-isolated/"
    prefix = namespace + args.run_id + "/" + args.case
    workflow = copy.deepcopy(graph)
    workflow[shape["save_node"]]["inputs"]["filename_prefix"] = prefix
    contract = {"case": args.case, "run_id": args.run_id, "input_workflow_sha256": args.workflow_sha256,
                "workflow_sha256": digest(workflow), "prefix": prefix, "shape": shape,
                "isolated_unit": args.isolated_unit, "isolated_gpu_uuid": args.isolated_gpu_uuid,
                "isolated_url": isolated_url,
                "timeout": args.timeout, "lease_ttl": args.lease_ttl, "poll_interval": args.poll_interval,
                "idle_wait_timeout": args.idle_wait_timeout,
                "cancel_timeout": args.cancel_timeout, "free_timeout": args.free_timeout,
                "max_fast_vram_mib": args.max_fast_vram_mib}
    output.mkdir(parents=True, exist_ok=True)
    with (output / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report_path = output / "report.json"
        if report_path.exists():
            report = json.loads(report_path.read_bytes())
            if (report.get("status") != "prepared" or report.get("submit_attempted") or report.get("lease_attempted")
                    or any(report.get(key) != value for key, value in contract.items())
                    or file_sha256(output / "workflow.json") != digest(workflow)):
                raise RuntimeError("run already attempted or frozen contract changed; never resubmit")
        else:
            if any(output.glob("*.json")):
                raise RuntimeError("incomplete prior preparation requires manual reconciliation")
            report = dict(contract, status="prepared", owner="h3val_" + uuid.uuid4().hex[:16],
                          prompt_id=str(uuid.uuid4()), client_id=str(uuid.uuid4()), started_at=time.time(),
                          submit_attempted=False, lease_released=False)
            persist(output / "input-workflow.json", graph)
            persist(output / "workflow.json", workflow)
            persist(report_path, report)
        if not args.execute:
            print(canonical(report).decode())
            return 0
        report.update(status="preflight", started_at=time.time())
        persist(report_path, report)
        with httpx.Client(headers={"Authorization": "Bearer " + key_from_service()}, trust_env=False,
                          follow_redirects=False, timeout=10) as fleet:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as comfy:
                return Driver(args, report, workflow, fleet, comfy).execute()


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
