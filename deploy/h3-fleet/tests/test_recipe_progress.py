import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import recipe_progress as module
from app.recipe_progress import RecipeProgress


def event(kind, prompt_id="owned", **data):
    return {"type": kind, "data": {"prompt_id": prompt_id, **data}}


def listener(nodes=("10",)):
    progress = RecipeProgress("http://127.0.0.1:18188", "client", nodes)
    progress.connected = True
    progress.bind("owned")
    return progress


def started(progress, now=0, node="10"):
    progress.feed(event("execution_start", timestamp=1700000000000), now=now)
    progress.feed(event("execution_cached", nodes=[]), now=now)
    progress.feed(event("executing", node=node), now=now)
    progress.feed(event("progress", node=node, value=1, max=8), now=now + 1)


def test_owned_progress_needs_60_seconds_and_allows_long_single_step():
    progress = listener()
    started(progress)
    assert not progress.receipt(now=60)["ready"]
    receipt = progress.receipt(now=61)
    assert receipt["ready"]
    assert receipt["sampler_started_at"] == 1
    assert receipt["last_progress_at"] == 1
    assert receipt["sampler_seconds_observed"] == 60
    assert progress.receipt(now=301)["ready"]
    assert progress.receipt(now=301)["continuous_sampler_seconds"] == 300


def test_loading_and_executing_do_not_start_sampler_window():
    progress = listener()
    progress.feed(event("execution_start"), now=0)
    progress.feed(event("executing", node="4"), now=1)
    progress.feed(event("progress", node="4", value=1, max=2), now=2)
    progress.feed(event("executing", node="10"), now=20)
    progress.feed(event("progress", node="10", value=0, max=8), now=30)
    assert not progress.receipt(now=500)["ready"]
    assert progress.receipt(now=500)["sampler_started_at"] is None
    progress.feed(event("progress", node="10", value=1, max=8), now=501)
    assert not progress.receipt(now=560)["ready"]
    assert progress.receipt(now=561)["ready"]


@pytest.mark.parametrize("kind,data", [
    ("execution_start", {}), ("executing", {"node": "4"}),
    ("progress", {"node": "10", "value": 8, "max": 8}),
    ("execution_cached", {"nodes": ["10"]}), ("execution_success", {}),
    ("execution_error", {}), ("execution_interrupted", {}),
])
@pytest.mark.parametrize("owner", ["foreign", None])
def test_foreign_and_missing_prompt_id_cannot_affect_sampler(kind, data, owner):
    progress = listener()
    started(progress)
    message = event(kind, prompt_id=owner, **data)
    progress.feed(message, now=40)
    receipt = progress.receipt(now=61)
    assert receipt["ready"] and not receipt["cached"]
    assert len(receipt["sampler_progress_events"]) == 1
    assert len(receipt["events"]) == 4


def test_missing_owned_executing_cannot_supply_progress_node():
    progress = listener()
    progress.feed(event("executing", prompt_id=None, node="10"), now=0)
    progress.feed(event("progress", value=1, max=8), now=1)
    assert not progress.receipt(now=100)["ready"]
    progress.feed(event("executing", node="10"), now=101)
    progress.feed(event("progress", value=1, max=8), now=102)
    assert progress.receipt(now=162)["ready"]


def test_prepost_buffer_never_autobinds_and_preserves_original_receive_times():
    progress = RecipeProgress("http://localhost:18188", "client", ["10"])
    progress.connected = True
    progress.feed(event("execution_start", prompt_id="foreign"), now=0)
    started(progress, now=1)
    assert progress.prompt_id is None
    assert not progress.receipt(now=100)["ready"]
    progress.bind("owned")
    assert progress.receipt(now=62)["ready"]
    assert progress.receipt(now=62)["sampler_started_at"] == 2
    assert progress.receipt(now=62)["ignored_unowned_events"] == 1
    progress.bind("owned")
    with pytest.raises(ValueError, match="rebind"):
        progress.bind("foreign")


def test_cached_sampler_never_qualifies_even_with_contradictory_progress():
    progress = listener()
    started(progress)
    progress.feed(event("execution_cached", nodes=["10"]), now=10)
    progress.feed(event("progress", node="10", value=2, max=8), now=80)
    receipt = progress.receipt(now=90)
    assert receipt["cached"] and not receipt["ready"]
    assert "sampler_cached" in receipt["reasons"]


