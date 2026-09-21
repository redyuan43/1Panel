from __future__ import annotations

import asyncio
import hashlib
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_router.api import _candidate_history_token_evidence, _prepare_routed_body
from ai_router.compute import BoundedExecutor
from ai_router.content_audit import ContentObservation
from ai_router.directed_archive import DirectedArchive
from ai_router.evaluator import TaskEvaluator
from ai_router.fixed_route import resolve_fixed_route_intent
from ai_router.config import Registry
from ai_router.identity import IdentityProfile
from ai_router.token_counter import SimpleTokenCounter
from ai_router.types import RouteDecision
from ai_router.api import create_app
from test_client_route_binding import _runtime as binding_runtime
from ai_router.content_audit import ArchiveReader
from ai_router.training_archive import TrainingArchive, archive_event
from cryptography.fernet import Fernet


def async_test(function):
    @wraps(function)
    def wrapper():
        return asyncio.run(function())
    return wrapper


class ArchiveDelegate:
    def __init__(self):
        self.events = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail_once = False
        self.fail_after_accept_once = False
        self.event_ids = set()

    def _digest(self, value):
        return hashlib.sha256(value.encode()).hexdigest()

    async def _record(self, operation, token=None, **kwargs):
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("worker failed")
        self.events.append((operation, token, kwargs))

    async def begin(self, **kwargs):
        await self._record("begin", **kwargs)
        return self._digest("request:" + kwargs["request_id"])

    async def set_effective_context(self, token, **kwargs):
        await self._record("effective", token, **kwargs)

    async def mark_routed(self, token, **kwargs):
        await self._record("routed", token, **kwargs)

    async def record_pipeline(self, token, pipeline):
        await self._record("pipeline", token, pipeline=pipeline)

    async def complete(self, token, **kwargs):
        await self._record("complete", token, **kwargs)

    async def fail(self, token, **kwargs):
        await self._record("fail", token, **kwargs)

    async def publish_history(self, trace):
        await self._record("history", trace=trace)

    async def enqueue_event(self, operation, token, kwargs, *, event_id):
        if event_id in self.event_ids:
            return
        label = {
            "set_effective_context": "effective",
            "mark_routed": "routed",
            "record_pipeline": "pipeline",
            "publish_history": "history",
        }.get(operation, operation)
        await self._record(label, token, **kwargs)
        self.event_ids.add(event_id)
        if self.fail_after_accept_once:
            self.fail_after_accept_once = False
            raise RuntimeError("worker result unknown")

    async def status(self):
        return {"mode": "delegate"}

    async def aclose(self):
        return None


def _endpoint(endpoint_id, public_model="test/model"):
    return SimpleNamespace(id=endpoint_id, public_model=public_model, role="responder")


def test_fixed_intent_uses_only_server_derived_sources():
    endpoint = _endpoint("one")
    registry = SimpleNamespace(
        by_id=lambda value: endpoint if value == "one" else None,
        by_public_model=lambda value: (endpoint,) if value == "test/model" else (),
    )
    binding = SimpleNamespace(target_endpoint_id="one")
    directive = SimpleNamespace(endpoint_id="one")
    assert resolve_fixed_route_intent(
        registry, requested_model="auto", route_resolution=binding,
        directive=None, conversation_control={},
    ).source == "client_route_binding"
    assert resolve_fixed_route_intent(
        registry, requested_model="auto", route_resolution=None,
        directive=directive, conversation_control={},
    ).source == "route_directive"
    assert resolve_fixed_route_intent(
        registry, requested_model="test/model", route_resolution=None,
        directive=None, conversation_control={},
    ).source == "explicit_model"
    assert resolve_fixed_route_intent(
        registry, requested_model="auto", route_resolution=None,
        directive=None, conversation_control={},
    ) is None


def test_fixed_intent_excludes_multi_candidate_alias_and_admin_pin():
    endpoints = (_endpoint("one"), _endpoint("two"))
    registry = SimpleNamespace(by_id=lambda value: endpoints[0], by_public_model=lambda value: endpoints)
    assert resolve_fixed_route_intent(
        registry, requested_model="test/model", route_resolution=None,
        directive=None, conversation_control={},
    ) is None
    assert resolve_fixed_route_intent(
        registry, requested_model="test/model", route_resolution=None,
        directive=None, conversation_control={"pin": {"endpoint_id": "one"}},
    ) is None


