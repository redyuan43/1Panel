"""Studio connector boundary; install with the already installed Router Contract.

TOOL_DEFINITIONS is the JSON-serializable schema source for the Control gateway.
Revisions are opaque state digests, including asynchronous stage changes.
No MCP SDK, model client, or independent generation store is introduced here.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib
import importlib.util
import json
import math
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool


def _support(name):
    spec = importlib.util.spec_from_file_location("h3_connector_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


API_PREFIX = "/api/router/connector"


class ConnectorError(HTTPException):
    pass


SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": SAFE_ID}
REVISION = {"type": "string", "pattern": r"^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64,
            "description": "Opaque revision from the latest task response; never increment it locally."}
PROMPT = {"type": "string", "minLength": 1, "maxLength": 20000, "pattern": r"\S"}
RECIPE_IDS = ("A4", "A4_C0", "A4_C1", "B8")
INPUT_MODES = ("t2v", "i2v", "l2v", "fl2v", "reference", "hybrid")
ASSET_KINDS = ("first_frame", "last_frame", "reference_image", "reference_video", "reference_audio")
FIXED_SCOPE = {"mode": "t2v", "duration": 15, "width": 480, "height": 864,
               "fps": 24, "orientation": "portrait", "audio_policy": "native"}
WRITE_FIELDS = {"operation_id": IDENTIFIER, "task_id": IDENTIFIER, "expected_revision": REVISION}
RUN_ID = {"anyOf": [IDENTIFIER, {"type": "null"}],
          "description": "Exact current preview run ID; null only if it has never run."}


def _tool(name, description, properties, required=(), *, readonly=False, destructive=False, **schema):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False, **schema},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": destructive,
                            "idempotentHint": True, "openWorldHint": False}}


TOOL_DEFINITIONS = [
    _tool("h3_capabilities", "Read fixed preview scope, recipe catalog confirmation and safety gates; no inference.",
          {}, readonly=True),
    _tool("h3_prompt_guidance", "Read the existing H3/cinematic/creative distilled Skill rules without LLM calls. "
          "Defaults to the two baseline Skills plus TVC. This does not execute client Skills.",
          {"skill_ids": {"type": "array", "maxItems": 6, "uniqueItems": True, "items": IDENTIFIER}}, readonly=True),
    _tool("h3_save_draft", "Synchronously save a manual draft without cloud/GPU calls. Creation omits task_id and "
          "expected_revision; edits require both. Original prompt is immutable. Omitted edit options are preserved.",
          {**WRITE_FIELDS, "original_prompt": PROMPT, "prompt": PROMPT,
           "recipe_id": {"type": "string", "enum": list(RECIPE_IDS), "default": "A4"},
           "preview_recipe_id": {"type": "string", "enum": ["A4", "A4_C0", "A4_C1", "B8"], "description": "Explicit first-frame comparison recipe; requires separate runtime qualification."},
           "name": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
           "seed": {"type": "integer", "minimum": -1, "maximum": 2**63 - 2},
           "verbatim": {"type": "boolean", "description": "Require prompt to equal original_prompt exactly."},
           "skill_sources": {"type": "array", "maxItems": 20, "items": {
               "type": "object", "additionalProperties": False, "required": ["name", "source"],
               "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"},
                              "source": {"type": "string", "enum": ["client_claimed", "local_workbuddy", "server_guidance"]}}}},
           "mode": {"type": "string", "enum": list(INPUT_MODES), "default": "t2v"},
           "assets": {"type": "object", "properties": {key: IDENTIFIER for key in ASSET_KINDS}, "additionalProperties": False},
           "duration": {"type": "integer", "minimum": 4, "maximum": 15, "default": 15},
           "orientation": {"type": "string", "enum": ["portrait", "landscape"], "default": "portrait"},
           "audio_policy": {"type": "string", "enum": ["native", "reference", "lock_source"], "default": "native"},
           "use_embedded_video_audio": {"type": "boolean", "default": False},
           "width": {"type": "integer", "enum": [480, 864]}, "height": {"type": "integer", "enum": [480, 864]},
           "fps": {"type": "integer", "const": 24}},
          ("operation_id", "original_prompt", "prompt"),
          dependentRequired={"task_id": ["expected_revision"], "expected_revision": ["task_id"]}),
    _tool("h3_confirm_prompt", "Approve only the current immutable context output; never rewrite the draft.",
          {**WRITE_FIELDS, "expected_output_id": IDENTIFIER},
          (*WRITE_FIELDS, "expected_output_id")),
    _tool("h3_start_preview", "Explicitly start preview through Studio/Fleet, or reconcile the existing unknown "
          "execution. Never starts a later stage; both write and generation gates are required.",
          {**WRITE_FIELDS, "expected_output_id": IDENTIFIER, "expected_run_id": RUN_ID},
          (*WRITE_FIELDS, "expected_output_id", "expected_run_id")),
    _tool("h3_list_tasks", "List only projects owned by this connector client, newest first.",
          {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
           "offset": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0}}, readonly=True),
    _tool("h3_get_task", "Read current owned task by exactly one task_id or operation_id. An operation lookup "
          "also returns its original immutable receipt, permitting read-only reconciliation after response loss.",
          {"task_id": IDENTIFIER, "operation_id": IDENTIFIER}, readonly=True,
          oneOf=[{"required": ["task_id"]}, {"required": ["operation_id"]}]),
    _tool("h3_review_preview", "Approve or reject this exact preview output/run. Rejection preserves artifacts "
          "and does not retry or advance automatically.",
          {**WRITE_FIELDS, "output_id": IDENTIFIER, "expected_run_id": IDENTIFIER,
           "decision": {"type": "string", "enum": ["approve", "reject"]},
           "feedback": {"type": "string", "maxLength": 4000}},
          (*WRITE_FIELDS, "output_id", "expected_run_id", "decision")),
    _tool("h3_cancel_task", "Cancel only the exact named run. Owned active runs may be cancelled while writes "
          "are paused. context_ir permits local cancellation of a non-active draft, without generation.",
          {**WRITE_FIELDS, "expected_run_id": RUN_ID,
           "stage_id": {"type": "string", "enum": ["context_ir", "preview"], "default": "preview"}},
          (*WRITE_FIELDS, "expected_run_id"), destructive=True),
]


input_contract = _support("input_contract")
connector_assets = _support("connector_assets")
assert INPUT_MODES == input_contract.MODES and set(ASSET_KINDS) == set(connector_assets.KINDS)


def _validate(value, schema, field="arguments"):
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                _validate(value, alternative, field)
                return
            except HTTPException:
                pass
        raise ConnectorError(400, f"invalid {field}")
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "null": value is None}
    if kind and not valid[kind]:
        raise HTTPException(400, f"invalid {field}")
    if ("enum" in schema and value not in schema["enum"]) or ("const" in schema and value != schema["const"]):
        raise ConnectorError(400, f"unsupported {field}")
    if kind == "object":
        properties = schema["properties"]
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise ConnectorError(400, f"invalid {field} fields")
        if "oneOf" in schema and sum(set(option["required"]) <= set(value) for option in schema["oneOf"]) != 1:
            raise ConnectorError(400, f"invalid {field} alternatives")
        for name, dependencies in schema.get("dependentRequired", {}).items():
            if name in value and set(dependencies) - set(value):
                raise ConnectorError(400, f"invalid {field} dependencies")
        for name, entry in value.items():
            _validate(entry, properties[name], field + "." + name)
    if kind == "array":
        if len(value) > schema["maxItems"]:
            raise ConnectorError(400, f"too many {field}")
        for entry in value:
            _validate(entry, schema["items"], field + " item")
        if schema.get("uniqueItems") and len(value) != len(set(value)):
            raise ConnectorError(400, f"duplicate {field}")
    if kind == "string":
        if (not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 20000)
                or (schema.get("pattern") and not re.search(schema["pattern"], value))):
            raise ConnectorError(400, f"invalid {field}")
        if schema.get("pattern") in {SAFE_ID, REVISION["pattern"]} and not re.fullmatch(schema["pattern"], value):
            raise HTTPException(400, f"invalid {field}")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ConnectorError(400, f"invalid {field} encoding") from error
        if "\x00" in value:
            raise HTTPException(400, f"invalid {field}")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise HTTPException(400, f"invalid {field}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _operation_key(owner, operation_id):
    return "connector:" + _digest([owner, operation_id])


def _enabled(name):
    return os.environ.get(name, "").lower() in {"true", "1"}


def _token(value):
    return value if isinstance(value, str) and re.fullmatch(SAFE_ID, value) else None


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _reason(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_:;-]{0,255}", value) else "admission_unavailable"


def _public_fixture(metadata, output_id, run_id):
    if (not isinstance(metadata, dict) or metadata.get("origin") != "fixture_import"
            or not output_id or not run_id
            or metadata.get("output_id") != output_id or metadata.get("run_id") != run_id):
        return None
    return {key: metadata.get(key) for key in (
        "origin", "label", "generated_in_this_task", "media_validation", "source_sha256", "source_bytes",
        "authorization_reference", "operation_id", "imported_at", "context_output_id", "task_id", "run_id", "output_id")}


def validate_connector_execution(project):
    """Contract.update calls this after mutator(project), before saving or dispatching."""
    if not project.get("connector_owner"):
        return
    if project.get("connector_input_sha256") and input_contract.digest(input_contract.snapshot(project)) != project["connector_input_sha256"]:
        raise RuntimeError("connector inputs changed outside a versioned edit")
    preview = project["stages"]["preview"]
    if preview["status"] in {"queued", "running"}:
        if preview.get("connector_cancelled_before_dispatch"):
            raise RuntimeError("connector preview cancelled before dispatch")
    execution = preview.get("execution") or {}
    if "contract" in execution and project.get("mode") == "t2v":
        binding = execution["contract"]
        if (not isinstance(binding, dict) or binding.get("recipe_id") != project.get("recipe_id")
                or binding.get("recipe_version") != project.get("connector_recipe_version")):
            raise RuntimeError("connector recipe changed before dispatch")
    elif "contract" in execution:
        binding = execution["contract"]
        profile = project.get("execution_profile") or {}
        if (not isinstance(binding, dict) or binding.get("profile_id") != profile.get("profile_id")
                or binding.get("profile_version") != profile.get("version")
                or binding.get("input_sha256") != project.get("connector_input_sha256")):
            raise RuntimeError("connector multimodal inputs changed before dispatch")
        expected = {kind: {key: asset[key] for key in ("asset_id", "sha256", "comfy_name", "size")}
                    for kind, asset in project.get("assets", {}).items()}
        if binding.get("assets") != expected:
            raise RuntimeError("connector multimodal assets changed before dispatch")


def connector_authorized(request):
    """Verify only the dedicated internal secret, for the parent's narrow bypass."""
    authorization = request.headers.getlist("authorization")
    try:
        key_path = os.environ.get("H3_CONNECTOR_KEY_FILE", "")
        with Path(key_path).open(encoding="utf-8") as handle:
            key = handle.read(4097).strip()
    except (OSError, ValueError, UnicodeError):
        key = ""
    return bool(key and len(key) <= 4096 and len(authorization) == 1
                and hmac.compare_digest(authorization[0].encode(), ("Bearer " + key).encode()))


