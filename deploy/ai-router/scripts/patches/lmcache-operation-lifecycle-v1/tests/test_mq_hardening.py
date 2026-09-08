"""CPU tests of extracted production MQ methods with real msgpack and futures.

Platform constructors are skipped; loopback ZMQ tests use local CPU sockets. No GPU inference.
"""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import enum
import importlib.util
import itertools
import logging
import os
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any, Generic, TypeVar, cast

import msgspec
import pytest
import zmq

ROOT = Path(__file__).resolve().parents[1]
_INSTALLED = Path(next(iter(importlib.util.find_spec("lmcache").submodule_search_locations))).parent
CANDIDATE = Path(os.environ.get("SOURCE_ROOT", _INSTALLED)) / "lmcache/v1/multiprocess"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


life = load_file("test_mq_lifecycle", CANDIDATE / "operation_lifecycle.py")


class RequestType(enum.IntEnum):
    STORE = 1
    RETRIEVE = 2
    REGISTER = 3


class HandlerType(enum.Enum):
    SYNC = 1
    BLOCKING = 2
    NON_BLOCKING = 3


def decode(value, cls):
    return msgspec.msgpack.decode(value, type=cls)


def encode(value, cls):
    return msgspec.msgpack.encode(msgspec.convert(value, type=cls))


def response_class(request_type):
    return type(None) if request_type == RequestType.REGISTER else bool


def extracted_classes(path, names, namespace):
    tree = ast.parse(path.read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)


class EventFDNotifier:
    """Real Linux wakeup descriptor for the real ZMQ polling-loop tests."""
    def __init__(self): self.fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
    def fileno(self): return self.fd
    def notify(self): os.eventfd_write(self.fd, 1)
    def consume(self):
        try: os.eventfd_read(self.fd)
        except BlockingIOError: pass
    def close(self): os.close(self.fd)


@pytest.fixture
def runtime():
    ns = dict(dataclass=dataclass, Generic=Generic, T=TypeVar("T"), cast=cast, enum=enum,
              lmcache_deprecate=lambda message: lambda func: func,
              ResponseType=TypeVar("ResponseType", covariant=True), Any=Any,
              RequestUID=int, RequestType=RequestType, HandlerType=HandlerType,
              threading=threading, time=time, queue=queue, Future=Future,
              itertools=itertools, ThreadPoolExecutor=ThreadPoolExecutor,
              create_event_notifier=EventFDNotifier, operation_timeout=life.operation_timeout,
              LMCacheTimeoutError=TimeoutError,
              logger=logging.getLogger("mq-test"), msgspec_decode=decode,
              decode_request_uid=lambda wire: msgspec.msgpack.decode(wire, type=int),
              msgspec_encode=encode, get_response_class=response_class,
              get_payload_classes=lambda kind: [] if kind == RequestType.REGISTER else [bool],
              ERROR_FRAME=life.ERROR_FRAME, RemoteOperationError=life.RemoteOperationError,
              OperationNotStarted=life.OperationNotStarted, zmq=zmq,
              AffinityThreadPool=type("UnusedAffinityPool", (), {}))
    ns["unwrap_request_payloads"] = lambda data, types: [decode(v, t) for v, t in zip(data, types, strict=True)]
    extracted_classes(CANDIDATE / "futures.py", {"MessagingFuture"}, ns)
    extracted_classes(CANDIDATE / "mq.py", {"_OpKind", "_PollOp", "ClientPollingLoop", "MessageQueueClient", "RequestHandlerBase",
        "SyncRequestHandler", "BlockingRequestHandler", "MessageQueueServer"}, ns)
    return SimpleNamespace(**ns)


class Socket:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.sent = []

    def recv_multipart(self):
        value = self.messages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def send_multipart(self, frames, flags=0):
        self.sent.append(frames)

    def close(self, linger=0):
        self.closed = True


def client(rt, *frames):
    c = object.__new__(rt.MessageQueueClient)
    c.socket = Socket(*frames)
    c.pending_futures = {1: rt.MessagingFuture(), 2: rt.MessagingFuture()}
    c._pending_request_types = {1: RequestType.STORE, 2: RequestType.RETRIEVE}
    c._pending_deadlines = {1: time.monotonic() + 10, 2: time.monotonic() + 10}
    c._submission_lock = threading.Lock()
    c._closing = False
    c._close_requested = False
    c._close_done = threading.Event()
    c.input_queue = queue.Queue()
    c._operation_timeout = 120
    return c


def wire(uid, request_type, *payload):
    return [encode(uid, int), encode(request_type, RequestType), *payload]