def test_content_snapshot_reuses_canonical_bytes(monkeypatch):
    import ai_router.content_audit as content_audit
    calls = []
    original = content_audit.encoded

    def counted(body):
        calls.append(1)
        return original(body)

    monkeypatch.setattr(content_audit, "encoded", counted)
    observation = ContentObservation(retain_canonical=True)
    body = {"messages": [{"role": "user", "content": "hello"}]}
    observation.capture("effective", body)
    first = observation.frozen(body)
    second = observation.frozen(body)
    assert first is not second and first.raw == second.raw
    assert len(calls) == 1


def test_content_snapshot_releases_canonical_bytes_for_sync_mode():
    observation = ContentObservation(retain_canonical=True)
    body = {"messages": [{"role": "user", "content": "hello"}]}
    observation.capture("effective", body)
    assert observation._canonical_bodies
    observation.set_retain_canonical(False)
    assert observation._canonical_bodies == {}
    assert observation._body_identities == {}


@async_test
async def test_evaluator_skips_model_but_keeps_deterministic_profile():
    evaluator = TaskEvaluator(
        {"enabled": True, "model_id": "classifier"},
        internal_base_url="http://unused", internal_api_key="unused",
    )
    async def forbidden(*args, **kwargs):
        raise AssertionError("classification model must not be called")
    evaluator._call_model = forbidden
    result = await evaluator.evaluate(
        {"messages": [{"role": "user", "content": "hello"}]},
        headers={}, api_kind="chat", prompt_tokens=2,
        current_task=None, is_new_conversation=True, allow_model_call=False,
    )
    assert result.task == "general"
    assert result.route_profile == "general"
    assert result.complexity == "standard"
    assert result.evidence["model_call_skipped"] is True
    await evaluator.client.aclose()


@async_test
async def test_ordinary_new_auto_request_still_calls_classifier():
    evaluator = TaskEvaluator(
        {"enabled": True, "model_id": "classifier", "confidence_threshold": 0.5},
        internal_base_url="http://unused", internal_api_key="unused",
    )
    calls = []

    async def classify(*args, **kwargs):
        calls.append(1)
        from ai_router.types import Evaluation
        return Evaluation("general", None, 0.9, "model", route_profile="general")

    evaluator._call_model = classify
    result = await evaluator.evaluate(
        {"messages": [{"role": "user", "content": "hello"}]},
        headers={}, api_kind="chat", prompt_tokens=2,
        current_task=None, is_new_conversation=True,
    )
    assert calls == [1]
    assert result.reason == "model"
    await evaluator.client.aclose()


@async_test
async def test_deterministic_tool_and_structured_checks_precede_skip():
    evaluator = TaskEvaluator(
        {"enabled": True, "model_id": "classifier", "tool_task_mappings": {"code": ["read"]}},
        internal_base_url="http://unused", internal_api_key="unused",
    )
    tool = await evaluator.evaluate(
        {"messages": [{"role": "user", "content": "hello"}],
         "tools": [{"type": "function", "function": {"name": "read.file"}}]},
        headers={}, api_kind="chat", prompt_tokens=2, current_task=None,
        is_new_conversation=True, allow_model_call=False,
    )
    structured = await evaluator.evaluate(
        {"messages": [{"role": "user", "content": "hello"}],
         "response_format": {"type": "json_object"}},
        headers={}, api_kind="chat", prompt_tokens=2, current_task=None,
        is_new_conversation=True, allow_model_call=False,
    )
    assert tool.task == "code" and tool.reason == "tool_mapping"
    assert structured.task == "batch" and structured.reason == "structured_output"
    await evaluator.client.aclose()


@async_test
async def test_directed_archive_handoff_is_nonblocking_and_ordered():
    delegate = ArchiveDelegate()
    delegate.block = True
    archive = DirectedArchive(delegate)
    observation = ContentObservation()
    body = {"messages": [{"role": "user", "content": "original"}]}
    observation.capture("effective", body)
    token = await archive.begin(request_id="r1", received_body=observation.frozen(body))
    await archive.select_mode(token, directed=True)
    await asyncio.wait_for(delegate.entered.wait(), 1)
    await asyncio.wait_for(
        archive.set_effective_context(token, effective_body=observation.frozen(body)),
        0.1,
    )
    route = {"endpoint_id": "one"}
    await archive.mark_routed(
        token,
        effective_body=observation.frozen(body),
        routed_body=observation.frozen(body),
        route=route,
    )
    await archive.record_pipeline(token, observation.frozen_archive())
    body["messages"][0]["content"] = "mutated"
    route["endpoint_id"] = "mutated"
    await archive.complete(token, status_code=200)
    delegate.release.set()
    await asyncio.wait_for(archive._queue.join(), 1)
    assert [event[0] for event in delegate.events] == [
        "begin", "effective", "routed", "pipeline", "complete"
    ]
    assert delegate.events[0][2]["received_body"]["messages"][0]["content"] == "original"
    assert delegate.events[2][2]["route"]["endpoint_id"] == "one"
    assert archive.background_status()["pending_body_bytes"] == 0
    await archive.aclose()


