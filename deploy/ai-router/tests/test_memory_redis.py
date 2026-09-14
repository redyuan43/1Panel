"""Opt-in real Redis contracts; use only a disposable, isolated instance."""
import asyncio
import os
from uuid import uuid4

import pytest

from ai_router.store import RedisStateStore
from test_memory_service import runtime, prepare


@pytest.mark.skipif(not os.environ.get("HISTORY_TEST_REDIS"), reason="requires disposable Redis")
def test_real_redis_lease_renewal_expiry_and_owner_fencing():
    async def scenario():
        stores = [RedisStateStore(os.environ["HISTORY_TEST_REDIS"]) for _ in range(2)]
        key = "test:history-lease:" + uuid4().hex
        try:
            first, second = stores
            assert await first.acquire_lock(key, "first", 1)
            assert not await second.renew_lock(key, "second", 60)
            assert await first.renew_lock(key, "first", 5)
            await asyncio.sleep(1.1)
            assert not await second.acquire_lock(key, "second", 1)
            await first.release_lock(key, "first")
            assert await second.acquire_lock(key, "second", 1)
            assert not await first.renew_lock(key, "first", 60)
            await first.release_lock(key, "first")
            assert not await first.acquire_lock(key, "first", 1)
            await asyncio.sleep(1.1)
            assert not await second.renew_lock(key, "second", 60)
            assert await first.acquire_lock(key, "first", 1)
        finally:
            for store in stores:
                try:
                    await store.release_lock(key, "first")
                    await store.release_lock(key, "second")
                finally:
                    await store.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("HISTORY_TEST_REDIS"), reason="requires disposable Redis")
def test_index_instances_share_real_redis_lease_and_resume(runtime, monkeypatch):
    import threading
    from copy import copy
    from ai_router.memory_service import HistoryMemory
    async def scenario():
        await prepare(runtime)
        stores = [RedisStateStore(os.environ["HISTORY_TEST_REDIS"]) for _ in range(2)]
        other = copy(runtime)
        runtime.store, other.store = stores
        first, second = HistoryMemory(runtime), HistoryMemory(other)
        first.renewal_seconds = 0.01
        first._open()
        entered, release = threading.Event(), threading.Event()
        original = first.index.add
        def paused(sources):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release SQLite write")
            return original(sources)
        monkeypatch.setattr(first.index, "add", paused)
        task = asyncio.create_task(first.index_page("alice"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert await second.index_page("alice") == {"state": "busy"}
            assert second.index is None
            release.set()
            assert (await task)["inserted_chunks"] == 1
            assert (await second.index_page("alice"))["state"] == "caught_up"
            assert second.index.status("alice")["chunks"] == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            for store in stores:
                await store.close()
    asyncio.run(scenario())
