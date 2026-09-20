import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_router.budget import BudgetReservation, CloudBudget, usage_cost
from ai_router.store import InMemoryStateStore
from ai_router.api import _StreamResourceFinalizer, _public_error_message
from ai_router.errors import RouterError
from ai_router.budget_pricing import budget_rates
from datetime import datetime, timezone


def reservation():
    return BudgetReservation('budget-test', 'request', 2, .44, 1.32, .044)


def test_actual_usage_and_cache_discount():
    amount, source = usage_cost(reservation(), {
        'prompt_tokens': 100000, 'completion_tokens': 10,
        'prompt_cache_hit_tokens': 99000})
    assert amount == pytest.approx((1000 * .44 + 99000 * .044 + 10 * 1.32) / 1000000)
    assert source == 'measured'


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': 1},
    {'prompt_tokens': 1, 'completion_tokens': -1},
    {'prompt_tokens': 1, 'completion_tokens': True},
    {'prompt_tokens': 1, 'completion_tokens': 2, 'output_tokens': 3},
    {'prompt_tokens': 1, 'completion_tokens': 2, 'prompt_cache_hit_tokens': 5}])
def test_missing_or_invalid_usage_is_not_free(usage):
    assert usage_cost(reservation(), usage) == (2, 'estimated_usage_missing')


def test_missing_cache_price_is_explicit_estimate():
    amount, source = usage_cost(replace(reservation(), cached_rate=None), {
        'input_tokens': 100, 'output_tokens': 2,
        'input_tokens_details': {'cached_tokens': 90}})
    assert amount == pytest.approx((100 * .44 + 2 * 1.32) / 1000000)
    assert source.startswith('estimated_')


def test_settlement_releases_reservation_and_is_idempotent():
    async def run():
        store = InMemoryStateStore()
        budget = CloudBudget(store, None)
        r = reservation()
        await store.set_json(r.key, {'spent_usd': 1, 'reservations': {
            r.request_id: {'amount_usd': r.amount_usd}}}, ttl_seconds=3600)
        usage = {'prompt_tokens': 100, 'completion_tokens': 1, 'prompt_cache_hit_tokens': 0}
        await budget.settle(r, usage)
        first = await store.get_json(r.key)
        await budget.settle(r, None)  # background fallback must not charge twice
        await budget.release(r)
        assert await store.get_json(r.key) == first
        assert first['reservations'] == {}
        assert first['spent_usd'] == pytest.approx(1 + (44 + 1.32) / 1000000)
        assert first['measured_spent_usd'] > 0
    asyncio.run(run())


def test_unknown_stream_settlement_does_not_leak_resources():
    async def run():
        current = SimpleNamespace(budget=SimpleNamespace(settle=AsyncMock(side_effect=RuntimeError('ledger'))),
            limiter=SimpleNamespace(release_parallel=AsyncMock()), track_request_finished=AsyncMock())
        lease = SimpleNamespace(release=AsyncMock(), owner_token='owner')
        finalizer = _StreamResourceFinalizer(current, lease, 'client', budget_reservation=reservation())
        with pytest.raises(RuntimeError):
            await finalizer()
        lease.release.assert_awaited_once()
        current.limiter.release_parallel.assert_awaited_once()
        current.track_request_finished.assert_awaited_once()
    asyncio.run(run())


def test_settlement_retry_charges_measured_usage_not_full_reservation():
    async def run():
        store = InMemoryStateStore()
        budget = CloudBudget(store, None)
        r = reservation()
        await store.set_json(r.key, {'spent_usd': 0, 'reservations': {
            r.request_id: {'amount_usd': r.amount_usd}}}, ttl_seconds=3600)
        attempts = 0

        async def settle(reserved, usage):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RouterError('busy', status_code=503, code='cloud_budget_busy')
            await budget.settle(reserved, usage)

        current = SimpleNamespace(budget=SimpleNamespace(settle=settle),
            limiter=SimpleNamespace(release_parallel=AsyncMock()), track_request_finished=AsyncMock())
        finalizer = _StreamResourceFinalizer(current,
            SimpleNamespace(release=AsyncMock(), owner_token='owner'),
            'client', budget_reservation=r)
        finalizer.budget_usage = {'prompt_tokens': 100, 'completion_tokens': 1,
                                 'prompt_cache_hit_tokens': 90}
        with pytest.raises(RouterError):
            await finalizer()
        await finalizer()
        ledger = await store.get_json(r.key)
        assert ledger['spent_usd'] == pytest.approx(usage_cost(r, finalizer.budget_usage)[0])
        assert ledger.get('estimated_spent_usd', 0) == 0
        assert ledger['reservations'] == {}
        await finalizer()
        assert attempts == 2
    asyncio.run(run())


