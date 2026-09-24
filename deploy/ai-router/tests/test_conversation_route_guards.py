"""Regressions for ambiguous directed histories and retired route targets."""
import asyncio
import json
import sqlite3
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_router.api import create_app
from ai_router.errors import NoEligibleModelError
from ai_router.history import persist_history
from ai_router.history_index import raw_aliases
from ai_router.policy import updated_conversation_state
from ai_router.types import ConversationState, Evaluation
from test_core import _public_test_runtime
from test_routing_modes import request, setup, status_for


run = asyncio.run


def messages():
    return [{"role": "user", "content": "Inspect the fixture."},
            {"role": "assistant", "content": "The fixture has been inspected."}]


async def seed(runtime, branch, *, lineage="session", directed=True, parent=None):
    endpoint = runtime.registry.by_id("codex-pro-gpt-6-astra")
    route = runtime.settings.section("routing")["prompt_directives"]["routes"]["beichen"]
    history = messages()
    state = ConversationState(
        lineage, endpoint.public_model, endpoint.id, endpoint.tier_rank, "general", time.time(),
        branch_id=branch, parent_branch_id=parent, provider_family="openai-codex",
        directive_id="beichen" if directed else None,
        directive_generation=route["generation"] if directed else 0,
        directive_endpoint_id=endpoint.id if directed else None,
    )
    await persist_history(runtime.compactor, runtime.conversations, state=state,
                          client_id="workbuddy-public", body={"messages": history[:1]},
                          api_kind="chat", assistant_message=history[1])
    trace = dict(request_id=branch, client_id="workbuddy-public", conversation_id=lineage,
                 protocol="chat", status="succeeded")
    archive = {
        "request": {k: trace[k] for k in ("request_id", "client_id", "conversation_id", "protocol")},
        "response": {"complete": True, "status_code": 200,
                     "body": {"encoding": "json", "value": {"choices": [{"message": history[1]}]}}},
        "pipeline": {"stages": [{"stage": "after_directives", "sha256": "fixture"}],
                     "bodies": {"fixture": {"messages": history[:1]}}},
    }
    await runtime.conversations.map_history("workbuddy-public", raw_aliases(archive, trace), branch)


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", ["directed", "mixed_directive", "overflow", "retired"])
def test_guard_rejects_before_upstream_with_real_lineage(tmp_path, monkeypatch, api_kind, stream, case):
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    monkeypatch.setenv("AI_ROUTER_ARCHIVE_QUEUE_ENABLED", "false")
    runtime, secret = _public_test_runtime(tmp_path, monkeypatch, client_id="workbuddy-public")
    runtime.settings.write_runtime({"identity": {"enabled": True},
                                   "routing": {"prompt_directives": {"enabled": True},
                                               "objectives": {"enabled": True, "mode": "efficiency"}}})
    run(seed(runtime, "first", directed=case != "retired"))
    if case != "retired":
        run(seed(runtime, "second", directed=case != "mixed_directive"))
    if case == "overflow":
        for i in range(17):
            run(seed(runtime, f"overflow-{i}"))
    if case == "retired":
        runtime.registry = runtime.registry.with_endpoints(
            [e for e in runtime.registry.endpoints if e.id != "codex-pro-gpt-6-astra"])
        runtime.policy.registry = runtime.registry
        # EndpointConfigManager reconstructs the request registry from its base.
        runtime.endpoint_configs.base_registry = runtime.registry

    calls = []

    async def unexpected(request):
        calls.append(request.url.path)
        raise AssertionError("guard must stop before inference")

    run(runtime.internal_client.aclose())
    runtime.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
    run(runtime.endpoint_token_counter.client.aclose())
    runtime.endpoint_token_counter.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"count": 100, "token_ids": [1] * 100})))
    body = {"model": "siyuan/auto", "stream": stream,
            "messages" if api_kind == "chat" else "input": [*messages(), {"role": "user", "content": "Continue."}]}
    with TestClient(create_app(runtime)) as client:
        response = client.post("/v1/chat/completions" if api_kind == "chat" else "/v1/responses",
                               headers={"Authorization": "Bearer " + secret}, json=body)
        expected = "no_eligible_model" if case == "retired" else "conversation_state_conflict"
        assert response.status_code == (503 if case == "retired" else 409), response.text
        assert response.json()["error"]["code"] == expected
        for private in ("codex", "astra", "endpoint", "beichen", "openai"):
            assert private not in response.text.lower()
        assert calls == []
        with sqlite3.connect(runtime.route_traces.database_path) as db:
            trace = json.loads(db.execute("SELECT payload_json FROM route_traces ORDER BY started_at DESC LIMIT 1").fetchone()[0])
        assert trace["status"] == "failed"
        assert trace["error"]["code"] == expected
        if case != "retired":
            assert trace["history_match"]["reason"] == "ambiguous_history"
            assert trace["history_match"]["directed_history"]
        else:
            assert trace["history_match"]["status"] == "verified"
    run(runtime.close())


@pytest.mark.parametrize("mode", ["efficiency", "quality", "cost"])
@pytest.mark.parametrize("explicit", ["none", "model", "directive", "pin"])
def test_retired_endpoint_stops_auto_but_allows_explicit_selection(tmp_path, mode, explicit):
    policy, registry, options = setup(tmp_path, "quality")
    previous = run(request(policy, options, evaluation=Evaluation("code", None, 1, "test", route_profile="code")))
    assert previous.endpoint.id == "codex-pro-gpt-6-astra"
    state = updated_conversation_state(None, conversation_id="session", branch_id="first",
                                       decision=previous, cache_generation="fixture")
    policy.registry = registry.with_endpoints([e for e in registry.endpoints if e.id != state.endpoint_id])
    target = registry.by_id("cloud-deepseek-v4-flash")
    options["mode"] = mode
    kwargs = {"conversation": state}
    if explicit == "none":
        with pytest.raises(NoEligibleModelError, match="no longer registered"):
            run(request(policy, options, **kwargs))
        return
    if explicit == "model":
        kwargs["requested_model"] = target.public_model
    elif explicit == "directive":
        kwargs["evaluation"] = Evaluation("general", None, 1, "test", required_endpoint_id=target.id)
    else:
        kwargs["conversation_control"] = {"pin": {"endpoint_id": target.id}}
    # Keep the explicit target's tier admissible; the regression is the missing ID,
    # not authorization to downgrade across an existing hard tier constraint.
    kwargs["conversation"] = replace(state, tier_rank=target.tier_rank)
    assert run(request(policy, options, **kwargs)).endpoint.id == target.id


@pytest.mark.parametrize("case,blocked", [("same", True), ("mixed", True), ("other", False), ("automatic", False)])
def test_ambiguity_retains_evidence_without_inheriting_a_branch(tmp_path, monkeypatch, case, blocked):
    runtime, _ = _public_test_runtime(tmp_path, monkeypatch, client_id="workbuddy-public")
    run(seed(runtime, "first", directed=case != "automatic"))
    run(seed(runtime, "second", lineage="other-session" if case == "other" else "session",
             directed=case not in {"automatic", "mixed"}))
    from ai_router.history import history_lookup_identities
    ids = tuple("wb-raw-v1:" + key for key in history_lookup_identities(
        [*messages(), {"role": "user", "content": "Continue."}]))
    parent, evidence = run(runtime.conversations.verified_history_match("workbuddy-public", ids))
    assert parent is None
    assert evidence["reason"] == "ambiguous_history"
    assert bool(evidence["directed_history"] and evidence["candidate_conversation_count"] == 1) is blocked
    run(runtime.close())