@pytest.mark.parametrize("kind,data", [
    ("progress", {"node": "10", "value": 8, "max": 8}),
    ("executing", {"node": "11"}), ("executing", {"node": None}),
    ("execution_success", {}), ("execution_error", {"exception_type": "OutOfMemoryError"}),
    ("execution_interrupted", {}),
])
def test_finishing_or_leaving_sampler_closes_interval(kind, data):
    progress = listener()
    started(progress)
    progress.feed(event(kind, **data), now=101)
    receipt = progress.receipt(now=200)
    assert not receipt["ready"]
    assert receipt["sampler_finished_at"] == 101
    assert receipt["sampler_seconds_observed"] == 100
    assert receipt["sampler_intervals"][0]["finished_at"] == 101


def test_disconnect_invalidates_continuity_and_does_not_extrapolate_unknown_tail():
    progress = listener()
    started(progress)
    progress.feed(event("progress", node="10", value=2, max=8), now=100)
    assert progress.receipt(now=120)["ready"]
    progress.disconnect()
    receipt = progress.receipt(now=200)
    assert not receipt["ready"] and not receipt["connected"]
    assert receipt["continuous_sampler_seconds"] == 0
    assert receipt["sampler_intervals"][0]["finished_at"] == 100
    assert receipt["sampler_seconds_observed"] == 99
    progress.connected = True
    progress.feed(event("progress", node="10", value=3, max=8), now=201)
    assert not progress.receipt(now=400)["ready"]


def test_multiple_nodes_have_separate_windows_and_no_gap_overlap():
    progress = listener(("10", "20"))
    started(progress)
    progress.feed(event("progress", node="10", value=8, max=8), now=101)
    progress.feed(event("executing", node="11"), now=102)
    progress.feed(event("executing", node="20"), now=300)
    progress.feed(event("progress", node="20", value=1, max=4), now=301)
    assert not progress.receipt(now=350)["ready"]
    assert progress.receipt(now=361)["ready"]
    progress.feed(event("progress", node="20", value=4, max=4), now=401)
    receipt = progress.receipt(now=500)
    assert receipt["sampler_seconds_observed"] == 200
    assert [(segment["started_at"], segment["finished_at"]) for segment in receipt["sampler_intervals"]] == [(1, 101), (301, 401)]


@pytest.mark.parametrize("value,maximum", [(True, 8), (2, True), (2, 0), (0, 8), (-1, 8), (9, 8), (2, 9), (0.5, 8), (float("nan"), 8)])
def test_invalid_progress_fails_closed_and_remains_json_serializable(value, maximum):
    progress = listener()
    started(progress)
    progress.feed(event("progress", node="10", value=value, max=maximum), now=5)
    receipt = progress.receipt(now=100)
    assert not receipt["ready"] and receipt["error"]
    json.dumps(receipt, allow_nan=False)


def test_duplicate_progress_does_not_inflate_confirmed_event_count():
    progress = listener()
    started(progress)
    progress.feed(event("progress", node="10", value=1, max=8), now=50)
    assert progress.receipt(now=61)["ready"]
    assert progress.receipt(now=61)["sampler_intervals"][0]["progress_events"] == 1
    assert progress.receipt(now=61)["last_progress_at"] == 1


def test_regression_and_late_execution_start_fail_closed():
    progress = listener()
    started(progress)
    progress.feed(event("progress", node="10", value=3, max=8), now=10)
    progress.feed(event("progress", node="10", value=2, max=8), now=20)
    assert not progress.receipt(now=100)["ready"]
    other = listener()
    started(other)
    other.feed(event("execution_start"), now=100)
    assert not other.receipt(now=200)["ready"]


def test_binary_previews_and_unknown_events_are_not_progress():
    progress = listener()
    progress.feed(b"preview bytes", now=0)
    progress.feed({"type": "logs", "data": {"prompt_id": "owned", "message": "sampling"}}, now=1)
    assert progress.receipt(now=100)["sampler_started_at"] is None
    progress.feed("{", now=2)
    assert progress.receipt(now=100)["error"] == "invalid_websocket_json"


