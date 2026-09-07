from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ai_router.api import _model_descriptor
from ai_router.config import Registry, Settings
from ai_router.errors import RouteDirectiveIncompatibleError
from ai_router.policy import RoutingPolicy
from ai_router.types import (
    EndpointStatus,
    Evaluation,
    RequestCapabilities,
)


ROOT = Path(__file__).resolve().parents[1]


class StaticHealth:
    def __init__(self, status=None):
        self.status = status

    async def statuses(self, endpoints):
        assert self.status is not None
        return {item.id: self.status for item in endpoints}

    async def in_cooldown(self, _identifier):
        return False

    async def in_capability_cooldown(self, _identifier, _capability):
        return False


def _registry_with_alias(tmp_path: Path) -> Registry:
    value = yaml.safe_load(
        (ROOT / "config/registry.yaml").read_text(encoding="utf-8")
    )
    value["model_aliases"] = {
        "siyuan/agent-fast": {
            "endpoint_ids": ["ai-qwen38-27b"],
            "deployment_profile_ids": ["v100-tp2-qwen38-196k"],
            "max_input_tokens": 49152,
            "max_output_tokens": 8192,
        }
    }
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return Registry(path)


def test_registry_resolves_semantic_model_without_cloning_endpoint(tmp_path):
    registry = _registry_with_alias(tmp_path)
    endpoints = registry.by_public_model("siyuan/agent-fast")
    assert len(endpoints) == 1
    assert endpoints[0].id == "ai-qwen38-27b"
    assert endpoints[0].public_model == "siyuan/agent-fast"
    assert endpoints[0].provider_model == "siyuan/qwen38-v100-196k"
    assert endpoints[0].metadata["allowed_deployment_profile_ids"] == [
        "v100-tp2-qwen38-196k"
    ]
    assert endpoints[0].metadata["model_alias_max_input_tokens"] == 49152
    assert endpoints[0].metadata["model_alias_max_output_tokens"] == 8192
    assert "siyuan/agent-fast" in registry.enabled_public_models()


def test_semantic_model_descriptor_applies_alias_token_limits(tmp_path):
    registry = _registry_with_alias(tmp_path)
    endpoint = registry.by_public_model("siyuan/agent-fast")[0]
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
        eligible_context_tokens=endpoint.safe_context_tokens,
        detail={"effective_modalities": ["text"]},
    )
    runtime = SimpleNamespace(
        registry=registry,
        settings=Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "missing-settings.yaml",
        ),
        health=StaticHealth(status),
    )

    descriptor = asyncio.run(
        _model_descriptor(runtime, "siyuan/agent-fast")
    )

    assert descriptor["maxInputTokens"] == 49152
    assert descriptor["maxOutputTokens"] == 8192
    assert descriptor["contextWindow"] == 57344


def test_semantic_model_filters_physical_deployment_profile(tmp_path):
    registry = _registry_with_alias(tmp_path)
    endpoint = registry.by_public_model("siyuan/agent-fast")[0]
    policy = RoutingPolicy(
        registry,
        Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "missing-settings.yaml",
        ),
        StaticHealth(),
    )
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=1,
        detail={
            "workers": [
                {
                    "worker_id": "tp2-1",
                    "api_base": "http://127.0.0.1:18001/v1",
                    "profile_id": "v100-tp2-qwen38-196k",
                    "tier": "local-general",
                    "priority": 0,
                    "gpu_ids": ["0"],
                    "gpu_uuids": ["GPU-P40"],
                    "names": ["Tesla P40"],
                    "port": 18001,
                    "context_size": 65536,
                    "safe_context_tokens": 65536,
                    "cache_type_k": "q8_0",
                    "cache_type_v": "q8_0",
                    "modalities": ["text", "image"],
                    "vision_status": "validated",
                    "max_images": 1,
                    "runtime_fingerprint": "p40",
                    "ready": True,
                    "state": "available",
                    "config_drift": [],
                    "short_request_rank": 0,
                },
                {
                    "worker_id": "v100-1",
                    "api_base": "http://127.0.0.1:18002/v1",
                    "profile_id": "legacy-v10032-qwen38-196k",
                    "tier": "local-general",
                    "priority": 0,
                    "gpu_ids": ["1"],
                    "gpu_uuids": ["GPU-V100"],
                    "names": ["Tesla V100"],
                    "port": 18002,
                    "context_size": 196608,
                    "safe_context_tokens": 196608,
                    "cache_type_k": "f16",
                    "cache_type_v": "f16",
                    "modalities": ["text", "image"],
                    "vision_status": "validated",
                    "max_images": 1,
                    "runtime_fingerprint": "v100",
                    "ready": True,
                    "state": "available",
                    "config_drift": [],
                    "short_request_rank": 10,
                },
            ]
        },
    )
    deployments = asyncio.run(
        policy._eligible_physical_deployments(
            endpoint,
            status,
            required_context=32000,
            modalities={"text"},
            image_count=0,
            excluded_deployment_ids=set(),
            require_available=True,
        )
    )
    assert [item.worker_id for item in deployments] == ["tp2-1"]


