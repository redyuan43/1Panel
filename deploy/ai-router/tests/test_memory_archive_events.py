"""Archive change-feed ordering, restart safety and account filtering."""
import asyncio
import sqlite3

from cryptography.fernet import Fernet
import pytest

from ai_router.content_audit import ArchiveReader
from ai_router.training_archive import TrainingArchive


@pytest.fixture
def archive(tmp_path):
    path, key = tmp_path / "archive.sqlite3", tmp_path / "archive.key"
    key.write_bytes(Fernet.generate_key())
    writer = TrainingArchive(str(path), str(key))
    yield writer, ArchiveReader(path, key)
    writer.close()


def test_idle_writer_keeps_wal_files_without_pinning_a_transaction(archive):
    writer, reader = archive
    begin(writer, "idle-read")
    assert not writer._wal_anchor.in_transaction
    for suffix in ("-wal", "-shm"):
        assert writer.database_path.with_name(writer.database_path.name + suffix).exists()
    # An idle anchor must not prevent checkpointing or visibility of new writes.
    with sqlite3.connect(writer.database_path) as db:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    begin(writer, "after-checkpoint")
    assert reader.read("after-checkpoint")["request"]["request_id"] == "after-checkpoint"
    writer.close()
    writer.close()


def begin(writer, request_id, owner="alice"):
    return asyncio.run(writer.begin(request_id=request_id, conversation_id="chat-" + owner,
        conversation_mode="stateful", client_id=owner, key_id="key", protocol="chat",
        received_body={"messages": [{"role": "user", "content": "E_MEMORY_782 原始资料"}]},
        instance_id="test", boot_id="test", history_source_local_only=False))


def test_late_completion_is_visible_after_cursor_advanced(archive):
    writer, reader = archive
    early = begin(writer, "early")
    begin(writer, "later")
    cursor, page = reader.history_page("alice")
    assert len(page) == 2
    asyncio.run(writer.complete(early, status_code=200,
        response_payload=b'{"choices":[{"message":{"role":"assistant","content":"late answer"}}]}'))
    next_cursor, page = reader.history_page("alice", cursor)
    assert next_cursor > cursor and len(page) == 1
    assert page[0]["request"]["request_id"] == "early"
    assert page[0]["response"]["complete"] is True


def test_filtered_page_advances_without_returning_other_account(archive):
    writer, reader = archive
    begin(writer, "bob-request", "bob")
    begin(writer, "alice-request")
    cursor, page = reader.history_page("alice", limit=1)
    assert cursor > 0 and page == []
    cursor, page = reader.history_page("alice", cursor, limit=1)
    assert [p["request"]["client_id"] for p in page] == ["alice"]
    assert reader.history_page("alice", cursor) == (cursor, [])


def test_reopening_does_not_reseed_and_old_writer_updates_emit_events(archive):
    writer, reader = archive
    begin(writer, "request")
    cursor, _ = reader.history_page("alice")
    TrainingArchive(str(writer.database_path), str(writer.key_path))
    assert reader.history_page("alice", cursor) == (cursor, [])
    # Older processes do not know about the event table; the DB trigger covers
    # their existing write path without coupling cursor correctness to wall time.
    with sqlite3.connect(writer.database_path) as db:
        db.execute("UPDATE training_records SET payload_ciphertext=payload_ciphertext")
    next_cursor, page = reader.history_page("alice", cursor)
    assert next_cursor > cursor and len(page) == 1


@pytest.mark.parametrize("shared", [False, True])
def test_ciphertext_swap_is_detected(archive, shared):
    writer, reader = archive
    begin(writer, "alice-request")
    begin(writer, "bob-request", "bob")
    with sqlite3.connect(writer.database_path) as db:
        rows = db.execute("SELECT id,payload_ciphertext FROM training_records ORDER BY id").fetchall()
        db.execute("UPDATE training_records SET payload_ciphertext=? WHERE id=?", (rows[1][1], rows[0][0]))
    with pytest.raises(ValueError, match="provenance"):
        if shared:
            reader.history_event(0, reader.history_head())
        else:
            reader.history_page("alice")


@pytest.mark.parametrize("owner,cursor,limit", [("", 0, 4), ("alice", -1, 4),
    ("alice", 0, 0), ("alice", 0, 33), ("alice", True, 4)])
def test_invalid_pagination_rejected(archive, owner, cursor, limit):
    with pytest.raises(ValueError):
        archive[1].history_page(owner, cursor, limit=limit)
