"""Studio node selection over independent, authoritative local Fleet schedulers."""
from __future__ import annotations

import contextvars
import json
import re
import time
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .fleet import FleetClient, SubmissionUnknown
from .fleet_routes import RouteStore


@dataclass(frozen=True)
class Node:
    id: str
    url: str
    key_file: str
    max_parallel: int = 1
    priority: int = 100
    enabled: bool = False
    legacy_profiles: tuple = ()


def read_nodes(path):
    config = json.loads(Path(path).read_text())
    if config.get("version") != 1:
        raise ValueError("unsupported Fleet node configuration")
    nodes = []
    for value in config["nodes"]:
        node = Node(**{**value, "legacy_profiles": tuple(value.get("legacy_profiles", ()))})
        url = urlsplit(node.url)
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", node.id)
                or url.scheme not in {"http", "https"} or not url.hostname
                or not url.hostname.endswith(".ts.net") or not url.port
                or url.path not in {"", "/"} or url.query or url.fragment or url.username
                or not Path(node.key_file).is_absolute()
                or type(node.max_parallel) is not int or node.max_parallel not in (1, 2)
                or type(node.enabled) is not bool or type(node.priority) is not int
                or any(p not in {"preview", "quality"} for p in node.legacy_profiles)):
            raise ValueError("invalid Fleet node configuration")
        nodes.append(node)
    if (not nodes or len({n.id for n in nodes}) != len(nodes)
            or len({n.url.rstrip('/') for n in nodes}) != len(nodes)
            or sum(n.max_parallel for n in nodes) > 3):
        raise ValueError("duplicate nodes or cluster capacity exceeds three")
    legacy = config.get("legacy_node", "ivan")
    if legacy not in {n.id for n in nodes}:
        raise ValueError("legacy Fleet node must remain addressable")
    return nodes, legacy


