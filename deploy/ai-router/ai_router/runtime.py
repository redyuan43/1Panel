from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
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
from .endpoint_config import EndpointConfigManager
from .health import HealthMonitor
from .history import history_identities
from .policy import ConversationRepository, RoutingPolicy
from .prefix_affinity import PrefixAffinityRepository
from .prefix_prewarm import PrefixPrewarmer
from .cache_audit import TelemetryCollector
from .prompt_directives import PromptDirectiveStore, configured_phrases
from .privacy_review import PrivacyReviewer
from .route_trace import RouteTraceStore, registry_fingerprint
from .scheduler import ClientLimiter, Scheduler
from .store import InMemoryStateStore, RedisStateStore, StateStore
from .token_counter import HuggingFaceTokenCounter, TokenCounter
from .training_archive import TrainingArchive


@dataclass
class RouterRuntime:
    settings: Settings
    base_registry: Registry
    registry: Registry
    endpoint_configs: EndpointConfigManager
    store: StateStore
    token_counter: TokenCounter
    health: HealthMonitor
    conversations: ConversationRepository
    prefix_affinity: PrefixAffinityRepository
    scheduler: Scheduler
    limiter: ClientLimiter
    budget: CloudBudget
    clients: ClientAccountManager
    auth: AuthManager
    evaluator: TaskEvaluator
    compactor: ContextCompactor
    policy: RoutingPolicy
    audit: AuditLog
    route_traces: RouteTraceStore
    prompt_directives: PromptDirectiveStore
    training: TrainingArchive | None
    internal_client: httpx.AsyncClient
    internal_base_url: str
    internal_api_key: str
    instance_id: str
    boot_id: str
    state_encryption_key: str = field(repr=False)
    privacy_reviewer: PrivacyReviewer | None = field(default=None, init=False)
    prefix_prewarmer: PrefixPrewarmer | None = field(default=None, init=False, repr=False)
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
    _endpoint_config_revision: int = field(
        default=-1,
        init=False,
        repr=False,
    )
    _endpoint_config_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    def reload_settings(self) -> None:
        self.settings.reload()
        self.evaluator.settings = self.settings.section("evaluator")
        self.compactor.model_id = str(
            self.settings.section("compaction").get("model_id", "")
        )
        self.scheduler.max_priority_burst = int(
            self.settings.section("affinity").get("max_priority_burst", 8)
        )

    async def reload_endpoint_config(
        self,
        *,
        force: bool = False,
    ) -> int:
        revision = await self.endpoint_configs.revision()
        if not force and revision == self._endpoint_config_revision:
            return revision
        changed = revision != self._endpoint_config_revision
        async with self._endpoint_config_lock:
            revision = await self.endpoint_configs.revision()
            if not force and revision == self._endpoint_config_revision:
                return revision
            changed = revision != self._endpoint_config_revision
            registry = await self.endpoint_configs.effective_registry()
            self.registry = registry
            self.policy.registry = registry
            self._endpoint_config_revision = revision
        if changed and self._started and self.track_instance:
            await self._publish_instance_state(
                "draining" if self.draining else "running"
            )
        return revision

    async def start(self) -> None:
        if self._started:
            return
        await self.reload_endpoint_config(force=True)
        self._started = True
        imported = await self.clients.bootstrap_legacy()
        for item in imported:
            self.audit.write(
                "legacy_client_imported",
                client_id=item["client_id"],
                key_id=item["key_id"],
            )
        history_indexes = await self.store.list_json_items(
            "router:history-conversation:"
        )
        clients_by_conversation: dict[str, set[str]] = {}
        history_prefix = "router:history-conversation:"
        for key, value in history_indexes:
            conversation_id = str(
                value.get("branch_id")
                or value.get("conversation_id", "")
            )
            suffix = key.removeprefix(history_prefix)
            if not conversation_id or ":" not in suffix:
                continue
            client_id, _identity = suffix.rsplit(":", 1)
            clients_by_conversation.setdefault(
                conversation_id,
                set(),
            ).add(client_id)
        reindexed = 0
        states = [
            *await self.store.list_json("router:conversation:"),
            *await self.store.list_json("router:conversation-branch:"),
        ]
        for value in states:
            conversation_id = str(
                value.get("branch_id")
                or value.get("conversation_id", "")
            )
            encrypted_capsule = value.get("encrypted_capsule")
            if not conversation_id or not encrypted_capsule:
                continue
            try:
                messages = self.compactor.cipher.decrypt(
                    str(encrypted_capsule)
                )
            except Exception:
                continue
            if not isinstance(messages, list):
                continue
            identities = history_identities(messages)
            for client_id in clients_by_conversation.get(
                conversation_id,
                set(),
            ):
                await self.conversations.map_history(
                    client_id,
                    identities,
                    conversation_id,
                )
                reindexed += 1
        if reindexed:
            self.audit.write(
                "conversation_history_indexes_rebuilt",
                records=reindexed,
            )
        if (
            self.training
            and await self.training.needs_legacy_backfill()
        ):
            backfilled = await self.training.backfill_conversation_snapshots(
                states,
                self.state_encryption_key,
            )
            self.audit.write(
                "training_archive_backfill_completed",
                records=backfilled,
            )
        removed_traces = await self.route_traces.cleanup()
        if removed_traces:
            self.audit.write(
                "route_trace_retention_cleanup",
                records=removed_traces,
            )
        if not self.track_instance:
            return
        interrupted_traces = (
            await self.route_traces.interrupt_previous_boot(
                self.instance_id,
                self.boot_id,
            )
        )
        if interrupted_traces:
            self.audit.write(
                "route_traces_interrupted_by_restart",
                records=interrupted_traces,
                instance_id=self.instance_id,
                boot_id=self.boot_id,
            )
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
                "registry_fingerprint": registry_fingerprint(self.registry),
                "endpoint_config_revision": self._endpoint_config_revision,
            },
            ttl_seconds=None,
        )

    def _instance_state_key(self) -> str:
        return f"router:instance-state:{self.instance_id}"

    @staticmethod
    def _draining_deployment_key(deployment_id: str) -> str:
        return f"router:draining-deployment:{deployment_id}"

    async def close(self) -> None:
        if getattr(self, "cache_collector", None):
            await self.cache_collector.close()
        if self.prefix_prewarmer is not None:
            await self.prefix_prewarmer.close()
        if self.privacy_reviewer is not None:
            await self.privacy_reviewer.close()
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

    def prepare_overflow_prefix(self, decision, body, *, client_id, request_id, api_kind):
        if self.prefix_prewarmer is None:
            self.prefix_prewarmer = PrefixPrewarmer(self)
        self.prefix_prewarmer.submit(
            decision, body, client_id=client_id, request_id=request_id, api_kind=api_kind,
        )

    def review_privacy(self, body: dict[str, Any], api_kind: str, *, request_id: str, client_id: str) -> None:
        settings = self.settings.section("identity").get("review", {})
        if settings.get("mode", "off") != "shadow":
            return
        try:
            if self.privacy_reviewer is None:
                self.privacy_reviewer = PrivacyReviewer(self.store, self.audit, traces=self.route_traces)
            self.privacy_reviewer.submit(
                body, api_kind, settings, request_id=request_id, client_id=client_id,
            )
        except Exception:
            # Shadow mode is not an availability dependency of the serving path.
            pass


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
    endpoint_configs = EndpointConfigManager(store, registry)
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
    state_encryption_key = _required_env("AI_ROUTER_STATE_KEY")
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
        CapsuleCipher(state_encryption_key),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
        model_id=str(settings.section("compaction").get("model_id", "")),
    )
    audit_path = os.environ.get(
        "AI_ROUTER_AUDIT_PATH",
        "/data/audit/router.jsonl",
    )
    trace_database_path = os.environ.get(
        "AI_ROUTER_ROUTE_TRACE_DB_PATH",
        str(Path(audit_path).with_name("route-traces.sqlite3")),
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
        state_encryption_key,
    )
    training = None
    if _enabled_env("AI_ROUTER_TRAINING_ENABLED"):
        training = TrainingArchive(
            _required_env("AI_ROUTER_TRAINING_DB_PATH"),
            _required_env("AI_ROUTER_TRAINING_KEY_PATH"),
        )
    prompt_directive_store = PromptDirectiveStore(
        os.environ.get(
            "AI_ROUTER_PROMPT_DIRECTIVE_DB_PATH",
            str(settings.runtime_path.with_name("prompt-directives.sqlite3")),
        )
    )
    prompt_directive_store.sync_active(
        configured_phrases(
            settings.section("routing").get("prompt_directives", {})
        )
    )
    prefix_affinity = PrefixAffinityRepository(
        store,
        settings,
        state_encryption_key,
    )
    return RouterRuntime(
        settings=settings,
        base_registry=registry,
        registry=registry,
        endpoint_configs=endpoint_configs,
        store=store,
        token_counter=token_counter,
        health=health,
        conversations=ConversationRepository(store, settings),
        prefix_affinity=prefix_affinity,
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
        limiter=ClientLimiter(
            store,
            request_ttl_seconds=int(
                settings.section("queue").get("lock_ttl_seconds", 900)
            ),
        ),
        budget=CloudBudget(store, settings),
        clients=clients,
        auth=AuthManager(settings, clients),
        evaluator=evaluator,
        compactor=compactor,
        policy=RoutingPolicy(registry, settings, health, store=store),
        audit=AuditLog(audit_path),
        route_traces=RouteTraceStore(
            trace_database_path,
            retention_days=int(
                os.environ.get(
                    "AI_ROUTER_ROUTE_TRACE_RETENTION_DAYS",
                    "30",
                )
            ),
        ),
        prompt_directives=prompt_directive_store,
        training=training,
        internal_client=httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=5.0),
        ),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
        instance_id=resolved_instance_id,
        boot_id=resolved_boot_id,
        state_encryption_key=state_encryption_key,
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


def _enabled_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
