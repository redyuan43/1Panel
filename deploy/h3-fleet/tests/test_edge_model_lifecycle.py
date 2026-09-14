import asyncio
import json
import pytest
from app.edge_model_lifecycle import EdgeModelLifecycle, GIB, MODEL_ID


class Ops:
    def __init__(self):
        self.data = dict(hashes={'unit': 'pinned'}, qwen_active=True,
                         qwen_idle=True, legacy_idle=True, mem_available_bytes=90*GIB,
                         h3_stopped=True)
        self.calls = []
        self.fence_ok = True
        self.loaded = True
        self.stop_error = False

    async def snapshot(self):
        return {**self.data, 'hashes': dict(self.data['hashes'])}

    async def fence(self, owner):
        self.calls.append('fence')
        return self.fence_ok

    async def unfence(self, owner):
        self.calls.append('unfence')

    async def stop_qwen(self):
        self.calls.append('stop_qwen')
        self.data['qwen_active'] = False
        if self.stop_error:
            raise asyncio.CancelledError

    async def start_qwen(self):
        self.calls.append('start_qwen')
        self.data['qwen_active'] = True

    async def stop_h3(self):
        self.calls.append('stop_h3')
        self.data['h3_stopped'] = True

    async def qwen_ready(self):
        return dict(ready=self.loaded, model_id=MODEL_ID, max_model_len=500000, mtp_tokens=3)


def setup(tmp_path):
    ops = Ops()
    clock = [100.0]
    controller = EdgeModelLifecycle(tmp_path/'state.json', ops, {'unit': 'pinned'}, clock=lambda:clock[0])
    return controller, ops, clock


async def ready(controller, clock):
    assert await controller.before_dispatch('one') is False
    clock[0] += 6
    assert await controller.before_dispatch('one') is True


