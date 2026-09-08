"""Bounded, post-response prefix diagnostics. Never run inference or log content."""
import asyncio
import copy
import json
import os
import sqlite3
import time
from contextlib import closing

from .content_audit import ArchiveReader


def common_prefix(left, right):
    return next((i for i, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))


def first_field(left, right, path=""):
    if type(left) is not type(right):
        return path or "$"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            field = path + "." + key if path else key
            if key not in left or key not in right:
                return field
            found = first_field(left[key], right[key], field)
            if found:
                return found
    elif isinstance(left, list):
        for i, (a, b) in enumerate(zip(left, right)):
            found = first_field(a, b, f"{path}[{i}]")
            if found:
                return found
        if len(left) != len(right):
            return f"{path}.length"
    elif left != right:
        return path or "$"
    return None


def stage_bodies(archive):
    pipeline = (archive or {}).get("pipeline") or {}
    bodies = pipeline.get("bodies") or {}
    return {s["stage"]: bodies[s["sha256"]] for s in pipeline.get("stages", [])
            if s.get("archived", True) and s.get("sha256") in bodies}


def rendered_messages(body):
    # Edge's template consumes these fields, not WorkBuddy's rawUsage/agent/UI metadata.
    return [{k: m[k] for k in ("role", "content", "reasoning_content", "tool_calls") if k in m}
            for m in body.get("messages", []) if isinstance(m, dict)]


def rendering_field(body, key):
    return rendered_messages(body) if key == "messages" else body.get(key)


def compare_stages(previous, current):
    result = []
    for stage in ["after_directives", "workbuddy_reordered", "workbuddy_history_preserved", "tools_stabilized", "normalized", "effective"]:
        if stage not in previous or stage not in current:
            result.append({"stage": stage, "state": "missing"})
            continue
        a, b = previous[stage], current[stage]
        fields = [first_field(rendering_field(a, key), rendering_field(b, key), key)
                  for key in ["tools", "messages", "chat_template_kwargs", "reasoning", "reasoning_effort"]]
        result.append({"stage": stage, "state": "changed" if any(fields) else "same",
                       "fields": [f for f in fields if f]})
    return result


def tool_delta(previous, current):
    def names(body):
        return [t.get("function", {}).get("name") for t in body.get("tools", [])
                if isinstance(t, dict) and isinstance(t.get("function"), dict)]
    old, new = names(previous), names(current)
    # Counts/field paths only; no arbitrary user-supplied tool strings in logs.
    return {"previous_count": len(old), "current_count": len(new),
            "added_count": len(set(new) - set(old)), "removed_count": len(set(old) - set(new)),
            "order_changed": old != new, "definitions_changed": previous.get("tools") != current.get("tools")}


