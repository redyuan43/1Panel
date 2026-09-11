"""Request-correlated cache telemetry. This module never stores prompt content."""
from __future__ import annotations

import asyncio
import codecs
import json
import math
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit, urlunsplit
from ai_router.usage_evidence import token_count


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS cache_operations (
            operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            updated_at REAL NOT NULL, payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS cache_operations_request
            ON cache_operations(request_id, updated_at);
        CREATE TABLE IF NOT EXISTS workbuddy_history (
            request_id TEXT PRIMARY KEY, scope TEXT NOT NULL, prefix_hash TEXT NOT NULL,
            message_count INTEGER NOT NULL, created_at REAL NOT NULL, version INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS workbuddy_history_scope ON workbuddy_history(scope,created_at);
        CREATE TABLE IF NOT EXISTS prefix_breaks (
            request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
            created_at REAL NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(request_id, attempt));
        CREATE INDEX IF NOT EXISTS prefix_breaks_created ON prefix_breaks(created_at);
    """)


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def metrics(trace, operations):
    request = trace.get("request", {})
    evidence = {}
    last_attempt = max((a.get("number", 0) for a in trace.get("attempts", [])), default=0)
    for attempt in trace.get("attempts", []):
        if attempt.get("number") != last_attempt:
            continue
        for step in attempt.get("steps", []):
            if step.get("node_id") == "upstream_request":
                evidence.update(step.get("evidence", {}))
    observer = trace.get("observation", {})
    content_stages = observer.get("content", {}).get("stages", [])
    tail = content_stages[0].get("last_role") if content_stages else None
    foreground = sorted((o for o in operations if o.get("kind") == "foreground" and o.get("request_id") == trace["request_id"]), key=lambda x: x.get("attempt", 0))
    last_attempt = max((a.get("number", 0) for a in trace.get("attempts", [])), default=0)
    final = next((o for o in reversed(foreground) if o.get("attempt") == last_attempt), {})
    timing = final.get("timings") or {}
    prepared = final.get("cache") or {}
    p = number(timing.get("prompt_n"))
    c = number(timing.get("cache_n"))
    q = number(prepared.get("prime_tokens"))
    f = number(prepared.get("fixed_tokens"))
    native_input = p + c if p is not None and c is not None else None
    actual_input = native_input if native_input is not None else number(evidence.get("input_tokens"))
    total_work = p + q if p is not None and q is not None else None
    fixed_ratio = max(0, min(f, c) - q) / f if f and c is not None and q is not None else None
    # Trace input_tokens may be the Router's estimate when usage is absent.
    # Only same-attempt native counters can prove preparation-adjusted reuse.
    net_ratio = max(0, native_input - total_work) / native_input if native_input and total_work is not None else None
    queue = observer.get("queue_wait_ms")
    if queue is None:
        waits = [number(s.get("evidence", {}).get("queue_wait_ms")) for a in trace.get("attempts", []) for s in a.get("steps", []) if "queue_wait_ms" in s.get("evidence", {})]
        queue = sum(x for x in waits if x is not None) if any(x is not None for x in waits) else None
    completed = trace.get("completed_at")
    reported = evidence.get("backend_usage") or {}
    reported_input = token_count(reported.get("input_tokens"))
    reported_cached = token_count(reported.get("cached_tokens"))
    success = trace.get("status") == "succeeded"
    backend_input = reported_input if success and reported.get("state") != "invalid" else None
    backend_cached = None
    source = "unavailable"
    if success and native_input is not None and token_count(p) is not None and token_count(c) is not None and final.get("terminal") and final.get("status") in {"completed", "succeeded"}:
        backend_input, backend_cached, source = int(native_input), int(c), "gateway_native"
    elif success and reported.get("state") == "complete" and reported_input is not None and reported_cached is not None and reported_cached <= reported_input:
        backend_input, backend_cached, source = reported_input, reported_cached, "upstream_usage"
    estimated_cached = token_count(evidence.get("cached_prompt_tokens")) if success and reported.get("state") != "incomplete" and evidence.get("cache_measurement_source") == "backend_counter_delta" else None
    estimated_input = token_count(evidence.get("input_tokens"))
    if estimated_input is None or (estimated_cached is not None and estimated_cached > estimated_input):
        estimated_cached = None
    backend_ratio = backend_cached / backend_input if backend_input and backend_cached is not None else None
    if trace.get("status") == "running":
        cache_status, reason = "running", "等待请求完成"
    elif backend_cached is not None:
        cache_status, reason = ("hit" if backend_cached > 0 else "miss"), "同次执行的完整后端计数"
    elif estimated_cached is not None:
        cache_status, reason = "estimated", "仅有全局计数差值，不能确认本次命中"
    else:
        cache_status = "unknown"
        reason = "请求未成功完成，不能判定命中" if not success else "后端计数无效" if reported.get("state") == "invalid" else "后端未提供完整逐请求计数"
    return {
        "cache_status": cache_status, "cache_reason": reason,
        "cache_measurement": "measured" if backend_cached is not None else "estimated" if estimated_cached is not None else "unavailable",
        "backend_input_tokens": backend_input, "backend_cached_tokens": backend_cached,
        "backend_reuse_ratio": backend_ratio, "backend_usage_source": source,
        "uncached_input_tokens": backend_input - backend_cached if backend_cached is not None else None,
        "estimated_cached_tokens": estimated_cached,
        "estimated_reuse_ratio": estimated_cached / estimated_input if estimated_input and estimated_cached is not None else None,
        "input_measurement": "measured" if backend_input is not None else "estimated" if actual_input is not None else "unavailable",
        "request_id": trace["request_id"], "started_at": trace["started_at"],
        "client_id": trace.get("client_id"), "conversation_id": trace.get("conversation_id"),
        "model": trace.get("selected_model") or trace.get("requested_model"),
        "device": final.get("deployment_id") or trace.get("deployment_id") or trace.get("endpoint_id"),
        "status": trace.get("status"), "event": prepared.get("event"),
        "request_kind": "工具续请求" if tail == "tool" else "助手续请求" if tail == "assistant" else "首次请求" if trace.get("lineage_relation") == "new" else "后续请求" if tail else "未采集",
        "input_tokens": actual_input, "output_tokens": number(evidence.get("output_tokens")),
        "cached_tokens": c if c is not None else number(evidence.get("cached_prompt_tokens")),
        "cache_source": "gateway_native" if c is not None else evidence.get("cache_measurement_source", "legacy_unspecified"),
        "reported_cache_ratio": (c if c is not None else number(evidence.get("cached_prompt_tokens"))) / actual_input if actual_input and (c is not None or number(evidence.get("cached_prompt_tokens")) is not None) else None,
        "prime_tokens": q, "prompt_tokens": p, "total_prefill_tokens": total_work,
        "fixed_tokens": f, "fixed_reuse_ratio": fixed_ratio, "net_cache_ratio": net_ratio,
        "dynamic_tokens": max(0, native_input - f) if native_input is not None and f is not None else None,
        "prefill_ms": number(timing.get("prompt_ms")), "prefill_tps": number(timing.get("prompt_per_second")),
        "queue_ms": number(queue), "gateway_queue_ms": number(final.get("queue_ms")),
        "restore_ms": number(prepared.get("restore_ms")),
        "ttft_ms": number(observer.get("ttft_ms")), "first_text_ms": number(observer.get("first_text_ms")),
        "gateway_ttft_ms": number(final.get("ttft_ms")),
        "total_ms": max(0, (completed - trace["started_at"]) * 1000) if completed else None,
        "measurement": "measured" if total_work is not None else "unavailable",
        "attempts": len(trace.get("attempts", [])),
        "background_operations": sum(o.get("kind") == "prewarm" for o in operations),
        "all_attempts_prefill_tokens": sum((number((o.get("timings") or {}).get("prompt_n")) or 0) + (number((o.get("cache") or {}).get("prime_tokens")) or 0) for o in foreground) if foreground and all(number((o.get("timings") or {}).get("prompt_n")) is not None and number((o.get("cache") or {}).get("prime_tokens")) is not None for o in foreground) and len(foreground) == last_attempt else None,
    }


class CacheAudit:
    def __init__(self, database_path):
        self.path = str(database_path)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        return connection

    async def save_operation(self, value):
        await asyncio.to_thread(self._save_operation, value)

    def _save_operation(self, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO cache_operations VALUES(?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET updated_at=excluded.updated_at,payload_json=excluded.payload_json WHERE cache_operations.request_id=excluded.request_id AND COALESCE(json_extract(cache_operations.payload_json,'$.terminal'),0)=0",
                       (value["operation_id"], value["request_id"], time.time(), json.dumps(value, separators=(",", ":"))))

    async def detail(self, trace):
        return await asyncio.to_thread(self._detail, trace)

    def _detail(self, trace):
        with closing(self.connect()) as db:
            ops = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM cache_operations WHERE request_id=? ORDER BY updated_at", (trace["request_id"],))]
            breaks = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM prefix_breaks WHERE request_id=? ORDER BY attempt", (trace["request_id"],))]
        return {"request": metrics(trace, ops), "operations": ops, "prefix_breaks": breaks}

    async def query(self, **filters):
        return await asyncio.to_thread(self._query, filters)

    async def for_requests(self, request_ids):
        return await asyncio.to_thread(self._for_requests, request_ids)

    def _for_requests(self, request_ids):
        if not request_ids: return {}
        placeholders = ",".join("?" for _ in request_ids)
        with closing(self.connect()) as db:
            traces = db.execute("SELECT request_id,payload_json FROM route_traces WHERE request_id IN ("+placeholders+")", request_ids).fetchall()
            ops = db.execute("SELECT request_id,payload_json FROM cache_operations WHERE request_id IN ("+placeholders+")", request_ids).fetchall()
        grouped = {}
        for row in ops: grouped.setdefault(row[0], []).append(json.loads(row[1]))
        return {r[0]: metrics(json.loads(r[1]), grouped.get(r[0], [])) for r in traces}

    def _query(self, filters):
        end = float(filters.get("until") or time.time())
        start = float(filters.get("since") or end - 86400)
        start = max(start, end - 30 * 86400)
        clauses, params = ["started_at>=?", "started_at<=?"], [start, end]
        if filters.get("client_group") == "workbuddy" and not filters.get("client_id"):
            clauses.append("lower(client_id) LIKE ?"); params.append("workbuddy%")
        for key, column in (("client_id", "client_id"), ("conversation_id", "conversation_id"), ("model", "selected_model"), ("status", "status")):
            if filters.get(key):
                clauses.append(column + "=?"); params.append(filters[key])
        items = []
        with closing(self.connect()) as db:
            # Bounded analysis window, explicitly reported when truncated.
            rows = db.execute("SELECT request_id,payload_json FROM route_traces WHERE " + " AND ".join(clauses) + " ORDER BY started_at DESC,request_id DESC LIMIT 10001", params).fetchall()
            truncated = len(rows) > 10000
            for row in rows[:10000]:
                ops = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM cache_operations WHERE request_id=?", (row[0],))]
                item = metrics(json.loads(row[1]), ops)
                if filters.get("device") and item["device"] != filters["device"]: continue
                if filters.get("event") and item["event"] != filters["event"]: continue
                items.append(item)
        return {"items": items, "total": len(items), "truncated": truncated, "since": start, "until": end}

    @staticmethod
    def summarize(result):
        complete = [x for x in result["items"] if x["status"] == "succeeded"]
        def distribution(key, rows=complete):
            values = sorted(x[key] for x in rows if x.get(key) is not None)
            n = len(values)
            return {"n": n, "median": (values[(n-1)//2] + values[n//2])/2 if n else None,
                    "p95": values[max(0, math.ceil(n*.95)-1)] if n else None}
        keys = ("backend_reuse_ratio", "ttft_ms", "first_text_ms", "queue_ms", "prefill_ms", "prefill_tps", "restore_ms", "net_cache_ratio", "fixed_reuse_ratio")
        known = [x for x in complete if x["fixed_reuse_ratio"] is not None]
        buckets = {}
        for row in complete:
            bucket = int(row["started_at"] // 3600) * 3600
            buckets.setdefault(bucket, []).append(row)
        return {"total": result["total"], "succeeded": len(complete), "truncated": result["truncated"],
                "since": result["since"], "until": result["until"],
                "metrics": {k: distribution(k) for k in keys},
                "fixed_pass": {"n": len(known), "passed": sum(x["fixed_reuse_ratio"] >= .95 for x in known)},
                "trend": [{"at": b, "count": len(rows), **{k: distribution(k, rows) for k in keys}} for b, rows in sorted(buckets.items())]}


class TelemetryCollector:
    def __init__(self, runtime):
        self.runtime = runtime
        self.tasks = set()

    def collect(self, *, base_url, key, request_id, operation_id, attempt, deployment_id, kind="foreground"):
        if len(self.tasks) >= 64:
            self.runtime.audit.write("cache_telemetry_unavailable", request_id=request_id, operation_id=operation_id, reason="collector_capacity")
            return
        parts = urlsplit(base_url)
        root = urlunsplit((parts.scheme, parts.netloc, parts.path.removesuffix("/v1").rstrip("/"), "", ""))
        task = asyncio.create_task(self._collect(root, key, request_id, operation_id, attempt, deployment_id, kind))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _collect(self, root, key, request_id, operation_id, attempt, deployment_id, kind):
        try:
            misses = 0
            for _ in range(360):
                response = await self.runtime.internal_client.get(root + "/cache/telemetry/" + operation_id,
                    headers={"Authorization": "Bearer " + key}, timeout=2)
                if response.status_code == 404:
                    misses += 1
                    if misses > 3: return
                    await asyncio.sleep(5); continue
                if response.status_code != 200: return
                value = response.json()
                if value.get("operation_id") != operation_id or value.get("request_id") != request_id or value.get("attempt") != attempt or value.get("kind") != kind: return
                value.update(attempt=attempt, deployment_id=deployment_id, kind=kind)
                await CacheAudit(self.runtime.route_traces.database_path).save_operation(value)
                if value.get("terminal"): return
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.runtime.audit.write("cache_telemetry_unavailable", request_id=request_id, operation_id=operation_id, error_type=type(error).__name__)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class OutputClock:
    """Observe complete SSE events, never role-only frames or keepalives."""
    def __init__(self, protocol, started_at):
        self.protocol = protocol
        self.started_at = started_at
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""
        self.values = {}

    def feed(self, block):
        self.buffer += self.decoder.decode(block)
        self.buffer = self.buffer.replace("\r\n", "\n")
        while "\n\n" in self.buffer:
            frame, self.buffer = self.buffer.split("\n\n", 1)
            raw = "\n".join(x[5:].lstrip() for x in frame.splitlines() if x.startswith("data:"))
            try:
                value = json.loads(raw)
                if not isinstance(value, dict): continue
                timings = value.get("timings") or {}
                if number(timings.get("prompt_ms")) is not None:
                    self.values["prefill_seconds"] = timings["prompt_ms"] / 1000
                if self.protocol == "chat":
                    deltas = [(c.get("delta") or {}) for c in value.get("choices", [])]
                    text = any(d.get("content") for d in deltas)
                    output = text or any(d.get(k) for d in deltas for k in ("reasoning", "reasoning_content")) or any(
                        (call.get("function") or {}).get("name") or (call.get("function") or {}).get("arguments")
                        for d in deltas for call in d.get("tool_calls", []))
                else:
                    event = value.get("type", "")
                    text = event == "response.output_text.delta" and bool(value.get("delta"))
                    output = text or (event in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta", "response.function_call_arguments.delta"} and bool(value.get("delta"))) or (event == "response.output_item.added" and (value.get("item") or {}).get("type") == "function_call")
                elapsed = (time.monotonic() - self.started_at) * 1000
                if output:
                    self.values.setdefault("ttft_ms", elapsed)
                    self.values["last_output_ms"] = elapsed
                if text: self.values.setdefault("first_text_ms", elapsed)
            except (ValueError, AttributeError, TypeError):
                pass
        if len(self.buffer) > 1024*1024: self.buffer = ""
