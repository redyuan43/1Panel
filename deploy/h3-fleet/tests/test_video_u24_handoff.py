"""No model or systemd calls: exercise the u24 handoff state transitions."""
import asyncio
import sqlite3
from types import SimpleNamespace

from scripts import serve_video_u24_handoff as module
from scripts.serve_video_u24_handoff import Handoff, HostOps, TEXT_UNIT, WORKER_UNIT, proxy_text


class Ops:
    def __init__(self):
        self.units = {TEXT_UNIT: "active", WORKER_UNIT: "inactive"}
        self.connections = 0
        self.gpu = 12000
        self.idle = True
        self.calls = []

    def text_connections(self):
        return self.connections

    async def unit(self, name):
        return self.units[name]

    async def start(self, name):
        self.calls.append(("start", name))
        self.units[name] = "active"
        self.gpu = 12000 if name == TEXT_UNIT else 10000

    async def stop(self, name):
        self.calls.append(("stop", name))
        self.units[name] = "inactive"
        self.gpu = 0

    async def gpu_used_mib(self):
        return self.gpu

    async def worker_ready(self):
        return self.units[WORKER_UNIT] == "active"

    async def text_ready(self):
        return self.units[TEXT_UNIT] == "active"

    async def fleet_idle(self):
        return self.idle


def test_handoff_prepares_once_and_restores_text(tmp_path):
    async def scenario():
        ops = Ops()
        now = [100.0]

        async def sleep(seconds):
            now[0] += seconds
            await asyncio.sleep(0)

        handoff = Handoff(tmp_path / "handoff.json", ops, stable_seconds=0,
                          clock=lambda: now[0], sleep=sleep)
        assert await handoff.prepare("vid_first") == "preparing"
        assert await handoff.prepare("vid_first") == "preparing"
        await handoff.task
        assert handoff.read()["phase"] == "video"
        assert await handoff.prepare("vid_first") == "ready"
        assert ops.calls == [("stop", TEXT_UNIT), ("start", WORKER_UNIT)]
        ops.idle = False
        assert await ops.fleet_idle() is False
        ops.idle = True
        await handoff.restore()
        assert handoff.read()["phase"] == "text"
        assert ops.calls[-2:] == [("stop", WORKER_UNIT), ("start", TEXT_UNIT)]
    asyncio.run(scenario())


def test_busy_text_does_not_get_stopped(tmp_path):
    async def scenario():
        ops = Ops()
        ops.connections = 1
        now = [100.0]

        async def sleep(seconds):
            now[0] += seconds
            await asyncio.sleep(0)

        handoff = Handoff(tmp_path / "handoff.json", ops, drain_seconds=3,
                          clock=lambda: now[0], sleep=sleep)
        assert await handoff.prepare("vid_wait") == "preparing"
        await handoff.task
        assert handoff.read()["phase"] == "cooldown"
        assert await handoff.prepare("vid_wait") == "unavailable"
        assert ops.calls == []
        assert ops.units[TEXT_UNIT] == "active"
    asyncio.run(scenario())


