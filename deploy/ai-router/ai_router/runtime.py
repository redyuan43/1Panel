from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from .audit import AuditLog
from .auth import AuthManager
from .budget import CloudBudget
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
    auth: AuthManager
    evaluator: TaskEvaluator
    compactor: ContextCompactor
    policy: RoutingPolicy
    audit: AuditLog
    internal_client: httpx.AsyncClient
    internal_base_url: str
    internal_api_key: str

    def reload_settings(self) -> None:
        self.settings.reload()
        self.evaluator.settings = self.settings.section("evaluator")
        self.compactor.model_id = str(
            self.settings.section("compaction").get("model_id", "")
        )
        self.scheduler.max_priority_burst = int(
            self.settings.section("affinity").get("max_priority_burst", 8)
        )

    async def close(self) -> None:
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
) -> RouterRuntime:
    settings = settings or Settings()
    registry = registry or Registry()
    store = store or _build_store()
    token_counter = token_counter or HuggingFaceTokenCounter(
        _required_env("AI_ROUTER_TOKENIZER_PATH")
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
        ),
        limiter=ClientLimiter(store),
        budget=CloudBudget(store, settings),
        auth=AuthManager(settings),
        evaluator=evaluator,
        compactor=compactor,
        policy=RoutingPolicy(registry, settings, health),
        audit=AuditLog(audit_path),
        internal_client=httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=5.0),
        ),
        internal_base_url=internal_base_url,
        internal_api_key=internal_api_key,
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