class MultiFleetClient(FleetClient):
    is_multifleet = True

    def __init__(self, nodes, legacy_node, database, *, client_factory=FleetClient):
        self.nodes = {n.id: n for n in nodes}
        self.legacy_node = legacy_node
        legacy = self.nodes[legacy_node]
        super().__init__(legacy.url, legacy.key_file)
        self.clients = {n.id: client_factory(n.url, n.key_file) for n in nodes}
        for node in nodes:
            if node.id != legacy_node:
                self.clients[node.id].external_status_path = self.external_status_path.parent / node.id / "live.json"
        self.routes = RouteStore(Path(database))
        self.context = contextvars.ContextVar("h3_fleet_route", default=None)

    def validate_target(self, target):
        if not isinstance(target, str) or target not in {"auto", *self.nodes}:
            raise ValueError("未知执行设备。")
        return target

    def _client(self, route):
        node = self.nodes.get(route["node_id"])
        if node is None or node.url != route["origin"]:
            raise SubmissionUnknown("原任务节点已移除或地址改变，保留原执行归属，不能重发。")
        return self.clients[node.id]

    def _request(self, method, path, payload=None, **kwargs):
        route = self.context.get()
        if route is None:
            # Legacy settings reads remain compatible. Writes require explicit ownership.
            if method != "GET" and path != "/api/router/backend-lifecycle":
                raise RuntimeError("多节点写操作需要明确执行设备。")
            return self.clients[self.legacy_node]._request(method, path, payload, **kwargs)
        if method == "POST" and path == "/prompt":
            if (payload or {}).get("extra_data", {}).get("h3", {}).get("execution_id") != route["execution_id"]:
                raise RuntimeError("执行编号与持久化节点归属不一致，未提交生成。")
            if not self.routes.claim_submission(route["execution_id"]):
                raise SubmissionUnknown("该执行已提交或结果未知，只能查询原节点。")
        try:
            result = self._client(route)._request(method, path, payload, **kwargs)
        except urllib.error.HTTPError as error:
            if method == "POST" and path == "/prompt" and error.code in {400, 401, 403, 409, 413}:
                self.routes.update(route["execution_id"], "error")
            raise
        if method == "POST" and path == "/prompt" and isinstance(result, dict) and result.get("prompt_id"):
            self.routes.update(route["execution_id"], "submitted", result["prompt_id"])
        return result

    def snapshots(self):
        def read(node):
            try:
                value = self.clients[node.id].capacity()
                if value.get("available") is not True or not 0 <= time.time() - value.get("sampled_at", 0) <= 15:
                    raise ValueError("stale node capacity")
                value = dict(value)
                value["active_execution_ids"] = list(getattr(self.clients[node.id], "active_execution_ids",
                                                            value.get("active_execution_ids", [])))
                return node.id, value
            except Exception:
                return node.id, {"available": False, "lanes": [], "reason": "节点离线或容量过期"}
        with ThreadPoolExecutor(max_workers=len(self.nodes)) as pool:
            return dict(pool.map(read, self.nodes.values()))

    def eligible(self, node, snapshot, capability, version=None):
        if (not node.enabled or snapshot.get("available") is not True or snapshot.get("resources_ok") is not True
                or snapshot.get("counts_complete") is not True or snapshot.get("exclusive_window")
                or type(snapshot.get("active")) is not int or snapshot["active"] < 0):
            return None
        if capability.startswith("legacy:"):
            profile = capability.split(":", 1)[1]
            if profile not in node.legacy_profiles:
                return None
            slots = (snapshot.get("studio_preview") or {}).get("available", 0) if profile == "preview" else 0
            if profile == "quality":
                slots = min(snapshot.get("limits", {}).get("long", 0),
                            sum(l.get("status") == "idle" for l in snapshot.get("lanes", [])))
        else:
            entry = snapshot.get("recipe_capacity", {}).get(capability, {})
            if not entry.get("eligible_lanes") or (version and entry.get("recipe_version") != version):
                return None
            slots = entry.get("available_slots", 0)
        if type(slots) is not int or slots <= 0:
            return None
        return {"slots": min(slots, node.max_parallel), "active": snapshot.get("active", 0),
                "execution_ids": set(snapshot.get("active_execution_ids", []))}

    def acceptance_admission(self, node, target, project_id, capability, version):
        """Reserve only an explicitly scoped acceptance POST, never global capacity."""
        if node.id == "edge" or target != node.id or not node.enabled or node.max_parallel != 1 or not version:
            return None
        try:
            snapshot = self.clients[node.id].capacity(fresh=True)
            entry = snapshot.get("recipe_capacity", {}).get(capability, {})
            if (snapshot.get("available") is not True or snapshot.get("resources_ok") is not True
                    or snapshot.get("counts_complete") is not True or snapshot.get("exclusive_window") is not False
                    or type(snapshot.get("active")) is not int or snapshot["active"] != 0
                    or type(snapshot.get("queued")) is not int or snapshot["queued"] != 0
                    or not 0 <= time.time() - snapshot.get("sampled_at", 0) <= 15
                    or entry.get("recipe_version") != version
                    or type(entry.get("available_slots")) is not int or entry["available_slots"] != 0
                    or entry.get("reasons") != ["release_validation_gate"]):
                return None
            # recipe_catalog performs a new GET, with no aggregate or cached catalog.
            catalog = self.clients[node.id].recipe_catalog()
            if catalog.get("enabled") is not True:
                return None
            exact = [item for item in catalog.get("multimodal_profiles", [])
                     if item.get("profile_id") == capability and item.get("version") == version]
            if (len(exact) != 1 or not isinstance(exact[0].get("acceptance_tasks"), list)
                    or project_id not in exact[0]["acceptance_tasks"]):
                return None
            return {"slots": 1, "active": 0, "execution_ids": set()}
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def edge_preparation_admission(self, node, target, project_id, capability, version):
        """Explicit Edge preparation reservation; never advertise a GPU slot."""
        if target != "edge" or node.id != "edge" or not node.enabled or node.max_parallel != 1 or not version:
            return None
        try:
            snapshot = self.clients[node.id].capacity(fresh=True)
            lifecycle = snapshot.get("model_lifecycle") or {}
            guard = snapshot.get("preparation_guard") or {}
            entry = snapshot.get("recipe_capacity", {}).get(capability, {})
            if (snapshot.get("available") is not True or snapshot.get("counts_complete") is not True
                    or snapshot.get("exclusive_window") is not False
                    or type(snapshot.get("active")) is not int or snapshot["active"] != 0
                    or type(snapshot.get("queued")) is not int or snapshot["queued"] != 0
                    or not 0 <= time.time() - snapshot.get("sampled_at", 0) <= 10
                    or lifecycle.get("state") != "idle" or lifecycle.get("queueable") is not True
                    or lifecycle.get("execution_id") is not None
                    or guard.get("eligible") is not True or guard.get("reasons") != []
                    or guard.get("requires_model_switch") is not True
                    or not 0 <= time.time() - guard.get("observed_at", 0) <= 10
                    or not isinstance(guard.get("profile_ids"), list) or capability not in guard["profile_ids"]
                    or entry.get("recipe_version") != version
                    or type(entry.get("available_slots")) is not int or entry["available_slots"] != 0):
                return None
            # The fresh Fleet guard explicitly verifies every non-memory guard.
            # resources_ok=False alone never grants this exception.
            catalog = self.clients[node.id].recipe_catalog()
            if catalog.get("enabled") is not True:
                return None
            exact = [item for item in catalog.get("multimodal_profiles", [])
                     if item.get("profile_id") == capability and item.get("version") == version]
            if len(exact) != 1:
                return None
            accepted = isinstance(exact[0].get("acceptance_tasks"), list) and project_id in exact[0]["acceptance_tasks"]
            if exact[0].get("qualified") is not True and not accepted:
                return None
            return {"slots": 1, "active": 0, "execution_ids": set(), "preparation_only": True}
        except (KeyError, TypeError, ValueError, OSError):
            return None

    @contextmanager
    def execution_scope(self, module, project_id, stage_id):
        project = module._require_project(project_id)
        stage = project["stages"][stage_id]
        existing = stage.get("execution_id")
        target = self.validate_target(stage.get("target_node", "auto"))
        batch_id = stage.get("batch_schedule_id")
        capability = (project.get("execution_profile") or {}).get("profile_id")
        version = (project.get("execution_profile") or {}).get("version")
        if module.recipe_scope(project, stage_id):
            capability = project.get("recipe_id")
        capability = capability or ("legacy:preview" if stage_id == "preview" and module.uses_turbo_preview(project) else "legacy:quality")
        execution_id = existing or (("studio_batch_" + batch_id) if batch_id else "studio") + "_" + project_id + "_" + stage_id + "_" + uuid.uuid4().hex
        if not existing:
            module._set_stage(project_id, stage_id, execution_id=execution_id, fleet_prepared=True)
        route = self.routes.get(execution_id)
        # Imported tasks always stay at the original Fleet, including when disabled.
        if existing and route is None and not stage.get("fleet_prepared"):
            route = self.routes.reserve(execution_id, [(self.nodes[self.legacy_node], {})], capability, batch_id, legacy=True)
        while route is None:
            if module._cancelled(project_id, stage_id):
                raise RuntimeError("排队任务已取消，未提交生成。")
            snapshots = self.snapshots()
            lease = self.routes.batch(batch_id) if batch_id else None
            candidates = []
            for node in self.nodes.values():
                if target != "auto" and node.id != target or lease and node.id != lease["node_id"]:
                    continue
                snapshot = dict(snapshots[node.id])
                if lease and lease["node_id"] == node.id:
                    # The Fleet batch lease is owned by this schedule, not a foreign gate.
                    snapshot["exclusive_window"] = False
                admission = self.eligible(node, snapshot, capability, version)
                if not admission and stage_id == "preview" and not batch_id:
                    admission = self.acceptance_admission(node, target, project_id, capability, version)
                    if not admission:
                        admission = self.edge_preparation_admission(node, target, project_id, capability, version)
                if admission:
                    candidates.append((node, admission))
            candidates.sort(key=lambda pair: (pair[1]["active"], pair[0].priority, pair[0].id))
            route = self.routes.reserve(execution_id, candidates, capability, batch_id)
            if route is None:
                module._set_stage(project_id, stage_id, status="queued", detail="等待设备配方资格、空闲名额及资源准入", progress=1)
                time.sleep(3)
        self._client(route)
        module._set_stage(project_id, stage_id, node_id=route["node_id"],
                          fleet_prepared=route["state"] == "reserved", status="running")
        token = self.context.set(route)
        try:
            yield
        finally:
            # No prompt POST was attempted: release only this preparation reservation.
            if self.routes.get(execution_id)["state"] == "reserved":
                self.routes.update(execution_id, "error")
            self.context.reset(token)

    def execution(self, execution_id):
        route = self.routes.get(execution_id)
        if route is None:
            raise SubmissionUnknown("缺少原任务节点归属，不能猜测或重新提交。")
        token = self.context.set(route)
        try:
            job = super().execution(execution_id)
            if job:
                status = job.get("status")
                if status in {"completed", "cancelled", "error"}:
                    self.routes.update(execution_id, status, job.get("prompt_id"))
                job["node_id"] = route["node_id"]
            return job
        finally:
            self.context.reset(token)

    def _download_output(self, job, destination, started):
        route = self.routes.get(job["execution_id"])
        result = self._client(route)._download_output(job, destination, started)
        return {**result, "node_id": route["node_id"]}

    def recipe_catalog(self):
        if self.context.get():
            return self._client(self.context.get()).recipe_catalog()
        entries, conflicts, profiles, profile_conflicts = {}, set(), {}, set()
        unavailable = []
        for node in self.nodes.values():
            if not node.enabled:
                continue
            catalog = self.clients[node.id].recipe_catalog()
            if not catalog.get("enabled"):
                continue
            for entry in catalog.get("recipes", []):
                key = entry.get("recipe_id")
                if key in entries and entries[key].get("version") != entry.get("version"):
                    conflicts.add(key)
                entries[key] = entry
            for profile in catalog.get("multimodal_profiles", []):
                key = profile.get("profile_id")
                if key in profiles and profiles[key].get("version") != profile.get("version"):
                    profile_conflicts.add(key)
                if key not in profiles or profile.get("qualified") is True:
                    profiles[key] = profile
            unavailable.extend(catalog.get("unavailable_multimodal_profiles", []))
        return {"enabled": bool(entries), "recipes": [v for k, v in entries.items() if k not in conflicts],
                "version_conflicts": sorted(conflicts | profile_conflicts),
                "multimodal_profiles": [v for k, v in profiles.items() if k not in profile_conflicts],
                "unavailable_multimodal_profiles": unavailable}

    def capacity(self):
        snapshots = self.snapshots()
        result = {"available": any(n.enabled and snapshots[n.id].get("available") for n in self.nodes.values()), "sampled_at": time.time(),
                  "nodes": [], "lanes": [], "active": 0, "queued": 0, "recipe_capacity": {},
                  "counts_complete": True, "resources_ok": True, "exclusive_window": False,
                  "limits": {"short_preview": 0, "short_quality": 0, "short_frames": 124, "long": 0}}
        for node in self.nodes.values():
            s = snapshots[node.id]
            held = [r for r in self.routes.active() if r["node_id"] == node.id]
            active = max(len(held), s.get("active", 0))
            result["nodes"].append({"id": node.id, "enabled": node.enabled, "max_parallel": node.max_parallel,
                                    "available": s.get("available", False), "active": active,
                                    "reason": s.get("reason"), "memory_available_bytes": s.get("memory_available_bytes"),
                                    "swap_used_bytes": s.get("swap_used_bytes"), "recipe_capacity": s.get("recipe_capacity", {}),
                                    "model_lifecycle": s.get("model_lifecycle")})
            result["lanes"].extend({**lane, "id": node.id + ":" + lane["id"], "node_id": node.id,
                                     "name": node.id + " · " + lane["name"]} for lane in s.get("lanes", []))
            result["active"] += active
            result["queued"] += s.get("queued", 0)
            result["counts_complete"] &= s.get("available", False) and s.get("counts_complete", True)
            if not node.enabled:
                continue  # Retain historical node diagnostics, exclude it from admission aggregates.
            result["resources_ok"] &= s.get("resources_ok", False)
            for key in ("short_preview", "short_quality", "long"):
                result["limits"][key] += min(node.max_parallel, s.get("limits", {}).get(key, 0)) if node.enabled else 0
            for key, entry in s.get("recipe_capacity", {}).items():
                aggregate = result["recipe_capacity"].setdefault(key, {"available_slots": 0, "eligible_lanes": [],
                    "reasons": [], "recipe_version": entry.get("recipe_version")})
                if aggregate["recipe_version"] != entry.get("recipe_version"):
                    aggregate["recipe_version"] = None
                    aggregate["reasons"].append("node_recipe_version_conflict")
                eligible = self.eligible(node, s, key)
                if eligible:
                    unseen = sum(r["execution_id"] not in eligible["execution_ids"] for r in held)
                    aggregate["available_slots"] += max(0, min(eligible["slots"] - unseen, node.max_parallel - active))
                    aggregate["eligible_lanes"].extend(node.id + ":" + lane for lane in entry.get("eligible_lanes", []))
                aggregate["reasons"].extend(node.id + ":" + reason for reason in entry.get("reasons", []))
        for entry in result["recipe_capacity"].values():
            if entry["recipe_version"] is None:
                entry["available_slots"] = 0
        return result

    def health(self):
        if not self.capacity()["available"]:
            raise RuntimeError("所有执行节点均不可用。")
        return {"system": {"os": "H3 Fleet cluster"}, "devices": []}

    def begin_batch(self, schedule_id, *, recovery_stages=()):
        lease = self.routes.batch(schedule_id)
        owners = set()
        for stage in recovery_stages:
            route = self.routes.get(stage["execution_id"])
            if route:
                self._client(route)
                owners.add(route["node_id"])
            elif not stage.get("fleet_prepared"):
                owners.add(self.legacy_node)
        if len(owners) > 1 or (owners and lease and lease["node_id"] not in owners):
            raise SubmissionUnknown("恢复批次的原节点归属冲突，保留原任务。")
        if lease is None and owners:
            lease = self.routes.begin_batch(schedule_id, [self.nodes[next(iter(owners))]], recovering=True)
            if lease is None:
                raise SubmissionUnknown("原批次节点仍被其他任务占用，不能迁移恢复任务。")
        if lease is None:
            snapshots = self.snapshots()
            candidates = [n for n in self.nodes.values() if n.enabled and "quality" in n.legacy_profiles
                          and snapshots[n.id].get("available") and not snapshots[n.id].get("active")
                          and not snapshots[n.id].get("exclusive_window")]
            lease = self.routes.begin_batch(schedule_id, sorted(candidates, key=lambda n: (n.priority, n.id)))
        if lease is None:
            raise RuntimeError("没有可取得批次租约的节点。")
        return self._client(lease).begin_batch(schedule_id)

    def end_batch(self, schedule_id):
        lease = self.routes.batch(schedule_id)
        if lease:
            result = self._client(lease).end_batch(schedule_id)
            self.routes.end_batch(schedule_id)
            return result
