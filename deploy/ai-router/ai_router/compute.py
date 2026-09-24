"""Bounded off-loop work; cancellation never frees a still-running slot."""
import asyncio
import contextvars
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .phase_timing import phase


token_cache = contextvars.ContextVar("request_token_cache", default=None)


class BoundedExecutor:
    def __init__(self, workers=2, name="router-compute"):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.slots = asyncio.Semaphore(workers)

    async def run(self, function, *args, **kwargs):
        await self.slots.acquire()
        try:
            context = contextvars.copy_context()
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, context.run, partial(function, *args, **kwargs)
            )
        except BaseException:
            self.slots.release()
            raise
        # shield keeps the executor Future alive until the thread really stops.
        def finished(task):
            self.slots.release()
            if not task.cancelled():
                task.exception()
        future.add_done_callback(finished)
        return await asyncio.shield(future)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


async def compute(current, function, *args, **kwargs):
    pool = getattr(current, "compute_executor", None)
    if not isinstance(pool, BoundedExecutor):
        pool = current.compute_executor = BoundedExecutor()
    return await pool.run(function, *args, **kwargs)


async def count_tokens(current, body, api_kind):
    async def calculate():
        with phase("token_count_offloop"):
            return await compute(current, current.token_counter.count_request, body, api_kind)

    cache = token_cache.get()
    if cache is None:
        return await calculate()
    # The key preserves all input fields and insertion order: template rendering
    # can depend on tool order, and changed histories must never share a count.
    key = (id(current.token_counter), api_kind, hashlib.sha256(
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    ).digest())
    task = cache.get(key)
    if task is not None:
        with phase("token_count_cache_hit"):
            return await asyncio.shield(task)
    if len(cache) >= 32:
        return await calculate()
    task = asyncio.create_task(calculate())
    cache[key] = task

    def completed(future):
        # Observe failures even if all waiters were cancelled. Failed work is
        # never reusable, and cancellation does not release a running CPU slot.
        if future.cancelled() or future.exception() is not None:
            if cache.get(key) is future:
                cache.pop(key, None)
    task.add_done_callback(completed)
    return await asyncio.shield(task)