@pytest.mark.parametrize("payload", [[b"\xc1"], [], [b"\xc3", b"extra"],
    [life.ERROR_FRAME], [life.ERROR_FRAME, b"not_started", b"extra"],
    [life.ERROR_FRAME, b"\xff"], [life.ERROR_FRAME, b"unrecognized"]])
def test_corrupt_response_terminal_unknown_without_losing_other_requests(runtime, payload):
    c = client(runtime, wire(1, RequestType.STORE, *payload), wire(2, RequestType.RETRIEVE, b"\xc3"))
    first, second = c.pending_futures[1], c.pending_futures[2]
    c.process_inbound()
    with pytest.raises(life.RemoteOperationError) as error:
        first.result(timeout=0)
    assert not error.value.safe_to_release
    assert 1 not in c.pending_futures and 1 not in c._pending_deadlines
    assert not second.query()
    c.process_inbound()
    assert second.result(timeout=0) is True


def test_wrong_response_type_never_authorizes_safe_release(runtime):
    c = client(runtime, wire(1, RequestType.RETRIEVE, life.ERROR_FRAME, b"not_started"))
    f = c.pending_futures[1]
    c.process_inbound()
    with pytest.raises(life.RemoteOperationError) as error:
        f.result(0)
    assert not error.value.safe_to_release and 2 in c.pending_futures


@pytest.mark.parametrize("frames", [[], [b"\xc1"], OSError("receive failed"),
    [b"\xc3", encode(RequestType.STORE, RequestType), life.ERROR_FRAME, b"not_started"],
    [msgspec.msgpack.encode("1"), encode(RequestType.STORE, RequestType), b"\xc3"]])
def test_unidentifiable_response_preserves_deadline(runtime, frames):
    c = client(runtime, frames)
    c.process_inbound()
    assert set(c.pending_futures) == {1, 2}
    c._pending_deadlines[1] = time.monotonic() - 1
    f = c.pending_futures[1]
    c.expire_pending()
    with pytest.raises(life.RemoteOperationError) as error:
        f.result(0)
    assert not error.value.safe_to_release
    assert 1 not in c._pending_request_types


def test_truncated_identifiable_response_resolves_unknown(runtime):
    c = client(runtime, [encode(1, int)])
    f = c.pending_futures[1]
    c.process_inbound()
    with pytest.raises(life.RemoteOperationError):
        f.result(0)


def test_duplicate_and_late_responses_are_ignored_before_payload_decode(runtime):
    c = client(runtime, wire(1, RequestType.STORE, b"\xc3"),
               [encode(1, int), b"\xc1"], [encode(2, int), b"\xc1"])
    first, second = c.pending_futures[1], c.pending_futures[2]
    c.process_inbound()
    c.process_inbound()
    assert first.result(0) is True and not second.query()
    c._pending_deadlines[2] = time.monotonic() - 1
    c.expire_pending()
    c.process_inbound()
    with pytest.raises(life.RemoteOperationError) as error:
        second.result(0)
    assert error.value.code == "outcome_unknown_timeout"


def test_void_success_and_safe_typed_error(runtime):
    c = client(runtime, wire(1, RequestType.REGISTER),
               wire(2, RequestType.RETRIEVE, life.ERROR_FRAME, b"not_started"))
    c._pending_request_types[1] = RequestType.REGISTER
    first, second = c.pending_futures[1], c.pending_futures[2]
    c.process_inbound()
    c.process_inbound()
    assert first.result(0) is None
    with pytest.raises(life.RemoteOperationError) as error:
        second.result(0)
    assert error.value.safe_to_release


class Notifier:
    def __init__(self): self.count = 0
    def notify(self): self.count += 1
    def fileno(self): return 7
    def consume(self): pass


def server(rt):
    s = object.__new__(rt.MessageQueueServer)
    s.output_queue = queue.Queue()
    s._output_efd = Notifier()
    s.socket = Socket()
    return s


@pytest.mark.parametrize("failure,safe", [(ValueError("test"), False), (life.OperationNotStarted("test"), True)])
@pytest.mark.parametrize("mode", ["sync", "submit", "callback"])
def test_all_handler_failure_paths_send_typed_correlated_receipt(runtime, failure, safe, mode):
    s = server(runtime)
    def fail(*args): raise failure
    if mode == "sync":
        handler = runtime.SyncRequestHandler([], bool, fail)
    else:
        handler = runtime.BlockingRequestHandler([], bool, lambda: True)
        if mode == "submit":
            handler.executor = SimpleNamespace(submit=fail)
        else:
            done = Future()
            done.set_exception(failure)
            handler.executor = SimpleNamespace(submit=lambda *args: done)
    prefix = [b"client", encode(1, int), encode(RequestType.STORE, RequestType)]
    s._call_handler(handler, [], prefix)
    assert s.output_queue.get_nowait() == prefix + [life.ERROR_FRAME,
        b"not_started" if safe else b"outcome_unknown_remote_error"]
    assert s.output_queue.empty() and s._output_efd.count == 1


