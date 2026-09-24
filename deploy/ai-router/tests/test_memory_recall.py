import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from ai_router.memory_index import MemoryIndex, MemorySource
from ai_router.memory_recall import prepare_recall, recall_for_send
from ai_router.memory_sources import RECALL_MARKER
from ai_router.scheduler import ClientLimiter
from ai_router.store import InMemoryStateStore


@pytest.fixture
def setup(tmp_path):
    index = MemoryIndex(tmp_path / "index.sqlite3", Fernet.generate_key().decode())
    index.add([MemorySource("alice", "old-chat", "old-request", "old-message", "user",
                            "E_MEMORY_782 的配置路径是 /srv/router/config.yaml", 123, True)])
    count = lambda body, kind: len(json.dumps(body, ensure_ascii=False))
    async def endpoint_count(endpoint, body, kind, fallback):
        return {"tokens": fallback}
    current = SimpleNamespace(history_memory=SimpleNamespace(index=index),
        clients=SimpleNamespace(history_policy=AsyncMock(return_value=SimpleNamespace(tpm_limit=100000))),
        token_counter=SimpleNamespace(count_request=count),
        endpoint_token_counter=SimpleNamespace(count=endpoint_count),
        limiter=ClientLimiter(InMemoryStateStore()))
    identity = SimpleNamespace(inject=lambda body, kind: copy.deepcopy(body))
    decision = SimpleNamespace(endpoint=SimpleNamespace(cloud=True, safe_context_tokens=16000,
        backend_type="openai", node="test"),
        deployment_safe_context_tokens=None, prompt_tokens=100, output_reserve_tokens=100,
        recall_projection=None)
    return current, identity, decision


def prepare(setup, body, kind="chat"):
    current, identity, decision = setup
    decision.prompt_tokens = current.token_counter.count_request(body, kind)
    return prepare_recall(current, body, api_kind=kind, decision=decision,
                          identity=identity, client_id="alice", key_id="active-key")


@pytest.mark.parametrize("kind,body", [
    ("chat", {"messages": [{"role": "system", "content": "current rules"},
                          {"role": "user", "content": "E_MEMORY_782 配置在哪里？"}]}),
    ("responses", {"input": "E_MEMORY_782 配置在哪里？"}),
])
def test_projection_is_transient_bounded_and_keeps_current_question_last(setup, kind, body):
    async def scenario():
        original = copy.deepcopy(body)
        projection, reason = await prepare(setup, body, kind)
        assert reason == "prepared" and projection
        assert body == original and RECALL_MARKER not in json.dumps(body)
        assert 0 < projection.added_tokens <= 1600
        assert len(projection.sources) == 1
        messages = projection.body["messages" if kind == "chat" else "input"]
        assert "E_MEMORY_782" in messages[-1]["content"]
        assert RECALL_MARKER in messages[-2]["content"]
        assert "old-request" in messages[-2]["content"]
        setup[2].recall_projection = projection
        forwarded, state = await recall_for_send(setup[0], body, decision=setup[2])
        assert state == "injected" and forwarded == projection.body
        assert body == original
    asyncio.run(scenario())


@pytest.mark.parametrize("change,expected", [
    ("revoke", "permission_revoked"), ("exclude", "source_changed_or_excluded"),
    ("history", "history_changed"), ("timeout", "validation_timeout"),
])
def test_send_revalidates_permissions_sources_and_base_history(setup, change, expected):
    async def scenario():
        current, _, decision = setup
        body = {"messages": [{"role": "user", "content": "E_MEMORY_782"}]}
        projection, _ = await prepare(setup, body)
        decision.recall_projection = projection
        if change == "revoke":
            current.clients.history_policy.return_value = None
        elif change == "exclude":
            current.history_memory.index.exclude("alice", "old-chat", True)
        elif change == "history":
            body["messages"].append({"role": "user", "content": "new correction"})
        else:
            decision.recall_projection = replace(projection, deadline=0)
        forwarded, state = await recall_for_send(current, body, decision=decision)
        assert state == expected and forwarded is body
    asyncio.run(scenario())


