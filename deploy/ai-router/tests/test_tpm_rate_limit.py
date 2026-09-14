import asyncio

from ai_router.api import _rate_limit_error
from ai_router.scheduler import ClientLimiter
from ai_router.store import InMemoryStateStore


def run(value):
    return asyncio.run(value)


def test_consume_window_rejects_without_consuming():
    async def scenario():
        store = InMemoryStateStore()
        assert await store.consume_window("k", 1, 60, 2) == (True, 1)
        assert await store.consume_window("k", 1, 60, 2) == (True, 2)
        assert await store.consume_window("k", 1, 60, 2) == (False, 2)
        assert await store.consume_window("k", 1, 60, 2) == (False, 2)

    run(scenario())


def test_rate_limit_rejection_does_not_consume_tpm_window(monkeypatch):
    monkeypatch.setattr("ai_router.scheduler.time.time", lambda: 1800000000.0)

    async def scenario():
        limiter = ClientLimiter(InMemoryStateStore())
        assert await limiter.check_rate_limits(
            "client", prompt_tokens=80, rpm_limit=10, tpm_limit=100
        ) == (True, None)
        assert await limiter.check_rate_limits(
            "client", prompt_tokens=80, rpm_limit=10, tpm_limit=100
        ) == (False, "tpm_limit_exceeded")
        assert await limiter.check_rate_limits(
            "client", prompt_tokens=20, rpm_limit=10, tpm_limit=100
        ) == (True, None)

    run(scenario())


def test_oversized_request_uses_dedicated_code_without_consuming_window(monkeypatch):
    monkeypatch.setattr("ai_router.scheduler.time.time", lambda: 1800000000.0)

    async def scenario():
        limiter = ClientLimiter(InMemoryStateStore())
        assert await limiter.check_rate_limits(
            "client", prompt_tokens=101, rpm_limit=10, tpm_limit=100
        ) == (False, "request_exceeds_tpm_limit")
        assert await limiter.check_rate_limits(
            "client", prompt_tokens=100, rpm_limit=10, tpm_limit=100
        ) == (True, None)

    run(scenario())


def test_optional_recall_rejection_does_not_consume_tpm_window(monkeypatch):
    monkeypatch.setattr("ai_router.scheduler.time.time", lambda: 1800000000.0)

    async def scenario():
        limiter = ClientLimiter(InMemoryStateStore())
        assert await limiter.check_additional_tokens("client", 80, 100)
        assert not await limiter.check_additional_tokens("client", 40, 100)
        assert await limiter.check_additional_tokens("client", 20, 100)

    run(scenario())


def test_rate_limit_error_maps_oversized_request_to_413():
    oversized = _rate_limit_error(
        "request_exceeds_tpm_limit",
        prompt_tokens=101,
        tpm_limit=100,
    )
    assert oversized.status_code == 413
    assert oversized.code == "request_exceeds_tpm_limit"
    assert oversized.details == {"prompt_tokens": 101, "tpm_limit": 100}

    throttled = _rate_limit_error(
        "tpm_limit_exceeded",
        prompt_tokens=80,
        tpm_limit=100,
    )
    assert throttled.status_code == 429
    assert throttled.code == "tpm_limit_exceeded"
