from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .config import Settings
from .errors import RouterError
from .store import StateStore
from .types import Endpoint


@dataclass(frozen=True)
class BudgetReservation:
    key: str
    request_id: str
    amount_usd: float


class CloudBudget:
    def __init__(self, store: StateStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    async def reserve(
        self,
        endpoint: Endpoint,
        *,
        request_id: str,
        prompt_tokens: int,
        output_reserve_tokens: int,
    ) -> BudgetReservation | None:
        if not endpoint.cloud:
            return None
        if endpoint.metadata.get("billing_mode") == "subscription":
            return None
        cloud = self.settings.section("cloud")
        budget = float(cloud.get("monthly_budget", 0))
        if budget <= 0:
            raise RouterError(
                "cloud routing requires a positive monthly budget",
                status_code=402,
                code="cloud_budget_not_configured",
            )
        input_rate = endpoint.metadata.get("input_cost_per_million_usd")
        output_rate = endpoint.metadata.get("output_cost_per_million_usd")
        if input_rate is None or output_rate is None:
            raise RouterError(
                "cloud endpoint cost metadata is not configured",
                status_code=503,
                code="cloud_cost_not_configured",
            )
        amount = (
            prompt_tokens * float(input_rate)
            + output_reserve_tokens * float(output_rate)
        ) / 1_000_000
        key = f"router:cloud-budget:{datetime.now(timezone.utc):%Y-%m}"
        lock_key = f"{key}:lock"
        lock_token = uuid4().hex
        deadline = time.monotonic() + 2
        while not await self.store.acquire_lock(lock_key, lock_token, 5):
            if time.monotonic() >= deadline:
                raise RouterError(
                    "cloud budget ledger is busy",
                    status_code=503,
                    code="cloud_budget_busy",
                )
            await asyncio.sleep(0.05)
        try:
            value = await self.store.get_json(key) or {
                "spent_usd": 0.0,
                "reservations": {},
            }
            reservations = value.setdefault("reservations", {})
            self._commit_stale_reservations(value)
            projected = (
                float(value.get("spent_usd", 0))
                + sum(
                    float(item.get("amount_usd", 0))
                    for item in reservations.values()
                )
                + amount
            )
            if projected > budget:
                raise RouterError(
                    "cloud monthly budget would be exceeded",
                    status_code=429,
                    code="cloud_budget_exceeded",
                )
            reservations[request_id] = {
                "amount_usd": amount,
                "created_at": time.time(),
                "endpoint_id": endpoint.id,
            }
            await self.store.set_json(key, value, ttl_seconds=3456000)
        finally:
            await self.store.release_lock(lock_key, lock_token)
        return BudgetReservation(key, request_id, amount)

    async def commit(self, reservation: BudgetReservation | None) -> None:
        await self._finish(reservation, commit=True)

    async def release(self, reservation: BudgetReservation | None) -> None:
        await self._finish(reservation, commit=False)

    async def _finish(
        self,
        reservation: BudgetReservation | None,
        *,
        commit: bool,
    ) -> None:
        if reservation is None:
            return
        lock_key = f"{reservation.key}:lock"
        lock_token = uuid4().hex
        deadline = time.monotonic() + 2
        while not await self.store.acquire_lock(lock_key, lock_token, 5):
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.05)
        try:
            value = await self.store.get_json(reservation.key)
            if not value:
                return
            item = value.setdefault("reservations", {}).pop(
                reservation.request_id,
                None,
            )
            if commit and item:
                value["spent_usd"] = float(value.get("spent_usd", 0)) + float(
                    item.get("amount_usd", reservation.amount_usd)
                )
            await self.store.set_json(
                reservation.key,
                value,
                ttl_seconds=3456000,
            )
        finally:
            await self.store.release_lock(lock_key, lock_token)

    @staticmethod
    def _commit_stale_reservations(value: dict) -> None:
        reservations = value.setdefault("reservations", {})
        stale = [
            request_id
            for request_id, item in reservations.items()
            if time.time() - float(item.get("created_at", 0)) > 7200
        ]
        for request_id in stale:
            item = reservations.pop(request_id)
            value["spent_usd"] = float(value.get("spent_usd", 0)) + float(
                item.get("amount_usd", 0)
            )
