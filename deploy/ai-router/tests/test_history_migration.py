"""Offline migration evidence; never read or modify production data."""
import asyncio
import copy
import json
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from ai_router import history_index
from ai_router.compaction import CapsuleCipher, message_hash
from ai_router.history import history_lookup_identities, verified_history_identity
from ai_router.history_identity import is_verified_history_identity
from ai_router.policy import ConversationRepository
from ai_router.shared_contracts import contract_report, history_samples
from ai_router.store import InMemoryStateStore
from ai_router.types import ConversationState


def fixture(tmp_path, monkeypatch, count=3):
    path = tmp_path / "traces.sqlite3"
    store = InMemoryStateStore()
    repo = ConversationRepository(store, NS(section=lambda _: {}))
    archives, logs = {}, []
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE route_traces(request_id TEXT PRIMARY KEY, client_id TEXT, protocol TEXT, status TEXT, started_at REAL, payload_json TEXT)")
        for i in range(count):
            request = {"request_id": str(i), "client_id": "workbuddy-public", "protocol": "chat", "conversation_id": "lineage"}
            trace = {**request, "status": "succeeded", "branch_id": "branch"}
            db.execute("INSERT INTO route_traces VALUES(?,?,?,?,?,?)",
                       (str(i), request["client_id"], "chat", "succeeded", now, json.dumps(trace)))
            archives[str(i)] = {"request": request, "response": {"complete": True, "status_code": 200,
                "assistant_items": [{"role": "assistant", "content": "answer " + str(i), "reasoning": "thought"}]},
                "pipeline": {"stages": [{"stage": "after_directives", "sha256": "body"}],
                             "bodies": {"body": {"messages": [{"role": "user", "content": "question " + str(i)}]}}}}
    monkeypatch.setattr(history_index, "reader", lambda: NS(read=archives.get))
    runtime = NS(store=store, conversations=repo, route_traces=NS(database_path=str(path)),
                 audit=NS(write=lambda event, **kwargs: logs.append((event, kwargs))))
    return runtime, archives, logs


async def save(repo, branch="branch", conversation="lineage", endpoint="astra", directive="beichen"):
    state = ConversationState(conversation, "auto", endpoint, 1, "code", time.time(), branch_id=branch,
        directive_id=directive, directive_generation=3, directive_endpoint_id=endpoint if directive else None)
    await repo.save(state)
    return state


def test_golden_contracts_require_an_explicit_versioned_migration():
    assert contract_report() == {
        "probe_version": 1,
        "history": {"version": "v7", "samples_sha256": "e33bce0723fce9ec8915607a05859490984fd0a81a8718f80a8bc5a73730bc2f",
                    "consumer_sha256": "cb08d1b602fdd6bb8fd746d8cab0818bc192330e36d0e9e130a8f743682aaf90",
                    "public_samples_sha256": "62d164bc8934d67bc91cc48f78141e3f8b3dbca8af36b6b5ca1387079049824a",
                    "workbuddy_prefix_version": 2,
                    "workbuddy_prefix_sha256": "ea8eb37ce7646fae715ea3c092affd497f949b1900811dda650928070e3d4aba"},
        "archive": {"version": 1, "samples_sha256": "055cc8165d586691bd69dc7ab500c0e2a9d1880ffedea0aba04f57d0c2339a21",
                    "completed_public_history": True},
        "conversation": {"samples_sha256": "9087e954d650850f373481bab6bbad864154f96bc11b6a7949aff0f92357cb6b"},
    }
    samples = history_samples()
    assert verified_history_identity(samples[2]) == verified_history_identity(samples[3])
    assert verified_history_identity(samples[1]) != verified_history_identity(samples[2])
    assert verified_history_identity(samples[1]) == verified_history_identity(samples[5])


