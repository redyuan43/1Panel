from __future__ import annotations

import hashlib
import json
import os
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .clients import ComfyClient
from .config import get_settings
from .recipes import confirmed_recipe, execution_info
from .fleet_progress import owned_sampler_progress, execution_detail


class SubmissionUnknown(RuntimeError):
    pass


class FleetClient:
    is_fleet = True

    def __init__(self, base_url: str, key_file: str):
        self.base_url = base_url.rstrip("/")
        self.key_file = Path(key_file)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.capacity_lock = threading.Lock()
        self.capacity_cache = None
        self.capacity_expires = 0
        self.active_execution_ids = set()
        self.external_status_path = get_settings().comparison_results_root / "live.json"

    def _merge_external_capacity(self, result, health, capacity):
        uncertain = False
        try:
            snapshot = json.loads(self.external_status_path.read_text())
            age = time.time() - float(snapshot["observed_at"])
            if snapshot.get("available") is not True or not 0 <= age <= 30:
                raise ValueError("stale external telemetry")
            records = snapshot["cases"]
        except (OSError, ValueError, KeyError, TypeError):
            records = []
            uncertain = result["exclusive_window"] or self.external_status_path.exists()
        lanes = {source.get("gpu_uuid"): target for source, target in zip(health["lanes"], result["lanes"])
                 if source.get("gpu_uuid")}
        seen = {item.get("prompt_id") for item in capacity["active"] if item.get("prompt_id")}
        for record in records:
            status = record.get("status")
            if status not in {"running", "queued", "reconciling", "finalizing", "submitting"}:
                continue
            identifier = record.get("prompt_id")
            if identifier and identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            lane = lanes.get(record.get("gpu_uuid"))
            if status == "queued":
                result["queued"] += 1
            elif status == "running" and lane is not None:
                result["active"] += 1
                lane["status"] = "busy"
                lane.setdefault("external_tasks", []).append(str(record.get("id", "验证任务")))
            elif lane is not None:
                if lane["status"] == "idle":
                    lane["status"] = "unknown"
                uncertain = True
            else:
                uncertain = True
        if uncertain:
            for lane in result["lanes"]:
                if lane["status"] == "idle":
                    lane["status"] = "unknown"
        result["counts_complete"] = not uncertain

    def _headers(self):
        key = self.key_file.read_text().strip()
        if not key:
            raise RuntimeError("Fleet 密钥未配置。")
        return {"Authorization": "Bearer " + key}

    def _request(self, method, path, payload=None, *, data=None, content_type=None, timeout=60):
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def health(self):
        return self._request("GET", "/system_stats")

    def recipe_catalog(self):
        try:
            options = self._request("GET", "/api/router/options", timeout=8)
            catalog = options.get("recipe_catalog")
            return catalog if isinstance(catalog, dict) else {"enabled": False, "recipes": []}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"enabled": False, "recipes": [], "reason": "配方调度未就绪"}

    def capacity(self, *, fresh=False):
        with self.capacity_lock:
            if not fresh and time.monotonic() < self.capacity_expires:
                return self.capacity_cache
            try:
                health = self._request("GET", "/api/health", timeout=8)
                capacity = self._request("GET", "/api/router/capacity", timeout=8)
                queues = {item["lane_id"]: item for item in capacity["queues"]}
                lanes = []
                for lane in health["lanes"]:
                    queue = queues.get(lane["id"], {})
                    known = lane.get("ok") is True and lane.get("enabled") is True and "queued_or_running" in queue
                    busy = max(lane.get("active_job_count", 0), queue.get("queued_or_running", 0), queue.get("untracked_count", 0))
                    lanes.append({"id": lane["id"], "name": lane["device"],
                                  "status": "unknown" if not known else "busy" if busy else "idle",
                                  "enabled": lane.get("enabled") is True,
                                  "preview_only": lane.get("preview_only", False),
                                  "vram_free": lane.get("vram_free"), "vram_total": lane.get("vram_total")})
                resources = capacity["resources"]
                policy = capacity["policy"]
                maximum = policy.get("max_active_jobs", 3)
                if type(maximum) is not int or not 1 <= maximum <= 3:
                    raise ValueError("invalid max_active_jobs")
                self.active_execution_ids = {item["execution_id"] for item in capacity["active"]
                                             if item.get("execution_id") and item.get("status") != "queued"}
                result = {"available": True, "sampled_at": time.time(), "lanes": lanes,
                          "queued": sum(item["status"] == "queued" for item in capacity["active"]),
                          "active": sum(item["status"] != "queued" for item in capacity["active"]),
                          "limits": {"short_preview": min(maximum, policy["short"]["preview"]["max_parallel"]),
                                     "short_quality": min(maximum, policy["short"]["quality"]["max_parallel"]),
                                     "short_frames": policy["short"]["preview"]["max_frames"],
                                     "long": min(maximum, policy["long"]["max_parallel"])},
                          "resources_ok": resources.get("ok") is True,
                          "model_lifecycle": capacity.get("model_lifecycle"),
                          "preparation_guard": capacity.get("preparation_guard"),
                          "studio_preview": {key: capacity["studio_preview"].get(key) for key in (
                              "max_parallel", "available", "validated_parallel", "reason")}
                              if isinstance(capacity.get("studio_preview"), dict) else None,
                          "memory_available_bytes": resources.get("memory_available_bytes"),
                          "swap_used_bytes": resources.get("swap_used_bytes"),
                          "swap_recovery": resources.get("swap_recovery"),
                          "exclusive_window": bool(capacity.get("validation_lease"))}
                result["recipe_capacity"] = {
                    identifier: {key: value.get(key) for key in (
                        "available_slots", "eligible_lanes", "reasons", "recipe_version")}
                    for identifier, value in (capacity.get("recipe_capacity") or {}).items()
                    if isinstance(value, dict)
                }
                self._merge_external_capacity(result, health, capacity)
                remaining = max(0, maximum - result["active"])
                if result["studio_preview"] is not None:
                    preview = result["studio_preview"]
                    preview["max_parallel"] = min(maximum, preview["max_parallel"])
                    preview["available"] = min(remaining, preview["max_parallel"], preview["available"])
                    preview["validated_parallel"] = preview["validated_parallel"] is True and preview["max_parallel"] > 1
                for entry in result["recipe_capacity"].values():
                    if type(entry.get("available_slots")) is int:
                        entry["available_slots"] = min(remaining, entry["available_slots"])
            except Exception:
                result = {"available": False, "sampled_at": time.time(), "lanes": [],
                          "reason": "无法读取三卡实时状态；不沿用旧的空闲数"}
            self.capacity_cache = result
            self.capacity_expires = time.monotonic() + 10
            return result

    def free(self):
        return None

    def begin_batch(self, schedule_id):
        return self._request("POST", "/api/studio/batch-window", {"owner": "studio_batch_" + schedule_id})

    def end_batch(self, schedule_id):
        return self._request("DELETE", "/api/studio/batch-window", {"owner": "studio_batch_" + schedule_id})

    def upload_assets(self, assets):
        for asset in assets.values():
            path = Path(asset["path"])
            if path.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("单个素材不能超过128MiB。")
            data = path.read_bytes()
            boundary = "h3studio-" + hashlib.sha256(data).hexdigest()
            filename = asset["comfy_name"]
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
            self._request("POST", "/api/inputs/" + urllib.parse.quote(filename, safe="") + "?overwrite=false",
                          data=body, content_type="multipart/form-data; boundary=" + boundary)

    def execution(self, execution_id):
        try:
            job = self._request("GET", "/api/jobs/by-execution/" + urllib.parse.quote(execution_id, safe=""))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        if job.get("recipe_id"):
            job["execution"] = self._request("GET", "/api/router/executions/" + urllib.parse.quote(execution_id, safe=""))
        return job

    def submit_stage(self, workflow, execution_id, stage, profile, *, recipe=None, prepared=None):
        existing = self.execution(execution_id)
        if existing:
            return existing["prompt_id"]
        payload = {"prompt": workflow, "extra_data": {"h3": {
            "execution_id": execution_id, "stage": stage, "profile": profile, "studio": True,
        }}}
        if recipe is not None:
            if stage != "preview" or profile != "preview":
                raise RuntimeError("配方仅支持限定文生视频预览。")
            entry = confirmed_recipe(self.recipe_catalog(), recipe["recipe_id"])
            try:
                built = self._request("POST", "/api/router/recipe-workflow", recipe)
                graph, binding = built["prompt"], built["recipe_binding"]
                digest = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                if (not isinstance(graph, dict) or not graph
                        or built.get("enabled") is not True
                        or binding.get("recipe_id") != recipe["recipe_id"]
                        or binding.get("recipe_version") != entry["version"]
                        or binding.get("graph_sha256") != digest):
                    raise ValueError("canonical graph binding mismatch")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise RuntimeError("配方调度未就绪：规范图构建或绑定校验失败；未提交生成。") from error
            payload["prompt"] = graph
            payload["extra_data"]["h3"].update(recipe_id=recipe["recipe_id"], recipe_version=entry["version"])
            payload["extra_data"]["h3"]["contract"] = dict(binding)
            if prepared:
                try:
                    prepared(graph, binding)
                except OSError as error:
                    raise RuntimeError("规范图本地记录失败；未提交生成。") from error
        try:
            response = self._request("POST", "/prompt", payload)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code in {400, 401, 403, 409, 413}:
                raise RuntimeError(f"Fleet 拒绝任务：HTTP {error.code}") from error
            raise SubmissionUnknown("提交结果未知，保留执行ID，仅查询对账，禁止重新生成。") from error
        if not response.get("prompt_id"):
            raise SubmissionUnknown("提交响应缺少任务ID，等待原执行ID对账。")
        return response["prompt_id"]

    def prompt_state(self, prompt_id):
        job = self._request("GET", "/api/jobs/" + urllib.parse.quote(prompt_id, safe=""))
        return "active" if job["status"] in {"queued", "reserved", "submitted", "running", "reconciling"} else job["status"]

    def wait_execution(self, execution_id, destination, *, progress, cancelled, timeout_seconds=10800):
        started = time.monotonic()
        cancellation_sent = False
        while time.monotonic() - started < timeout_seconds:
            try:
                job = self.execution(execution_id)
                if job:
                    status = job["status"]
                    execution = execution_info(job)
                    execution.update(node_id=job.get("node_id"), sampler_progress=owned_sampler_progress(job))
                    progress({"execution": execution})
                    if status in {"cancelled", "error"}:
                        raise RuntimeError("任务已取消。" if status == "cancelled" else "工作流执行失败。")
                    if cancelled() and status != "completed" and not cancellation_sent:
                        self._request("POST", "/api/jobs/" + job["prompt_id"] + "/cancel", {})
                        cancellation_sent = True
                        continue
                    if status == "completed":
                        return self._download_output(job, destination, started)
                    progress({"status": "queued" if status == "queued" else "running", "progress": None,
                              "prompt_id": job["prompt_id"], "lane_id": job.get("lane_id"),
                              "detail": execution_detail(job, execution)})
                else:
                    progress({"detail": "等待原执行ID对账；未重新提交", "submission_unknown": True})
            except (urllib.error.URLError, TimeoutError, OSError):
                progress({"detail": "节点连接中断，保留任务并等待恢复"})
            time.sleep(3)
        raise SubmissionUnknown("观察窗口已结束；后台任务结果未知，不能重跑。")

    def _download_output(self, job, destination, started):
        history = self._request("GET", "/history/" + job["prompt_id"])
        output = ComfyClient._find_video_output(history.get(job["prompt_id"], {}))
        if not output:
            raise SubmissionUnknown("执行完成但产物尚未可读取，需继续对账。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        request = urllib.request.Request(self.base_url + "/view?" + urllib.parse.urlencode(output), headers=self._headers())
        with self.opener.open(request, timeout=300) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
        if not temporary.stat().st_size:
            raise SubmissionUnknown("产物下载为空；保留原任务，不能重新生成。")
        temporary.replace(destination)
        return {"prompt_id": job["prompt_id"], "execution_id": job["execution_id"], "lane_id": job.get("lane_id"),
                "comfy_output": output, "elapsed_seconds": round(time.monotonic() - started, 1),
                "execution": execution_info(job)}


def configured_client():
    nodes_file = os.environ.get("H3_FLEET_NODES_FILE", "").strip()
    if nodes_file:
        from .multifleet import MultiFleetClient, read_nodes
        nodes, legacy = read_nodes(nodes_file)
        return MultiFleetClient(nodes, legacy, get_settings().data_root / "fleet-routes.sqlite3")
    url = os.environ.get("H3_FLEET_URL", "").strip()
    if not url:
        return ComfyClient()
    key_file = os.environ.get("H3_FLEET_KEY_FILE", "").strip()
    if not key_file:
        raise RuntimeError("H3_FLEET_URL requires H3_FLEET_KEY_FILE")
    return FleetClient(url, key_file)