def test_malformed_server_request_does_not_stop_next_request(runtime):
    s = server(runtime)
    s.socket = Socket([b"bad"], [b"client", encode(1, int), encode(RequestType.REGISTER, RequestType)])
    s.is_finished = threading.Event()
    calls = []
    s.handlers = {RequestType.REGISTER: runtime.SyncRequestHandler([], type(None), lambda: calls.append(True))}
    class Poller:
        count = 0
        def poll(self, timeout):
            self.count += 1
            if self.count == 2: s.is_finished.set()
            return [(s.socket, 1)]
    s.poller = Poller()
    s._main_loop()
    assert calls == [True] and len(s.socket.sent) == 1


@pytest.mark.parametrize("bad_type", [False, True])
def test_server_rejects_invalid_type_or_missing_handler_with_correlated_unknown(runtime, bad_type):
    s = server(runtime)
    prefix = [b"client", encode(1, int), b"\xc1" if bad_type else encode(RequestType.STORE, RequestType)]
    s.socket = Socket(prefix)
    s.is_finished = threading.Event()
    s.handlers = {}
    class Poller:
        def poll(self, timeout):
            s.is_finished.set()
            return [(s.socket, 1)]
    s.poller = Poller()
    s._main_loop()
    assert s.output_queue.get_nowait() == prefix + [life.ERROR_FRAME, b"outcome_unknown_remote_error"]
    assert s._output_efd.count == 1


@pytest.mark.parametrize("mode", ["sync", "callback"])
def test_response_encode_failure_completes_wire_unknown(runtime, mode):
    s = server(runtime)
    if mode == "sync":
        handler = runtime.SyncRequestHandler([], bool, lambda: object())
    else:
        handler = runtime.BlockingRequestHandler([], bool, lambda: object())
        done = Future()
        done.set_result(object())
        handler.executor = SimpleNamespace(submit=lambda *args: done)
    prefix = [b"client", encode(1, int), encode(RequestType.STORE, RequestType)]
    s._call_handler(handler, [], prefix)
    frames = s.output_queue.get_nowait()
    c = client(runtime, frames[1:])
    f, untouched = c.pending_futures[1], c.pending_futures[2]
    c.process_inbound()
    with pytest.raises(life.RemoteOperationError) as error:
        f.result(0)
    assert error.value.code == "outcome_unknown_remote_error"
    assert not error.value.safe_to_release and not untouched.query()


# Builder-transform idempotence tests remain in the development experiment.
# This shipped suite exercises the installed runtime and needs no builder files.

@pytest.mark.parametrize("payload", [[], [object()]])
def test_outbound_validation_failure_does_not_stop_next_request(runtime, payload):
    c = client(runtime)
    bad, good = runtime.MessagingFuture(), runtime.MessagingFuture()
    c.input_queue.put(runtime.MessageQueueClient.WrappedRequest(3, bad, RequestType.STORE, payload))
    c.input_queue.put(runtime.MessageQueueClient.WrappedRequest(4, good, RequestType.STORE, [True]))
    c.process_outbound_task()
    with pytest.raises(life.RemoteOperationError) as error: bad.result(0)
    assert error.value.safe_to_release
    assert len(c.socket.sent) == 1 and 4 in c.pending_futures and not good.query()


def test_outbound_send_failure_unknown_no_retry_and_next_request_continues(runtime):
    c = client(runtime)
    flags_seen = []
    def send(frames, flags=0):
        flags_seen.append(flags)
        if len(flags_seen) == 1: raise zmq.Again()
        c.socket.sent.append(frames)
    c.socket.send_multipart = send
    bad, good = runtime.MessagingFuture(), runtime.MessagingFuture()
    for uid, future in [(3, bad), (4, good)]:
        c.input_queue.put(runtime.MessageQueueClient.WrappedRequest(uid, future, RequestType.STORE, [True]))
    c.process_outbound_task()
    with pytest.raises(life.RemoteOperationError) as error: bad.result(0)
    assert not error.value.safe_to_release
    assert flags_seen == [zmq.DONTWAIT, zmq.DONTWAIT]
    assert 3 not in c.pending_futures and 4 in c.pending_futures


