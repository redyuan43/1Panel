"""CPU-only history search, confidentiality and provenance regression tests."""
from dataclasses import replace
import json
import sqlite3

from cryptography.fernet import Fernet
import pytest

from ai_router.memory_index import MemoryIndex, MemorySource, search_terms
from ai_router.memory_sources import archived_sources, text_chunks, visible_message


@pytest.fixture
def index(tmp_path):
    return MemoryIndex(tmp_path / "memory.sqlite3", Fernet.generate_key().decode())


def source(**kwargs):
    values = dict(client_id="alice", conversation_id="older-chat", request_id="request-1",
        message_id="message-1", role="user", text="部署规则：生产服务禁止重启。文件 /srv/project/router.py，错误标识 E_MEMORY_782。",
        created_at=10, cloud_allowed=True)
    return MemorySource(**{**values, **kwargs})


def test_cross_conversation_finds_exact_original_with_source(index):
    index.add([source()])
    hits = index.search("alice", "E_MEMORY_782 的处理记录", conversation_id="new-chat")
    assert len(hits) == 1
    assert hits[0].source == source()
    assert index.read("alice", hits[0].source_id) == source()


def test_search_filters_permissions_before_candidate_limit(index, monkeypatch):
    index.add([source(message_id=f"blocked-{i}", conversation_id=f"blocked-{i}",
                      cloud_allowed=False, created_at=100+i) for i in range(80)])
    index.add([source()])
    decoded = []
    original = index._decode
    def decode(row, client_id):
        decoded.append(row["id"])
        return original(row, client_id)
    monkeypatch.setattr(index, "_decode", decode)
    hits = index.search("alice", "E_MEMORY_782", cloud=True)
    assert len(hits) == 1 and hits[0].source.message_id == "message-1"
    assert len(decoded) == 1
    index.search("alice", "E_MEMORY_782", cloud=False)
    assert len(decoded) <= 65


def test_cancelled_search_stops_before_sql_and_reports_phase(index):
    from threading import Event
    stopped = Event(); stopped.set()
    diagnostics = {}
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        index.search("alice", "E_MEMORY_782", cancel_event=stopped, diagnostics=diagnostics)
    assert diagnostics["stage"] == "corpus"
    assert diagnostics["sqlite_errorname"] == "SQLITE_INTERRUPT"


def test_expired_search_deadline_is_not_extended(index):
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        index.search("alice", "E_MEMORY_782", deadline=0)


def test_legacy_cloud_grant_does_not_rewrite_source_or_allow_known_local_history(index):
    unknown = source(cloud_allowed=False, cloud_unknown=True)
    blocked = source(message_id="blocked", cloud_allowed=False, cloud_unknown=False)
    index.add([unknown, blocked])
    assert not index.search("alice", "E_MEMORY_782", cloud=True)
    hits = index.search("alice", "E_MEMORY_782", cloud=True, legacy_cloud_approved=True)
    assert len(hits) == 1 and hits[0].source == unknown
    assert index.read("alice", hits[0].source_id, legacy_cloud_approved=True) == unknown
    assert index.read("alice", hits[0].source_id) is None
    assert not index.search("bob", "E_MEMORY_782", legacy_cloud_approved=True)
    assert not index.search("alice", "E_MEMORY_782", legacy_cloud_approved="true")
    assert len(index.search("alice", "E_MEMORY_782", cloud=False)) == 2
    index.add([replace(unknown, cloud_allowed=True, cloud_unknown=False)])
    assert not index.search("alice", "E_MEMORY_782", cloud=True)
    assert index.read("alice", hits[0].source_id, cloud=False) == unknown
    index.add([replace(unknown, cloud_unknown=False)])
    assert not index.search("alice", "E_MEMORY_782", legacy_cloud_approved=True)
    index.add([unknown])
    assert not index.search("alice", "E_MEMORY_782", legacy_cloud_approved=True)


def test_only_missing_archive_policy_gets_unknown_provenance():
    payload = {"received_at": 1, "request": {"client_id": "alice", "conversation_id": "old",
        "request_id": "request", "protocol": "chat", "received_body": {
            "messages": [{"role": "user", "content": "E_LEGACY_782 配置路径"}]}}}
    result = archived_sources(payload, client_id="alice", cloud_allowed=True)
    assert result and result[0].cloud_unknown and not result[0].cloud_allowed
    for policy in ({"version": 1, "local_only": True}, {}, None, {"version": 9, "local_only": False}):
        payload["request"]["history_source_policy"] = policy
        result = archived_sources(payload, client_id="alice", cloud_allowed=True)
        assert result and not result[0].cloud_unknown and not result[0].cloud_allowed
        approved = archived_sources(payload, client_id="alice", cloud_allowed=True, legacy_cloud_approved=True)
        assert approved and not approved[0].cloud_unknown and not approved[0].cloud_allowed


def test_chinese_search_without_external_tokenizer(index):
    index.add([source()])
    hits = index.search("alice", "生产服务禁止重启的部署规则是什么？")
    assert len(hits) == 1
    assert "禁止重启" in hits[0].source.text