def test_complete_restore_order_and_identity(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        o.data['h3_stopped']=False
        await c.reconcile([{'execution_id':'one','status':'completed'}])
        assert o.calls.index('stop_h3') < o.calls.index('start_qwen') < o.calls.index('unfence')
        assert c.public()['state']=='idle'
        assert c.public()['queueable']
    asyncio.run(run())


def test_inflight_requests_and_missing_fence_never_stop(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        o.data['qwen_idle']=False
        assert not await c.before_dispatch('one')
        t[0]+=100
        assert not await c.before_dispatch('one')
        o.data['qwen_idle']=True
        o.fence_ok=False
        assert not await c.before_dispatch('one')
        assert 'stop_qwen' not in o.calls
    asyncio.run(run())


def test_cross_instance_lease_and_restart_terminal_recovery(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        c2=EdgeModelLifecycle(c.path,o,{'unit':'pinned'})
        assert not await c2.before_dispatch('two')
        assert await c2.before_dispatch('one')
        await c2.reconcile([])
        assert c2.public()['state']=='idle'
        assert o.calls.count('stop_qwen')==1
    asyncio.run(run())


def test_cancel_after_stop_restores_from_intent(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await c.before_dispatch('one')
        t[0]+=6
        o.stop_error=True
        with pytest.raises(asyncio.CancelledError):
            await c.before_dispatch('one')
        assert not o.data['qwen_active']
        c2=EdgeModelLifecycle(c.path,o,{'unit':'pinned'})
        await c2.reconcile([{'execution_id':'one','status':'cancelled'}])
        assert o.data['qwen_active'] and c2.public()['state']=='idle'
    asyncio.run(run())


def test_restoring_waits_loaded_without_restart_or_new_job(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        o.loaded=False
        rows=[{'execution_id':'one','status':'failed'}]
        await c.reconcile(rows)
        await c.reconcile(rows)
        assert c.public()['state']=='restoring_qwen'
        assert not await c.before_dispatch('two')
        assert o.calls.count('start_qwen')==1
        o.loaded=True
        await c.reconcile(rows)
        assert c.public()['state']=='idle'
    asyncio.run(run())


def test_hash_drift_blocks_controls_and_release(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        o.data['hashes']={'unit':'changed'}
        await c.reconcile([])
        assert c.public()['state']=='blocked'
        assert 'start_qwen' not in o.calls and 'unfence' not in o.calls
        o.data['hashes']={'unit':'pinned'}
        await c.reconcile([])
        assert c.public()['state']=='idle'
    asyncio.run(run())


def test_low_memory_timeout_restores_instead_of_weakening(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        o.data['mem_available_bytes']=79*GIB
        await c.before_dispatch('one');t[0]+=6
        assert not await c.before_dispatch('one')
        assert c.public()['state']=='waiting_memory'
        t[0]+=301
        assert not await c.before_dispatch('one')
        await c.reconcile([{'execution_id':'one','status':'queued'}])
        assert o.data['qwen_active'] and c.public()['state']=='idle'
    asyncio.run(run())


def test_original_inactive_not_started(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        o.data['qwen_active']=False
        await ready(c,t)
        await c.reconcile([])
        assert 'start_qwen' not in o.calls
    asyncio.run(run())


def test_unconfirmed_h3_stop_blocks_qwen_restart(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        o.data['h3_stopped']=False
        async def fail_stop():
            pass
        o.stop_h3=fail_stop
        await c.reconcile([])
        assert c.public()['state']=='blocked'
        assert 'start_qwen' not in o.calls
    asyncio.run(run())


def test_wrong_restored_identity_keeps_fence(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        async def wrong():
            return dict(ready=True,model_id=MODEL_ID,max_model_len=32768,mtp_tokens=3)
        o.qwen_ready=wrong
        await c.reconcile([])
        assert c.public()['state']=='blocked' and 'unfence' not in o.calls
    asyncio.run(run())


def test_corrupt_state_is_failclosed(tmp_path):
    c,o,t=setup(tmp_path)
    c.path.write_text('{broken')
    assert not c.public()['queueable']
    assert c.public()['state']=='blocked'


def test_process_file_lock_cannot_be_bypassed(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        c2=EdgeModelLifecycle(c.path,o,{'unit':'pinned'})
        with c._lease():
            assert not await c2.before_dispatch('two')
        assert not o.calls
    asyncio.run(run())


class SystemHarness:
    def __init__(self):
        from app.edge_model_lifecycle import EdgeSystemOps
        self.states={unit:{'ActiveState':'active','SubState':'running'} for unit in (EdgeSystemOps.QWEN_UNIT,*EdgeSystemOps.LEGACY_UNITS)}
        self.calls=[]
        self.busy=False
        self.healthy=True
        async def command(*args):
            if args[:3]==('systemctl','--user','show'):
                return '\n'.join(f'{k}={v}' for k,v in self.states[args[3]].items())
            if args[:2]==('systemctl','--user') and args[2] in {'start','stop'}:
                self.calls.append((args[2],args[3]))
                self.states[args[3]]={'ActiveState':'active' if args[2]=='start' else 'inactive','SubState':'running' if args[2]=='start' else 'dead'}
                return ''
            raise AssertionError(args)
        async def stop(): pass
        async def stopped(): return True
        self.ops=EdgeSystemOps({},stop_h3=stop,h3_stopped=stopped,command=command,http_client=object())
        self.ops.bind_original(json.loads(json.dumps(self.states)))
        async def get(url,text=False):
            if url.endswith('/queue'):
                return {'queue_running':['job'] if self.busy else [],'queue_pending':[]}
            if url.endswith('/api/projects'): return {'projects':[]}
            if url.endswith('/schedules'): return {'schedules':[]}
            if not self.healthy: raise RuntimeError('not ready')
            return {}
        self.ops._get=get


def test_legacy_units_stop_and_restore_original_states(tmp_path):
    async def run():
        h=SystemHarness()
        assert await h.ops.fence('one')
        assert h.calls==[('stop','h3-video-studio.service'),('stop','comfyui-edge.service')]
        assert await h.ops.unfence('one')
        assert h.calls[-2:]==[('start','comfyui-edge.service'),('start','h3-video-studio.service')]
        assert await h.ops.unfence('one')
        assert len(h.calls)==4
    asyncio.run(run())


def test_legacy_busy_never_stopped_and_unhealthy_restore_waits(tmp_path):
    async def run():
        h=SystemHarness();h.busy=True
        assert not await h.ops.fence('one') and not h.calls
        h.busy=False
        assert await h.ops.fence('one')
        h.healthy=False
        assert not await h.ops.unfence('one')
        h.healthy=True
        assert await h.ops.unfence('one')
    asyncio.run(run())


def test_legacy_original_inactive_preserved(tmp_path):
    async def run():
        h=SystemHarness()
        for unit in h.ops.LEGACY_UNITS:
            h.states[unit]={'ActiveState':'inactive','SubState':'dead'}
        h.ops.bind_original(json.loads(json.dumps(h.states)))
        assert await h.ops.fence('one') and await h.ops.unfence('one')
        assert not h.calls
    asyncio.run(run())


def test_missing_metrics_family_and_nan_cannot_prove_idle(tmp_path):
    async def run():
        h=SystemHarness()
        for text in ['vllm:num_requests_running{} 0', 'vllm:num_requests_running{} NaN\nvllm:num_requests_waiting{} 0']:
            async def get(url,text=False,content=text): return content
            h.ops._get=get
            assert not await h.ops._qwen_idle()
        async def get(url,text=False):
            return 'vllm:num_requests_running{engine="0"} 0\nvllm:num_requests_waiting{engine="0"} 0'
        h.ops._get=get
        assert await h.ops._qwen_idle()
    asyncio.run(run())


def test_fleet_error_status_triggers_restore(tmp_path):
    async def run():
        c,o,t=setup(tmp_path)
        await ready(c,t)
        assert c.public()['transition_started_at']==106
        await c.reconcile([{'execution_id':'one','status':'error'}])
        assert c.public()['state']=='idle' and o.data['qwen_active']
    asyncio.run(run())
