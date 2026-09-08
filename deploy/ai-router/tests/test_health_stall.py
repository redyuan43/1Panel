"""Synthetic CPU probes for the vLLM no-progress guard. No live HTTP/model calls."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

HERE = Path(__file__).resolve().parent
LOCAL_COMPONENT = HERE.parent if (HERE.parent / 'ai_router/store.py').exists() else None
SOURCE = Path(os.environ.get('AI_ROUTER_TEST_SOURCE', str(LOCAL_COMPONENT or '/home/ai/github/1Panel/deploy/ai-router')))
sys.path.insert(0, str(SOURCE))
CANDIDATE = HERE / 'ai_router/health.py'
if not CANDIDATE.exists(): CANDIDATE = SOURCE / 'ai_router/health.py'
SPEC = importlib.util.spec_from_file_location('ai_router._health_stall_under_test', CANDIDATE)
health = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = health
SPEC.loader.exec_module(health)
from ai_router.store import InMemoryStateStore
from ai_router.types import Endpoint


def endpoint(name='synthetic-vllm', metadata=None):
    return Endpoint(id=name, public_model='synthetic/model', provider_model='synthetic/model',
        api_base='http://vllm.invalid/v1', node='synthetic', role='local', tier='synthetic', tier_rank=1,
        modalities=('text',), tasks=('general',), safe_context_tokens=8192,
        configured_context_tokens=8192, max_concurrency=8, backend_type='vllm',
        health_url='http://vllm.invalid/health', load_url='http://vllm.invalid/metrics',
        metadata=metadata or {})


def metrics(running=0, waiting=2, computed=100, prompt=100, generated=10, process=1000):
    values = [('vllm:num_requests_running', running), ('vllm:num_requests_waiting', waiting),
              ('vllm:prompt_tokens_total', prompt), ('vllm:generation_tokens_total', generated),
              ('vllm:prompt_tokens_by_source_total{engine="0",source="local_compute"}', computed),
              ('process_start_time_seconds', process)]
    return '\n'.join(f'{name} {value}' for name, value in values if value is not None)


class HealthStallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0.0
        self.wall = 10000.0
        self.text = metrics()
        self.http_health_status = 200
        self.lmcache_status = 200
        self.registered = []
        self.calls = []
        async def handler(request):
            self.calls.append(str(request.url))
            if request.url.host == 'lmcache.invalid':
                if request.url.path == '/status':
                    return httpx.Response(self.lmcache_status, json={'is_healthy': True,
                        'registered_gpu_ids': self.registered, 'storage_manager': {}, 'cache_context_meta': {}})
                return httpx.Response(200, text='process_start_time_seconds 1000\n')
            if request.url.path == '/health': return httpx.Response(self.http_health_status)
            if request.url.path == '/metrics': return httpx.Response(200, text=self.text)
            raise AssertionError('unexpected synthetic URL')
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.monitor = health.HealthMonitor(InMemoryStateStore(), client=self.client)
        self.endpoint = endpoint()
        self.clock = SimpleNamespace(monotonic=lambda: self.now, time=lambda: self.wall)
        self.patcher = mock.patch.object(health, 'time', self.clock)
        self.patcher.start()

    async def asyncTearDown(self):
        await self.monitor.close()
        self.patcher.stop()
        await self.client.aclose()

    async def probe(self, at, text=None, target=None):
        self.now = at
        self.text = text if text is not None else metrics()
        return await self.monitor.status(target or self.endpoint, force_refresh=True)

    async def advance(self, *times, text=None):
        result = None
        for at in times: result = await self.probe(at, text)
        return result

    async def test_health200_waiting_without_tokens_is_rejected_at_30_seconds(self):
        for at in (0, 10, 20, 29.9): self.assertTrue((await self.probe(at)).healthy)
        value = await self.probe(30)
        self.assertFalse(value.healthy)
        self.assertEqual(value.detail['reason'], 'backend_no_progress')
        self.assertEqual(value.detail['backend_progress']['no_progress_seconds'], 30)
        self.assertEqual(value.load_headroom, 0)

    async def test_two_valid_samples_one_window_apart_can_detect_stall(self):
        self.assertTrue((await self.probe(0)).healthy)
        self.assertFalse((await self.probe(30)).healthy)

    async def test_local_compute_counter_without_legacy_prompt_total_is_sufficient(self):
        text = metrics(prompt=None, generated=None)
        self.assertFalse((await self.advance(0,10,20,30,text=text)).healthy)

    async def test_computed_prefill_progress_keeps_long_prefill_healthy(self):
        for at in range(0, 151, 10):
            value = await self.probe(at, metrics(computed=100+at*10))
            self.assertTrue(value.healthy)
        self.assertEqual(value.detail['backend_progress']['state'], 'progressing')

    async def test_prompt_and_generation_progress_each_reset_the_window(self):
        for counter in ('prompt', 'generated'):
            with self.subTest(counter=counter):
                self.monitor._vllm_progress.clear()
                await self.advance(0, 10, 20)
                changed = metrics(**{counter: 200})
                self.assertTrue((await self.probe(25, changed)).healthy)
                self.assertTrue((await self.advance(35, 45, text=changed)).healthy)
                self.assertFalse((await self.probe(55, changed)).healthy)

    async def test_short_remote_cache_wait_and_idle_are_not_failure(self):
        self.endpoint.metadata['lmcache_http_url'] = 'http://lmcache.invalid'
        self.endpoint.metadata['lmcache_expected_registrations'] = 1
        for at in (0, 10, 20, 29):
            value = await self.probe(at)
            self.assertTrue(value.healthy)
            self.assertFalse(value.detail['lmcache']['connector_active'])
        self.assertTrue((await self.probe(30, metrics(computed=200))).healthy)
        for at in range(40, 141, 10): self.assertTrue((await self.probe(at, metrics(waiting=0))).healthy)

    async def test_running_requests_are_not_quarantined_for_no_output_tokens(self):
        for at in range(0, 151, 10):
            self.assertTrue((await self.probe(at, metrics(running=1))).healthy)

    async def test_missing_prefill_counters_do_not_become_zero_or_use_decode_only(self):
        await self.advance(0, 10, 20)
        value = await self.probe(25, metrics(computed=None, prompt=None))
        self.assertTrue(value.healthy)
        self.assertEqual(value.detail['backend_progress']['state'], 'unavailable')
        self.assertTrue((await self.advance(30, 40, 50)).healthy)
        self.assertFalse((await self.probe(60)).healthy)

    async def test_missing_observed_optional_counter_resets_comparison(self):
        await self.advance(0, 10, 20)
        changed = metrics(generated=None)
        value = await self.probe(25, changed)
        self.assertEqual(value.detail['backend_progress']['state'], 'observation_reset')
        self.assertTrue((await self.advance(35, 45, text=changed)).healthy)
        self.assertFalse((await self.probe(55, changed)).healthy)

    async def test_missing_or_invalid_gauges_and_counters_cannot_prove_stall(self):
        for field in ('running', 'waiting', 'computed'):
            for bad in (None, -1, 'NaN', 'Inf', 'broken', 0.5):
                if field == 'computed' and bad is None: continue
                with self.subTest(field=field, value=bad):
                    self.monitor._vllm_progress.clear()
                    await self.advance(0, 10, 20)
                    value = await self.probe(30, metrics(**{field: bad}))
                    self.assertTrue(value.healthy)
                    self.assertEqual(value.detail['backend_progress']['state'], 'unavailable')
                    self.assertTrue((await self.probe(31)).healthy)

    async def test_counter_reset_starts_a_fresh_window(self):
        await self.advance(0, 10, 20)
        reset = metrics(computed=0, prompt=0, generated=0)
        value = await self.probe(25, reset)
        self.assertEqual(value.detail['backend_progress']['state'], 'counter_reset')
        self.assertTrue((await self.advance(35, 45, text=reset)).healthy)
        self.assertFalse((await self.probe(55, reset)).healthy)

    async def test_process_generation_and_runtime_generation_changes_reset_window(self):
        for kind in ('process', 'runtime'):
            with self.subTest(kind=kind):
                self.monitor._vllm_progress.clear()
                await self.advance(0, 10, 20)
                changed = metrics(process=2000) if kind == 'process' else metrics()
                if kind == 'runtime': self.endpoint.metadata['runtime_generation'] = 'new-generation'
                value = await self.probe(25, changed)
                self.assertEqual(value.detail['backend_progress']['state'], 'observation_reset')
                self.assertTrue((await self.advance(35, 45, text=changed)).healthy)
                self.assertFalse((await self.probe(55, changed)).healthy)

    async def test_missing_generation_and_long_sample_gap_reset_window(self):
        await self.advance(0, 10, 20)
        value = await self.probe(25, metrics(process=None))
        self.assertEqual(value.detail['backend_progress']['state'], 'observation_reset')
        value = await self.probe(100, metrics(process=None))
        self.assertTrue(value.healthy)
        self.assertEqual(value.detail['backend_progress']['no_progress_seconds'], 0)

    async def test_http_failure_resets_observation_before_recovery(self):
        await self.advance(0, 10, 20)
        self.http_health_status = 503
        value = await self.probe(25)
        self.assertFalse(value.healthy)
        self.assertNotIn(self.endpoint.id, self.monitor._vllm_progress)
        self.http_health_status = 200
        self.assertTrue((await self.advance(26, 36, 46)).healthy)
        self.assertFalse((await self.probe(56)).healthy)

    async def test_lmcache_disconnection_does_not_block_local_fallback(self):
        self.endpoint.metadata['lmcache_http_url'] = 'http://lmcache.invalid'
        for lmcache_status in (200, 503):
            self.lmcache_status = lmcache_status
            for at in range(0, 61, 10):
                value = await self.probe(at, metrics(computed=100+at))
                self.assertTrue(value.healthy)
                self.assertFalse(value.detail['lmcache']['connector_active'])

    async def test_monotonic_window_ignores_wall_clock_jumps(self):
        for at, wall in ((0,10000), (10,-100000), (20,1e12)):
            self.wall = wall
            self.assertTrue((await self.probe(at)).healthy)
        self.wall = 1
        self.assertFalse((await self.probe(30)).healthy)

    async def test_configurable_window_and_invalid_configuration(self):
        self.endpoint.metadata['backend_no_progress_seconds'] = 5
        self.assertTrue((await self.probe(0)).healthy)
        self.assertTrue((await self.probe(4.9)).healthy)
        self.assertFalse((await self.probe(5)).healthy)
        for bad in (0,-1,True,None,'bad',float('nan'),float('inf')):
            self.monitor._vllm_progress.clear()
            self.endpoint.metadata['backend_no_progress_seconds'] = bad
            value = await self.probe(0)
            self.assertEqual(value.detail['backend_progress']['window_seconds'], 30)

    async def test_quarantine_recovers_after_progress_running_or_no_waiters(self):
        for recovery in (metrics(computed=200), metrics(running=1), metrics(waiting=0)):
            self.monitor._vllm_progress.clear()
            self.assertFalse((await self.advance(0,10,20,30)).healthy)
            value = await self.probe(31,recovery)
            self.assertTrue(value.healthy)
            self.assertNotIn('reason',value.detail)

    async def test_engine_series_are_aggregated_and_resets_not_hidden_by_other_growth(self):
        first = metrics(computed=None) + '\n' + '\n'.join([
            'vllm:prompt_tokens_by_source_total{engine="0",source="local_compute"} 100',
            'vllm:prompt_tokens_by_source_total{engine="1",source="local_compute"} 200'])
        await self.advance(0,10,20,text=first)
        changed = first.replace('local_compute"} 100','local_compute"} 50').replace('local_compute"} 200','local_compute"} 300')
        value = await self.probe(25,changed)
        self.assertEqual(value.detail['backend_progress']['state'],'counter_reset')
        running = changed.replace('vllm:num_requests_running 0', 'vllm:num_requests_running{engine="0"} 0\nvllm:num_requests_running{engine="1"} 1')
        for at in range(30,71,10):
            value = await self.probe(at,running)
            self.assertTrue(value.healthy)
            self.assertEqual(value.detail['running'],1)

    async def test_endpoint_observations_are_independent(self):
        await self.advance(0,10,20)
        other=endpoint('synthetic-other')
        self.assertTrue((await self.probe(25,target=other)).healthy)
        self.assertFalse((await self.probe(30)).healthy)
        self.assertTrue((await self.probe(35,target=other)).healthy)

class WatcherTests(unittest.IsolatedAsyncioTestCase):
    probe = HealthStallTests.probe

    async def asyncSetUp(self):
        await HealthStallTests.asyncSetUp(self)
        self.sleepers = []
        async def controlled_sleep(delay):
            future = asyncio.get_running_loop().create_future()
            item = (self.now + delay, future)
            self.sleepers.append(item)
            try:
                await future
            finally:
                if item in self.sleepers: self.sleepers.remove(item)
        proxy = SimpleNamespace(gather=asyncio.gather, current_task=asyncio.current_task,
            create_task=asyncio.create_task, CancelledError=asyncio.CancelledError,
            Task=asyncio.Task, Lock=asyncio.Lock, sleep=controlled_sleep)
        self.async_patcher = mock.patch.object(health, 'asyncio', proxy)
        self.async_patcher.start()

    async def asyncTearDown(self):
        await self.monitor.close()
        self.async_patcher.stop()
        self.patcher.stop()
        await self.client.aclose()

    async def pump(self):
        for _ in range(25): await asyncio.sleep(0)

    async def advance(self, seconds=5):
        await self.pump()
        self.now += seconds
        self.wall += seconds
        for at, future in list(self.sleepers):
            if at <= self.now and not future.done(): future.set_result(None)
        await self.pump()

    async def cached(self):
        return await self.monitor.store.get_json('router:health:' + self.endpoint.id)

    async def test_no_ui_or_new_request_is_needed_to_publish_stall(self):
        self.assertTrue((await self.probe(0)).healthy)
        for _ in range(5):
            await self.advance()
            self.assertTrue((await self.cached())['healthy'])
        await self.advance()
        cached = await self.cached()
        self.assertFalse(cached['healthy'])
        self.assertEqual(cached['detail']['reason'], 'backend_no_progress')
        self.assertEqual(cached['detail']['backend_progress']['no_progress_seconds'], 30)
        self.assertEqual(self.monitor._vllm_watch_tasks, {})
        self.assertEqual(len([url for url in self.calls if url.endswith('/health')]), 7)

    async def test_queue_empty_publishes_recovery_and_stops_watching(self):
        await self.probe(0)
        self.text = metrics(waiting=0)
        await self.advance()
        self.assertTrue((await self.cached())['healthy'])
        self.assertEqual((await self.cached())['detail']['waiting'], 0)
        self.assertFalse(self.monitor._vllm_watch_tasks)
        count = len(self.calls)
        await self.advance(60)
        self.assertEqual(len(self.calls), count)

    async def test_running_or_token_progress_ends_the_observer(self):
        for recovered in (metrics(running=1), metrics(computed=200)):
            with self.subTest(recovery=recovered):
                await self.probe(self.now)
                self.text = recovered
                await self.advance()
                self.assertTrue((await self.cached())['healthy'])
                self.assertFalse(self.monitor._vllm_watch_tasks)

    async def test_repeated_and_concurrent_foreground_probes_do_not_duplicate_watches(self):
        await self.probe(0)
        task = self.monitor._vllm_watch_tasks[self.endpoint.id]
        await asyncio.gather(*(self.monitor.status(self.endpoint, force_refresh=True) for _ in range(5)))
        self.assertIs(self.monitor._vllm_watch_tasks[self.endpoint.id], task)
        self.assertEqual(len(self.monitor._vllm_watch_tasks), 1)
        await self.pump()
        self.assertEqual(len(self.sleepers), 1)

    async def test_healthy_idle_running_or_missing_metrics_never_start_watching(self):
        for text in (metrics(waiting=0), metrics(running=1), metrics(computed=None, prompt=None), metrics(waiting=None)):
            self.assertTrue((await self.probe(self.now,text)).healthy)
            self.assertFalse(self.monitor._vllm_watch_tasks)
            self.now += 5

    async def test_probe_failure_is_published_and_releases_observer(self):
        await self.probe(0)
        self.http_health_status = 503
        await self.advance()
        self.assertFalse((await self.cached())['healthy'])
        self.assertFalse(self.monitor._vllm_watch_tasks)
        self.assertNotIn(self.endpoint.id, self.monitor._vllm_progress)

    async def test_close_cancels_sleeping_watch_and_is_idempotent(self):
        await self.probe(0)
        await self.pump()
        task = self.monitor._vllm_watch_tasks[self.endpoint.id]
        self.assertEqual(len(self.sleepers), 1)
        await self.monitor.close()
        await self.pump()
        self.assertTrue(task.done())
        self.assertFalse(self.monitor._vllm_watch_tasks)
        self.assertFalse(self.sleepers)
        count = len(self.calls)
        await self.advance(60)
        self.assertEqual(len(self.calls), count)
        await self.monitor.close()

    async def test_close_cancels_inflight_probe_and_releases_http_wait(self):
        await self.probe(0)
        released = asyncio.Event()
        entered = asyncio.Event()
        await self.client.aclose()
        async def blocking_handler(request):
            if request.url.path == '/health':
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.set()
            return httpx.Response(200,text=self.text)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(blocking_handler))
        self.monitor.client = self.client
        await self.advance()
        self.assertTrue(entered.is_set())
        task = self.monitor._vllm_watch_tasks[self.endpoint.id]
        await self.monitor.close()
        self.assertTrue(released.is_set())
        self.assertTrue(task.done())
        self.assertFalse(self.monitor._vllm_watch_tasks)

    async def test_foreground_recovery_cancels_watch_without_self_cancellation(self):
        await self.probe(0)
        await self.pump()
        task = self.monitor._vllm_watch_tasks[self.endpoint.id]
        value = await self.probe(1,metrics(waiting=0))
        self.assertTrue(value.healthy)
        await self.pump()
        self.assertTrue(task.done())
        self.assertFalse(self.monitor._vllm_watch_tasks)

    async def test_unhealthy_state_survives_sparse_requests_until_recovery_evidence(self):
        await self.probe(0)
        for _ in range(6): await self.advance()
        self.assertFalse(self.monitor._vllm_watch_tasks)
        self.assertFalse((await self.probe(300)).healthy)
        self.assertFalse(self.monitor._vllm_watch_tasks)
        self.assertTrue((await self.probe(301,metrics(computed=200))).healthy)
        self.assertFalse(self.monitor._vllm_watch_tasks)

    async def test_repeated_generation_resets_cannot_keep_one_watch_alive_forever(self):
        await self.probe(0)
        for i in range(25):
            self.text = metrics(process=2000+i)
            await self.advance()
        self.assertFalse(self.monitor._vllm_watch_tasks)
        self.assertFalse(self.sleepers)

if __name__ == '__main__': unittest.main(verbosity=2)