@pytest.mark.parametrize("prefix", ["", "wb-raw-v1:"])
def test_legacy_and_future_evidence_cannot_be_promoted_or_matched(prefix):
    async def case():
        repo = ConversationRepository(InMemoryStateStore(), NS(section=lambda _: {}))
        await save(repo)
        current = prefix + verified_history_identity(history_samples()[2])
        for version in (5, 6, 8):
            retired = current.replace("v7-history-", f"v{version}-history-")
            assert is_verified_history_identity(retired)
            await repo.map_history("wb", (retired,), "branch")
            assert await repo.store.get_json("router:history-conversation:wb:" + retired) is None
            assert await repo.store.get_json("router:verified-history:wb:" + retired) is None
            # Simulate a retained legacy Redis key, not just an absent key.
            await repo.store.add_history_candidate("router:verified-history:wb:" + retired, "branch", 60)
            assert (await repo.verified_history_match("wb", (retired,)))[0] is None
            assert (await repo.verified_history_match("wb", (current,)))[0] is None
        await repo.map_history("wb", (current,), "branch")
        assert (await repo.verified_history_match("wb", (current,)))[0].directive_id == "beichen"
    asyncio.run(case())


def test_backfill_resumes_equal_timestamps_and_does_not_renew_state(tmp_path, monkeypatch):
    runtime, archives, logs = fixture(tmp_path, monkeypatch, count=5)
    async def case():
        await save(runtime.conversations)
        before = await runtime.store.get_json("router:conversation-branch:branch")
        reports = [await history_index.rebuild_verified_history(runtime, max_records=2, page_size=1) for _ in range(3)]
        assert [r["scanned"] for r in reports] == [2, 2, 1]
        assert [r["complete"] for r in reports] == [False, False, True]
        assert sum(r["records"] for r in reports) == len(archives)
        assert await runtime.store.get_json("router:conversation-branch:branch") == before
        for request_id, archive in archives.items():
            aliases = history_index.raw_aliases(archive, {**archive["request"], "status": "succeeded"})
            assert (await runtime.conversations.verified_history_match("workbuddy-public", aliases[::-1]))[0]
        assert logs[-1][0] == "verified_history_backfill_completed"
        assert all("request_id" not in entry for _, entry in logs)
    asyncio.run(case())


def test_backfill_failure_retries_page_and_releases_lock(tmp_path, monkeypatch):
    runtime, _, logs = fixture(tmp_path, monkeypatch)
    original = history_index.index_completed
    async def fail(*args):
        raise ValueError("synthetic private content must not be logged")
    async def case():
        await save(runtime.conversations)
        monkeypatch.setattr(history_index, "index_completed", fail)
        first = await history_index.rebuild_verified_history(runtime)
        assert first["failed"] == 1 and not first["complete"]
        assert "synthetic private content" not in json.dumps(logs)
        assert await runtime.store.get_json("router:verified-history:backfill-lock") is None
        monkeypatch.setattr(history_index, "index_completed", original)
        retry = await history_index.rebuild_verified_history(runtime)
        assert retry["records"] == 3 and retry["complete"]
    asyncio.run(case())


def test_branch_expiring_during_archive_read_cannot_be_indexed(tmp_path, monkeypatch):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=1)
    async def case():
        state = await save(runtime.conversations)
        trace = {**archives["0"]["request"], "branch_id": "branch", "status": "succeeded"}
        runtime.conversations.get = AsyncMock(side_effect=[state, None])
        runtime.conversations.map_history = AsyncMock()
        assert not await history_index.index_completed(runtime, trace, NS(read=archives.get))
        runtime.conversations.map_history.assert_not_awaited()
    asyncio.run(case())


def test_backfill_deadline_retains_cursor_for_retry(tmp_path, monkeypatch):
    runtime, _, _ = fixture(tmp_path, monkeypatch, count=1)
    original = history_index.index_completed
    async def stalled(*args):
        await asyncio.Event().wait()
    async def case():
        await save(runtime.conversations)
        monkeypatch.setattr(history_index, "index_completed", stalled)
        report = await history_index.rebuild_verified_history(runtime, max_seconds=0.05)
        assert not report["complete"] and report["records"] == 0
        progress = await runtime.store.get_json(history_index.BACKFILL_PROGRESS_KEY)
        assert progress["before"] is None
        monkeypatch.setattr(history_index, "index_completed", original)
        assert (await history_index.rebuild_verified_history(runtime))["complete"]
    asyncio.run(case())


