"""Process-local handoff for fixed-route archive events.

The durable contract remains the existing encrypted Redis outbox. This layer
only moves its serialization, encryption, and Redis admission off the request
path. It is opt-in and falls back to the delegate synchronously when local
admission is unavailable.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class FrozenBody:
    sha256: str
    raw: bytes

    def materialize(self) -> Any:
        return json.loads(self.raw)


@dataclass
class _Event:
    event_id: str
    operation: str
    token: str
    kwargs: dict[str, Any]
    body_digests: tuple[str, ...]
    completed: asyncio.Future[None]


def _materialize(value: Any) -> Any:
    if isinstance(value, FrozenBody):
        return value.materialize()
    if isinstance(value, dict):
        return {key: _materialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_materialize(item) for item in value)
    return value


def _frozen_bodies(value: Any) -> tuple[FrozenBody, ...]:
    found: dict[str, FrozenBody] = {}

    def visit(item: Any) -> None:
        if isinstance(item, FrozenBody):
            found.setdefault(item.sha256, item)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found.values())


class DirectedArchive:
    def __init__(
        self,
        delegate: Any,
        *,
        max_events: int = 64,
        max_body_bytes: int = 128 * 1024**2,
        flush_timeout_seconds: float = 120,
    ) -> None:
        self.delegate = delegate
        self.max_events = max_events
        self.max_body_bytes = max_body_bytes
        self.flush_timeout_seconds = flush_timeout_seconds
        self._queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=max_events)
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._healthy = True
        self._pending_begin: dict[str, dict[str, Any]] = {}
        self._modes: dict[str, str] = {}
        self._mode_observers: dict[str, Any] = {}
        self._reported_modes: dict[str, str] = {}
        self._tails: dict[str, asyncio.Future[None]] = {}
        self._body_refs: dict[str, list[int]] = {}
        self._pending_body_bytes = 0
        self._active_events = 0
        self.sync_fallbacks = 0
        self.worker_retries = 0
        self.flush_failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def snapshot_body(self, observation: Any, body: dict[str, Any]) -> FrozenBody:
        return observation.frozen(body)

    async def begin(self, **kwargs: Any) -> str:
        token = self.delegate._digest(f"request:{kwargs['request_id']}")
        self._pending_begin[token] = copy.deepcopy(kwargs)
        return token

    async def select_mode(
        self,
        token: str | None,
        *,
        directed: bool,
        mode_observer=None,
    ) -> str:
        if not token or token in self._modes:
            return self._reported_modes.get(token or "", "existing_sync")
        if mode_observer is not None:
            self._mode_observers[token] = mode_observer
        self._modes[token] = "async" if directed else "sync"
        await self._notify_mode(
            token,
            "process_local" if directed else "existing_sync",
        )
        kwargs = self._pending_begin.pop(token)
        await self._submit("begin", token, kwargs)
        return self._reported_modes.get(token, "existing_sync")

    async def mark_routed(self, token: str | None, **kwargs: Any) -> None:
        await self._submit("mark_routed", token, kwargs)

    async def set_effective_context(self, token: str | None, **kwargs: Any) -> None:
        await self._submit("set_effective_context", token, kwargs)

    async def record_pipeline(self, token: str | None, pipeline: Any) -> None:
        await self._submit("record_pipeline", token, {"pipeline": pipeline})

    async def complete(self, token: str | None, **kwargs: Any) -> None:
        await self._submit("complete", token, kwargs)

    async def fail(self, token: str | None, **kwargs: Any) -> None:
        await self._submit("fail", token, kwargs)

    async def publish_history(self, trace: dict[str, Any]) -> None:
        token = self.delegate._digest(f"request:{trace['request_id']}")
        await self._submit("publish_history", token, {"trace": trace})

    async def _submit(self, operation: str, token: str | None, kwargs: dict[str, Any]) -> None:
        if not token:
            return
        if token not in self._modes:
            self._modes[token] = "sync"
            pending = self._pending_begin.pop(token, None)
            if pending is not None:
                await self._call_delegate("begin", pending)
        if self._modes[token] != "async":
            await self._call_delegate(operation, kwargs, token=token)
            if operation in {"complete", "fail", "publish_history"}:
                self._forget_token(token)
            return
        # Large bodies arrive as immutable FrozenBody objects. Copy the small
        # metadata envelope so later caller mutation cannot alter the event.
        kwargs = copy.deepcopy(kwargs)
        bodies = _frozen_bodies(kwargs)
        new_bytes = sum(
            len(body.raw) for body in bodies if body.sha256 not in self._body_refs
        )
        if (
            self._closing
            or not self._healthy
            or self._active_events + self._queue.qsize() >= self.max_events
            or self._pending_body_bytes + new_bytes > self.max_body_bytes
        ):
            await self._fallback(token, operation, kwargs)
            return
        completed = asyncio.get_running_loop().create_future()
        completed.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        # This ID is reused if Redis accepts the write but its reply is lost.
        # QueuedTrainingArchive deduplicates it atomically per request token.
        event = _Event(
            uuid4().hex,
            operation,
            token,
            kwargs,
            tuple(body.sha256 for body in bodies),
            completed,
        )
        for body in bodies:
            reference = self._body_refs.get(body.sha256)
            if reference is None:
                self._body_refs[body.sha256] = [len(body.raw), 1]
                self._pending_body_bytes += len(body.raw)
            else:
                reference[1] += 1
        self._tails[token] = completed
        self._queue.put_nowait(event)
        self._ensure_worker()

    async def _fallback(self, token: str, operation: str, kwargs: dict[str, Any]) -> None:
        self.sync_fallbacks += 1
        tail = self._tails.get(token)
        if tail is not None:
            await asyncio.shield(tail)
        self._modes[token] = "sync"
        await self._notify_mode(token, "existing_sync")
        await self._call_delegate(operation, kwargs, token=token)
        if operation in {"complete", "fail", "publish_history"}:
            self._forget_token(token)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._healthy = True
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._closing or not self._queue.empty():
                event = await self._queue.get()
                self._active_events += 1
                succeeded = False
                try:
                    retry_delay = 0.05
                    retry_reported = False
                    while True:
                        try:
                            await self._call_delegate(
                                event.operation,
                                event.kwargs,
                                token=event.token,
                                event_id=event.event_id,
                            )
                        except asyncio.CancelledError:
                            raise
                        except BaseException:
                            # The Redis result may be unknown. Retain the event
                            # and retry its stable ID; never advance this token
                            # past an unconfirmed transition.
                            self._healthy = False
                            self.worker_retries += 1
                            if not retry_reported:
                                await self._notify_mode(
                                    event.token,
                                    "process_local_retry",
                                )
                                retry_reported = True
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(1.0, retry_delay * 2)
                            continue
                        break
                except asyncio.CancelledError:
                    if not event.completed.done():
                        event.completed.set_exception(
                            RuntimeError("archive flush was cancelled")
                        )
                    raise
                else:
                    succeeded = True
                    self._healthy = True
                    if not event.completed.done():
                        event.completed.set_result(None)
                finally:
                    self._active_events -= 1
                    self._release_bodies(event.body_digests)
                    self._queue.task_done()
                    if succeeded and self._tails.get(event.token) is event.completed:
                        self._tails.pop(event.token, None)
                        if event.operation in {"complete", "fail", "publish_history"}:
                            self._forget_token(event.token)
        except asyncio.CancelledError:
            self._healthy = False
            raise
        except BaseException:
            self._healthy = False

    def _release_bodies(self, digests: tuple[str, ...]) -> None:
        for digest in digests:
            reference = self._body_refs[digest]
            reference[1] -= 1
            if reference[1] == 0:
                self._pending_body_bytes -= reference[0]
                del self._body_refs[digest]

    async def _call_delegate(
        self,
        operation: str,
        kwargs: dict[str, Any],
        *,
        token: str | None = None,
        event_id: str | None = None,
    ) -> Any:
        materialized = _materialize(kwargs)
        enqueue_event = getattr(self.delegate, "enqueue_event", None)
        if event_id is not None and callable(enqueue_event):
            return await enqueue_event(
                operation,
                token,
                materialized,
                event_id=event_id,
            )
        method = getattr(self.delegate, operation)
        if operation == "begin":
            return await method(**materialized)
        if operation == "record_pipeline":
            return await method(token, materialized["pipeline"])
        if operation == "publish_history":
            return await method(materialized["trace"])
        return await method(token, **materialized)

    async def _notify_mode(self, token: str, mode: str) -> None:
        if self._reported_modes.get(token) == mode:
            return
        self._reported_modes[token] = mode
        observer = self._mode_observers.get(token)
        if observer is not None:
            try:
                result = observer(mode)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                # Observability must not break archive ordering or recovery.
                pass

    def _forget_token(self, token: str) -> None:
        self._modes.pop(token, None)
        self._mode_observers.pop(token, None)
        self._reported_modes.pop(token, None)

    def background_status(self) -> dict[str, Any]:
        return {
            "mode": "directed_async",
            "pending_events": self._active_events + self._queue.qsize(),
            "pending_body_bytes": self._pending_body_bytes,
            "max_events": self.max_events,
            "max_body_bytes": self.max_body_bytes,
            "worker_healthy": self._healthy,
            "sync_fallbacks": self.sync_fallbacks,
            "worker_retries": self.worker_retries,
            "flush_failures": self.flush_failures,
            "closing": self._closing,
        }

    async def status(self) -> dict[str, Any]:
        result = await self.delegate.status()
        result["directed_background"] = self.background_status()
        return result

    async def aclose(self) -> None:
        self._closing = True
        flush_error = None
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self.flush_timeout_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                self.flush_failures += 1
                flush_error = RuntimeError(
                    "directed archive did not flush before shutdown deadline"
                )
            if not self._worker.done():
                self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if flush_error is not None:
            while not self._queue.empty():
                event = self._queue.get_nowait()
                if not event.completed.done():
                    event.completed.set_exception(flush_error)
                self._release_bodies(event.body_digests)
                self._queue.task_done()
            for token in tuple(self._modes):
                await self._notify_mode(token, "flush_failed")
            self._pending_begin.clear()
            self._tails.clear()
            self._modes.clear()
            self._mode_observers.clear()
            self._reported_modes.clear()
        close = getattr(self.delegate, "aclose", None)
        if close is not None:
            await close()
        else:
            self.delegate.close()
        if flush_error is not None:
            raise flush_error
