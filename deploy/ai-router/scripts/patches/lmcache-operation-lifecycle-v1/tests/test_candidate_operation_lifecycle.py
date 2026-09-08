"""CPU tests for the LMCache candidate operation lifecycle."""
import os
import sys
from concurrent.futures import Future
from pathlib import Path
import pytest

import importlib.util
_INSTALLED = Path(next(iter(importlib.util.find_spec("lmcache").submodule_search_locations))).parent
SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", _INSTALLED))
sys.path.insert(0, str(SOURCE_ROOT))

# Candidate is an overlay; extend the installed package paths for unchanged files.
import lmcache.v1.multiprocess as _mp
_mp.__path__.insert(0, str(SOURCE_ROOT / "lmcache" / "v1" / "multiprocess"))
import lmcache.v1.multiprocess.modules as _mods
import lmcache.integration.vllm as _iv
_iv.__path__.insert(0, str(SOURCE_ROOT / "lmcache" / "integration" / "vllm"))
_mods.__path__.insert(0, str(SOURCE_ROOT / "lmcache" / "v1" / "multiprocess" / "modules"))
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.operation_lifecycle import (
    ERROR_FRAME, RemoteOperationError, StoreFutureGroup,
)

class Target:
    def __init__(self, ids): self.ids=set(ids); self.touched=[]
    def touch_instance(self, iid): self.touched.append(iid); return iid in self.ids

def test_unknown_ping_is_false_but_untracked_probe_is_true():
    from lmcache.v1.multiprocess.modules.management import ManagementModule
    m=object.__new__(ManagementModule); t=Target({7}); m._liveness_targets=(t,)
    assert m.ping(None) is True
    assert m.ping(7) is True
    assert m.ping(8) is False

def test_error_frame_terminates_messaging_future():
    f=MessagingFuture(); f.set_exception(RemoteOperationError("outcome_unknown_remote_error"))
    assert f.query()
    with pytest.raises(RemoteOperationError): f.result(0)

def test_device_wrapper_propagates_remote_error_without_gpu():
    from lmcache.v1.multiprocess.futures import DeviceMessagingFuture
    raw=MessagingFuture(); raw.set_exception(RemoteOperationError("outcome_unknown_remote_error"))
    d=object.__new__(DeviceMessagingFuture); d.raw_future_=raw; d.event_=None
    with pytest.raises(RemoteOperationError): d.result(0)

def test_store_group_does_not_finish_until_every_batch_is_done():
    group=StoreFutureGroup(); first=MessagingFuture(); last=MessagingFuture()
    group.add(first, object()); group.add(last, object())
    assert group.query() is False
    first.set_result(True)
    assert group.query() is False
    last.set_result(True)
    assert group.query() is True
    assert group.result() is True

def test_store_group_unknown_failure_is_not_reported_finished_or_safe():
    group=StoreFutureGroup(); f=MessagingFuture(); group.add(f, object())
    f.set_exception(RemoteOperationError("outcome_unknown_remote_error"))
    with pytest.raises(RemoteOperationError): group.query()
    assert group.pending
    assert group.registration_lost is False

def test_not_started_store_failure_is_terminal_and_explicitly_releasable():
    group=StoreFutureGroup(); f=MessagingFuture(); group.add(f, object())
    f.set_exception(RemoteOperationError("not_started"))
    assert group.query() is True
    assert group.success is False
    assert group.registration_lost is True

def test_late_duplicate_response_cannot_replace_terminal_error():
    f=MessagingFuture(); f.set_exception(RemoteOperationError("outcome_unknown_remote_error"))
    f.set_result(True)
    with pytest.raises(RemoteOperationError): f.result(0)
def test_mq_error_frame_sets_exception_and_removes_pending_entry():
    import msgspec
    from lmcache.v1.multiprocess.mq import MessageQueueClient
    from lmcache.v1.multiprocess.protocol import RequestType
    from lmcache.v1.multiprocess.mq import encode_request_uid
    client=object.__new__(MessageQueueClient)
    uid=41; future=MessagingFuture(); client.pending_futures={uid:future}; client._pending_deadlines={uid:999999999.0}; client._pending_request_types={uid:RequestType.STORE}
    class Socket:
        def recv_multipart(self):
            return [encode_request_uid(uid), msgspec.msgpack.encode(RequestType.STORE), ERROR_FRAME, b"outcome_unknown_remote_error"]
    client.socket=Socket()
    client.process_inbound()
    assert uid not in client.pending_futures
    with pytest.raises(RemoteOperationError): future.result(0)