def test_maintenance_entrypoint_defaults_to_plan(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["history_index"])
    def forbidden():
        pytest.fail("plan must not open archives")
    monkeypatch.setattr(history_index, "reader", forbidden)
    monkeypatch.delenv("AI_ROUTER_REDIS_URL", raising=False)
    assert history_index.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "plan" and report["writes"] is False


def test_backfill_checkpoint_failure_cannot_report_completion(tmp_path, monkeypatch):
    runtime, _, _ = fixture(tmp_path, monkeypatch, count=0)
    async def case():
        runtime.store.delete = AsyncMock(side_effect=OSError("synthetic storage failure"))
        report = await history_index.rebuild_verified_history(runtime)
        assert not report["complete"] and report["failed"] == 1
    asyncio.run(case())


@pytest.mark.parametrize("client,protocol", [
    ("other-client", "chat"), ("other-client", "responses"), ("workbuddy-public", "responses")])
def test_migration_restores_headerless_history_for_all_clients_and_protocols(tmp_path, monkeypatch, client, protocol):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=1)
    archive = archives["0"]
    archive["request"].update(client_id=client, protocol=protocol)
    trace = {**archive["request"], "status": "succeeded", "branch_id": "branch"}
    with sqlite3.connect(runtime.route_traces.database_path) as db:
        db.execute("UPDATE route_traces SET client_id=?, protocol=?, payload_json=?",
                   (client, protocol, json.dumps(trace)))
    messages = [{"role": "user", "content": "task"},
                {"role": "assistant", "content": "answer", "reasoning_content": "preserved"}]
    if protocol == "responses":
        messages = [{"role": "user", "content": "task"},
                    {"type": "reasoning", "encrypted_content": "synthetic encrypted provider state", "summary": []},
                    {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"}]
    runtime.history_cipher = CapsuleCipher(Fernet.generate_key().decode())
    async def case():
        state = await save(runtime.conversations)
        state.encrypted_capsule = runtime.history_cipher.encrypt(messages)
        state.boundary_hash = message_hash(messages[-1])
        await runtime.conversations.save(state)
        before = await runtime.store.get_json("router:conversation-branch:branch")
        # A cursor from the old WorkBuddy-only scan must not exclude other clients.
        await runtime.store.set_json("router:verified-history:v6:backfill-progress",
            {"since": time.time() - 86400, "until": time.time(), "before": [0, ""]})
        report = await history_index.rebuild_verified_history(runtime)
        assert report["records"] == 1 and report["complete"] and not report["failed"]
        lineage = await runtime.conversations.lineage_context(client_id=client,
            identities=history_lookup_identities([*messages, {"role": "user", "content": "continue"}]),
            explicit_lineage_id=None, previous_response_id=None, force_new=False)
        assert lineage.relation == "continuation" and lineage.parent.directive_id == "beichen"
        assert await runtime.store.get_json("router:conversation-branch:branch") == before
        assert (await runtime.conversations.verified_history_match("another-client",
            (verified_history_identity(messages),)))[0] is None
    asyncio.run(case())


@pytest.mark.parametrize("damage,reason", [
    ("missing", "archive_missing"), ("incomplete", "archive_not_complete"),
    ("owner", "archive_identity_mismatch"), ("protocol", "archive_identity_mismatch"),
    ("pipeline", "history_evidence_missing"), ("output", "history_evidence_missing")])
def test_live_branch_evidence_failure_blocks_completion_and_can_be_retried(tmp_path, monkeypatch, damage, reason):
    runtime, archives, logs = fixture(tmp_path, monkeypatch, count=2)
    saved = copy.deepcopy(archives["0"])
    if damage == "missing": del archives["0"]
    elif damage == "incomplete": archives["0"]["response"]["complete"] = False
    elif damage == "owner": archives["0"]["request"]["client_id"] = "wrong-client"
    elif damage == "protocol": archives["0"]["request"]["protocol"] = "responses"
    elif damage == "pipeline": archives["0"]["pipeline"] = {}
    elif damage == "output": archives["0"]["response"]["assistant_items"] = None
    async def case():
        await save(runtime.conversations)
        failed = await history_index.rebuild_verified_history(runtime, page_size=1)
        assert failed["records"] == 1 and failed["failed"] == 1 and failed["skipped"] == 0
        assert failed["complete"] is False and failed["failure_reason"] == reason
        cursor = await runtime.store.get_json(history_index.BACKFILL_PROGRESS_KEY)
        assert cursor["before"][1] == "1"  # Failed row was not skipped.
        assert all("request_id" not in report for _, report in logs)
        archives["0"] = saved
        retried = await history_index.rebuild_verified_history(runtime)
        assert retried["records"] == 1 and retried["complete"] and not retried["failed"]
    asyncio.run(case())


@pytest.mark.parametrize("damage,reason", [("missing", "persisted_history_missing"),
    ("boundary", "persisted_history_boundary_mismatch"), ("shape", "persisted_history_invalid")])
def test_generic_history_without_valid_capsule_cannot_pass_migration(tmp_path, monkeypatch, damage, reason):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=1)
    archives["0"]["request"]["client_id"] = "other-client"
    trace = {**archives["0"]["request"], "branch_id": "branch", "status": "succeeded"}
    runtime.history_cipher = CapsuleCipher(Fernet.generate_key().decode())
    async def case():
        state = await save(runtime.conversations)
        if damage != "missing":
            state.encrypted_capsule = runtime.history_cipher.encrypt({} if damage == "shape" else history_samples()[2])
            state.boundary_hash = "incorrect"
            await runtime.conversations.save(state)
        with pytest.raises(history_index.HistoryIndexEvidenceError) as failure:
            await history_index.index_completed(runtime, trace, NS(read=archives.get))
        assert failure.value.reason == reason
        assert await runtime.store.list_json("router:verified-history:other-client:") == []
    asyncio.run(case())


def test_nonstream_raw_history_uses_archived_json_response_without_field_loss():
    messages = history_samples()[2]
    output = {"role": "assistant", "content": "answer", "reasoning": "raw field must survive"}
    trace = {"request_id": "r", "client_id": "workbuddy-public", "status": "succeeded", "protocol": "chat"}
    archive = {"request": trace, "response": {"complete": True, "status_code": 200, "assistant_items": None,
        "body": {"encoding": "json", "value": {"choices": [{"message": output}]}}},
        "pipeline": {"stages": [{"stage": "after_directives", "sha256": "body"}],
                     "bodies": {"body": {"messages": messages}}}}
    before = copy.deepcopy(archive)
    assert "wb-raw-v1:" + verified_history_identity([*messages, output]) in history_index.raw_aliases(archive, trace)
    assert archive == before


def test_public_responses_boundary_survives_reindex_without_discarding_private_reasoning(tmp_path, monkeypatch):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=1)
    archive = archives["0"]
    archive["request"]["protocol"] = "responses"
    messages = [{"role": "user", "content": "question"}]
    output = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
    private = {**output, "reasoning_content": "retained only on the server"}
    archive["pipeline"]["bodies"]["body"] = {"input": messages}
    archive["pipeline"]["stages"].append({"stage": "history_identity_input", "sha256": "body"})
    archive["response"]["public_assistant_items"] = [output]
    original = copy.deepcopy(archive)
    runtime.history_cipher = CapsuleCipher(Fernet.generate_key().decode())

    async def case():
        state = await save(runtime.conversations, endpoint="bonsai", directive=None)
        state.encrypted_capsule = runtime.history_cipher.encrypt([*messages, private])
        state.boundary_hash = message_hash(private)
        await runtime.conversations.save(state)
        trace = {**archive["request"], "branch_id": "branch", "status": "succeeded"}
        assert await history_index.index_completed(runtime, trace, NS(read=archives.get))
        identities = history_lookup_identities([*messages, output, {"role": "user", "content": "continue"}])
        parent, _ = await runtime.conversations.verified_history_match("workbuddy-public", identities)
        assert parent.endpoint_id == "bonsai"
        assert runtime.history_cipher.decrypt(parent.encrypted_capsule)[-1] == private
        assert (await runtime.conversations.verified_history_match("another-client", identities))[0] is None
        await save(runtime.conversations, branch="other", conversation="other", endpoint="qwen", directive=None)
        await runtime.conversations.map_history("workbuddy-public", identities[:1], "other")
        parent, evidence = await runtime.conversations.verified_history_match("workbuddy-public", identities)
        assert parent is None and evidence["reason"] == "ambiguous_history"
        assert archive == original
    asyncio.run(case())


