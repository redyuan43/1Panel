"""Owned Comfy sampler telemetry, independent of resource admission.

Await start() before POST and reuse client_id in POST; bind the returned
upstream prompt ID explicitly. There is no automatic binding or reconnect.
feed(message, now=seconds) and receipt(now=seconds) support one consistent
offline clock. Without now, timestamps are Unix seconds and elapsed durations
use monotonic time. Offline tests may set connected=True to simulate transport.
Final overlap must use sampler_intervals, not the outer start/finish envelope.
These are observed sampler-event spans, not GPU kernel concurrency or quality.
start() lazily requires websockets >=15 (proxy=None); importing is CPU-only.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from urllib.parse import urlencode, urlsplit, urlunsplit


MAX_BUFFERED_EVENTS = 512
MAX_EVENTS = 4096
EVENT_TYPES = {"execution_start", "executing", "progress", "execution_cached",
               "execution_success", "execution_error", "execution_interrupted"}


def _clock(now):
    if now is None:
        return time.time(), time.monotonic()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("now must be finite seconds")
    return float(now), float(now)


def _reject_constant(value):
    raise ValueError("nonfinite JSON number: " + value)


class RecipeProgress:
    def __init__(self, url, client_id, sampler_nodes):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("url must be a backend origin without credentials/path/query")
        if not isinstance(client_id, str) or not client_id.strip():
            raise ValueError("client_id is required")
        if (not isinstance(sampler_nodes, (list, tuple, set)) or not sampler_nodes
                or any(not isinstance(node, str) or not node for node in sampler_nodes)):
            raise ValueError("sampler_nodes must contain explicit API node IDs")
        self.client_id = client_id
        self.sampler_nodes = frozenset(sampler_nodes)
        scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
        self.url = urlunsplit((scheme, parsed.netloc, "/ws", urlencode({"clientId": client_id}), ""))
        self.prompt_id = None
        self.connected = False
        self.error = None
        self.cached = False
        self._closed = False
        self._started = False
        self._socket = None
        self._reader = None
        self._buffer = []
        self._events = []
        self._segments = []
        self._active = None
        self._node = None
        self._terminal = False
        self._execution_started = False
        self._last_received = None
        self._last_progress_at = None
        self._sampler_finished_at = None
        self._ignored = 0

    async def start(self):
        if self._started or self._closed:
            raise RuntimeError("progress listener is single-use; never reconnect an old window")
        self._started = True
        try:
            from websockets.asyncio.client import connect
            self._socket = await connect(self.url, proxy=None, open_timeout=10,
                                         close_timeout=2, ping_interval=20, ping_timeout=60,
                                         max_size=2**20, max_queue=64)
            self.connected = True
            self._reader = asyncio.create_task(self._receive())
        except Exception as error:
            self.disconnect("websocket_connect_failed:" + type(error).__name__)
            raise

    async def _receive(self):
        try:
            async for message in self._socket:
                self.feed(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.disconnect("websocket_receive_failed:" + type(error).__name__)
        finally:
            if not self._closed:
                self.disconnect(self.error or "websocket_disconnected")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        previous_error = self.error
        self.disconnect(self.error or "listener_closed")
        self.error = previous_error
        try:
            if self._socket is not None:
                await self._socket.close()
        finally:
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except asyncio.CancelledError:
                    pass

    def disconnect(self, reason="websocket_disconnected"):
        self.connected = False
        self.error = reason
        if self._active is not None:
            self._finish_segment(self._active["last_progress_at"], self._active["_last_mono"],
                                 "connection_lost")
        self._node = None
        self._buffer.clear()

    def bind(self, upstream_prompt_id):
        if not isinstance(upstream_prompt_id, str) or not upstream_prompt_id.strip():
            raise ValueError("an explicit upstream prompt ID is required")
        if self.prompt_id is not None:
            if self.prompt_id != upstream_prompt_id:
                raise ValueError("cannot rebind a listener to another prompt")
            return
        self.prompt_id = upstream_prompt_id
        buffered, self._buffer = self._buffer, []
        for message, wall, monotonic in buffered:
            self._accept(message, wall, monotonic)

    def feed(self, message, now=None):
        if self._closed or self.error:
            return
        if isinstance(message, bytes):
            return
        if isinstance(message, str):
            try:
                message = json.loads(message, parse_constant=_reject_constant)
            except (ValueError, TypeError):
                self.disconnect("invalid_websocket_json")
                return
        if not isinstance(message, dict) or message.get("type") not in EVENT_TYPES:
            return
        data = message.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("prompt_id"), str) or not data["prompt_id"]:
            self._ignored += 1
            return
        wall, monotonic = _clock(now)
        if self.prompt_id is None:
            if len(self._buffer) >= MAX_BUFFERED_EVENTS:
                self.disconnect("prebind_event_buffer_overflow")
                return
            self._buffer.append((copy.deepcopy(message), wall, monotonic))
            return
        self._accept(message, wall, monotonic)

    def _finish_segment(self, wall, monotonic, reason):
        if self._active is None:
            return
        self._active.update(finished_at=wall, seconds_observed=max(0, monotonic - self._active["_start_mono"]),
                            finish_reason=reason)
        self._sampler_finished_at = wall
        self._active = None

    def _accept(self, message, wall, monotonic):
        data = message["data"]
        if data["prompt_id"] != self.prompt_id:
            self._ignored += 1
            return
        if self.error:
            return
        if self._last_received is not None and monotonic < self._last_received:
            self.disconnect("event_clock_regressed")
            return
        self._last_received = monotonic
        if len(self._events) >= MAX_EVENTS:
            self.disconnect("owned_event_buffer_overflow")
            return
        kind = message["type"]
        summary = {"type": kind, "prompt_id": self.prompt_id, "received_at": wall,
                   "received_monotonic": monotonic,
                   "data": {key: copy.deepcopy(data[key]) for key in
                            ("node", "nodes", "value", "max", "timestamp", "exception_type", "exception_message") if key in data}}
        try:
            json.dumps(summary, allow_nan=False)
        except (ValueError, TypeError):
            self.disconnect("invalid_event_payload")
            return
        self._events.append(summary)
        if kind == "execution_cached":
            nodes = data.get("nodes")
            if not isinstance(nodes, list) or any(not isinstance(node, str) for node in nodes):
                self.disconnect("invalid_cached_node_list")
            elif self.sampler_nodes.intersection(nodes):
                self.cached = True
                self._finish_segment(wall, monotonic, "cached_sampler")
            return
        if kind in {"execution_error", "execution_interrupted"}:
            self._terminal = True
            self._finish_segment(wall, monotonic, kind)
            self.error = kind
            return
        if self._terminal:
            return
        if kind == "execution_start":
            if self._execution_started or self._segments:
                self.disconnect("duplicate_or_late_execution_start")
            self._execution_started = True
        elif kind == "execution_success":
            self._terminal = True
            self._finish_segment(wall, monotonic, "execution_success")
            self._node = None
        elif kind == "executing":
            node = data.get("node")
            if node is not None and not isinstance(node, str):
                self.disconnect("invalid_executing_node")
                return
            if node != self._node:
                self._finish_segment(wall, monotonic, "node_changed")
            self._node = node
        elif kind == "progress":
            node = data.get("node", self._node)
            if not isinstance(node, str) or node not in self.sampler_nodes or self.cached:
                return
            value, maximum = data.get("value"), data.get("max")
            if type(value) is not int or type(maximum) is not int or not 0 <= value <= maximum or maximum <= 0:
                self.disconnect("invalid_sampler_progress")
                return
            if value == 0:
                if self._active is not None and self._active["node"] == node:
                    self.disconnect("sampler_progress_regressed_or_max_changed")
                return
            if self._active is not None and self._active["node"] != node:
                self._finish_segment(wall, monotonic, "sampler_changed")
            if self._active is None:
                if any(segment["node"] == node for segment in self._segments):
                    self.disconnect("sampler_node_reentered")
                    return
                self._active = {"node": node, "started_at": wall, "last_progress_at": wall,
                                "finished_at": None, "seconds_observed": 0, "progress_events": 0,
                                "value": 0, "max": maximum, "_start_mono": monotonic, "_last_mono": monotonic}
                self._segments.append(self._active)
                self._sampler_finished_at = None
            active = self._active
            if maximum != active["max"] or value < active["value"]:
                self.disconnect("sampler_progress_regressed_or_max_changed")
                return
            if value == active["value"]:
                summary["duplicate_progress"] = True
                return
            self._node = node
            active.update(value=value, last_progress_at=wall, _last_mono=monotonic,
                          progress_events=active["progress_events"] + 1,
                          seconds_observed=max(0, monotonic - active["_start_mono"]))
            self._last_progress_at = wall
            summary["confirmed_sampler_progress"] = True
            summary.update(node=node, value=value, max=maximum)
            if value == maximum:
                self._finish_segment(wall, monotonic, "sampler_progress_complete")

    def receipt(self, now=None):
        wall, monotonic = _clock(now)
        reasons = []
        if not self.connected or self.error:
            reasons.append(self.error or ("listener_closed" if self._closed else "sampler_websocket_unavailable"))
        if self.prompt_id is None:
            reasons.append("upstream_prompt_not_bound")
        if self.cached:
            reasons.append("sampler_cached")
        active_seconds = 0
        if self._active is None or self._terminal:
            reasons.append("no_active_owned_sampler_progress")
        else:
            active_seconds = monotonic - self._active["_start_mono"]
            if monotonic < self._last_received:
                reasons.append("receipt_clock_regressed")
            if active_seconds < 60:
                reasons.append("awaiting_60s_owned_sampler_observation")
        intervals = [{key: copy.deepcopy(value) for key, value in segment.items() if not key.startswith("_")}
                     for segment in self._segments]
        if self._active is not None:
            intervals[-1]["seconds_observed"] = max(0, active_seconds)
        return {"ready": not reasons, "reasons": reasons, "prompt_id": self.prompt_id,
                "client_id": self.client_id, "connected": self.connected, "closed": self._closed, "error": self.error,
                "observed_at": wall, "sampler_started_at": intervals[0]["started_at"] if intervals else None,
                "last_progress_at": self._last_progress_at, "sampler_finished_at": self._sampler_finished_at,
                "cached": self.cached, "sampler_progress_events": copy.deepcopy([
                    event for event in self._events if event.get("confirmed_sampler_progress")]),
                "events": copy.deepcopy(self._events),
                "sampler_seconds_observed": sum(segment["seconds_observed"] for segment in intervals),
                "continuous_sampler_seconds": max(0, active_seconds), "sampler_intervals": intervals,
                "ignored_unowned_events": self._ignored, "buffered_events": len(self._buffer),
                "measurement": "owned sampler event intervals; not GPU kernel overlap"}
