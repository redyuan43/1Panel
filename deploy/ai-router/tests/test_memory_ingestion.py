"""Shared reads, independent live progress, bounded work and crash recovery."""
import asyncio
from unittest.mock import Mock

import pytest

from ai_router.memory_ingestion import IngestionProgress, indexing_options
from ai_router.memory_service import HistoryMemory
from test_memory_service import runtime


async def owner(runtime, name):
    await runtime.clients.create_account(dict(id=name, name=name, models=["siyuan/auto"],
        rpm_limit=10, tpm_limit=10000, max_parallel_requests=1), allowed_models={"siyuan/auto"})


async def record(runtime, name, number):
    return await runtime.training.begin(request_id=f"request-{name}-{number}",
        conversation_id=f"chat-{name}-{number}", conversation_mode="stateful", client_id=name,
        key_id="key", protocol="chat", received_body={"messages": [{"role": "user",
        "content": f"EVIDENCE_{name}_{number} final port 7241"}]},
        instance_id="test", boot_id="test", history_source_local_only=False)


def options(runtime, **changes):
    values = indexing_options(changes)
    runtime.settings.section = lambda name: {"history_indexing": values} if name == "compaction" else {}
    return values


def test_sixteen_accounts_share_reads_and_idle_does_not_decrypt(runtime, monkeypatch):
    async def scenario():
        names = [f"owner-{i:02d}" for i in range(16)]
        for name in names:
            await owner(runtime, name)
            await record(runtime, name, 1)
        options(runtime, batch_records=32, max_batch_seconds=2)
        service = HistoryMemory(runtime)
        service._open()
        read = Mock(wraps=service.reader.history_event)
        monkeypatch.setattr(service.reader, "history_event", read)
        result = await service.index_cycle()
        assert result["read_events"] == read.call_count == 16
        assert result["inserted_chunks"] == 16
        for name in names:
            assert service.index.status(name)["chunks"] == 1
            assert service.index.status(name)["archive_cursor"] == 16
            if name != names[0]:
                assert service.index.search(name, "EVIDENCE_owner-00_1", cloud=False) == []
        assert (await service.index_cycle())["state"] == "caught_up"
        assert read.call_count == 16
        await record(runtime, names[0], 2)
        assert (await service.index_cycle())["read_events"] == 1
        assert read.call_count == 17
    asyncio.run(scenario())


def test_new_record_and_late_completion_bypass_backlog_then_survive_restart(runtime):
    async def scenario():
        await owner(runtime, "alice")
        first = await record(runtime, "alice", 0)
        for i in range(1, 20):
            await record(runtime, "alice", i)
        options(runtime, batch_records=1)
        service = HistoryMemory(runtime)
        await service.index_cycle()
        assert service.index.status("alice")["archive_cursor"] == 1
        await record(runtime, "alice", "new")
        await runtime.training.complete(first, status_code=200,
            response_payload=b'{"choices":[{"message":{"role":"assistant","content":"LATE_ANSWER final answer"}}]}')
        resumed = HistoryMemory(runtime)
        await resumed.index_cycle()
        assert resumed.index.search("alice", "EVIDENCE_alice_new", cloud=False)
        assert resumed.index.status("alice")["archive_cursor"] == 2
        await resumed.index_cycle()
        assert resumed.index.search("alice", "LATE_ANSWER", cloud=False)
        progress = resumed.index.status("alice")["progress"]
        assert progress["live_cursor"] == 22
        assert progress["backfill_cursor"] == 3
        assert progress["remaining_events"] == 17
        for _ in range(17):
            await resumed.index_cycle()
        assert resumed.index.status("alice")["archive_cursor"] == 22
        assert resumed.index.status("alice")["progress"]["caught_up"]
    asyncio.run(scenario())


def test_old_cursor_migration_and_new_account_backfill_without_reinserting_other_owner(runtime):
    async def scenario():
        await owner(runtime, "alice")
        for i in range(3):
            await record(runtime, "alice", i)
            await record(runtime, "bob", i)
        options(runtime, batch_records=32, max_batch_seconds=2)
        service = HistoryMemory(runtime)
        service._open()
        service.index.checkpoint("alice", 2)
        await service.index_cycle()
        assert service.index.status("alice")["chunks"] == 2  # imported prefix is not replayed
        await owner(runtime, "bob")
        await service.index_cycle()
        assert service.index.status("bob")["chunks"] == 3
        assert service.index.status("alice")["chunks"] == 2
        assert service.index.status("bob")["archive_cursor"] == 6
    asyncio.run(scenario())


