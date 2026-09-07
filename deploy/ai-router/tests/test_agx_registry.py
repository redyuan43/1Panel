from __future__ import annotations

from pathlib import Path

from ai_router.config import Registry
from ai_router.types import RequestCapabilities


def endpoint():
    registry = Registry(Path(__file__).resolve().parents[1] / "config" / "registry.yaml")
    value = registry.by_id("agx-qwen36-cerebellum-256k")
    assert value is not None
    return value


def test_agx_uses_discovered_artifact_and_single_slot():
    value = endpoint()
    assert value.provider_model == "Cerebellum-v1-Q3_K_M.gguf"
    assert value.api_base == "http://agx.taild500c8.ts.net:8080/v1"
    assert value.node == "agx"
    assert value.backend_type == "llama_cpp"
    assert value.configured_context_tokens == 262144
    assert value.safe_context_tokens == value.configured_context_tokens
    assert value.max_concurrency == 1
    assert not value.enabled
    assert value.modalities == ("text", "image")
    assert (
        value.metadata["lifecycle_status"]
        == "replaced-by-qwen36-shared-fleet-2026-09-06"
    )
    assert value.metadata["vision_status"] == "user-enabled-live-validation-pending"
    assert value.metadata["vision_context_status"] == "unverified"
    assert not value.cloud
    assert not value.quality
    assert value.metadata["upstream_read_timeout_seconds"] == 1800


def test_agx_does_not_advertise_failed_json_object_capability():
    capabilities = endpoint().capabilities
    assert capabilities.supports(
        RequestCapabilities(protocol="chat", structured_output="json_schema")
    )
    assert not capabilities.supports(
        RequestCapabilities(protocol="chat", structured_output="json_object")
    )
    assert capabilities.supports(
        RequestCapabilities(protocol="responses", streaming=True)
    )
    assert capabilities.supports(
        RequestCapabilities(protocol="chat", tools=True, parallel_tools=True)
    )