def test_owner_isolation_also_applies_to_direct_reads(index):
    index.add([source(), source(client_id="bob", text="E_MEMORY_782 是 Bob 的机密，路由参数属于另一个账号。")])
    alice = index.search("alice", "E_MEMORY_782")[0]
    bob = index.search("bob", "E_MEMORY_782")[0]
    assert alice.source.client_id == "alice"
    assert bob.source.client_id == "bob"
    assert index.read("bob", alice.source_id) is None
    assert index.read("alice", bob.source_id) is None
    assert index.search("unknown", "E_MEMORY_782") == []


def test_plaintext_is_not_in_sqlite_or_search_terms(index):
    record = source(text="unique-private-canary-89371 生产数据库口令仅用于合成测试")
    index.add([record])
    raw = index.path.read_bytes()
    for forbidden in ("unique-private-canary-89371", "生产数据库", "older-chat", "request-1", "alice"):
        assert forbidden.encode() not in raw
    with sqlite3.connect(index.path) as db:
        terms = db.execute("SELECT owner,term FROM memory_terms").fetchall()
    assert terms and all(len(owner) == len(term) == 64 for owner, term in terms)


def test_identical_terms_have_different_account_digests(index):
    index.add([source(), source(client_id="bob")])
    with sqlite3.connect(index.path) as db:
        grouped = db.execute("SELECT owner,term FROM memory_terms").fetchall()
    owners = sorted(set(row[0] for row in grouped))
    assert {t for o, t in grouped if o == owners[0]}.isdisjoint({t for o, t in grouped if o == owners[1]})


def test_exclusion_takes_effect_on_search_and_read(index):
    index.add([source()])
    hit = index.search("alice", "E_MEMORY_782")[0]
    index.exclude("alice", "older-chat", True)
    assert not index.search("alice", "E_MEMORY_782")
    assert index.read("alice", hit.source_id) is None
    index.exclude("alice", "older-chat", False)
    assert index.search("alice", "E_MEMORY_782")


def test_local_only_sources_do_not_leak_to_cloud(index):
    index.add([source(cloud_allowed=False)])
    assert not index.search("alice", "E_MEMORY_782")
    hit = index.search("alice", "E_MEMORY_782", cloud=False)[0]
    assert index.read("alice", hit.source_id) is None
    assert index.read("alice", hit.source_id, cloud=False)


def test_replay_does_not_duplicate_or_relax_source_restriction(index):
    assert index.add([source()]) == 1
    assert index.add([source(request_id="request-2")]) == 0
    assert index.status("alice")["chunks"] == 1
    index.add([source(cloud_allowed=False)])
    index.add([source(cloud_allowed=True)])
    assert not index.search("alice", "E_MEMORY_782")


def test_old_and_corrected_facts_are_both_available_with_dates(index):
    index.add([source(text="release-budget-893 默认预算为 10。"), source(message_id="message-2", created_at=20,
        text="release-budget-893 预算已经更正为 20，之前的 10 不再有效。")])
    hits = index.search("alice", "release-budget-893")
    assert {hit.source.created_at for hit in hits} == {10, 20}


def test_no_generic_or_unmatched_history_injection(index):
    index.add([source()])
    assert not index.search("alice", "帮我看看之前的历史记录")
    assert not index.search("alice", "火星探测器轨道")
    assert not index.search("alice", "E_MEMORY_782", exclude_message_ids=frozenset({"message-1"}))


def test_cursor_is_monotonic_and_scoped(index):
    index.checkpoint("alice", 8)
    index.checkpoint("alice", 2)
    assert index.status("alice")["archive_cursor"] == 8
    assert index.status("bob")["archive_cursor"] == 0


def test_invalid_owner_and_roles_are_rejected(index):
    with pytest.raises(ValueError):
        index.search("", "E_MEMORY_782")
    with pytest.raises(ValueError):
        index.add([source(role="system")])


