"""Independent CPU regressions for request-claim attribution; no backend calls."""
import time
from types import SimpleNamespace as NS
import unittest
from ai_router.local_pool import LocalPool, MEMBERS, PREFIX, bucket
from ai_router.store import InMemoryStateStore

class LocalPoolAttributionReview(unittest.IsolatedAsyncioTestCase):
    async def make_finished(self, final_endpoint, attempts=1, status="succeeded"):
        pool = LocalPool(InMemoryStateStore(), NS(section=lambda _: {"local_pool": {"enabled": True}}))
        endpoint = NS(id=MEMBERS[1], cloud=False, max_concurrency=1)
        trace = NS(request_id="review-request", terminal=False, payload={
            "request_id": "review-request", "client_id": "review-client", "conversation_id": "review-conversation"})
        await pool.select([endpoint], {endpoint.id: NS(detail={"running": 0}, load_headroom=1, cache_generation="g1")},
                          trace=trace, conversation=None, prompt_tokens=40000, output_tokens=16000)
        await pool.start(NS(endpoint=endpoint, prompt_tokens=40000, output_reserve_tokens=16000), trace, None)
        trace.terminal = True
        trace.payload.update(endpoint_id=final_endpoint, deployment_id=final_endpoint, status=status,
                             started_at=time.time()-10, completed_at=time.time(),
                             observation={"queue_wait_ms": 0, "ttft_ms": 1000},
                             attempts=[{"number": i+1, "steps": [{"node_id": "upstream_request", "evidence": {
                                 "backend_usage": {"state": "complete", "input_tokens": 40000, "cached_tokens": 39000}}}]} for i in range(attempts)])
        await pool.finish(trace)
        return pool

    async def test_same_attempt_reselection_to_non_pool_cannot_warm_previous_endpoint(self):
        pool = await self.make_finished("non-pool-final-endpoint")
        self.assertEqual(await pool.store.list_json(PREFIX + "claim:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "recent:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "samples:"), [])

    async def test_retried_non_pool_success_cannot_warm_previous_endpoint(self):
        pool = await self.make_finished("non-pool-final-endpoint", attempts=2)
        self.assertEqual(await pool.store.list_json(PREFIX + "recent:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "samples:"), [])

    async def test_matching_success_is_observed(self):
        pool = await self.make_finished(MEMBERS[1])
        self.assertEqual(len(await pool.store.list_json(PREFIX + "recent:")), 1)
        self.assertEqual(len(await pool.store.list_json(PREFIX + "samples:")), 1)

    async def test_cancelled_request_releases_without_warming(self):
        pool = await self.make_finished(MEMBERS[1], status="interrupted")
        self.assertEqual(await pool.store.list_json(PREFIX + "claim:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "recent:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "samples:"), [])

class LocalPoolCostReview(unittest.IsolatedAsyncioTestCase):
    async def test_reserved_single_slot_is_not_costed_as_idle(self):
        pool = LocalPool(InMemoryStateStore(), NS(section=lambda _: {"local_pool": {"enabled": True}}))
        endpoint = NS(id=MEMBERS[2], cloud=False, max_concurrency=1)
        status = NS(detail={"processing": 0}, load_headroom=1, cache_generation="g1")
        trace = NS(request_id="migration", payload={"request_id": "migration", "client_id": "c"})
        await pool.store.set_json(PREFIX + "claim:already-selected", {
            "request_id": "already-selected", "endpoint_id": endpoint.id, "owner": "another-owner",
            "phase": "selected", "assigned_at": time.time(), "started_at": None,
            "prompt_bucket": bucket(40000), "output_bucket": bucket(16000, 1024), "generation": "g1"}, 30)
        await pool.store.set_json(PREFIX + "samples:" + endpoint.id, {"items": [
            {"request_id": str(i), "generation": "g1", "at": time.time(), "prompt_bucket": 65536,
             "output_bucket": bucket(16000, 1024), "cache_state": "cold", "first_s": 1, "duration_s": 10} for i in range(5)]}, 3600)
        rows = await pool.costs([endpoint], {endpoint.id: status}, trace=trace, conversation=None,
                               prompt_tokens=40000, output_tokens=16000)
        self.assertEqual(rows[0]["sample_count"], 5)
        self.assertIsNone(rows[0]["queue_s"], "a selected reservation already occupies this single-slot target")

class LocalPoolTierReview(unittest.TestCase):
    def test_approved_peer_migration_survives_fallback_tier_filter(self):
        from ai_router.policy import RoutingPolicy
        ai = NS(id=MEMBERS[0], tier_rank=20, cloud=False)
        edge = NS(id=MEMBERS[1], tier_rank=30, cloud=False)
        settings = NS(section=lambda _: {"local_pool": {"enabled": True}})
        policy = RoutingPolicy(NS(by_id=lambda key: edge if key == edge.id else ai), settings, None, InMemoryStateStore())
        conversation = NS(endpoint_id=edge.id, tier_rank=30)
        candidates, _, _ = policy._conversation_fallback_candidates([ai], conversation, NS())
        self.assertEqual([e.id for e in candidates], [ai.id])

class LocalPoolApiWaitReview(unittest.IsolatedAsyncioTestCase):
    async def test_five_second_reselection_keeps_one_120_second_deadline(self):
        from unittest.mock import AsyncMock, patch
        from ai_router import api
        from ai_router.errors import QueueTimeoutError, NoEligibleModelError, AllLocalCapacityBusyError
        clock = NS(now=1000.0)
        waits = []
        releases = []
        endpoint = NS(id=MEMBERS[1], cloud=False, max_concurrency=1, metadata={}, backend_type="vllm")
        decision = NS(endpoint=endpoint, deployment_id=endpoint.id, deployment_candidates={},
                      affinity="hit", reason="conversation_affinity")
        async def choose(**kwargs):
            if endpoint.id in kwargs["excluded_endpoint_ids"]:
                raise NoEligibleModelError()
            return decision
        async def acquire(*args, **kwargs):
            waits.append(kwargs["timeout_seconds"])
            clock.now += kwargs["timeout_seconds"]
            raise QueueTimeoutError()
        async def release(*args):
            releases.append(args)
        pool = NS(member=lambda e: e.id == endpoint.id, config={"recheck_seconds": 5}, release=release)
        current = NS(policy=NS(choose=choose, local_pool=pool),
                     settings=NS(section=lambda _: {"affinity_capacity_wait_seconds": 120}),
                     scheduler=NS(acquire_deployment_candidates=acquire))
        lease = NS(release_deployment=AsyncMock())
        with patch.object(api, "time", NS(monotonic=lambda: clock.now)), patch.object(api, "_apply_protocol_constraints"), patch.object(api, "_filter_restart_draining_deployments", AsyncMock(return_value=True)):
            with self.assertRaises(AllLocalCapacityBusyError):
                await api._acquire_route_capacity(current, request_id="wait-review", requested_model="auto",
                    evaluation=NS(required_endpoint_id=None), prompt_tokens=100, output_reserve_tokens=20,
                    modalities={"text"}, has_tools=False, required_capabilities=None, conversation=NS(),
                    body={}, api_kind="chat", lease=lease, excluded_endpoints=set(), excluded_deployments=set(),
                    capacity_attempts=0, queue_wait_ms=0, identity=object())
        self.assertEqual(waits, [5.0] * 24)
        self.assertEqual(clock.now, 1120.0)
        self.assertGreaterEqual(len(releases), 24)

    async def test_slow_backend_health_probe_cannot_exceed_positive_wait_budget(self):
        import asyncio
        from ai_router.api import _wait_for_selected_deployment
        endpoint = NS(id=MEMBERS[2], backend_type="llama_cpp")
        async def health(*args, **kwargs):
            await asyncio.sleep(.2)
            return NS(healthy=True, load_headroom=0, detail={"processing": 1})
        current = NS(policy=NS(local_pool=NS(member=lambda _: True)), health=NS(status=health))
        started = time.monotonic()
        result = await _wait_for_selected_deployment(current, NS(endpoint=endpoint), endpoint.id, timeout_seconds=.02)
        elapsed = time.monotonic() - started
        self.assertFalse(result)
        self.assertLess(elapsed, .1, "positive queue budget must also bound an in-flight health probe")

    async def test_actual_exception_finalizer_releases_selected_claim_on_cancel(self):
        import asyncio
        from unittest.mock import AsyncMock
        from ai_router.api import _finish_trace_exception
        from ai_router.route_trace import DecisionTrace
        pool = LocalPool(InMemoryStateStore(), NS(section=lambda _: {"local_pool": {"enabled": True}}))
        endpoint = NS(id=MEMBERS[2], cloud=False, max_concurrency=1)
        trace = DecisionTrace(request_id="cancel-review", client_id="review", key_id="test", protocol="chat", requested_model="auto",
                              excerpt={}, instance_id="test", boot_id="test", settings_hash="test", registry_hash="test")
        await pool.select([endpoint], {endpoint.id: NS(detail={"processing": 0}, load_headroom=1, cache_generation="g1")},
                          trace=trace, conversation=None, prompt_tokens=100, output_tokens=20)
        current = NS(policy=NS(local_pool=pool), route_traces=NS(save=AsyncMock()), audit=NS(write=lambda *a, **k: None))
        await _finish_trace_exception(current, trace, asyncio.CancelledError())
        self.assertEqual(trace.payload["status"], "interrupted")
        self.assertEqual(await pool.store.list_json(PREFIX + "claim:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "recent:"), [])
        self.assertEqual(await pool.store.list_json(PREFIX + "samples:"), [])

    async def test_zero_wait_still_checks_idle_backend_once(self):
        from unittest.mock import AsyncMock
        from ai_router.api import _wait_for_selected_deployment
        endpoint = NS(id=MEMBERS[2], backend_type="llama_cpp")
        probe = AsyncMock(return_value=NS(healthy=True, load_headroom=1, detail={"processing": 0}))
        current = NS(policy=NS(local_pool=NS(member=lambda _: True)), health=NS(status=probe))
        self.assertTrue(await _wait_for_selected_deployment(current, NS(endpoint=endpoint), endpoint.id, timeout_seconds=0))
        probe.assert_awaited_once()

    async def test_cancel_during_backend_probe_propagates_and_stops_probe(self):
        import asyncio
        from ai_router.api import _wait_for_selected_deployment
        entered, exited = asyncio.Event(), asyncio.Event()
        endpoint = NS(id=MEMBERS[2], backend_type="llama_cpp")
        async def health(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        current = NS(policy=NS(local_pool=NS(member=lambda _: True)), health=NS(status=health))
        task = asyncio.create_task(_wait_for_selected_deployment(current, NS(endpoint=endpoint), endpoint.id, timeout_seconds=5))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())