def _adapter_state():
    import threading
    from lmcache.integration.vllm import vllm_multi_process_adapter as mod
    a=object.__new__(mod.LMCacheMPWorkerAdapter)
    a.store_futures={}; a.retrieve_futures={}; a.store_events={}; a.retrieve_events={}
    a.finished_stores=set(); a.previously_finished=set(); a._returned_finished=set()
    a._dropped_retrieves=set(); a.error_block_ids=set(); a._health_event=threading.Event(); a._health_event.set(); a.dispatcher=None
    a.request_telemetry=type("T",(),{"on_request_store_finished":lambda *args, **kwargs: None, "on_request_retrieve_finished":lambda *args, **kwargs: None})()
    a.model_name="model"; a.parallel_strategy=type("P",(),{"kv_worker_id":0,"kv_world_size":1,"is_kv_writer":True})()
    return a,mod

def test_adapter_finished_store_requires_engine_and_transfer_completion():
    a,_=_adapter_state(); f=MessagingFuture(); a.store_futures['r1']=StoreFutureGroup(); a.store_futures['r1'].add(f, object())
    assert a.get_finished({'r1'}) == (set(), set())
    f.set_result(True); assert a.get_finished({'r1'})[0] == {'r1'}
    f2=MessagingFuture(); a.store_futures['r2']=StoreFutureGroup(); a.store_futures['r2'].add(f2, object())
    assert a.get_finished({'r2'}) == (set(), set()); assert 'r2' in a.store_futures

def test_adapter_retrieve_not_started_reports_once_and_marks_blocks():
    a,_=_adapter_state(); f=MessagingFuture(); f.set_exception(RemoteOperationError('not_started'))
    a.retrieve_futures['r']=(f,[3,4]); a.retrieve_events['r']=object()
    assert a.get_finished({'r'})[1] == {'r'}; assert a.error_block_ids == {3,4}
    assert a.get_finished({'r'})[1] == set()

def test_adapter_health_loss_does_not_drain_pending_store():
    a,_=_adapter_state(); a._health_event.clear(); f=MessagingFuture(); a.store_futures['r']=StoreFutureGroup(); a.store_futures['r'].add(f, object())
    assert a.get_finished({'r'}) == (set(), set()); assert 'r' in a.store_futures

def test_heartbeat_probes_server_then_recovers_registration(monkeypatch):
    import threading
    from lmcache.integration.vllm import vllm_multi_process_adapter as mod
    calls=[]; outcomes=iter([False,True])
    monkeypatch.setattr(mod,'send_ping',lambda client,timeout,instance_id=None: calls.append(instance_id) or next(outcomes))
    h=mod.HeartbeatThread(object(), threading.Event(), interval=1, instance_id=99); h._health_event.clear(); recovered=[]
    h.register_recover_callback(lambda: recovered.append(1) or True); h._execute()
    assert calls == [99,None]; assert recovered == [1]; assert h._health_event.is_set()

def test_reaper_waits_for_active_operation_and_stream_completion():
    from lmcache.v1.multiprocess.modules.lmcache_driven_transfer import ContextEntry, LMCacheDrivenTransferModule
    import threading, time
    class Stream:
        def __init__(self): self.done=False
        def query(self): return self.done
    entry=ContextEntry(cache_context=type('C',(),{'stream':Stream()})(), model_name='m', world_size=2, last_seen=time.monotonic()-1000, active_operations=1, has_liveness_signal=True)
    mod=object.__new__(LMCacheDrivenTransferModule); mod._cache_contexts={1:entry}; mod._lock=threading.Lock(); mod._release_entries=lambda entries: None
    assert mod.reap_stale_instances(1,1) == []
    entry.active_operations=0; assert mod.reap_stale_instances(1,1) == []
    entry.cache_context.stream.done=True; assert mod.reap_stale_instances(1,1) == [1]