def test_pause_preserves_index_and_resume_processes_only_pending_changes(runtime, monkeypatch):
    async def scenario():
        await owner(runtime, "alice")
        await record(runtime, "alice", 1)
        config = options(runtime)
        service = HistoryMemory(runtime)
        await service.index_cycle()
        config["enabled"] = False
        await record(runtime, "alice", 2)
        read = Mock(wraps=service.reader.history_event)
        monkeypatch.setattr(service.reader, "history_event", read)
        assert await service.index_cycle() == {"state": "paused"}
        assert read.call_count == 0
        assert service.index.search("alice", "EVIDENCE_alice_1", cloud=False)
        assert service.index.status("alice")["archive_cursor"] == 1
        config["enabled"] = True
        assert (await service.index_cycle())["read_events"] == 1
        assert service.index.status("alice")["archive_cursor"] == 2
    asyncio.run(scenario())


def test_pause_file_reload_does_not_require_a_foreground_request(runtime, tmp_path):
    from ai_router.config import Settings
    async def scenario():
        await owner(runtime, "alice")
        await record(runtime, "alice", 1)
        runtime.settings = Settings(runtime_path=tmp_path / "settings.yaml")
        service = HistoryMemory(runtime)
        await service.index_cycle()
        editor = Settings(runtime_path=runtime.settings.runtime_path)
        editor.write_runtime({"compaction": {"history_indexing": {"enabled": False}}})
        assert runtime.settings.section("compaction")["history_indexing"]["enabled"] is True
        await record(runtime, "alice", 2)
        assert await service.index_cycle() == {"state": "paused"}
        editor.write_runtime({"compaction": {"history_indexing": {"enabled": True}}})
        assert (await service.index_cycle())["read_events"] == 1
    asyncio.run(scenario())


def test_slow_record_finishes_without_truncation_then_yields_batch(runtime, monkeypatch):
    import time
    async def scenario():
        await owner(runtime, "alice")
        for i in range(3):
            await record(runtime, "alice", i)
        options(runtime, batch_records=32, max_batch_seconds=0.01)
        service = HistoryMemory(runtime)
        service._open()
        original = service.reader.history_event
        def slow(*args):
            time.sleep(0.02)
            return original(*args)
        monkeypatch.setattr(service.reader, "history_event", slow)
        result = await service.index_cycle()
        assert result["read_events"] == result["inserted_chunks"] == 1
        assert service.index.status("alice")["archive_cursor"] == 1
    asyncio.run(scenario())


def test_retained_old_worker_cursor_is_adopted_on_resume(runtime):
    async def scenario():
        await owner(runtime, "alice")
        for i in range(6):
            await record(runtime, "alice", i)
        options(runtime, batch_records=1)
        service = HistoryMemory(runtime)
        await service.index_cycle()
        assert service.index.status("alice")["archive_cursor"] == 1
        # Simulate the old worker's verified contiguous checkpoint in a rollout.
        service.index.checkpoint("alice", 4)
        await service.index_cycle()
        assert service.index.status("alice")["archive_cursor"] == 5
        await record(runtime, "alice", "new")
        await service.index_cycle()
        service.index.checkpoint("alice", 6)
        assert (await service.index_cycle())["state"] == "caught_up"
        assert service.index.status("alice")["archive_cursor"] == 7
    asyncio.run(scenario())


def test_reenabled_account_gap_does_not_block_other_accounts_new_records(runtime):
    async def scenario():
        for name in ("alice", "bob"):
            await owner(runtime, name)
            for i in range(3):
                await record(runtime, name, i)
        options(runtime, batch_records=1)
        service = HistoryMemory(runtime)
        await service.index_cycle()
        await runtime.clients.update_account("bob", {"history_recall_enabled": False}, allowed_models={"siyuan/auto"})
        for i in range(4):
            await record(runtime, "alice", f"during-pause-{i}")
            await service.index_cycle()
        await runtime.clients.update_account("bob", {"history_recall_enabled": True}, allowed_models={"siyuan/auto"})
        await record(runtime, "alice", "urgent-new")
        await service.index_cycle()
        assert service.index.search("alice", "EVIDENCE_alice_urgent-new", cloud=False)
        assert service.index.status("bob")["progress"]["remaining_events"] > 0
        assert service.index.status("bob")["progress"]["estimated_remaining_seconds"] is None
        for _ in range(12):
            await service.index_cycle()
        assert service.index.status("bob")["progress"]["caught_up"]
        assert service.index.status("bob")["chunks"] == 3
    asyncio.run(scenario())


