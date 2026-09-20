import asyncio
from contextlib import closing
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import fakeredis.aioredis
import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from ai_router.archive_queue import ArchiveQueue, QueuedTrainingArchive
from ai_router.archive_worker import ArchiveWorker
from ai_router.compute import BoundedExecutor, count_tokens, token_cache
from ai_router.content_audit import ArchiveReader
from ai_router.errors import TrainingArchiveUnavailableError
from ai_router.training_archive import TrainingArchive


def setup(tmp_path, server=None):
    key = tmp_path / "training.key"
    if not key.exists():
        key.write_bytes(Fernet.generate_key())
    producer = QueuedTrainingArchive(str(tmp_path / "archive.sqlite3"), str(key), "redis://unused")
    server = server or fakeredis.FakeServer()
    producer.queue.redis = fakeredis.aioredis.FakeRedis(server=server)
    archive = TrainingArchive(str(producer.database_path), str(key))
    consumer_queue = ArchiveQueue(fakeredis.aioredis.FakeRedis(server=server), archive._cipher)
    worker = ArchiveWorker(consumer_queue, archive, SimpleNamespace(), min_free_bytes=0)
    reader = ArchiveReader(str(archive.database_path), str(key))
    return producer, worker, reader, server


async def begin(producer, request="request-1"):
    return await producer.begin(request_id=request, conversation_id="conversation", conversation_mode="stateful",
        client_id="test", key_id="key", protocol="chat", received_body={"messages":[{"role":"user", "content":"SECRET"}]},
        instance_id="local", boot_id="old-boot")


async def finish(producer, worker):
    await producer.aclose()
    await worker.queue.close()
    worker.archive.close()


@pytest.mark.asyncio
async def test_api_restart_keeps_accepted_events_encrypted_and_ordered(tmp_path):
    producer, worker, reader, server = setup(tmp_path)
    token = await begin(producer)
    await producer.mark_routed(token, effective_body={"messages": []}, routed_body={"model":"first"}, route={"selected_model":"first"})
    await producer.mark_routed(token, effective_body={"messages": []}, routed_body={"model":"second"}, route={"selected_model":"second"})
    await producer.complete(token, status_code=200, response_payload=b'{"answer":"SECRET_ANSWER"}')
    blobs = await producer.queue.redis.lrange(producer.queue.keys(token)[0], 0, -1)
    assert len(blobs) == 4 and all(b"SECRET" not in value for value in blobs)
    assert reader.read("request-1") is None
    await producer.aclose()
    restarted, unused, _, _ = setup(tmp_path, server)
    assert (await restarted.queue.status())["pending_requests"] == 1
    for _ in range(4):
        assert await worker.step()
    value = reader.read("request-1")
    assert value["state"] == "completed"
    assert [r["selected_model"] for r in value["routing_attempts"]] == ["first", "second"]
    assert value["response"]["body"]["value"]["answer"] == "SECRET_ANSWER"
    assert (await worker.queue.status())["pending_bytes"] == 0
    await finish(restarted, worker)
    await unused.queue.close()
    unused.archive.close()