@async_test
async def test_non_directed_archive_retains_synchronous_contract():
    delegate = ArchiveDelegate()
    archive = DirectedArchive(delegate)
    token = await archive.begin(request_id="r2", received_body={"value": 1})
    await archive.select_mode(token, directed=False)
    assert [event[0] for event in delegate.events] == ["begin"]
    await archive.fail(token, status_code=400, error={"code": "bad"})
    assert [event[0] for event in delegate.events] == ["begin", "fail"]
    await archive.aclose()


@async_test
async def test_queue_overload_waits_for_prior_token_then_falls_back():
    delegate = ArchiveDelegate()
    delegate.block = True
    archive = DirectedArchive(delegate, max_events=2)
    token = await archive.begin(request_id="r3", received_body={"value": 1})
    await archive.select_mode(token, directed=True)
    await delegate.entered.wait()
    await archive.set_effective_context(token, effective_body={"value": 2})
    fallback = asyncio.create_task(archive.complete(token, status_code=200))
    await asyncio.sleep(0)
    assert not fallback.done()
    delegate.release.set()
    await asyncio.wait_for(fallback, 1)
    assert [event[0] for event in delegate.events] == ["begin", "effective", "complete"]
    assert archive.sync_fallbacks == 1
    await archive.aclose()


@async_test
async def test_worker_failure_retries_stable_event_without_loss():
    delegate = ArchiveDelegate()
    delegate.fail_once = True
    archive = DirectedArchive(delegate)
    token = await archive.begin(request_id="r4", received_body={"value": 1})
    observed = []
    await archive.select_mode(
        token,
        directed=True,
        mode_observer=observed.append,
    )
    await asyncio.wait_for(archive._queue.join(), 1)
    await archive.set_effective_context(token, effective_body={"value": 2})
    await asyncio.wait_for(archive._queue.join(), 1)
    assert [event[0] for event in delegate.events] == ["begin", "effective"]
    assert archive.sync_fallbacks == 0
    assert archive.background_status()["worker_retries"] == 1
    assert archive.background_status()["worker_healthy"] is True
    assert observed == ["process_local", "process_local_retry"]
    await archive.aclose()


@async_test
async def test_unknown_worker_result_is_deduplicated_by_stable_event_id():
    delegate = ArchiveDelegate()
    delegate.fail_after_accept_once = True
    archive = DirectedArchive(delegate)
    token = await archive.begin(request_id="r4-unknown", received_body={"value": 1})
    await archive.select_mode(token, directed=True)
    await asyncio.wait_for(archive._queue.join(), 1)
    assert [event[0] for event in delegate.events] == ["begin"]
    assert len(delegate.event_ids) == 1
    assert archive.background_status()["worker_retries"] == 1
    await archive.aclose()


@async_test
async def test_unhealthy_local_worker_uses_existing_sync_path():
    delegate = ArchiveDelegate()
    archive = DirectedArchive(delegate)
    token = await archive.begin(request_id="r4b", received_body={"value": 1})
    archive._healthy = False
    observed = []
    mode = await archive.select_mode(
        token,
        directed=True,
        mode_observer=observed.append,
    )
    assert [event[0] for event in delegate.events] == ["begin"]
    assert archive.sync_fallbacks == 1
    assert mode == "existing_sync"
    assert observed == ["process_local", "existing_sync"]
    await archive.complete(token, status_code=200)
    assert [event[0] for event in delegate.events] == ["begin", "complete"]
    await archive.aclose()


@async_test
async def test_close_flushes_pending_events():
    delegate = ArchiveDelegate()
    archive = DirectedArchive(delegate, flush_timeout_seconds=1)
    token = await archive.begin(request_id="r5", received_body={"value": 1})
    await archive.select_mode(token, directed=True)
    await archive.complete(token, status_code=200)
    await archive.aclose()
    assert [event[0] for event in delegate.events] == ["begin", "complete"]
    assert archive.flush_failures == 0