def _authenticate(request):
    if not connector_authorized(request):
        raise ConnectorError(401, "connector authentication required")
    owners = request.headers.getlist("x-h3-connector-owner")
    if len(owners) != 1 or not re.fullmatch(SAFE_ID, owners[0]):
        raise ConnectorError(400, "valid connector owner required")
    return owners[0]


def _json_object(pairs):
    value = {}
    for name, entry in pairs:
        if name in value:
            raise ValueError("duplicate JSON field")
        value[name] = entry
    return value


def _reject_constant(value):
    raise ValueError("invalid JSON constant")


class ConnectorAPI:
    def __init__(self, module, contract):
        if contract.m is not module or contract.store is not module.STORE:
            raise ValueError("connector requires the existing Studio Contract")
        self.module = module
        self.contract = contract
        self.store = module.STORE
        self.skills = importlib.import_module(module.__package__ + ".skill_catalog")
        self.assets = connector_assets.AssetStore(contract)

    def owned(self, task_id, owner):
        project = self.store.get(task_id)
        if not project or not project.get("router_managed") or project.get("connector_owner") != owner:
            raise ConnectorError(404, "task not found")
        return project

    def check_revision(self, project, arguments):
        if arguments["expected_revision"] != _digest(project):
            raise ConnectorError(409, "stale task revision")

    def idle(self, project):
        if any(stage["status"] in {"queued", "running", "scheduled", "cancelling"}
               or stage.get("fleet_pending") for stage in project["stages"].values()):
            raise ConnectorError(409, "task is active or requires reconciliation")
        if self.module._active_batch_for_project(project["id"]):
            raise ConnectorError(409, "task belongs to an active batch")

    def recipe(self, project):
        if project.get("mode") != "t2v":
            profile = input_contract.profile(self.module, project)
            return profile
        if (not self.module.recipe_scope(project, "preview")
                or project.get("recipe_id") not in RECIPE_IDS):
            raise ConnectorError(409, "unsupported recipe scope")
        if not getattr(self.module.COMFY, "is_fleet", False):
            raise ConnectorError(409, "recipe catalog unavailable")
        try:
            recipe = self.module.confirmed_recipe(self.module.recipe_catalog(), project["recipe_id"])
            version = recipe["version"]
            if not re.fullmatch(SAFE_ID, version):
                raise ValueError("invalid recipe version")
            return recipe
        except Exception as error:
            raise ConnectorError(409, "recipe catalog unavailable") from error

    def stage_public(self, name, stage, now):
        status = stage["status"]
        if stage.get("fleet_pending") and stage.get("submission_unknown"):
            status = "needs_reconciliation"
        elif stage.get("cancel_requested") and status in {"queued", "running"}:
            status = "cancelling"
        review = stage.get("connector_review", {})
        if (status == "awaiting_approval" and review.get("decision") == "reject"
                and review.get("output_id") == stage.get("output_id")):
            status = "rejected"
        execution = stage.get("execution") or {}
        binding = execution.get("contract") or {}
        result = {"id": name, "status": status,
                  **{key: stage.get(key) for key in ("progress", "run_id", "output_id", "execution_id",
                                                       "queued_at", "started_at", "finished_at", "approved_at")},
                  "artifact_id": stage.get("output_id"),
                  "recipe_id": execution.get("recipe_id", binding.get("recipe_id")),
                  "recipe_version": execution.get("recipe_version", binding.get("recipe_version")),
                  "submission_unknown": bool(stage.get("submission_unknown")),
                  "reconciliation_required": bool(stage.get("fleet_pending"))}
        for key in ("backend_id", "runtime_version", "gpu_uuid", "lane_id"):
            result[key] = _token(execution.get(key, binding.get(key, stage.get(key))))
        result["gpu_id"] = result["gpu_uuid"]
        result["execution_seconds"] = _number(execution.get("execution_seconds", stage.get("execution_seconds")))
        result["phase"] = _token(execution.get("phase"))
        result["model_state"] = _token(execution.get("model_state"))
        result["phase_timings"] = {key: _number(value) for key, value in (execution.get("phase_timings") or {}).items()
                                   if key in {"model_preparing", "input_preparing", "sampling", "decoding", "saving"}}
        reason = stage.get("admission_reason", execution.get("admission_reason"))
        result["admission_reason"] = _reason(reason) if reason else None
        for label, start, stop in (("elapsed_seconds", "started_at", "finished_at"),
                                   ("queue_seconds", "queued_at", "started_at")):
            began = stage.get(start)
            ended = stage.get(stop) or stage.get("finished_at") or now
            result[label] = (round(max(0, ended - began), 3)
                             if isinstance(began, (int, float)) and math.isfinite(began) else None)
        if stage.get("error"):
            result["error_code"] = "outcome_unknown" if stage.get("fleet_pending") else "stage_failed"
        if fixture := _public_fixture(stage.get("fixture_import"), stage.get("output_id"), stage.get("run_id")):
            result["fixture_import"] = fixture
        return result

    def public(self, project):
        now = time.time()
        stages = {name: self.stage_public(name, project["stages"][name], now)
                  for name in ("context_ir", "preview")}
        context, preview = stages["context_ir"], stages["preview"]
        if preview["status"] != "pending":
            status = {"approved": "completed", "awaiting_approval": "awaiting_preview_approval"}.get(
                preview["status"], preview["status"])
        else:
            status = {"approved": "ready", "awaiting_approval": "awaiting_prompt_approval"}.get(
                context["status"], context["status"])
        history = []
        for entry in project.get("connector_history", []):
            history.append({**{key: entry.get(key) for key in (
                "id", "revision", "saved_at", "name", "prompt", "recipe_id", "recipe_version", "seed",
                "verbatim", "skill_sources", "context_output_id", "input_snapshot")},
                "stages": {name: self.stage_public(name, stage, now)
                           for name, stage in entry["stages"].items() if name in stages}})
        return {"id": project["id"], "task_id": project["id"], "revision": _digest(project),
                "name": project["name"], "original_prompt": project["prompt_original"],
                "prompt": project["prompt_ir"], "prompt_approved": project["prompt_approved"],
                "recipe_id": project.get("recipe_id"), "recipe_version": project.get("connector_recipe_version"),
                "mode": project["mode"], "input_assets": input_contract.public_assets(project),
                "input_sha256": project.get("connector_input_sha256"), "execution_profile": project.get("execution_profile"),
                "seed": project["seed"], "verbatim": project.get("connector_verbatim", False),
                "scope": {**FIXED_SCOPE, **{key: project[key] for key in ("mode", "duration", "orientation", "audio_policy")},
                          "width": 480 if project["orientation"] == "portrait" else 864,
                          "height": 864 if project["orientation"] == "portrait" else 480}, "actual_duration": project["actual_duration"],
                "context_output_id": context["output_id"], "status": status,
                "stages": stages, "pipeline": list(stages.values()), "preview": preview,
                "elapsed_seconds": preview["elapsed_seconds"], "queue_seconds": preview["queue_seconds"],
                "created_at": project["created_at"], "updated_at": project["updated_at"],
                "skill_sources": project.get("connector_skill_sources", []),
                "history": history, "reviews": project.get("connector_reviews", []),
                "outputs": [{"id": output["id"], "artifact_id": output["id"],
                             "stage_id": output["stage"], "run_id": output.get("run_id"),
                             "content_type": output["content_type"],
                             **({"fixture_import": _public_fixture(output["fixture_import"], output["id"], output.get("run_id"))}
                                if output.get("fixture_import") else {})}
                            for output in project.get("router_outputs", {}).values()
                            if output.get("stage") in stages]}

    def capabilities(self):
        recipes = []
        profiles = []
        unavailable_profiles = []
        for identifier in RECIPE_IDS:
            recipes.append({"recipe_id": identifier, "version": None, "catalog_confirmed": False})
        if getattr(self.module.COMFY, "is_fleet", False):
            try:
                catalog = self.module.recipe_catalog()
                profiles = [{key: profile.get(key) for key in ("profile_id", "version", "mode", "family", "audio_policy", "sampling", "qualified", "qualification", "execution_specs")}
                            for profile in catalog.get("multimodal_profiles", []) if isinstance(profile, dict)]
                unavailable_profiles = [{key: profile.get(key) for key in ("profile_id", "mode", "reason")}
                                        for profile in catalog.get("unavailable_multimodal_profiles", []) if isinstance(profile, dict)]
                for entry in recipes:
                    recipe = self.module.confirmed_recipe(catalog, entry["recipe_id"])
                    if re.fullmatch(SAFE_ID, recipe["version"]):
                        entry.update(version=recipe["version"], catalog_confirmed=True)
                        entry["trigger"] = (recipe.get("trigger") if isinstance(recipe.get("trigger"), str)
                                            and len(recipe["trigger"]) <= 256 else None)
                        entry["people_lora_strength"] = _number(recipe.get("people_lora_strength"))
                        entry["sampling"] = {key: _number(recipe.get("sampling", {}).get(key))
                                             for key in ("steps", "shift_audio", "shift_video")}
            except Exception:
                pass
        capacity = self.capacity(recipes + [{"recipe_id": profile["profile_id"], "version": profile["version"],
                                           "catalog_confirmed": profile.get("qualified") is True} for profile in profiles])
        for profile in profiles:
            profile["capacity"] = capacity["recipe_capacity"].get(profile["profile_id"])
        return {"contract_version": 2, "scope": dict(FIXED_SCOPE), "recipes": recipes,
                "input_modes": [{"mode": mode, "required_assets": sorted(input_contract.REQUIRED.get(mode, set())),
                                 "allowed_assets": sorted(input_contract.ALLOWED[mode]),
                                 "profiles": [profile for profile in profiles if profile.get("mode") == mode],
                                 "unavailable_profiles": [profile for profile in unavailable_profiles if profile.get("mode") == mode],
                                 "qualification": "fleet_confirmation_required" if mode != "t2v" else "historical_single_lane"}
                                for mode in input_contract.MODES],
                "asset_upload": {"transport": "authenticated_stream", "max_bytes": connector_assets.MAX_BYTES,
                                 "account_quota_bytes": connector_assets.OWNER_QUOTA, "local_paths_are_not_uploads": True,
                                 "reference_video": {"model_input_fps": 24, "constant_frame_rate_required": True,
                                     "automatic_conversion": False, "original_preserved": True,
                                     "decode_budget": "server_verified_input_dimensions_and_frame_count"}},
                "capacity": capacity, "skill_catalog": self.skill_catalog(),
                "writes_enabled": _enabled("H3_CONNECTOR_WRITES_ENABLED"),
                "generation_enabled": _enabled("H3_CONNECTOR_GENERATION_ENABLED"),
                "inference_validated": False, "context_ir": "synchronous_manual_no_model_calls",
                "stage_outputs": "immutable", "revision": "opaque_sha256",
                "execution": "existing_studio_fleet", "stages": ["context_ir", "preview"],
                "active_run_cancellation_when_paused": True}

    def capacity(self, recipes):
        try:
            raw = self.module.COMFY.capacity() if getattr(self.module.COMFY, "is_fleet", False) else {}
        except Exception:
            raw = {}
        available = isinstance(raw, dict) and raw.get("available") is True
        if not available:
            raw = {}
        capacities = raw.get("recipe_capacity") or {}
        matched = {}
        for recipe in recipes:
            entry = capacities.get(recipe["recipe_id"], {})
            confirmed = (available and recipe["catalog_confirmed"]
                         and entry.get("recipe_version") == recipe["version"]
                         and type(entry.get("available_slots")) is int and entry["available_slots"] >= 0)
            matched[recipe["recipe_id"]] = {
                "available_slots": entry["available_slots"] if confirmed else 0,
                "recipe_version": recipe["version"], "confirmed": confirmed,
                "eligible_lanes": [identifier for identifier in entry.get("eligible_lanes", []) if _token(identifier)]
                if confirmed else [],
                "waiting_reasons": [_reason(reason) for reason in entry.get("reasons", [])]
                if confirmed else ["capacity_unavailable_or_recipe_version_mismatch"]}
            recipe.update(matched[recipe["recipe_id"]])
        return {"available": available, "sampled_at": _number(raw.get("sampled_at")),
                "active": _number(raw.get("active")), "queued": _number(raw.get("queued")),
                "resources_ok": raw.get("resources_ok") is True,
                "recipe_capacity": matched,
                "lanes": [{"id": _token(lane.get("id")), "status": lane.get("status")
                           if lane.get("status") in {"idle", "busy", "unknown"} else "unknown"}
                          for lane in raw.get("lanes", []) if isinstance(lane, dict)],
                "idle_lanes_are_not_available_slots": True}

    def skill_catalog(self):
        allowed = {identifier for identifier, name, profile, aliases, description in self.skills.GROUPS
                   if profile in {"h3", "cinematography"} or (profile and profile.startswith("creative:"))}
        return [{key: entry[key] for key in ("id", "name", "description", "execution_kind", "ruleset_sha256", "baseline")}
                for entry in self.skills.catalog() if entry["available"] and entry["id"] in allowed]

    def prompt_guidance(self, arguments):
        catalog = self.skill_catalog()
        identifiers = arguments.get("skill_ids", ["tvc-video"])
        if any(identifier not in {entry["id"] for entry in catalog} for identifier in identifiers):
            raise ConnectorError(400, "unsupported T2V skill selection")
        selected = self.skills.selected_skills(identifiers)
        rules = copy.deepcopy(self.skills.rules_for(selected))
        return {"source": "server_guidance", "execution_kind": "distilled_prompt_guidance",
                "execution_verified": False, "model_calls": 0, "scope": dict(FIXED_SCOPE),
                "available_skills": catalog, "selected_skills": [entry for entry in catalog
                    if entry["id"] in {skill["id"] for skill in selected}],
                "rules": rules, "ruleset_sha256": self.skills.RULESET_HASH,
                "verbatim_policy": "verbatim=true preserves exact input and takes precedence over rewriting guidance",
                "provenance_policy": "client Skill claims are recorded but not verified as executed"}

    def save_draft(self, owner, arguments):
        project = self.owned(arguments["task_id"], owner) if "task_id" in arguments else None
        if project:
            self.check_revision(project, arguments)
            self.idle(project)
            if arguments["original_prompt"] != project["prompt_original"]:
                raise ConnectorError(409, "original prompt is immutable")
        verbatim = arguments.get("verbatim", project.get("connector_verbatim", False) if project else False)
        if verbatim and arguments["prompt"] != arguments["original_prompt"]:
            raise ConnectorError(400, "verbatim prompt must equal original_prompt exactly")
        inputs = {**FIXED_SCOPE, "use_embedded_video_audio": False,
                  **({key: project.get(key) for key in ("mode", "duration", "orientation", "audio_policy", "use_embedded_video_audio")} if project else {})}
        inputs.update({key: arguments[key] for key in inputs if key in arguments})
        expected_shape = (480, 864) if inputs["orientation"] == "portrait" else (864, 480)
        if any(arguments.get(key, expected) != expected for key, expected in zip(("width", "height"), expected_shape)):
            raise ConnectorError(400, "dimensions do not match orientation")
        assets = self.assets.bind(owner, arguments["assets"]) if "assets" in arguments else copy.deepcopy(project.get("assets", {}) if project else {})
        inputs["assets"] = assets
        if "preview_recipe_id" in arguments:
            inputs["preview_recipe_id"] = arguments["preview_recipe_id"]
        elif project and project.get("preview_recipe_id"):
            inputs["preview_recipe_id"] = project["preview_recipe_id"]
        if inputs.get("preview_recipe_id") and inputs["mode"] != "i2v":
            raise ConnectorError(400, "accelerated first-frame recipe requires i2v")
        input_contract.validate_media(inputs)
        previous_recipe = project.get("recipe_id") if project and project["mode"] == inputs["mode"] else None
        recipe_id = arguments.get("recipe_id", previous_recipe)
        if inputs["mode"] == "t2v":
            recipe_id = recipe_id or "A4"
            if any(inputs[key] != value for key, value in FIXED_SCOPE.items() if key not in {"width", "height"}):
                raise ConnectorError(400, "four recipes require 15s portrait native audio T2V")
        elif recipe_id is not None:
            raise ConnectorError(400, "multimodal inputs require a dedicated workflow, not an A4/B8 label")
        recipe = self.recipe({**inputs, "recipe_id": recipe_id})
        recipe_version = recipe["version"]
        if verbatim and recipe.get("trigger") and not arguments["prompt"].startswith(recipe["trigger"]):
            raise ConnectorError(400, "verbatim input must already contain the selected recipe trigger")
        if not project:
            created = self.module.create_project(
                name=arguments.get("name", "H3 preview"), mode="t2v", strategy="fast",
                prompt=arguments["original_prompt"], duration=inputs["duration"], orientation=inputs["orientation"],
                prompt_processing="manual", seed=arguments.get("seed", -1), audio_policy="native",
                watermark=False, use_embedded_video_audio=False, recipe_id=recipe_id,
                script_plan_id=None, script_plan_revision=None, first_frame=None, last_frame=None,
                reference_image=None, reference_video=None, reference_audio=None)
            project = self.store.get(created["id"])

        def mutate(item):
            if item.get("connector_owner"):
                item.setdefault("connector_history", []).append({
                    "id": "history_" + uuid4().hex, "revision": _digest(item), "saved_at": time.time(),
                    "name": item["name"], "prompt": item["prompt_ir"], "seed": item["seed"],
                    "recipe_id": item.get("recipe_id"), "recipe_version": item.get("connector_recipe_version"),
                    "input_snapshot": input_contract.snapshot(item),
                    "verbatim": item.get("connector_verbatim", False),
                    "skill_sources": copy.deepcopy(item.get("connector_skill_sources", [])),
                    "context_output_id": item["stages"]["context_ir"].get("output_id"),
                    "stages": copy.deepcopy(item["stages"])})
                for name, stage in item["stages"].items():
                    if stage.get("output_id") or stage.get("run_id"):
                        item.setdefault("stage_history", []).append({
                            **copy.deepcopy(stage), "id": uuid4().hex, "stage_id": name,
                            "recipe_id": item.get("recipe_id"), "seed": item["seed"]})
            item.update(router_managed=True, connector_owner=owner, recipe_id=recipe_id,
                        connector_recipe_version=recipe_version, connector_verbatim=verbatim,
                        prompt_original=arguments["original_prompt"], prompt_ir=arguments["prompt"],
                        prompt_approved="", prompt_processing="manual")
            item.update({key: inputs[key] for key in ("mode", "duration", "orientation", "audio_policy", "use_embedded_video_audio", "assets")})
            item["actual_duration"] = self.module.actual_duration(inputs["duration"])
            item["execution_profile"] = recipe if inputs["mode"] != "t2v" else None
            if inputs.get("preview_recipe_id"):
                item["preview_recipe_id"] = inputs["preview_recipe_id"]
            self.module.validate_project_config(item)
            if "name" in arguments:
                item["name"] = arguments["name"]
            if "seed" in arguments and arguments["seed"] >= 0:
                item["seed"] = arguments["seed"]
            elif "seed" in arguments and "task_id" in arguments:
                raise ConnectorError(400, "draft edits require an explicit nonnegative seed")
            if "skill_sources" in arguments:
                item["connector_skill_sources"] = [
                    {**entry, "reported_by": "client", "execution_verified": False}
                    for entry in arguments["skill_sources"]]
            item["stages"] = {name: self.module._new_stage() for name in self.module.STAGE_IDS}
            item["stages"]["context_ir"].update(
                status="awaiting_approval", progress=100, prompt_processing="manual",
                run_id="run_" + uuid4().hex, finished_at=time.time())
            item["connector_input_sha256"] = input_contract.digest(input_contract.snapshot(item))

        return self.contract.update(project["id"], mutate)

    def confirm_prompt(self, project, arguments):
        self.idle(project)
        if project.get("assets"):
            self.assets.bind(project["connector_owner"], {kind: asset["asset_id"] for kind, asset in project["assets"].items()})
        context = project["stages"]["context_ir"]
        output = project.get("router_outputs", {}).get(arguments["expected_output_id"], {})
        if (context["status"] != "awaiting_approval" or context.get("output_id") != output.get("id")
                or output.get("stage") != "context_ir" or output.get("text") != project["prompt_ir"]):
            raise ConnectorError(409, "stale context output")
        self.module.approve_context_ir(project["id"], {"prompt": project["prompt_ir"]})
        return self.contract.update(project["id"], lambda item: item.update(prompt_approved=project["prompt_ir"]))

    def start_preview(self, project, arguments):
        if not _enabled("H3_CONNECTOR_GENERATION_ENABLED"):
            raise ConnectorError(403, "connector generation disabled")
        if project.get("connector_fixture_import"):
            raise ConnectorError(409, "fixture acceptance tasks cannot generate; create a new task")
        context, preview = project["stages"]["context_ir"], project["stages"]["preview"]
        if (context["status"] != "approved" or context.get("output_id") != arguments["expected_output_id"]
                or project["prompt_approved"] != project["prompt_ir"]):
            raise ConnectorError(409, "stale approved context output")
        if preview.get("run_id") != arguments["expected_run_id"]:
            raise ConnectorError(409, "stale preview run")
        if any(stage["status"] in {"queued", "running", "scheduled", "cancelling"}
               for stage in project["stages"].values()):
            raise ConnectorError(409, "task is already active")
        if not getattr(self.module.COMFY, "is_fleet", False):
            raise ConnectorError(409, "Fleet required")
        validate_connector_execution(project)
        input_contract.validate_execution_media(project)
        if project.get("assets"):
            self.assets.bind(project["connector_owner"], {kind: asset["asset_id"] for kind, asset in project["assets"].items()})
            input_contract.quality_graph(self.module, project)
            profile = project["execution_profile"]
            registered = self.module.recipe_catalog().get("multimodal_profiles", [])
            if project["orientation"] != "portrait" or project["duration"] != 15:
                raise ConnectorError(409, "当前多素材执行只接受15秒竖版；其他规格尚未取得执行证据，没有修改输入或提交生成")
            if not any(entry.get("profile_id") == profile["profile_id"] and entry.get("version") == profile["version"]
                       and (entry.get("qualified") is True or project["id"] in entry.get("acceptance_tasks", []))
                       for entry in registered):
                raise ConnectorError(409, "专用多素材运行配置尚未通过准入资格；没有提交生成")
        if not preview.get("fleet_pending"):
            if self.recipe(project)["version"] != project.get("connector_recipe_version"):
                raise ConnectorError(409, "recipe version changed; save and confirm a new draft")
        elif not preview.get("execution_id"):
            raise ConnectorError(409, "unknown execution requires reconciliation")
        options = {"new_seed": False}
        if project["mode"] == "t2v":
            options["recipe_id"] = project["recipe_id"]
        self.module.start_stage(project["id"], "preview", options)
        return self.store.get(project["id"])

    def review_preview(self, project, arguments):
        self.idle(project)
        preview = project["stages"]["preview"]
        output = project.get("router_outputs", {}).get(arguments["output_id"], {})
        if (preview["status"] != "awaiting_approval" or preview.get("output_id") != arguments["output_id"]
                or preview.get("run_id") != arguments["expected_run_id"] or output.get("stage") != "preview"
                or output.get("run_id") != arguments["expected_run_id"]):
            raise ConnectorError(409, "stale preview output or run")
        if arguments["decision"] == "approve":
            self.module.approve_stage(project["id"], "preview")

        def mutate(item):
            review = {"output_id": arguments["output_id"], "run_id": arguments["expected_run_id"],
                      "decision": arguments["decision"], "feedback": arguments.get("feedback", ""),
                      "reviewed_at": time.time(), "revision": arguments["expected_revision"]}
            item.setdefault("connector_reviews", []).append(review)
            item["stages"]["preview"]["connector_review"] = review
            if arguments["decision"] == "reject":
                item["stages"]["preview"].update(approved_at=None, detail="预览已拒绝；产物保留，未自动重试。")
        return self.contract.update(project["id"], mutate)

    def cancel_task(self, project, arguments):
        stage_id = arguments.get("stage_id", "preview")
        stage = project["stages"][stage_id]
        if stage.get("run_id") != arguments["expected_run_id"]:
            raise ConnectorError(409, "stale cancellation run")
        if stage["status"] in {"queued", "running"}:
            self.module.cancel_stage(project["id"], stage_id)
            if stage_id == "preview" and stage["status"] == "queued" and not stage.get("execution_id"):
                self.contract.update(project["id"], lambda item: item["stages"][stage_id].update(
                    status="cancelled", connector_cancelled_before_dispatch=True, finished_at=time.time()))
            return self.store.get(project["id"])
        if stage_id != "context_ir":
            raise ConnectorError(409, "no active run to cancel")
        if not _enabled("H3_CONNECTOR_WRITES_ENABLED"):
            raise ConnectorError(403, "connector writes disabled")
        self.idle(project)
        if (stage["status"] not in {"awaiting_approval", "approved"}
                or project["stages"]["preview"]["status"] != "pending"):
            raise ConnectorError(409, "no draft to cancel")

        def mutate(item):
            item["prompt_approved"] = ""
            item["stages"]["context_ir"].update(status="cancelled", approved_at=None, finished_at=time.time())
        return self.contract.update(project["id"], mutate)

    def call(self, tool, arguments, owner):
        definition = next((entry for entry in TOOL_DEFINITIONS if entry["name"] == tool), None)
        if definition is None:
            raise ConnectorError(404, "tool not found")
        _validate(arguments, definition["inputSchema"])
        if tool == "h3_capabilities":
            return self.capabilities()
        if tool == "h3_prompt_guidance":
            return self.prompt_guidance(arguments)
        if tool == "h3_get_task":
            with self.contract.connect() as database:
                if "task_id" in arguments:
                    return self.public(self.owned(arguments["task_id"], owner))
                row = database.execute("SELECT result_json FROM router_operations WHERE operation_id = ?",
                                       (_operation_key(owner, arguments["operation_id"]),)).fetchone()
                if not row:
                    raise ConnectorError(404, "operation not found")
                receipt = json.loads(row[0])
                current = self.public(self.owned(receipt["id"], owner))
                current["receipt"] = {"operation_id": arguments["operation_id"],
                                      "result_revision": receipt["revision"], "task_id": receipt["id"],
                                      "result": receipt}
                return current
        if tool == "h3_list_tasks":
            limit, offset = arguments.get("limit", 20), arguments.get("offset", 0)
            with self.contract.connect() as database:
                rows = database.execute(
                    "SELECT data_json FROM projects WHERE json_extract(data_json, '$.connector_owner') = ? "
                    "AND json_extract(data_json, '$.router_managed') = 1 "
                    "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", (owner, limit + 1, offset)).fetchall()
            return {"tasks": [self.public(json.loads(row[0])) for row in rows[:limit]],
                    "next_offset": offset + limit if len(rows) > limit else None}

        key = _operation_key(owner, arguments["operation_id"])
        digest = _digest([tool, arguments])
        task_id = arguments.get("task_id")

        def mutate():
            if tool != "h3_cancel_task" and not _enabled("H3_CONNECTOR_WRITES_ENABLED"):
                raise ConnectorError(403, "connector writes disabled")
            if tool == "h3_save_draft":
                return self.public(self.save_draft(owner, arguments))
            project = self.owned(task_id, owner)
            self.check_revision(project, arguments)
            handlers = {"h3_confirm_prompt": self.confirm_prompt, "h3_start_preview": self.start_preview,
                        "h3_review_preview": self.review_preview, "h3_cancel_task": self.cancel_task}
            return self.public(handlers[tool](project, arguments))

        with self.contract.connect():
            if task_id:
                self.owned(task_id, owner)
            result = self.contract.operation(key, digest, task_id, mutate)
            self.owned(result["id"], owner)
            return result

    def output(self, task_id, output_id, owner):
        _validate(task_id, IDENTIFIER, "task_id")
        _validate(output_id, IDENTIFIER, "output_id")
        artifact = self.owned(task_id, owner).get("router_outputs", {}).get(output_id)
        if not artifact or artifact.get("id") != output_id:
            raise ConnectorError(404, "output not found")
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        if artifact.get("stage") == "context_ir" and "text" in artifact:
            return PlainTextResponse(artifact["text"], headers=headers)
        if artifact.get("stage") != "preview" or not artifact.get("path"):
            raise HTTPException(404, "output not found")
        path = Path(artifact["path"])
        root = (self.module._project_dir(task_id) / "router-outputs").resolve()
        expected_root = (self.module.SETTINGS.data_root / "projects" / task_id / "router-outputs").absolute()
        if (root != expected_root or path.is_symlink() or not path.resolve().is_relative_to(root)
                or path.name != output_id + ".mp4" or not path.is_file()):
            raise HTTPException(404, "output not found")
        return FileResponse(path, media_type="video/mp4", headers=headers)

    def browser_output(self, task_id, output_id):
        _validate(task_id, IDENTIFIER, "task_id")
        project = self.store.get(task_id)
        owner = project.get("connector_owner") if project else None
        if not isinstance(owner, str) or not re.fullmatch(SAFE_ID, owner):
            raise HTTPException(404, "task not found")
        return self.output(task_id, output_id, owner)


