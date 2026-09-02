from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from .audit import AuditLog
from .auth import AuthManager
from .budget import CloudBudget
from .client_accounts import ClientAccountManager
from .compaction import CapsuleCipher, ContextCompactor
from .config import Registry, Settings
from .evaluator import TaskEvaluator
from .health import HealthMonitor
from .policy import ConversationRepository, RoutingPolicy
from .scheduler import ClientLimiter, Scheduler
from .store import InMemoryStateStore, RedisStateStore, StateStore
from .token_counter import HuggingFaceTokenCounter, TokenCounter


@dataclass
class RouterRuntime:
    settings: Settings
    registry: Registry
    store: StateStore
    token_counter: TokenCounter
    health: HealthMonitor
    conversations: ConversationRepository
    scheduler: Scheduler
    limiter: ClientLimiter
    budget: CloudBudget
    clients: ClientAccountManager
    auth: AuthManager
    evaluator: TaskEvaluator
    compactor: ContextCompactor
    policy: RoutingPolicy
    audit: AuditLog
    internal_client: httpx.AsyncClient
    internal_base_url: str
    internal_api_key: str
    instance_id: str
    boot_id: str
    track_instance: bool = False
    draining: bool = False
    started_at: float = field(default_factory=time.time)
    startup_cleanup: dict[str, Any] = field(default_factory=dict)
    _active_requests: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _state_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _started: bool = field(default=False, init=False, repr=False)

    def reload_settings(self) -> None:
        self.settings.reload()
        self.evaluator.settings = self.settings.section("evaluator")
        self.compactor.model_id = str(
            self.settings.section("compaction").get("model_id", "")
        )
        self.scheduler.max_priority_burst = int(
            self.settings.section("affinity").get("max_priority_burst", 8)
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        imported = await self.clients.bootstrap_legacy()
        for item in imported:
            self.audit.write(
                "legacy_client_imported",
                client_id=item["client_id"],
                key_id=item["key_id"],
            )
        if not self.track_instance:
            return
        previous = await self.store.get_json(self._instance_state_key())
        cleanup = await self.scheduler.cleanup_previous_instance_leases()
        self.startup_cleanup = cleanup
        for deployment_id in cleanup.get("deployments", []):
            await self.store.set_json(
                self._draining_deployment_key(str(deployment_id)),
                {
                    "deployment_id": str(deployment_id),
                    "instance_id": self.instance_id,
                    "previous_boot_id": (
                        str(previous.get("boot_id", ""))
                        if previous
                        else ""
                    ),
                    "cleared_at": time.time(),
                    "last_busy_audit_at": 0.0,
                },
                ttl_seconds=max(7200, self.scheduler.lock_ttl_seconds * 2),
            )
        completed_request_ids = {
            str(item.get("request_id"))
            for item in self.audit.recent(5000)
            if item.get("event") == "request_completed"
            and item.get("request_id")
        }
        if previous:
            for item in previous.get("active_requests", []):
                if not isinstance(item, dict) or not item.get("request_id"):
                    continue
                if str(item["request_id"]) in completed_request_ids:
                    continue
                self.audit.write(
                    "request_interrupted_by_restart",
                    request_id=str(item["request_id"]),
                    conversation_id=item.get("conversation_id"),
                    requested_model=item.get("requested_model"),
                    selected_model=item.get("selected_model"),
                    endpoint_id=item.get("endpoint_id"),
                    deployment_id=item.get("deployment_id"),
                    node=item.get("node"),
                    task=item.get("task"),
                    reason=item.get("reason"),
                    affinity=item.get("affinity"),
                    prompt_tokens=item.get("prompt_tokens"),
                    output_reserve_tokens=item.get(
                        "output_reserve_tokens"
                    ),
                    instance_id=self.instance_id,
                    previous_boot_id=previous.get("boot_id"),
                    boot_id=self.boot_id,
                )
        self.audit.write(
            "startup_lease_cleanup",
            instance_id=self.instance_id,
            boot_id=self.boot_id,
            previous_boot_id=previous.get("boot_id") if previous else None,
            **cleanup,
        )
        await self._publish_instance_state("running")

    async def track_request_started(
        self,
        tracking_id: str,
        request_id: str,
        conversation_id: str | None,
    ) -> None:
        if not self.track_instance:
            return
        async with self._state_lock:
            self._active_requests[tracking_id] = {
                "request_id": request_id,
                "conversation_id": conversation_id,
                "started_at": time.time(),
            }
            await self._publish_instance_state_locked("draining" if self.draining else "running")

    async def track_request_routed(
        self,
        tracking_id: str,
        *,
        requested_model: str,
        selected_model: str,
        endpoint_id: str,
        deployment_id: str,
        node: str,
        task: str,
        reason: str,
        affinity: str,
        prompt_tokens: int,
        output_reserve_tokens: int,
    ) -> None:
        if not self.track_instance:
            return
        async with self._state_lock:
            item = self._active_requests.get(tracking_id)
            if item is None:
                return
            item.update(
                {
                    "requested_model": requested_model,
                    "selected_model": selected_model,
                    "endpoint_id": endpoint_id,
                    "deployment_id": deployment_id,
                    "node": node,
                    "task": task,
                    "reason": reason,
                    "affinity": affinity,
                    "prompt_tokens": prompt_tokens,
                    "output_reserve_tokens": output_reserve_tokens,
                }
            )
            await self._publish_instance_state_locked(
                "draining" if self.draining else "running"
            )

    async def track_request_finished(self, tracking_id: str) -> None:
        if not self.track_instance:
            return
        async with self._state_lock:
            self._active_requests.pop(tracking_id, None)
            await self._publish_instance_state_locked(
                "draining" if self.draining else "running"
            )

    async def set_draining(self, value: bool = True) -> None:
        self.draining = value
        if self.track_instance:
            await self._publish_instance_state(
                "draining" if value else "running"
            )

    async def draining_marker(
        self,
        deployment_id: str,
    ) -> dict[str, Any] | None:
        return await self.store.get_json(
            self._draining_deployment_key(deployment_id)
        )

    async def clear_draining_marker(self, deployment_id: str) -> None:
        await self.store.delete(
            self._draining_deployment_key(deployment_id)
        )

    async def record_draining_busy(
        self,
        deployment_id: str,
        marker: dict[str, Any],
    ) -> None:
        now = time.time()
        if now - float(marker.get("last_busy_audit_at", 0)) < 5:
            return
        marker["last_busy_audit_at"] = now
        await self.store.set_json(
            self._draining_deployment_key(deployment_id),
            marker,
            ttl_seconds=max(7200, self.scheduler.lock_ttl_seconds * 2),
        )
        self.audit.write(
            "backend_busy_after_restart",
            deployment_id=deployment_id,
            instance_id=self.instance_id,
            boot_id=self.boot_id,
            cleared_by_instance=marker.get("instance_id"),
            previous_boot_id=marker.get("previous_boot_id"),
        )

    async def instance_states(self) -> list[dict[str, Any]]:
        values = await self.store.list_json("router:instance-state:")
        return sorted(
            values,
            key=lambda item: str(item.get("instance_id", "")),
        )

    async def _publish_instance_state(self, status: str) -> None:
        async with self._state_lock:
            await self._publish_instance_state_locked(status)

    async def _publish_instance_state_locked(self, status: str) -> None:
        await self.store.set_json(
            self._instance_state_key(),
            {
                "instance_id": self.instance_id,
                "boot_id": self.boot_id,
                "status": status,
                "draining": self.draining,
                "started_at": self.started_at,
                "updated_at": time.time(),
                "active_request_count": len(self._active_requests),
                "active_requests": list(self._active_requests.values()),
                "startup_cleanup": self.startup_cleanup,
            },
            ttl_seconds=None,
        )

    def _instance_state_key(self) -> str:
        return f"router:instance-state:{self.instance_id}"

    @staticmethod
    def _draining_deployment_key(deployment_id: str) -> str:
        return f"router:draining-deployment:{deployment_id}"

    async def close(self) -> None:
        if self.track_instance:
            await self._publish_instance_state("stopped")
        clients = {
            getattr(self.health, "client", None),
            getattr(self.evaluator, "client", None),
            getattr(self.compactor, "client", None),
            self.internal_client,
        }
        for client in clients:
            if client is not None:
                await client.aclose()
        close = getattr(self.store, "close", None)
        if close:
            await close()


def build_runtime(
    *,
    settings: Settings | None = None,
    registry: Registry | None = None,
    store: StateStore | None = None,
    token_counter: TokenCounter | None = None,
    instance_id: str | None = None,
    boot_id: str | None = None,
) -> RouterRuntime:
    settings = settings or Settings()
    registry = registry or Registry()
    store = store or _build_store()
    limits = settings.section("limits")
    token_counter = token_counter or HuggingFaceTokenCounter(
        _required_env("AI_ROUTER_TOKENIZER_PATH"),
        image_token_estimate=int(
            limits.get("image_token_estimate", 1024)
        ),
        audio_token_estimate=int(
            limits.get("audio_token_estimate", 4096)
        ),
    )
    internal_base_url = os.environ.get(
        "AI_ROUTER_LITELLM_URL",
        "http://litellm:4000",
    ).rstrip("/")
    internal_api_key = _required_env("AI_ROUTER_LITELLM_MASTER_KEY")
    health_settings = settings.section("health")
    health = HealthMonitor(
        store,
        refresh_seconds=float(health_settings.get("refresh_seconds", 5)),
        stale_after_seconds=float(health_settings.get("stale_after_seconds", 15)),
    )
    evaluator = TaskEvaluator(
        settings.section("evaluator"),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
    )
    compactor = ContextCompactor(
        token_counter,
        CapsuleCipher(_required_env("AI_ROUTER_STATE_KEY")),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
        model_id=str(settings.section("compaction").get("model_id", "")),
    )
    audit_path = os.environ.get(
        "AI_ROUTER_AUDIT_PATH",
        "/data/audit/router.jsonl",
    )
    configured_instance_id = (
        instance_id
        or os.environ.get("AI_ROUTER_INSTANCE_ID", "").strip()
    )
    resolved_instance_id = configured_instance_id or "standalone"
    resolved_boot_id = boot_id or uuid4().hex
    clients = ClientAccountManager(
        store,
        settings,
        _required_env("AI_ROUTER_STATE_KEY"),
    )
    return RouterRuntime(
        settings=settings,
        registry=registry,
        store=store,
        token_counter=token_counter,
        health=health,
        conversations=ConversationRepository(store, settings),
        scheduler=Scheduler(
            store,
            lock_ttl_seconds=int(
                settings.section("queue").get("lock_ttl_seconds", 900)
            ),
            max_priority_burst=int(
                settings.section("affinity").get("max_priority_burst", 8)
            ),
            instance_id=resolved_instance_id,
            boot_id=resolved_boot_id,
        ),
        limiter=ClientLimiter(store),
        budget=CloudBudget(store, settings),
        clients=clients,
        auth=AuthManager(settings, clients),
        evaluator=evaluator,
        compactor=compactor,
        policy=RoutingPolicy(registry, settings, health),
        audit=AuditLog(audit_path),
        internal_client=httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=5.0),
        ),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
        instance_id=resolved_instance_id,
        boot_id=resolved_boot_id,
        track_instance=bool(configured_instance_id),
    )


def _build_store() -> StateStore:
    backend = os.environ.get("AI_ROUTER_STATE_BACKEND", "redis").strip().lower()
    if backend == "memory":
        return InMemoryStateStore()
    if backend != "redis":
        raise ValueError(f"unsupported AI_ROUTER_STATE_BACKEND: {backend}")
    return RedisStateStore(
        os.environ.get("AI_ROUTER_REDIS_URL", "redis://redis:6379/0")
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value
