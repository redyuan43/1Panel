from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import Registry, Settings


SCHEMA_VERSION = 2
GRAPH_VERSION = 3
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}
REVIEW_VERDICTS = {"correct", "incorrect", "needs_review"}

GRAPH_NODES: tuple[dict[str, str], ...] = (
    {
        "id": "task_evaluation",
        "label": "任务画像、模态与 Token",
        "kind": "process",
        "group": "input",
    },
    {
        "id": "explicit_model",
        "label": "用户锁定模型？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "candidate_scope",
        "label": "完整约束资格筛选",
        "mermaid_label": (
            "完整约束资格筛选<br/>"
            "模态 · 协议 · 工具 · 历史 · 健康"
        ),
        "kind": "process",
        "group": "core",
    },
    {
        "id": "context_formula",
        "label": "上下文完整满足？",
        "mermaid_label": (
            "上下文完整满足？<br/>"
            "prompt + 客户端 max_output ≤ safe_context"
        ),
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "conversation_affinity",
        "label": "会话原模型仍合格？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "explicit_selection",
        "label": "锁定指定模型",
        "kind": "process",
        "group": "core",
    },
    {
        "id": "local_sufficiency",
        "label": "本地候选充分？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "provider_priority",
        "label": "legacy_v1 Provider 优先",
        "kind": "process",
        "group": "core",
    },
    {
        "id": "remote_expert_dispatch",
        "label": "云端专家分流",
        "mermaid_label": (
            "云端专家分流<br/>"
            "画像与复杂度决定固定回退顺序"
        ),
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "score_candidates",
        "label": "综合评分并选择模型",
        "mermaid_label": (
            "阶段内排序并选择<br/>"
            "质量 · 负载 · 延迟 · 上下文余量"
        ),
        "kind": "process",
        "group": "core",
    },
    {
        "id": "deployment_binding",
        "label": "绑定物理部署",
        "kind": "process",
        "group": "core",
    },
    {
        "id": "capacity_check",
        "label": "容量可获取？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "history_preflight",
        "label": "历史可无损迁移？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "request_prepare",
        "label": "协议翻译与请求准备",
        "kind": "process",
        "group": "core",
    },
    {
        "id": "retry_decision",
        "label": "排除当前候选并重选？",
        "kind": "decision",
        "group": "core",
    },
    {
        "id": "route_selected",
        "label": "最终路由模型",
        "kind": "terminal-success",
        "group": "result",
    },
    {
        "id": "failed",
        "label": "无可用路由",
        "kind": "terminal-error",
        "group": "result",
    },
)

GRAPH_EDGES: tuple[dict[str, str], ...] = (
    {"id": "e-eval-explicit", "from": "task_evaluation", "to": "explicit_model", "label": ""},
    {"id": "e-explicit-context", "from": "explicit_model", "to": "context_formula", "label": "显式", "branch": "explicit"},
    {"id": "e-auto-context", "from": "explicit_model", "to": "context_formula", "label": "Auto", "branch": "auto"},
    {"id": "e-context-scope", "from": "context_formula", "to": "candidate_scope", "label": "完整保留"},
    {"id": "e-context-failed", "from": "context_formula", "to": "failed", "label": "无模型满足"},
    {"id": "e-scope-affinity", "from": "candidate_scope", "to": "conversation_affinity", "label": "有合格候选"},
    {"id": "e-scope-failed", "from": "candidate_scope", "to": "failed", "label": "无合格候选"},
    {"id": "e-affinity-deploy", "from": "conversation_affinity", "to": "deployment_binding", "label": "命中"},
    {"id": "e-affinity-explicit", "from": "conversation_affinity", "to": "explicit_selection", "label": "显式未命中"},
    {"id": "e-affinity-local", "from": "conversation_affinity", "to": "local_sufficiency", "label": "intelligent_v2"},
    {"id": "e-affinity-legacy", "from": "conversation_affinity", "to": "provider_priority", "label": "legacy_v1"},
    {"id": "e-explicit-deploy", "from": "explicit_selection", "to": "deployment_binding", "label": ""},
    {"id": "e-local-score", "from": "local_sufficiency", "to": "score_candidates", "label": "本地充分"},
    {"id": "e-local-remote", "from": "local_sufficiency", "to": "remote_expert_dispatch", "label": "本地不足或全忙"},
    {"id": "e-legacy-score", "from": "provider_priority", "to": "score_candidates", "label": "旧策略"},
    {"id": "e-remote-score", "from": "remote_expert_dispatch", "to": "score_candidates", "label": "固定顺序"},
    {"id": "e-score-deploy", "from": "score_candidates", "to": "deployment_binding", "label": ""},
    {"id": "e-deploy-capacity", "from": "deployment_binding", "to": "capacity_check", "label": ""},
    {"id": "e-capacity-history", "from": "capacity_check", "to": "history_preflight", "label": "可用"},
    {"id": "e-capacity-retry", "from": "capacity_check", "to": "retry_decision", "label": "忙碌"},
    {"id": "e-history-prepare", "from": "history_preflight", "to": "request_prepare", "label": "原生或无损标准化"},
    {"id": "e-history-retry", "from": "history_preflight", "to": "retry_decision", "label": "不兼容"},
    {"id": "e-prepare-selected", "from": "request_prepare", "to": "route_selected", "label": "准备完成"},
    {"id": "e-retry-scope", "from": "retry_decision", "to": "candidate_scope", "label": "继续"},
    {"id": "e-retry-failed", "from": "retry_decision", "to": "failed", "label": "停止"},
)