def test_maintenance_command_returns_nonzero_when_live_evidence_is_missing(monkeypatch, capsys):
    from ai_router import store, config
    monkeypatch.setattr("sys.argv", ["history_index", "--apply"])
    monkeypatch.setenv("AI_ROUTER_REDIS_URL", "redis://offline-unused")
    monkeypatch.setattr(store, "RedisStateStore", lambda _: InMemoryStateStore())
    monkeypatch.setattr(config, "Settings", lambda: NS(section=lambda _: {}))
    monkeypatch.setattr(history_index, "rebuild_verified_history", AsyncMock(return_value={
        "complete": False, "failed": 1, "failure_reason": "archive_missing"}))
    assert history_index.main() == 2
    assert json.loads(capsys.readouterr().out)["failure_reason"] == "archive_missing"


@pytest.mark.parametrize("missing", [True, False])
def test_backfill_never_revives_missing_or_expired_branch(tmp_path, monkeypatch, missing):
    runtime, _, _ = fixture(tmp_path, monkeypatch)
    async def case():
        if not missing:
            state = await save(runtime.conversations)
            state.last_seen = time.time() - 86401
            await runtime.store.set_json("router:conversation-branch:branch", state.to_dict())
        report = await history_index.rebuild_verified_history(runtime)
        assert report["records"] == 0 and report["skipped"] == 3 and report["complete"]
        assert await runtime.store.list_json("router:verified-history:workbuddy-public:") == []
    asyncio.run(case())


