from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ai_router.api import (
    _identity_model_descriptors,
    _resolve_requested_model,
)
from ai_router.config import Registry, Settings, validate_settings
from ai_router.errors import AuthenticationError
from ai_router.identity import (
    IdentityProfile,
    IdentityStreamSanitizer,
    sanitize_payload,
)
from ai_router.history import SSEAccumulator
from ai_router.types import EndpointStatus


ROOT = Path(__file__).resolve().parents[1]


def profile() -> IdentityProfile:
    return IdentityProfile(
        enabled=True,
        public_model_id="siyuan/auto",
        display_name_zh="思源",
        display_name_en="SIYUAN",
        provider_name="SIYUAN",
        description="由思源智能路由服务提供的统一 AI 助手。",
        identity_response=(
            "我是思源（SIYUAN），由思源智能路由服务提供的统一 AI 助手。"
        ),
    )


def test_default_identity_is_branded_but_disabled(tmp_path: Path) -> None:
    settings = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    value = settings.section("identity")
    assert value["enabled"] is False
    assert value["public_model_id"] == "siyuan/auto"
    assert value["display_name_zh"] == "思源"
    assert value["display_name_en"] == "SIYUAN"


def test_identity_prompt_is_stable_and_injected_after_client_instructions() -> None:
    identity = profile()
    body = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": "Client policy."},
            {"role": "developer", "content": "Client developer policy."},
            {"role": "user", "content": "你是什么模型？"},
        ],
    }
    first = identity.inject(body, "chat")
    second = identity.inject(body, "chat")

    assert first == second
    assert first["messages"][2]["role"] == "system"
    assert "思源（SIYUAN）" in first["messages"][2]["content"]
    assert "reply with exactly" in first["messages"][2]["content"]
    assert body["messages"][2]["role"] == "user"


def test_responses_identity_extends_instructions_without_mutating_input() -> None:
    identity = profile()
    body = {
        "model": "auto",
        "instructions": "Return concise answers.",
        "input": "Who built you?",
    }
    result = identity.inject(body, "responses")

    assert result["instructions"].startswith("Return concise answers.")
    assert "siyuan/auto" in result["instructions"]
    assert body["instructions"] == "Return concise answers."


def test_payload_sanitizer_masks_protocol_and_internal_ids() -> None:
    identity = profile()
    internal_model = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
    payload = json.dumps(
        {
            "model": internal_model,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            f"I run on {internal_model} through "
                            "edge-qwen38-flash."
                        ),
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": json.dumps(
                                        {"model": internal_model}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
        }
    ).encode()

    result, count = sanitize_payload(
        payload,
        identity,
        (internal_model, "edge-qwen38-flash"),
    )
    value = json.loads(result)
    assert value["model"] == "siyuan/auto"
    assert value["choices"][0]["message"]["content"] == (
        "I run on 思源（SIYUAN） through 思源（SIYUAN）."
    )
    arguments = value["choices"][0]["message"]["tool_calls"][0][
        "function"
    ]["arguments"]
    assert internal_model in arguments
    assert count >= 3


def test_stream_sanitizer_masks_identifier_split_across_sse_events() -> None:
    identity = profile()
    internal_model = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
    sanitizer = IdentityStreamSanitizer(
        "chat",
        identity,
        (internal_model,),
    )
    first = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": internal_model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": "RadixArk/Qwen3.8-Flash-"},
                "finish_reason": None,
            }
        ],
    }
    second = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": internal_model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": "Next-NVFP4"},
                "finish_reason": None,
            }
        ],
    }
    chunks = [
        f"data: {json.dumps(first)}\n\n".encode(),
        f"data: {json.dumps(second)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]
    output = b"".join(
        item
        for chunk in chunks
        for item in sanitizer.feed(chunk)
    )
    output += b"".join(sanitizer.finish())
    text = output.decode()

    assert internal_model not in text
    assert "siyuan/auto" in text
    assert "思源（SIYUAN）" in text
    assert "data: [DONE]" in text
    accumulator = SSEAccumulator("chat")
    accumulator.feed(output)
    accumulator.finish()
    assert accumulator.assistant_message()["content"] == "思源（SIYUAN）"


def test_public_alias_preserves_single_model_legacy_permissions() -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")

    class Runtime:
        pass

    runtime = Runtime()
    runtime.registry = registry
    assert _resolve_requested_model(
        runtime,
        ("auto",),
        "siyuan/auto",
        profile(),
    ) == "auto"
    assert _resolve_requested_model(
        runtime,
        ("huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF",),
        "siyuan/auto",
        profile(),
    ) == "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF"
    with pytest.raises(AuthenticationError):
        _resolve_requested_model(
            runtime,
            (
                "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF",
                "zhipu/glm-5.3-flash",
            ),
            "siyuan/auto",
            profile(),
        )


def test_public_alias_descriptor_uses_resolved_target_limits(
    tmp_path: Path,
) -> None:
    registry = Registry(ROOT / "config" / "registry.yaml")
    settings = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )

    class Health:
        async def statuses(self, endpoints):
            return {
                endpoint.id: EndpointStatus(
                    endpoint_id=endpoint.id,
                    healthy=True,
                    checked_at=time.time(),
                    eligible_context_tokens=endpoint.safe_context_tokens,
                    detail={},
                )
                for endpoint in endpoints
            }

    class Runtime:
        pass

    runtime = Runtime()
    runtime.registry = registry
    runtime.settings = settings
    runtime.health = Health()

    broad = asyncio.run(
        _identity_model_descriptors(
            runtime,
            ("auto",),
            profile(),
        )
    )
    descriptors = {item["id"]: item for item in broad}
    public = descriptors["siyuan/auto"]
    assert public["maxInputTokens"] == 196608
    assert public["maxOutputTokens"] == 65536
    assert public["contextWindow"] == 262144

    single = asyncio.run(
        _identity_model_descriptors(
            runtime,
            ("zhipu/glm-5.3-flash",),
            profile(),
        )
    )
    assert [item["id"] for item in single] == ["siyuan/auto"]
    assert single[0]["contextWindow"] == 262144

    unavailable_alias = asyncio.run(
        _identity_model_descriptors(
            runtime,
            (
                "huihui/Qwen3.8-27B-abliterated-NVFP4-GGUF",
                "zhipu/glm-5.3-flash",
            ),
            profile(),
        )
    )
    assert unavailable_alias == []


def test_identity_cannot_be_enabled_with_incomplete_profile(
    tmp_path: Path,
) -> None:
    settings = Settings(
        defaults_path=ROOT / "config" / "defaults.yaml",
        runtime_path=tmp_path / "settings.yaml",
    )
    value = settings.value
    value["identity"]["enabled"] = True
    value["identity"]["identity_response"] = ""
    with pytest.raises(ValueError, match="identity fields"):
        validate_settings(value)
