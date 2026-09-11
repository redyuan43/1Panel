from __future__ import annotations

import re
import time

from fastapi import HTTPException


def acceptance_scope(dispatcher, recipe_id, backend, now=None):
    entry = backend.get("recipes", {}).get(recipe_id, {})
    if recipe_id not in getattr(dispatcher.catalog, "entries", {}) or entry.get("qualification") != "designated_acceptance":
        return None
    profile = dispatcher.catalog.get(recipe_id)
    scope = entry.get("acceptance", {})
    prefix = scope.get("execution_prefix", "")
    current = time.time() if now is None else now
    gate = dispatcher.fleet.store.release_validation_gate()
    if (entry.get("recipe_version") != profile["version"]
            or not re.fullmatch(r"studio_[a-f0-9]{12}_", prefix)
            or type(scope.get("expires_at")) not in (int, float)
            or not current < scope["expires_at"] <= current + 86400
            or gate.get("enabled") is not True or prefix not in gate.get("allow_execution_prefixes", [])
            or dispatcher.fleet.policy.data.get("max_active_jobs") != 1
            or dispatcher.control("quarantine:" + dispatcher.profile_key(recipe_id, backend))):
        return None
    return {"task_id": prefix[7:-1], "execution_prefix": prefix, "expires_at": scope["expires_at"],
            "qualification": "designated_acceptance_only", "parallel_validated": False}


def check_submission(dispatcher, recipe_id, binding, execution_id):
    if recipe_id not in getattr(dispatcher.catalog, "entries", {}):
        return
    if [binding.get(field) for field in ("width", "height", "frame_count", "fps")] != [480, 864, 362, 24]:
        raise HTTPException(409, "multimodal_spec_not_execution_validated")
    for backend in dispatcher.backends.values():
        if not dispatcher.qualifications(recipe_id, backend):
            continue
        if backend.get("recipes", {}).get(recipe_id, {}).get("qualification") != "designated_acceptance":
            return
        scope = acceptance_scope(dispatcher, recipe_id, backend)
        if scope and execution_id.startswith(scope["execution_prefix"]) and len(execution_id) > len(scope["execution_prefix"]):
            return
    raise HTTPException(409, "multimodal_runtime_not_qualified_for_this_task")


def install(dispatcher):
    previous_qualification = dispatcher.qualifications
    previous_public = dispatcher.public

    def qualified(recipe_id, backend):
        if backend.get("recipes", {}).get(recipe_id, {}).get("qualification") == "designated_acceptance":
            return acceptance_scope(dispatcher, recipe_id, backend) is not None
        return previous_qualification(recipe_id, backend)

    def public():
        result = previous_public()
        for profile in result.get("multimodal_profiles", []):
            identifier = profile["profile_id"]
            formal = [backend for backend in dispatcher.backends.values()
                      if backend.get("recipes", {}).get(identifier, {}).get("qualification") != "designated_acceptance"
                      and dispatcher.qualifications(identifier, backend)]
            scopes = [scope for backend in dispatcher.backends.values()
                      if (scope := acceptance_scope(dispatcher, identifier, backend))]
            profile.update(qualified=bool(formal), qualification="completed_single_lane" if formal else "not_execution_validated",
                           acceptance_tasks=[scope["task_id"] for scope in scopes],
                           execution_specs=[{"width": 480, "height": 864, "frame_count": 362, "fps": 24}],
                           parallel_validated=False)
        return result

    dispatcher.qualifications = qualified
    dispatcher.public = public
