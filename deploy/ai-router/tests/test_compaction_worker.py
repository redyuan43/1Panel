import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import pytest
import httpx

from ai_router.compaction import CapsuleCipher
from ai_router.compaction_worker import CompactionWorker, validate_background_settings


@pytest.fixture
def runtime(tmp_path):
    config = {"background_enabled": True}
    key = Fernet.generate_key().decode()
    cipher = CapsuleCipher(key)
    return SimpleNamespace(settings=SimpleNamespace(runtime_path=tmp_path / "runtime.yaml",
        section=lambda name: config if name == "compaction" else {}),
        compactor=SimpleNamespace(model_id="summary", cipher=cipher, client=Mock()),
        registry=SimpleNamespace(by_id=lambda name: SimpleNamespace(enabled=True, safe_context_tokens=32000)),
        token_counter=Mock(), state_encryption_key=key, internal_base_url="http://isolated.invalid",
        internal_api_key="", audit=Mock())


def prepare(runtime):
    worker = CompactionWorker(runtime)
    worker._open()
    job = worker.jobs.create("alice", "branch", {"messages": [{"role": "user", "content": "source"}]},
        "chat", {"model_id": "summary", "target_context_tokens": 32000})
    return worker, job


def test_disabled_worker_has_no_database_side_effect(runtime):
    runtime.settings.section("compaction")["background_enabled"] = False
    worker = CompactionWorker(runtime)
    assert asyncio.run(worker.tick()) is False
    assert not runtime.settings.runtime_path.with_name("compaction-jobs.sqlite3").exists()


def test_worker_observes_admin_disable_without_foreground_request(runtime, tmp_path):
    from ai_router.config import Settings
    from ai_router.runtime import RouterRuntime
    runtime.settings = Settings(runtime_path=tmp_path / "settings.yaml")
    runtime.settings.write_runtime({"compaction": {"background_enabled": True, "model_id": "summary"}})
    runtime.health = SimpleNamespace()
    runtime.evaluator = SimpleNamespace()
    runtime.scheduler = SimpleNamespace()
    runtime.reload_settings = lambda: RouterRuntime.reload_settings(runtime)
    worker, job = prepare(runtime)
    editor = Settings(runtime_path=runtime.settings.runtime_path)
    editor.write_runtime({"compaction": {"background_enabled": False, "model_id": "summary"}})
    assert runtime.settings.section("compaction")["background_enabled"] is True
    assert asyncio.run(worker.tick()) is False
    assert worker.jobs.read("alice", job["id"])["state"] == "queued"


def test_running_worker_observes_admin_disable_without_foreground_request(runtime, tmp_path, monkeypatch):
    from ai_router.config import Settings
    from ai_router.runtime import RouterRuntime
    runtime.settings = Settings(runtime_path=tmp_path / "settings.yaml")
    runtime.settings.write_runtime({"compaction": {"background_enabled": True, "model_id": "summary"}})
    runtime.health = SimpleNamespace()
    runtime.evaluator = SimpleNamespace()
    runtime.scheduler = SimpleNamespace()
    runtime.reload_settings = lambda: RouterRuntime.reload_settings(runtime)
    async def scenario():
        worker, job = prepare(runtime)
        started, released = asyncio.Event(), asyncio.Event()
        async def compact(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        fake = SimpleNamespace(summary_output_tokens=8192, compact=compact)
        monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
        task = asyncio.create_task(worker.tick())
        try:
            await asyncio.wait_for(started.wait(), 2)
            editor = Settings(runtime_path=runtime.settings.runtime_path)
            editor.write_runtime({"compaction": {"background_enabled": False, "model_id": "summary"}})
            await asyncio.wait_for(task, 8)
            assert released.is_set()
            assert worker.jobs.read("alice", job["id"])["state"] == "failed"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["missing", "disabled", "context", "cloud_grant", "cloud_budget"])
def test_activation_rejects_unusable_summary_dependency(problem):
    endpoint = SimpleNamespace(enabled=True, safe_context_tokens=32000, cloud=False,
        public_model="summary", metadata={"provider": "test"})
    settings = {"compaction": {"background_enabled": True, "model_id": "summary"},
        "cloud": {"enabled": True, "allowed_models": ["summary"], "allowed_providers": ["test"], "monthly_budget": 10}}
    if problem == "disabled":
        endpoint.enabled = False
    elif problem == "context":
        endpoint.safe_context_tokens = 8192
    elif problem == "cloud_grant":
        endpoint.cloud = True
        settings["cloud"]["allowed_providers"] = []
    elif problem == "cloud_budget":
        endpoint.cloud = True
        settings["cloud"]["monthly_budget"] = 0
    registry = SimpleNamespace(by_id=lambda name: None if problem == "missing" else endpoint)
    with pytest.raises(ValueError):
        validate_background_settings(settings, registry)
    settings["compaction"]["background_enabled"] = False
    validate_background_settings(settings, registry)