def test_reopen_retains_encrypted_index(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / "memory.sqlite3"
    MemoryIndex(path, key).add([source()])
    assert MemoryIndex(path, key).search("alice", "E_MEMORY_782")[0].source == source()


def test_ciphertext_cannot_be_swapped_between_documents(index):
    index.add([source(), source(message_id="message-2", text="another-marker-987 信息")])
    hits = index.search("alice", "E_MEMORY_782")
    with sqlite3.connect(index.path) as db:
        rows = db.execute("SELECT id,ciphertext FROM memory_documents").fetchall()
        db.execute("UPDATE memory_documents SET ciphertext=? WHERE id=?", (rows[1][1], rows[0][0]))
    with pytest.raises(ValueError, match="integrity"):
        index.read("alice", rows[0][0])


def test_database_flag_cannot_promote_encrypted_local_only_source(index):
    index.add([source(cloud_allowed=False)])
    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE memory_documents SET cloud_allowed=1")
    assert not index.search("alice", "E_MEMORY_782")


def test_archive_extraction_ignores_system_reasoning_media_and_synthetic_context():
    payload = {"received_at": 17, "request": {"client_id": "alice", "conversation_id": "old-chat",
        "request_id": "req", "protocol": "chat", "received_body": {"messages": [
            {"role": "system", "content": "NEVER_INDEX_SYSTEM"},
            {"role": "user", "content": [{"type": "text", "text": "357742 配置路径 /srv/test.py"},
                {"type": "image_url", "image_url": {"url": "NEVER_INDEX_IMAGE"}}]},
            {"role": "assistant", "content": "公开说明", "reasoning_content": "NEVER_INDEX_REASONING"},
            {"role": "user", "content": "<router-history-recall>NEVER_REINDEX</router-history-recall>"},
            {"role": "tool", "content": "api_key=sk-test123456789 password=supersecret Authorization: Bearer abcdefghijklmnop"},
        ]}}, "response": {"complete": True, "status_code": 200,
            "assistant_items": [{"role": "assistant", "content": "可见的完整回答"}]}}
    records = archived_sources(payload, client_id="alice", cloud_allowed=False, forbidden_phrases=["357742"])
    text = json.dumps([r.text for r in records], ensure_ascii=False)
    for forbidden in ("NEVER_", "357742", "sk-test123456789", "supersecret", "abcdefghijklmnop"):
        assert forbidden not in text
    assert "/srv/test.py" in text and "可见的完整回答" in text
    assert all(not r.cloud_allowed for r in records)
    assert not archived_sources(payload, client_id="bob", cloud_allowed=True)


def test_responses_tool_results_and_visible_text_are_indexable():
    role, text, identifier = visible_message({"type": "function_call_output", "call_id": "x", "output": "result"})
    assert (role, text) == ("tool", "result") and len(identifier) == 64
    assert visible_message({"type": "reasoning", "content": "secret"}) == ("", "", "")


@pytest.mark.parametrize("message", [
    {"role": "tool", "tool_call_id": "call", "content": ""},
    {"type": "function_call_output", "call_id": "call", "output": ""},
])
def test_compacted_tool_result_is_not_reindexed_as_original(message):
    from ai_router.compaction import TOOL_SUMMARY_PREFIX
    field = "output" if "output" in message else "content"
    message[field] = TOOL_SUMMARY_PREFIX + "\nSYNTHETIC_FACT"
    assert visible_message(message) == ("", "", "")


def test_chunk_offsets_reconstruct_unicode_without_gaps():
    text = ("中文日志🙂\n" * 1000) + "TAIL"
    chunks = list(text_chunks(text))
    rebuilt = [None] * len(text)
    for offset, value in chunks:
        assert text[offset:offset + len(value)] == value
        rebuilt[offset:offset + len(value)] = value
    assert "".join(rebuilt) == text
    assert all(len(value) <= 2400 for _, value in chunks)


@pytest.mark.parametrize("policy,legacy,expected", [
    ({"version": 1, "local_only": True}, True, False),
    ({"version": 1, "local_only": False}, False, True),
    ({"version": 1, "local_only": None}, True, False),
    ({"version": 1, "local_only": "false"}, True, False),
    ({"version": 2, "local_only": False}, True, False),
    ({"version": True, "local_only": False}, True, False),
    (None, False, False), (None, True, True),
])
def test_historical_source_policy_is_not_overridden_by_current_cloud_grant(policy, legacy, expected):
    payload = {"request": {"client_id": "alice", "conversation_id": "old",
        "request_id": "req", "protocol": "chat",
        "received_body": {"messages": [{"role": "user", "content": "部署文档 E_MEMORY_782"}]}}}
    if policy is not None:
        payload["request"]["history_source_policy"] = policy
    records = archived_sources(payload, client_id="alice", cloud_allowed=True,
                               legacy_cloud_approved=legacy)
    assert records and all(record.cloud_allowed == expected for record in records)
    records = archived_sources(payload, client_id="alice", cloud_allowed=False,
                               legacy_cloud_approved=legacy)
    assert records and not any(record.cloud_allowed for record in records)


@pytest.mark.parametrize("local_only", [True, False, None])
def test_archive_persists_original_policy_snapshot(tmp_path, local_only):
    import asyncio
    from ai_router.training_archive import TrainingArchive
    from ai_router.content_audit import ArchiveReader
    key_path = tmp_path / "archive.key"
    key_path.write_bytes(Fernet.generate_key())
    path = tmp_path / "archive.sqlite3"
    archive = TrainingArchive(str(path), str(key_path))
    asyncio.run(archive.begin(request_id="req", conversation_id="old",
        conversation_mode="stateful", client_id="alice", key_id="key",
        protocol="chat", received_body={"messages": []}, instance_id="test",
        boot_id="test", history_source_local_only=local_only))
    payload = ArchiveReader(path, key_path).read("req")
    assert payload["request"]["history_source_policy"] == {
        "version": 1, "local_only": local_only}