def test_new_account_registration_does_not_move_existing_live_work_into_backfill(runtime):
    async def scenario():
        await owner(runtime, "alice")
        for i in range(4):
            await record(runtime, "alice", i)
        options(runtime, batch_records=1)
        service = HistoryMemory(runtime)
        await service.index_cycle()
        await record(runtime, "alice", "pending-new")
        await owner(runtime, "bob")
        await service.index_cycle()
        assert service.index.search("alice", "EVIDENCE_alice_pending-new", cloud=False)
        assert service.index.status("alice")["progress"]["backfill_cursor"] < 4
    asyncio.run(scenario())


def test_batch_failure_replays_deduplicated_prefix_without_skipping(runtime, monkeypatch):
    async def scenario():
        await owner(runtime, "alice")
        for i in range(3):
            await record(runtime, "alice", i)
        service = HistoryMemory(runtime)
        service._open()
        original = service.reader.history_event
        def fail(cursor, through):
            if cursor == 1:
                raise ValueError("synthetic corrupt record")
            return original(cursor, through)
        monkeypatch.setattr(service.reader, "history_event", fail)
        with pytest.raises(ValueError):
            await service.index_cycle()
        assert service.index.status("alice")["archive_cursor"] == 0
        assert service.index.status("alice")["chunks"] == 1
        # New work can still advance its independent frontier on the next cycle.
        await record(runtime, "alice", "new")
        with pytest.raises(ValueError):
            await service.index_cycle()
        assert service.index.search("alice", "EVIDENCE_alice_new", cloud=False)
        assert service.index.status("alice")["progress"]["live_cursor"] == 4
        monkeypatch.setattr(service.reader, "history_event", original)
        assert (await service.index_cycle())["inserted_chunks"] == 2
        assert service.index.status("alice")["archive_cursor"] == 4
    asyncio.run(scenario())


def test_cooldown_retains_global_lease_and_other_instance_cannot_double_budget(runtime, monkeypatch):
    async def scenario():
        await owner(runtime, "alice")
        await record(runtime, "alice", 1)
        service = HistoryMemory(runtime)
        sleeping, release = asyncio.Event(), asyncio.Event()
        original = asyncio.sleep
        async def sleep(delay):
            if delay == service.renewal_seconds:
                return await original(delay)
            assert delay > 0
            sleeping.set()
            await release.wait()
        monkeypatch.setattr("ai_router.memory_service.asyncio.sleep", sleep)
        task = asyncio.create_task(service.index_cycle(throttle=True))
        try:
            await asyncio.wait_for(sleeping.wait(), 5)
            assert await HistoryMemory(runtime).index_cycle() == {"state": "busy"}
        finally:
            release.set()
            await task
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [[], {"enabled": "true"}, {"batch_records": True},
    {"batch_records": 0}, {"batch_records": 33}, {"max_batch_seconds": 0},
    {"max_batch_seconds": float("nan")}, {"duty_cycle": True}, {"duty_cycle": 0.51},
    {"unbounded": True}])
def test_invalid_resource_options_rejected(value):
    with pytest.raises(ValueError):
        indexing_options(value)


def test_progress_sampling_includes_cooldown_and_idle_estimates_expire(runtime, monkeypatch):
    async def scenario():
        service = HistoryMemory(runtime)
        service._open()
        progress = IngestionProgress(service.index)
        progress.states(["alice"], 100)
        monkeypatch.setattr("ai_router.memory_ingestion.time.time", lambda: 1000)
        progress.advance(["alice"], "backfill", 10, 100)
        progress.sample(["alice"])
        assert progress.status("alice")["estimated_remaining_seconds"] is None
        monkeypatch.setattr("ai_router.memory_ingestion.time.time", lambda: 1010)
        progress.advance(["alice"], "backfill", 20, 100)
        progress.sample(["alice"])
        assert progress.status("alice")["events_per_second"] == 1
        assert progress.status("alice")["estimated_remaining_seconds"] == 80
        monkeypatch.setattr("ai_router.memory_ingestion.time.time", lambda: 1071)
        assert progress.status("alice")["estimated_remaining_seconds"] is None
    asyncio.run(scenario())