def test_worker_publishes_candidate_without_mutating_original(runtime, monkeypatch):
    worker, job = prepare(runtime)
    summary = [{"role": "user", "content": "candidate only"}]
    fake = SimpleNamespace(summary_output_tokens=8192, cipher=runtime.compactor.cipher,
        compact=AsyncMock(return_value=SimpleNamespace(encrypted_messages=runtime.compactor.cipher.encrypt(summary), summary_indices=())))
    monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
    assert asyncio.run(worker.tick())
    saved = worker.jobs.read("alice", job["id"])
    assert saved["state"] == "ready" and saved["candidate"] == summary
    assert saved["body"] == job["body"]


def test_worker_chunks_to_smallest_ready_physical_context(runtime, monkeypatch):
    worker, _job = prepare(runtime)
    captured = {}

    async def compact(*args, **kwargs):
        captured.update(kwargs)
        raise ValueError("stop after budget inspection")

    fake = SimpleNamespace(summary_output_tokens=8192, compact=compact)
    monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
    runtime.registry.by_id = lambda name: SimpleNamespace(
        enabled=True,
        safe_context_tokens=262144,
    )
    runtime.health = SimpleNamespace(status=AsyncMock(return_value=SimpleNamespace(
        detail={"workers": [
            {"ready": True, "safe_context_tokens": 65536},
            {"ready": True, "safe_context_tokens": 196608},
        ]},
    )))

    assert asyncio.run(worker.tick())
    assert captured["summary_input_tokens"] == 65536 - 8192


def test_failure_is_terminal_and_worker_can_take_next_job(runtime, monkeypatch):
    worker, job = prepare(runtime)
    fake = SimpleNamespace(summary_output_tokens=8192, compact=AsyncMock(side_effect=ValueError("synthetic failure")))
    monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
    assert asyncio.run(worker.tick())
    assert worker.jobs.read("alice", job["id"])["state"] == "failed"
    assert asyncio.run(worker.tick()) is False
    runtime.audit.write.assert_called_with("background_compaction_failed", job_id=job["id"], error_type="ValueError")