@async_test
async def test_body_budget_counts_unique_canonical_bytes_once():
    delegate = ArchiveDelegate()
    delegate.block = True
    observation = ContentObservation()
    body = {"messages": [{"role": "user", "content": "same"}]}
    observation.capture("effective", body)
    frozen = observation.frozen(body)
    archive = DirectedArchive(delegate, max_body_bytes=len(frozen.raw) + 1)
    token = await archive.begin(request_id="r6", received_body=frozen)
    await archive.select_mode(token, directed=True)
    await delegate.entered.wait()
    await archive.set_effective_context(token, effective_body=frozen)
    assert archive.background_status()["pending_body_bytes"] == len(frozen.raw)
    delegate.release.set()
    await archive._queue.join()
    await archive.aclose()


@async_test
async def test_close_timeout_is_counted_without_silent_success():
    delegate = ArchiveDelegate()
    delegate.block = True
    archive = DirectedArchive(delegate, flush_timeout_seconds=0.01)
    token = await archive.begin(request_id="r7", received_body={"value": 1})
    observed = []
    await archive.select_mode(
        token,
        directed=True,
        mode_observer=observed.append,
    )
    await delegate.entered.wait()
    with pytest.raises(RuntimeError, match="shutdown deadline"):
        await archive.aclose()
    assert archive.flush_failures == 1
    assert archive.background_status()["pending_events"] == 0
    assert archive.background_status()["pending_body_bytes"] == 0
    assert observed == ["process_local", "flush_failed"]


def test_background_archive_matches_existing_database_contract(tmp_path):
    async def scenario():
        key = Fernet.generate_key()
        key_path = tmp_path / "archive.key"
        key_path.write_bytes(key)
        direct = TrainingArchive(str(tmp_path / "direct.sqlite3"), str(key_path))
        background_delegate = TrainingArchive(
            str(tmp_path / "background.sqlite3"), str(key_path)
        )
        background = DirectedArchive(background_delegate)
        kwargs = {
            "request_id": "same-request",
            "conversation_id": "conversation",
            "conversation_mode": "stateful",
            "client_id": "client",
            "key_id": "key",
            "protocol": "chat",
            "received_body": {"messages": [{"role": "user", "content": "hello"}]},
            "instance_id": "instance",
            "boot_id": "boot",
        }
        context = archive_event.set(("fixed-event", 123.0))
        try:
            direct_token = await direct.begin(**kwargs)
            await direct.set_effective_context(
                direct_token, effective_body=kwargs["received_body"]
            )
            await direct.mark_routed(
                direct_token,
                effective_body=kwargs["received_body"],
                routed_body=kwargs["received_body"],
                route={"selected_model": "model", "endpoint_id": "endpoint"},
            )
            await direct.complete(direct_token, status_code=200, response_payload=b'{"ok":true}')

            token = await background.begin(**kwargs)
            await background.select_mode(token, directed=True)
            await background.set_effective_context(
                token, effective_body=kwargs["received_body"]
            )
            await background.mark_routed(
                token,
                effective_body=kwargs["received_body"],
                routed_body=kwargs["received_body"],
                route={"selected_model": "model", "endpoint_id": "endpoint"},
            )
            await background.complete(token, status_code=200, response_payload=b'{"ok":true}')
            await background.aclose()
        finally:
            archive_event.reset(context)
            direct.close()
        expected = ArchiveReader(str(tmp_path / "direct.sqlite3"), str(key_path)).read("same-request")
        actual = ArchiveReader(str(tmp_path / "background.sqlite3"), str(key_path)).read("same-request")
        assert actual == expected

    asyncio.run(scenario())


def test_real_registry_has_single_explicit_targets_available_for_intent():
    registry = Registry(Path("config/registry.yaml"))
    singles = [
        model for model in registry.public_models()
        if model != "auto" and len(registry.by_public_model(model)) == 1
    ]
    assert singles
    intent = resolve_fixed_route_intent(
        registry, requested_model=singles[0], route_resolution=None,
        directive=None, conversation_control={},
    )
    assert intent is not None


