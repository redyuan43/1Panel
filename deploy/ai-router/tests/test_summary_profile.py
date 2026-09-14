import asyncio
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx

from ai_router.compaction import ContextCompactor
from ai_router.compaction_worker import CompactionWorker, RoutedCompactor, validate_background_settings
from ai_router.config import Settings, validate_settings
from ai_router.errors import CompactionUnavailableError
from ai_router.summary_profile import SummaryProfile, reasoning_mode, summary_profile
from ai_router.types import ModelCallTarget
from test_compaction_worker import prepare, runtime


@pytest.mark.parametrize("mode", ["provider_default", "disabled", "low"])
def test_supported_modes_are_validated(mode, tmp_path):
    assert reasoning_mode(mode) == mode
    settings = Settings(runtime_path=tmp_path / "settings.yaml").value
    settings["compaction"]["summary_reasoning"] = mode
    validate_settings(settings)


@pytest.mark.parametrize("mode", [None, True, 0, [], {}, "", "high", "LOW"])
def test_invalid_modes_are_rejected(mode, tmp_path):
    settings = Settings(runtime_path=tmp_path / "settings.yaml").value
    settings["compaction"]["summary_reasoning"] = mode
    with pytest.raises(ValueError, match="summary_reasoning"):
        reasoning_mode(mode)
    with pytest.raises(ValueError, match="summary_reasoning"):
        validate_settings(settings)


@pytest.mark.parametrize("provider", ["", "openai", "DeepSeek", None])
@pytest.mark.parametrize("mode", ["disabled", "low"])
def test_overrides_require_exact_deepseek_provider(provider, mode):
    endpoint = SimpleNamespace(metadata={"provider": provider})
    with pytest.raises(ValueError, match="DeepSeek"):
        summary_profile({"summary_reasoning": mode}, endpoint)
    with pytest.raises(ValueError, match="DeepSeek"):
        validate_background_settings({"compaction": {"summary_reasoning": mode}},
            SimpleNamespace(by_id=lambda name: endpoint))


def test_default_is_provider_independent_and_immutable():
    for endpoint in (None, SimpleNamespace(), SimpleNamespace(metadata={"provider": "deepseek"})):
        assert summary_profile({}, endpoint) == SummaryProfile()
    profile = SummaryProfile("low", "deepseek")
    with pytest.raises(FrozenInstanceError):
        profile.mode = "disabled"
    fields = profile.request_fields()
    fields["thinking"]["type"] = "disabled"
    assert profile.request_fields()["thinking"]["type"] == "enabled"