@pytest.mark.asyncio
async def test_commit_before_ack_crash_replay_does_not_append_route_twice(tmp_path):
    producer, worker, reader, _ = setup(tmp_path)
    token = await begin(producer)
    await worker.step()
    await producer.mark_routed(token, effective_body={}, routed_body={}, route={"selected_model":"v100"})
    ack = worker.queue.acknowledge
    worker.queue.acknowledge = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await worker.step()
    assert len(reader.read("request-1")["routing_attempts"]) == 1
    worker.queue.acknowledge = ack
    assert await worker.step()
    assert len(reader.read("request-1")["routing_attempts"]) == 1
    assert (await producer.queue.status())["pending_bytes"] == 0
    await finish(producer, worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_committed,has_successor", [(False, False), (True, False), (True, True)])
async def test_ack_timeout_retries_only_the_original_head(tmp_path, ack_committed, has_successor):
    producer, worker, reader, _ = setup(tmp_path)
    token = await begin(producer)
    if has_successor:
        await producer.complete(token, status_code=200)
    acknowledge = worker.queue.acknowledge

    async def timeout_ack(*args):
        if ack_committed:
            await acknowledge(*args)
        raise RedisTimeoutError("injected ACK reply loss")

    worker.queue.acknowledge = timeout_ack
    assert not await worker.step()
    redis, queue = worker.queue.redis, worker.queue
    assert await redis.hget(queue.key("retries"), token) == (None if ack_committed else b"1")
    if ack_committed and not has_successor:
        assert await redis.zscore(queue.key("ready"), token) is None
        assert (await queue.status())["pending_bytes"] == 0
    # A replacement consumer must progress using the same Redis state.
    restarted = ArchiveWorker(queue, worker.archive, worker.runtime, min_free_bytes=0)
    queue.acknowledge = acknowledge
    if not ack_committed:
        await redis.zadd(queue.key("ready"), {token: 0})
        assert await restarted.step()
    if has_successor:
        assert await restarted.step()
        assert reader.read("request-1")["state"] == "completed"
    await begin(producer, "other-request")
    assert await restarted.step()
    assert reader.read("other-request")["state"] == "received"
    assert (await queue.status())["pending_bytes"] == 0
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_stale_ready_index_is_healed_without_blocking_other_requests(tmp_path):
    producer, worker, reader, _ = setup(tmp_path)
    queue = worker.queue
    await queue.redis.zadd(queue.key("ready"), {"old-ghost": 0})
    await queue.redis.hset(queue.key("retries"), "old-ghost", 1)
    await queue.redis.hset(queue.key("errors"), "old-ghost", "TimeoutError")
    await begin(producer)
    assert not await worker.step()
    assert await queue.redis.zscore(queue.key("ready"), "old-ghost") is None
    assert await queue.redis.hget(queue.key("errors"), "old-ghost") is None
    assert await worker.step()
    assert reader.read("request-1")["state"] == "received"
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_empty_index_cleanup_preserves_concurrent_append(tmp_path):
    producer, worker, reader, _ = setup(tmp_path)
    token = producer._digest("request:request-1")
    queue = worker.queue
    await queue.redis.zadd(queue.key("ready"), {token: 0})
    lindex = queue.redis.lindex

    async def append_after_empty_read(*args):
        result = await lindex(*args)
        await begin(producer)
        return result

    queue.redis.lindex = append_after_empty_read
    assert await queue.head() is None
    queue.redis.lindex = lindex
    assert await queue.redis.zscore(queue.key("ready"), token) is not None
    assert await worker.step()
    assert reader.read("request-1")["state"] == "received"
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_bad_record_is_retained_and_other_requests_progress(tmp_path):
    producer, worker, reader, _ = setup(tmp_path)
    token = await begin(producer)
    key = producer.queue.keys(token)[0]
    blob = await producer.queue.redis.lindex(key, 0)
    await producer.queue.redis.lset(key, 0, b"x" * len(blob))
    for _ in range(5):
        await worker.queue.redis.zadd(worker.queue.key("ready"), {token:0})
        assert not await worker.step()
    assert (await worker.queue.status())["quarantined_requests"] == 1
    assert await worker.queue.redis.llen(key) == 1
    await begin(producer, "request-2")
    assert await worker.step()
    assert reader.read("request-2")["state"] == "received"
    # Repair and explicitly retry the retained head.
    await worker.queue.redis.lset(key, 0, blob)
    await worker.queue.retry(token)
    assert await worker.step()
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_admission_failure_is_bounded_and_does_not_fall_back_to_memory(tmp_path):
    producer, worker, _, server = setup(tmp_path)
    producer.queue.max_bytes = 1
    with pytest.raises(TrainingArchiveUnavailableError):
        await begin(producer)
    assert (await producer.queue.status())["pending_requests"] == 0
    producer.queue.max_bytes = 1024**2
    server.connected = False
    with pytest.raises(TrainingArchiveUnavailableError):
        await begin(producer)
    server.connected = True
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_background_archive_wait_does_not_block_admission(tmp_path):
    producer, worker, _, _ = setup(tmp_path)
    await begin(producer)
    entered, release = asyncio.Event(), asyncio.Event()
    real_begin = worker.archive.begin
    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await real_begin(**kwargs)
    worker.archive.begin = blocked
    task = asyncio.create_task(worker.step())
    await entered.wait()
    # A second API admission succeeds while the archive worker is stalled.
    await asyncio.wait_for(begin(producer, "request-2"), timeout=0.5)
    assert not task.done()
    release.set()
    await task
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_history_is_published_only_after_archive_complete(tmp_path, monkeypatch):
    producer, worker, reader, _ = setup(tmp_path)
    token = await begin(producer)
    await producer.complete(token, status_code=200)
    await producer.publish_history({"request_id":"request-1"})
    observed = []
    async def publish(*args):
        observed.append(reader.read("request-1")["state"])
    monkeypatch.setattr("ai_router.archive_worker.publish_history", publish)
    for _ in range(3):
        await worker.step()
    assert observed == ["completed"]
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_cancelled_compute_keeps_slot_until_thread_finishes():
    pool = BoundedExecutor(1)
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        release.wait(5)
    first = asyncio.create_task(pool.run(blocked))
    await asyncio.to_thread(entered.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(pool.run(lambda: 42))
    await asyncio.sleep(0.02)
    assert not second.done()
    release.set()
    assert await second == 42
    pool.close()


@pytest.mark.asyncio
async def test_token_cache_respects_mutations_and_request_boundaries():
    calls = []
    def count(body, kind):
        calls.append((body["value"], kind))
        return body["value"]
    runtime = SimpleNamespace(token_counter=SimpleNamespace(count_request=count))
    context = token_cache.set({})
    body = {"value":1}
    try:
        assert await count_tokens(runtime, body, "chat") == 1
        assert await count_tokens(runtime, body, "chat") == 1
        body["value"] = 2
        assert await count_tokens(runtime, body, "chat") == 2
    finally:
        token_cache.reset(context)
    assert await count_tokens(runtime, body, "chat") == 2
    assert len(calls) == 3
    runtime.compute_executor.close()


@pytest.mark.asyncio
async def test_large_identical_bodies_are_stored_once_and_rehydrated(tmp_path):
    producer, worker, reader, _ = setup(tmp_path)
    token = await begin(producer)
    body = {"messages":[{"role":"user", "content":"private" * 20000}]}
    await producer.mark_routed(token, effective_body=body, routed_body=body, route={})
    await producer.record_pipeline(token, {"bodies":{"snapshot":body}})
    assert await producer.queue.redis.hlen(producer.queue.key("bodies:" + token)) == 1
    assert "messages" in body  # caller objects were not mutated
    for _ in range(3):
        assert await worker.step()
    archived = reader.read("request-1")
    assert archived["request"]["effective_body"] == body
    assert archived["pipeline"]["bodies"]["snapshot"] == body
    assert (await producer.queue.status())["pending_bytes"] == 0
    assert not await producer.queue.redis.exists(producer.queue.key("bodies:" + token))
    await finish(producer, worker)


@pytest.mark.asyncio
async def test_ack_cleanup_failure_does_not_create_payloadless_retry(tmp_path, monkeypatch):
    producer, worker, _, _ = setup(tmp_path)
    await begin(producer)
    def broken(_):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(worker.archive, "forget_event", broken)
    assert await worker.step()
    assert (await worker.queue.status())["pending_requests"] == 0
    assert await worker.queue.head() is None
    await finish(producer, worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("enqueue_fails", [False, True])
@pytest.mark.parametrize("protocol", ["chat", "responses", "adapter"])
async def test_stream_releases_deployment_before_queue_wait_and_withholds_done(tmp_path, monkeypatch, enqueue_fails, protocol):
    from ai_router import api
    from ai_router.config import Registry
    from ai_router.identity import IdentityProfile
    import httpx
    import time
    from pathlib import Path
    producer, worker, _, _ = setup(tmp_path)
    token = await begin(producer)
    endpoint = Registry(Path(__file__).resolve().parents[1] / "config/registry.yaml").by_id("ai-qwen38-27b")
    decision = api.RouteDecision(endpoint=endpoint, requested_model=endpoint.public_model, task="general",
        prompt_tokens=5, output_reserve_tokens=5, reason="test", affinity="test", score=1)
    decision.native_or_adapter = "adapter" if protocol == "adapter" else "native"
    api_kind = "chat" if protocol == "chat" else "responses"
    terminal = b"[DONE]" if protocol == "chat" else b"response.completed"
    lease = SimpleNamespace(owner_token="owner", release_deployment=AsyncMock(), release=AsyncMock())
    current = SimpleNamespace(training=producer, compactor=None, conversations=None,
        limiter=SimpleNamespace(release_parallel=AsyncMock()), track_request_finished=AsyncMock())
    finalizer = api._StreamResourceFinalizer(current, lease, "client")
    entered, release = asyncio.Event(), asyncio.Event()
    async def complete(*args, **kwargs):
        assert lease.release_deployment.await_count == 1
        assert lease.release.await_count == 0
        entered.set()
        await release.wait()
        if enqueue_fails:
            raise TrainingArchiveUnavailableError()
    monkeypatch.setattr(producer, "complete", complete)
    monkeypatch.setattr(api, "persist_history", AsyncMock())
    monkeypatch.setattr(api, "_audit", AsyncMock())
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if protocol == "responses":
                yield b'data: {"type":"response.output_text.delta","delta":"ok","output_index":0,"content_index":0}\n\n'
                yield b'data: {"type":"response.completed","response":{"id":"resp-test","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}]}}\n\n'
            else:
                yield b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
                yield b'data: [DONE]\n\n'
    upstream = httpx.Response(200, stream=Stream())
    parts = []
    async def consume():
        async for part in api._stream_response(current, upstream, resource_finalizer=finalizer,
            client_id="client", key_id="key", request_id="request-1", conversation_id=None,
            decision=decision, state=None, body={}, api_kind=api_kind, training_token=token,
            started_at=time.monotonic(), cache_snapshot=None,
            identity=IdentityProfile.from_settings({"enabled":False}), identifiers=()):
            parts.append(part)
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    assert b"ok" in b"".join(parts) and terminal not in b"".join(parts)
    assert upstream.is_closed
    release.set()
    await task
    if enqueue_fails:
        assert terminal not in b"".join(parts)
        assert b"training_archive_unavailable" in b"".join(parts)
    else:
        assert terminal in b"".join(parts)
    assert lease.release.await_count == 1
    await finish(producer, worker)
