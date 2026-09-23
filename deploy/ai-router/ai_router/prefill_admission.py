"""Versioned, metadata-only backend admission contract (no cache estimation)."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
import time
from typing import Any
from .errors import CapacityBusyError


CAPABILITY = "cache-prefill-admission-v1"
BUSY_CODE = "prefill_admission_busy"


def configured_policy(config: dict[str, Any]) -> "AdmissionPolicy":
    return AdmissionPolicy(config)


@dataclass(frozen=True)
class ServiceGroup:
    id: str
    deployments: tuple[str, ...]
    mode: str
    capacity: int


class AdmissionPolicy:
    def __init__(self, config: dict[str, Any]):
        if not isinstance(config, dict):
            raise ValueError("prefill admission configuration must be a mapping")
        if not isinstance(config.get("enabled", False), bool):
            raise ValueError("prefill admission enabled must be a boolean")
        self.enabled = config.get("enabled", False) is True
        self.groups: dict[str, ServiceGroup] = {}
        if not self.enabled:
            return
        groups = config.get("groups", {})
        if not isinstance(groups, dict):
            raise ValueError("prefill admission groups must be a mapping")
        for group_id, value in groups.items():
            if not isinstance(group_id, str) or not group_id.strip() or not isinstance(value, dict):
                raise ValueError("invalid prefill admission service group")
            mode = value.get("mode")
            capacity = value.get("capacity", 1)
            deployments = value.get("deployments", ())
            if (not isinstance(deployments, (list, tuple)) or not deployments
                    or mode not in ("cache", "idle")
                    or isinstance(capacity, bool) or not isinstance(capacity, int)
                    or capacity < 1 or (mode == "idle" and capacity != 1)):
                raise ValueError("invalid prefill admission service group")
            group = ServiceGroup(group_id, tuple(deployments), mode, capacity)
            for deployment in deployments:
                if not isinstance(deployment, str) or not deployment or deployment in self.groups:
                    raise ValueError("ambiguous prefill admission deployment")
                self.groups[deployment] = group
        if not self.groups:
            raise ValueError("prefill admission requires explicit physical service groups")

    def group(self, deployment_id: str) -> ServiceGroup | None:
        return self.groups.get(deployment_id)

    def lease_target(self, deployment_id: str, capacity: int) -> tuple[str, int]:
        group = self.group(deployment_id)
        if group is None:
            return deployment_id, capacity
        return "prefill-group:" + group.id, group.capacity

    def verify(self, deployment_id: str, capability: dict[str, Any]) -> None:
        group = self.group(deployment_id)
        if group is None or group.mode != "cache":
            raise ValueError("unconfigured cache admission backend")
        if (not isinstance(capability, dict)
                or capability.get("capability") != CAPABILITY
                or capability.get("enabled") is not True
                or capability.get("service_group") != group.id
                or type(capability.get("max_concurrency")) is not int
                or capability.get("max_concurrency") != group.capacity
                or capability.get("lookup_protocol") != "lookup-admission-v1"
                or not isinstance(capability.get("cleanup_healthy"), bool)):
            raise ValueError("backend prefill admission capability mismatch")

    async def verify_registry(self, registry, client) -> None:
        if not self.enabled:
            return
        urls = {}
        for deployment_id, group in self.groups.items():
            endpoint = registry.by_id(deployment_id)
            if (endpoint is None or endpoint.cloud or not endpoint.api_base
                    or endpoint.backend_type == "ai_pool"):
                raise ValueError("admission requires registered direct local deployments")
            url = endpoint.api_base.rstrip("/")
            if url in urls and urls[url] != group.id:
                raise ValueError("one backend cannot use separate admission groups")
            urls[url] = group.id
            if group.mode == "cache":
                key = os.environ.get(endpoint.backend_api_key_env, "")
                response = await client.get(url + "/_prefill_admission",
                    headers={"Authorization": "Bearer " + key} if key else {}, timeout=5)
                response.raise_for_status()
                self.verify(deployment_id, response.json())

    def is_busy(self, deployment_id: str, status: int, headers: Any, payload: bytes) -> bool:
        group = self.group(deployment_id)
        if group is None or group.mode != "cache" or status != 409:
            return False
        if headers.get("x-prefill-admission") != CAPABILITY:
            return False
        try:
            error = json.loads(payload).get("error", {})
        except (ValueError, AttributeError, TypeError):
            return False
        return isinstance(error, dict) and error.get("code") == BUSY_CODE

    def exclude_group(self, deployment_id: str, excluded: set[str]) -> None:
        group = self.group(deployment_id)
        excluded.update(group.deployments if group else (deployment_id,))


class AttemptWindow:
    """One pre-output deadline and one dispatch per physical target, per request."""
    def __init__(self, policy, seconds):
        self.policy = policy
        self.deadline = time.monotonic() + seconds if policy.enabled else None
        self.tried = set()

    def remaining(self):
        if self.deadline is None:
            return None
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise CapacityBusyError()
        return value

    def dispatch(self, deployment_id, excluded):
        if not self.policy.enabled:
            return
        self.remaining()
        group = self.policy.group(deployment_id)
        key = ("group", group.id) if group else ("deployment", deployment_id)
        if key in self.tried:
            raise CapacityBusyError()
        self.tried.add(key)
        self.policy.exclude_group(deployment_id, excluded)

    async def run(self, operation):
        try:
            remaining = self.remaining()
        except BaseException:
            operation.close()
            raise
        if remaining is None:
            return await operation
        try:
            return await asyncio.wait_for(operation, remaining)
        except asyncio.TimeoutError:
            raise CapacityBusyError() from None

def can_spill(*, requested_model: str, required_endpoint_id: str | None,
              directive: bool, pinned: bool, output_started: bool) -> bool:
    return (requested_model == "auto" and not required_endpoint_id
            and not directive and not pinned and not output_started)
