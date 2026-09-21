"""Display-only summaries from the request's recorded evidence, never today's registry."""
from .usage_evidence import token_count


def token_summary(payload):
    request = payload.get("request") or {}
    attempts = payload.get("attempts") or []
    attempt = attempts[-1] if attempts else {}
    selection = attempt.get("selection") or {}
    endpoint_id = selection.get("endpoint_id")
    steps = attempt.get("steps") or []
    reserve = token_count(request.get("output_reserve_tokens"))
    required = token_count(selection.get("context_required"))
    if not required:
        required = None
    target = required - reserve if required is not None and reserve is not None and required >= reserve else None
    counts = payload.get("token_counting") or {}
    counted = (counts.get("selected") or {}) if endpoint_id else {}
    if target is None or token_count(counted.get("tokens")) != target:
        counted = (counts.get("candidates") or {}).get(endpoint_id) or {}
    if endpoint_id and target is None:
        target = token_count(counted.get("tokens"))
    if required is None and target is not None and reserve is not None:
        required = target + reserve
    exact = counted.get("exact") if token_count(counted.get("tokens")) == target and target is not None else None
    source = counted.get("source") if exact is not None else None
    safe = None
    finalized = False
    deployment_id = selection.get("deployment_id") or endpoint_id
    for step in reversed(steps):
        evidence = step.get("evidence") or {}
        if (step.get("reason") in {"deployment_finalized", "deployment_selected"}
                and endpoint_id and evidence.get("endpoint_id") == endpoint_id
                and evidence.get("deployment_id") == deployment_id):
            safe = token_count(evidence.get("safe_context_tokens"))
            finalized = step.get("reason") == "deployment_finalized"
            break
    if safe is not None and not finalized:
        # Preserve the endpoint cap, but never substitute it for missing deployment evidence.
        for step in reversed(steps):
            if step.get("node_id") == "candidate_scope":
                candidate = next((item for item in (step.get("evidence") or {}).get("candidates", [])
                                  if item.get("endpoint_id") == endpoint_id), {})
                cap = token_count(candidate.get("safe_context_tokens"))
                if cap is not None:
                    safe = min(safe, cap)
                break
    upstream = next((s.get("evidence") or {} for s in reversed(steps)
                     if s.get("node_id") == "upstream_request" and s.get("reason") == "upstream_response"), {})
    usage = upstream.get("backend_usage") or {}
    measured_input = token_count(usage.get("input_tokens")) if usage.get("state") != "invalid" else None
    measured_output = token_count(upstream.get("output_tokens")) if upstream.get("output_tokens_measured") is True else None
    return {"ingress_estimated_input_tokens": token_count(request.get("prompt_tokens")),
            "target_input_tokens": target, "target_count_source": source, "target_count_exact": exact,
            "measured_input_tokens": measured_input, "measured_output_tokens": measured_output,
            "output_reserve_tokens": reserve, "required_context_tokens": required,
            "safe_context_tokens": safe}
