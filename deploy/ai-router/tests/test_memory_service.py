import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pytest

from ai_router.client_accounts import ClientAccountManager
from ai_router.memory_service import HistoryMemory
from ai_router.store import InMemoryStateStore
from ai_router.training_archive import TrainingArchive


@pytest.fixture
def runtime(tmp_path):
    key = tmp_path / "archive.key"
    key.write_bytes(Fernet.generate_key())
    store = InMemoryStateStore()
    settings = SimpleNamespace(section=lambda name: {}, runtime_path=tmp_path / "runtime.yaml")
    state_key = Fernet.generate_key().decode()
    return SimpleNamespace(training=TrainingArchive(str(tmp_path / "archive.sqlite3"), str(key)),
        clients=ClientAccountManager(store, settings, state_key), store=store,
        settings=settings, state_encryption_key=state_key, audit=Mock())


async def prepare(runtime):
    await runtime.clients.create_account(dict(id="alice", name="Alice", models=["siyuan/auto"],
        rpm_limit=10, tpm_limit=10000, max_parallel_requests=1,
        history_owner_confirmed=True, history_recall_enabled=True), allowed_models={"siyuan/auto"})
    return await runtime.training.begin(request_id="request", conversation_id="old-chat",
        conversation_mode="stateful", client_id="alice", key_id="key", protocol="chat",
        received_body={"messages": [{"role": "user", "content": "部署参数 E_MEMORY_782 保留原文"}]},
        instance_id="test", boot_id="test", history_source_local_only=False)


def test_restart_resumes_cursor_and_indexes_late_answer(runtime):
    async def scenario():
        token = await prepare(runtime)
        first = HistoryMemory(runtime)
        result = await first.index_page("alice")
        assert result["inserted_chunks"] == 1
        resumed = HistoryMemory(runtime)
        assert (await resumed.index_page("alice"))["state"] == "caught_up"
        await runtime.training.complete(token, status_code=200,
            response_payload=b'{"choices":[{"message":{"role":"assistant","content":"E_ANSWER_456 final answer"}}]}')
        result = await resumed.index_page("alice")
        assert result["inserted_chunks"] == 1
        assert len(resumed.index.search("alice", "E_ANSWER_456", cloud=False)) == 1
        assert resumed.index.status("alice")["chunks"] == 2
    asyncio.run(scenario())


def test_disabled_account_does_not_open_index_or_advance(runtime):
    async def scenario():
        await prepare(runtime)
        await runtime.clients.update_account("alice", {"history_recall_enabled": False},
                                             allowed_models={"siyuan/auto"})
        service = HistoryMemory(runtime)
        assert await runtime.clients.history_accounts() == []
        assert await service.index_page("alice") == {"state": "disabled"}
        assert service.index is None
        assert not runtime.training.database_path.with_name("history-memory.sqlite3").exists()
    asyncio.run(scenario())


def test_failed_insert_does_not_advance_cursor_and_releases_worker(runtime, monkeypatch):
    async def scenario():
        await prepare(runtime)
        service = HistoryMemory(runtime)
        service._open()
        original = service.index.add
        def fail(sources):
            raise OSError("synthetic disk error")
        monkeypatch.setattr(service.index, "add", fail)
        with pytest.raises(OSError):
            await service.index_page("alice")
        assert service.index.status("alice")["archive_cursor"] == 0
        monkeypatch.setattr(service.index, "add", original)
        assert (await service.index_page("alice"))["inserted_chunks"] == 1
    asyncio.run(scenario())


def test_other_worker_lease_prevents_index_open(runtime):
    async def scenario():
        await prepare(runtime)
        assert await runtime.store.acquire_lock("router:history-memory:index-worker", "other", 120)
        service = HistoryMemory(runtime)
        assert await service.index_page("alice") == {"state": "busy"}
        assert service.index is None
    asyncio.run(scenario())


def test_recall_reads_existing_index_without_owning_ingestion_lease(runtime):
    import copy
    import json
    from unittest.mock import AsyncMock
    from ai_router.memory_recall import prepare_recall
    from ai_router.scheduler import ClientLimiter

    async def scenario():
        await prepare(runtime)
        writer = HistoryMemory(runtime)
        await writer.index_page("alice")
        assert await runtime.store.acquire_lock("router:history-memory:index-worker", "other", 120)
        runtime.history_memory = HistoryMemory(runtime)
        assert await runtime.history_memory.index_page("alice") == {"state": "busy"}
        assert runtime.history_memory.index is None
        runtime.clients.history_policy = AsyncMock(return_value=SimpleNamespace(tpm_limit=100000))
        runtime.token_counter = SimpleNamespace(count_request=lambda body, kind: len(json.dumps(body)))
        runtime.endpoint_token_counter = SimpleNamespace(count=AsyncMock(return_value={"tokens": 1000}))
        runtime.limiter = ClientLimiter(runtime.store)
        decision = SimpleNamespace(endpoint=SimpleNamespace(cloud=False, safe_context_tokens=32000),
            deployment_safe_context_tokens=None, prompt_tokens=100, output_reserve_tokens=100)
        projection, reason = await prepare_recall(runtime,
            {"messages": [{"role": "user", "content": "E_MEMORY_782 部署参数是什么？"}]},
            api_kind="chat", decision=decision,
            identity=SimpleNamespace(inject=lambda body, kind: copy.deepcopy(body)),
            client_id="alice", key_id="key")
        assert reason == "prepared" and projection is not None
        assert projection.sources[0].source.request_id == "request"
        assert await runtime.store.renew_lock("router:history-memory:index-worker", "other", 120)
        assert runtime.history_memory.index.status("alice")["chunks"] == 1

    asyncio.run(scenario())


