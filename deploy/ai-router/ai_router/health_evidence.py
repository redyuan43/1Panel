"""Content-free health evidence. Classification never changes endpoint eligibility."""
from __future__ import annotations

import socket
import ssl
import asyncio
import logging
import queue
import threading

import httpx


class HealthAuditWriter:
    """One daemon writer, bounded backlog, and no disk wait in the probe path."""

    def __init__(self, capacity=256):
        self.pending = queue.Queue(maxsize=capacity)
        self.stopping = threading.Event()
        self.thread = None
        self.dropped = 0

    def submit(self, audit, event, **fields):
        if self.stopping.is_set():
            self.dropped += 1
            return
        try:
            self.pending.put_nowait((audit, event, fields))
        except queue.Full:
            self.dropped += 1
            return
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="health-audit", daemon=True)
            self.thread.start()

    def _run(self):
        reported_drops = 0
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                audit, event, fields = self.pending.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                audit.write(event, **fields)
            except Exception as exc:
                logging.getLogger(__name__).warning("Health audit unavailable (%s)", type(exc).__name__)
            finally:
                self.pending.task_done()
            dropped = self.dropped
            if dropped > reported_drops:
                logging.getLogger(__name__).warning("Health audit queue dropped %s events", dropped - reported_drops)
                reported_drops = dropped

    async def close(self, timeout=1.0):
        self.stopping.set()
        deadline = asyncio.get_running_loop().time() + timeout
        while self.thread is not None and self.thread.is_alive():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))


def probe_failure(error=None, status=None):
    if error is not None:
        chain = []
        current = error
        while current is not None and len(chain) < 8 and current not in chain:
            chain.append(current)
            current = current.__cause__ or current.__context__
        category, phase = "unknown", "unknown"
        if any(isinstance(item, socket.gaierror) for item in chain):
            category, phase = "dns_error", "dns"
        elif any(isinstance(item, ssl.SSLError) for item in chain):
            category, phase = "tls_error", "tls"
        else:
            for kind, name, stage in (
                (httpx.PoolTimeout, "pool_timeout", "pool"),
                (httpx.ConnectTimeout, "connect_timeout", "connect"),
                (httpx.ReadTimeout, "read_timeout", "read"),
                (httpx.WriteTimeout, "write_timeout", "write"),
                (httpx.ProxyError, "proxy_error", "proxy"),
                (httpx.ConnectError, "connect_error", "connect"),
                (httpx.ReadError, "read_error", "read"),
                (httpx.WriteError, "write_error", "write"),
                (httpx.ProtocolError, "protocol_error", "protocol"),
            ):
                if isinstance(error, kind):
                    category, phase = name, stage
                    break
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        if code is not None:
            category, phase = http_failure_category(code), "response"
        return {"category": category, "phase": phase, "http_status": code,
                "exception_type": type(error).__name__,
                "cause_types": [type(item).__name__ for item in chain[1:]]}
    if status is None or status.healthy:
        return None
    code = status.detail.get("status_code")
    category, phase = "backend_unhealthy", "backend"
    if isinstance(code, int) and not 200 <= code < 300:
        category, phase = http_failure_category(code), "response"
    elif status.detail.get("reason") == "backend_no_progress":
        category = "backend_no_progress"
    return {"category": category,
            "phase": phase, "http_status": code,
            "exception_type": None, "cause_types": []}


def http_failure_category(code):
    if code in (401, 403):
        return "authentication"
    if code == 429:
        return "rate_limit"
    if code >= 500:
        return "server_error"
    return "http_error"


def health_snapshot(status, now, stale_after):
    # Old Redis samples may have arbitrary backend detail: never copy it wholesale.
    probe = status.detail.get("probe") or {}
    failure = probe.get("failure")
    if isinstance(failure, dict):
        failure = {key: failure.get(key) for key in
                   ("category", "phase", "http_status", "exception_type", "cause_types")}
    else:
        failure = None
    age = max(0.0, now - status.checked_at)
    return {"probe_id": probe.get("probe_id"), "instance_id": probe.get("instance_id"),
            "boot_id": probe.get("boot_id"), "checked_at": status.checked_at,
            "completed_at": probe.get("completed_at"), "age_seconds": round(age, 3),
            "elapsed_ms": probe.get("elapsed_ms"), "healthy": status.healthy,
            "stale": age > stale_after, "failure": failure}
