"""Durable, fail-closed Edge model switching. Never submits a Comfy prompt.

The local fence pauses legacy Studio/Comfy after an idle check. Qwen is
checked idle before normal stop; direct new arrivals have a documented race.
No Router-wide admission or forced termination is introduced here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time

GIB = 1024**3
TERMINAL = {"completed", "succeeded", "failed", "error", "cancelled", "canceled", "rejected", "expired"}
MODEL_ID = "RadixArk/Qwen3.8-Flash-Next-NVFP4"


class EdgeModelLifecycle:
    """One durable execution lease shared by cooperating Fleet processes.

    ops async contract:
      snapshot(): hashes, qwen_active(bool), qwen_idle(bool), legacy_idle(bool),
                  mem_available_bytes, h3_stopped(bool)
      fence(execution_id): pause legacy producers after idle check; bool confirms stop
      unfence(execution_id): idempotently release only this owner's fence
      stop_qwen(), start_qwen(), stop_h3(): idempotent, allowlisted controls
      qwen_ready(): ready, model_id, max_model_len, mtp_tokens
    snapshot hashes must cover unit definition, launcher, all sourced config and
    EnvironmentFile references. Adapters must not log raw environment/secrets.
    """

    def __init__(self, state_path, ops, expected_hashes, min_available_bytes=80 * GIB,
                 clock=time.time, idle_seconds=5, memory_wait_seconds=300):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ops = ops
        self.expected_hashes = dict(expected_hashes)
        if not self.expected_hashes:
            raise ValueError("pinned_hashes_required")
        self.min_available_bytes = min_available_bytes
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.memory_wait_seconds = memory_wait_seconds
        self.lock = asyncio.Lock()

    @contextmanager
    def _lease(self):
        # Lock acquisition is non-blocking; another process must never block the
        # event loop while waiting for a process holding the lock across await.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {"state": "idle"}
        value = json.loads(self.path.read_text())
        if value.get("state") not in {"idle", "draining", "stopping_qwen", "waiting_memory", "ready", "restoring_h3", "restoring_qwen", "unfencing", "blocked"}:
            raise ValueError("invalid_durable_lifecycle_state")
        return value

    def _save(self, record, **updates):
        if "state" in updates and updates["state"] != record.get("state"):
            updates["transition_started_at"] = self.clock()
        record.update(updates, updated_at=self.clock())
        temporary = self.path.with_suffix(self.path.suffix + ".new")
        with temporary.open("w") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _verify(self, snapshot, state):
        expected = state.get("original_hashes", self.expected_hashes)
        if snapshot.get("hashes") != expected or expected != self.expected_hashes:
            raise RuntimeError("original_model_configuration_changed")
        for key in ("qwen_active", "qwen_idle", "legacy_idle", "h3_stopped"):
            if type(snapshot.get(key)) is not bool:
                raise RuntimeError("incomplete_lifecycle_snapshot:" + key)

    def public(self):
        try:
            state = self._read()
            return {"state": state["state"], "execution_id": state.get("execution_id"),
                    "queueable": state["state"] == "idle", "ready": state["state"] == "ready",
                    "reason": state.get("reason"), "updated_at": state.get("updated_at"),
                    "phase": state["state"], "transition_started_at": state.get("transition_started_at")}
        except Exception:
            return {"state": "blocked", "queueable": False, "ready": False,
                    "reason": "durable_state_unreadable"}

    async def before_dispatch(self, execution_id):
        if not isinstance(execution_id, str) or not execution_id or len(execution_id) > 256:
            raise ValueError("invalid_execution_id")
        async with self.lock:
            try:
                with self._lease():
                    state = self._read()
                    if state["state"] != "idle" and state.get("execution_id") != execution_id:
                        return False
                    if state["state"] in {"restoring_h3", "restoring_qwen", "unfencing", "blocked"}:
                        return False
                    try:
                        snapshot = await self.ops.snapshot()
                        self._verify(snapshot, state)
                        if state["state"] == "idle":
                            if not snapshot["h3_stopped"]:
                                return False
                            self._save(state, state="draining", execution_id=execution_id,
                                       original_qwen_active=snapshot["qwen_active"],
                                       original_hashes=snapshot["hashes"], original_unit_states=snapshot.get("unit_states", {}), idle_since=None, reason="waiting_for_idle")
                        if hasattr(self.ops, "bind_original"):
                            self.ops.bind_original(state.get("original_unit_states", {}))
                        if state["state"] == "ready":
                            # Own H3 may already be running. No new submission here.
                            return not snapshot["qwen_active"] and await self.ops.fence(execution_id)
                        if state["state"] == "draining":
                            # Persist lease BEFORE pausing legacy producers.
                            if not await self.ops.fence(execution_id):
                                self._save(state, reason="qwen_admission_fence_unavailable")
                                return False
                            if not snapshot["legacy_idle"] or (snapshot["qwen_active"] and not snapshot["qwen_idle"]):
                                self._save(state, idle_since=None, reason="waiting_existing_requests")
                                return False
                            if state.get("idle_since") is None:
                                self._save(state, idle_since=self.clock())
                            if self.clock() - state["idle_since"] < self.idle_seconds:
                                return False
                            # Final idle check after pausing the legacy producers.
                            snapshot = await self.ops.snapshot()
                            self._verify(snapshot, state)
                            if not snapshot["legacy_idle"] or not snapshot["h3_stopped"] or (snapshot["qwen_active"] and not snapshot["qwen_idle"]):
                                self._save(state, idle_since=None, reason="idle_changed")
                                return False
                            self._save(state, state="stopping_qwen", reason="unloading_qwen")
                        if state["state"] == "stopping_qwen":
                            # Re-read after restart: if still active, do not stop an
                            # in-flight request even when a previous stop was uncertain.
                            snapshot = await self.ops.snapshot()
                            self._verify(snapshot, state)
                            if not await self.ops.fence(execution_id):
                                raise RuntimeError("qwen_admission_fence_lost")
                            if snapshot["qwen_active"]:
                                if not snapshot["qwen_idle"] or not snapshot["legacy_idle"]:
                                    self._save(state, state="draining", idle_since=None, reason="waiting_existing_requests")
                                    return False
                                await self.ops.stop_qwen()
                            self._save(state, state="waiting_memory", memory_wait_since=self.clock())
                        if state["state"] == "waiting_memory":
                            snapshot = await self.ops.snapshot()
                            self._verify(snapshot, state)
                            if snapshot["qwen_active"] or not snapshot["h3_stopped"]:
                                raise RuntimeError("model_unload_unconfirmed")
                            if snapshot["mem_available_bytes"] < self.min_available_bytes:
                                if self.clock() - state["memory_wait_since"] > self.memory_wait_seconds:
                                    self._save(state, state="restoring_h3", reason="memory_wait_timeout")
                                else:
                                    self._save(state, reason="waiting_80GiB_available")
                                return False
                            self._save(state, state="ready", reason=None)
                            return True
                        return False
                    except asyncio.CancelledError:
                        # Durable intent survives cancellation; reconciliation restores.
                        self._save(state, state="restoring_h3", reason="lifecycle_call_cancelled")
                        raise
                    except Exception as exc:
                        # Recovery still verifies pinned definitions before controls.
                        self._save(state, state="restoring_h3", reason=type(exc).__name__)
                        return False
            except BlockingIOError:
                return False

    async def reconcile(self, rows):
        """rows must be an authoritative complete execution list, including terminals.

        Missing owner is treated as abandoned and restored. Caller must run this
        on startup BEFORE allowing dispatch, and periodically even without jobs.
        """
        async with self.lock:
            try:
                with self._lease():
                    state = self._read()
                    if state["state"] == "idle":
                        return
                    owner = next((r for r in rows if r.get("execution_id") == state.get("execution_id")), None)
                    restoring = state["state"] in {"restoring_h3", "restoring_qwen", "unfencing", "blocked"}
                    if not restoring and owner is not None and owner.get("status") not in TERMINAL:
                        return
                    try:
                        if not restoring:
                            self._save(state, state="restoring_h3", reason="execution_terminal_or_missing")
                        if hasattr(self.ops, "bind_original"):
                            self.ops.bind_original(state.get("original_unit_states", {}))
                        snapshot = await self.ops.snapshot()
                        self._verify(snapshot, state)
                        if state["state"] in {"restoring_h3", "blocked"}:
                            await self.ops.stop_h3()
                            snapshot = await self.ops.snapshot()
                            self._verify(snapshot, state)
                            if not snapshot["h3_stopped"]:
                                raise RuntimeError("h3_unload_unconfirmed")
                            self._save(state, state="restoring_qwen")
                        if state["state"] == "restoring_qwen":
                            # Recheck stop before EVERY restoration attempt.
                            if not snapshot["h3_stopped"]:
                                raise RuntimeError("h3_unload_unconfirmed")
                            if state.get("original_qwen_active"):
                                if not snapshot["qwen_active"]:
                                    await self.ops.start_qwen()
                                ready = await self.ops.qwen_ready()
                                if not ready.get("ready"):
                                    self._save(state, reason="qwen_loading_original_model")
                                    return
                                if (ready.get("model_id"), ready.get("max_model_len"), ready.get("mtp_tokens")) != (MODEL_ID, 500000, 3):
                                    raise RuntimeError("restored_qwen_identity_mismatch")
                            self._save(state, state="unfencing")
                        if state["state"] == "unfencing":
                            # Crash may occur after readiness was persisted. Recheck
                            # before reopening admission, never trust stale health.
                            if state.get("original_qwen_active"):
                                ready = await self.ops.qwen_ready()
                                if not ready.get("ready"):
                                    self._save(state, state="restoring_qwen", reason="qwen_readiness_lost")
                                    return
                                if (ready.get("model_id"), ready.get("max_model_len"), ready.get("mtp_tokens")) != (MODEL_ID, 500000, 3):
                                    raise RuntimeError("restored_qwen_identity_mismatch")
                            released = await self.ops.unfence(state["execution_id"])
                            if released is False:
                                self._save(state, reason="legacy_services_restoring")
                                return
                            self._save(state, state="idle", execution_id=None, reason=None,
                                       last_restored_execution_id=state["execution_id"])
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._save(state, state="blocked", reason=str(exc)[:160])
            except BlockingIOError:
                return

    async def request_restore(self, reason):
        """Persist startup failure/cancellation recovery without resubmitting work."""
        async with self.lock:
            with self._lease():
                state = self._read()
                if state['state'] != 'idle':
                    self._save(state, state='restoring_h3', reason=reason)


class EdgeSystemOps:
    """Local Edge-only adapter. Callbacks supply Fleet-owned H3 and routing fence.

    No credential values are returned or persisted. Pin unit definitions and every
    referenced config/script file in manifest. Caller is the original admin user.
    Default fence controls only legacy local Studio/Comfy; direct Qwen HTTP
    callers have a small idle-check/normal-stop race.
    """
    QWEN_UNIT = "qwen38-flash-next-vllm.service"
    LEGACY_UNITS = ("comfyui-edge.service", "h3-video-studio.service")
    MODEL_PATH = "/home/admin/model-sources/RadixArk-Qwen3.8-Flash-Next-NVFP4"
    IMAGE_ID = "sha256:b5ca7f18ea7670dab3bcaf2a0b75376abd4531ca124d12be2746e37275608fd4"

    def __init__(self, manifest, *, stop_h3, h3_stopped, fence=None, unfence=None,
                 qwen_url="http://100.101.54.115:18300", command=None, http_client=None):
        import httpx
        self.manifest = manifest
        self.fence = fence or self._fence
        self.unfence = unfence or self._unfence
        self.original_states = {}
        self.stop_h3 = stop_h3
        self.h3_stopped = h3_stopped
        self.qwen_url = qwen_url.rstrip("/")
        self.command = command or self._command
        self.client = http_client or httpx.AsyncClient(timeout=5, trust_env=False)
        self.owns_client = http_client is None

    def bind_original(self, states):
        self.original_states = states

    async def _fence(self, execution_id):
        # Original active states were durably saved by the controller first.
        if set(self.original_states) != {self.QWEN_UNIT, *self.LEGACY_UNITS}:
            return False
        states = {unit: await self._state(unit) for unit in self.LEGACY_UNITS}
        if not await self._legacy_idle(states):
            return False
        # Stop the legacy producer first, then recheck its upstream queue before
        # stopping Comfy. Existing nonempty queues are never interrupted.
        studio, comfy = self.LEGACY_UNITS[1], self.LEGACY_UNITS[0]
        if states[studio]["ActiveState"] == "active":
            await self.command("systemctl", "--user", "stop", studio)
        states = {unit: await self._state(unit) for unit in self.LEGACY_UNITS}
        if not await self._legacy_idle(states):
            return False
        if states[comfy]["ActiveState"] == "active":
            await self.command("systemctl", "--user", "stop", comfy)
        final_states = [await self._state(unit) for unit in self.LEGACY_UNITS]
        return all(value["ActiveState"] == "inactive" for value in final_states)

    async def _unfence(self, execution_id):
        for unit in self.LEGACY_UNITS:
            if self.original_states.get(unit, {}).get("ActiveState") == "active":
                state = await self._state(unit)
                if state["ActiveState"] == "inactive":
                    await self.command("systemctl", "--user", "start", unit)
                if (await self._state(unit))["ActiveState"] != "active":
                    return False
                url = "http://127.0.0.1:8188/system_stats" if unit == self.LEGACY_UNITS[0] else "http://127.0.0.1:8789/api/health"
                try:
                    await self._get(url)
                except Exception:
                    return False
        return True

    @staticmethod
    async def _command(*args):
        import subprocess
        def call():
            completed = subprocess.run(args, capture_output=True, text=True, timeout=240)
            if completed.returncode:
                # Avoid propagating arbitrary command output with secrets.
                raise RuntimeError("edge_lifecycle_command_failed:" + args[0])
            return completed.stdout
        return await asyncio.to_thread(call)

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def _get(self, url, text=False):
        response = await self.client.get(url)
        response.raise_for_status()
        return response.text if text else response.json()

    async def _state(self, unit):
        raw = await self.command("systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState")
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values.get("ActiveState") not in {"active", "inactive", "activating", "deactivating", "failed"}:
            raise RuntimeError("unknown_original_unit_state")
        return values

    async def _hashes(self):
        import hashlib
        hashes = {}
        units = self.manifest["units"]
        if set(units) != {self.QWEN_UNIT, *self.LEGACY_UNITS}:
            raise RuntimeError("unit_manifest_incomplete")
        for unit in units:
            definition = await self.command("systemctl", "--user", "cat", unit)
            hashes["unit:" + unit] = hashlib.sha256(definition.encode()).hexdigest()
        for filename in self.manifest["files"]:
            # Only digest leaves this method; raw credentials are never returned.
            hashes["file:" + filename] = await asyncio.to_thread(
                lambda name=filename: hashlib.sha256(Path(name).read_bytes()).hexdigest())
        return hashes

    async def _qwen_idle(self):
        import math
        import re
        try:
            metrics = await self._get(self.qwen_url + "/metrics", text=True)
        except Exception:
            return False
        groups = {}
        for metric in ("running", "waiting"):
            values = re.findall(r"^vllm:num_requests_" + metric + r"(?:\{[^\n]*\})?\s+([^\s]+)", metrics, re.M)
            try:
                groups[metric] = bool(values) and all(math.isfinite(float(v)) and float(v) == 0 for v in values)
            except ValueError:
                return False
        return all(groups.values())

    async def _legacy_idle(self, states):
        try:
            if states[self.LEGACY_UNITS[0]]["ActiveState"] == "active":
                queue = await self._get("http://127.0.0.1:8188/queue")
                if queue.get("queue_running") != [] or queue.get("queue_pending") != []:
                    return False
            elif states[self.LEGACY_UNITS[0]]["ActiveState"] != "inactive":
                return False
            if states[self.LEGACY_UNITS[1]]["ActiveState"] == "active":
                projects = await self._get("http://127.0.0.1:8789/api/projects")
                allowed = {"approved", "pending", "failed", "awaiting_approval", "cancelled", "completed", "skipped"}
                for project in projects["projects"]:
                    if any(stage.get("status") not in allowed for stage in project.get("stages", {}).values()):
                        return False
                schedules = await self._get("http://127.0.0.1:8789/api/768-queue/schedules")
                if schedules.get("active_batch_id") or any(row.get("status") not in {"completed", "cancelled", "failed", "paused"} for row in schedules["schedules"]):
                    return False
            elif states[self.LEGACY_UNITS[1]]["ActiveState"] != "inactive":
                return False
            return True
        except Exception:
            return False

    async def snapshot(self):
        hashes = await self._hashes()
        states = {unit: await self._state(unit) for unit in (self.QWEN_UNIT, *self.LEGACY_UNITS)}
        qwen = states[self.QWEN_UNIT]["ActiveState"]
        # A transitioning/failed unit is not confirmed inactive; no H3 admission.
        active = qwen != "inactive"
        memory = dict((line.split(":", 1)[0], int(line.split()[1]) * 1024)
                      for line in Path("/proc/meminfo").read_text().splitlines())
        return {"hashes": hashes, "qwen_active": active,
                "qwen_idle": await self._qwen_idle() if qwen == "active" else not active,
                "legacy_idle": await self._legacy_idle(states),
                "mem_available_bytes": memory["MemAvailable"],
                "h3_stopped": await self.h3_stopped(), "unit_states": states}

    async def stop_qwen(self):
        await self.command("systemctl", "--user", "stop", self.QWEN_UNIT)

    async def allowed_gpu_pids(self):
        """Actual owned processes only; third-party GPU users never justify switching."""
        pids = set()
        comfy = await self._state(self.LEGACY_UNITS[0])
        if comfy['ActiveState'] == 'active':
            raw = await self.command('systemctl', '--user', 'show', self.LEGACY_UNITS[0], '-p', 'MainPID', '--value')
            pid = raw.strip()
            if not pid.isdigit() or int(pid) <= 0:
                raise RuntimeError('legacy_comfy_pid_unverified')
            membership = (Path('/proc') / pid / 'cgroup').read_text()
            if not membership.strip().endswith('/' + self.LEGACY_UNITS[0]):
                raise RuntimeError('legacy_comfy_cgroup_unverified')
            pids.add(pid)
        qwen = await self._state(self.QWEN_UNIT)
        if qwen['ActiveState'] == 'active':
            if not (await self.qwen_ready()).get('ready'):
                raise RuntimeError('original_qwen_model_unverified')
            raw = await self.command('sudo', '-n', 'docker', 'top', 'qwen38-flash-next', '-eo', 'pid')
            values = raw.splitlines()[1:]
            if not values or any(not pid.strip().isdigit() for pid in values):
                raise RuntimeError('qwen_container_pid_inventory_unverified')
            pids.update(pid.strip() for pid in values)
        return pids

    async def start_qwen(self):
        await self.command("systemctl", "--user", "start", self.QWEN_UNIT)

    async def qwen_ready(self):
        try:
            await self._get(self.qwen_url + "/health", text=True)
            models = (await self._get(self.qwen_url + "/v1/models"))["data"]
            await self._get(self.qwen_url + "/metrics", text=True)
            raw = await self.command("sudo", "-n", "docker", "inspect", "qwen38-flash-next")
            container = json.loads(raw)[0]
            if not container["State"]["Running"] or container["Image"] != self.IMAGE_ID:
                return {"ready": False}
            args = container["Config"]["Cmd"]
            def value(flag):
                positions = [i for i, arg in enumerate(args) if arg == flag]
                if len(positions) != 1:
                    raise ValueError("missing_or_duplicate_argument")
                return args[positions[0] + 1]
            speculative = json.loads(value("--speculative-config"))
            mount_ok = any(m.get("Source") == self.MODEL_PATH and m.get("Destination") == "/model" and not m.get("RW") for m in container["Mounts"])
            model_ok = (len(models) == 1 and models[0]["id"] == MODEL_ID and
                        models[0].get("max_model_len") == 500000 and
                        value("--served-model-name") == MODEL_ID and
                        value("--max-model-len") == "500000" and
                        value("--kv-cache-dtype") == "auto" and
                        speculative == {"method": "mtp", "num_speculative_tokens": 3, "max_model_len": 500000})
            return {"ready": bool(mount_ok and model_ok), "model_id": models[0]["id"],
                    "max_model_len": models[0].get("max_model_len"),
                    "mtp_tokens": speculative.get("num_speculative_tokens")}
        except Exception:
            return {"ready": False}