@pytest.mark.parametrize("case,expected", [("nohit", "no_trustworthy_match"),
    ("disabled", "not_authorized"), ("small", "no_context_headroom"),
    ("tpm", "tpm_limit_exceeded")])
def test_optional_recall_failure_leaves_original_untouched(setup, case, expected):
    async def scenario():
        current, _, decision = setup
        body = {"messages": [{"role": "user", "content": "E_MEMORY_782"}]}
        if case == "nohit":
            body["messages"][0]["content"] = "NO_MATCH_98543"
        elif case == "disabled":
            current.clients.history_policy.return_value = None
        elif case == "small":
            decision.endpoint.safe_context_tokens = 200
        elif case == "tpm":
            current.clients.history_policy.return_value = SimpleNamespace(tpm_limit=1)
        original = copy.deepcopy(body)
        projection, reason = await prepare(setup, body)
        assert projection is None and reason == expected and body == original
    asyncio.run(scenario())


def test_recall_does_not_consume_an_extra_rpm_request():
    async def scenario():
        limiter = ClientLimiter(InMemoryStateStore())
        assert await limiter.check_additional_tokens("alice", 5, 10)
        allowed, _ = await limiter.check_rate_limits("alice", prompt_tokens=5, rpm_limit=1, tpm_limit=10)
        assert allowed
        assert not await limiter.check_additional_tokens("alice", 1, 10)
    asyncio.run(scenario())


@pytest.mark.parametrize("error,reason", [("interrupted", "query_timeout"), ("database is locked", "database_busy")])
def test_recall_reports_specific_sql_failure(setup, monkeypatch, error, reason):
    import sqlite3
    current, _, decision = setup
    decision.trace = SimpleNamespace(payload={})
    def fail(*args, **kwargs):
        kwargs["diagnostics"]["stage"] = "candidates"
        raise sqlite3.OperationalError(error)
    monkeypatch.setattr(current.history_memory.index, "search", fail)
    projection, state = asyncio.run(prepare(setup, {"messages": [{"role": "user", "content": "E_MEMORY_782"}]}))
    assert projection is None and state == reason
    assert decision.trace.payload["observation"]["history_search"][0]["stage"] == "candidates"
    current.recall_executor.close()


def test_cancelled_recall_stops_sql_worker_and_frees_slot(setup):
    import threading
    import time
    from ai_router.memory_recall import search_history
    from ai_router.compute import BoundedExecutor
    async def scenario():
        current, _, decision = setup
        current.recall_executor = BoundedExecutor(1, "test-recall")
        entered, exited = threading.Event(), threading.Event()
        def blocked(*args, cancel_event, **kwargs):
            entered.set()
            assert cancel_event.wait(2)
            exited.set()
            return []
        task = asyncio.create_task(search_history(current, SimpleNamespace(search=blocked), "alice", "query",
                                                 decision=decision, deadline=time.monotonic()+5))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(exited.wait, 1)
        assert await asyncio.wait_for(current.recall_executor.run(lambda: "next"), timeout=1) == "next"
        current.recall_executor.close()
    asyncio.run(scenario())


def test_nohit_rewrite_runs_once_and_preserves_current_body(setup, monkeypatch):
    async def scenario():
        current, _, _ = setup
        body = {"messages": [{"role": "user", "content": "上回那个文件放哪儿了？"}]}
        original = copy.deepcopy(body)
        assert not current.history_memory.index.search("alice", body["messages"][0]["content"], cloud=True)
        rewrite = AsyncMock(return_value=("配置 路径", "rewritten"))
        monkeypatch.setattr("ai_router.memory_recall.rewrite_query", rewrite)
        projection, reason = await prepare(setup, body)
        assert reason == "prepared" and projection
        rewrite.assert_awaited_once()
        assert body == original
        assert projection.sources[0].source.request_id == "old-request"
        rewrite.reset_mock()
        rewrite.return_value = ("NO_MATCH_78213", "rewritten")
        projection, reason = await prepare(setup, body)
        assert projection is None and reason == "rewrite_rewritten_no_match"
        rewrite.assert_awaited_once()
    asyncio.run(scenario())