def install_connector_api(module, contract):
    """Called by router_contract.install(module), before the frontend mount.

    The parent must restrict access.py's connector-secret bypass to API_PREFIX
    and reject connector_owner in the old router managed() mutation boundary.
    This function neither creates nor initializes another Contract.
    """
    connector = ConnectorAPI(module, contract)
    module.app.state.h3_connector = connector
    api = APIRouter(prefix=API_PREFIX)
    connector_assets.install_routes(api, connector.assets, _authenticate, lambda: _enabled("H3_CONNECTOR_WRITES_ENABLED"))

    @api.post("/call/{tool}")
    async def call(tool: str, request: Request):
        owner = _authenticate(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256 * 1024:
                raise ConnectorError(413, "connector request too large")
        try:
            arguments = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ConnectorError(400, "invalid JSON arguments") from error
        try:
            return await run_in_threadpool(connector.call, tool, arguments, owner)
        except HTTPException as error:
            if isinstance(error, ConnectorError) or error.detail in {"operation key conflict", "operation_id is required"}:
                raise
            raise ConnectorError(error.status_code, "Studio rejected the operation") from error
        except Exception as error:
            raise ConnectorError(503, "Studio operation unavailable; reconcile task and operation_id before retry") from error

    @api.api_route("/tasks/{task_id}/outputs/{output_id}", methods=["GET", "HEAD"])
    def output(task_id: str, output_id: str, request: Request):
        return connector.output(task_id, output_id, _authenticate(request))

    module.app.include_router(api)

    @module.app.api_route("/api/projects/{task_id}/connector-outputs/{output_id}", methods=["GET", "HEAD"])
    def browser_output(task_id: str, output_id: str):
        return connector.browser_output(task_id, output_id)

    @module.app.api_route("/api/projects/{task_id}/input-assets/{kind}", methods=["GET", "HEAD"])
    def browser_input(task_id: str, kind: str):
        project = module.STORE.get(task_id)
        if not project or kind not in connector_assets.KINDS or kind not in project.get("assets", {}):
            raise HTTPException(404, "asset not found")
        asset = project["assets"][kind]
        if project.get("connector_owner"):
            asset = connector.assets.bind(project["connector_owner"], {kind: asset["asset_id"]})[kind]
        else:
            path = Path(asset["path"])
            if path.is_symlink() or not path.resolve().is_relative_to(module._project_dir(task_id).resolve() / "assets"):
                raise HTTPException(404, "asset not found")
        return FileResponse(asset["path"], media_type=asset["mime"], headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    return connector