class PrefixBreakCollector:
    def __init__(self, runtime):
        self.runtime = runtime
        self.tasks = set()
        self.lock = asyncio.Lock()

    def submit(self, trace, decision):
        if not trace.get("conversation_id") or len(self.tasks) >= 2:
            return
        task = asyncio.create_task(self._guard(copy.deepcopy(trace), decision))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _guard(self, trace, decision):
        result = {"request_id": trace["request_id"], "attempt": decision.attempts,
                  "state": "unavailable", "token_source": "native_tokenize_reconstruction"}
        try:
            async with self.lock:
                await asyncio.wait_for(self._inspect(trace, decision, result), timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            result.update(state="unavailable", reason=type(error).__name__)
        try:
            await asyncio.to_thread(self._save, result)
        except Exception as error:
            self.runtime.audit.write("prefix_break_unavailable", request_id=trace["request_id"], error_type=type(error).__name__)

    def _save(self, result):
        with closing(sqlite3.connect(self.runtime.route_traces.database_path, timeout=3)) as db, db:
            db.execute("INSERT OR REPLACE INTO prefix_breaks VALUES(?,?,?,?)",
                       (result["request_id"], result["attempt"], time.time(), json.dumps(result)))
            db.execute("DELETE FROM prefix_breaks WHERE created_at<?", (time.time() - 30 * 86400,))

    async def _inspect(self, trace, decision, result):
        rows = await self.runtime.route_traces.conversation(trace["conversation_id"], limit=100)
        earlier = [x for x in rows if x["started_at"] < trace["started_at"]
                   and x.get("client_id") == trace.get("client_id") and x.get("status") == "succeeded"]
        history = next((c for c in trace.get("observation", {}).get("content", {}).get("checks", []) if c.get("check") == "workbuddy_history"), {})
        matched = None
        if history.get("association") == "exact_raw_prefix" and history.get("previous_request_id"):
            matched = await self.runtime.route_traces.get(history["previous_request_id"])
            if matched and (matched.get("client_id") != trace.get("client_id") or matched.get("status") != "succeeded" or matched.get("started_at", float("inf")) >= trace["started_at"]):
                matched = None
        if not earlier and not matched:
            result.update(state="unavailable" if history.get("association") == "unconfirmed" else "baseline",
                          reason="raw_history_association_unconfirmed" if history.get("association") == "unconfirmed" else "no_previous_completed_request")
            return
        previous = matched or max(earlier, key=lambda x: x["started_at"])
        result["association"] = "verified_raw_prefix" if matched else "conversation_timeline"
        result["previous_request_id"] = previous["request_id"]
        if (previous.get("selected_model"), previous.get("deployment_id")) != (trace.get("selected_model"), trace.get("deployment_id")):
            result.update(reason="model_or_device_changed")
            return
        if (previous.get("completed_at") or float("inf")) > trace["started_at"]:
            result.update(reason="overlapping_requests")
            return
        reader = ArchiveReader(os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3"),
                               os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key"))
        old = stage_bodies(await asyncio.to_thread(reader.read, previous["request_id"]))
        new = stage_bodies(await asyncio.to_thread(reader.read, trace["request_id"]))
        result["stages"] = compare_stages(old, new)
        previous_attempt = max((a.get("number", 0) for a in previous.get("attempts", [])), default=0)
        a, b = old.get(f"forwarded_{previous_attempt}"), new.get(f"forwarded_{decision.attempts}")
        if a is None or b is None:
            result.update(reason="forwarded_archive_missing")
            return
        result["tools"] = tool_delta(a, b)
        result["forwarded_fields"] = [f for f in (first_field(rendering_field(a, k), rendering_field(b, k), k)
                                      for k in ["tools", "messages", "chat_template_kwargs", "reasoning", "reasoning_effort"]) if f]
        if decision.endpoint.backend_type != "vllm":
            result.update(state="structural", reason="native_tokenize_not_supported")
            return
        if len(json.dumps(a)) + len(json.dumps(b)) > 8 * 1024 * 1024:
            result.update(state="structural", reason="tokenize_size_limit")
            return
        root = (decision.upstream_api_base or decision.endpoint.api_base).rstrip("/").removesuffix("/v1")
        details = decision.deployment_details.get(decision.deployment_id or "", {})
        key = os.environ.get(details.get("backend_api_key_env") or decision.endpoint.backend_api_key_env, "")
        async def tokenize(body):
            # /tokenize is CPU template rendering, never /completions or /responses.
            response = await self.runtime.internal_client.post(root + "/tokenize", json=body,
                headers={"Authorization": "Bearer " + key}, timeout=8)
            response.raise_for_status()
            tokens = response.json().get("tokens")
            if not isinstance(tokens, list) or len(tokens) > 600000 or not all(type(t) is int for t in tokens):
                raise ValueError("invalid_tokenize_result")
            return tokens
        left, right = await tokenize(a), await tokenize(b)
        common = common_prefix(left, right)
        result.update(state="compared", previous_tokens=len(left), current_tokens=len(right),
                      common_tokens=common, first_different_token=common if common < max(len(left), len(right)) else None,
                      prefix_preserved=common == len(left), index_base=0,
                      equivalence="template_reconstruction_not_inference_counter")
        # Attribute the early break to tools only when replacing that field alone
        # moves the native-token boundary; otherwise leave attribution structural.
        if common < len(left) and a.get("tools") != b.get("tools"):
            probe = {**b, "tools": a.get("tools", [])}
            probe_tokens = await tokenize(probe)
            probe_common = common_prefix(left, probe_tokens)
            result["tools_counterfactual_common_tokens"] = probe_common
            if probe_common > common:
                result["cause_field"] = "tools"
                result["attribution"] = "native_tokenize_counterfactual"
        if not result.get("cause_field") and result["forwarded_fields"] and not result["prefix_preserved"]:
            result["cause_field"] = result["forwarded_fields"][0]
            result["attribution"] = "structural_candidate"