def test_direct_match_never_calls_query_rewriter(setup, monkeypatch):
    async def scenario():
        rewrite = AsyncMock()
        monkeypatch.setattr("ai_router.memory_recall.rewrite_query", rewrite)
        projection, reason = await prepare(setup, {"messages": [{"role": "user", "content": "E_MEMORY_782"}]})
        assert projection and reason == "prepared"
        rewrite.assert_not_awaited()
    asyncio.run(scenario())


def test_unknown_explicit_identifier_does_not_recall_another_errors_config(setup):
    async def scenario():
        body = {"messages": [{"role": "user", "content": "NO_MATCH_98543 的配置路径在哪里？"}]}
        projection, reason = await prepare(setup, body)
        assert projection is None and reason == "no_trustworthy_match"
    asyncio.run(scenario())


def test_rewriter_cannot_drop_an_unknown_identifier_to_inject_generic_history(setup, monkeypatch):
    async def scenario():
        rewrite = AsyncMock(return_value=("配置 路径", "rewritten"))
        monkeypatch.setattr("ai_router.memory_recall.rewrite_query", rewrite)
        body = {"messages": [{"role": "user", "content": "NO_MATCH_98543 配置路径"}]}
        projection, reason = await prepare(setup, body)
        assert projection is None and reason == "rewrite_rewritten_no_match"
        rewrite.assert_awaited_once()
    asyncio.run(scenario())


def test_oversized_archived_log_recalls_detail_missing_from_summary(setup):
    import hashlib
    from ai_router.memory_sources import archived_sources
    async def scenario():
        current, _, decision = setup
        current.token_counter.count_request = lambda body, kind: len(json.dumps(body, ensure_ascii=False)) // 4
        rows = [f"事件 {n:06d}：节点 node-{n % 17:02d} 完成例行检查；摘要 {hashlib.sha256(str(n).encode()).hexdigest()[:12]}。\n"
                for n in range(10090)]
        payload = {"request": {"client_id": "alice", "conversation_id": "source-long-chat",
            "request_id": "source-long-request", "protocol": "chat", "created_at": 123,
            "history_source_policy": {"version": 1, "local_only": False}, "received_body": {"messages": [
                {"role": "tool", "tool_call_id": "original-log", "content": "".join(rows)}]}}}
        current.history_memory.index.add(archived_sources(payload, client_id="alice", cloud_allowed=True))
        body = {"messages": [{"role": "user", "content": "事件 004321 的摘要值是什么？"}]}
        projection, reason = await prepare(setup, body)
        assert projection and reason == "prepared"
        assert hashlib.sha256(b"4321").hexdigest()[:12] in json.dumps(projection.body, ensure_ascii=False)
        assert any(hit.source.request_id == "source-long-request" for hit in projection.sources)
        assert len(projection.sources) <= 6 and projection.added_tokens <= 1600
        decision.recall_projection = projection
        current.history_memory.index.exclude("alice", "source-long-chat", True)
        forwarded, reason = await recall_for_send(current, body, decision=decision)
        assert forwarded is body and reason == "source_changed_or_excluded"
    asyncio.run(scenario())


