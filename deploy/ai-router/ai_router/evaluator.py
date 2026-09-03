from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .errors import RouterError
from .token_counter import redact_media_payloads, request_modalities
from .types import Evaluation, ModelCallTarget


ALLOWED_TASKS = {"general", "code", "batch", "long-context", "home-automation", "asr"}
ALLOWED_ROUTE_PROFILES = {
    "general",
    "agent_text",
    "code",
    "multimodal",
}
ALLOWED_COMPLEXITIES = {"standard", "complex"}
ROUTE_TASK_ALIASES = {"agent-text": "agent_text"}
ALLOWED_TIERS = {
    "edge-small",
    "local-general",
    "local-large",
    "cloud-frontier",
    "subscription-frontier",
}


class TaskEvaluator:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        internal_base_url: str,
        internal_api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.internal_base_url = internal_base_url.rstrip("/")
        self.internal_api_key = internal_api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0))

    async def evaluate(
        self,
        body: dict[str, Any],
        *,
        headers: dict[str, str],
        api_kind: str,
        prompt_tokens: int,
        current_task: str | None,
        current_route_profile: str | None = None,
        current_complexity: str | None = None,
        is_new_conversation: bool,
        before_model_call: (
            Callable[[], Awaitable[ModelCallTarget | None]] | None
        ) = None,
        after_model_call: Callable[[], Awaitable[None]] | None = None,
    ) -> Evaluation:
        modalities = request_modalities(body, api_kind)
        required_tier = headers.get("x-1panel-route-tier", "").strip().lower()
        if required_tier and required_tier not in ALLOWED_TIERS:
            raise RouterError(
                f"unsupported routing tier: {required_tier}",
                status_code=400,
                code="invalid_route_tier",
            )
        required_tier = required_tier or None
        explicit = headers.get("x-1panel-route-task", "").strip().lower()
        if explicit:
            explicit = ROUTE_TASK_ALIASES.get(explicit, explicit)
            if explicit not in {
                *ALLOWED_TASKS,
                "agent_text",
                "multimodal",
            }:
                raise RouterError(
                    f"unsupported routing task: {explicit}",
                    status_code=400,
                    code="invalid_route_task",
                )
            task = (
                "general"
                if explicit in {"agent_text", "multimodal"}
                else explicit
            )
            profile = (
                explicit
                if explicit in ALLOWED_ROUTE_PROFILES
                else _profile_for_task(task, modalities)
            )
            return _evaluation(
                task,
                required_tier,
                1.0,
                "explicit_task",
                profile,
                "standard",
                {"explicit": explicit, "modalities": sorted(modalities)},
            )

        if api_kind == "audio":
            return _evaluation(
                "asr",
                required_tier,
                1.0,
                "audio_endpoint",
                "general",
                "standard",
                {"protocol": api_kind},
            )
        complex_code = self._complex_code_preference(
            body,
            required_tier,
            modalities,
        )
        if complex_code:
            return complex_code
        tool_task = self._task_from_tools(body)
        if tool_task:
            result = _evaluation(
                tool_task,
                required_tier,
                1.0,
                "tool_mapping",
                _profile_for_task(tool_task, modalities),
                "standard",
                {
                    "tool_task": tool_task,
                    "modalities": sorted(modalities),
                },
            )
            if (
                tool_task == "code"
                and bool(
                    self.settings.get(
                        "prefer_frontier_for_code_tools",
                        True,
                    )
                )
            ):
                result.preferred_tier = "subscription-frontier"
            return result
        deterministic = self._deterministic_profile(
            body,
            required_tier,
            modalities,
        )
        if deterministic:
            if prompt_tokens > int(
                self.settings.get(
                    "long_context_threshold_tokens",
                    65536,
                )
            ):
                deterministic.task = "long-context"
                deterministic.reason = (
                    f"{deterministic.reason}+context_threshold"
                )
                deterministic.evidence["prompt_tokens"] = prompt_tokens
            return deterministic
        if prompt_tokens > int(self.settings.get("long_context_threshold_tokens", 65536)):
            return _evaluation(
                "long-context",
                required_tier,
                1.0,
                "context_threshold",
                current_route_profile or "general",
                current_complexity or "standard",
                {"prompt_tokens": prompt_tokens},
            )
        if _has_structured_output(body):
            return _evaluation(
                "batch",
                required_tier,
                0.95,
                "structured_output",
                "general",
                "standard",
                {"structured_output": True},
            )

        enabled = bool(self.settings.get("enabled", False))
        reevaluate = headers.get("x-1panel-route-reevaluate", "").strip().lower()
        task_change_requested = reevaluate in {"1", "true", "yes"}
        should_call = enabled and (
            is_new_conversation
            or (
                bool(self.settings.get("evaluate_task_changes", True))
                and task_change_requested
            )
        )
        if not should_call:
            return _evaluation(
                current_task or "general",
                required_tier,
                1.0,
                "conversation_task",
                current_route_profile or "general",
                current_complexity or "standard",
                {"conversation_profile_preserved": True},
            )
        if not str(self.settings.get("model_id", "")).strip():
            return _evaluation(
                current_task or "general",
                required_tier,
                0.0,
                "evaluator_not_configured",
                current_route_profile or "general",
                current_complexity or "standard",
                {"fallback": "conversation_or_general"},
            )
        acquired = False
        target = None
        try:
            try:
                if before_model_call:
                    target = await before_model_call()
                    acquired = True
                result = await self._call_model(body, target=target)
            except Exception:
                return _evaluation(
                    current_task or tool_task or "general",
                    required_tier,
                    0.0,
                    "evaluator_unavailable",
                    current_route_profile or "general",
                    current_complexity or "standard",
                    {"fallback": "conversation_or_general"},
                )
        finally:
            if acquired and after_model_call:
                await after_model_call()
        threshold = float(self.settings.get("confidence_threshold", 0.85))
        if result.confidence < threshold:
            return _evaluation(
                current_task or "general",
                required_tier,
                result.confidence,
                "evaluator_low_confidence",
                current_route_profile or "general",
                current_complexity or "standard",
                {
                    "reported_profile": result.route_profile,
                    "reported_complexity": result.complexity,
                    "confidence_threshold": threshold,
                },
            )
        if required_tier:
            result.required_tier = required_tier
        return result

    def _deterministic_profile(
        self,
        body: dict[str, Any],
        required_tier: str | None,
        modalities: set[str],
    ) -> Evaluation | None:
        text = _request_text(body).lower()
        if not text:
            return None
        code_markers = (
            "code",
            "coding",
            "repository",
            "repo",
            "implementation",
            "patch",
            "bug",
            "programming",
            "代码",
            "编程",
            "仓库",
            "实现",
            "修复",
            "调试",
        )
        agent_markers = (
            "research",
            "investigate",
            "multi-step",
            "multiple steps",
            "compare sources",
            "deep dive",
            "研究",
            "调查",
            "多步骤",
            "多轮执行",
            "对比资料",
        )
        if _matches_any_marker(text, code_markers):
            return _evaluation(
                "code",
                required_tier,
                1.0,
                "code_heuristic",
                _profile_for_task("code", modalities),
                "standard",
                {"matched": "code", "modalities": sorted(modalities)},
            )
        if _matches_any_marker(text, agent_markers):
            return _evaluation(
                "general",
                required_tier,
                0.95,
                "agent_text_heuristic",
                (
                    "multimodal"
                    if "image" in modalities
                    else "agent_text"
                ),
                "standard",
                {
                    "matched": "agent_text",
                    "modalities": sorted(modalities),
                },
            )
        if "image" in modalities:
            return _evaluation(
                "general",
                required_tier,
                1.0,
                "multimodal_input",
                "multimodal",
                "standard",
                {"modalities": sorted(modalities)},
            )
        return None

    def _task_from_tools(self, body: dict[str, Any]) -> str | None:
        mappings = self.settings.get("tool_task_mappings", {})
        if not isinstance(mappings, dict):
            return None
        names = []
        for item in body.get("tools", []) or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function", {})
            if isinstance(function, dict) and function.get("name"):
                names.append(str(function["name"]).lower())
            elif item.get("name"):
                names.append(str(item["name"]).lower())
        for task, prefixes in mappings.items():
            for name in names:
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
                    return str(task)
        return None

    def _complex_code_preference(
        self,
        body: dict[str, Any],
        required_tier: str | None,
        modalities: set[str],
    ) -> Evaluation | None:
        if not bool(self.settings.get("prefer_frontier_for_complex_code", True)):
            return None
        text = _request_text(body).lower()
        if not text:
            return None

        strong_markers = (
            "multi-file",
            "multiple files",
            "multi-repo",
            "multiple repositories",
            "distributed system",
            "security-sensitive",
            "security audit",
            "threat model",
            "race condition",
            "root cause analysis",
            "多文件",
            "跨仓库",
            "分布式系统",
            "安全审计",
            "威胁模型",
            "竞态条件",
            "根因分析",
        )
        signal_groups = (
            ("architecture", "system design", "架构", "系统设计"),
            ("security", "vulnerability", "安全", "漏洞"),
            ("concurrency", "race", "deadlock", "并发", "竞态", "死锁"),
            ("debugging", "root cause", "调试", "根因"),
            ("rollback", "migration", "回滚", "迁移"),
        )
        code_markers = (
            "code",
            "coding",
            "repository",
            "repo",
            "implementation",
            "patch",
            "bug",
            "代码",
            "编程",
            "仓库",
            "实现",
            "修复",
        )
        strong = _matches_any_marker(text, strong_markers)
        signals = sum(
            1
            for group in signal_groups
            if _matches_any_marker(text, group)
        )
        has_code_marker = _matches_any_marker(text, code_markers)
        if not strong and not (signals >= 3 and has_code_marker):
            return None
        result = _evaluation(
            "code",
            required_tier,
            1.0,
            "complex_code_heuristic",
            _profile_for_task("code", modalities),
            "complex",
            {
                "strong_marker": strong,
                "signal_groups": signals,
                "modalities": sorted(modalities),
            },
        )
        result.preferred_tier = "subscription-frontier"
        return result

    async def _call_model(
        self,
        body: dict[str, Any],
        *,
        target: ModelCallTarget | None = None,
    ) -> Evaluation:
        model = (
            target.model
            if target
            else str(self.settings.get("model_id", "")).strip()
        )
        if not model:
            return _evaluation(
                "general",
                None,
                0.0,
                "evaluator_not_configured",
                "general",
                "standard",
            )
        prompt = {
            "task": "Classify the routing requirements for this request.",
            "allowed_tasks": sorted(ALLOWED_TASKS - {"asr"}),
            "allowed_route_profiles": sorted(ALLOWED_ROUTE_PROFILES),
            "allowed_complexities": sorted(ALLOWED_COMPLEXITIES),
            "request": _request_excerpt(body),
            "output_schema": {
                "task": "string",
                "route_profile": "general|agent_text|code|multimodal",
                "complexity": "standard|complex",
                "confidence": "number 0..1",
                "reason": "short string",
            },
        }
        response = await self.client.post(
            (
                f"{target.base_url.rstrip('/')}/chat/completions"
                if target
                else f"{self.internal_base_url}/v1/chat/completions"
            ),
            headers=(
                {"Authorization": f"Bearer {target.api_key}"}
                if target and target.api_key
                else {"Authorization": f"Bearer {self.internal_api_key}"}
            ),
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return only one JSON object. Do not answer the user request.",
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "temperature": 0,
                "max_tokens": 128,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        value = json.loads(content)
        task = str(value.get("task", "general"))
        if task not in ALLOWED_TASKS:
            task = "general"
        route_profile = str(value.get("route_profile", "general"))
        if route_profile not in ALLOWED_ROUTE_PROFILES:
            route_profile = "general"
        complexity = str(value.get("complexity", "standard"))
        if complexity not in ALLOWED_COMPLEXITIES:
            complexity = "standard"
        return _evaluation(
            task,
            None,
            max(0.0, min(1.0, float(value.get("confidence", 0)))),
            str(value.get("reason", "evaluator")),
            route_profile,
            complexity,
            {
                "source": "local_evaluator",
                "reported_task": value.get("task"),
            },
        )


def _profile_for_task(task: str, modalities: set[str]) -> str:
    if "image" in modalities:
        return "multimodal"
    if task == "code":
        return "code"
    return "general"


def _matches_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(_matches_marker(text, marker) for marker in markers)


def _matches_marker(text: str, marker: str) -> bool:
    if not marker.isascii():
        return marker in text
    return bool(
        re.search(
            rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])",
            text,
        )
    )


def _evaluation(
    task: str,
    required_tier: str | None,
    confidence: float,
    reason: str,
    route_profile: str,
    complexity: str,
    evidence: dict[str, Any] | None = None,
) -> Evaluation:
    return Evaluation(
        task=task,
        required_tier=required_tier,
        confidence=confidence,
        reason=reason,
        route_profile=route_profile,
        complexity=complexity,
        evidence=evidence or {},
    )


def _has_structured_output(body: dict[str, Any]) -> bool:
    response_format = body.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}


def _request_excerpt(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages")
    if isinstance(messages, list):
        return redact_media_payloads(
            {
                "messages": messages[-4:],
                "tools": body.get("tools", []),
            }
        )
    value = body.get("input")
    if isinstance(value, list):
        value = value[-4:]
    return redact_media_payloads(
        {
            "input": value,
            "tools": body.get("tools", []),
        }
    )


def _request_text(body: dict[str, Any]) -> str:
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            return _content_text(message.get("content"))
    return _content_text(body.get("input"))


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_content_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(
            _content_text(value.get(key))
            for key in ("text", "content", "input_text")
            if key in value
        )
    return ""
