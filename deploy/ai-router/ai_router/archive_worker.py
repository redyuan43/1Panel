"""Independent archive consumer; restart APIs without stopping this process."""
import argparse
import asyncio
import fcntl
import logging
import os
from pathlib import Path
import shutil
import signal
import time
from types import SimpleNamespace

from redis.asyncio import Redis

from .archive_queue import ArchiveQueue
from .config import Settings
from .content_audit import ArchiveReader
from .history_index import index_completed
from .policy import ConversationRepository
from .store import RedisStateStore
from .training_archive import TrainingArchive, archive_event
from .workbuddy_history import WorkBuddyHistory

log = logging.getLogger(__name__)
OPERATIONS = {"begin", "mark_routed", "set_effective_context", "record_pipeline", "complete", "fail"}


async def publish_history(runtime, trace, archive):
    reader = ArchiveReader(str(archive.database_path), str(archive.key_path))
    await index_completed(runtime, trace, reader)
    await asyncio.to_thread(WorkBuddyHistory(
        runtime.route_traces.database_path, str(archive.database_path), str(archive.key_path)
    ).record, trace)
    history = next((c for c in trace.get("observation", {}).get("content", {}).get("checks", [])
                    if c.get("check") == "workbuddy_history"), {})
    aliases = [v for v in history.get("raw_identities", []) if "v5-history-" not in v]
    if aliases and trace.get("branch_id"):
        await runtime.conversations.map_history(trace["client_id"], tuple(aliases), trace["branch_id"])


class ArchiveWorker:
    def __init__(self, queue, archive, runtime, *, min_free_bytes=2 * 1024**3):
        self.queue, self.archive, self.runtime = queue, archive, runtime
        self.min_free_bytes = min_free_bytes

    async def step(self):
        head = await self.queue.head()
        if head is None:
            return False
        token, encrypted = head
        try:
            if shutil.disk_usage(self.archive.database_path.parent).free < self.min_free_bytes:
                raise OSError("archive disk free space is below reserve")
            event = await self.queue.decode(token, encrypted)
            if event.get("version") != 1 or event.get("token") != token:
                raise ValueError("invalid archive event envelope")
            context = archive_event.set((event["id"], event["created_at"]))
            try:
                operation = event["operation"]
                if operation == "history":
                    await publish_history(self.runtime, event["kwargs"]["trace"], self.archive)
                elif operation in OPERATIONS:
                    arguments = () if operation == "begin" else (token,)
                    await getattr(self.archive, operation)(*arguments, **event["kwargs"])
                else:
                    raise ValueError("unsupported archive event")
            finally:
                archive_event.reset(context)
            if await self.queue.acknowledge(token, encrypted):
                try:
                    await asyncio.to_thread(self.archive.forget_event, event["id"])
                except Exception:
                    # The event has already been consumed. Retaining a receipt
                    # is safe; recreating a retry entry without a payload is not.
                    log.warning("archive receipt cleanup deferred")
            return True
        except Exception as error:
            # Never log content, ciphertext, credentials or exception messages.
            log.error("archive event failed request_hash=%s error_type=%s", token, type(error).__name__)
            await self.queue.failed(token, encrypted, error)
            return False


async def run(retry_token=None):
    path = os.environ.get("AI_ROUTER_TRAINING_DB_PATH", "/training/conversations.sqlite3")
    key = os.environ.get("AI_ROUTER_TRAINING_KEY_PATH", "/training/training.key")
    url = os.environ["AI_ROUTER_REDIS_URL"]
    archive = TrainingArchive(path, key)
    queue = ArchiveQueue(Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2), archive._cipher)
    store = RedisStateStore(url)
    runtime = SimpleNamespace(conversations=ConversationRepository(store, Settings()),
        route_traces=SimpleNamespace(database_path=os.environ.get("AI_ROUTER_ROUTE_TRACE_DB_PATH", "/data/audit/route-traces.sqlite3")))
    worker = ArchiveWorker(queue, archive, runtime)
    stopped = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopped.set)

    async def heartbeat():
        while not stopped.is_set():
            free = shutil.disk_usage(archive.database_path.parent).free
            await queue.redis.set(queue.key("admission"), "ready" if free >= worker.min_free_bytes else "blocked", ex=15)
            await queue.redis.set(queue.key("worker-heartbeat"), str(time.time()), ex=15)
            await asyncio.sleep(5)

    task = None
    try:
        if retry_token:
            if not await queue.redis.hexists(queue.key("quarantine"), retry_token):
                raise ValueError("request is not quarantined")
            await queue.retry(retry_token)
            return
        # A host bind-mounted flock cannot expire while a live worker is writing.
        with open(Path(path).with_suffix(".worker.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            task = asyncio.create_task(heartbeat())
            while not stopped.is_set():
                if task.done():
                    await task
                if not await worker.step():
                    try:
                        await asyncio.wait_for(stopped.wait(), timeout=0.2)
                    except asyncio.TimeoutError:
                        pass
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await queue.close()
        await store.close()
        archive.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry", help="requeue a quarantined request hash after repairing its cause")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args.retry))