def test_concurrent_readers_initialize_index_once(runtime, monkeypatch):
    async def scenario():
        service = HistoryMemory(runtime)
        opening = Mock(wraps=service._open)
        monkeypatch.setattr(service, "_open", opening)
        await asyncio.gather(*(service.ensure_open() for _ in range(8)))
        opening.assert_called_once()
        assert service.index is not None
    asyncio.run(scenario())


@pytest.mark.parametrize("late_error", [False, True])
@pytest.mark.parametrize("cancel_count", [1, 2])
def test_worker_shutdown_preserves_cancel_until_sqlite_finishes(runtime, monkeypatch, late_error, cancel_count):
    import threading
    from ai_router.memory_service import _thread

    async def scenario():
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def write():
            entered.set()
            try:
                assert release.wait(3)
                if late_error:
                    raise OSError("synthetic write failure after cancellation")
                return {"state": "caught_up"}
            finally:
                finished.set()
        async def cycle(**kwargs):
            return await _thread(write)
        service = HistoryMemory(runtime)
        monkeypatch.setattr(service, "index_cycle", cycle)
        task = asyncio.create_task(service.run())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done() and not finished.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 1)
            assert finished.is_set()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_worker_cancellation_waits_for_inflight_sqlite_write(runtime, monkeypatch):
    import threading
    async def scenario():
        await prepare(runtime)
        service = HistoryMemory(runtime)
        service._open()
        entered, release = threading.Event(), threading.Event()
        original = service.index.add
        def paused(sources):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release write")
            return original(sources)
        monkeypatch.setattr(service.index, "add", paused)
        task = asyncio.create_task(service.index_page("alice"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert not await runtime.store.acquire_lock("router:history-memory:index-worker", "other", 120)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert service.index.status("alice")["archive_cursor"] == 0
        monkeypatch.setattr(service.index, "add", original)
        result = await service.index_page("alice")
        assert result["state"] == "indexing" and result["inserted_chunks"] == 0
        assert service.index.status("alice")["chunks"] == 1
    asyncio.run(scenario())


def test_revoked_during_archive_read_does_not_index(runtime, monkeypatch):
    async def scenario():
        await prepare(runtime)
        service = HistoryMemory(runtime)
        service._open()
        original = runtime.clients.history_account_policy
        calls = 0
        async def revoked(client_id):
            nonlocal calls
            calls += 1
            return await original(client_id) if calls == 1 else None
        monkeypatch.setattr(runtime.clients, "history_account_policy", revoked)
        assert await service.index_page("alice") == {"state": "disabled"}
        assert service.index.status("alice")["archive_cursor"] == 0
        assert service.index.status("alice")["chunks"] == 0
    asyncio.run(scenario())


def test_slow_write_renews_lease_until_cancel_cleanup_finishes(runtime, monkeypatch):
    import threading
    async def scenario():
        await prepare(runtime)
        service = HistoryMemory(runtime)
        service.renewal_seconds = 0.005
        service._open()
        entered, release = threading.Event(), threading.Event()
        renewed = asyncio.Event()
        original_add, original_renew = service.index.add, runtime.store.renew_lock
        async def renew(*args):
            result = await original_renew(*args)
            if entered.is_set():
                renewed.set()
            return result
        def paused(sources):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release write")
            return original_add(sources)
        monkeypatch.setattr(runtime.store, "renew_lock", renew)
        monkeypatch.setattr(service.index, "add", paused)
        task = asyncio.create_task(service.index_page("alice"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.wait_for(renewed.wait(), 1)
            assert not task.done()
            assert not await runtime.store.acquire_lock("router:history-memory:index-worker", "other", 120)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert service.index.status("alice")["archive_cursor"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["replaced", "unavailable"])
def test_lease_loss_stops_extraction_and_does_not_checkpoint(runtime, monkeypatch, failure):
    import threading
    async def scenario():
        await prepare(runtime)
        service = HistoryMemory(runtime)
        service.renewal_seconds = 0.005
        service._open()
        entered, release = threading.Event(), threading.Event()
        failed = asyncio.Event()
        original_add, original_renew = service.index.add, runtime.store.renew_lock
        async def renew(*args):
            if entered.is_set():
                failed.set()
                if failure == "unavailable":
                    raise OSError("synthetic Redis disconnect")
                await runtime.store.release_lock(args[0], args[1])
                assert await runtime.store.acquire_lock(args[0], "replacement", 120)
                return False
            return await original_renew(*args)
        def paused(sources):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release write")
            return original_add(sources)
        monkeypatch.setattr(runtime.store, "renew_lock", renew)
        monkeypatch.setattr(service.index, "add", paused)
        task = asyncio.create_task(service.index_page("alice"))
        try:
            await asyncio.wait_for(failed.wait(), 1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="lease lost"):
            await task
        assert service.index.status("alice")["archive_cursor"] == 0
        assert service.index.status("alice")["chunks"] == 0
        if failure == "replaced":
            assert not await runtime.store.acquire_lock("router:history-memory:index-worker", "third", 120)
    asyncio.run(scenario())


def test_lock_renewal_cannot_revive_expired_or_replace_other_owner(monkeypatch):
    async def scenario():
        now = 1000.0
        monkeypatch.setattr("ai_router.store.time.time", lambda: now)
        store = InMemoryStateStore()
        assert await store.acquire_lock("lease", "first", 10)
        now += 8
        assert await store.renew_lock("lease", "first", 10)
        now += 8
        assert not await store.acquire_lock("lease", "second", 10)
        now += 3
        assert not await store.renew_lock("lease", "first", 10)
        assert await store.acquire_lock("lease", "second", 10)
        assert not await store.renew_lock("lease", "first", 10)
        await store.release_lock("lease", "first")
        assert not await store.acquire_lock("lease", "third", 10)
    asyncio.run(scenario())
