import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import patch
import pytest
from ai_router.local_pool import LocalPool, MEMBERS, PREFIX, bucket
from ai_router.store import InMemoryStateStore


def run(coro):
    return asyncio.run(coro)


def fixtures():
    settings = NS(section=lambda key: {"local_pool": {"enabled": True}} if key == "routing" else {})
    pool = LocalPool(InMemoryStateStore(), settings)
    endpoints = [NS(id=x, cloud=False, max_concurrency=4 if x.startswith("ai-") else 1) for x in MEMBERS]
    statuses = {e.id: NS(cache_generation="version-1", load_headroom=1, detail={"running": 0}) for e in endpoints}
    return pool, endpoints, statuses


def trace(rid, conversation=None):
    return NS(request_id=rid, terminal=False, payload={"request_id": rid, "client_id": "client", "conversation_id": conversation or rid})


async def select(pool, endpoints, statuses, rid, conversation=None):
    t = trace(rid, conversation)
    e = await pool.select(endpoints, statuses, trace=t, conversation=None, prompt_tokens=40000, output_tokens=16000)
    return e, t


def test_parallel_new_conversations_spread_across_three_devices():
    async def case():
        pool, es, sts = fixtures()
        choices = await asyncio.gather(*(select(pool, es, sts, str(i)) for i in range(3)))
        assert {e.id for e, _ in choices} == set(MEMBERS)
        assert len(await pool.store.list_json(PREFIX + "claim:")) == 3
    run(case())


def test_finished_conversation_still_protects_idle_device():
    async def case():
        pool, es, sts = fixtures()
        edge = es[1]
        await pool.store.set_json(PREFIX + "recent:edge:old", {"endpoint_id": edge.id, "owner": "old", "assigned_at": time.time()}, 600)
        e, _ = await select(pool, es, sts, "new")
        assert e.id != edge.id
    run(case())


def test_busy_direct_backend_is_not_preferred_even_without_router_claim():
    async def case():
        pool, es, sts = fixtures()
        sts[es[0].id].load_headroom = 0
        sts[es[0].id].detail = {"running": 4}
        e, _ = await select(pool, es, sts, "new")
        assert e.id != es[0].id
    run(case())


def test_recent_occupancy_expires_and_slot_capacity_is_respected():
    async def case():
        pool, es, sts = fixtures()
        now = time.time()
        for e in es:
            await pool.store.set_json(PREFIX + "recent:" + e.id, {"endpoint_id": e.id, "owner": "old-" + e.id, "assigned_at": now}, 600)
        e, t = await select(pool, es, sts, "new")
        assert e.id == es[0].id  # 1/4 versus 1/1 recent conversations.
        await pool.release(t.request_id)
        with patch("time.time", return_value=now + 601):
            assert await pool.store.list_json(PREFIX + "recent:") == []
    run(case())


def test_same_conversation_not_counted_as_competing_recent_owner():
    async def case():
        pool, es, sts = fixtures()
        t = trace("retry", "A")
        owner = pool.identity(t)
        await pool.store.set_json(PREFIX + "recent:x", {"endpoint_id": es[1].id, "owner": owner, "assigned_at": 2}, 600)
        await pool.select([es[1]], sts, trace=t, conversation=None, prompt_tokens=40000, output_tokens=16000)
        assert t.payload["local_pool"]["candidates"][0]["recent_conversations"] == 0
        assert owner != pool.identity(NS(payload={**t.payload, "client_id": "another-client"}))
    run(case())


async def add_samples(pool, e, *, first=5, duration=60, state="cold", n=5, generation="version-1"):
    values = [{"request_id": str(i), "at": time.time(), "generation": generation, "prompt_bucket": bucket(40000),
               "output_bucket": bucket(16000, 1024), "cache_state": state, "first_s": first, "duration_s": duration} for i in range(n)]
    await pool.store.set_json(PREFIX + "samples:" + e.id, {"items": values}, 86400)


async def busy_original(pool, es, sts):
    original = es[1]
    sts[original.id].load_headroom = 0
    sts[original.id].detail = {"running": 1}
    await pool.store.set_json(PREFIX + "claim:other", {"request_id": "other", "endpoint_id": original.id, "owner": "other", "phase": "running",
        "started_at": time.time() - 10, "assigned_at": time.time() - 10, "prompt_bucket": bucket(40000), "output_bucket": bucket(16000, 1024)}, 3600)
    return original






@pytest.mark.parametrize("status,usage,ttft", [
    ("succeeded", {"state": "complete", "input_tokens": 40000, "cached_tokens": 0}, 5000),
    ("succeeded", {"state": "complete", "input_tokens": 40000, "cached_tokens": 39000}, 5000),
    ("succeeded", {"state": "incomplete", "input_tokens": 40000}, 5000),
    ("interrupted", {"state": "complete", "input_tokens": 40000, "cached_tokens": 39000}, 5000),
    ("succeeded", {"state": "complete", "input_tokens": 40000, "cached_tokens": 0}, None),
])
def test_terminal_cleanup_and_strict_sample_sources(status, usage, ttft):
    async def case():
        pool, es, sts = fixtures()
        e, t = await select(pool, es, sts, "finish")
        t.terminal = True
        t.payload.update(endpoint_id=e.id, status=status, started_at=time.time()-10, completed_at=time.time(),
            attempts=[{"number": 1, "steps": [{"node_id": "upstream_request", "evidence": {"backend_usage": usage}}]}],
            observation={"queue_wait_ms": 0, "ttft_ms": ttft})
        await pool.finish(t)
        await pool.finish(t)  # duplicate completion cannot double-count observations.
        assert await pool.store.list_json(PREFIX + "claim:") == []
        samples = (await pool.store.get_json(PREFIX + "samples:" + e.id) or {}).get("items", [])
        assert samples == []  # Forecast sampling was removed; audit usage remains separate.
        recent = await pool.store.list_json(PREFIX + "recent:")
        assert len(recent) == int(status == "succeeded")
    run(case())


@pytest.mark.parametrize("change", [{"members":[{}]}, {"min_samples":4}, {"min_samples":5.5}, {"recent_seconds":True}, {"min_saving_ratio":float("nan")}])
def test_pool_settings_reject_invalid_values_without_crashing(tmp_path,change):
    from tests.test_core import settings
    from ai_router.config import validate_settings
    value=settings(tmp_path).value
    value["routing"]["local_pool"].update(change)
    with pytest.raises(ValueError): validate_settings(value)