@pytest.mark.parametrize("mode,fields", [
    ("provider_default", {}),
    ("disabled", {"thinking": {"type": "disabled"}}),
    ("low", {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
])
@pytest.mark.parametrize("target", [None, ModelCallTarget("http://isolated.invalid", "actual-model", "")])
def test_request_profile_preserves_model_and_output_budget(mode, fields, target):
    kwargs = dict(internal_base_url="http://isolated.invalid", internal_api_key="",
        model_id="summary", client=Mock())
    baseline = ContextCompactor(Mock(), Mock(), **kwargs)
    selected = ContextCompactor(Mock(), Mock(), **kwargs,
        summary_profile=SummaryProfile(mode, "deepseek"))
    messages = [{"role": "user", "content": "source"}]
    expected = baseline._summary_request(messages, target)
    assert "thinking" not in expected and "reasoning_effort" not in expected
    request = selected._summary_request(messages, target)
    assert request == {**expected, **fields}
    assert request["model"] == (target.model if target else "summary")
    assert request["max_tokens"] == expected["max_tokens"] == 8192


@pytest.mark.parametrize("change_at", ["before_admission", "during_admission", "legacy_default"])
def test_job_profile_is_pinned_and_changes_fail_closed(runtime, monkeypatch, change_at):
    async def scenario():
        worker, job = prepare(runtime)
        worker.jobs.claim("worker")
        config = runtime.settings.section("compaction")
        if change_at != "legacy_default":
            config["summary_reasoning"] = "low"
            job["parameters"]["summary_reasoning"] = "low"
        endpoint = SimpleNamespace(id="summary", public_model="summary", enabled=True,
            cloud=False, metadata={"provider": "deepseek"})
        runtime.registry.by_id = lambda name: endpoint
        runtime.clients = SimpleNamespace(current_policy=AsyncMock(return_value=SimpleNamespace(
            allow_compaction=True, local_only=False, routing_mode="inherit", rpm_limit=10,
            tpm_limit=10000, max_parallel_requests=1)), is_key_active=AsyncMock(return_value=True))
        runtime.policy = SimpleNamespace(choose=AsyncMock(return_value=SimpleNamespace(endpoint=endpoint)))
        lease = SimpleNamespace(release=AsyncMock())
        runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
        runtime.limiter = SimpleNamespace(check_rate_limits=AsyncMock(return_value=(True, None)),
            acquire_parallel=AsyncMock(return_value=True), release_parallel=AsyncMock())
        runtime.budget = SimpleNamespace(reserve=AsyncMock(return_value=None),
            commit=AsyncMock(), release=AsyncMock())
        runtime.token_counter.count_request.return_value = 100
        monkeypatch.setattr("ai_router.api._acquire_internal_model", AsyncMock(
            return_value=ModelCallTarget("http://isolated.invalid", "summary", "")))
        response = AsyncMock(return_value={"facts": ["saved"]})
        monkeypatch.setattr("ai_router.compaction_checkpoint.CheckpointedCompactor._summarize", response)
        value = RoutedCompactor(runtime.token_counter, runtime.compactor.cipher,
            jobs=worker.jobs, job=job, worker="worker", runtime=runtime,
            internal_base_url=runtime.internal_base_url, internal_api_key="",
            model_id="summary", client=Mock())
        if change_at == "before_admission":
            config["summary_reasoning"] = "disabled"
        elif change_at == "during_admission":
            async def change_profile(*args, **kwargs):
                config["summary_reasoning"] = "disabled"
            runtime.budget.reserve.side_effect = change_profile
        messages = [{"role": "user", "content": "source"}]
        if change_at == "legacy_default":
            assert "summary_reasoning" not in job["parameters"]
            assert value.summary_profile == SummaryProfile()
            assert await value._summarize(messages) == {"facts": ["saved"]}
            response.assert_awaited_once()
        else:
            with pytest.raises(CompactionUnavailableError, match="summary reasoning changed"):
                await value._summarize(messages)
            assert value.summary_profile == SummaryProfile("low", "deepseek")
            response.assert_not_awaited()
        if change_at == "before_admission":
            runtime.scheduler.begin_request.assert_not_awaited()
            runtime.budget.reserve.assert_not_awaited()
        else:
            lease.release.assert_awaited_once()
            runtime.limiter.release_parallel.assert_awaited_once()
            runtime.budget.release.assert_awaited_once_with(None)
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [1, 8192, 16384, 393216])
def test_output_allowance_is_explicit_and_preserves_model(limit, tmp_path):
    settings = Settings(runtime_path=tmp_path / "settings.yaml").value
    settings["compaction"]["summary_output_tokens"] = limit
    validate_settings(settings)
    profile = summary_profile(settings["compaction"], SimpleNamespace(metadata={}))
    value = ContextCompactor(Mock(), Mock(), internal_base_url="http://isolated.invalid",
        internal_api_key="", model_id="summary", client=Mock(), summary_profile=profile)
    request = value._summary_request([{"role": "user", "content": "source"}], None)
    assert request["max_tokens"] == value.summary_output_tokens == limit
    assert request["model"] == "summary"
    assert "thinking" not in request and "reasoning_effort" not in request
    assert (profile == SummaryProfile()) == (limit == 8192)


@pytest.mark.parametrize("limit", [0, -1, 393217, True, False, None, 1.5, "8192", [], {}])
def test_invalid_output_allowances_fail_validation(limit, tmp_path):
    settings = Settings(runtime_path=tmp_path / "settings.yaml").value
    settings["compaction"]["summary_output_tokens"] = limit
    with pytest.raises(ValueError, match="summary_output_tokens"):
        validate_settings(settings)
    with pytest.raises(ValueError, match="summary_output_tokens"):
        summary_profile(settings["compaction"], SimpleNamespace(metadata={}))


@pytest.mark.parametrize("backend", ["foreground", "checkpoint"])
def test_larger_per_call_allowance_cannot_expand_job_budget(runtime, backend):
    import time
    from ai_router.compaction import SummaryWork
    from ai_router.compaction_checkpoint import CheckpointedCompactor
    from ai_router.compaction_jobs import CompactionJobConflict
    async def scenario():
        worker = CompactionWorker(runtime)
        worker._open()
        limits = {"max_output_tokens": 10000}
        # The database must capture the same immutable task budget.
        budget_job = worker.jobs.create("alice", "budget-branch", {"messages": []}, "chat",
            {"limits": limits})
        worker.jobs.claim("budget-worker")
        client = SimpleNamespace(post=AsyncMock())
        runtime.token_counter.count_request.return_value = 100
        kwargs = dict(internal_base_url="http://isolated.invalid", internal_api_key="",
            model_id="summary", client=client,
            summary_profile=summary_profile({"summary_output_tokens": 16384}, None))
        messages = [{"role": "user", "content": "source"}]
        if backend == "foreground":
            value = ContextCompactor(runtime.token_counter, runtime.compactor.cipher,
                work_limits=limits, **kwargs)
            with pytest.raises(CompactionUnavailableError, match="workload"):
                await value._summarize_bounded(messages, target=None, input_budget=10000,
                    work=SummaryWork(started_at=time.monotonic()))
        else:
            value = CheckpointedCompactor(runtime.token_counter, runtime.compactor.cipher,
                jobs=worker.jobs, job=budget_job, worker="budget-worker", **kwargs)
            with pytest.raises(CompactionJobConflict, match="budget"):
                await value._summarize(messages)
        client.post.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("change_at", ["last_policy", "last_key", "dispatch", "operation"])
@pytest.mark.parametrize("changed_field,initial,replacement", [
    ("summary_reasoning", "low", "disabled"),
    ("summary_output_tokens", 8192, 16384),
])
def test_profile_change_at_final_await_prevents_http_and_unknown_state(
        runtime, monkeypatch, change_at, changed_field, initial, replacement):
    async def scenario():
        worker, job = prepare(runtime)
        worker.jobs.claim("worker")
        config = runtime.settings.section("compaction")
        config[changed_field] = initial
        job["parameters"][changed_field] = initial
        endpoint = SimpleNamespace(id="summary", public_model="summary", enabled=True,
            cloud=False, metadata={"provider": "deepseek"})
        runtime.registry.by_id = lambda name: endpoint
        account_policy = SimpleNamespace(allow_compaction=True, local_only=False,
            routing_mode="inherit", rpm_limit=10, tpm_limit=10000, max_parallel_requests=1)
        policy_calls = key_calls = 0
        async def current_policy(owner):
            nonlocal policy_calls
            policy_calls += 1
            await asyncio.sleep(0)
            if change_at == "last_policy" and policy_calls == 2:
                config[changed_field] = replacement
            return account_policy
        async def is_key_active(owner, key):
            nonlocal key_calls
            key_calls += 1
            await asyncio.sleep(0)
            if change_at == "last_key" and key_calls == 2:
                config[changed_field] = replacement
            return True
        runtime.clients = SimpleNamespace(current_policy=current_policy, is_key_active=is_key_active)
        runtime.policy = SimpleNamespace(choose=AsyncMock(return_value=SimpleNamespace(endpoint=endpoint)))
        lease = SimpleNamespace(release=AsyncMock())
        runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
        runtime.limiter = SimpleNamespace(check_rate_limits=AsyncMock(return_value=(True, None)),
            acquire_parallel=AsyncMock(return_value=True), release_parallel=AsyncMock())
        runtime.budget = SimpleNamespace(reserve=AsyncMock(return_value=None),
            commit=AsyncMock(), release=AsyncMock())
        runtime.token_counter.count_request.return_value = 100
        monkeypatch.setattr("ai_router.api._acquire_internal_model", AsyncMock(
            return_value=ModelCallTarget("http://isolated.invalid", "summary", "")))
        if change_at in {"dispatch", "operation"}:
            import ai_router.compaction_checkpoint as checkpoint
            original_thread = checkpoint._thread
            async def changing_thread(function, *args, **kwargs):
                result = await original_thread(function, *args, **kwargs)
                if function.__name__ == change_at:
                    config[changed_field] = replacement
                return result
            monkeypatch.setattr(checkpoint, "_thread", changing_thread)
        sent = []
        def upstream(request):
            sent.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"facts": ["saved"]})}}], "usage": {"completion_tokens": 10}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            value = RoutedCompactor(runtime.token_counter, runtime.compactor.cipher,
                jobs=worker.jobs, job=job, worker="worker", runtime=runtime,
                internal_base_url=runtime.internal_base_url, internal_api_key="",
                model_id="summary", client=client)
            failure = None
            try:
                await value._summarize([{"role": "user", "content": "source"}])
            except CompactionUnavailableError as exc:
                failure = exc
            assert not sent, f"profile changed at {change_at}, but HTTP was sent"
            assert failure is not None, "changed profile must reject the pending summary"
            state = worker.jobs.read("alice", job["id"])
            assert not worker.jobs.public(state)["unresolved_operation_ids"]
            assert all(step["state"] != "dispatched" for step in state["steps"].values())
            lease.release.assert_awaited_once()
            runtime.limiter.release_parallel.assert_awaited_once()
            runtime.budget.release.assert_awaited_once_with(None)
            runtime.budget.commit.assert_not_awaited()
    asyncio.run(scenario())