def test_semantic_model_enforces_separate_input_and_output_budgets(tmp_path):
    registry = _registry_with_alias(tmp_path)
    endpoint = registry.by_public_model("siyuan/agent-fast")[0]
    policy = RoutingPolicy(
        registry,
        Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "missing-settings.yaml",
        ),
        StaticHealth(),
    )
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
    )
    common = {
        "endpoint": endpoint,
        "status": status,
        "evaluation": Evaluation(
            task="general",
            required_tier=None,
            confidence=1.0,
            reason="test",
        ),
        "modalities": {"text"},
        "required_capabilities": RequestCapabilities(protocol="chat"),
        "conversation": None,
        "auto": False,
        "excluded_deployment_ids": set(),
        "image_count": 0,
    }
    input_reason = asyncio.run(
        policy._ineligible_reason(
            prompt_tokens=49153,
            output_reserve_tokens=1,
            **common,
        )
    )
    output_reason = asyncio.run(
        policy._ineligible_reason(
            prompt_tokens=1,
            output_reserve_tokens=8193,
            **common,
        )
    )
    assert input_reason == "context"
    assert output_reason == "output_context"


def test_route_directive_cannot_bypass_semantic_profile_budget(tmp_path):
    registry = _registry_with_alias(tmp_path)
    endpoint = registry.by_id("ai-qwen38-27b")
    assert endpoint is not None
    status = EndpointStatus(
        endpoint_id=endpoint.id,
        healthy=True,
        checked_at=time.time(),
    )
    policy = RoutingPolicy(
        registry,
        Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "missing-settings.yaml",
        ),
        StaticHealth(status),
    )
    with pytest.raises(RouteDirectiveIncompatibleError):
        asyncio.run(
            policy.choose(
                requested_model="siyuan/agent-fast",
                evaluation=Evaluation(
                    task="general",
                    required_tier=None,
                    confidence=1.0,
                    reason="directive-test",
                    directive_id="force-ai",
                    required_endpoint_id="ai-qwen38-27b",
                ),
                prompt_tokens=49153,
                output_reserve_tokens=1,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )


def test_route_directive_cannot_escape_explicit_model_scope(tmp_path):
    registry = Registry(ROOT / "config/registry.yaml")
    endpoint = registry.by_id("cloud-deepseek-v4-pro")
    assert endpoint is not None
    policy = RoutingPolicy(
        registry,
        Settings(
            defaults_path=ROOT / "config/defaults.yaml",
            runtime_path=tmp_path / "missing-settings.yaml",
        ),
        StaticHealth(
            EndpointStatus(
                endpoint_id=endpoint.id,
                healthy=True,
                checked_at=time.time(),
            )
        ),
    )

    with pytest.raises(RouteDirectiveIncompatibleError):
        asyncio.run(
            policy.choose(
                requested_model="zhipu/glm-5.3-flash",
                evaluation=Evaluation(
                    task="general",
                    required_tier=None,
                    confidence=1.0,
                    reason="directive-test",
                    directive_id="qinglan",
                    required_endpoint_id=endpoint.id,
                ),
                prompt_tokens=100,
                output_reserve_tokens=100,
                modalities={"text"},
                has_tools=False,
                conversation=None,
            )
        )
