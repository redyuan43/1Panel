from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import httpx
from fastapi import HTTPException, Request


BUSY = {"reserved", "running", "submitted", "reconciling", "cancelling"}
POLICY_KEY = "h3_backend_lifecycle"
GIB = 1024**3


def command(*arguments):
    if arguments[:2] in {("systemctl", "start"), ("systemctl", "stop")}:
        if len(arguments) != 3 or not re.fullmatch(r"h3-single-[a-z0-9-]+\.service", arguments[2]):
            raise ValueError("service_control_not_allowlisted")
        arguments = ("sudo", "-n", "/usr/bin/systemctl", arguments[1], arguments[2])
    return subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=True).stdout


class BackendLifecycle:
    def __init__(self, fleet, registry, runner=command):
        self.fleet = fleet
        self.dispatcher = fleet.recipes
        self.registry = registry
        self.runner = runner
        self.state = {}
        self.starting = {}
        self.edge = None
        self.edge_reconcile_task = None
        for backend_id, spec in registry.items():
            if backend_id not in self.dispatcher.backends or not re.fullmatch(r"h3-single-[a-z0-9-]+\.service", spec.get("unit", "")):
                raise ValueError("lifecycle unit is outside the immutable H3 backend allowlist")
            if not re.fullmatch(r"[a-f0-9]{64}", spec.get("unit_sha256", "")):
                raise ValueError("lifecycle must pin the service definition")
        edge = self.dispatcher.policy.get('edge_model_lifecycle')
        if edge:
            from .edge_model_lifecycle import EdgeModelLifecycle, EdgeSystemOps
            if len(registry) != 1 or not all(b.get('unified_memory') for b in self.dispatcher.backends.values()):
                raise ValueError('edge_lifecycle_requires_one_unified_backend')
            manifest = json.loads(Path(edge['manifest_path']).read_text())
            ops = EdgeSystemOps(manifest, stop_h3=self.stop_edge_worker, h3_stopped=self.edge_worker_stopped)
            self.edge = EdgeModelLifecycle(edge['state_path'], ops, edge['expected_hashes'])

    async def edge_worker_stopped(self):
        return all([await self.inactive_lane(lane, allow_failed=True) for lane in self.fleet.lanes if lane.enabled])

    def spawned_identity(self, backend, pid):
        """Prove this managed process even before its HTTP socket opens."""
        process = Path('/proc') / str(pid)
        command = hashlib.sha256((process / 'cmdline').read_bytes()).hexdigest()
        env = dict(item.split(b'=', 1) for item in (process / 'environ').read_bytes().split(b'\0') if b'=' in item)
        membership = (process / 'cgroup').read_text().strip()
        if (command != backend['cmdline_sha256'] or env.get(b'CUDA_VISIBLE_DEVICES', b'').decode() != backend['gpu_uuid']
                or membership != '0::/' + str(Path(backend['cgroup_path']).relative_to('/sys/fs/cgroup'))):
            raise RuntimeError('spawned_backend_identity_unverified')
        return {'pid': pid, 'start_ticks': (process / 'stat').read_text().rsplit(')', 1)[1].split()[19],
                'cmdline_sha256': command, 'gpu_uuid': backend['gpu_uuid'], 'cgroup_path': backend['cgroup_path']}

    async def preparation_guard(self):
        reasons = []
        sample = self.dispatcher.snapshot or {}
        now = time.time()
        if not self.edge:
            return None
        if not self.policy()['enabled'] or not self.edge.public()['queueable']:
            reasons.append('edge_lifecycle_not_idle')
        if self.fleet.draining or self.dispatcher.control('recipe_hard_stop'):
            reasons.append('fleet_draining_or_hard_stop')
        if self.fleet.store.active() or self.fleet.store.validation_lease() or self.fleet.store.studio_batch():
            reasons.append('execution_or_control_lease_present')
        if not sample.get('ok') or not 0 <= now - sample.get('timestamp', 0) <= 10:
            reasons.append('resource_telemetry_unavailable')
        # Stopping the pinned idle Qwen releases memory; it is not H3 admission.
        # Actual worker startup still enforces the unchanged continuous window.
        preparation_waits = {'psi_not_stable', 'awaiting_continuous_stable_window'}
        stability = set(sample.get('progressive', {}).get('reasons', ['stability_not_observed']))
        if stability - preparation_waits:
            reasons.append('resource_stability_not_ready')
        if sample.get('kernel_alerts'):
            reasons.append('kernel_oom_or_xid')
        if sample.get('root_available_bytes', 0) < 25 * GIB or sample.get('offload_available_bytes', 0) < 40 * GIB:
            reasons.append('disk_headroom')
        if max(sample.get('swap_used_bytes', 8 * GIB), sample.get('cgroup_swap_bytes', 8 * GIB)) >= 8 * GIB:
            reasons.append('swap_hard_limit')
        profiles = []
        for backend in self.dispatcher.backends.values():
            if self.dispatcher.control('lifecycle_quarantine:' + backend['id']):
                reasons.append('backend_quarantined')
            profiles.extend(p for p in backend['recipes'] if self.dispatcher.qualifications(p, backend))
        try:
            if not await self.edge_worker_stopped():
                reasons.append('managed_worker_not_stopped')
            snapshot = await self.edge.ops.snapshot()
            self.edge._verify(snapshot, {})
            if not snapshot['legacy_idle'] or (snapshot['qwen_active'] and not snapshot['qwen_idle']):
                reasons.append('original_service_requests_active')
            allowed_pids = await self.edge.ops.allowed_gpu_pids()
            if 'gpu_process_identities' not in sample or 'gpu_names' not in sample:
                reasons.append('gpu_process_inventory_unavailable')
            elif any(pid not in allowed_pids for pid, gpu in sample['gpu_process_identities']):
                reasons.append('third_party_gpu_process_present')
        except Exception:
            reasons.append('original_service_or_worker_identity_unverified')
        return {'eligible': not reasons and bool(profiles), 'reasons': sorted(set(reasons)),
                'observed_at': now, 'profile_ids': sorted(set(profiles)), 'requires_model_switch': True,
                'stability_reasons': sample.get('progressive', {}).get('reasons', []),
                'stability_seconds': sample.get('progressive', {}).get('stable_seconds')}

    async def stop_edge_worker(self):
        if any(row['status'] in BUSY for row in self.fleet.store.active()):
            raise RuntimeError('edge_owned_execution_not_terminal')
        for backend_id in self.registry:
            backend = self.dispatcher.backends[backend_id]
            if await self.inactive_lane(self.fleet.lanes_by_id[backend['lane_id']], allow_failed=True):
                continue
            service = await asyncio.to_thread(self.service, backend_id)
            owned = self.dispatcher.control('lifecycle_identity:' + backend_id, {})
            spawned = self.dispatcher.control('lifecycle_spawned:' + backend_id, {})
            if service.get('ActiveState') != 'active' or int(service['MainPID']) not in {backend['pid'], owned.get('pid'), spawned.get('pid')}:
                raise RuntimeError('cannot_stop_unowned_edge_worker')
            from .recipe_dispatch import backend_identity
            proof = owned if owned.get('pid') == int(service['MainPID']) else spawned
            if proof:
                current = await asyncio.to_thread(self.spawned_identity, backend, proof['pid'])
                if current['start_ticks'] != proof['start_ticks']:
                    raise RuntimeError('spawned_backend_pid_reused')
            try:
                response = await self.fleet.client.get(backend['url'] + '/queue', timeout=5)
                response.raise_for_status()
                if response.json() != {'queue_running': [], 'queue_pending': []}:
                    raise RuntimeError('edge_worker_queue_not_empty')
            except httpx.ConnectError:
                # A verified process that never opened HTTP could not receive a prompt.
                if not spawned or owned:
                    raise
            owner = self.fleet.store.get_by_execution(self.edge.public().get('execution_id'))
            if owner and owner.get('upstream_prompt_id'):
                directory = self.edge.path.parent.parent / 'evidence' / owner['prompt_id']
                directory.mkdir(parents=True, exist_ok=True)
                receipt = directory / 'terminal-runtime-receipt.json'
                if not receipt.exists():
                    try:
                        history = await self.fleet.client.get(backend['url'] + '/history/' + owner['upstream_prompt_id'], timeout=10)
                        history.raise_for_status()
                        identity = await asyncio.to_thread(backend_identity, backend)
                        value = {'job': owner, 'history': history.json(), 'runtime_identity': identity,
                                 'observed_at': time.time(), 'worker_still_owned': True}
                        temporary = receipt.with_suffix('.new')
                        temporary.write_text(json.dumps(value, indent=2))
                        temporary.replace(receipt)
                        receipt.chmod(0o400)
                    except Exception as error:
                        # Evidence failure blocks promotion, never Qwen restoration.
                        (directory / 'evidence-error.json').write_text(json.dumps({'error': type(error).__name__, 'qualified': False}))
            await asyncio.to_thread(self.runner, 'systemctl', 'stop', self.registry[backend_id]['unit'])
            if not await self.inactive_lane(self.fleet.lanes_by_id[backend['lane_id']], allow_failed=True):
                raise RuntimeError('edge_worker_stop_unconfirmed')
            self.save_control('warm:' + backend_id, None)

    def policy(self):
        return self.dispatcher.control(POLICY_KEY, {"revision": "initial", "enabled": True})

    def save_control(self, name, value):
        with self.fleet.store._connect() as database:
            database.execute("INSERT OR REPLACE INTO controls(name,value) VALUES (?,?)", (name, json.dumps(value)))

    def service(self, backend_id):
        spec = self.registry[backend_id]
        definition = self.runner("systemctl", "cat", spec["unit"])
        if hashlib.sha256(definition.encode()).hexdigest() != spec["unit_sha256"]:
            raise RuntimeError("backend_service_definition_changed")
        raw = self.runner("systemctl", "show", spec["unit"], "-p", "MainPID", "-p", "ActiveState", "-p", "SubState", "-p", "ControlGroup")
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    async def inactive_lane(self, lane, *, allow_failed=False):
        """Only a pinned managed service with no listener proves a cold empty lane."""
        from urllib.parse import urlsplit
        candidates = [b for b in self.dispatcher.backends.values()
                      if b['lane_id'] == lane.id and b['id'] in self.registry]
        if len(candidates) != 1:
            return False
        backend = candidates[0]
        service = await asyncio.to_thread(self.service, backend['id'])
        failed = service.get('ActiveState') == 'failed'
        if service.get('MainPID') != '0':
            return False
        if failed:
            # Restoration may accept a crashed, empty owned unit without resetting
            # its failure state. This does not make it eligible for cold admission.
            if not allow_failed or not getattr(self, 'edge', None):
                return False
            group = Path(backend.get('cgroup_path', ''))
            cgroup_root = Path('/sys/fs/cgroup')
            unit = self.registry[backend['id']]['unit']
            if cgroup_root not in group.parents or group.name != unit or '..' in group.parts:
                return False
            expected = '/' + str(group.relative_to(cgroup_root))
            if service.get('ControlGroup') not in ('', expected):
                return False
            if group.exists():
                events = dict(line.split() for line in (group / 'cgroup.events').read_text().splitlines())
                # populated covers descendant cgroups as well as this unit root.
                if events.get('populated') != '0' or (group / 'cgroup.procs').read_text().strip():
                    return False
        elif service.get('ActiveState') != 'inactive':
            return False
        port = urlsplit(backend['url']).port
        for name in ('tcp', 'tcp6'):
            for line in (Path('/proc/net') / name).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) == port:
                    raise RuntimeError('inactive_backend_port_owned_by_unknown_process')
        if failed:
            current = await asyncio.to_thread(self.service, backend['id'])
            if (current.get('MainPID') != '0' or current.get('ActiveState') != 'failed'
                    or current.get('ControlGroup') != service.get('ControlGroup')):
                return False
        return True

    async def set_enabled(self, value):
        if not isinstance(value, dict) or set(value) != {"operation_id", "expected_revision", "enabled"} or type(value["enabled"]) is not bool:
            raise HTTPException(400, "invalid lifecycle policy")
        if not isinstance(value["operation_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", value["operation_id"]):
            raise HTTPException(400, "invalid lifecycle operation")
        async with self.fleet.assignment_lock:
            key = "lifecycle_operation:" + value["operation_id"]
            previous = self.dispatcher.control(key)
            request_digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise HTTPException(409, "lifecycle operation conflict")
                return previous["result"]
            if self.policy()["revision"] != value["expected_revision"]:
                raise HTTPException(409, "lifecycle policy changed")
            result = {"revision": request_digest, "enabled": value["enabled"], "updated_at": time.time()}
            with self.fleet.store._connect() as database:
                database.execute("BEGIN IMMEDIATE")
                database.execute("INSERT OR REPLACE INTO controls(name,value) VALUES (?,?)", (POLICY_KEY, json.dumps(result)))
                database.execute("INSERT INTO controls(name,value) VALUES (?,?)", (key, json.dumps({"request_digest": request_digest, "result": result})))
            self.dispatcher.audit("lifecycle_policy", result)
            return result

    async def adopt(self, backend_id, service):
        from .recipe_dispatch import backend_identity, verify_backend
        backend = self.dispatcher.backends[backend_id]
        process_id = int(service["MainPID"])
        if process_id <= 0 or service["ActiveState"] != "active" or service["SubState"] != "running":
            raise RuntimeError("backend_not_running")
        updated = dict(backend, pid=process_id)
        fields = (Path("/proc") / str(process_id) / "stat").read_text().rsplit(")", 1)[1].split()
        updated["start_ticks"] = fields[19]
        if service["ControlGroup"] != "/" + str(Path(backend["cgroup_path"]).relative_to("/sys/fs/cgroup")):
            raise RuntimeError("backend_service_cgroup_changed")
        await asyncio.to_thread(verify_backend, updated, self.dispatcher.catalog)
        identity = await asyncio.to_thread(backend_identity, updated)
        backend.update(pid=identity["pid"], start_ticks=identity["start_ticks"])
        self.save_control("lifecycle_identity:" + backend_id, identity)
        self.dispatcher.audit("lifecycle_identity_verified", {"backend_id": backend_id, **identity})

    async def before_dispatch(self, waiting):
        if self.edge:
            owner = self.edge.public().get('execution_id')
            if owner:
                waiting = [job for job in waiting if job.get('execution_id') == owner]
        if not self.policy()["enabled"]:
            self.dispatcher.waiting(waiting, "backend_disabled")
            return False
        if self.starting:
            self.dispatcher.waiting(waiting, "backend_starting")
            return False
        rows = self.fleet.store.active()
        if any(row["status"] in BUSY for row in rows):
            return True
        lease = self.fleet.store.validation_lease()
        if lease:
            waiting = [job for job in waiting if lease["expires_at"] > time.time()
                       and job["execution_id"].startswith(lease["owner"] + "_")]
        batch = self.fleet.store.studio_batch()
        if batch:
            waiting = [job for job in waiting if job["execution_id"].startswith(batch["owner"] + "_")]
        for job in waiting:
            for backend_id, backend in self.dispatcher.backends.items():
                if backend_id not in self.registry or not self.dispatcher.qualifications(job["recipe_id"], backend):
                    continue
                if self.dispatcher.control("lifecycle_quarantine:" + backend_id):
                    self.dispatcher.waiting([job], "backend_start_failed_requires_operator")
                    continue
                try:
                    if self.edge and not await self.edge.before_dispatch(job['execution_id']):
                        self.dispatcher.waiting([job], 'edge_model_' + self.edge.public()['state'])
                        return False
                    service = await asyncio.to_thread(self.service, backend_id)
                    if service["ActiveState"] == "active":
                        if int(service["MainPID"]) != backend["pid"]:
                            identity = self.dispatcher.control("lifecycle_identity:" + backend_id)
                            if not identity or int(service["MainPID"]) != identity.get("pid"):
                                raise RuntimeError("unowned_backend_process_identity")
                            await self.adopt(backend_id, service)
                        continue
                    if service["ActiveState"] != "inactive":
                        raise RuntimeError("backend_not_cleanly_stopped")
                    prior_start = self.dispatcher.control("lifecycle_start:" + backend_id, {})
                    if prior_start.get("state") == "starting":
                        raise RuntimeError("backend_start_result_unknown_requires_reconciliation")
                    for previous in self.dispatcher.backends.values():
                        if previous["id"] == backend_id or previous["gpu_uuid"] != backend["gpu_uuid"]:
                            continue
                        if self.dispatcher.control("warm:" + previous["id"]):
                            if not await self.dispatcher.unload(previous, reason="cold_start_backend_switch"):
                                self.dispatcher.waiting([job], "previous_backend_unload_unconfirmed")
                                return False
                    sample = await self.dispatcher.sample()
                    if sample.get("kernel_alerts"):
                        self.dispatcher.waiting([job], "kernel_oom_or_xid")
                        return False
                    if any(item["queued_or_running"] for item in await self.fleet.inspect_queues()):
                        self.dispatcher.waiting([job], "existing_upstream_work")
                        return False
                    allocation = None
                    if not backend.get("unified_memory"):
                        allocation = await asyncio.to_thread(self.runner, "nvidia-smi", "--id=" + backend["gpu_uuid"], "--query-gpu=memory.free", "--format=csv,noheader,nounits")
                    try:
                        candidate = self.dispatcher.candidate(job["recipe_id"], backend, job=job)
                    except ValueError as error:
                        self.dispatcher.waiting([job], str(error))
                        return False
                    cold_budget = max(self.dispatcher.policy.get("static_budget_gib", 18) * GIB,
                                      self.registry[backend_id].get("cold_start_budget_bytes", 18 * GIB))
                    if backend.get("unified_memory"):
                        # Explicit 64 GiB unified reservation covers the full cold model.
                        # It is not added a second time, and no VRAM value is invented.
                        cold_budget = 0
                    candidate = {**candidate, "candidate_budget_bytes": candidate["candidate_budget_bytes"] + cold_budget,
                                 "budget_bytes": candidate["candidate_budget_bytes"] + cold_budget,
                                 "cold_start_budget_bytes": cold_budget}
                    decision = self.dispatcher.memory.decide(sample, candidate, [], limits=self.fleet.policy.data["resources"],
                                                             now=time.time(), vram_free_bytes=None if allocation is None else int(allocation.strip()) * 1024**2)
                    decision = {**candidate, **decision}
                    if decision["admission"] != "allow":
                        self.dispatcher.waiting([job], "backend_start_resource_gate")
                        return False
                    if not self.policy()["enabled"] or self.fleet.draining or self.fleet.store.release_gate_reason(job.get("execution_id")):
                        return False
                    receipt = {"backend_id": backend_id, "execution_id": job["execution_id"], "state": "starting",
                               "at": time.time(), "admission": decision}
                    self.save_control("lifecycle_start:" + backend_id, receipt)
                    self.dispatcher.audit("lifecycle_start_requested", receipt)
                    self.state[backend_id] = "starting"
                    self.dispatcher.waiting([job], "backend_starting")
                    self.starting[backend_id] = asyncio.create_task(self.start_registered(backend_id, receipt))
                    return False
                except Exception as error:
                    reason = str(error) if re.fullmatch(r"[a-z_]+", str(error)) else "backend_lifecycle_failed"
                    self.save_control("lifecycle_quarantine:" + backend_id, {"reason": reason, "at": time.time()})
                    self.dispatcher.audit("lifecycle_failed", {"backend_id": backend_id, "reason": reason})
                    self.dispatcher.waiting([job], reason)
                    if self.edge:
                        await self.edge.request_restore(reason)
                    return False
        return True

    async def start_registered(self, backend_id, receipt):
        backend = self.dispatcher.backends[backend_id]
        try:
            if not self.policy()["enabled"] or self.fleet.draining or self.fleet.store.release_gate_reason(receipt["execution_id"]):
                self.save_control("lifecycle_start:" + backend_id, {**receipt, "state": "not_started", "finished_at": time.time()})
                self.state[backend_id] = "stopped"
                if self.edge:
                    await self.edge.request_restore('backend_start_cancelled')
                return
            await asyncio.to_thread(self.runner, "systemctl", "start", self.registry[backend_id]["unit"])
            for attempt in range(60):
                service = await asyncio.to_thread(self.service, backend_id)
                if service.get("ActiveState") == "failed":
                    raise RuntimeError("backend_start_failed")
                if self.edge and int(service.get('MainPID', '0')) > 0:
                    identity = await asyncio.to_thread(self.spawned_identity, backend, int(service['MainPID']))
                    self.save_control('lifecycle_spawned:' + backend_id, identity)
                try:
                    response = await self.fleet.client.get(backend["url"] + "/system_stats", timeout=2)
                except (OSError, TimeoutError, httpx.HTTPError):
                    response = None
                if response is not None and response.status_code == 200:
                    if self.edge and int(service.get('MainPID', '0')) > 0:
                        from .recipe_dispatch import backend_identity
                        pid = int(service['MainPID'])
                        ticks = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                        identity = await asyncio.to_thread(backend_identity, {**backend, 'pid': pid, 'start_ticks': ticks})
                        self.save_control('lifecycle_spawned:' + backend_id, identity)
                    async with self.fleet.assignment_lock:
                        await self.adopt(backend_id, service)
                        self.state[backend_id] = "online_model_unknown"
                        self.save_control("lifecycle_start:" + backend_id, {**receipt, "state": "started", "finished_at": time.time()})
                    return
                await asyncio.sleep(1)
            raise RuntimeError("backend_start_timeout")
        except asyncio.CancelledError:
            if self.edge:
                await asyncio.shield(self.edge.request_restore('backend_start_cancelled'))
            raise
        except Exception as error:
            reason = str(error) if re.fullmatch(r"[a-z_]+", str(error)) else "backend_lifecycle_failed"
            self.save_control("lifecycle_quarantine:" + backend_id, {"reason": reason, "at": time.time()})
            self.dispatcher.audit("lifecycle_failed", {"backend_id": backend_id, "reason": reason})
            self.state[backend_id] = "start_failed_requires_operator"
            if self.edge:
                await self.edge.request_restore(reason)
        finally:
            self.starting.pop(backend_id, None)

    async def close(self):
        pending = list(self.starting.values())
        if self.edge_reconcile_task is not None:
            pending.append(self.edge_reconcile_task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def drain_stopped(self):
        if self.policy()["enabled"]:
            return
        async with self.fleet.assignment_lock:
            if self.starting or any(row["status"] in BUSY for row in self.fleet.store.active()):
                return
            for backend_id, backend in self.dispatcher.backends.items():
                if backend_id not in self.registry:
                    continue
                try:
                    service = await asyncio.to_thread(self.service, backend_id)
                    if service["ActiveState"] == "inactive":
                        self.state[backend_id] = "stopped"
                        continue
                    if service["ActiveState"] != "active" or int(service["MainPID"]) != backend["pid"]:
                        raise RuntimeError("cannot_stop_unowned_backend")
                    if not await self.dispatcher.unload(backend, reason="administrator_disabled"):
                        self.state[backend_id] = "waiting_for_verified_unload"
                        continue
                    await asyncio.to_thread(self.runner, "systemctl", "stop", self.registry[backend_id]["unit"])
                    stopped = await asyncio.to_thread(self.service, backend_id)
                    if stopped["ActiveState"] != "inactive" or int(stopped["MainPID"]) != 0:
                        raise RuntimeError("backend_stop_unconfirmed")
                    self.state[backend_id] = "stopped"
                    self.dispatcher.audit("lifecycle_stopped", {"backend_id": backend_id})
                except Exception:
                    self.state[backend_id] = "stop_failed_requires_operator"

    def public(self):
        return {"policy": self.policy(), "model_lifecycle": self.edge.public() if self.edge else None,
                "backends": [{"backend_id": identifier, "state": self.state.get(identifier, "unknown"),
                "model_state": "unknown", "startup": self.dispatcher.control("lifecycle_start:" + identifier),
                "quarantined": bool(self.dispatcher.control("lifecycle_quarantine:" + identifier))}
                for identifier in self.registry]}

    async def status(self):
        from .recipe_dispatch import backend_identity
        result = self.public()
        for item in result["backends"]:
            identifier = item["backend_id"]
            if identifier in self.starting:
                item["state"] = "starting"
                continue
            try:
                service = await asyncio.to_thread(self.service, identifier)
                if service["ActiveState"] == "inactive" and int(service["MainPID"]) == 0:
                    item.update(state="stopped", model_state="unloaded")
                elif service["ActiveState"] == "active":
                    backend = self.dispatcher.backends[identifier]
                    if int(service["MainPID"]) != backend["pid"]:
                        raise ValueError("identity unknown")
                    await asyncio.to_thread(backend_identity, backend)
                    response = await self.fleet.client.get(backend["url"] + "/system_stats", timeout=2)
                    response.raise_for_status()
                    if not isinstance(response.json().get("devices"), list):
                        raise ValueError("health unknown")
                    item["state"] = "online_model_unknown"
                else:
                    item["state"] = "unhealthy"
            except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError, httpx.HTTPError):
                item["state"] = "unknown"
        return result


def install(fleet, app, authenticate):
    from .admission import BUSY as RUNTIME_BUSY
    global BUSY
    BUSY = set(RUNTIME_BUSY) | BUSY
    registry = fleet.recipes.policy.get("lifecycle_backends", {})
    if not registry:
        return None
    lifecycle = BackendLifecycle(fleet, registry)
    fleet.backend_lifecycle = lifecycle
    previous_qualification = fleet.recipes.qualifications
    previous_decision = fleet.recipes.decision
    previous_tick = fleet.recipes._tick_locked
    previous_cleanup = fleet.recipes.idle_cleanup
    previous_close = fleet.recipes.close

    def qualified(recipe_id, backend):
        return not fleet.recipes.control("lifecycle_quarantine:" + backend["id"]) and previous_qualification(recipe_id, backend)

    def decision(*args, **kwargs):
        if not lifecycle.policy()["enabled"]:
            return {"admission": "wait", "reasons": ["backend_disabled"]}
        if lifecycle.edge and not lifecycle.edge.public()['ready']:
            return {"admission": "wait", "reasons": ['edge_model_' + lifecycle.edge.public()['state']]}
        if lifecycle.edge:
            job = kwargs.get('job', args[5] if len(args) > 5 else None)
            if not job or job.get('execution_id') != lifecycle.edge.public().get('execution_id'):
                return {'admission': 'wait', 'reasons': ['edge_model_reserved_for_other_execution']}
        return previous_decision(*args, **kwargs)

    async def tick():
        if lifecycle.edge:
            state = lifecycle.edge.public()
            if state['state'] in {'restoring_h3', 'restoring_qwen', 'unfencing', 'blocked'} and state.get('execution_id'):
                owner = fleet.store.get_by_execution(state['execution_id'])
                if owner and owner['status'] == 'queued':
                    fleet.store.update(owner['prompt_id'], status='error',
                        failure_reason='edge_preparation_failed:' + str(state.get('reason')))
            if lifecycle.edge_reconcile_task is None:
                with fleet.store._connect() as database:
                    complete_rows = [dict(zip(('execution_id', 'status'), row)) for row in database.execute('SELECT execution_id,status FROM jobs')]
                lifecycle.edge_reconcile_task = asyncio.create_task(lifecycle.edge.reconcile(complete_rows))
                return
            if not lifecycle.edge_reconcile_task.done():
                return
            lifecycle.edge_reconcile_task.result()
            lifecycle.edge_reconcile_task = None
            if lifecycle.edge.public()['state'] in {'restoring_h3', 'restoring_qwen', 'unfencing', 'blocked'}:
                return
        rows = fleet.store.active()
        waiting = [row for row in rows if row["status"] == "queued" and row.get("recipe_id")]
        eligible = [row for row in waiting if not fleet.store.release_gate_reason(row.get("execution_id"))]
        await fleet.recipes.sample()
        if await fleet.recipes.hard_protection(rows):
            return
        if fleet.recipes.control("recipe_hard_stop") or fleet.draining:
            return
        if eligible and not await fleet.recipes.fence_reason() and not await lifecycle.before_dispatch(eligible):
            return
        await previous_tick()

    async def cleanup():
        await lifecycle.drain_stopped()
        if lifecycle.policy()["enabled"]:
            await previous_cleanup()

    async def close():
        await lifecycle.close()
        await previous_close()

    fleet.recipes.qualifications = qualified
    fleet.recipes.decision = decision
    fleet.recipes._tick_locked = tick
    fleet.recipes.idle_cleanup = cleanup
    fleet.recipes.close = close

    async def status(request: Request):
        authenticate(request)
        return await lifecycle.status()

    async def policy(request: Request):
        authenticate(request)
        return await lifecycle.set_enabled(await request.json())

    app.add_api_route("/api/router/backend-lifecycle", status, methods=["GET"])
    app.add_api_route("/api/router/backend-lifecycle", policy, methods=["POST"])
    return lifecycle
