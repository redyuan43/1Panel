import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from ai_router.prefix_prewarm import PrefixPrewarmer
from ai_router.scheduler import Scheduler
from test_client_deployment_pins import environment, choose, agx_worker


def setup_runtime(environment, monkeypatch, responder):
    settings, registry, health, store, policy = environment
    target = agx_worker(); target["backend_api_key_env"] = "TEST_WARM_WORKER_KEY"
    health.value.detail["workers"].append(target)
    monkeypatch.setenv("TEST_WARM_WORKER_KEY", "cpu-worker-secret")
    events = []
    runtime = SimpleNamespace(
        draining=False, health=health, policy=policy, store=store,
        scheduler=Scheduler(store),
        internal_client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        audit=SimpleNamespace(write=lambda event, **fields: events.append((event, fields))),
        track_request_started=AsyncMock(), track_request_routed=AsyncMock(),
        track_request_finished=AsyncMock(), draining_marker=AsyncMock(return_value=None),
    )
    return runtime, events


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("signature", [None, "opaque-prefix"])
def test_live_prefix_preparation_is_idle_only_deduplicated_and_rate_limited(environment, monkeypatch, busy, signature):
    async def scenario():
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"event": "miss_saved", "fixed_tokens": 33028, "prime_tokens": 33028, "seconds": 1})
        runtime, events = setup_runtime(environment, monkeypatch, respond)
        manager = PrefixPrewarmer(runtime)
        decision = await choose(runtime.policy)
        decision.prefix_affinity_key = signature
        decision.prefix_affinity_prefix_tokens = 33028 if signature else 0
        body = {"model": "siyuan/qwen36-shared", "messages": [{"role": "user", "content": "CPU fixture"}], "max_tokens": 4096}
        holder = await runtime.scheduler.begin_request(None)
        if busy:
            await runtime.scheduler.try_acquire_deployment_candidates(holder, ("qwen36-agx",))
        for _ in range(2):
            manager.submit(decision, body, client_id="workbuddy-qwen36-shared", request_id="real-parent", api_kind="chat")
        await asyncio.gather(*manager.tasks)
        if busy:
            assert not calls
            await holder.release()
            manager.submit(decision, body, client_id="workbuddy-qwen36-shared", request_id="next-parent", api_kind="chat")
            await asyncio.gather(*manager.tasks)
        assert len(calls) == 1
        assert [event for event, _ in events] == ["prefix_overflow_prepared"]
        assert events[0][1]["cache_event"] == "miss_saved"
        assert str(calls[0].url) == "http://qwen36-agx/cache/prepare"
        assert json.loads(calls[0].content) == body
        assert calls[0].headers["authorization"] == "Bearer cpu-worker-secret"
        manager.submit(decision, body, client_id="workbuddy-qwen36-shared", request_id="third-parent", api_kind="chat")
        await asyncio.gather(*manager.tasks)
        assert len(calls) == 1
        assert await runtime.scheduler.try_acquire_deployment_candidates(holder, ("qwen36-agx",)) == "qwen36-agx"
        await holder.release()
        assert runtime.track_request_started.await_count == runtime.track_request_finished.await_count == 1
        await runtime.internal_client.aclose()
    asyncio.run(scenario())


def test_failure_and_shutdown_release_background_capacity(environment, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        async def respond(request):
            started.set()
            await asyncio.Event().wait()
        runtime, _ = setup_runtime(environment, monkeypatch, respond)
        manager = PrefixPrewarmer(runtime)
        decision = await choose(runtime.policy); decision.prefix_affinity_key = "opaque-prefix"
        manager.submit(decision, {"messages": []}, client_id="workbuddy-qwen36-shared", request_id="parent", api_kind="chat")
        await started.wait()
        await manager.close()
        lease = await runtime.scheduler.begin_request(None)
        assert await runtime.scheduler.try_acquire_deployment_candidates(lease, ("qwen36-agx",)) == "qwen36-agx"
        await lease.release()
        assert runtime.track_request_finished.await_count == 1
        await runtime.internal_client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["small", "already_overflow", "draining", "disabled_cache"])
def test_irrelevant_requests_never_start_model_preparation(environment, monkeypatch, reason):
    async def scenario():
        runtime, _ = setup_runtime(environment, monkeypatch, lambda request: pytest.fail("unexpected backend request"))
        decision = await choose(runtime.policy); decision.prefix_affinity_key = "opaque-prefix"
        body = {}
        if reason == "small": decision.prompt_tokens = 44000
        if reason == "already_overflow": decision.deployment_id = "qwen36-agx"
        if reason == "draining": runtime.draining = True
        if reason == "disabled_cache": body["cache_prompt"] = False
        manager = PrefixPrewarmer(runtime)
        manager.submit(decision, body, client_id="workbuddy-qwen36-shared", request_id="parent", api_kind="chat")
        assert not manager.tasks
        await runtime.internal_client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["release", "start_publish", "cancel_registration"])
def test_failed_prewarm_lifecycle_always_removes_active_tracking(environment, monkeypatch, failure):
    async def scenario():
        runtime, _ = setup_runtime(environment, monkeypatch, lambda request: httpx.Response(200, json={"event": "hot"}))
        active = set()
        published = []
        async def started(tracking_id, *args):
            active.add(tracking_id)
            if failure == "start_publish":
                raise ConnectionError("injected publication failure")
            if failure == "cancel_registration":
                raise asyncio.CancelledError()
        async def finished(tracking_id):
            active.discard(tracking_id)
            published.append(len(active))
        runtime.track_request_started = AsyncMock(side_effect=started)
        runtime.track_request_finished = AsyncMock(side_effect=finished)
        begin = runtime.scheduler.begin_request
        async def begin_with_release_failure(*args):
            lease = await begin(*args)
            release = lease.release
            async def fail_release():
                await release()
                raise ConnectionError("injected lost release acknowledgement")
            lease.release = fail_release
            return lease
        if failure == "release":
            monkeypatch.setattr(runtime.scheduler, "begin_request", begin_with_release_failure)
        manager = PrefixPrewarmer(runtime)
        decision = await choose(runtime.policy)
        manager.submit(decision, {"messages": [{"role": "user", "content": "request"}]}, client_id="workbuddy-qwen36-shared", request_id="parent", api_kind="chat")
        results = await asyncio.gather(*manager.tasks, return_exceptions=True)
        assert not active
        assert published == [0]
        assert runtime.track_request_finished.await_count == 1
        if failure == "cancel_registration":
            assert isinstance(results[0], asyncio.CancelledError)
        await runtime.internal_client.aclose()
    asyncio.run(scenario())


def test_image_request_does_not_schedule_text_prefix_preparation(environment, monkeypatch):
    async def scenario():
        runtime, _ = setup_runtime(environment, monkeypatch, lambda request: pytest.fail("image prewarm must not reach backend"))
        manager = PrefixPrewarmer(runtime)
        decision = await choose(runtime.policy)
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}},
        ]}]}
        manager.submit(decision, body, client_id="workbuddy-qwen36-shared", request_id="image", api_kind="chat")
        assert not manager.tasks
        await runtime.internal_client.aclose()
    asyncio.run(scenario())
