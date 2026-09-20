from __future__ import annotations

import asyncio
import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .config import Settings
from .errors import RouterError
from .store import StateStore
from .types import Endpoint
from .usage_evidence import token_count, usage_measurement
from .budget_pricing import budget_rates


@dataclass(frozen=True)
class BudgetReservation:
    key: str
    request_id: str
    amount_usd: float
    input_rate: float | None = None
    output_rate: float | None = None
    cached_rate: float | None = None


def usage_cost(reservation: BudgetReservation, usage: dict | None) -> tuple[float, str]:
    """Use measured tokens; missing evidence keeps a conservative estimate."""
    measured = usage_measurement(usage=usage)
    outputs = [token_count(usage[k]) for k in ("completion_tokens", "output_tokens")
               if isinstance(usage, dict) and k in usage]
    inputs, cached = measured["input_tokens"], measured["cached_tokens"]
    if (measured["state"] == "invalid" or inputs is None or not outputs
            or None in outputs or len(set(outputs)) != 1
            or reservation.input_rate is None or reservation.output_rate is None):
        return reservation.amount_usd, "estimated_usage_missing"
    rates = (reservation.input_rate, reservation.output_rate,
             reservation.cached_rate if reservation.cached_rate is not None else reservation.input_rate)
    if any(not math.isfinite(r) or r < 0 for r in rates):
        return reservation.amount_usd, "estimated_price_missing"
    amount = ((inputs - (cached or 0)) * rates[0]
              + (cached or 0) * rates[2] + outputs[0] * rates[1]) / 1_000_000
    measurement = "measured"
    if cached is None or (cached > 0 and reservation.cached_rate is None):
        measurement = "estimated_cache_price_or_usage_missing"
    return amount, measurement


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
        input_rate, output_rate, cached_rate = budget_rates(endpoint, time.time())
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
                    status_code=402,
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
        return BudgetReservation(key, request_id, amount, float(input_rate),
                                 float(output_rate), float(cached_rate) if cached_rate is not None else None)

    async def commit(self, reservation: BudgetReservation | None) -> None:
        await self._finish(reservation, commit=True)

    async def settle(self, reservation: BudgetReservation | None, usage: dict | None) -> None:
        if reservation is not None:
            amount, measurement = usage_cost(reservation, usage)
            await self._finish(reservation, commit=True, amount=amount, measurement=measurement)

    async def release(self, reservation: BudgetReservation | None) -> None:
        await self._finish(reservation, commit=False)

    async def _finish(
        self,
        reservation: BudgetReservation | None,
        *,
        commit: bool,
        amount: float | None = None,
        measurement: str = "estimated_usage_missing",
    ) -> None:
        if reservation is None:
            return
        lock_key = f"{reservation.key}:lock"
        lock_token = uuid4().hex
        deadline = time.monotonic() + 2
        while not await self.store.acquire_lock(lock_key, lock_token, 5):
            if time.monotonic() >= deadline:
                raise RouterError("cloud budget settlement is busy", status_code=503,
                                  code="cloud_budget_busy")
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
                charge = amount if amount is not None else float(item.get("amount_usd", reservation.amount_usd))
                value["spent_usd"] = float(value.get("spent_usd", 0)) + charge
                bucket = "measured_spent_usd" if measurement == "measured" else "estimated_spent_usd"
                value[bucket] = float(value.get(bucket, 0)) + charge
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
            amount = float(item.get("amount_usd", 0))
            value["spent_usd"] = float(value.get("spent_usd", 0)) + amount
            value["estimated_spent_usd"] = float(value.get("estimated_spent_usd", 0)) + amount
