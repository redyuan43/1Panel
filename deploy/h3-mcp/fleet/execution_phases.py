from __future__ import annotations

import json
import math
import time


def phases(job, receipt, now=None):
    now = time.time() if now is None else now
    result = {"phase": "unknown", "model_state": "unknown", "phase_timings": {},
              "timing_basis": "owned execution node events; model preparation includes transfers and conditioning"}
    reason = job.get("admission_reason") or ""
    if job["status"] == "queued":
        result["phase"] = "backend_disabled" if "backend_disabled" in reason else "backend_starting" if "backend_starting" in reason else "resource_waiting"
        return result
    if job["status"] in {"reconciling", "cancelling", "cancelled", "error", "missing"}:
        result["phase"] = job["status"]
        return result
    try:
        workflow = json.loads(job["request_json"])["prompt"]
        sampler = {node for node, item in workflow.items() if "Sampler" in item["class_type"] and "Select" not in item["class_type"]}
        if not receipt or receipt.get("error") or receipt.get("prompt_id") != job.get("upstream_prompt_id"):
            return result
        events = receipt.get("events", [])
        previous = None
        phase = "unknown"
        for event in events:
            stamp = event["received_at"]
            if type(stamp) not in (float, int) or not math.isfinite(stamp) or stamp > now or previous is not None and stamp < previous:
                raise ValueError("invalid phase event clock")
            if event.get("prompt_id") != job["upstream_prompt_id"]:
                continue
            if previous is not None and phase != "unknown":
                result["phase_timings"][phase] = result["phase_timings"].get(phase, 0) + stamp - previous
            previous = stamp
            kind, data = event["type"], event.get("data", {})
            if kind == "executing" and data.get("node") is not None:
                node = str(data["node"])
                class_name = workflow[node]["class_type"]
                phase = "model_preparing" if "Loader" in class_name or node in sampler else "decoding" if "Decode" in class_name else "saving" if class_name == "SaveVideo" else "input_preparing"
            elif kind == "progress" and event.get("confirmed_sampler_progress"):
                phase = "sampling"
                result["model_state"] = "sampling_model_loaded"
            elif kind in {"execution_success", "execution_error", "execution_interrupted"}:
                phase = {"execution_success": "completed", "execution_error": "error", "execution_interrupted": "cancelled"}[kind]
        if previous is not None and phase not in {"unknown", "completed", "error", "cancelled"} and receipt.get("connected"):
            result["phase_timings"][phase] = result["phase_timings"].get(phase, 0) + now - previous
        result["phase"] = "completed" if job["status"] == "completed" else phase
        if result["phase"] == "model_preparing":
            result["model_state"] = "loading"
        result["phase_timings"] = {key: round(value, 3) for key, value in result["phase_timings"].items()}
        return result
    except (ValueError, KeyError, TypeError):
        return {**result, "phase": "unknown", "model_state": "unknown", "phase_timings": {}}


def install(module):
    original = module.execution_public

    def public(job):
        result = original(job)
        listener = module.fleet.recipes.progress.get(job["prompt_id"])
        try:
            receipt = listener.receipt() if listener else json.loads(job.get("progress_json") or "null")
        except ValueError:
            receipt = None
        result.update(phases(job, receipt))
        return result

    module.execution_public = public