def test_unknown_fleet_keeps_text_stopped(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units = {TEXT_UNIT: "inactive", WORKER_UNIT: "active"}
        ops.idle = None
        handoff = Handoff(tmp_path / "handoff.json", ops)
        handoff.save({"phase": "video", "last_request": 0})
        await handoff.restore()
        assert handoff.read()["phase"] == "blocked"
        assert ops.units[TEXT_UNIT] == "inactive"
    asyncio.run(scenario())


def test_recent_prepare_prevents_idle_restore(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units = {TEXT_UNIT: "inactive", WORKER_UNIT: "active"}
        ops.gpu = 10000
        now = [1000.0]
        handoff = Handoff(tmp_path / "handoff.json", ops, idle_seconds=300,
                          clock=lambda: now[0])
        handoff.save({"phase": "video", "last_request": 100.0})
        assert await handoff.prepare("vid_new") == "ready"
        await handoff.restore(only_if_expired=True)
        assert handoff.read()["phase"] == "video"
        assert ops.calls == []
    asyncio.run(scenario())


def test_repeated_queued_probe_cannot_hold_gpu_after_idle_window(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units = {TEXT_UNIT: "inactive", WORKER_UNIT: "active"}
        ops.gpu = 10000
        now = [1000.0]
        handoff = Handoff(tmp_path / "handoff.json", ops, idle_seconds=300,
                          clock=lambda: now[0])
        handoff.save({"phase": "video", "operation_id": "vid_queued", "last_request": 1000.0})
        now[0] = 1200.0
        assert await handoff.prepare("vid_queued") == "ready"
        assert handoff.read()["last_request"] == 1000.0
        now[0] = 1301.0
        assert await handoff.prepare("vid_queued") == "unavailable"
        await handoff.restore(only_if_expired=True)
        assert handoff.read()["phase"] == "text"
        assert ops.calls[-2:] == [("stop", WORKER_UNIT), ("start", TEXT_UNIT)]
    asyncio.run(scenario())


def test_text_video_overlap_fails_closed(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units[WORKER_UNIT] = "active"
        handoff = Handoff(tmp_path / "handoff.json", ops)
        handoff.save({"phase": "video", "last_request": 1})
        assert await handoff.prepare("vid_overlap") == "unavailable"
        assert handoff.read()["phase"] == "blocked"
        assert ops.calls == []
    asyncio.run(scenario())


def test_restart_during_worker_start_adopts_owned_worker(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units = {TEXT_UNIT: "inactive", WORKER_UNIT: "active"}
        ops.gpu = 10000
        handoff = Handoff(tmp_path / "handoff.json", ops, stable_seconds=0)
        handoff.save({"phase": "starting_worker", "operation_id": "vid_restart"})
        await handoff.activate()
        assert handoff.read()["phase"] == "video"
        assert ops.calls == []
    asyncio.run(scenario())


def test_proxy_preserves_text_response_and_rejects_new_requests_while_draining(tmp_path):
    async def scenario():
        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        target = await asyncio.start_server(upstream, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]
        ops = Ops()
        handoff = Handoff(tmp_path / "handoff.json", ops)
        server = await asyncio.start_server(
            lambda reader, writer: proxy_text(handoff, reader, writer, target_port),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: example\r\nConnection: close\r\n\r\n")
            await writer.drain()
            assert b"200 OK" in await reader.read()
            writer.close()
            await writer.wait_closed()
            handoff.save({"phase": "draining", "operation_id": "vid_test"})
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: example\r\n\r\n")
            await writer.drain()
            assert b"503 Service Unavailable" in await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            target.close()
            await server.wait_closed()
            await target.wait_closed()
    asyncio.run(scenario())


def test_host_idle_probe_uses_fleet_listener_not_loopback(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(module, "ROOT", tmp_path)
        database = tmp_path / "data/fleet.sqlite3"
        database.parent.mkdir()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE jobs (status TEXT)")
        ops = HostOps("test-key")
        await ops.close()
        calls = []

        class Http:
            async def get(self, url, **kwargs):
                calls.append(url)
                return SimpleNamespace(status_code=200, json=lambda: {"active": [], "queues": []})

        ops.http = Http()

        async def unit(_name):
            return "active"

        ops.unit = unit
        assert await ops.fleet_idle() is True
        assert calls == ["http://100.120.143.109:19390/api/router/capacity"]
    asyncio.run(scenario())


def test_restart_with_missing_text_restores_only_after_fleet_idle(tmp_path):
    async def scenario():
        ops = Ops()
        ops.units[TEXT_UNIT] = "inactive"
        ops.gpu = 0
        ops.idle = None
        handoff = Handoff(tmp_path / "handoff.json", ops)
        handoff.save({"phase": "text"})
        await handoff.reconcile_startup()
        assert handoff.read()["phase"] == "blocked"
        assert ops.calls == []

        ops.idle = True
        handoff.save({"phase": "text"})
        await handoff.reconcile_startup()
        assert handoff.read()["phase"] == "text"
        assert ops.calls == [("start", TEXT_UNIT)]

        ops.units[WORKER_UNIT] = "active"
        ops.units[TEXT_UNIT] = "inactive"
        handoff.save({"phase": "video", "last_request": 0})
        before = list(ops.calls)
        await handoff.reconcile_startup()
        assert handoff.read()["phase"] == "video"
        assert ops.calls == before
    asyncio.run(scenario())
