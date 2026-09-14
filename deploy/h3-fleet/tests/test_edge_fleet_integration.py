import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.backend_lifecycle import BackendLifecycle
from unittest.mock import AsyncMock
import time


def lifecycle(state='inactive', pid='0'):
    value = BackendLifecycle.__new__(BackendLifecycle)
    value.registry = {'edge': {'unit': 'h3-single-edge-a4.service'}}
    value.dispatcher = SimpleNamespace(backends={'edge': {
        'id': 'edge', 'lane_id': 'fast', 'url': 'http://127.0.0.1:19188'}})
    value.service = lambda _: {'ActiveState': state, 'MainPID': pid}
    return value


def test_managed_stopped_lane_does_not_call_dead_comfy(monkeypatch):
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: 'header\n')
    assert asyncio.run(lifecycle().inactive_lane(SimpleNamespace(id='fast')))


def test_active_worker_never_reported_as_cold_empty():
    assert not asyncio.run(lifecycle('active', '777').inactive_lane(SimpleNamespace(id='fast')))


def test_unknown_listener_blocks_inactive_unit(monkeypatch):
    monkeypatch.setattr(Path, 'read_text', lambda *a, **kw: 'header\n0: 0100007F:4AF4 00000000:0000 0A\n')
    with pytest.raises(RuntimeError, match='unknown_process'):
        asyncio.run(lifecycle().inactive_lane(SimpleNamespace(id='fast')))


def test_unmanaged_lane_cannot_claim_zero_queue():
    assert not asyncio.run(lifecycle().inactive_lane(SimpleNamespace(id='other')))


def preparation():
    value = lifecycle()
    value.edge = SimpleNamespace(public=lambda: {'queueable': True},
        ops=SimpleNamespace(snapshot=AsyncMock(return_value={'legacy_idle': True, 'qwen_active': True, 'qwen_idle': True}), allowed_gpu_pids=AsyncMock(return_value={'42'})),
        _verify=lambda *args: None)
    value.policy = lambda: {'enabled': True}
    value.edge_worker_stopped = AsyncMock(return_value=True)
    value.fleet = SimpleNamespace(draining=False, store=SimpleNamespace(active=lambda: [], validation_lease=lambda: None, studio_batch=lambda: None))
    value.dispatcher.control = lambda *args: None
    value.dispatcher.qualifications = lambda *args: True
    value.dispatcher.backends['edge']['recipes'] = {'H3_I2V_A4_TURBO4': {}}
    value.dispatcher.snapshot = {'ok': True, 'timestamp': time.time(), 'memory_available_bytes': 14 * 1024**3,
        'gpu_process_identities': [('42', 'GPU')], 'gpu_names': {'GPU': 'NVIDIA GB10'}, 'progressive': {'reasons': []}, 'kernel_alerts': [], 'root_available_bytes': 100 * 1024**3,
        'offload_available_bytes': 100 * 1024**3, 'swap_used_bytes': 5 * 1024**3, 'cgroup_swap_bytes': 0}
    return value


def test_preparation_is_not_generation_capacity():
    value = preparation()
    result = asyncio.run(value.preparation_guard())
    assert result['eligible'] and result['requires_model_switch']
    assert 'available_slots' not in result


@pytest.mark.parametrize('fault', ['stale', 'disk', 'swap', 'swap_growth', 'oom', 'unknown_stability', 'hardstop', 'active', 'worker', 'unknown_gpu'])
def test_preparation_keeps_every_non_model_guard(fault):
    value = preparation()
    if fault == 'stale': value.dispatcher.snapshot['timestamp'] -= 20
    if fault == 'disk': value.dispatcher.snapshot['root_available_bytes'] = 0
    if fault == 'swap': value.dispatcher.snapshot['swap_used_bytes'] = 8 * 1024**3
    if fault == 'swap_growth': value.dispatcher.snapshot['progressive']['reasons'] = ['swap_growing']
    if fault == 'oom': value.dispatcher.snapshot['progressive']['reasons'] = ['cgroup_events_changed']
    if fault == 'unknown_stability': value.dispatcher.snapshot['progressive']['reasons'] = ['stability_not_observed']
    if fault == 'hardstop': value.dispatcher.control = lambda name, *args: name == 'recipe_hard_stop'
    if fault == 'active': value.fleet.store.active = lambda: [{'status': 'queued'}]
    if fault == 'unknown_gpu': value.dispatcher.snapshot['gpu_process_identities'].append(('99', 'GPU'))
    if fault == 'worker': value.edge_worker_stopped = AsyncMock(return_value=False)
    assert not asyncio.run(value.preparation_guard())['eligible']


@pytest.mark.parametrize('reasons', [['psi_not_stable'], ['awaiting_continuous_stable_window'], ['psi_not_stable', 'awaiting_continuous_stable_window']])
def test_prepare_may_release_qwen_before_generation_stability_window(reasons):
    value = preparation()
    value.dispatcher.snapshot['progressive']['reasons'] = reasons
    result = asyncio.run(value.preparation_guard())
    assert result['eligible']
    assert result['stability_reasons'] == reasons
    assert 'available_slots' not in result