def test_shutdown_cancels_execution_before_returning(runtime, monkeypatch):
    async def scenario():
        worker, job = prepare(runtime)
        started, released = asyncio.Event(), asyncio.Event()
        async def compact(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        fake = SimpleNamespace(summary_output_tokens=8192, compact=compact)
        monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
        task = asyncio.create_task(worker.tick())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()
        assert worker.jobs.read("alice", job["id"])["state"] == "failed"
    asyncio.run(scenario())


def test_configured_model_change_does_not_execute_stale_job(runtime, monkeypatch):
    worker, job = prepare(runtime)
    runtime.compactor.model_id = "different-model"
    factory = Mock()
    monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", factory)
    asyncio.run(worker.tick())
    factory.assert_not_called()
    assert worker.jobs.read("alice", job["id"])["state"] == "failed"


@pytest.mark.parametrize("status,second_fails,calls,final", [(503, False, 2, "ready"),
    (429, False, 2, "ready"), (503, True, 2, "failed"), (200, False, 1, "failed")])
def test_worker_only_retries_confirmed_transient_response_once(runtime, monkeypatch, status, second_fails, calls, final):
    from ai_router.compaction import SummaryResponseError
    worker, job = prepare(runtime)
    error = SummaryResponseError(status, "synthetic", "0")
    capsule = SimpleNamespace(encrypted_messages=runtime.compactor.cipher.encrypt([{"role": "user", "content": "candidate"}]), summary_indices=())
    fake = SimpleNamespace(summary_output_tokens=8192, cipher=runtime.compactor.cipher,
        compact=AsyncMock(side_effect=[error, error if second_fails else capsule]))
    monkeypatch.setattr("ai_router.compaction_worker.RoutedCompactor", lambda *a, **kw: fake)
    asyncio.run(worker.tick())
    assert fake.compact.await_count == calls
    assert worker.jobs.read("alice", job["id"])["state"] == final


@pytest.mark.parametrize("mode", ["success", "failure", "cached", "disabled_during_admission", "endpoint_disabled_during_admission", "global_local_only_during_admission", "cloud_allowed"])
def test_routed_call_releases_capacity_and_account_slot(runtime, monkeypatch, mode):
    from ai_router.compaction_worker import RoutedCompactor
    from ai_router.types import ModelCallTarget
    async def scenario():
        fail = mode == "failure"
        worker, job = prepare(runtime)
        worker.jobs.claim("worker")
        endpoint = SimpleNamespace(id="summary", public_model="summary", enabled=True, cloud=False)
        runtime.registry.by_id = lambda name: endpoint
        runtime.clients = SimpleNamespace(current_policy=AsyncMock(return_value=SimpleNamespace(
            allow_compaction=True, local_only=False, routing_mode="inherit", rpm_limit=10,
            tpm_limit=10000, max_parallel_requests=1)), is_key_active=AsyncMock(return_value=True))
        runtime.policy = SimpleNamespace(choose=AsyncMock(return_value=SimpleNamespace(endpoint=endpoint)))
        lease = SimpleNamespace(release=AsyncMock())
        runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
        runtime.limiter = SimpleNamespace(check_rate_limits=AsyncMock(return_value=(True, None)),
            acquire_parallel=AsyncMock(return_value=True), release_parallel=AsyncMock())
        runtime.budget = SimpleNamespace(reserve=AsyncMock(return_value=None), commit=AsyncMock(), release=AsyncMock())
        if mode == "endpoint_disabled_during_admission":
            from dataclasses import replace
            from pathlib import Path
            from ai_router.config import Registry
            from ai_router.endpoint_config import EndpointConfigManager
            from ai_router.runtime import RouterRuntime
            from ai_router.store import InMemoryStateStore
            registry = Registry(Path(__file__).resolve().parents[1] / "config" / "registry.yaml")
            endpoint = replace(registry.by_id("edge-qwen38-flash"),
                id="summary", public_model="summary", enabled=True)
            runtime.registry = registry.with_endpoints([endpoint])
            runtime.policy.choose.return_value = SimpleNamespace(endpoint=endpoint)
            runtime.endpoint_configs = EndpointConfigManager(InMemoryStateStore(), runtime.registry)
            runtime._endpoint_config_revision = 0
            runtime._endpoint_config_lock = asyncio.Lock()
            runtime._started = False
            async def disable_endpoint(*args, **kwargs):
                await runtime.endpoint_configs.action("summary", "disable", expected_revision=0, source="test")
                await RouterRuntime.reload_endpoint_config(runtime)
                assert not runtime.registry.by_id("summary").enabled
            runtime.budget.reserve.side_effect = disable_endpoint
        if mode in {"global_local_only_during_admission", "cloud_allowed"}:
            endpoint.cloud = True
            job["parameters"]["local_only"] = False
            routing = {"objectives": {"local_only": False}}
            original_section = runtime.settings.section
            runtime.settings.section = lambda name: routing if name == "routing" else original_section(name)
            async def reserve(*args, **kwargs):
                routing["objectives"]["local_only"] = mode != "cloud_allowed"
            runtime.budget.reserve.side_effect = reserve
        if mode == "disabled_during_admission":
            async def reserve(*args, **kwargs):
                runtime.settings.section("compaction")["background_enabled"] = False
            runtime.budget.reserve.side_effect = reserve
        runtime.token_counter.count_request.return_value = 100
        monkeypatch.setattr("ai_router.api._acquire_internal_model",
            AsyncMock(return_value=ModelCallTarget("http://isolated.invalid", "summary", "")))
        response = AsyncMock(side_effect=ValueError("synthetic") if fail else None, return_value={"facts": ["saved"]})
        monkeypatch.setattr("ai_router.compaction_checkpoint.CheckpointedCompactor._summarize", response)
        value = RoutedCompactor(runtime.token_counter, runtime.compactor.cipher,
            jobs=worker.jobs, job=job, worker="worker", runtime=runtime,
            internal_base_url=runtime.internal_base_url, internal_api_key="", model_id="summary", client=Mock())
        if mode == "cached":
            target = ModelCallTarget("http://isolated.invalid", "summary", "")
            step = value.step_key(value._summary_request([{"role": "user", "content": "source"}], target), target)
            worker.jobs.dispatch(job["id"], "worker", step, 100, 8192)
            worker.jobs.complete_step(job["id"], "worker", step, {"facts": ["saved"]}, 10)
        if mode in {"disabled_during_admission", "endpoint_disabled_during_admission", "global_local_only_during_admission"}:
            from ai_router.errors import CompactionUnavailableError
            with pytest.raises(CompactionUnavailableError, match="during admission"):
                await value._summarize([{"role": "user", "content": "source"}])
            response.assert_not_awaited()
        elif fail:
            with pytest.raises(ValueError):
                await value._summarize([{"role": "user", "content": "source"}])
        else:
            assert await value._summarize([{"role": "user", "content": "source"}]) == {"facts": ["saved"]}
        lease.release.assert_awaited_once()
        runtime.limiter.release_parallel.assert_awaited_once()
        runtime.budget.release.assert_awaited_once_with(None)
        if mode == "cached":
            response.assert_not_awaited()
            runtime.limiter.check_rate_limits.assert_not_awaited()
            runtime.budget.reserve.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["success", "timeout", "commit_failure"])
