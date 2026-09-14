"""A failed unit is stopped for recovery only with complete negative evidence."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.backend_lifecycle import BackendLifecycle

GROUP='/sys/fs/cgroup/h3-edge-compute.slice/h3-single-edge-a4.service'


def fixture(monkeypatch, *, exists=True, populated='0', procs='', listener=False, pid='0', control_group=None):
    v=BackendLifecycle.__new__(BackendLifecycle)
    v.edge=object()
    v.registry={'edge':{'unit':'h3-single-edge-a4.service'}}
    v.dispatcher=SimpleNamespace(backends={'edge':{'id':'edge','lane_id':'fast','url':'http://127.0.0.1:19188','cgroup_path':GROUP}})
    v.service=Mock(return_value={'ActiveState':'failed','MainPID':pid,'ControlGroup':control_group if control_group is not None else GROUP.removeprefix('/sys/fs/cgroup')})
    v.runner=Mock(side_effect=AssertionError('recovery proof must not reset/kill/restart any service'))
    lane=SimpleNamespace(id='fast',enabled=True)
    v.fleet=SimpleNamespace(lanes=[lane],lanes_by_id={'fast':lane},store=SimpleNamespace(active=lambda:[]))
    original_exists=Path.exists;original_read=Path.read_text
    def path_exists(path):return exists if str(path)==GROUP else original_exists(path)
    def read(path,*a,**kw):
        name=str(path)
        if name==GROUP+'/cgroup.events':return 'populated '+populated+'\nfrozen 0\n'
        if name==GROUP+'/cgroup.procs':return procs
        if name in {'/proc/net/tcp','/proc/net/tcp6'}:
            return 'header\n'+('0: 0100007F:4AF4 00000000:0000 0A\n' if listener and name.endswith('tcp6') else '')
        return original_read(path,*a,**kw)
    monkeypatch.setattr(Path,'exists',path_exists);monkeypatch.setattr(Path,'read_text',read)
    return v,lane


def test_empty_failed_worker_restores_without_reset_or_stop(monkeypatch):
    async def run():
        v,lane=fixture(monkeypatch)
        assert await v.edge_worker_stopped()
        await v.stop_edge_worker()
        assert not v.runner.called
        assert not await v.inactive_lane(lane)  # Never advertise it as cold capacity.
    asyncio.run(run())


def test_removed_cgroup_and_empty_systemd_group_are_stopped(monkeypatch):
    v,lane=fixture(monkeypatch,exists=False,control_group='')
    assert asyncio.run(v.inactive_lane(lane,allow_failed=True))


@pytest.mark.parametrize('kwargs',[{'pid':'99'},{'populated':'1'},{'procs':'99\n'},{'control_group':'/other.service'}])
def test_residual_process_or_unknown_group_never_stopped(monkeypatch,kwargs):
    v,lane=fixture(monkeypatch,**kwargs)
    assert not asyncio.run(v.inactive_lane(lane,allow_failed=True))


def test_unknown_listener_blocks_failed_empty_unit(monkeypatch):
    v,lane=fixture(monkeypatch,listener=True)
    with pytest.raises(RuntimeError,match='unknown_process'):
        asyncio.run(v.inactive_lane(lane,allow_failed=True))


def test_pin_mismatch_cannot_be_hidden_by_failed_state(monkeypatch):
    v,lane=fixture(monkeypatch)
    v.service.side_effect=RuntimeError('backend_service_definition_changed')
    with pytest.raises(RuntimeError,match='definition_changed'):
        asyncio.run(v.inactive_lane(lane,allow_failed=True))


def test_failed_exception_is_edge_only(monkeypatch):
    v,lane=fixture(monkeypatch);v.edge=None
    assert not asyncio.run(v.inactive_lane(lane,allow_failed=True))


def test_restart_during_proof_invalidates_stopped_claim(monkeypatch):
    v,lane=fixture(monkeypatch);first=v.service.return_value
    v.service.side_effect=[first,{**first,'ActiveState':'active','MainPID':'44'}]
    assert not asyncio.run(v.inactive_lane(lane,allow_failed=True))
