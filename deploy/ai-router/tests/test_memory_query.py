import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ai_router.memory_query import rewrite_query


@pytest.fixture
def runtime(monkeypatch):
    endpoint = SimpleNamespace(id="summary", public_model="summary-public", cloud=True, enabled=True)
    policy = SimpleNamespace(routing_mode="inherit", local_only=False, max_parallel_requests=2,
                             rpm_limit=10, tpm_limit=10000)
    lease = SimpleNamespace(release=AsyncMock())
    current = SimpleNamespace(settings=SimpleNamespace(section=lambda name:
        {"history_query_rewrite_enabled": True} if name == "compaction" else {}),
        registry=SimpleNamespace(by_id=lambda name: endpoint),
        clients=SimpleNamespace(history_policy=AsyncMock(return_value=policy)),
        token_counter=SimpleNamespace(count_request=lambda body, kind: len(json.dumps(body, ensure_ascii=False)) // 3),
        policy=SimpleNamespace(choose=AsyncMock(return_value=SimpleNamespace(endpoint=endpoint))),
        scheduler=SimpleNamespace(begin_request=AsyncMock(return_value=lease)),
        limiter=SimpleNamespace(acquire_parallel=AsyncMock(return_value=True), release_parallel=AsyncMock(),
            check_rate_limits=AsyncMock(return_value=(True, None))),
        budget=SimpleNamespace(reserve=AsyncMock(return_value="reservation"), commit=AsyncMock(), release=AsyncMock()),
        compactor=SimpleNamespace(model_id="summary", client=None), internal_api_key="test-internal", audit=Mock())
    target = SimpleNamespace(base_url="https://summary.invalid/v1", model="actual-summary", api_key="test-key")
    monkeypatch.setattr("ai_router.api._acquire_internal_model", AsyncMock(return_value=target))
    return current, lease


@pytest.mark.parametrize("case", ["success", "503", "timeout", "truncated", "malformed", "tool", "long"])
def test_single_call_validation_cleanup_and_no_retry(runtime, case):
    async def scenario():
        current, lease = runtime
        requests = []
        async def handler(request):
            requests.append(request)
            if case == "timeout":
                raise httpx.ReadTimeout("synthetic timeout")
            if case == "503":
                return httpx.Response(503)
            content = json.dumps({"query": "部署 参数 配置 路径" if case != "long" else "x" * 1025})
            message = {"content": "invalid" if case == "malformed" else content}
            if case == "tool":
                message["tool_calls"] = [{"id": "forbidden"}]
            return httpx.Response(200, json={"choices": [{"finish_reason": "length" if case == "truncated" else "stop",
                                                         "message": message}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            current.compactor.client = client
            result, reason = await rewrite_query(current, "部署的配置在哪？ password=private782",
                client_id="alice", key_id="key", deadline=time.monotonic() + 5)
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert "private782" not in requests[0].content.decode()
        assert payload["model"] == "actual-summary" and payload["max_tokens"] == 256
        assert payload["temperature"] == 0
        assert "concrete domain nouns" in payload["messages"][0]["content"]
        assert len(payload["messages"]) == 2 and "tools" not in payload
        assert requests[0].headers["X-1Panel-Operation-Kind"] == "history_query_rewrite"
        assert bool(result) == (case == "success")
        if case == "success":
            assert reason == "rewritten"
        current.budget.commit.assert_awaited_once_with("reservation")
        lease.release.assert_awaited_once()
        current.limiter.release_parallel.assert_awaited_once()
        assert "private782" not in str(current.audit.write.call_args)
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["disabled", "permission", "input", "parallel", "rate", "revoked", "deadline"])
def test_admission_rejects_without_model_call(runtime, case):
    async def scenario():
        current, lease = runtime
        current.compactor.client = SimpleNamespace(post=AsyncMock())
        query, deadline = "部署配置在哪", time.monotonic() + 5
        if case == "disabled":
            current.settings = SimpleNamespace(section=lambda name: {})
        elif case == "permission":
            current.clients.history_policy.return_value = None
        elif case == "input":
            query = "长" * 9000
        elif case == "parallel":
            current.limiter.acquire_parallel.return_value = False
        elif case == "rate":
            current.limiter.check_rate_limits.return_value = (False, None)
        elif case == "revoked":
            current.clients.history_policy.side_effect = [current.clients.history_policy.return_value, None]
        elif case == "deadline":
            deadline = time.monotonic()
        result, _ = await rewrite_query(current, query, client_id="alice", key_id="key", deadline=deadline)
        assert result is None
        current.compactor.client.post.assert_not_awaited()
        current.budget.commit.assert_not_awaited()
        if case in {"parallel", "rate", "revoked"}:
            lease.release.assert_awaited_once()
        if case == "revoked":
            current.budget.release.assert_awaited_once_with("reservation")
    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["deepseek", "other"])
def test_keyword_response_controls_are_provider_specific(runtime, provider):
    async def scenario():
        current, _ = runtime
        current.registry.by_id("summary").metadata = {"provider": provider}
        def handler(request):
            payload = json.loads(request.content)
            if provider == "deepseek":
                assert payload["thinking"] == {"type": "disabled"}
                assert payload["response_format"] == {"type": "json_object"}
            else:
                assert "thinking" not in payload and "response_format" not in payload
            assert payload["max_tokens"] == 256
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": '{"query":"日志 校验码 摘要"}'}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            current.compactor.client = client
            result, reason = await rewrite_query(current, "检查记录指纹", client_id="alice", key_id="key",
                                                  deadline=time.monotonic() + 5)
        assert result and reason == "rewritten"
    asyncio.run(scenario())
