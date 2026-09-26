"""Borrow u24's 4060 Ti for video, then restore its original text service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from fastapi import FastAPI, HTTPException, Request
import httpx
import uvicorn


ROOT = Path.home() / ".local/state/siyuan-video-u24-production"
GPU_UUID = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"
TEXT_UNIT = "siyuan-ninfer-restored.service"
WORKER_UNIT = "siyuan-video-u24-workers.service"
logger = logging.getLogger(__name__)


class HostOps:
    def __init__(self, key):
        self.key = key
        self.http = httpx.AsyncClient(timeout=8, trust_env=False)
        self.connections = 0

    async def close(self):
        await self.http.aclose()

    @staticmethod
    async def command(*args):
        def run():
            result = subprocess.run(args, capture_output=True, timeout=90, check=False)
            if result.returncode:
                raise RuntimeError("u24 control command failed: " + args[0])
            return result.stdout.decode(errors="replace")
        return await asyncio.to_thread(run)

    async def unit(self, name):
        result = (await self.command("systemctl", "--user", "show", name,
                                     "-p", "ActiveState", "--value")).strip()
        if result not in {"active", "inactive", "failed"}:
            raise RuntimeError("u24 unit state is unknown")
        return result

    async def start(self, name):
        await self.command("systemctl", "--user", "start", name)

    async def stop(self, name):
        await self.command("systemctl", "--user", "stop", name)

    def text_connections(self):
        return self.connections

    async def gpu_used_mib(self):
        output = await self.command("nvidia-smi", "--query-gpu=uuid,memory.used",
                                    "--format=csv,noheader,nounits")
        for line in output.splitlines():
            uuid, used = [part.strip() for part in line.split(",")]
            if uuid == GPU_UUID:
                return int(used)
        raise RuntimeError("qualified 4060 Ti missing")

    async def worker_ready(self):
        if await self.unit(WORKER_UNIT) != "active":
            return False
        try:
            response = await self.http.get("http://127.0.0.1:19388/system_stats")
            return response.status_code == 200 and bool(response.json().get("devices"))
        except (httpx.HTTPError, ValueError):
            return False

    async def text_ready(self):
        if await self.unit(TEXT_UNIT) != "active":
            return False
        try:
            response = await self.http.get("http://127.0.0.1:18087/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def fleet_idle(self):
        database = ROOT / "data/fleet.sqlite3"
        if not database.is_file():
            return None
        def active_jobs():
            with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True, timeout=5) as connection:
                return connection.execute(
                    "SELECT count(*) FROM jobs WHERE status IN "
                    "('queued','reserved','reconciling','submitted','running','cancelling')"
                ).fetchone()[0]
        try:
            if await asyncio.to_thread(active_jobs):
                return False
            if await self.unit(WORKER_UNIT) in {"inactive", "failed"}:
                return await self.gpu_used_mib() <= 1500
            response = await self.http.get("http://100.120.143.109:19390/api/router/capacity",
                                           headers={"Authorization": "Bearer " + self.key})
            if response.status_code != 200:
                return None
            value = response.json()
            active = value.get("active")
            queues = value.get("queues")
            if not isinstance(active, list) or not isinstance(queues, list):
                return None
            return not active and all(not row.get("queued_or_running") and not row.get("untracked_count")
                                      for row in queues)
        except (httpx.HTTPError, ValueError, TypeError, sqlite3.Error, OSError):
            return None


class Handoff:
    def __init__(self, path, ops, *, idle_seconds=300, drain_seconds=120,
                 stable_seconds=10, clock=time.time, sleep=asyncio.sleep):
        self.path = Path(path)
        self.ops = ops
        self.idle_seconds = idle_seconds
        self.drain_seconds = drain_seconds
        self.stable_seconds = stable_seconds
        self.clock = clock
        self.sleep = sleep
        self.lock = asyncio.Lock()
        self.transition = asyncio.Lock()
        self.task = None

    def read(self):
        if not self.path.exists():
            return {"phase": "text"}
        value = json.loads(self.path.read_text())
        if value.get("phase") not in {"text", "draining", "stopping_text", "starting_worker",
                                     "video", "restoring", "cooldown", "blocked"}:
            raise ValueError("invalid u24 handoff state")
        return value

    def save(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = self.path.with_suffix(".new")
        with pending.open("w") as output:
            json.dump(value, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        pending.chmod(0o600)
        pending.replace(self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def reconcile_startup(self):
        """Recover text after a reboot only when Fleet proves the GPU is free."""
        state = self.read()
        if state["phase"] in {"text", "cooldown"}:
            if (await self.ops.unit(TEXT_UNIT) != "active"
                    or await self.ops.unit(WORKER_UNIT) != "inactive"):
                async with self.transition:
                    await self.restore()
        elif state["phase"] == "video" and await self.ops.unit(TEXT_UNIT) != "inactive":
            async with self.lock:
                self.save({**state, "phase": "blocked", "reason": "text_video_overlap"})

    async def prepare(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"vid_[A-Za-z0-9_-]{1,128}", operation_id):
            raise ValueError("invalid video operation")
        async with self.lock:
            state = self.read()
            phase = state["phase"]
            if phase == "video":
                if await self.ops.unit(TEXT_UNIT) != "inactive":
                    self.save({**state, "phase": "blocked", "reason": "text_video_overlap"})
                    return "unavailable"
                if not await self.ops.worker_ready():
                    return "unavailable"
                if state.get("operation_id") == operation_id:
                    # Repeated admission probes for one queued job cannot keep
                    # the borrowed GPU away from text indefinitely.
                    if (self.clock() - state.get("last_request", 0) >= self.idle_seconds
                            and await self.ops.fleet_idle() is True):
                        return "unavailable"
                else:
                    state["operation_id"] = operation_id
                    state["last_request"] = self.clock()
                    self.save(state)
                return "ready"
            if phase in {"draining", "stopping_text", "starting_worker"}:
                return "preparing"
            if phase == "cooldown" and self.clock() < state.get("until", 0):
                return "unavailable"
            if phase in {"blocked", "restoring"}:
                return "unavailable"
            if await self.ops.unit(TEXT_UNIT) != "active":
                return "unavailable"
            self.save({"phase": "draining", "operation_id": operation_id,
                       "last_request": self.clock()})
            self.task = asyncio.create_task(self.activate())
            return "preparing"

    async def activate(self):
        async with self.transition:
            try:
                resume_phase = self.read()["phase"]
                # The local proxy rejects new text connections once prepare()
                # persists the draining state. Existing streams finish first.
                deadline = self.clock() + self.drain_seconds
                stable = None
                while self.clock() < deadline:
                    if self.ops.text_connections() == 0:
                        if stable is None:
                            stable = self.clock()
                        if self.clock() - stable >= self.stable_seconds:
                            break
                    else:
                        stable = None
                    await self.sleep(1)
                else:
                    raise TimeoutError("text requests did not drain")
                async with self.lock:
                    state = self.read()
                    self.save({**state, "phase": "stopping_text"})
                if await self.ops.unit(TEXT_UNIT) == "active":
                    await self.ops.stop(TEXT_UNIT)
                if await self.ops.unit(TEXT_UNIT) != "inactive":
                    raise RuntimeError("text service did not stop")
                if resume_phase == "starting_worker" and await self.ops.worker_ready():
                    async with self.lock:
                        state = self.read()
                        self.save({**state, "phase": "video", "last_request": self.clock()})
                    return
                for _ in range(30):
                    if await self.ops.gpu_used_mib() <= 1500:
                        break
                    await self.sleep(1)
                else:
                    raise RuntimeError("4060 Ti remained occupied after text stop")
                async with self.lock:
                    state = self.read()
                    self.save({**state, "phase": "starting_worker"})
                await self.ops.start(WORKER_UNIT)
                for _ in range(120):
                    if await self.ops.worker_ready():
                        break
                    if await self.ops.unit(WORKER_UNIT) == "failed":
                        raise RuntimeError("video worker failed during startup")
                    await self.sleep(1)
                else:
                    raise TimeoutError("video worker did not become ready")
                async with self.lock:
                    state = self.read()
                    self.save({**state, "phase": "video", "last_request": self.clock()})
            except Exception as error:
                logger.warning("u24 handoff preparation failed: %s", type(error).__name__)
                await self.restore(after_failure=True, reason=type(error).__name__)

    async def restore(self, *, after_failure=False, only_if_expired=False, reason=None):
        async with self.lock:
            state = self.read()
            if only_if_expired and (state["phase"] != "video"
                                    or self.clock() - state.get("last_request", 0) < self.idle_seconds):
                return
            previous_phase = state["phase"]
            self.save({**state, "phase": "restoring"})
        try:
            if (after_failure and previous_phase in {"draining", "stopping_text", "starting_worker"}
                    and await self.ops.unit(TEXT_UNIT) == "active"
                    and await self.ops.unit(WORKER_UNIT) == "inactive"):
                async with self.lock:
                    self.save({"phase": "cooldown", "until": self.clock() + 300, "reason": reason})
                return
            # Never restore text while Fleet may still own an unknown video.
            if await self.ops.fleet_idle() is not True:
                raise RuntimeError("Fleet is not confirmed idle")
            if await self.ops.unit(WORKER_UNIT) in {"active", "failed"}:
                await self.ops.stop(WORKER_UNIT)
            if await self.ops.unit(WORKER_UNIT) != "inactive":
                raise RuntimeError("video worker did not stop")
            for _ in range(30):
                if await self.ops.gpu_used_mib() <= 1500:
                    break
                await self.sleep(1)
            else:
                raise RuntimeError("4060 Ti did not release video memory")
            if await self.ops.unit(TEXT_UNIT) != "active":
                await self.ops.start(TEXT_UNIT)
            for _ in range(120):
                if await self.ops.text_ready():
                    break
                await self.sleep(1)
            else:
                raise TimeoutError("original text service did not recover")
            async with self.lock:
                self.save({"phase": "cooldown" if after_failure else "text",
                           "until": self.clock() + 300 if after_failure else 0,
                           "reason": reason})
        except Exception as error:
            async with self.lock:
                state = self.read()
                self.save({**state, "phase": "blocked", "reason": type(error).__name__})

    async def watch(self):
        while True:
            try:
                phase = self.read()["phase"]
                if phase in {"text", "cooldown"}:
                    if await self.ops.unit(WORKER_UNIT) != "inactive" or await self.ops.unit(TEXT_UNIT) != "active":
                        async with self.lock:
                            state = self.read()
                            if state["phase"] in {"text", "cooldown"}:
                                self.save({**state, "phase": "blocked", "reason": "unexpected_service_state"})
                elif phase == "video":
                    if await self.ops.unit(TEXT_UNIT) != "inactive":
                        async with self.lock:
                            state = self.read()
                            if state["phase"] == "video":
                                self.save({**state, "phase": "blocked", "reason": "text_video_overlap"})
                        await self.sleep(5)
                        continue
                    idle = await self.ops.fleet_idle()
                    if idle is False:
                        async with self.lock:
                            state = self.read()
                            if state["phase"] == "video":
                                self.save({**state, "last_request": self.clock()})
                    elif idle is True and self.clock() - self.read().get("last_request", 0) >= self.idle_seconds:
                        async with self.transition:
                            if self.read()["phase"] == "video" and await self.ops.fleet_idle() is True:
                                await self.restore(only_if_expired=True)
                elif phase in {"draining", "stopping_text", "starting_worker"} and (self.task is None or self.task.done()):
                    self.task = asyncio.create_task(self.activate())
                elif phase == "restoring":
                    async with self.transition:
                        await self.restore()
            except Exception as error:
                # Fail closed; a later observation may recover, but no text is
                # restarted while Fleet status or ownership is uncertain.
                logger.warning("u24 handoff supervision failed: %s", type(error).__name__)
            await self.sleep(5)


def create_app(handoff, key):
    @asynccontextmanager
    async def lifespan(_app):
        await handoff.reconcile_startup()
        state = handoff.read()
        if state["phase"] in {"text", "cooldown"}:
            if (await handoff.ops.unit(TEXT_UNIT) != "active"
                    or await handoff.ops.unit(WORKER_UNIT) != "inactive"):
                handoff.save({**state, "phase": "blocked", "reason": "unexpected_service_state"})
        proxy = await asyncio.start_server(
            lambda reader, writer: proxy_text(handoff, reader, writer),
            host="0.0.0.0", port=18086,
        )
        task = asyncio.create_task(handoff.watch())
        try:
            yield
        finally:
            proxy.close()
            await proxy.wait_closed()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await handoff.ops.close()

    app = FastAPI(lifespan=lifespan)

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
            raise HTTPException(401, "unauthorized")

    @app.post("/prepare")
    async def prepare(request: Request):
        authenticate(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"operation_id"}:
                raise ValueError("invalid request")
            state = await handoff.prepare(body["operation_id"])
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid request")
        return {"state": state}

    @app.get("/status")
    async def status(request: Request):
        authenticate(request)
        state = handoff.read()
        return {"phase": state["phase"], "operation_id": state.get("operation_id"),
                "updated_at": state.get("last_request"), "reason": state.get("reason")}

    return app


async def proxy_text(handoff, reader, writer, target_port=18087):
    try:
        phase = handoff.read()["phase"]
    except (OSError, ValueError, json.JSONDecodeError):
        phase = "blocked"
    if phase not in {"text", "cooldown"}:
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            length = re.search(rb"(?im)^content-length:[ \t]*(\d+)[ \t]*\r?$", headers)
            if length and b"expect: 100-continue" not in headers.lower():
                size = int(length.group(1))
                if size <= 16 * 1024 * 1024:
                    await asyncio.wait_for(reader.readexactly(size), timeout=10)
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 30\r\n\r\n")
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            pass
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()
        return
    handoff.ops.connections += 1
    upstream = None
    try:
        upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", target_port)

        async def relay(source, destination):
            try:
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                if destination.can_write_eof():
                    with suppress(ConnectionError, OSError):
                        destination.write_eof()

        await asyncio.gather(relay(reader, upstream), relay(upstream_reader, writer))
    except (ConnectionError, OSError):
        pass
    finally:
        if upstream is not None:
            upstream.close()
            with suppress(ConnectionError, OSError):
                await upstream.wait_closed()
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()
        handoff.ops.connections -= 1


def main():
    key = (ROOT / "key").read_text().strip()
    if not key:
        raise RuntimeError("u24 handoff key is empty")
    ops = HostOps(key)
    handoff = Handoff(ROOT / "handoff.json", ops)
    uvicorn.run(create_app(handoff, key), host="100.120.143.109", port=19391,
                access_log=False)


if __name__ == "__main__":
    main()
