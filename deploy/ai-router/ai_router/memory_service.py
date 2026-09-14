"""Authorized, restartable archive indexing; does not control conversations."""
from __future__ import annotations

import asyncio
import logging
import time
from threading import Event
from uuid import uuid4

from .content_audit import ArchiveReader
from .memory_index import MemoryIndex
from .memory_ingestion import IngestionProgress, indexing_options
from .memory_sources import archived_sources
from .prompt_directives import configured_phrases


async def _thread(function, *args, **kwargs):
    # Cancellation cannot stop a SQLite thread. Wait for it before releasing
    # the worker lease or shutting down the runtime underneath it.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        pending = asyncio.gather(task, return_exceptions=True)
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Repeated shutdown cancellation must not detach a live SQLite
                # thread or release its lease before the transaction ends.
                pass
        outcome = pending.result()[0]
        if isinstance(outcome, Exception):
            logging.getLogger(__name__).warning(
                "sqlite_task_failed_during_cancellation error_type=%s", type(outcome).__name__)
        raise


class HistoryMemory:
    lease_seconds = 120
    renewal_seconds = 20

    def __init__(self, runtime):
        self.runtime = runtime
        self.index = None
        self.reader = None
        self._open_lock = asyncio.Lock()

    def _open(self):
        archive = self.runtime.training
        if archive is None:
            raise ValueError("history recall requires encrypted archive")
        self.reader = ArchiveReader(archive.database_path, archive.key_path)
        self.index = MemoryIndex(self.runtime.settings.runtime_path.with_name("history-memory.sqlite3"),
                                 self.runtime.state_encryption_key)

    async def ensure_open(self):
        # Reading an existing index does not require the ingestion worker lease.
        # Serialize initialization against both foreground readers and the worker.
        if self.index is None:
            async with self._open_lock:
                if self.index is None:
                    await _thread(self._open)

    async def index_page(self, client_id):
        """Compatibility entry for isolated single-account checks."""
        return await self.index_cycle([client_id])

    def options(self):
        settings = self.runtime.settings
        if hasattr(settings, "defaults_path"):
            # Pause/resume must work even when no foreground request reloads the
            # API runtime. Cache by file identity; do not parse YAML per record
            # or mutate unrelated runtime settings from this background worker.
            def stamp(path):
                try:
                    stat = path.stat()
                    return stat.st_ino, stat.st_mtime_ns, stat.st_size
                except FileNotFoundError:
                    return None
            version = stamp(settings.defaults_path), stamp(settings.runtime_path)
            if version != getattr(self, "_options_version", None):
                from .config import Settings
                fresh = Settings(settings.defaults_path, settings.runtime_path)
                self._options_value = indexing_options(fresh.section("compaction").get("history_indexing"))
                self._options_version = version
            return self._options_value
        return indexing_options(settings.section("compaction").get("history_indexing"))

    async def index_cycle(self, owners=None, *, throttle=False):
        current = self.runtime
        options = self.options()
        if not options["enabled"]:
            return {"state": "paused"}
        if owners is None:
            owners = await current.clients.history_accounts()
        owners = [owner for owner in dict.fromkeys(owners)
                  if await current.clients.history_account_policy(owner)]
        if not owners:
            return {"state": "disabled"}
        token = uuid4().hex
        lock = "router:history-memory:index-worker"
        if not await current.store.acquire_lock(lock, token, self.lease_seconds):
            return {"state": "busy"}
        lost = Event()

        async def verify_lease():
            if lost.is_set() or not await current.store.renew_lock(lock, token, self.lease_seconds):
                lost.set()
                raise RuntimeError("history index lease lost")

        async def renew():
            try:
                while True:
                    await asyncio.sleep(self.renewal_seconds)
                    await verify_lease()
            except Exception:
                # Stop extraction at the next chunk, and never checkpoint an
                # uncertain page. SQLite rolls back interrupted additions.
                lost.set()

        heartbeat = asyncio.create_task(renew())
        try:
            await self.ensure_open()
            started = time.monotonic()
            head = await _thread(self.reader.history_head)
            progress = IngestionProgress(self.index)
            states = await _thread(progress.states, owners, head)
            phrases = configured_phrases(current.settings.section("routing").get("prompt_directives", {}))
            inserted = events = 0
            moved = False
            # New records are independent of the old-history frontier. Both
            # lanes get a bounded batch so sustained traffic cannot starve old
            # history, and old history cannot monopolize the worker.
            for lane in ("live", "backfill"):
                field = lane + "_cursor"
                eligible = [owner for owner in owners if states[owner][field] <
                            (head if lane == "live" else states[owner]["boundary"])]
                if not eligible:
                    continue
                cursor = min(states[owner][field] for owner in eligible)
                through = head if lane == "live" else max(states[owner]["boundary"] for owner in eligible)
                initial = cursor
                batch_started = time.monotonic()
                for _ in range(options["batch_records"]):
                    if not self.options()["enabled"]:
                        break
                    await verify_lease()
                    sequence, payload = await _thread(self.reader.history_event, cursor, through)
                    allowed = [owner for owner in eligible
                               if await current.clients.history_account_policy(owner)]
                    if not allowed:
                        return {"state": "disabled"}
                    target = payload.get("request", {}).get("client_id") if payload else None
                    if (target in allowed and sequence > states[target][field]
                            and (lane == "live" or sequence <= states[target]["boundary"])):
                        def sources():
                            for source in archived_sources(payload, client_id=target,
                                    cloud_allowed=True, forbidden_phrases=phrases):
                                if lost.is_set():
                                    raise RuntimeError("history index lease lost")
                                yield source
                        inserted += await _thread(self.index.add, sources())
                    cursor = sequence
                    events += int(payload is not None)
                    if cursor >= through or time.monotonic() - batch_started >= options["max_batch_seconds"]:
                        break
                if cursor > initial:
                    # An interrupted batch replays deduplicated sources. Never
                    # checkpoint a failed write or a revoked owner's frontier.
                    allowed = [owner for owner in eligible
                               if await current.clients.history_account_policy(owner)]
                    await verify_lease()
                    await _thread(progress.advance, allowed, lane, cursor, head)
                    moved = moved or bool(allowed)
            if moved:
                await verify_lease()
                await _thread(progress.sample, owners)
            if throttle and moved:
                # Keep the global lease during cooldown: two API instances must
                # not each consume a separate duty-cycle budget. Cooperative,
                # not a hard cgroup CPU or per-record memory limit.
                elapsed = time.monotonic() - started
                await asyncio.sleep(elapsed * (1 / options["duty_cycle"] - 1))
            return {"state": "indexing" if moved else "caught_up",
                    "inserted_chunks": inserted, "read_events": events, "accounts": len(owners)}
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await current.store.release_lock(lock, token)

    async def run(self):
        while True:
            progressed = False
            try:
                if self.runtime.training is not None:
                    report = await self.index_cycle(throttle=True)
                    progressed = report["state"] == "indexing"
                    if progressed:
                        self.runtime.audit.write("history_index_cycle", **report)
            except Exception as exc:
                self.runtime.audit.write("history_index_failed", error_type=type(exc).__name__)
            await asyncio.sleep(0.1 if progressed else 5)
