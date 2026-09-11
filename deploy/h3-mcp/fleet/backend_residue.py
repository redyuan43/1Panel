from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4


PREFIX = "backend_residue:"
TERMINAL = {"cancelled", "error", "missing"}
GIB = 1024**3
RESOLVED = {"released", "retired", "superseded"}


def process_start_ticks(process_id):
    if type(process_id) is not int or process_id <= 0:
        raise ValueError("residue_process_identity_unknown")
    try:
        ticks = (Path("/proc") / str(process_id) / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if not ticks.isdigit():
            raise ValueError("residue_process_identity_unknown")
        return ticks
    except FileNotFoundError:
        if not Path("/proc/self/stat").is_file():
            raise ValueError("residue_process_telemetry_unavailable") from None
        return None


def record_terminal(connection, prompt_id):
    job = dict(connection.execute("SELECT * FROM jobs WHERE prompt_id=?", (prompt_id,)).fetchone())
    if job["status"] not in TERMINAL or not job.get("recipe_id") or not job.get("backend_json"):
        return
    binding = json.loads(job["backend_json"])
    record = json.loads(job.get("reconciliation_json") or "null")
    settled = bool(record and record.get("state") == "finished" and connection.execute(
        "SELECT 1 FROM recipe_audit WHERE event='task_finished' AND json_extract(details_json,'$.prompt_id')=? LIMIT 1",
        (prompt_id,)).fetchone())
    receipt = {"schema_version": 1, "prompt_id": prompt_id, "execution_id": job.get("execution_id"),
               "backend_id": binding["id"], "identity": binding["identity"], "state": "pending",
               "settled": settled, "terminal_status": job["status"], "terminal_at": job["updated_at"],
               "created_at": time.time()}
    inserted = connection.execute("INSERT OR IGNORE INTO controls(name,value) VALUES (?,?)",
                                  (PREFIX + prompt_id, json.dumps(receipt))).rowcount
    if inserted:
        warm_name = "warm:" + binding["id"]
        row = connection.execute("SELECT value FROM controls WHERE name=?", (warm_name,)).fetchone()
        warm = json.loads(row[0]) if row else {}
        if (warm.get("runtime_version") == binding.get("runtime_version")
                and type(warm.get("idle_since")) in (int, float) and warm["idle_since"] <= job["updated_at"]
                and ("identity" not in warm or warm["identity"] == binding["identity"])):
            connection.execute("DELETE FROM controls WHERE name=?", (warm_name,))


def record_settled(connection, event, details):
    if event != "task_finished":
        return
    name = PREFIX + details["prompt_id"]
    row = connection.execute("SELECT value FROM controls WHERE name=?", (name,)).fetchone()
    if row:
        receipt = json.loads(row[0])
        receipt.update(settled=True, settled_at=time.time())
        connection.execute("UPDATE controls SET value=? WHERE name=?", (json.dumps(receipt), name))


class BackendResidue:
    poll_seconds = 10
    poll_interval = 0.25

    def __init__(self, fleet):
        self.fleet = fleet
        self.dispatcher = fleet.recipes
        self._history_recovered = False

    def recover_history(self):
        if self._history_recovered:
            return
        with self.fleet.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute("SELECT prompt_id FROM jobs WHERE status IN ('cancelled','error','missing') AND recipe_id IS NOT NULL AND backend_json IS NOT NULL").fetchall():
                record_terminal(connection, row[0])
        self._history_recovered = True

    def records(self):
        self.recover_history()
        with self.fleet.store._connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT value FROM controls WHERE name LIKE ?", (PREFIX + "%",))]

    def update(self, records, **values):
        with self.fleet.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for receipt in records:
                name = PREFIX + receipt["prompt_id"]
                current = json.loads(connection.execute("SELECT value FROM controls WHERE name=?", (name,)).fetchone()[0])
                current.update(values, updated_at=time.time())
                connection.execute("UPDATE controls SET value=? WHERE name=?", (json.dumps(current), name))
                if values.get("state") == "released":
                    warm_name = "warm:" + current["backend_id"]
                    row = connection.execute("SELECT value FROM controls WHERE name=?", (warm_name,)).fetchone()
                    warm = json.loads(row[0]) if row else {}
                    if (warm.get("runtime_version") == current["identity"].get("runtime_version")
                            and ("identity" not in warm or warm["identity"] == current["identity"])):
                        connection.execute("DELETE FROM controls WHERE name=?", (warm_name,))

    def claim(self, records):
        with self.fleet.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = [json.loads(connection.execute("SELECT value FROM controls WHERE name=?",
                       (PREFIX + receipt["prompt_id"],)).fetchone()[0]) for receipt in records]
            if any(receipt["state"] != "pending" for receipt in current):
                return False
            operation_id = uuid4().hex
            for receipt in current:
                receipt.update(state="unload_requested", free_operation_id=operation_id, requested_at=time.time())
                connection.execute("UPDATE controls SET value=? WHERE name=?",
                                   (json.dumps(receipt), PREFIX + receipt["prompt_id"]))
        return True

    def superseding_job(self, receipt):
        if receipt["state"] != "pending" or "terminal_at" not in receipt:
            return None
        warm = self.dispatcher.control("warm:" + receipt["backend_id"], {})
        if (warm.get("runtime_version") != receipt["identity"].get("runtime_version")
                or type(warm.get("idle_since")) not in (int, float)
                or warm["idle_since"] <= receipt["terminal_at"]
                or "identity" in warm and warm["identity"] != receipt["identity"]):
            return None
        with self.fleet.store._connect() as connection:
            jobs = connection.execute(
                "SELECT jobs.* FROM jobs WHERE status='completed' AND recipe_id=? AND updated_at>? AND updated_at<=? "
                "AND EXISTS (SELECT 1 FROM recipe_audit WHERE event='task_finished' "
                "AND json_extract(details_json,'$.prompt_id')=jobs.prompt_id AND timestamp>=jobs.updated_at)",
                (warm.get("recipe_id"), receipt["terminal_at"], warm["idle_since"]))
            for row in jobs:
                job = dict(row)
                if job["prompt_id"] == receipt["prompt_id"] or warm.get("prompt_id", job["prompt_id"]) != job["prompt_id"]:
                    continue
                binding = json.loads(job.get("backend_json") or "{}")
                if binding.get("id") == receipt["backend_id"] and binding.get("identity") == receipt["identity"]:
                    return job["prompt_id"]
        return None

    def no_busy(self, backend):
        from .admission import BUSY
        for job in self.fleet.store.active():
            if job["status"] not in BUSY:
                continue
            try:
                binding = json.loads(job["backend_json"]) if job.get("backend_json") else None
                gpu_uuid = binding["identity"]["gpu_uuid"] if binding else self.fleet.lanes_by_id[job["lane_id"]].gpu_uuid
            except (KeyError, TypeError, ValueError):
                raise ValueError("residue_busy_identity_unknown") from None
            if gpu_uuid == backend["gpu_uuid"]:
                raise ValueError("residue_same_gpu_busy")

    async def idle_identity(self, backend, records):
        from .recipe_dispatch import backend_identity
        identity = await asyncio.to_thread(backend_identity, backend)
        if any(receipt["identity"] != identity for receipt in records):
            raise ValueError("residue_process_identity_changed")
        self.no_busy(backend)
        urls = {peer["url"] for peer in self.dispatcher.backends.values() if peer["gpu_uuid"] == backend["gpu_uuid"]}
        urls.update(lane.url for lane in self.fleet.lanes if lane.gpu_uuid == backend["gpu_uuid"])
        for url in sorted(urls):
            response = await self.fleet.client.get(url + "/queue", timeout=5)
            response.raise_for_status()
            queue = response.json()
            if any(not isinstance(queue.get(key), list) or queue[key] for key in ("queue_running", "queue_pending")):
                raise ValueError("residue_same_gpu_queue_not_empty_or_unknown")
        self.no_busy(backend)
        if await asyncio.to_thread(backend_identity, backend) != identity:
            raise ValueError("residue_process_identity_changed")

    async def released(self, backend):
        from .recipe_dispatch import backend_identity
        self.no_busy(backend)
        identity = await asyncio.to_thread(backend_identity, backend)
        health = await self.fleet.lane_health(self.dispatcher.lane_for_binding(backend))
        total, free = health.get("vram_total"), health.get("vram_free")
        if not health.get("ok") or type(total) is not int or type(free) is not int or not 0 <= free <= total or total <= 0:
            return False
        if total - free > GIB:
            return False
        sample = await self.dispatcher.sample()
        stamp = sample.get("timestamp", 0)
        memory = sample.get("gpu_process_memory")
        if (not sample.get("ok") or not 0 <= time.time() - stamp <= 10 or not isinstance(memory, dict)
                or sample.get("backend_identities", {}).get(backend["id"]) != identity):
            return False
        used = memory.get(str(identity["pid"]) + ":" + identity["gpu_uuid"])
        self.no_busy(backend)
        return type(used) is int and 0 <= used <= GIB and await asyncio.to_thread(backend_identity, backend) == identity

    async def unload(self, backend, records):
        try:
            await self.idle_identity(backend, records)
            if await self.released(backend):
                await self.idle_identity(backend, records)
                self.update(records, state="released", reason="verified_gpu_release", confirmed_at=time.time())
                return True
            await self.idle_identity(backend, records)
            if not self.claim(records):
                self.update(records, reason="residue_unload_unknown_requires_reconciliation")
                return False
            response = await self.fleet.client.post(backend["url"] + "/free",
                json={"unload_models": True, "free_memory": True}, timeout=15)
            response.raise_for_status()
            deadline = time.monotonic() + self.poll_seconds
            while time.monotonic() < deadline:
                if await asyncio.wait_for(self.released(backend), max(0.001, deadline - time.monotonic())):
                    await self.idle_identity(backend, records)
                    self.update(records, state="released", reason="verified_gpu_release", confirmed_at=time.time())
                    self.dispatcher.audit("residue_unload_confirmed", {"backend_id": backend["id"]})
                    return True
                await asyncio.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
            self.update(records, state="unload_unknown", reason="residue_unload_not_yet_confirmed")
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) and str(error).startswith("residue_") else "residue_verification_or_free_failed"
            self.update(records, reason=reason)
        return False

    async def cleanup(self):
        for receipt in self.records():
            if receipt["settled"]:
                continue
            job = self.fleet.store.get(receipt["prompt_id"])
            try:
                await self.dispatcher.completed(job, {})
                self.fleet.cleanup_job_inputs(job)
            except Exception:
                self.update([receipt], reason="residue_terminal_settlement_failed")
        grouped, unverified = {}, set()
        for receipt in self.records():
            if receipt["state"] in RESOLVED:
                continue
            try:
                identity = receipt["identity"]
                if not str(identity["start_ticks"]).isdigit():
                    raise ValueError("residue_process_identity_unknown")
                current_ticks = await asyncio.to_thread(process_start_ticks, identity["pid"])
                if current_ticks != str(identity["start_ticks"]):
                    if receipt["settled"]:
                        self.update([receipt], state="retired", reason="old_worker_generation_absent", retired_at=time.time())
                        continue
                elif receipt["settled"]:
                    successor = self.superseding_job(receipt)
                    if successor:
                        self.update([receipt], state="superseded", reason="newer_completed_same_worker",
                                    superseded_by=successor, superseded_at=time.time())
                        continue
            except (KeyError, ValueError, OSError, IndexError):
                self.update([receipt], reason="residue_process_telemetry_unavailable")
                unverified.add(receipt["backend_id"])
                grouped.setdefault(receipt["backend_id"], []).append(receipt)
                continue
            grouped.setdefault(receipt["backend_id"], []).append(receipt)
        confirmed = True
        for backend_id, records in grouped.items():
            backend = self.dispatcher.backends.get(backend_id)
            if not backend or backend_id in unverified or not all(receipt["settled"] for receipt in records):
                confirmed = False
                continue
            confirmed = await self.unload(backend, records) and confirmed
        return confirmed