CORE_GRAPH_NODE_IDS = frozenset(
    item["id"] for item in GRAPH_NODES
)

_REJECTION_STAGE = {
    "excluded": 0,
    "disabled": 1,
    "auto_disabled": 1,
    "cooldown": 2,
    "unhealthy_or_stale": 2,
    "physical_deployment": 3,
    "modality": 3,
    "deepseek_multimodal_unsupported": 3,
    "capability": 3,
    "task": 4,
    "context": 4,
    "cloud_disabled": 5,
    "cloud_auto_disabled": 5,
    "cloud_model_not_allowed": 5,
    "cloud_provider_not_allowed": 5,
    "cloud_budget": 5,
    "cloud_pricing": 5,
    "tier_downgrade": 5,
    "tier": 5,
}

_GATE_NODES = (
    "candidate_enabled",
    "candidate_health",
    "candidate_capability",
    "candidate_task_context",
    "candidate_policy",
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|password|secret)\b"
        r"\s*[:=]\s*[^\s,;]{6,}"
    ),
    re.compile(r"\b[A-Za-z0-9+/]{160,}={0,2}\b"),
)


def graph_document() -> dict[str, Any]:
    return {
        "graph_version": GRAPH_VERSION,
        "nodes": [dict(item) for item in GRAPH_NODES],
        "edges": [dict(item) for item in GRAPH_EDGES],
        "mermaid": _graph_mermaid(),
    }


def _graph_mermaid() -> str:
    lines = [
        "---",
        "config:",
        "  flowchart:",
        "    curve: basis",
        "    htmlLabels: true",
        "---",
        "flowchart LR",
    ]
    groups = (
        ("input", "请求约束"),
        ("core", "智能路由核心"),
        ("result", "路由结果"),
    )
    for group_id, group_label in groups:
        lines.append(f'  subgraph {group_id}["{group_label}"]')
        lines.append("    direction LR")
        for node in GRAPH_NODES:
            if node.get("group") != group_id:
                continue
            node_id = node["id"]
            label = node.get(
                "mermaid_label",
                node["label"],
            ).replace('"', "'")
            if node["kind"] == "decision":
                lines.append(f'    {node_id}{{"{label}"}}')
            elif node["kind"].startswith("terminal"):
                lines.append(f'    {node_id}(["{label}"])')
            else:
                lines.append(f'    {node_id}["{label}"]')
        lines.append("  end")
    for edge in GRAPH_EDGES:
        label = edge.get("label", "")
        connector = f' -->|"{label}"| ' if label else " --> "
        lines.append(f"  {edge['from']}{connector}{edge['to']}")
    lines.extend(
        (
            "  classDef process fill:#10161d,stroke:#52606d,color:#f3f6f8;",
            "  classDef decision fill:#151c24,stroke:#73808d,color:#f3f6f8;",
            "  classDef success fill:#10291f,stroke:#3fd091,color:#b9f5d8,stroke-width:2px;",
            "  classDef error fill:#30171b,stroke:#ef6a72,color:#ffc2c7,stroke-width:2px;",
            "  class task_evaluation,candidate_scope,explicit_selection,provider_priority,score_candidates,deployment_binding,request_prepare process;",
            "  class explicit_model,context_formula,conversation_affinity,local_sufficiency,remote_expert_dispatch,capacity_check,history_preflight,retry_decision decision;",
            "  class route_selected success;",
            "  class failed error;",
            "  style input fill:#0b1015,stroke:#29313a,color:#8d99a5;",
            "  style core fill:#090c10,stroke:#55c9e8,color:#d9f7ff,stroke-width:2px;",
            "  style result fill:#0b1015,stroke:#29313a,color:#8d99a5;",
        )
    )
    return "\n".join(lines)