@async_test
async def test_fixed_candidate_counts_one_target_and_reuses_projection():
    registry = Registry(Path("config/registry.yaml"))
    endpoint = next(item for item in registry.responders() if item.enabled)
    runtime = SimpleNamespace(
        registry=registry,
        token_counter=SimpleTokenCounter(),
        endpoint_token_counter=None,
        settings=SimpleNamespace(section=lambda name: {}),
        compute_executor=BoundedExecutor(),
    )
    body = {"messages": [{"role": "user", "content": "hello"}]}
    identity = IdentityProfile.from_settings({"enabled": False})
    cache = {}
    evidence = await _candidate_history_token_evidence(
        runtime, body=body, api_kind="chat", prompt_tokens=10,
        requested_model="auto", conversation=None, identity=identity,
        fixed_endpoint_id=endpoint.id, projection_cache=cache,
    )
    assert list(evidence) == [endpoint.id]
    assert evidence[endpoint.id]["tokens"] == 10
    assert list(cache) == [endpoint.id]
    decision = RouteDecision(
        endpoint=endpoint, requested_model="auto", task="general",
        prompt_tokens=evidence[endpoint.id]["tokens"], output_reserve_tokens=1,
        reason="test", affinity="new", score=1,
    )
    routed, capsule = await _prepare_routed_body(
        runtime, body, api_kind="chat", decision=decision, request_id="r",
        identity=identity, projection_cache=cache,
    )
    assert capsule is None
    assert routed is cache[endpoint.id]["projected"]
    runtime.compute_executor.close()


@async_test
async def test_ordinary_auto_does_not_retain_per_endpoint_projection_cache():
    registry = Registry(Path("config/registry.yaml"))
    runtime = SimpleNamespace(
        registry=registry,
        token_counter=SimpleTokenCounter(),
        endpoint_token_counter=None,
        settings=SimpleNamespace(section=lambda name: {}),
        compute_executor=BoundedExecutor(),
    )
    body = {"messages": [{"role": "user", "content": "hello"}]}
    cache = {}
    evidence = await _candidate_history_token_evidence(
        runtime,
        body=body,
        api_kind="chat",
        prompt_tokens=10,
        requested_model="auto",
        conversation=None,
        identity=IdentityProfile.from_settings({"enabled": False}),
        projection_cache=cache,
    )
    assert len(evidence) == len(
        [endpoint for endpoint in registry.responders() if endpoint.enabled]
    )
    assert cache == {}
    runtime.compute_executor.close()


@async_test
async def test_projection_cache_invalidates_when_body_changes():
    registry = Registry(Path("config/registry.yaml"))
    endpoint = next(item for item in registry.responders() if item.enabled)
    runtime = SimpleNamespace(
        registry=registry, token_counter=SimpleTokenCounter(),
        endpoint_token_counter=None,
        settings=SimpleNamespace(section=lambda name: {}),
        compute_executor=BoundedExecutor(),
    )
    body = {"messages": [{"role": "user", "content": "before"}]}
    identity = IdentityProfile.from_settings({"enabled": False})
    cache = {}
    evidence = await _candidate_history_token_evidence(
        runtime, body=body, api_kind="chat", prompt_tokens=10,
        requested_model="auto", conversation=None, identity=identity,
        fixed_endpoint_id=endpoint.id, projection_cache=cache,
    )
    body["messages"][0]["content"] = "after"
    decision = RouteDecision(
        endpoint=endpoint, requested_model="auto", task="general",
        prompt_tokens=evidence[endpoint.id]["tokens"], output_reserve_tokens=1,
        reason="test", affinity="new", score=1,
    )
    routed, _ = await _prepare_routed_body(
        runtime, body, api_kind="chat", decision=decision, request_id="r",
        identity=identity, projection_cache=cache,
    )
    assert routed["messages"][0]["content"] == "after"
    assert routed is not cache[endpoint.id]["projected"]
    runtime.compute_executor.close()


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_fixed_binding_integration_skips_classifier_and_preserves_protocol(
    tmp_path, monkeypatch, api_kind, stream
):
    monkeypatch.setenv("AI_ROUTER_DIRECTED_FAST_PATH_ENABLED", "true")
    runtime, secrets, captured = binding_runtime(tmp_path, monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("classification model must not be called")

    runtime.evaluator._call_model = forbidden
    path = "/v1/chat/completions" if api_kind == "chat" else "/v1/responses"
    body = (
        {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": stream}
        if api_kind == "chat"
        else {"model": "auto", "input": "hello", "stream": stream}
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer " + secrets["home-assistant"]},
            json=body,
        )
    assert response.status_code == 200, response.text
    assert captured and captured[0]["body"]["model"] == "siyuan/qwen38-v100-196k"
    trace = asyncio.run(runtime.route_traces.get(response.headers["x-request-id"]))
    fast = trace["directed_fast_path"]
    assert fast["enabled"] is True
    assert fast["source"] == "client_route_binding"
    assert fast["classification_model_skipped"] is True
    assert fast["candidate_count"] == 1
    assert fast["projection_reused"] is True
    asyncio.run(runtime.close())
