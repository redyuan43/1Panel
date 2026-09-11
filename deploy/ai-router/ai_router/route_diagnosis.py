from __future__ import annotations

from typing import Any

from .config import Registry, Settings


DIAGNOSIS_VERSION = 1

_REJECTION_LABELS = {
    "unhealthy_or_stale": "健康状态异常或过期",
    "context": "上下文容量不足",
    "output_context": "输出预留超限",
    "modality": "模态不兼容",
    "capability": "协议或工具能力不兼容",
    "task": "任务类型不匹配",
    "tier_downgrade": "迁移后等级锁定",
    "physical_deployment": "无合格物理部署",
    "cloud_disabled": "云端能力未启用",
    "cloud_auto_disabled": "云端自动升级未启用",
    "excluded": "本轮重试已排除",
}


def diagnose_route(
    trace: dict[str, Any],
    conversation_traces: list[dict[str, Any]],
    settings: Settings,
    registry: Registry,
) -> dict[str, Any]:
    attempts = [
        item
        for item in trace.get("attempts", [])
        if isinstance(item, dict)
    ]
    steps = [
        step
        for attempt in attempts
        for step in attempt.get("steps", [])
        if isinstance(step, dict)
    ]
    candidates = _latest_candidates(steps)
    selection = _latest_selection(attempts)
    endpoint_id = str(
        trace.get("endpoint_id")
        or selection.get("endpoint_id")
        or ""
    )
    selected = registry.by_id(endpoint_id)
    selected_label = (
        f"{selected.id}（{selected.node}）"
        if selected
        else endpoint_id or "无可用端点"
    )
    affinity = str(selection.get("affinity") or "")
    reason = str(selection.get("reason") or "")
    previous_endpoint_id = _previous_endpoint_id(steps)
    previous_rejection = next(
        (
            item.get("rejection_reason")
            for item in candidates
            if item.get("endpoint_id") == previous_endpoint_id
        ),
        None,
    )

    if trace.get("status") == "failed" and not trace.get("route_selected"):
        verdict = "本轮未找到同时满足硬约束和可用性要求的端点。"
    elif affinity == "admin-pin":
        verdict = f"管理员临时固定生效，本轮路由到 {selected_label}。"
    elif trace.get("local_pool", {}).get("selection") == "capacity_timeout_cold_fallback":
        verdict = f"原设备等待达到上限，本轮按容量回退到 {selected_label}；异机可能需要冷计算。"
    elif trace.get("local_pool", {}).get("allocation_fallback") == "allocation_lock_busy":
        verdict = f"本地分配锁繁忙，本轮按既有候选与容量机制选择了 {selected_label}；未保证会话分散。"
    elif reason == "local_pool_spread":
        verdict = f"本轮按本地候选组的空闲程度和近期会话占用选择了 {selected_label}；原始评分仅供审计。"
    elif reason == "local_pool_faster_first_output":
        verdict = f"历史实测估算显示异机首个输出等待明显更短，本轮迁移到 {selected_label}；实际缓存复用需查看后端计数。"
    elif previous_endpoint_id and endpoint_id != previous_endpoint_id:
        cause = _REJECTION_LABELS.get(
            str(previous_rejection),
            reason or "原亲和端点不再合格",
        )
        verdict = (
            f"会话从 {previous_endpoint_id} 迁移到 {selected_label}，"
            f"直接原因是原端点{cause}。"
        )
    elif affinity in {"hit", "logical-hit", "cache-reset"}:
        verdict = f"原会话端点仍满足约束，本轮保持在 {selected_label}。"
    else:
        verdict = f"本轮按候选资格与评分选择了 {selected_label}。"

    objective = trace.get("routing_objective", {})
    objective_labels = {
        "cost_affinity": "成本优先，继续复用原会话模型",
        "cost_local": "成本优先，选择合格本地模型",
        "cost_flash_fallback": "本地无法处理，使用 Flash 兜底",
        "efficiency_affinity": "原会话模型尚未触发迁移门槛",
        "efficiency_initial": "效率优先，选择当前可用模型",
        "efficiency_cooldown": "迁移冷却期内，继续保持当前模型",
        "efficiency_measured_gain": "性能样本显示迁移收益达到门槛",
        "efficiency_severe_wait": "上一轮首个有效输出等待超过门槛，下一轮重选",
        "efficiency_sustained_slowdown": "近期持续降速，下一轮重选",
        "quality_preferred": "质量优先，按任务首选与备选顺序选择",
        "quality_flash_fallback": "高质量模型不可用，已降到 Flash",
        "quality_wait": "质量候选繁忙，等待配置的高质量模型",
    }
    if reason in objective_labels and not objective.get("observe_only"):
        verdict = objective_labels[reason] + "；本轮使用 " + selected_label + "。"
    elif previous_rejection == "context":
        counts = trace.get("token_counting", {}).get("candidates", {})
        verdict = f"原会话端点 {previous_endpoint_id} 的上下文容量不足，本轮迁移到 {selected_label}。"
        if counts.get(previous_endpoint_id, {}).get("exact") is False:
            verdict += " 原端点计数为估算。"

    causal_chain = _causal_chain(
        trace,
        candidates,
        selection,
        previous_endpoint_id,
        previous_rejection,
    )
    non_causes = _non_causes(trace, candidates, previous_endpoint_id)
    alternatives = [_alternative(item) for item in candidates]
    policy_refs = _policy_refs(
        previous_rejection=previous_rejection,
        reason=reason,
        affinity=affinity,
    )
    return {
        "routing_objective": objective,
        "token_counting": trace.get("token_counting", {}),
        "performance_observation": trace.get("performance_observation", {}),
        "diagnosis_version": DIAGNOSIS_VERSION,
        "verdict": verdict,
        "causal_chain": causal_chain,
        "non_causes": non_causes,
        "alternatives": alternatives,
        "conversation_phases": _conversation_phases(
            conversation_traces,
            registry,
        ),
        "policy_refs": policy_refs,
        "actions": _actions(
            trace,
            previous_endpoint_id=previous_endpoint_id,
            endpoint_id=endpoint_id,
            settings=settings,
        ),
    }


