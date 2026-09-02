from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

from .errors import ConversationBusyError, QueueTimeoutError
from .store import StateStore


@dataclass
class Lease:
    store: StateStore
    owner_token: str
    conversation_key: str | None
    conversation_token: str | None
    deployment_key: str | None = None
    deployment_token: str | None = None
    queue_key: str | None = None
    queue_member: str | None = None

    async def release_deployment(self) -> None:
        if self.deployment_key and self.deployment_token:
            await self.store.release_semaphore(
                self.deployment_key,
                self.deployment_token,
            )
        if self.queue_key and self.queue_member:
            await self.store.dequeue(self.queue_key, self.queue_member)
        self.deployment_key = None
        self.deployment_token = None
        self.queue_key = None
        self.queue_member = None

    async def release(self) -> None:
        await self.release_deployment()
        if self.conversation_key and self.conversation_token:
            await self.store.release_lock(self.conversation_key, self.conversation_token)
        self.conversation_key = None
        self.conversation_token = None


class Scheduler:
    def __init__(
        self,
        store: StateStore,
        *,
        lock_ttl_seconds: int = 900,
        max_priority_burst: int = 8,
        instance_id: str = "standalone",
        boot_id: str | None = None,
    ) -> None:
        self.store = store
        self.lock_ttl_seconds = lock_ttl_seconds
        self.max_priority_burst = max_priority_burst
        self.instance_id = instance_id
        self.boot_id = boot_id or uuid4().hex

    @property
    def token_prefix(self) -> str:
        return f"{self.instance_id}:"

    def _owner_token(self) -> str:
        return f"{self.instance_id}:{self.boot_id}:{uuid4().hex}"

    async def cleanup_previous_instance_leases(self) -> dict[str, object]:
        return await self.store.cleanup_instance_leases(self.token_prefix)

    async def begin_request(self, conversation_id: str | None) -> Lease:
        token = self._owner_token()
        if not conversation_id:
            return Lease(self.store, token, None, None)
        key = f"router:conversation-lock:{conversation_id}"
        if not await self.store.acquire_lock(key, token, self.lock_ttl_seconds):
            raise ConversationBusyError()
        return Lease(self.store, token, key, token)

    async def acquire_deployment(
        self,
        lease: Lease,
        endpoint_id: str,
        request_id: str,
        *,
        timeout_seconds: float,
        affinity_priority: bool,
        capacity: int = 1,
    ) -> None:
        await self.acquire_deployment_candidates(
            lease,
            endpoint_id,
            (endpoint_id,),
            request_id,
            timeout_seconds=timeout_seconds,
            affinity_priority=affinity_priority,
            capacity=capacity,
        )

    async def try_acquire_deployment_candidates(
        self,
        lease: Lease,
        deployment_ids: tuple[str, ...],
        *,
        capacity: int = 1,
    ) -> str | None:
        if not deployment_ids:
            raise ValueError("at least one deployment candidate is required")
        await lease.release_deployment()
        token = lease.owner_token
        for deployment_id in deployment_ids:
            deployment_key = f"router:deployment-capacity:{deployment_id}"
            acquired = await self.store.acquire_semaphore(
                deployment_key,
                token,
                max(1, capacity),
                self.lock_ttl_seconds,
            )
            if not acquired:
                continue
            lease.deployment_key = deployment_key
            lease.deployment_token = token
            return deployment_id
        return None

    async def acquire_deployment_candidates(
        self,
        lease: Lease,
        endpoint_id: str,
        deployment_ids: tuple[str, ...],
        request_id: str,
        *,
        timeout_seconds: float,
        affinity_priority: bool,
        capacity: int = 1,
    ) -> str:
        if not deployment_ids:
            raise ValueError("at least one deployment candidate is required")
        await lease.release_deployment()
        queue_key = f"router:queue:{endpoint_id}"
        token = lease.owner_token
        queue_member = f"{token}:{request_id}"
        priority = False
        if affinity_priority:
            minute = int(time.time() // 60)
            burst = await self.store.increment_window(
                f"router:affinity-burst:{endpoint_id}:{minute}",
                1,
                70,
            )
            priority = burst <= self.max_priority_burst
        score = time.time() - (60.0 if priority else 0.0)
        await self.store.enqueue(queue_key, queue_member, score)
        deadline = time.monotonic() + timeout_seconds

        try:
            while True:
                stale_before = (
                    time.time() - self.lock_ttl_seconds - 60.0
                )
                if (
                    await self.store.queue_head(
                        queue_key,
                        stale_before=stale_before,
                    )
                    == queue_member
                ):
                    for deployment_id in deployment_ids:
                        deployment_key = (
                            f"router:deployment-capacity:{deployment_id}"
                        )
                        acquired = await self.store.acquire_semaphore(
                            deployment_key,
                            token,
                            max(1, capacity),
                            self.lock_ttl_seconds,
                        )
                        if not acquired:
                            continue
                        await self.store.dequeue(queue_key, queue_member)
                        lease.deployment_key = deployment_key
                        lease.deployment_token = token
                        lease.queue_key = None
                        lease.queue_member = None
                        return deployment_id
                if time.monotonic() >= deadline:
                    raise QueueTimeoutError()
                await asyncio.sleep(0.1)
        except BaseException:
            await self.store.dequeue(queue_key, queue_member)
            raise


class ClientLimiter:
    def __init__(self, store: StateStore, *, request_ttl_seconds: int = 900) -> None:
        self.store = store
        self.request_ttl_seconds = request_ttl_seconds

    async def acquire_parallel(self, client_id: str, token: str, limit: int) -> bool:
        return await self.store.acquire_semaphore(
            f"router:client-parallel:{client_id}",
            token,
            limit,
            self.request_ttl_seconds,
        )

    async def release_parallel(self, client_id: str, token: str) -> None:
        await self.store.release_semaphore(
            f"router:client-parallel:{client_id}",
            token,
        )

    async def check_rate_limits(
        self,
        client_id: str,
        *,
        prompt_tokens: int,
        rpm_limit: int,
        tpm_limit: int,
    ) -> tuple[bool, str | None]:
        minute = int(time.time() // 60)
        requests = await self.store.increment_window(
            f"router:client-rpm:{client_id}:{minute}",
            1,
            70,
        )
        if requests > rpm_limit:
            return False, "rpm_limit_exceeded"
        tokens = await self.store.increment_window(
            f"router:client-tpm:{client_id}:{minute}",
            prompt_tokens,
            70,
        )
        if tokens > tpm_limit:
            return False, "tpm_limit_exceeded"
        return True, None
