from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol


class StateStore(Protocol):
    async def ping(self) -> bool: ...

    async def get_json(self, key: str) -> dict[str, Any] | None: ...

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def acquire_lock(self, key: str, token: str, ttl_seconds: int) -> bool: ...

    async def release_lock(self, key: str, token: str) -> None: ...

    async def enqueue(self, key: str, member: str, score: float) -> None: ...

    async def queue_head(self, key: str) -> str | None: ...

    async def dequeue(self, key: str, member: str) -> None: ...

    async def increment_window(self, key: str, amount: int, window_seconds: int) -> int: ...

    async def acquire_semaphore(self, key: str, token: str, limit: int, ttl_seconds: int) -> bool: ...

    async def release_semaphore(self, key: str, token: str) -> None: ...


@dataclass
class _ExpiringValue:
    value: Any
    expires_at: float | None

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class InMemoryStateStore:
    def __init__(self) -> None:
        self._values: dict[str, _ExpiringValue] = {}
        self._queues: dict[str, dict[str, float]] = defaultdict(dict)
        self._semaphores: dict[str, dict[str, float]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_json(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            self._purge_locked()
            item = self._values.get(key)
            if item is None:
                return None
            return json.loads(json.dumps(item.value))

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        async with self._lock:
            self._values[key] = _ExpiringValue(json.loads(json.dumps(value)), expires_at)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)

    async def acquire_lock(self, key: str, token: str, ttl_seconds: int) -> bool:
        async with self._lock:
            self._purge_locked()
            if key in self._values:
                return False
            self._values[key] = _ExpiringValue(token, time.time() + ttl_seconds)
            return True

    async def release_lock(self, key: str, token: str) -> None:
        async with self._lock:
            item = self._values.get(key)
            if item and item.value == token:
                self._values.pop(key, None)

    async def enqueue(self, key: str, member: str, score: float) -> None:
        async with self._lock:
            self._queues[key][member] = score

    async def queue_head(self, key: str) -> str | None:
        async with self._lock:
            values = self._queues.get(key, {})
            if not values:
                return None
            return min(values, key=lambda item: (values[item], item))

    async def dequeue(self, key: str, member: str) -> None:
        async with self._lock:
            self._queues.get(key, {}).pop(member, None)

    async def increment_window(self, key: str, amount: int, window_seconds: int) -> int:
        async with self._lock:
            self._purge_locked()
            item = self._values.get(key)
            if item is None:
                total = amount
                self._values[key] = _ExpiringValue(total, time.time() + window_seconds)
                return total
            total = int(item.value) + amount
            item.value = total
            return total

    async def acquire_semaphore(self, key: str, token: str, limit: int, ttl_seconds: int) -> bool:
        now = time.time()
        async with self._lock:
            values = self._semaphores[key]
            expired = [item for item, expires_at in values.items() if expires_at <= now]
            for item in expired:
                values.pop(item, None)
            if token in values:
                values[token] = now + ttl_seconds
                return True
            if len(values) >= limit:
                return False
            values[token] = now + ttl_seconds
            return True

    async def release_semaphore(self, key: str, token: str) -> None:
        async with self._lock:
            self._semaphores.get(key, {}).pop(token, None)

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [key for key, item in self._values.items() if item.expired(now)]
        for key in expired:
            self._values.pop(key, None)


class RedisStateStore:
    def __init__(self, url: str) -> None:
        try:
            from redis.asyncio import from_url
        except ImportError as exc:
            raise RuntimeError("redis package is required for RedisStateStore") from exc
        self._client = from_url(url, decode_responses=True)

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()

    async def get_json(self, key: str) -> dict[str, Any] | None:
        value = await self._client.get(key)
        return json.loads(value) if value else None

    async def set_json(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._client.set(key, payload, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def acquire_lock(self, key: str, token: str, ttl_seconds: int) -> bool:
        return bool(await self._client.set(key, token, nx=True, ex=ttl_seconds))

    async def release_lock(self, key: str, token: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        await self._client.eval(script, 1, key, token)

    async def enqueue(self, key: str, member: str, score: float) -> None:
        await self._client.zadd(key, {member: score})

    async def queue_head(self, key: str) -> str | None:
        values = await self._client.zrange(key, 0, 0)
        return values[0] if values else None

    async def dequeue(self, key: str, member: str) -> None:
        await self._client.zrem(key, member)

    async def increment_window(self, key: str, amount: int, window_seconds: int) -> int:
        script = """
        local total = redis.call('incrby', KEYS[1], ARGV[1])
        if total == tonumber(ARGV[1]) then
          redis.call('expire', KEYS[1], ARGV[2])
        end
        return total
        """
        return int(await self._client.eval(script, 1, key, amount, window_seconds))

    async def acquire_semaphore(self, key: str, token: str, limit: int, ttl_seconds: int) -> bool:
        script = """
        local now = tonumber(ARGV[1])
        local expires = tonumber(ARGV[2])
        local limit = tonumber(ARGV[3])
        redis.call('zremrangebyscore', KEYS[1], '-inf', now)
        if redis.call('zscore', KEYS[1], ARGV[4]) then
          redis.call('zadd', KEYS[1], expires, ARGV[4])
          redis.call('expire', KEYS[1], tonumber(ARGV[5]))
          return 1
        end
        if redis.call('zcard', KEYS[1]) >= limit then
          return 0
        end
        redis.call('zadd', KEYS[1], expires, ARGV[4])
        redis.call('expire', KEYS[1], tonumber(ARGV[5]))
        return 1
        """
        now = time.time()
        return bool(
            await self._client.eval(
                script,
                1,
                key,
                now,
                now + ttl_seconds,
                limit,
                token,
                ttl_seconds,
            )
        )

    async def release_semaphore(self, key: str, token: str) -> None:
        await self._client.zrem(key, token)