def test_bounded_buffers_fail_closed(monkeypatch):
    monkeypatch.setattr(module, "MAX_BUFFERED_EVENTS", 1)
    progress = RecipeProgress("http://localhost:18188", "client", ["10"])
    progress.feed(event("execution_start"), now=0)
    progress.feed(event("executing", node="10"), now=1)
    assert progress.error == "prebind_event_buffer_overflow"
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    progress = listener()
    progress.feed(event("execution_start"), now=0)
    progress.feed(event("executing", node="10"), now=1)
    assert progress.error == "owned_event_buffer_overflow"


def test_raw_timestamp_summary_and_receipt_are_detached():
    progress = listener()
    message = event("progress", node="10", value=1, max=8, timestamp=1700000000123)
    original = copy.deepcopy(message)
    progress.feed(json.dumps(message), now=1)
    receipt = progress.receipt(now=61)
    assert receipt["ready"]
    assert receipt["sampler_progress_events"][0]["data"]["timestamp"] == 1700000000123
    assert receipt["sampler_started_at"] == 1
    receipt["sampler_intervals"][0]["node"] = "changed"
    receipt["sampler_progress_events"].clear()
    assert progress.receipt(now=61)["sampler_intervals"][0]["node"] == "10"
    assert message == original


class FakeSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        if isinstance(message, Exception):
            raise message
        return message

    async def close(self):
        self.closed = True
        self.messages.put_nowait(None)


@pytest.mark.parametrize("failure", [None, RuntimeError("transport broke")])
def test_async_connection_buffering_and_cleanup_without_network(monkeypatch, failure):
    import websockets.asyncio.client
    connected = []
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))

    async def scenario():
        socket = FakeSocket()

        async def connect(url, **kwargs):
            connected.append((url, kwargs))
            return socket

        monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
        progress = RecipeProgress("http://localhost:18188/", "client + /?", ["10"])
        await progress.start()
        assert progress.connected
        assert parse_qs(urlsplit(connected[0][0]).query)["clientId"] == ["client + /?"]
        assert connected[0][1]["proxy"] is None
        socket.messages.put_nowait(json.dumps(event("progress", node="10", value=1, max=8)))
        await asyncio.sleep(0)
        assert progress.prompt_id is None
        progress.bind("owned")
        clock[0] = 160
        assert progress.receipt()["ready"]
        if failure is not None:
            socket.messages.put_nowait(failure)
            await asyncio.sleep(0)
            assert not progress.receipt()["ready"]
        await progress.close()
        await progress.close()
        assert socket.closed and not progress.connected and progress._reader.done()
        assert progress.receipt()["closed"]
        if failure is None:
            assert progress.error is None
        with pytest.raises(RuntimeError, match="single-use"):
            await progress.start()

    asyncio.run(scenario())


def test_start_failure_is_visible_before_post(monkeypatch):
    import websockets.asyncio.client

    async def connect(*args, **kwargs):
        raise OSError("offline test refusal")

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    progress = RecipeProgress("http://localhost:18188", "client", ["10"])
    with pytest.raises(OSError):
        asyncio.run(progress.start())
    assert not progress.receipt()["ready"]
    assert progress.error == "websocket_connect_failed:OSError"
    asyncio.run(progress.close())


def test_elapsed_uses_monotonic_not_wall_clock(monkeypatch):
    wall, monotonic = [1000.0], [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: wall[0], monotonic=lambda: monotonic[0]))
    progress = listener()
    progress.feed(event("progress", node="10", value=1, max=8))
    wall[0] = 9000
    monotonic[0] = 110
    assert not progress.receipt()["ready"]
    monotonic[0] = 160
    assert progress.receipt()["ready"]
    assert progress.receipt()["sampler_started_at"] == 1000


def test_event_and_receipt_clock_regression_cannot_open_window():
    progress = listener()
    started(progress, now=100)
    assert not progress.receipt(now=90)["ready"]
    progress.feed(event("progress", node="10", value=2, max=8), now=90)
    assert progress.error == "event_clock_regressed"
    assert not progress.receipt(now=1000)["ready"]


@pytest.mark.parametrize("url", ["http://user:pass@localhost:1", "http://localhost:1/path", "http://localhost:1?x=y", "file:///tmp/a"])
def test_unsafe_origin_rejected(url):
    with pytest.raises(ValueError):
        RecipeProgress(url, "client", ["10"])