def test_backfill_preserves_ambiguity_and_does_not_restore_a_split_directive(tmp_path, monkeypatch):
    runtime, archives, _ = fixture(tmp_path, monkeypatch, count=2)
    async def case():
        await save(runtime.conversations)
        await save(runtime.conversations, "split", "split-lineage", "qwen", None)
        first = archives["0"]
        second = archives["1"]
        second["request"]["conversation_id"] = "split-lineage"
        second["pipeline"]["bodies"]["body"]["messages"] = [
            *first["pipeline"]["bodies"]["body"]["messages"], *first["response"]["assistant_items"],
            {"role": "user", "content": "continue"}]
        for request_id, archive in archives.items():
            trace = {**archive["request"], "status": "succeeded", "branch_id": "branch" if request_id == "0" else "split"}
            assert await history_index.index_completed(runtime, trace, NS(read=archives.get))
        messages = [*second["pipeline"]["bodies"]["body"]["messages"], *second["response"]["assistant_items"],
                    {"role": "user", "content": "continue again"}]
        identities = tuple("wb-raw-v1:" + v for v in history_lookup_identities(messages))
        parent, _ = await runtime.conversations.verified_history_match("workbuddy-public", identities)
        assert parent.branch_id == "split" and parent.directive_id is None
        # A second valid branch with identical evidence must remain ambiguous.
        await runtime.conversations.map_history("workbuddy-public", identities[:1], "branch")
        parent, report = await runtime.conversations.verified_history_match("workbuddy-public", identities)
        assert parent is None and report["reason"] == "ambiguous_history"
    asyncio.run(case())


def test_backfill_cancellation_and_lock_loss_do_not_leak_work(tmp_path, monkeypatch):
    runtime, _, _ = fixture(tmp_path, monkeypatch)
    async def case():
        await save(runtime.conversations)
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(history_index, "index_completed", blocked)
        task = asyncio.create_task(history_index.rebuild_verified_history(runtime))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await runtime.store.get_json("router:verified-history:backfill-lock") is None
        runtime.store.renew_lock = AsyncMock(return_value=False)
        report = await history_index.rebuild_verified_history(runtime)
        assert report["failed"] == 1 and report["scanned"] == 0
    asyncio.run(case())