def preview_policy_impact(
    traces: list[dict[str, Any]],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    stability = (
        proposed.get("routing", {}).get("conversation_stability", {})
    )
    enabled = bool(stability.get("enabled"))
    affected: list[dict[str, Any]] = []
    cloud_switches = 0
    observed_429 = 0
    unroutable = 0
    for trace in traces:
        if trace.get("requested_model") not in {"auto", "siyuan/auto"}:
            continue
        candidates = _latest_candidates(
            [
                step
                for attempt in trace.get("attempts", [])
                for step in attempt.get("steps", [])
            ]
        )
        previous_id = _previous_endpoint_id(
            [
                step
                for attempt in trace.get("attempts", [])
                for step in attempt.get("steps", [])
            ]
        )
        previous = next(
            (
                item
                for item in candidates
                if item.get("endpoint_id") == previous_id
            ),
            None,
        )
        if (
            enabled
            and previous
            and previous.get("rejection_reason")
            == "unhealthy_or_stale"
        ):
            affected.append(
                {
                    "request_id": trace.get("request_id"),
                    "client_id": trace.get("client_id"),
                    "conversation_id": trace.get("conversation_id"),
                    "change": "health_recheck_before_migration",
                    "from_endpoint_id": previous_id,
                    "to_endpoint_id": trace.get("endpoint_id"),
                }
            )
        selected = next(
            (
                item
                for item in candidates
                if item.get("endpoint_id") == trace.get("endpoint_id")
            ),
            None,
        )
        if selected and selected.get("cloud"):
            cloud_switches += 1
        if trace.get("status_code") == 429:
            observed_429 += 1
        if (
            trace.get("status") == "failed"
            and not trace.get("route_selected")
        ):
            unroutable += 1
    clients = sorted(
        {
            str(item["client_id"])
            for item in affected
            if item.get("client_id")
        }
    )
    return {
        "evaluated_requests": len(traces),
        "route_changes": len(affected),
        "health_recheck_candidates": len(affected),
        "migration_outcome": "conditional_on_live_recheck",
        "cloud_switches": cloud_switches,
        "observed_429": observed_429,
        "unroutable_requests": unroutable,
        "affected_clients": clients,
        "examples": affected[:20],
        "offline_only": True,
        "objective_evaluation": "requires_live_performance_observation" if proposed.get("routing", {}).get("objectives", {}).get("enabled") else "legacy",
    }


def _latest_candidates(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for step in reversed(steps):
        evidence = step.get("evidence") or {}
        candidates = evidence.get("candidates")
        if step.get("node_id") == "candidate_scope" and isinstance(
            candidates,
            list,
        ):
            return [
                item for item in candidates if isinstance(item, dict)
            ]
    return []


def _latest_selection(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    for attempt in reversed(attempts):
        selection = attempt.get("selection")
        if isinstance(selection, dict):
            return selection
    return {}


def _previous_endpoint_id(steps: list[dict[str, Any]]) -> str | None:
    for step in reversed(steps):
        evidence = step.get("evidence") or {}
        value = evidence.get("previous_endpoint_id")
        if value:
            return str(value)
    return None


def _causal_chain(
    trace: dict[str, Any],
    candidates: list[dict[str, Any]],
    selection: dict[str, Any],
    previous_endpoint_id: str | None,
    previous_rejection: Any,
) -> list[dict[str, Any]]:
    request = trace.get("request") or {}
    result = [
        {
            "stage": "request",
            "title": "请求约束",
            "detail": (
                f"需要 {int(request.get('required_context_tokens') or 0):,} "
                "Token，模态 "
                + " / ".join(request.get("modalities") or ["text"])
            ),
        },
        {
            "stage": "candidate_scope",
            "title": "候选资格筛选",
            "detail": (
                f"{sum(not item.get('rejection_reason') for item in candidates)} "
                f"个合格，{sum(bool(item.get('rejection_reason')) for item in candidates)} "
                "个被淘汰"
            ),
        },
    ]
    if previous_endpoint_id:
        result.append(
            {
                "stage": "affinity",
                "title": "会话亲和判断",
                "detail": (
                    f"{previous_endpoint_id}: "
                    + (
                        _REJECTION_LABELS.get(
                            str(previous_rejection),
                            str(previous_rejection),
                        )
                        if previous_rejection
                        else "仍然合格"
                    )
                ),
            }
        )
    result.append(
        {
            "stage": "selection",
            "title": "最终去向",
            "detail": (
                f"{selection.get('endpoint_id') or trace.get('endpoint_id') or '无'}"
                f"；原因 {selection.get('reason') or '无可用路由'}"
            ),
        }
    )
    return result


def _non_causes(
    trace: dict[str, Any],
    candidates: list[dict[str, Any]],
    previous_endpoint_id: str | None,
) -> list[dict[str, Any]]:
    required = int(
        (trace.get("request") or {}).get("required_context_tokens") or 0
    )
    result: list[dict[str, Any]] = []
    previous = next(
        (
            item
            for item in candidates
            if item.get("endpoint_id") == previous_endpoint_id
        ),
        None,
    )
    if (
        previous
        and required
        and int(previous.get("safe_context_tokens") or 0) >= required
        and previous.get("rejection_reason") != "context"
    ):
        result.append(
            {
                "code": "context_within_limit",
                "label": "不是上下文溢出",
                "evidence": (
                    f"{required:,} < "
                    f"{int(previous.get('safe_context_tokens') or 0):,}"
                ),
            }
        )
    if not trace.get("context_compacted"):
        result.append(
            {
                "code": "no_compaction",
                "label": "不是压缩切换",
                "evidence": "本轮未执行上下文压缩",
            }
        )
    return result


def _alternative(item: dict[str, Any]) -> dict[str, Any]:
    reason = item.get("rejection_reason")
    return {
        "endpoint_id": item.get("endpoint_id"),
        "node": item.get("node"),
        "eligible": not bool(reason),
        "rejection_reason": reason,
        "rejection_label": _REJECTION_LABELS.get(
            str(reason),
            str(reason or "合格"),
        ),
        "required_context_tokens": item.get("required_context_tokens"),
        "safe_context_tokens": item.get("safe_context_tokens"),
        "load_headroom": item.get("load_headroom"),
        "quality_score": item.get("quality_score"),
        "healthy": item.get("healthy"),
        "fresh": item.get("fresh"),
        "tier": item.get("tier"),
        "cloud": bool(item.get("cloud")),
    }


def _conversation_phases(
    traces: list[dict[str, Any]],
    registry: Registry,
) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for trace in sorted(
        traces,
        key=lambda item: (
            float(item.get("started_at") or 0),
            str(item.get("request_id") or ""),
        ),
    ):
        endpoint_id = str(trace.get("endpoint_id") or "")
        if not endpoint_id:
            continue
        flags = _trace_flags(trace)
        if phases and phases[-1]["endpoint_id"] == endpoint_id:
            phases[-1]["request_count"] += 1
            phases[-1]["last_request_id"] = trace.get("request_id")
            phases[-1]["ended_at"] = trace.get("started_at")
            phases[-1]["flags"] = sorted(
                set(phases[-1]["flags"]) | set(flags)
            )
            continue
        endpoint = registry.by_id(endpoint_id)
        phases.append(
            {
                "endpoint_id": endpoint_id,
                "node": endpoint.node if endpoint else None,
                "request_count": 1,
                "first_request_id": trace.get("request_id"),
                "last_request_id": trace.get("request_id"),
                "started_at": trace.get("started_at"),
                "ended_at": trace.get("started_at"),
                "flags": flags,
            }
        )
    return phases


def _trace_flags(trace: dict[str, Any]) -> list[str]:
    selection = _latest_selection(trace.get("attempts") or [])
    reasons = {
        str(step.get("reason"))
        for attempt in trace.get("attempts") or []
        for step in attempt.get("steps") or []
    }
    flags: list[str] = []
    if selection.get("affinity") == "migrated":
        flags.append("endpoint_migration")
        steps = [
            step
            for attempt in trace.get("attempts") or []
            for step in attempt.get("steps") or []
        ]
        candidates = _latest_candidates(steps)
        previous_id = _previous_endpoint_id(steps)
        previous = next(
            (
                item
                for item in candidates
                if item.get("endpoint_id") == previous_id
            ),
            None,
        )
        if (
            previous
            and previous.get("rejection_reason")
            == "unhealthy_or_stale"
        ):
            flags.append("health_failure_migration")
            required = int(
                previous.get("required_context_tokens") or 0
            )
            safe = int(previous.get("safe_context_tokens") or 0)
            if required and safe >= required:
                flags.append("premature_migration")
    if "affinity_health_recovered" in reasons:
        flags.append("health_flap_recovered")
    if "affinity_health_threshold_reached" in reasons:
        flags.append("health_failure_migration")
    if selection.get("reason") == "monotonic_upgrade":
        flags.append("tier_lock")
    if "capacity_busy" in reasons:
        flags.append("capacity_overflow")
    if trace.get("context_compacted"):
        flags.append("context_migration")
    return flags


def _policy_refs(
    *,
    previous_rejection: Any,
    reason: str,
    affinity: str,
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    if previous_rejection == "unhealthy_or_stale":
        refs.extend(
            [
                {
                    "field": "routing.conversation_stability.enabled",
                    "section": "conversation-stability",
                    "label": "会话健康复检",
                },
                {
                    "field": (
                        "routing.conversation_stability."
                        "health_failure_threshold"
                    ),
                    "section": "conversation-stability",
                    "label": "连续失败阈值",
                },
                {
                    "field": "health.stale_after_seconds",
                    "section": "health-advanced",
                    "label": "健康状态过期时间",
                },
            ]
        )
    if reason == "monotonic_upgrade" or affinity == "migrated":
        refs.append(
            {
                "field": (
                    "routing.conversation_stability."
                    "preserve_tier_after_migration"
                ),
                "section": "conversation-stability",
                "label": "迁移后等级锁定",
            }
        )
    if not refs:
        refs.append(
            {
                "field": "routing.strategy",
                "section": "policy-routing-strategy",
                "label": "路由策略",
            }
        )
    return refs


def _actions(
    trace: dict[str, Any],
    *,
    previous_endpoint_id: str | None,
    endpoint_id: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    stability = settings.section("routing").get(
        "conversation_stability",
        {},
    )
    actions = [
        {
            "id": "open_policy",
            "label": "打开对应策略",
            "section": "conversation-stability",
        },
        {
            "id": "reset_affinity",
            "label": "下一轮重置路由亲和",
            "conversation_id": trace.get("conversation_id"),
            "client_id": trace.get("client_id"),
        },
    ]
    for target in dict.fromkeys(
        item
        for item in (endpoint_id, previous_endpoint_id)
        if item
    ):
        actions.append(
            {
                "id": "pin_endpoint",
                "label": f"临时固定 {target}",
                "endpoint_id": target,
                "default_ttl_seconds": 3600,
                "conversation_id": trace.get("conversation_id"),
                "client_id": trace.get("client_id"),
            }
        )
    actions.append(
        {
            "id": "stability_status",
            "label": "当前稳定策略",
            "enabled": bool(stability.get("enabled")),
            "recovery_mode": stability.get("recovery_mode", "manual"),
        }
    )
    return actions
