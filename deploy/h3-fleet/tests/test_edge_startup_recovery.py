"""Independent startup failure/cancellation recovery tests; no GPU or service calls."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.backend_lifecycle import BackendLifecycle
from test_edge_model_lifecycle import setup, ready

async def harness(tmp_path):
    edge,ops,clock=setup(tmp_path)
    await ready(edge,clock)
    value=BackendLifecycle.__new__(BackendLifecycle)
    controls={}
    value.edge=edge;value.state={};value.starting={'edge':None}
    value.registry={'edge':{'unit':'h3-single-edge-a4.service'}}
    backend={'id':'edge','url':'http://127.0.0.1:19188','pid':os.getpid()}
    value.dispatcher=SimpleNamespace(backends={'edge':backend},audit=lambda *a:None)
    value.save_control=lambda k,v:controls.update({k:v})
    value.policy=lambda:{'enabled':True}
    value.fleet=SimpleNamespace(draining=False,assignment_lock=asyncio.Lock(),store=SimpleNamespace(release_gate_reason=lambda _:None),client=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=503))))
    value.runner=lambda *a:None
    value.service=lambda _:{'ActiveState':'active','MainPID':str(os.getpid())}
    value.spawned_identity=lambda backend,pid:{'pid':pid,'start_ticks':'123'}
    value.adopt=AsyncMock()
    return value,edge,ops,controls


def test_start_failure_requests_durable_restoration(tmp_path):
    async def run():
        v,e,o,c=await harness(tmp_path)
        def fail(*args):raise RuntimeError('backend_start_failed')
        v.runner=fail
        await v.start_registered('edge',{'execution_id':'one'})
        assert e._read()['state']=='restoring_h3'
        assert c['lifecycle_quarantine:edge']['reason']=='backend_start_failed'
        assert 'edge' not in v.starting
        await e.reconcile([{'execution_id':'one','status':'error'}])
        assert e.public()['state']=='idle' and o.data['qwen_active']
    asyncio.run(run())


def test_disabled_before_start_restores_without_launch(tmp_path):
    async def run():
        v,e,o,c=await harness(tmp_path)
        v.policy=lambda:{'enabled':False}
        def unexpected(*args):raise AssertionError('must not start')
        v.runner=unexpected
        await v.start_registered('edge',{'execution_id':'one'})
        assert c['lifecycle_start:edge']['state']=='not_started'
        assert e._read()['state']=='restoring_h3'
    asyncio.run(run())


def test_cancelled_start_requests_durable_restoration(tmp_path):
    async def run():
        v,e,o,c=await harness(tmp_path)
        v.fleet.client.get=AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await v.start_registered('edge',{'execution_id':'one'})
        assert e._read()['state']=='restoring_h3'
        assert 'edge' not in v.starting
    asyncio.run(run())


def test_never_ready_preserves_spawn_identity_for_recovery(tmp_path,monkeypatch):
    async def run():
        v,e,o,c=await harness(tmp_path)
        v.fleet.client.get=AsyncMock(side_effect=httpx.ConnectError('not listening'))
        monkeypatch.setattr(asyncio,'sleep',AsyncMock())
        await v.start_registered('edge',{'execution_id':'one'})
        assert c['lifecycle_spawned:edge']['pid']==os.getpid()
        assert c['lifecycle_quarantine:edge']['reason']=='backend_start_timeout'
        assert e._read()['state']=='restoring_h3'
    asyncio.run(run())


def test_adopt_failure_retains_owned_pid_before_restoration(tmp_path,monkeypatch):
    async def run():
        from app import recipe_dispatch
        v,e,o,c=await harness(tmp_path)
        v.fleet.client.get=AsyncMock(return_value=SimpleNamespace(status_code=200))
        monkeypatch.setattr(recipe_dispatch,'backend_identity',lambda backend:{'pid':backend['pid'],'start_ticks':backend['start_ticks']})
        v.adopt=AsyncMock(side_effect=RuntimeError('qualification_rejected'))
        await v.start_registered('edge',{'execution_id':'one'})
        assert c['lifecycle_spawned:edge']['pid']==os.getpid()
        assert e._read()['state']=='restoring_h3'
    asyncio.run(run())


def test_restoration_tick_marks_queued_owner_error_without_retry(tmp_path,monkeypatch):
    async def run():
        from app import backend_lifecycle
        from contextlib import contextmanager
        from fastapi import FastAPI
        v,e,o,c=await harness(tmp_path)
        row={'execution_id':'one','prompt_id':'owned','status':'queued','recipe_id':'profile'}
        class Store:
            def get_by_execution(self,execution):return row if execution=='one' else None
            def update(self,prompt,**values):
                assert prompt=='owned';row.update(values);return dict(row)
            @contextmanager
            def _connect(self):
                yield SimpleNamespace(execute=lambda sql:[(row['execution_id'],row['status'])])
            def active(self):return [dict(row)] if row['status']=='queued' else []
        v.fleet.store=Store()
        original_tick=AsyncMock()
        v.dispatcher.policy={'lifecycle_backends':v.registry}
        v.dispatcher.qualifications=lambda *a:True
        v.dispatcher.decision=lambda *a,**kw:{'admission':'allow'}
        v.dispatcher._tick_locked=original_tick
        v.dispatcher.idle_cleanup=AsyncMock();v.dispatcher.close=AsyncMock()
        v.dispatcher.control=lambda *a:None
        v.fleet.recipes=v.dispatcher
        v.edge_reconcile_task=None
        monkeypatch.setattr(backend_lifecycle,'BackendLifecycle',lambda *a:v)
        backend_lifecycle.install(v.fleet,FastAPI(),lambda request:None)
        await e.request_restore('backend_start_timeout')
        await v.dispatcher._tick_locked()
        assert row['status']=='error' and 'backend_start_timeout' in row['failure_reason']
        await v.edge_reconcile_task
        assert e.public()['state']=='idle' and o.data['qwen_active']
        original_tick.assert_not_awaited()
        assert row['status']=='error'  # Restoration must never reset it to queued.
    asyncio.run(run())
