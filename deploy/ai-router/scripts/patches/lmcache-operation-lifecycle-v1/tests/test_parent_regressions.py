"""Parent acceptance probes for the exact retained-request failure boundaries."""
import time
from types import SimpleNamespace
import pytest
from test_candidate_operation_lifecycle import _adapter_state
from lmcache.v1.multiprocess.futures import MessagingFuture, DeviceMessagingFuture
from lmcache.v1.multiprocess.operation_lifecycle import RemoteOperationError, StoreFutureGroup


def device(raw):
    f=object.__new__(DeviceMessagingFuture)
    f.raw_future_=raw; f.event_=None; f._completion_deadline=time.monotonic()+120
    return f


def prepare_submit(a, future):
    a._ensure_heartbeat_started=lambda: None
    a._create_key=lambda *args, **kw: None
    a._block_ids_per_group=lambda op: [[3]]
    a.transfer_ctx=SimpleNamespace(submit_store=lambda *args: future)
    a.instance_id=99; a.kv_caches={}; a.blocks_in_chunk=1


def test_finished_marker_from_previous_batch_cannot_free_later_batch():
    a,_=_adapter_state(); first=MessagingFuture(); first.set_result(True)
    prepare_submit(a,first); op=SimpleNamespace(token_ids=[1],start=0,end=1)
    a.submit_store_request('r',op,object())
    assert a.get_finished(set())==(set(),set())
    assert 'r' in a.finished_stores
    second=MessagingFuture(); prepare_submit(a,second)
    a.submit_store_request('r',op,object())
    assert a.get_finished({'r'})==(set(),set())
    assert 'r' in a.store_futures
    second.set_result(True)
    assert a.get_finished(set())==({'r'},set())
    assert a.get_finished({'r'})==(set(),set())


def test_cancelled_receive_is_not_freed_before_device_outcome():
    a,_=_adapter_state(); raw=MessagingFuture(); a.retrieve_futures['r']=(device(raw),[3,4])
    assert a.get_finished({'r'})==(set(),set())
    assert 'r' in a.retrieve_futures
    raw.set_exception(RemoteOperationError('not_started'))
    assert a.get_finished(set())==(set(),{'r'})
    assert a.error_block_ids=={3,4}
    assert a.get_finished({'r'})==(set(),set())


def test_cancel_and_safe_receive_failure_same_poll_has_only_one_completion():
    a,_=_adapter_state(); raw=MessagingFuture(); raw.set_exception(RemoteOperationError('not_started'))
    a.retrieve_futures['r']=(device(raw),[3,4])
    assert a.get_finished({'r'})==(set(),{'r'})
    assert a.get_finished({'r'})==(set(),set())


def test_unknown_receive_outcome_does_not_report_finished():
    a,_=_adapter_state(); raw=MessagingFuture(); raw.set_exception(RemoteOperationError('outcome_unknown_timeout'))
    a.retrieve_futures['r']=(device(raw),[3,4])
    with pytest.raises(RemoteOperationError): a.get_finished({'r'})
    assert 'r' in a.retrieve_futures
    assert not a.error_block_ids
    assert 'r' not in a._returned_finished


def test_registration_starts_heartbeat_without_waiting_for_first_request():
    a,_=_adapter_state(); events=[]
    a._send_register_kv_caches_request=lambda caches: events.append('register')
    a._ensure_heartbeat_started=lambda: events.append('heartbeat')
    a.register_kv_caches({})
    assert events==['register','heartbeat']


def test_expired_device_event_cannot_authorize_release():
    raw=MessagingFuture(); f=device(raw)
    f.event_=object(); f._event_backend=SimpleNamespace(query_event=lambda e: False)
    f._completion_deadline=time.monotonic()-1
    group=StoreFutureGroup(); group.add(f,object())
    with pytest.raises(RemoteOperationError,match='device_timeout'):group.query()
    assert group.pending


def test_failure_of_last_batch_does_not_cover_pending_first_batch():
    a,_=_adapter_state(); pending=MessagingFuture(); rejected=MessagingFuture()
    rejected.set_exception(RemoteOperationError('not_started'))
    group=StoreFutureGroup(); group.add(pending,object()); group.add(device(rejected),object())
    a.store_futures['r']=group
    assert a.get_finished({'r'})==(set(),set())
    pending.set_result(True)
    assert a.get_finished(set())==({'r'},set())
    assert not a.is_healthy
