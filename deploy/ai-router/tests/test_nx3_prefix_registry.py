import asyncio
import os
from pathlib import Path

import httpx

from ai_router.bootstrap_litellm import build_config
from ai_router.config import Registry
from ai_router.health import HealthMonitor
from ai_router.store import InMemoryStateStore


ROOT = Path(__file__).resolve().parents[1]


def test_nx3_prefix_endpoint_is_fail_closed_until_router_validation() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("nx3-qwen36-prefix-lab")

    assert endpoint is not None
    assert endpoint.public_model == "prefix-lab/nx3-qwen36"
    assert endpoint.modalities == ("text",)
    assert endpoint.safe_context_tokens == 57344
    assert endpoint.max_concurrency == 1
    assert endpoint.backend_type == "llama_cpp"
    assert endpoint.enabled is False
    assert endpoint.auto_candidate is False
    assert endpoint.capabilities.responses == "none"
    assert endpoint.capabilities.tools == "none"
    assert (
        endpoint.capabilities.validation_status
        == "isolated-router-chat-streaming-prefix-cache-validated"
    )

    enabled_ids = {
        item["model_name"] for item in build_config(registry)["model_list"]
    }
    assert endpoint.id not in enabled_ids


def test_nx3_llama_health_probe_uses_backend_key(monkeypatch) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    endpoint = registry.by_id("nx3-qwen36-prefix-lab")
    assert endpoint is not None
    monkeypatch.setenv(endpoint.backend_api_key_env, "nx3-secret")
    requests = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer nx3-secret"
        if request.url.path == "/slots":
            return httpx.Response(
                200,
                json=[{"id": 0, "n_ctx": 57344, "is_processing": False}],
            )
        return httpx.Response(200, json={"status": "ok"})

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        monitor = HealthMonitor(InMemoryStateStore(), client=client)
        try:
            return await monitor.status(endpoint, force_refresh=True)
        finally:
            await client.aclose()

    status = asyncio.run(scenario())
    assert status.healthy is True
    assert len(requests) == 2
    assert os.environ[endpoint.backend_api_key_env] == "nx3-secret"
