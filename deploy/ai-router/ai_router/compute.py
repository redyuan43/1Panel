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
    def count():
        cache = token_cache.get()
        key = None
        if cache is not None:
            # Preserve message/tool ordering and every tokenizer input field.
            key = (id(current.token_counter), api_kind, hashlib.sha256(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            ).digest())
            if key in cache:
                return cache[key]
        result = current.token_counter.count_request(body, api_kind)
        if cache is not None and key is not None and len(cache) < 32:
            cache[key] = result
        return result
    with phase("token_count_offloop"):
        return await compute(current, count)
