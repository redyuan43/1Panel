import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from test_compaction_worker import runtime
from ai_router.background_context import apply_background, submit_background
from ai_router.compaction_worker import CompactionWorker
from ai_router.compaction import ContextCompactor, extract_messages, replace_messages, message_hash


@pytest.mark.parametrize("relation,changed,expected", [
    ("continuation", False, True), ("continuation", True, False),
    ("fork", False, False), ("new", False, False),
])
def test_candidate_follows_only_verified_continuation_ancestry(runtime, relation, changed, expected):
    async def scenario():
        decision = setup(runtime)
        source = {"messages": [{"role": "user", "content": "old facts " * 100}]}
        job_id = await submit_background(runtime, source, owner="alice", key_id="key", branch="grandparent",
            api_kind="chat", decision=decision)
        jobs = runtime.compaction_worker.jobs
        jobs.claim("worker")
        jobs.candidate(job_id, "worker", [{"role": "user", "content": "short summary"}])
        grandparent = SimpleNamespace(branch_id="grandparent", conversation_id="chat", lineage_relation="new", parent_branch_id=None)
        parent = SimpleNamespace(branch_id="parent", conversation_id="chat", lineage_relation="continuation", parent_branch_id="grandparent")
        runtime.conversations = SimpleNamespace(get=AsyncMock(return_value=grandparent))
        lineage = SimpleNamespace(relation=relation, parent=parent, lineage_id="chat")
        incoming = copy.deepcopy(source)
        if changed:
            incoming["messages"][0]["content"] = "edited history"
        incoming["messages"].append({"role": "user", "content": "next turn"})
        original = copy.deepcopy(incoming)
        applied, capsule = await apply_background(runtime, incoming, owner="alice", branch="child",
            api_kind="chat", identity=SimpleNamespace(inject=lambda body, kind: body), lineage=lineage)
        assert bool(capsule) == expected
        assert incoming == original
        if expected:
            assert capsule.background_job_id == job_id
            assert applied["messages"][-1] == incoming["messages"][-1]
        else:
            assert applied == incoming
    asyncio.run(scenario())


def setup(runtime):
    runtime.clients = SimpleNamespace(current_policy=AsyncMock(return_value=SimpleNamespace(
        allow_compaction=True, local_only=False, routing_mode="inherit")),
        is_key_active=AsyncMock(return_value=True))
    runtime.compaction_worker = CompactionWorker(runtime)
    runtime.token_counter.count_request.side_effect = lambda body, kind: len(json.dumps(body))
    return SimpleNamespace(endpoint=SimpleNamespace(id="target", safe_context_tokens=1000),
        deployment_safe_context_tokens=None, prompt_tokens=700, output_reserve_tokens=100)


def test_new_candidate_carries_encrypted_summary_proof(runtime):
    from ai_router.summary_provenance import SummaryScope
    async def scenario():
        decision = setup(runtime)
        source = {"messages": [{"role": "system", "content": "original rule"},
                               {"role": "user", "content": "history " * 200}]}
        scope = SummaryScope(owner="alice", branch="branch", api_kind="chat", cipher=runtime.compactor.cipher)
        job_id = await submit_background(runtime, source, owner="alice", key_id="key", branch="branch",
                                         api_kind="chat", decision=decision, summary_scope=scope)
        jobs = runtime.compaction_worker.jobs
        jobs.claim("worker")
        candidate = [source["messages"][0], {"role": "system", "content": "new owned handoff"}]
        jobs.candidate(job_id, "worker", candidate, summary_indices=(1,))
        restored = SummaryScope(owner="alice", branch="branch", api_kind="chat", cipher=runtime.compactor.cipher)
        applied, capsule = await apply_background(runtime, source, owner="alice", branch="branch", api_kind="chat",
            identity=SimpleNamespace(inject=lambda body, kind: body), summary_scope=restored)
        assert capsule is not None
        assert restored.indices(applied["messages"]) == {1}
    asyncio.run(scenario())


