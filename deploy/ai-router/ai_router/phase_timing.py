"""Request-scoped, metadata-only timing for the Router's critical path."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time


_current = ContextVar("router_phase_timings", default=None)


def current_timings():
    return _current.get()


@contextmanager
def phase(name):
    timings = _current.get()
    if timings is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        # Observability must never replace the original result or exception.
        try:
            elapsed = max(0.0, (time.monotonic() - started) * 1000)
            entry = timings["stages"].setdefault(name, {"calls": 0, "total_ms": 0.0, "max_ms": 0.0})
            entry["calls"] += 1
            entry["total_ms"] = round(entry["total_ms"] + elapsed, 3)
            entry["max_ms"] = round(max(entry["max_ms"], elapsed), 3)
        except Exception:
            pass


def timed(name):
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with phase(name):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def timed_async(name):
    def decorate(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            with phase(name):
                return await fn(*args, **kwargs)
        return wrapper
    return decorate


class PhaseTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") not in ("/v1/chat/completions", "/v1/responses"):
            return await self.app(scope, receive, send)
        from .compute import token_cache
        cache_token = token_cache.set({})
        token = _current.set({"version": 1, "stages": {}})
        try:
            return await self.app(scope, receive, send)
        finally:
            _current.reset(token)
            token_cache.reset(cache_token)
