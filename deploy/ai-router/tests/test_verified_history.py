import asyncio
import copy
import time
from types import SimpleNamespace as NS

import pytest

from ai_router.history import history_identities, history_lookup_identities, verified_history_identity
from ai_router.history_index import raw_aliases
from ai_router.policy import ConversationRepository
from ai_router.store import InMemoryStateStore
from ai_router.types import ConversationState


def run(value):
    return asyncio.run(value)


def histories():
    calls = [{"id": name, "type": "function", "function": {"name": "read", "arguments": '{"path":"' + name + '"}'}} for name in ("a", "b")]
    outputs = [{"role": "tool", "tool_call_id": c["id"], "content": c["id"] + " result"} for c in calls]
    opening = [{"role": "system", "content": "rules"}, {"role": "user", "content": [{"type": "text", "text": "task"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    grouped = [*opening, {"role": "assistant", "content": "I will inspect both files.", "tool_calls": calls}, *outputs]
    split = [*copy.deepcopy(opening), {"role": "assistant", "content": "I will inspect both files.", "tool_calls": [copy.deepcopy(calls[0])]}, copy.deepcopy(outputs[0]), {"role": "assistant", "content": None, "tool_calls": [copy.deepcopy(calls[1])]}, copy.deepcopy(outputs[1])]
    return grouped, split


def repository():
    return ConversationRepository(InMemoryStateStore(), NS(section=lambda _: {}))


async def save(repo, branch, endpoint="AI", conversation="task"):
    await repo.save(ConversationState(conversation, "auto", endpoint, 1, "code", time.time(), branch_id=branch))


def test_prose_tool_grouping_identity_preserves_input():
    grouped, split = histories()
    before = copy.deepcopy([grouped, split])
    assert verified_history_identity(grouped) == verified_history_identity(split)
    assert "v5-history-strong-" in verified_history_identity(grouped)
    assert [grouped, split] == before


@pytest.mark.parametrize("field", ["prose", "id", "argument", "output", "image", "name", "reasoning", "order", "duplicate"])
def test_semantic_changes_are_not_equivalent(field):
    a, b = histories()
    if field == "prose": b[2]["content"] += " changed"
    if field == "id": b[2]["tool_calls"][0]["id"] += "changed"
    if field == "argument": b[2]["tool_calls"][0]["function"]["arguments"] = '{"path":"other"}'
    if field == "output": b[3]["content"] += " changed"
    if field == "image": b[1]["content"][1]["image_url"]["url"] += "BBBB"
    if field == "name": b[2]["name"] = "another"
    if field == "reasoning": b[2]["reasoning_content"] = "additional reasoning"
    if field == "order": a[2]["tool_calls"].reverse()
    if field == "duplicate": b[4]["tool_calls"][0]["id"] = "a"
    assert verified_history_identity(a) != verified_history_identity(b)


def test_unique_longest_history_selects_ai_instead_of_opening_amd():
    async def case():
        repo = repository(); a, b = histories()
        await save(repo, "old", "AMD"); await save(repo, "latest", "AI")
        await repo.map_history("wb", history_identities(a[:2]), "old")
        await repo.map_history("wb", history_identities(a), "latest")
        parent, evidence = await repo.verified_history_match("wb", history_lookup_identities([*b, {"role": "user", "content": "continue"}]))
        assert parent.branch_id == "latest" and parent.endpoint_id == "AI"
        assert evidence["status"] == "verified"
    run(case())


def test_opening_legacy_cross_client_and_ambiguity_do_not_inherit_state():
    async def case():
        repo = repository(); a, b = histories(); await save(repo, "old", "AMD")
        await repo.map_history("wb", history_identities(a[:2]), "old")
        ids = history_lookup_identities([*b, {"role": "user", "content": "next"}])
        assert (await repo.verified_history_match("wb", ids))[0] is None
        await repo.map_history("wb", history_identities(a)[1:], "old")
        assert (await repo.verified_history_match("wb", ids))[0] is None
        await repo.map_history("wb", history_identities(a), "old")
        assert (await repo.verified_history_match("other", ids))[0] is None
        await save(repo, "parallel", "Edge")
        await asyncio.gather(repo.map_history("wb", history_identities(a), "parallel"), repo.map_history("wb", history_identities(a), "old"))
        parent, report = await repo.verified_history_match("wb", ids)
        assert parent is None and report["reason"] == "ambiguous_history"
    run(case())


def test_old_ancestor_is_not_replaced_with_latest_device_without_proof():
    async def case():
        repo = repository(); a, b = histories()
        await save(repo, "old", "AMD"); await save(repo, "latest", "AI")
        await repo.map_history("wb", history_identities(a), "old")
        await repo.map_lineage("wb", "task", "latest")
        parent, report = await repo.verified_history_match("wb", history_lookup_identities([*b, {"role": "user", "content": "changed history"}]))
        assert parent is None and report["reason"] == "historical_prefix_only"
    run(case())


def test_expired_branch_cannot_supply_affinity():
    async def case():
        repo = repository(); a, b = histories(); await save(repo, "old")
        await repo.map_history("wb", history_identities(a), "old")
        await repo.store.delete("router:conversation-branch:old")
        assert (await repo.verified_history_match("wb", history_lookup_identities([*b, {"role": "user", "content": "next"}])))[0] is None
    run(case())


def test_completed_archive_aliases_include_response_without_rewriting():
    a, _ = histories(); response = {"role": "assistant", "content": "finished"}
    trace = {"request_id": "request", "client_id": "workbuddy-public", "status": "succeeded"}
    archive = {"request": {**trace, "protocol": "chat"}, "response": {"status_code": 200, "complete": True, "assistant_items": [response]},
               "pipeline": {"stages": [{"stage": "after_directives", "sha256": "body"}], "bodies": {"body": {"messages": a}}}}
    before = copy.deepcopy(archive)
    assert "wb-raw-v1:" + verified_history_identity([*a, response]) in raw_aliases(archive, trace)
    assert archive == before
    archive["response"]["complete"] = False
    assert raw_aliases(archive, trace) == ()
    archive["response"]["complete"] = True
    assert raw_aliases(archive, {**trace, "client_id": "other"}) == ()


def test_pool_owner_is_conversation_not_per_turn_branch():
    from tests.test_local_pool import fixtures, trace
    pool, _, _ = fixtures()
    a = NS(conversation_id="task", branch_id="turn-1")
    b = NS(conversation_id="task", branch_id="turn-2")
    assert pool.identity(trace("r1"), a) == pool.identity(trace("r2"), b)
    assert pool.identity(trace("r1"), a) != pool.identity(trace("r2", "other"))


def test_plain_text_transport_blocks_match_without_discarding_rich_content():
    plain = [{"role": "user", "content": "ask"}, {"role": "assistant", "content": "answer"}]
    blocks = copy.deepcopy(plain)
    blocks[1]["content"] = [{"type": "text", "text": "answer", "annotations": []}]
    assert verified_history_identity(plain) == verified_history_identity(blocks)
    blocks[1]["content"][0]["annotations"] = [{"type": "citation", "id": "source"}]
    assert verified_history_identity(plain) != verified_history_identity(blocks)


def test_lineage_context_rejects_weak_history_and_reports_evidence():
    async def case():
        repo = repository(); a, _ = histories(); await save(repo, "old", "AMD")
        await repo.map_history("wb", history_identities(a[:2]), "old")
        lineage = await repo.lineage_context(client_id="wb", identities=history_lookup_identities(a), explicit_lineage_id=None, previous_response_id=None, force_new=False)
        assert lineage.parent is None and lineage.relation == "new"
        assert lineage.history_match["status"] == "unconfirmed"
        await repo.map_lineage("wb", "task", "old")
        explicit = await repo.lineage_context(client_id="wb", identities=(), explicit_lineage_id="task", previous_response_id=None, force_new=False)
        assert explicit.parent.endpoint_id == "AMD"
        assert explicit.history_match["source"] == "explicit_conversation_id"
    run(case())


def test_bounded_index_preserves_ambiguity_and_duplicates_do_not_overflow():
    async def case():
        repo = repository(); key = "history-candidates"
        await asyncio.gather(*(repo.store.add_history_candidate(key, "same", 60) for _ in range(20)))
        assert await repo.store.get_json(key) == {"branches": ["same"], "overflow": False}
        await asyncio.gather(*(repo.store.add_history_candidate(key, str(i), 60) for i in range(20)))
        value = await repo.store.get_json(key)
        assert len(value["branches"]) == 16 and value["overflow"] is True
    run(case())


@pytest.mark.parametrize("relation,expected", [("siblings", True), ("descendant_pointer", False), ("ancestor_pointer", True), ("unrelated", False), ("cycle", False)])
def test_parallel_completion_order_uses_actual_ancestry(relation, expected):
    async def case():
        repo = repository()
        a, _ = histories()
        async def branch(bid, parent, endpoint="AI"):
            await repo.save(ConversationState("task", "auto", endpoint, 1, "code", time.time(), branch_id=bid, parent_branch_id=parent))
        await branch("matched", "root")
        await branch("latest", "root", "Edge")
        if relation == "descendant_pointer": await branch("latest", "matched", "Edge")
        if relation == "ancestor_pointer": await branch("matched", "latest")
        if relation == "unrelated": await branch("latest", "other-root", "Edge")
        if relation == "cycle": await branch("latest", "latest", "Edge")
        await repo.map_history("wb", history_identities(a), "matched")
        await repo.map_lineage("wb", "task", "latest")
        parent, evidence = await repo.verified_history_match("wb", history_lookup_identities([*a, {"role": "user", "content": "continue A"}]))
        assert (parent is not None) is expected
        if expected:
            assert parent.branch_id == "matched" and parent.endpoint_id == "AI"
            assert evidence["status"] == "verified"
        else:
            assert evidence["status"] == "unconfirmed"
    run(case())


def test_parallel_siblings_remain_independent_through_interleaved_rounds():
    async def case():
        repo = repository()
        histories_by_branch = {"a": [{"role": "user", "content": "A"}], "b": [{"role": "user", "content": "B"}]}
        tips = {"a": "root", "b": "root"}
        for turn in range(5):
            for branch_name, endpoint in [("a", "AI"), ("b", "Edge")]:
                history = histories_by_branch[branch_name]
                history.append({"role": "assistant", "content": f"{branch_name} answer {turn}"})
                bid = f"{branch_name}-{turn}"
                await repo.save(ConversationState("task", "auto", endpoint, 1, "code", time.time(), branch_id=bid, parent_branch_id=tips[branch_name]))
                tips[branch_name] = bid
                await repo.map_history("wb", history_identities(history), bid)
                await repo.map_lineage("wb", "task", bid)
            for branch_name, endpoint in [("a", "AI"), ("b", "Edge")]:
                history = histories_by_branch[branch_name]
                parent, _ = await repo.verified_history_match("wb", history_lookup_identities([*history, {"role": "user", "content": "continue"}]))
                assert parent.branch_id == tips[branch_name] and parent.endpoint_id == endpoint
                history.append({"role": "user", "content": f"next {turn}"})
    run(case())
