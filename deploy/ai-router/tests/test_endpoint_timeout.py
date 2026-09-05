from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from ai_router.api import _send_upstream
from ai_router.config import Registry, Settings, endpoint_from_dict
from ai_router.identity import IdentityProfile
from ai_router.runtime import build_runtime
from ai_router.store import InMemoryStateStore
from tests.test_core import SimpleTokenCounter


def endpoint():
    registry = Registry(Path(__file__).resolve().parents[1] / "config" / "registry.yaml")
    result = registry.by_id("agx-qwen36-cerebellum-256k")
    assert result is not None
    return result


@pytest.mark.parametrize("seconds", [0, -1, 3601, float("inf"), float("nan")])
def test_registry_rejects_unbounded_read_timeout(seconds):
    value = asdict(endpoint())
    value["metadata"]["upstream_read_timeout_seconds"] = seconds
    with pytest.raises(ValueError, match="upstream_read_timeout_seconds"):
        endpoint_from_dict(value)


@pytest.mark.parametrize("override, expected", [(None, 900), (1800, 1800)])
def test_read_timeout_is_per_endpoint_without_mutating_shared_client(override, expected):
    async def check():
        observed = []

        async def upstream(request):
            observed.append(request.extensions["timeout"])
            return httpx.Response(200, json={"ok": True})

        value = endpoint()
        metadata = dict(value.metadata)
        metadata.pop("upstream_read_timeout_seconds", None)
        if override is not None:
            metadata["upstream_read_timeout_seconds"] = override
        value = replace(value, metadata=metadata)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=900, write=6, pool=7),
            transport=httpx.MockTransport(upstream),
        ) as client:
            runtime = SimpleNamespace(internal_client=client, internal_api_key="")
            decision = SimpleNamespace(
                endpoint=value, native_or_adapter="native", upstream_api_base=value.api_base,
            )
            response = await _send_upstream(
                runtime,
                Request({"type": "http", "headers": []}),
                {"model": value.public_model, "messages": [{"role": "user", "content": "hello"}]},
                api_kind="chat",
                decision=decision,
                identity=IdentityProfile.from_settings({"enabled": False}),
            )
            await response.aclose()
            assert client.timeout.read == 900
            assert observed == [{"connect": 5, "read": expected, "write": 6, "pool": 7}]

    asyncio.run(check())


@pytest.mark.parametrize("seconds", [900, 3600])
def test_client_parallel_lease_matches_task_lease(tmp_path, monkeypatch, seconds):
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-only")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    settings = Settings(runtime_path=tmp_path / "settings.yaml")
    settings.write_runtime({"queue": {"lock_ttl_seconds": seconds}})
    runtime = build_runtime(
        settings=settings, store=InMemoryStateStore(), token_counter=SimpleTokenCounter(),
    )
    try:
        assert runtime.limiter.request_ttl_seconds == runtime.scheduler.lock_ttl_seconds == seconds
    finally:
        asyncio.run(runtime.close())