def test_dispatched_call_commits_budget_and_releases_resources(runtime, monkeypatch, mode):
    """Keep dispatch/checkpoint persistence real; replace only external boundaries."""
    from ai_router.compaction_worker import RoutedCompactor
    from ai_router.compaction_jobs import CompactionJobConflict
    from ai_router.errors import CompactionUnavailableError
    from ai_router.types import ModelCallTarget

    async def scenario():
        worker, job = prepare(runtime)
        worker.jobs.claim("worker")
        endpoint = SimpleNamespace(id="summary", public_model="summary", enabled=True, cloud=False)
        runtime.registry.by_id = lambda name: endpoint
        runtime.clients = SimpleNamespace(current_policy=AsyncMock(return_value=SimpleNamespace(
            allow_compaction=True, local_only=False, routing_mode="inherit", rpm_limit=10,
            tpm_limit=10000, max_parallel_requests=1)), is_key_active=AsyncMock(return_value=True))
        runtime.policy = SimpleNamespace(choose=AsyncMock(return_value=SimpleNamespace(endpoint=endpoint)))
        lease = SimpleNamespace(release=AsyncMock())
        runtime.scheduler = SimpleNamespace(begin_request=AsyncMock(return_value=lease))
        runtime.limiter = SimpleNamespace(check_rate_limits=AsyncMock(return_value=(True, None)),
            acquire_parallel=AsyncMock(return_value=True), release_parallel=AsyncMock())
        reservation = object()
        runtime.budget = SimpleNamespace(reserve=AsyncMock(return_value=reservation),
            commit=AsyncMock(side_effect=RuntimeError("budget unavailable") if mode == "commit_failure" else None),
            release=AsyncMock())
        runtime.token_counter.count_request.return_value = 100
        monkeypatch.setattr("ai_router.api._acquire_internal_model",
            AsyncMock(return_value=ModelCallTarget("http://isolated.invalid", "summary", "")))
        requests = []

        def provider(request):
            requests.append(request)
            if mode == "timeout":
                raise httpx.ReadTimeout("unknown provider outcome", request=request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps({"facts": ["saved"]})}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            value = RoutedCompactor(runtime.token_counter, runtime.compactor.cipher,
                jobs=worker.jobs, job=job, worker="worker", runtime=runtime,
                internal_base_url=runtime.internal_base_url, internal_api_key="", model_id="summary", client=client)
            messages = [{"role": "user", "content": "source"}]
            if mode == "timeout":
                with pytest.raises(CompactionUnavailableError):
                    await value._summarize(messages)
            elif mode == "commit_failure":
                with pytest.raises(RuntimeError, match="budget unavailable"):
                    await value._summarize(messages)
            else:
                assert (await value._summarize(messages))["facts"] == ["saved"]
            assert worker.jobs.read("alice", job["id"])["calls"] == 1
            assert len(requests) == 1
            runtime.budget.commit.assert_awaited_once_with(reservation)
            runtime.budget.release.assert_not_awaited()
            lease.release.assert_awaited_once()
            runtime.limiter.release_parallel.assert_awaited_once()
            if mode == "timeout":
                with pytest.raises(CompactionJobConflict):
                    await value._summarize(messages)
                assert len(requests) == 1
                assert worker.jobs.read("alice", job["id"])["calls"] == 1
                assert lease.release.await_count == 2
                assert runtime.limiter.release_parallel.await_count == 2
                runtime.budget.commit.assert_awaited_once_with(reservation)

    asyncio.run(scenario())
