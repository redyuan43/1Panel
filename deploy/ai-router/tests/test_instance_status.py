import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from ai_router.instance_status import classify_instances
from ai_router.runtime import RouterRuntime


@pytest.mark.parametrize("status,updated,fingerprint,expected", [
    ("running", 100, "current", ("running", "match")),
    ("running", 100, "old", ("running", "mismatch")),
    ("draining", 100, "old", ("draining", "mismatch")),
    ("stopped", 100, "old", ("stopped", "not_compared")),
    ("running", 39, "old", ("stale", "not_compared")),
    ("running", 40, "old", ("running", "mismatch")),
    ("running", None, "old", ("stale", "not_compared")),
    ("running", float("nan"), "old", ("stale", "not_compared")),
    ("running", 101, "old", ("stale", "not_compared")),
    ("running", 100, None, ("running", "unknown")),
    ("starting", 100, "old", ("unknown", "not_compared")),
])
def test_persistent_instance_evidence(status, updated, fingerprint, expected):
    record = {"status":status, "updated_at":updated, "registry_fingerprint":fingerprint}
    classified = classify_instances([record], "current", now=100)[0]
    assert (classified["liveness"], classified["registry_comparison"]) == expected
    assert "liveness" not in record


def test_idle_heartbeat_recovers_after_store_failure_and_close_stays_stopped(monkeypatch):
    async def case():
        monkeypatch.setattr("ai_router.runtime.INSTANCE_HEARTBEAT_SECONDS", .001)
        published = []
        ready = asyncio.Event()
        attempts = 0
        async def publish(status):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("transient store failure")
            published.append(status)
            ready.set()
        runtime = SimpleNamespace(draining=False, _publish_instance_state=publish,
                  health=SimpleNamespace(), evaluator=SimpleNamespace(), compactor=SimpleNamespace(),
                  internal_client=None, prefix_prewarmer=None, privacy_reviewer=None,
                  track_instance=True, store=SimpleNamespace(close=AsyncMock()))
        runtime._instance_heartbeat = asyncio.create_task(RouterRuntime._heartbeat_instances(runtime))
        await asyncio.wait_for(ready.wait(), 1)
        assert published[-1] == "running"
        ready.clear()
        runtime.draining = True
        await asyncio.wait_for(ready.wait(), 1)
        assert published[-1] == "draining"
        await RouterRuntime.close(runtime)
        assert runtime._instance_heartbeat is None
        assert published[-1] == "stopped"
        count = len(published)
        await asyncio.sleep(.01)
        assert len(published) == count
        runtime.store.close.assert_awaited_once()
    asyncio.run(case())