def settings_fingerprint(settings: Settings) -> str:
    return _fingerprint(settings.value)


def registry_fingerprint(registry: Registry) -> str:
    return _fingerprint(
        {
            "tier_ranks": registry.tier_ranks,
            "endpoints": [item.to_dict() for item in registry.endpoints],
        }
    )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def request_excerpt(
    body: dict[str, Any],
    api_kind: str,
    *,
    max_chars: int = 800,
) -> dict[str, Any]:
    messages = body.get("messages") if api_kind == "chat" else body.get("input")
    values = messages if isinstance(messages, list) else [messages]
    latest_user = ""
    for item in reversed(values):
        if not isinstance(item, dict):
            continue
        if str(item.get("role", "")).lower() not in {"user", "developer"}:
            continue
        latest_user = _content_text(item.get("content"))
        if latest_user:
            break
    if not latest_user and isinstance(messages, str):
        latest_user = messages
    latest_user = _redact_text(latest_user)
    if len(latest_user) > max_chars:
        latest_user = latest_user[: max_chars - 1] + "…"

    tool_names: list[str] = []
    for item in body.get("tools", []) or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict) and function.get("name"):
            tool_names.append(str(function["name"])[:120])
        elif item.get("name"):
            tool_names.append(str(item["name"])[:120])
        elif item.get("type"):
            tool_names.append(str(item["type"])[:120])
    return {
        "text": latest_user,
        "message_count": len(values),
        "tool_names": tool_names[:50],
        "stream": bool(body.get("stream")),
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).lower()
        if item_type in {"text", "input_text", "output_text"}:
            parts.append(str(item.get("text", "")))
        elif "image" in item_type:
            parts.append("[image]")
        elif "audio" in item_type:
            parts.append("[audio]")
        elif "file" in item_type:
            parts.append("[file]")
    return " ".join(part for part in parts if part)


def _redact_text(value: str) -> str:
    result = re.sub(r"\s+", " ", str(value or "")).strip()
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