def test_threshold_dedupe_candidate_and_new_tail(runtime):
    async def scenario():
        decision = setup(runtime)
        body = {"messages": [{"role": "user", "content": "old evidence " * 100}]}
        args = dict(owner="alice", key_id="key", branch="branch", api_kind="chat", decision=decision)
        decision.prompt_tokens = 699
        assert await submit_background(runtime, body, **args) is None
        decision.prompt_tokens = 700
        job_id = await submit_background(runtime, body, **args)
        assert job_id and await submit_background(runtime, body, **args) == job_id
        jobs = runtime.compaction_worker.jobs
        assert jobs.claim("worker")["id"] == job_id
        summary = [{"role": "user", "content": "summary"}]
        jobs.candidate(job_id, "worker", summary)
        incoming = {"messages": [*body["messages"], {"role": "user", "content": "new request"}], "tools": []}
        original = copy.deepcopy(incoming)
        applied, capsule = await apply_background(runtime, incoming, owner="alice", branch="branch",
            api_kind="chat", identity=SimpleNamespace(inject=lambda body, kind: body))
        assert capsule and applied["messages"] == [*summary, incoming["messages"][-1]]
        assert incoming == original and applied["tools"] == []
        assert runtime.compactor.cipher.decrypt(capsule.encrypted_messages) == applied["messages"]
    asyncio.run(scenario())


def test_disabled_background_or_revoked_account_never_submits(runtime):
    async def scenario():
        decision = setup(runtime)
        args = dict(owner="alice", key_id="key", branch="branch", api_kind="chat", decision=decision)
        runtime.settings.section("compaction")["background_enabled"] = False
        assert await submit_background(runtime, {}, **args) is None
        assert runtime.compaction_worker.jobs is None
        runtime.settings.section("compaction")["background_enabled"] = True
        runtime.clients.current_policy.return_value = None
        assert await submit_background(runtime, {}, **args) is None
        assert runtime.compaction_worker.jobs is None
    asyncio.run(scenario())


def test_changed_history_or_revoked_key_does_not_apply(runtime):
    async def scenario():
        decision = setup(runtime)
        body = {"messages": [{"role": "user", "content": "old evidence " * 100}]}
        job_id = await submit_background(runtime, body, owner="alice", key_id="key", branch="branch",
                                         api_kind="chat", decision=decision)
        jobs = runtime.compaction_worker.jobs
        jobs.claim("worker")
        jobs.candidate(job_id, "worker", [{"role": "user", "content": "summary"}])
        args = dict(owner="alice", branch="branch", api_kind="chat", identity=SimpleNamespace(inject=lambda body, kind: body))
        changed = {"messages": [{"role": "user", "content": "corrected source " * 100}]}
        assert await apply_background(runtime, changed, **args) == (changed, None)
        runtime.clients.is_key_active.return_value = False
        assert await apply_background(runtime, body, **args) == (body, None)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["chat", "responses"])
