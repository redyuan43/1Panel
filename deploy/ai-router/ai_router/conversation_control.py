from __future__ import annotations

import time
from typing import Any

from .store import StateStore


PIN_TTL_SECONDS = frozenset({900, 3600, 21600, 86400})


class ConversationControlManager:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    async def get(
        self,
        client_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        pin = await self.store.get_json(
            self._pin_key(client_id, conversation_id)
        )
        reset = await self.store.get_json(
            self._reset_key(client_id, conversation_id)
        )
        return {
            "client_id": client_id,
            "conversation_id": conversation_id,
            "pin": pin,
            "reset_pending": bool(reset),
        }

    async def pin(
        self,
        *,
        client_id: str,
        conversation_id: str,
        endpoint_id: str,
        ttl_seconds: int,
        operator: str,
        reason: str,
    ) -> dict[str, Any]:
        if ttl_seconds not in PIN_TTL_SECONDS:
            raise ValueError(
                "ttl_seconds must be 900, 3600, 21600, or 86400"
            )
        now = time.time()
        value = {
            "endpoint_id": endpoint_id,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "operator": operator,
            "reason": reason,
        }
        await self.store.set_json(
            self._pin_key(client_id, conversation_id),
            value,
            ttl_seconds=ttl_seconds,
        )
        return value

    async def unpin(
        self,
        client_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        key = self._pin_key(client_id, conversation_id)
        previous = await self.store.get_json(key)
        await self.store.delete(key)
        return previous

    async def request_reset(
        self,
        *,
        client_id: str,
        conversation_id: str,
        operator: str,
        reason: str,
    ) -> dict[str, Any]:
        value = {
            "created_at": time.time(),
            "operator": operator,
            "reason": reason,
        }
        await self.store.set_json(
            self._reset_key(client_id, conversation_id),
            value,
            ttl_seconds=86400,
        )
        return value

    async def consume_for_request(
        self,
        client_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        reset_key = self._reset_key(client_id, conversation_id)
        reset = await self.store.get_json(reset_key)
        if reset:
            await self.store.delete(reset_key)
        pin = await self.store.get_json(
            self._pin_key(client_id, conversation_id)
        )
        return {
            "pin": pin,
            "reset": reset,
        }

    @staticmethod
    def _pin_key(client_id: str, conversation_id: str) -> str:
        return f"router:conversation-pin:{client_id}:{conversation_id}"

    @staticmethod
    def _reset_key(client_id: str, conversation_id: str) -> str:
        return f"router:conversation-reset:{client_id}:{conversation_id}"