def test_budget_error_is_not_generic_rate_limit():
    error = RouterError('internal', status_code=402, code='cloud_budget_exceeded')
    assert 'Router cloud budget' in _public_error_message(error)


@pytest.mark.parametrize('day,hour,expected', [
    (18, 0, .15), (18, 1, .3), (18, 4, .15), (18, 6, .3),
    (18, 10, .15), (19, 1, .15), (20, 6, .15)])
def test_flash_price_boundaries(day, hour, expected):
    endpoint = SimpleNamespace(metadata={'provider': 'deepseek'},
        provider_model='deepseek-v4-flash', api_base='https://api.deepseek.com')
    timestamp = datetime(2026, 9, day, hour, tzinfo=timezone.utc).timestamp()
    assert budget_rates(endpoint, timestamp)[0] == expected


def test_prices_do_not_apply_to_old_records_or_other_provider():
    endpoint = SimpleNamespace(metadata={'provider': 'deepseek', 'input_cost_per_million_usd': 9},
        provider_model='deepseek-v4-flash', api_base='https://api.deepseek.com')
    assert budget_rates(endpoint, 0)[0] == 9
    endpoint.api_base = 'https://other.invalid'
    assert budget_rates(endpoint, datetime.now(timezone.utc).timestamp())[0] == 9


def test_budget_rejection_uses_402_and_does_not_reserve():
    async def run():
        settings = SimpleNamespace(section=lambda name: {'monthly_budget': .001})
        store = InMemoryStateStore()
        budget = CloudBudget(store, settings)
        endpoint = SimpleNamespace(cloud=True, id='test', api_base='https://example.invalid',
            provider_model='test', metadata={'input_cost_per_million_usd': 1,
                                            'output_cost_per_million_usd': 1})
        with pytest.raises(RouterError) as info:
            await budget.reserve(endpoint, request_id='reject', prompt_tokens=10000, output_reserve_tokens=10)
        assert info.value.status_code == 402
        assert info.value.code == 'cloud_budget_exceeded'
    asyncio.run(run())


def test_reconciliation_is_read_only(tmp_path, monkeypatch, capsys):
    import sqlite3
    import json
    import runpy
    import sys
    from pathlib import Path
    db = tmp_path / 'audit.sqlite'
    timestamp = datetime(2026, 9, 19, 12, tzinfo=timezone.utc).timestamp()
    trace = {'attempts': [{'number': 1, 'steps': [{'node_id': 'upstream_request',
        'timestamp': timestamp, 'evidence': {'backend_usage': {'state': 'complete',
        'input_tokens': 1000, 'cached_tokens': 900}, 'output_tokens': 10,
        'output_tokens_measured': True}}]}]}
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE route_traces(request_id,started_at,selected_model,payload_json)')
        conn.execute('INSERT INTO route_traces VALUES(?,?,?,?)',
                     ('test', timestamp, 'deepseek/deepseek-v4-flash', json.dumps(trace)))
    before = db.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['audit', '--db', str(db), '--since', '2026-09-19T00:00:00+08:00'])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/audit-deepseek-budget.py'), run_name='__main__')
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary['measured_usd'] == pytest.approx((100 * .15 + 900 * .003 + 10 * .6) / 1000000)
    assert summary['safe_to_replace_global_ledger'] is False
    assert db.read_bytes() == before
