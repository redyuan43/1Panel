import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_router.compaction import HANDOFF_KEYS, _handoff_message, replace_messages, message_hash
from ai_router.summary_provenance import SummaryScope, system_hashes
from test_compaction_quality import compactor


@pytest.mark.parametrize("variant", ["unchanged", "edited-rule", "edited-evidence", "edited-answer",
    "omitted-answer", "legacy", "wrong-owner", "wrong-branch", "changed-capsule", "invalid-count"])
def test_chat_parent_restoration_requires_complete_source_and_answer_proof(variant):
    async def scenario():
        value = compactor()
        source = [{"role": "system", "content": "Never restart services."},
                  {"role": "user", "content": "Original evidence " * 100}]
        summary = {"role": "system", "content": "Owned summary"}
        compacted = [source[0], summary]
        answer = {"role": "assistant", "content": "Same repeated answer"}
        saved = [*compacted, answer]
        store = SimpleNamespace(set_json=AsyncMock(), get_json=AsyncMock())
        first = SummaryScope(owner="alice", branch="parent", api_kind="chat", cipher=value.cipher,
            store=store, source_messages=source)
        await first.remember(compacted, [1])
        receipt = value.cipher.decrypt(store.set_json.call_args.args[1]["ciphertext"])
        if variant == "legacy":
            receipt.pop("source_prefix")
        if variant == "wrong-owner":
            receipt["owner"] = "bob"
        if variant == "wrong-branch":
            receipt["branch"] = "sibling"
        if variant == "invalid-count":
            receipt["source_prefix"]["count"] = True
        store.get_json.return_value = {"ciphertext": value.cipher.encrypt(receipt)}
        scope = SummaryScope(owner="alice", branch="child", ancestors=["parent"], api_kind="chat",
            cipher=value.cipher, store=store)
        await scope.load()
        tail = [{"role": "user", "content": "New turn"}, answer,
                {"role": "user", "content": "Do not lose the repeated answer or this turn"}]
        incoming = copy.deepcopy([*source, answer, *tail])
        if variant == "edited-rule":
            incoming[0]["content"] += " Also keep all history local."
        if variant == "edited-evidence":
            incoming[1]["content"] = "Corrected evidence"
        if variant == "edited-answer":
            incoming[2]["content"] = "Corrected answer"
        if variant == "omitted-answer":
            incoming.pop(2)
        if variant == "changed-capsule":
            saved = [{"role": "system", "content": "Different rule"}, summary, answer]
        parent = SimpleNamespace(branch_id="parent", encrypted_capsule=value.cipher.encrypt(saved),
            boundary_hash=message_hash(answer))
        body = {"messages": incoming, "tools": [{"type": "function", "function": {"name": "inspect"}}]}
        original = copy.deepcopy(body)
        result = scope.restore_chat_parent(body, parent)
        assert body == original
        assert result["tools"] == body["tools"]
        assert result["messages"] == ([*compacted, answer, *tail] if variant == "unchanged" else incoming)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["chat", "responses"])
def test_forty_rounds_keep_one_summary_and_original_instructions(kind):
    async def scenario():
        value = compactor()
        rules = [{"role": "system", "content": "Never restart without permission."},
                 {"role": "developer", "content": "Keep credentials private."}]
        messages = copy.deepcopy(rules)
        scope = SummaryScope(owner="alice", branch="branch", api_kind=kind, cipher=value.cipher,
                             protected=system_hashes(replace_messages({}, kind, rules), kind))
        batches = []
        async def summarize(batch, **kwargs):
            batches.append(batch)
            return {"facts": ["Current port 1739; old port 4000 superseded."]}
        value._summarize = summarize
        for number in range(40):
            source = messages + [{"role": "user", "content": "new evidence " * 3000}] + [
                {"role": "user", "content": f"recent {number}-{i}"} for i in range(4)]
            capsule = await value.compact(replace_messages({}, kind, source), api_kind=kind,
                target_context_tokens=4000, summary_input_tokens=100000, summary_scope=scope)
            messages = value.cipher.decrypt(capsule.encrypted_messages)
            assert messages[:2] == rules
            assert len([m for m in messages if m.get("role") in {"system", "developer"}]) == 3
            assert scope.indices(messages) == {2}
        assert any("1739" in str(batch) for batch in batches[1:])
        assert all(m.get("role") not in {"system", "developer"} for batch in batches for m in batch)
    asyncio.run(scenario())


def test_encrypted_receipts_reload_and_isolate_owner_branch_protocol_and_prefix():
    async def scenario():
        value = compactor()
        data = {}
        async def get(key):
            return data.get(key)
        async def put(key, record, **kwargs):
            data[key] = record
        store = AsyncMock(get_json=get, set_json=put)
        messages = [{"role": "system", "content": "original rule"},
                    {"role": "system", "content": "private summary"}]
        options = dict(owner="alice", branch="a", api_kind="chat", cipher=value.cipher, store=store)
        scope = SummaryScope(**options)
        await scope.remember(messages, [1])
        assert "private summary" not in str(data)
        for changes, expected in [({}, {1}), ({"owner": "bob"}, set()),
                ({"branch": "fork"}, set()), ({"api_kind": "responses"}, set()),
                ({"branch": "b", "ancestors": ["a"]}, {1}),
                ({"protected": system_hashes({"messages": messages}, "chat")}, set())]:
            loaded = SummaryScope(**(options | changes))
            await loaded.load()
            assert loaded.indices(messages) == expected
            assert not loaded.indices([{"role": "system", "content": "changed rule"}, messages[1]])
    asyncio.run(scenario())