def install(fleet):
    residue = BackendResidue(fleet)
    fleet.backend_residue = residue
    dispatcher = fleet.recipes
    previous_completed = dispatcher.completed
    previous_cleanup = dispatcher.idle_cleanup
    previous_blocking = dispatcher.cleanup_blocking_warm_backends
    previous_unload = dispatcher.unload

    async def completed(job, history):
        receipt = dispatcher.control(PREFIX + job["prompt_id"])
        if receipt and receipt["settled"]:
            return
        await previous_completed(job, history)

    async def cleanup():
        if dispatcher.enabled:
            async with fleet.assignment_lock:
                await residue.cleanup()
        await previous_cleanup()

    async def blocking(*args, **kwargs):
        if not await residue.cleanup():
            return False
        return await previous_blocking(*args, **kwargs)

    async def unload(backend, *, reason):
        records = [receipt for receipt in residue.records()
                   if receipt["backend_id"] == backend["id"] and receipt["state"] not in RESOLVED]
        if records:
            return all(receipt["settled"] for receipt in records) and await residue.unload(backend, records)
        return await previous_unload(backend, reason=reason)

    dispatcher.completed = completed
    dispatcher.idle_cleanup = cleanup
    dispatcher.cleanup_blocking_warm_backends = blocking
    dispatcher.unload = unload
    lifecycle = getattr(fleet, "backend_lifecycle", None)
    if lifecycle:
        previous_dispatch = lifecycle.before_dispatch

        async def before_dispatch(waiting):
            if not await residue.cleanup():
                dispatcher.waiting(waiting, "residue_cleanup_requires_reconciliation")
                return False
            return await previous_dispatch(waiting)

        lifecycle.before_dispatch = before_dispatch
    return residue