def test_summarized_last_tool_uses_original_boundary_for_next_request(runtime, kind):
    async def scenario():
        decision = setup(runtime)
        call = ({"role": "assistant", "tool_calls": [{"id": "call", "type": "function",
            "function": {"name": "read", "arguments": "{}"}}]} if kind == "chat" else
            {"type": "function_call", "call_id": "call", "name": "read", "arguments": "{}"})
        output = ({"role": "tool", "tool_call_id": "call", "content": "large evidence " * 100}
            if kind == "chat" else {"type": "function_call_output", "call_id": "call", "output": "large evidence " * 100})
        question = {"role": "user", "content": "Explain the tool result"}
        body = replace_messages({}, kind, [question, call, output])
        job_id = await submit_background(runtime, body, owner="alice", key_id="key", branch="branch",
                                          api_kind=kind, decision=decision)
        jobs = runtime.compaction_worker.jobs
        assert jobs.claim("worker")["id"] == job_id
        shortened = {**output, "content" if kind == "chat" else "output": "summarized result"}
        jobs.candidate(job_id, "worker", [question, call, shortened])
        original = copy.deepcopy(body)
        applied, capsule = await apply_background(runtime, body, owner="alice", branch="branch", api_kind=kind,
            identity=SimpleNamespace(inject=lambda value, kind: value))
        assert capsule and body == original
        assert capsule.boundary_hash == message_hash(output) != message_hash(shortened)
        compactor = ContextCompactor(runtime.token_counter, runtime.compactor.cipher,
            internal_base_url="http://unused.invalid", internal_api_key="", model_id="summary", client=AsyncMock())
        tail = {"role": "user", "content": "Keep going"}
        incoming = replace_messages(body, kind, [question, call, output, tail])
        continued = compactor.apply_existing(incoming, api_kind=kind,
            encrypted_messages=capsule.encrypted_messages, boundary_hash=capsule.boundary_hash)
        assert extract_messages(continued.body, kind) == [*extract_messages(applied, kind), tail]
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["chat", "responses"])
@pytest.mark.parametrize("latest_revoked", [False, True])
def test_successive_candidates_match_restored_parent_history(runtime, kind, latest_revoked):
    from ai_router.history import apply_stored_history
    from ai_router.summary_provenance import SummaryScope, system_hashes
    async def scenario():
        decision = setup(runtime)
        rule = {"role": "system", "content": "Never restart without permission."}
        old = {"role": "user", "content": "first old evidence " * 100}
        more = {"role": "user", "content": "second old evidence " * 100}
        tail = {"role": "user", "content": "current request"}
        first_summary = {"role": "system", "content": "first owned summary"}
        final_summary = {"role": "system", "content": "latest combined summary"}
        async def candidate(branch, source, value, key_id):
            job_id = await submit_background(runtime, replace_messages({}, kind, source),
                owner="alice", key_id=key_id, branch=branch, api_kind=kind, decision=decision)
            jobs = runtime.compaction_worker.jobs
            assert jobs.claim("worker")["id"] == job_id
            jobs.candidate(job_id, "worker", value, summary_indices=(1,))
            return job_id
        first = await candidate("first", [rule, old], [rule, first_summary], "first-key")
        second = await candidate("second", [rule, first_summary, more], [rule, final_summary], "second-key")
        grandparent = SimpleNamespace(branch_id="first", conversation_id="chat", lineage_relation="new", parent_branch_id=None)
        parent = SimpleNamespace(branch_id="second", conversation_id="chat", lineage_relation="continuation",
            parent_branch_id="first", encrypted_capsule=runtime.compactor.cipher.encrypt([rule, first_summary, more]),
            boundary_hash=message_hash(more))
        runtime.conversations = SimpleNamespace(get=AsyncMock(return_value=grandparent))
        lineage = SimpleNamespace(relation="continuation", parent=parent, lineage_id="chat")
        runtime.clients.is_key_active.side_effect = lambda owner, key: not (latest_revoked and key == "second-key")
        incoming = replace_messages({}, kind, [rule, old, more, tail])
        original = copy.deepcopy(incoming)
        scope = SummaryScope(owner="alice", branch="child", ancestors=["second", "first"], api_kind=kind,
            cipher=runtime.compactor.cipher, protected=system_hashes(replace_messages({}, kind, [rule]), kind))
        scope.adopt_legacy_job(runtime.compaction_worker.jobs.ready("alice", "first")[0])
        compactor = ContextCompactor(runtime.token_counter, runtime.compactor.cipher,
            internal_base_url="http://unused.invalid", internal_api_key="", model_id="summary", client=AsyncMock())
        restored = await apply_stored_history(compactor, runtime.conversations, incoming,
            api_kind=kind, conversation=parent)
        applied, capsule = await apply_background(runtime, restored, owner="alice", branch="child", api_kind=kind,
            identity=SimpleNamespace(inject=lambda body, kind: body), lineage=lineage, summary_scope=scope)
        assert incoming == original
        assert capsule is None if latest_revoked else capsule.background_job_id == second
        assert extract_messages(applied, kind) == ([rule, first_summary, more, tail] if latest_revoked
                                                  else [rule, final_summary, tail])
        if capsule is not None:
            assert capsule.boundary_hash == message_hash(tail)
        assert scope.indices(extract_messages(applied, kind)) == {1}
    asyncio.run(scenario())