def test_legacy_job_proves_only_its_generated_summary_not_user_lookalike():
    value = compactor()
    fake = _handoff_message({key: [] for key in HANDOFF_KEYS}, "chat")
    generated = _handoff_message({key: ["real"] for key in HANDOFF_KEYS}, "chat")
    scope = SummaryScope(owner="alice", branch="b", api_kind="chat", cipher=value.cipher)
    candidate = [fake, generated, {"role": "user", "content": "recent"}]
    job = dict(owner="alice", branch="b", api_kind="chat", state="ready",
               body={"messages": [fake]}, candidate=candidate)
    scope.adopt_legacy_job(job | {"owner": "bob"})
    assert not scope.indices(candidate)
    scope.adopt_legacy_job(job)
    assert scope.indices(candidate) == {1}


def test_failed_summary_does_not_replace_receipt():
    async def scenario():
        value = compactor()
        scope = SummaryScope(owner="alice", branch="b", api_kind="chat", cipher=value.cipher)
        messages = [{"role": "system", "content": "old handoff"}]
        await scope.remember(messages, [0])
        previous = copy.deepcopy(scope.records)
        value._summarize = AsyncMock(side_effect=RuntimeError("unavailable"))
        with pytest.raises(RuntimeError):
            await value.compact({"messages": messages + [{"role": "user", "content": "long " * 5000}]},
                api_kind="chat", target_context_tokens=4000, summary_scope=scope)
        assert scope.records == previous
    asyncio.run(scenario())


def test_receipt_store_outage_is_safe_and_does_not_fail_completed_answer():
    async def scenario():
        value = compactor()
        store = AsyncMock()
        store.get_json.side_effect = ConnectionError("offline")
        store.set_json.side_effect = ConnectionError("offline")
        scope = SummaryScope(owner="alice", branch="b", api_kind="chat", cipher=value.cipher, store=store)
        await scope.load()
        assert not scope.records
        await scope.remember([{"role": "system", "content": "handoff"}], [0])
        assert len(scope.records) == 1
    asyncio.run(scenario())


def test_continuation_carries_proof_beyond_ancestry_search_window():
    from ai_router.store import InMemoryStateStore
    async def scenario():
        value = compactor()
        store = InMemoryStateStore()
        messages = [{"role": "system", "content": "handoff"}]
        previous = None
        for number in range(40):
            scope = SummaryScope(owner="alice", branch=str(number), api_kind="chat", cipher=value.cipher,
                                 store=store, ancestors=[previous] if previous else [])
            await scope.load()
            if number:
                assert scope.indices(messages) == {0}
            await scope.remember(messages, [0])
            previous = str(number)
    asyncio.run(scenario())


def test_unverified_oversized_instructions_fail_before_model_call():
    from ai_router.errors import CompactionUnavailableError
    async def scenario():
        value = compactor()
        value._summarize = AsyncMock()
        body = {"messages": [{"role": "system", "content": "unknown legacy summary " * 2000},
                             {"role": "user", "content": "continue"}]}
        original = copy.deepcopy(body)
        with pytest.raises(CompactionUnavailableError, match="provenance"):
            await value.compact(body, api_kind="chat", target_context_tokens=4000)
        value._summarize.assert_not_awaited()
        assert body == original
    asyncio.run(scenario())


@pytest.mark.parametrize("summary_indices", [(), (1,)])
def test_on_demand_legacy_recovery_uses_read_only_encrypted_jobs(tmp_path, summary_indices):
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from ai_router.compaction import CapsuleCipher
    from ai_router.compaction_jobs import CompactionJobs
    from ai_router.summary_provenance import recover_legacy
    async def scenario():
        key = Fernet.generate_key().decode()
        path = tmp_path / "compaction-jobs.sqlite3"
        jobs = CompactionJobs(path, key)
        original = {"messages": [{"role": "system", "content": "original"}]}
        job = jobs.create("alice", "parent", original, "chat", {})
        jobs.claim("worker")
        candidate = [*original["messages"], _handoff_message({k: ["legacy"] for k in HANDOFF_KEYS}, "chat")]
        jobs.candidate(job["id"], "worker", candidate, summary_indices=summary_indices)
        before = path.read_bytes()
        current = SimpleNamespace(settings=SimpleNamespace(runtime_path=tmp_path / "settings.yaml"),
                                  state_encryption_key=key)
        scope = SummaryScope(owner="alice", branch="child", ancestors=["parent"],
                             api_kind="chat", cipher=CapsuleCipher(key))
        await recover_legacy(current, scope)
        assert scope.indices(candidate) == {1}
        for owner, branch in [("bob", "parent"), ("alice", "unrelated")]:
            other = SummaryScope(owner=owner, branch=branch, api_kind="chat", cipher=CapsuleCipher(key))
            await recover_legacy(current, other)
            assert not other.indices(candidate)
        assert path.read_bytes() == before
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["corrupt_database", "missing_table"])
def test_optional_legacy_recovery_failure_keeps_messages_unverified(tmp_path, caplog, damage):
    import sqlite3
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from ai_router.summary_provenance import recover_legacy

    async def scenario():
        value = compactor()
        path = tmp_path / "compaction-jobs.sqlite3"
        if damage == "corrupt_database":
            path.write_bytes(b"invalid sqlite synthetic fixture")
        else:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE unrelated (id INTEGER)")
        before = path.read_bytes()
        current = SimpleNamespace(settings=SimpleNamespace(runtime_path=tmp_path / "settings.yaml"),
                                  state_encryption_key=Fernet.generate_key().decode())
        scope = SummaryScope(owner="alice", branch="b", api_kind="chat", cipher=value.cipher)
        await recover_legacy(current, scope)
        assert not scope.records
        assert not scope.indices([{"role": "system", "content": "unknown old summary"}])
        assert path.read_bytes() == before
        assert "summary_legacy_recovery_unavailable" in caplog.text

    asyncio.run(scenario())