def test_server_response_drop_is_not_retried_or_changed_to_safe_error(runtime):
    s = server(runtime)
    attempts = []
    def fail(frames, flags=0):
        attempts.append((frames, flags))
        raise zmq.Again()
    s.socket.send_multipart = fail
    handler = runtime.SyncRequestHandler([], bool, lambda: True)
    prefix = [b"client", encode(1, int), encode(RequestType.STORE, RequestType)]
    s._call_handler(handler, [], prefix)
    assert attempts == [(prefix + [b"\xc3"], zmq.DONTWAIT)]
    assert s.output_queue.empty()


def test_owner_close_resolves_sent_unknown_and_queued_not_started(runtime):
    c = client(runtime)
    sent = c.pending_futures[1]
    queued = runtime.MessagingFuture()
    c.input_queue.put(runtime.MessageQueueClient.WrappedRequest(3, queued, RequestType.STORE, [True]))
    c._close_on_owner()
    with pytest.raises(life.RemoteOperationError) as error: sent.result(0)
    assert not error.value.safe_to_release
    with pytest.raises(life.RemoteOperationError) as error: queued.result(0)
    assert error.value.safe_to_release
    assert c.pending_futures == c._pending_request_types == c._pending_deadlines == {}
    assert c.socket.closed
    rejected = c.submit_request(RequestType.STORE, [True])
    with pytest.raises(life.RemoteOperationError) as error: rejected.result(0)
    assert error.value.safe_to_release


@pytest.mark.parametrize("mode", ["sync", "blocking"])
@pytest.mark.parametrize("error_type", [ValueError, life.OperationNotStarted])
def test_real_zmq_loopback_survives_handler_failure(runtime, mode, error_type):
    """Actual ROUTER/DEALER TCP, server thread, shared client loop and msgpack."""
    context = zmq.Context()
    s = runtime.MessageQueueServer("tcp://127.0.0.1:*", context)
    url = s.socket.getsockopt(zmq.LAST_ENDPOINT).decode()
    clients = []
    def handle(value):
        if not value: raise error_type("intentional CPU handler failure")
        return True
    try:
        if mode == "sync":
            s.add_sync_handler(RequestType.STORE, [bool], handle)
        else:
            s.add_blocking_handler(RequestType.STORE, [bool], handle)
            s.add_normal_thread_pool([RequestType.STORE], 1)
        s.start()
        c = runtime.MessageQueueClient(url, context)
        clients.append(c)
        assert c.submit_request(RequestType.STORE, [True]).result(5) is True
        failed = c.submit_request(RequestType.STORE, [False])
        with pytest.raises(life.RemoteOperationError) as error: failed.result(5)
        assert error.value.safe_to_release == (error_type is life.OperationNotStarted)
        assert c.submit_request(RequestType.STORE, [True]).result(5) is True
        assert s.worker_thread.is_alive() and c._polling_loop._thread.is_alive()
    finally:
        for c in clients: c.close()
        s.close()
        context.destroy(linger=0)


def test_real_zmq_close_pending_keeps_other_client_running(runtime):
    context = zmq.Context()
    s = runtime.MessageQueueServer("tcp://127.0.0.1:*", context)
    url = s.socket.getsockopt(zmq.LAST_ENDPOINT).decode()
    started, release = threading.Event(), threading.Event()
    clients = []
    close_threads = []
    original_close = runtime.MessageQueueClient._close_on_owner
    def record_close(c):
        close_threads.append(threading.get_ident())
        return original_close(c)
    runtime.MessageQueueClient._close_on_owner = record_close
    def wait_handler(value):
        started.set()
        assert release.wait(5)
        return value
    try:
        s.add_blocking_handler(RequestType.STORE, [bool], wait_handler)
        s.add_normal_thread_pool([RequestType.STORE], 1)
        s.add_sync_handler(RequestType.RETRIEVE, [bool], lambda value: value)
        s.start()
        first, second = runtime.MessageQueueClient(url, context), runtime.MessageQueueClient(url, context)
        clients.extend([first, second])
        pending = first.submit_request(RequestType.STORE, [True])
        assert started.wait(5)
        owner = first._polling_loop._thread.ident
        closing = [threading.Thread(target=first.close, daemon=True) for _ in range(2)]
        for worker in closing: worker.start()
        for worker in closing: worker.join(5)
        assert not any(worker.is_alive() for worker in closing)
        clients.remove(first)
        with pytest.raises(life.RemoteOperationError) as error: pending.result(0)
        assert not error.value.safe_to_release and close_threads == [owner]
        assert second.submit_request(RequestType.RETRIEVE, [True]).result(5) is True
        release.set()
    finally:
        release.set()
        for c in clients: c.close()
        s.close()
        context.destroy(linger=0)
