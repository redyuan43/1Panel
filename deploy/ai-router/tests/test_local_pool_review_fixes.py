"""Regressions for the f7f39f118 commit review; no model calls."""
import asyncio
import time
from unittest.mock import AsyncMock
import pytest
from ai_router.local_pool import PREFIX, LocalPoolLockBusy
from ai_router.route_diagnosis import diagnose_route
from tests.test_local_pool_policy import make_policy, choose, trace
from tests import test_local_pool_review as attribution


def test_busy_allocation_lock_falls_back_without_reserving():
    async def case():
        policy, endpoints = make_policy()
        await policy.store.acquire_lock(PREFIX + "lock", "other-router", 10)
        t = trace("contended")
        decision = await choose(policy, trace=t)
        assert decision.endpoint.id in {e.id for e in endpoints}
        assert t.payload["local_pool"]["allocation_fallback"] == "allocation_lock_busy"
        assert await policy.store.get_json(PREFIX + "claim:contended") is None
        assert decision.reason != "local_pool_spread"
    asyncio.run(case())


@pytest.mark.parametrize("error", [TimeoutError("store timeout"), asyncio.CancelledError()])
def test_unrelated_errors_and_cancellation_are_not_swallowed(error):
    async def case():
        policy, _ = make_policy()
        policy.local_pool.select = AsyncMock(side_effect=error)
        with pytest.raises(type(error)):
            await choose(policy)
    asyncio.run(case())


def test_completed_claim_released_even_when_history_lock_busy():
    async def case():
        pool = await attribution.LocalPoolAttributionReview().make_finished("edge-qwen38-flash", 1)
        # A terminal trace with the original successful record, as used by finish.
        records = await pool.store.list_json(PREFIX + "recent:")
        assert records
        from types import SimpleNamespace as NS
        claim = records[0]
        rid = claim["request_id"]
        t = NS(request_id=rid, terminal=True, payload={"request_id":rid,
               "status":"succeeded", "started_at":time.time()-10, "completed_at":time.time(), "endpoint_id":claim["endpoint_id"],
               "local_pool":{"claim":claim}})
        await pool.store.set_json(PREFIX + "claim:" + rid, claim, ttl_seconds=3600)
        await pool.store.acquire_lock(PREFIX + "lock", "other-router", 10)
        with pytest.raises(LocalPoolLockBusy):
            await pool.finish(t)
        assert await pool.store.get_json(PREFIX + "claim:" + rid) is None
    asyncio.run(case())


@pytest.mark.parametrize("reason", ["local_pool_spread", "local_pool_faster_first_output"])
def test_timeout_diagnosis_takes_precedence_over_final_selection(reason):
    policy, endpoints = make_policy()
    t = {"status":"succeeded", "endpoint_id":endpoints[0].id, "route_selected":True,
         "local_pool":{"selection":"capacity_timeout_cold_fallback"},
         "attempts":[{"selection":{"reason":reason,"affinity":"migrated"},"steps":[]}]}
    verdict = diagnose_route(t, [], policy.settings, policy.registry)["verdict"]
    assert "等待达到上限" in verdict and "冷计算" in verdict
    t["attempts"][0]["selection"]["affinity"] = "admin-pin"
    assert "管理员临时固定" in diagnose_route(t, [], policy.settings, policy.registry)["verdict"]


def test_lock_fallback_is_explained_in_diagnosis():
    policy, endpoints = make_policy()
    t = {"status":"succeeded", "endpoint_id":endpoints[0].id, "route_selected":True,
         "local_pool":{"allocation_fallback":"allocation_lock_busy"}, "attempts":[]}
    assert "未保证会话分散" in diagnose_route(t, [], policy.settings, policy.registry)["verdict"]