def test_legacy_source_grant_is_checked_again_before_send(setup):
    async def scenario():
        current, _, decision = setup
        current.history_memory.index.add([MemorySource("alice", "legacy-chat", "legacy-request", "legacy-message",
            "user", "E_LEGACY_718 配置路径", 123, cloud_unknown=True)])
        body = {"messages": [{"role": "user", "content": "E_LEGACY_718"}]}
        projection, _ = await prepare(setup, body)
        assert projection is None
        policy = current.clients.history_policy.return_value
        policy.history_legacy_cloud_allowed = True
        projection, reason = await prepare(setup, body)
        assert projection and reason == "prepared"
        assert projection.sources[0].source.cloud_unknown
        decision.recall_projection = projection
        policy.history_legacy_cloud_allowed = False
        forwarded, reason = await recall_for_send(current, body, decision=decision)
        assert forwarded is body and reason == "source_changed_or_excluded"
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,adapter", [("chat", False), ("responses", False), ("responses", True)])
def test_actual_send_uses_projection_without_mutating_history_body(setup, kind, adapter):
    import httpx
    from starlette.requests import Request
    from ai_router.api import _send_upstream
    async def scenario():
        current, identity, decision = setup
        body = ({"messages": [{"role": "user", "content": "E_MEMORY_782"}]} if kind == "chat"
                else {"input": "E_MEMORY_782"})
        original = copy.deepcopy(body)
        decision.recall_projection, _ = await prepare(setup, body, kind)
        decision.native_or_adapter = "adapter" if adapter else "native"
        decision.upstream_api_base = None
        decision.endpoint.metadata = {}
        decision.endpoint.id = "selected-test-model"
        decision.endpoint.provider_model = "test-provider-model"
        decision.trace = None
        decision.attempts = 1
        decision.deployment_id = None
        current.internal_api_key = ""
        current.internal_base_url = "http://isolated-test.invalid"
        current.training = None
        current.scheduler = SimpleNamespace(admission=SimpleNamespace(enabled=False))
        captured = []
        def upstream(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "test-response"})
        current.internal_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
        try:
            response = await _send_upstream(current, request, body, api_kind=kind,
                                             decision=decision, identity=identity)
            assert response.status_code == 200
            assert RECALL_MARKER in json.dumps(captured[0])
            assert captured[0]["model"] == "selected-test-model"
            assert body == original and RECALL_MARKER not in json.dumps(body)
            await response.aclose()
        finally:
            await current.internal_client.aclose()
    asyncio.run(scenario())
@pytest.mark.parametrize('mode', ['seed', 'update', 'continuation', 'responses'])
def test_catalog_is_not_used_as_recall_question(setup, mode):
    from ai_router.workbuddy_history import seed_tools, reconcile_tools
    from ai_router.memory_sources import visible_message
    raw = {'model': 'siyuan/auto', 'messages': [
        {'role': 'system', 'content': 'Rules'},
        {'role': 'user', 'content': 'E_MEMORY_782 配置在哪里？'}],
        'tools': [{'type': 'function', 'function': {'name': 'Skill',
                   'description': 'Available image skills', 'parameters': {'type': 'object'}}}]}
    body, report = seed_tools(raw, 'workbuddy-public')
    if mode in ('update', 'continuation'):
        fresh = copy.deepcopy(raw)
        fresh['messages'] += [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'c1', 'type': 'function', 'function': {'name': 'Skill', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'tool output'}]
        if mode == 'update':
            fresh['tools'][0]['function']['description'] = 'Available browser skills'
        body, _ = reconcile_tools(fresh, 'workbuddy-public', raw, body, report['positions'])
    kind = 'responses' if mode == 'responses' else 'chat'
    if kind == 'responses':
        body = {'input': body['messages']}
    original = copy.deepcopy(body)
    queries = []
    search = setup[0].history_memory.index.search
    def capture(client, query, **kwargs):
        queries.append(query)
        return search(client, query, **kwargs)
    setup[0].history_memory.index.search = capture
    async def scenario():
        projection, reason = await prepare(setup, body, kind)
        assert reason == 'prepared'
        assert queries == ['E_MEMORY_782 配置在哪里？']
        assert body == original
        messages = projection.body['input' if kind == 'responses' else 'messages']
        question = next(i for i, m in enumerate(messages) if visible_message(m)[1] == queries[0])
        assert RECALL_MARKER in messages[question - 1]['content']
        assert messages[:question - 1] + messages[question:] == original['input' if kind == 'responses' else 'messages']
    asyncio.run(scenario())


def test_catalog_only_has_no_recall_query(setup):
    from ai_router.workbuddy_history import TOOLS_MARKER
    body = {'messages': [{'role': 'user', 'content': TOOLS_MARKER + '\n{}\n</workbuddy_tool_catalog>'}]}
    projection, reason = asyncio.run(prepare(setup, body))
    assert projection is None and reason == 'no_query'
