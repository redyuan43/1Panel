"""Foreground-owned submission and application of background candidates."""
from __future__ import annotations

from .compaction import Capsule, extract_messages, message_hash
from .memory_service import _thread
from .routing_modes import resolve


async def _store(current, owner):
    if not current.settings.section("compaction").get("background_enabled", False):
        return None
    policy = await current.clients.current_policy(owner)
    if not policy or not policy.allow_compaction:
        return None
    worker = getattr(current, "compaction_worker", None)
    if worker is None:
        return None
    if worker.jobs is None:
        await _thread(worker._open)
    return worker.jobs


async def _candidate_branches(current, branch, lineage):
    branches = [branch]
    if lineage is None or lineage.relation != "continuation":
        return branches
    parent = lineage.parent
    # Only walk server-resolved continuation ancestry, never a client supplied
    # branch ID. Bound Redis work, stop at forks, expiry, and malformed cycles.
    while parent is not None and len(branches) <= 32:
        parent_id = parent.branch_id
        if (not parent_id or parent_id in branches
                or parent.conversation_id != lineage.lineage_id):
            break
        branches.append(parent_id)
        if parent.lineage_relation != "continuation" or not parent.parent_branch_id:
            break
        parent = await current.conversations.get(parent.parent_branch_id)
    return branches


async def apply_background(current, body, *, owner, branch, api_kind, identity, lineage=None, summary_scope=None):
    try:
        jobs = await _store(current, owner)
        if jobs is None:
            return body, None
        branches = await _candidate_branches(current, branch, lineage)
        for job in await _thread(jobs.ready, owner, branch, ancestors=branches[1:]):
            if job["parameters"].get("model_id") != current.compactor.model_id:
                continue
            if not await current.clients.is_key_active(owner, job["parameters"].get("key_id", "")):
                continue
            applied = await _thread(jobs.apply_candidate, owner, job["id"], job["branch"], body, api_kind)
            if applied is None:
                continue
            before = current.token_counter.count_request(identity.inject(body, api_kind), api_kind)
            after = current.token_counter.count_request(identity.inject(applied, api_kind), api_kind)
            if after >= before:
                continue
            messages = extract_messages(applied, api_kind)
            original = extract_messages(body, api_kind)
            if not original:
                continue
            capsule = Capsule(current.compactor.cipher.encrypt(messages), message_hash(original[-1]), before, after,
                              background_job_id=job["id"])
            if summary_scope is not None:
                summary_scope.adopt_legacy_job(job)
                await summary_scope.remember(messages, summary_scope.indices(messages))
            current.audit.write("background_compaction_candidate_applied", job_id=job["id"], client_id=owner,
                                before_tokens=before, after_tokens=after)
            return applied, capsule
    except Exception as exc:
        current.audit.write("background_compaction_apply_skipped", error_type=type(exc).__name__)
    return body, None


async def submit_background(current, body, *, owner, key_id, branch, api_kind, decision, summary_scope=None):
    try:
        jobs = await _store(current, owner)
        if jobs is None:
            return None
        window = decision.deployment_safe_context_tokens or decision.endpoint.safe_context_tokens
        if decision.prompt_tokens + decision.output_reserve_tokens < int(window * 0.8):
            return None
        policy = await current.clients.current_policy(owner)
        if not policy or not policy.allow_compaction or not await current.clients.is_key_active(owner, key_id):
            return None
        local_only = resolve(current.settings.section("routing"), policy.routing_mode, policy.local_only)["local_only"]
        job = await _thread(jobs.create, owner, branch, body, api_kind, {
            **(summary_scope.job_parameters() if summary_scope is not None else {}),
            "limits": current.settings.section("compaction").get("background_limits", {}),
            "summary_reasoning": current.settings.section("compaction").get("summary_reasoning", "provider_default"),
            "summary_output_tokens": current.settings.section("compaction").get("summary_output_tokens", 8192),
            "model_id": current.compactor.model_id, "target_context_tokens": window,
            "target_endpoint_id": decision.endpoint.id, "key_id": key_id, "local_only": local_only})
        current.audit.write("background_compaction_submitted", job_id=job["id"], client_id=owner, state=job["state"])
        return job["id"]
    except Exception as exc:
        current.audit.write("background_compaction_submit_skipped", error_type=type(exc).__name__)
        return None