class DecisionTrace:
    def __init__(
        self,
        *,
        request_id: str,
        client_id: str,
        key_id: str,
        protocol: str,
        requested_model: str,
        excerpt: dict[str, Any],
        instance_id: str,
        boot_id: str,
        settings_hash: str,
        registry_hash: str,
        client_models: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        now = time.time()
        self.payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "graph_version": GRAPH_VERSION,
            "request_id": request_id,
            "client_id": client_id,
            "key_id": key_id,
            "client_models": list(client_models or ()),
            "conversation_id": None,
            "protocol": protocol,
            "requested_model": requested_model,
            "selected_model": None,
            "endpoint_id": None,
            "deployment_id": None,
            "task": None,
            "status": "running",
            "status_code": None,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "instance_id": instance_id,
            "boot_id": boot_id,
            "settings_fingerprint": settings_hash,
            "registry_fingerprint": registry_hash,
            "excerpt": excerpt,
            "request": {},
            "evaluation": {},
            "attempts": [],
            "error": None,
            "route_selected": False,
        }
        self._last_nodes: dict[int, str] = {}
        self.record(
            1,
            "request_received",
            "passed",
            reason="authenticated",
            evidence={
                "client_id": client_id,
                "protocol": protocol,
                "requested_model": requested_model,
                "client_models": list(client_models or ()),
            },
            path=False,
        )

    @property
    def request_id(self) -> str:
        return str(self.payload["request_id"])

    @property
    def terminal(self) -> bool:
        return self.payload.get("status") in TERMINAL_STATUSES

    def set_request_context(
        self,
        *,
        conversation_id: str | None = None,
        prompt_tokens: int | None = None,
        output_reserve_tokens: int | None = None,
        modalities: set[str] | None = None,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if conversation_id is not None:
            self.payload["conversation_id"] = conversation_id
        request = self.payload["request"]
        if prompt_tokens is not None:
            request["prompt_tokens"] = int(prompt_tokens)
        if output_reserve_tokens is not None:
            request["output_reserve_tokens"] = int(output_reserve_tokens)
            request["requested_output_tokens"] = int(
                output_reserve_tokens
            )
        if (
            prompt_tokens is not None
            and output_reserve_tokens is not None
        ):
            request["required_context_tokens"] = (
                int(prompt_tokens) + int(output_reserve_tokens)
            )
        if modalities is not None:
            request["modalities"] = sorted(modalities)
        if required_capabilities is not None:
            request["required_capabilities"] = list(required_capabilities)
        self._touch()

    def set_evaluation(self, evaluation: Any, *, attempt: int = 1) -> None:
        self.payload["task"] = str(evaluation.task)
        self.payload["route_profile"] = str(
            evaluation.route_profile
        )
        self.payload["complexity"] = str(evaluation.complexity)
        self.payload["evaluation"] = {
            "task": str(evaluation.task),
            "route_profile": str(evaluation.route_profile),
            "complexity": str(evaluation.complexity),
            "confidence": float(evaluation.confidence),
            "reason": str(evaluation.reason),
            "required_tier": evaluation.required_tier,
            "preferred_tier": evaluation.preferred_tier,
            "evidence": dict(evaluation.evidence),
        }
        self.record(
            attempt,
            "task_evaluation",
            "passed",
            reason=str(evaluation.reason),
            evidence=dict(self.payload["evaluation"]),
        )
        mode = (
            "auto"
            if self.payload["requested_model"] == "auto"
            else "explicit"
        )
        self.record(
            attempt,
            "explicit_model",
            "evaluated",
            branch=mode,
            reason=f"{mode}_model",
            evidence={"requested_model": self.payload["requested_model"]},
        )
        request = self.payload.get("request", {})
        prompt_tokens = int(request.get("prompt_tokens", 0))
        output_tokens = int(
            request.get("requested_output_tokens", 0)
        )
        self.record(
            attempt,
            "context_formula",
            "evaluated",
            branch="preserved",
            reason="full_requested_context",
            evidence={
                "prompt_tokens": prompt_tokens,
                "requested_output_tokens": output_tokens,
                "required_context_tokens": (
                    prompt_tokens + output_tokens
                ),
                "formula": (
                    "prompt_tokens + requested_output_tokens"
                ),
            },
        )

    def record(
        self,
        attempt: int,
        node_id: str,
        status: str,
        *,
        branch: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        path: bool | None = None,
    ) -> None:
        if self.terminal:
            return
        if path is None:
            path = node_id in CORE_GRAPH_NODE_IDS
        item = self._attempt(attempt)
        now = time.time()
        step = {
            "sequence": len(item["steps"]) + 1,
            "timestamp": now,
            "node_id": node_id,
            "status": status,
            "branch": branch,
            "reason": reason,
            "evidence": evidence or {},
            "path": path,
        }
        item["steps"].append(step)
        previous = self._last_nodes.get(attempt)
        if path and previous and previous != node_id:
            item["transitions"].append(
                {
                    "sequence": len(item["transitions"]) + 1,
                    "from": previous,
                    "to": node_id,
                    "branch": branch,
                    "status": status,
                }
            )
        if path:
            self._last_nodes[attempt] = node_id
        self._touch(now)

    def record_candidate_round(
        self,
        attempt: int,
        candidates: list[dict[str, Any]],
        *,
        mode: str,
    ) -> bool:
        active = [
            item
            for item in candidates
            if item.get("rejection_reason") != "excluded"
        ]
        eligible = [
            item for item in active if not item.get("rejection_reason")
        ]
        rejection_summary: dict[str, int] = {}
        for item in active:
            reason = item.get("rejection_reason")
            if reason:
                rejection_summary[str(reason)] = (
                    rejection_summary.get(str(reason), 0) + 1
                )
        self.record(
            attempt,
            "candidate_scope",
            "passed" if eligible else "blocked",
            branch=mode,
            reason="candidate_scope",
            evidence={
                "mode": mode,
                "candidate_count": len(active),
                "eligible_count": len(eligible),
                "rejected_count": len(active) - len(eligible),
                "rejection_summary": rejection_summary,
                "candidates": candidates,
            },
        )
        if not active:
            return False

        for index, node_id in enumerate(_GATE_NODES, start=1):
            progressed = [
                item
                for item in active
                if (
                    not item.get("rejection_reason")
                    or _REJECTION_STAGE.get(
                        str(item.get("rejection_reason")),
                        5,
                    )
                    > index
                )
            ]
            rejected_here = [
                item
                for item in active
                if _REJECTION_STAGE.get(
                    str(item.get("rejection_reason")),
                    -1,
                )
                == index
            ]
            status = "passed" if progressed else "blocked"
            self.record(
                attempt,
                node_id,
                status,
                branch="continue" if progressed else "no_candidates",
                reason=(
                    "candidate_gate_passed"
                    if progressed
                    else "candidate_gate_failed"
                ),
                evidence={
                    "surviving_endpoint_ids": [
                        item["endpoint_id"] for item in progressed
                    ],
                    "rejected_here": [
                        {
                            "endpoint_id": item["endpoint_id"],
                            "reason": item.get("rejection_reason"),
                        }
                        for item in rejected_here
                    ],
                },
                path=False,
            )
            if not progressed:
                return False
        return bool(eligible)

    def set_selection(
        self,
        *,
        attempt: int,
        selected_model: str,
        endpoint_id: str,
        deployment_id: str | None,
        task: str,
        reason: str,
        affinity: str,
        strategy_version: str = "legacy_v1",
        route_profile: str = "general",
        complexity: str = "standard",
        context_required: int = 0,
        history_mode: str = "native",
        remote_fallback_position: int | None = None,
    ) -> None:
        self.payload.update(
            {
                "selected_model": selected_model,
                "endpoint_id": endpoint_id,
                "deployment_id": deployment_id,
                "task": task,
                "strategy_version": strategy_version,
                "route_profile": route_profile,
                "complexity": complexity,
                "context_required": int(context_required),
                "history_mode": history_mode,
                "remote_fallback_position": remote_fallback_position,
            }
        )
        self._attempt(attempt)["selection"] = {
            "selected_model": selected_model,
            "endpoint_id": endpoint_id,
            "deployment_id": deployment_id,
            "reason": reason,
            "affinity": affinity,
            "strategy_version": strategy_version,
            "route_profile": route_profile,
            "complexity": complexity,
            "context_required": int(context_required),
            "history_mode": history_mode,
            "remote_fallback_position": remote_fallback_position,
        }
        self._touch()

    def confirm_selection(self, *, attempt: int) -> None:
        selection = self._attempt(attempt).get("selection") or {}
        self.payload["route_selected"] = True
        self.record(
            attempt,
            "route_selected",
            "selected",
            reason=str(selection.get("reason") or "route_selected"),
            evidence={
                "selected_model": self.payload.get("selected_model"),
                "endpoint_id": self.payload.get("endpoint_id"),
                "deployment_id": self.payload.get("deployment_id"),
                "task": self.payload.get("task"),
                "affinity": selection.get("affinity"),
                "strategy_version": selection.get(
                    "strategy_version"
                ),
                "route_profile": selection.get("route_profile"),
                "complexity": selection.get("complexity"),
                "context_required": selection.get(
                    "context_required"
                ),
                "history_mode": selection.get("history_mode"),
                "remote_fallback_position": selection.get(
                    "remote_fallback_position"
                ),
            },
        )

    def finish(
        self,
        *,
        attempt: int,
        status_code: int,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        if self.terminal:
            return
        succeeded = status_code < 400
        self.record(
            attempt,
            "upstream_request",
            "passed" if succeeded else "error",
            reason="upstream_response",
            evidence={"status_code": status_code, **(evidence or {})},
        )
        self.record(
            attempt,
            "completed" if succeeded else "failed",
            "selected" if succeeded else "error",
            branch="success" if succeeded else "final_error",
            reason="request_completed" if succeeded else "request_failed",
            evidence={"status_code": status_code},
        )
        now = time.time()
        self.payload["status"] = "succeeded" if succeeded else "failed"
        self.payload["status_code"] = int(status_code)
        self.payload["completed_at"] = now
        self._touch(now)

    def fail(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        interrupted: bool = False,
        attempt: int | None = None,
    ) -> None:
        if self.terminal:
            return
        attempt_number = attempt or max(
            (int(item["number"]) for item in self.payload["attempts"]),
            default=1,
        )
        self.record(
            attempt_number,
            "failed",
            "error",
            branch="final_error",
            reason=code,
            evidence={
                "status_code": int(status_code),
                "code": code,
                "message": _redact_text(message)[:1000],
            },
            path=not bool(self.payload.get("route_selected")),
        )
        now = time.time()
        self.payload["status"] = (
            "interrupted" if interrupted else "failed"
        )
        self.payload["status_code"] = int(status_code)
        self.payload["completed_at"] = now
        self.payload["error"] = {
            "code": code,
            "message": _redact_text(message)[:1000],
        }
        self._touch(now)

    def _attempt(self, number: int) -> dict[str, Any]:
        for item in self.payload["attempts"]:
            if int(item["number"]) == number:
                return item
        item = {
            "number": number,
            "started_at": time.time(),
            "steps": [],
            "transitions": [],
            "selection": None,
        }
        self.payload["attempts"].append(item)
        return item

    def _touch(self, now: float | None = None) -> None:
        self.payload["updated_at"] = now or time.time()


class RouteTraceStore:
    def __init__(
        self,
        database_path: str | Path,
        *,
        retention_days: int = 30,
    ) -> None:
        self.database_path = Path(database_path)
        self.retention_days = max(1, int(retention_days))
        self.database_path.parent.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )
        try:
            os.chmod(self.database_path.parent, 0o700)
        except OSError:
            pass
        self._initialize()

    async def save(self, trace: DecisionTrace) -> None:
        await asyncio.to_thread(self._save, trace.payload)

    async def get(self, request_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get, request_id)

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        request_mode: str = "auto",
        client_id: str | None = None,
        conversation_id: str | None = None,
        task: str | None = None,
        route_profile: str | None = None,
        selected_model: str | None = None,
        status: str | None = None,
        review_status: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._list,
            limit,
            cursor,
            request_mode,
            client_id,
            conversation_id,
            task,
            route_profile,
            selected_model,
            status,
            review_status,
            search,
        )

    async def add_review(
        self,
        request_id: str,
        *,
        verdict: str,
        expected_task: str | None,
        expected_model: str | None,
        note: str | None,
        reviewer_source: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._add_review,
            request_id,
            verdict,
            expected_task,
            expected_model,
            note,
            reviewer_source,
        )

    async def interrupt_previous_boot(
        self,
        instance_id: str,
        boot_id: str,
    ) -> int:
        return await asyncio.to_thread(
            self._interrupt_previous_boot,
            instance_id,
            boot_id,
        )

    async def cleanup(self) -> int:
        return await asyncio.to_thread(self._cleanup)

    def _initialize(self) -> None:
        with self._connect(initialize=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS route_traces (
                    request_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    instance_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    conversation_id TEXT,
                    protocol TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    selected_model TEXT,
                    endpoint_id TEXT,
                    task TEXT,
                    route_profile TEXT,
                    strategy_version TEXT,
                    history_mode TEXT,
                    status TEXT NOT NULL,
                    status_code INTEGER,
                    graph_version INTEGER NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'unreviewed',
                    excerpt_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS route_traces_started
                    ON route_traces(started_at DESC, request_id DESC);
                CREATE INDEX IF NOT EXISTS route_traces_client
                    ON route_traces(client_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS route_traces_conversation
                    ON route_traces(conversation_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS route_traces_task
                    ON route_traces(task, started_at DESC);
                CREATE INDEX IF NOT EXISTS route_traces_model
                    ON route_traces(selected_model, started_at DESC);
                CREATE INDEX IF NOT EXISTS route_traces_status
                    ON route_traces(status, review_status, started_at DESC);

                CREATE TABLE IF NOT EXISTS route_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    verdict TEXT NOT NULL,
                    expected_task TEXT,
                    expected_model TEXT,
                    note TEXT,
                    reviewer_source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS route_reviews_request
                    ON route_reviews(request_id, id DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(route_traces)"
                ).fetchall()
            }
            for name in (
                "route_profile",
                "strategy_version",
                "history_mode",
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE route_traces ADD COLUMN {name} TEXT"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS route_traces_profile
                ON route_traces(route_profile, started_at DESC)
                """
            )

    def _save(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        excerpt = json.dumps(
            payload.get("excerpt", {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO route_traces (
                    request_id, started_at, updated_at, completed_at,
                    instance_id, boot_id, client_id, conversation_id,
                    protocol, requested_model, selected_model, endpoint_id,
                    task, route_profile, strategy_version, history_mode,
                    status, status_code, graph_version,
                    excerpt_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    completed_at=excluded.completed_at,
                    conversation_id=excluded.conversation_id,
                    selected_model=excluded.selected_model,
                    endpoint_id=excluded.endpoint_id,
                    task=excluded.task,
                    route_profile=excluded.route_profile,
                    strategy_version=excluded.strategy_version,
                    history_mode=excluded.history_mode,
                    status=excluded.status,
                    status_code=excluded.status_code,
                    graph_version=excluded.graph_version,
                    excerpt_json=excluded.excerpt_json,
                    payload_json=excluded.payload_json
                """,
                (
                    payload["request_id"],
                    payload["started_at"],
                    payload["updated_at"],
                    payload.get("completed_at"),
                    payload["instance_id"],
                    payload["boot_id"],
                    payload["client_id"],
                    payload.get("conversation_id"),
                    payload["protocol"],
                    payload["requested_model"],
                    payload.get("selected_model"),
                    payload.get("endpoint_id"),
                    payload.get("task"),
                    payload.get("route_profile"),
                    payload.get("strategy_version"),
                    payload.get("history_mode"),
                    payload["status"],
                    payload.get("status_code"),
                    payload["graph_version"],
                    excerpt,
                    encoded,
                ),
            )

    def _get(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM route_traces WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            reviews = connection.execute(
                """
                SELECT id, created_at, verdict, expected_task,
                       expected_model, note, reviewer_source
                FROM route_reviews
                WHERE request_id = ?
                ORDER BY id DESC
                """,
                (request_id,),
            ).fetchall()
        payload = json.loads(row["payload_json"])
        payload["review_status"] = row["review_status"]
        payload["reviews"] = [dict(item) for item in reviews]
        payload["current_review"] = (
            dict(reviews[0]) if reviews else None
        )
        return payload

    def _list(
        self,
        limit: int,
        cursor: str | None,
        request_mode: str,
        client_id: str | None,
        conversation_id: str | None,
        task: str | None,
        route_profile: str | None,
        selected_model: str | None,
        status: str | None,
        review_status: str | None,
        search: str | None,
    ) -> dict[str, Any]:
        limit = max(1, min(100, int(limit)))
        where: list[str] = []
        values: list[Any] = []
        if request_mode == "auto":
            where.append("t.requested_model = 'auto'")
        elif request_mode == "explicit":
            where.append("t.requested_model <> 'auto'")
        if client_id:
            where.append("t.client_id = ?")
            values.append(client_id)
        if conversation_id:
            where.append("t.conversation_id = ?")
            values.append(conversation_id)
        if task:
            where.append("t.task = ?")
            values.append(task)
        if route_profile:
            where.append("t.route_profile = ?")
            values.append(route_profile)
        if selected_model:
            where.append("t.selected_model = ?")
            values.append(selected_model)
        if status:
            where.append("t.status = ?")
            values.append(status)
        if review_status:
            where.append("t.review_status = ?")
            values.append(review_status)
        if search:
            where.append(
                "(t.request_id LIKE ? OR t.conversation_id LIKE ?)"
            )
            term = f"%{search[:256]}%"
            values.extend((term, term))
        decoded_cursor = _decode_cursor(cursor)
        if decoded_cursor:
            where.append(
                "(t.started_at < ? OR "
                "(t.started_at = ? AND t.request_id < ?))"
            )
            values.extend(
                (
                    decoded_cursor["started_at"],
                    decoded_cursor["started_at"],
                    decoded_cursor["request_id"],
                )
            )
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        query = f"""
            SELECT t.*,
                   r.verdict AS review_verdict,
                   r.expected_task AS review_expected_task,
                   r.expected_model AS review_expected_model,
                   r.note AS review_note,
                   r.created_at AS reviewed_at
            FROM route_traces t
            LEFT JOIN route_reviews r ON r.id = (
                SELECT MAX(latest.id)
                FROM route_reviews latest
                WHERE latest.request_id = t.request_id
            )
            {clause}
            ORDER BY t.started_at DESC, t.request_id DESC
            LIMIT ?
        """
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._summary(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = _encode_cursor(
                float(rows[-1]["started_at"]),
                str(rows[-1]["request_id"]),
            )
        return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        review = None
        if row["review_verdict"]:
            review = {
                "verdict": row["review_verdict"],
                "expected_task": row["review_expected_task"],
                "expected_model": row["review_expected_model"],
                "note": row["review_note"],
                "created_at": row["reviewed_at"],
            }
        return {
            "request_id": row["request_id"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "client_id": row["client_id"],
            "conversation_id": row["conversation_id"],
            "protocol": row["protocol"],
            "requested_model": row["requested_model"],
            "selected_model": row["selected_model"],
            "endpoint_id": row["endpoint_id"],
            "task": row["task"],
            "route_profile": (
                row["route_profile"]
                or payload.get("route_profile")
                or "general"
            ),
            "strategy_version": (
                row["strategy_version"]
                or payload.get("strategy_version")
                or "legacy_v1"
            ),
            "history_mode": (
                row["history_mode"]
                or payload.get("history_mode")
                or "native"
            ),
            "status": row["status"],
            "status_code": row["status_code"],
            "graph_version": row["graph_version"],
            "review_status": row["review_status"],
            "excerpt": json.loads(row["excerpt_json"]),
            "error": payload.get("error"),
            "route_selected": bool(payload.get("route_selected")),
            "current_review": review,
        }

    def _add_review(
        self,
        request_id: str,
        verdict: str,
        expected_task: str | None,
        expected_model: str | None,
        note: str | None,
        reviewer_source: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM route_traces WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(request_id)
            cursor = connection.execute(
                """
                INSERT INTO route_reviews (
                    request_id, created_at, verdict, expected_task,
                    expected_model, note, reviewer_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    now,
                    verdict,
                    expected_task,
                    expected_model,
                    note,
                    reviewer_source,
                ),
            )
            connection.execute(
                """
                UPDATE route_traces
                SET review_status = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (verdict, now, request_id),
            )
        return {
            "id": cursor.lastrowid,
            "request_id": request_id,
            "created_at": now,
            "verdict": verdict,
            "expected_task": expected_task,
            "expected_model": expected_model,
            "note": note,
            "reviewer_source": reviewer_source,
        }

    def _interrupt_previous_boot(
        self,
        instance_id: str,
        boot_id: str,
    ) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, payload_json
                FROM route_traces
                WHERE instance_id = ? AND boot_id <> ? AND status = 'running'
                """,
                (instance_id, boot_id),
            ).fetchall()
            now = time.time()
            for row in rows:
                payload = json.loads(row["payload_json"])
                attempts = payload.setdefault("attempts", [])
                attempt = attempts[-1] if attempts else {
                    "number": 1,
                    "started_at": payload.get("started_at", now),
                    "steps": [],
                    "transitions": [],
                    "selection": None,
                }
                if not attempts:
                    attempts.append(attempt)
                previous = (
                    attempt["steps"][-1]["node_id"]
                    if attempt["steps"]
                    else None
                )
                attempt["steps"].append(
                    {
                        "sequence": len(attempt["steps"]) + 1,
                        "timestamp": now,
                        "node_id": "failed",
                        "status": "error",
                        "branch": "final_error",
                        "reason": "router_restarted",
                        "evidence": {
                            "previous_boot_id": payload.get("boot_id"),
                            "current_boot_id": boot_id,
                        },
                        "path": not bool(
                            payload.get("route_selected")
                        ),
                    }
                )
                if previous and previous != "failed":
                    attempt["transitions"].append(
                        {
                            "sequence": len(attempt["transitions"]) + 1,
                            "from": previous,
                            "to": "failed",
                            "branch": "final_error",
                            "status": "error",
                        }
                    )
                payload.update(
                    {
                        "status": "interrupted",
                        "status_code": 499,
                        "completed_at": now,
                        "updated_at": now,
                        "error": {
                            "code": "router_restarted",
                            "message": "request interrupted by router restart",
                        },
                    }
                )
                connection.execute(
                    """
                    UPDATE route_traces
                    SET status='interrupted', status_code=499,
                        completed_at=?, updated_at=?, payload_json=?
                    WHERE request_id=?
                    """,
                    (
                        now,
                        now,
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        row["request_id"],
                    ),
                )
        return len(rows)

    def _cleanup(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        with self._connect() as connection:
            request_ids = [
                str(item["request_id"])
                for item in connection.execute(
                    "SELECT request_id FROM route_traces WHERE started_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if request_ids:
                connection.executemany(
                    "DELETE FROM route_reviews WHERE request_id = ?",
                    ((item,) for item in request_ids),
                )
            cursor = connection.execute(
                "DELETE FROM route_traces WHERE started_at < ?",
                (cutoff,),
            )
        return int(cursor.rowcount)

    def _connect(self, *, initialize: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if initialize:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def validate_review(
    value: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None]:
    verdict = str(value.get("verdict", "")).strip()
    if verdict not in REVIEW_VERDICTS:
        raise ValueError(
            "verdict must be correct, incorrect, or needs_review"
        )
    expected_task = _optional_text(value.get("expected_task"), 80)
    expected_model = _optional_text(value.get("expected_model"), 240)
    note = _optional_text(value.get("note"), 1000)
    if (
        verdict == "incorrect"
        and not expected_task
        and not expected_model
        and not note
    ):
        raise ValueError(
            "incorrect review requires expected_task, expected_model, or note"
        )
    return verdict, expected_task, expected_model, note


def _optional_text(value: Any, limit: int) -> str | None:
    text = _redact_text(str(value or ""))
    return text[:limit] if text else None


def _encode_cursor(started_at: float, request_id: str) -> str:
    payload = json.dumps(
        {"started_at": started_at, "request_id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(value + padding).decode("utf-8")
        )
        return {
            "started_at": float(payload["started_at"]),
            "request_id": str(payload["request_id"]),
        }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
