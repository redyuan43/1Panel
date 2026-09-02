from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .config import Registry, endpoint_from_dict
from .errors import RouterError
from .store import StateStore
from .types import Endpoint, EndpointStatus


REVISION_KEY = "router:endpoint-config:revision"
UPDATE_LOCK_KEY = "router:endpoint-config:update-lock"
CONFIG_FIELDS = {
    "safe_context_tokens",
    "configured_context_tokens",
    "max_concurrency",
    "tasks",
    "modalities",
    "capabilities",
}
OPERATION_FIELDS = {"enabled", "auto_candidate"}


class EndpointConfigManager:
    def __init__(
        self,
        store: StateStore,
        base_registry: Registry,
    ) -> None:
        self.store = store
        self.base_registry = base_registry

    async def revision(self) -> int:
        value = await self.store.get_json(REVISION_KEY)
        return int((value or {}).get("revision", 0))

    async def effective_registry(self) -> Registry:
        endpoints = []
        for base in self.base_registry.endpoints:
            active = await self.store.get_json(_active_key(base.id))
            values = (
                active.get("values", {})
                if isinstance(active, dict)
                else {}
            )
            endpoints.append(_apply_values(base, values))
        return self.base_registry.with_endpoints(endpoints)

    async def records(self) -> dict[str, dict[str, Any]]:
        revision = await self.revision()
        result = {}
        for base in self.base_registry.endpoints:
            active = await self.store.get_json(_active_key(base.id))
            draft = await self.store.get_json(_draft_key(base.id))
            effective = _apply_values(
                base,
                active.get("values", {})
                if isinstance(active, dict)
                else {},
            )
            result[base.id] = {
                "revision": revision,
                "has_override": bool(active),
                "effective": _editable_values(effective),
                "draft": draft,
                "baseline": _editable_values(base),
            }
        return result

    async def save_draft(
        self,
        endpoint_id: str,
        changes: dict[str, Any],
        *,
        expected_revision: int | None,
        source: str,
    ) -> dict[str, Any]:
        base = self._base_endpoint(endpoint_id)
        unknown = sorted(set(changes) - CONFIG_FIELDS)
        if unknown:
            raise _invalid(
                "endpoint draft contains immutable or unknown fields: "
                + ", ".join(unknown)
            )

        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            active = await self.store.get_json(_active_key(endpoint_id))
            draft = await self.store.get_json(_draft_key(endpoint_id))
            current = _config_values(
                _apply_values(
                    base,
                    active.get("values", {})
                    if isinstance(active, dict)
                    else {},
                )
            )
            if isinstance(draft, dict):
                current.update(draft.get("values", {}))
            current.update(changes)
            normalized = _validated_config(base, current)
            record = {
                "endpoint_id": endpoint_id,
                "values": normalized,
                "validation": {
                    "status": "pending",
                    "checked_at": None,
                    "errors": [],
                    "draft_hash": _value_hash(normalized),
                },
                "updated_at": time.time(),
                "updated_by": source,
            }
            await self.store.set_json(_draft_key(endpoint_id), record)
            await self._set_revision(revision + 1)
            record["revision"] = revision + 1
            return record

    async def validate_draft(
        self,
        endpoint_id: str,
        *,
        expected_revision: int | None,
        status: EndpointStatus,
        source: str,
    ) -> dict[str, Any]:
        base = self._base_endpoint(endpoint_id)
        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            draft = await self.store.get_json(_draft_key(endpoint_id))
            if not draft:
                raise RouterError(
                    "endpoint has no draft configuration",
                    status_code=409,
                    code="endpoint_draft_missing",
                )
            values = _validated_config(base, draft.get("values", {}))
            errors = []
            if not status.healthy:
                errors.append("endpoint_health_check_failed")
            validation = {
                "status": "passed" if not errors else "failed",
                "checked_at": time.time(),
                "errors": errors,
                "draft_hash": _value_hash(values),
                "health": {
                    "healthy": status.healthy,
                    "checked_at": status.checked_at,
                },
            }
            draft.update(
                {
                    "values": values,
                    "validation": validation,
                    "updated_at": time.time(),
                    "updated_by": source,
                }
            )
            await self.store.set_json(_draft_key(endpoint_id), draft)
            await self._set_revision(revision + 1)
            return {
                **draft,
                "revision": revision + 1,
            }

    async def activate(
        self,
        endpoint_id: str,
        *,
        expected_revision: int | None,
        source: str,
    ) -> dict[str, Any]:
        self._base_endpoint(endpoint_id)
        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            draft = await self.store.get_json(_draft_key(endpoint_id))
            if not draft:
                raise RouterError(
                    "endpoint has no draft configuration",
                    status_code=409,
                    code="endpoint_draft_missing",
                )
            validation = draft.get("validation", {})
            values = draft.get("values", {})
            if (
                validation.get("status") != "passed"
                or validation.get("draft_hash") != _value_hash(values)
            ):
                raise RouterError(
                    "endpoint draft has not passed validation",
                    status_code=409,
                    code="endpoint_not_validated",
                )
            current = await self.store.get_json(_active_key(endpoint_id))
            active_values = (
                dict(current.get("values", {}))
                if isinstance(current, dict)
                else {}
            )
            active_values.update(values)
            record = {
                "endpoint_id": endpoint_id,
                "values": active_values,
                "validation": validation,
                "updated_at": time.time(),
                "updated_by": source,
            }
            await self.store.set_json(_active_key(endpoint_id), record)
            await self.store.delete(_draft_key(endpoint_id))
            await self._set_revision(revision + 1)
            return {
                **record,
                "revision": revision + 1,
            }

    async def discard_draft(
        self,
        endpoint_id: str,
        *,
        expected_revision: int | None,
    ) -> int:
        self._base_endpoint(endpoint_id)
        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            await self.store.delete(_draft_key(endpoint_id))
            await self._set_revision(revision + 1)
            return revision + 1

    async def action(
        self,
        endpoint_id: str,
        action: str,
        *,
        expected_revision: int | None,
        source: str,
    ) -> dict[str, Any]:
        base = self._base_endpoint(endpoint_id)
        actions = {
            "enable": ("enabled", True),
            "disable": ("enabled", False),
            "auto-enable": ("auto_candidate", True),
            "auto-disable": ("auto_candidate", False),
        }
        if action not in actions:
            raise RouterError(
                f"unsupported endpoint action: {action}",
                status_code=400,
                code="endpoint_action_invalid",
            )
        field, value = actions[action]
        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            current = await self.store.get_json(_active_key(endpoint_id))
            values = (
                dict(current.get("values", {}))
                if isinstance(current, dict)
                else {}
            )
            effective = _apply_values(base, values)
            if action == "auto-enable":
                if not effective.enabled:
                    raise RouterError(
                        "disabled endpoint cannot join automatic routing",
                        status_code=409,
                        code="endpoint_disabled",
                    )
                if (
                    "unverified"
                    in effective.capabilities.validation_status.lower()
                ):
                    raise RouterError(
                        "endpoint capabilities are not validated",
                        status_code=409,
                        code="endpoint_not_validated",
                    )
            values[field] = value
            record = {
                "endpoint_id": endpoint_id,
                "values": values,
                "validation": (
                    current.get("validation", {})
                    if isinstance(current, dict)
                    else {}
                ),
                "updated_at": time.time(),
                "updated_by": source,
            }
            await self.store.set_json(_active_key(endpoint_id), record)
            await self._set_revision(revision + 1)
            return {
                **record,
                "revision": revision + 1,
            }

    async def reset(
        self,
        endpoint_id: str,
        *,
        expected_revision: int | None,
    ) -> int:
        self._base_endpoint(endpoint_id)
        async with _UpdateLock(self.store):
            revision = await self._check_revision(expected_revision)
            await self.store.delete(_active_key(endpoint_id))
            await self.store.delete(_draft_key(endpoint_id))
            await self._set_revision(revision + 1)
            return revision + 1

    def _base_endpoint(self, endpoint_id: str) -> Endpoint:
        endpoint = self.base_registry.by_id(endpoint_id)
        if endpoint is None:
            raise RouterError(
                f"unknown endpoint: {endpoint_id}",
                status_code=404,
                code="endpoint_not_found",
            )
        return endpoint

    async def _check_revision(
        self,
        expected_revision: int | None,
    ) -> int:
        revision = await self.revision()
        if (
            expected_revision is not None
            and expected_revision != revision
        ):
            raise RouterError(
                "endpoint configuration changed; reload and retry",
                status_code=409,
                code="endpoint_revision_conflict",
                details={
                    "expected_revision": expected_revision,
                    "current_revision": revision,
                },
            )
        return revision

    async def _set_revision(self, revision: int) -> None:
        await self.store.set_json(
            REVISION_KEY,
            {"revision": revision, "updated_at": time.time()},
        )


class _UpdateLock:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.token = uuid4().hex

    async def __aenter__(self) -> None:
        if not await self.store.acquire_lock(
            UPDATE_LOCK_KEY,
            self.token,
            10,
        ):
            raise RouterError(
                "endpoint configuration is being updated",
                status_code=409,
                code="endpoint_update_busy",
            )

    async def __aexit__(self, *_args: Any) -> None:
        await self.store.release_lock(UPDATE_LOCK_KEY, self.token)


def _active_key(endpoint_id: str) -> str:
    return f"router:endpoint-config:{endpoint_id}:active"


def _draft_key(endpoint_id: str) -> str:
    return f"router:endpoint-config:{endpoint_id}:draft"


def _editable_values(endpoint: Endpoint) -> dict[str, Any]:
    return {
        "enabled": endpoint.enabled,
        "auto_candidate": endpoint.auto_candidate,
        **_config_values(endpoint),
    }


def _config_values(endpoint: Endpoint) -> dict[str, Any]:
    capabilities = asdict(endpoint.capabilities)
    capabilities.pop("validation_status", None)
    capabilities.pop("validated_at", None)
    return {
        "safe_context_tokens": endpoint.safe_context_tokens,
        "configured_context_tokens": endpoint.configured_context_tokens,
        "max_concurrency": endpoint.max_concurrency,
        "tasks": list(endpoint.tasks),
        "modalities": list(endpoint.modalities),
        "capabilities": capabilities,
    }


def _apply_values(
    endpoint: Endpoint,
    values: dict[str, Any],
) -> Endpoint:
    raw = json.loads(json.dumps(endpoint.to_dict()))
    for field in CONFIG_FIELDS | OPERATION_FIELDS:
        if field in values:
            if field == "capabilities":
                raw[field].update(values[field])
            else:
                raw[field] = values[field]
    return endpoint_from_dict(raw)


def _validated_config(
    base: Endpoint,
    values: dict[str, Any],
) -> dict[str, Any]:
    unknown = sorted(set(values) - CONFIG_FIELDS)
    if unknown:
        raise _invalid(
            "endpoint configuration contains unsupported fields: "
            + ", ".join(unknown)
        )
    candidate = _apply_values(base, values)
    if candidate.safe_context_tokens <= 0:
        raise _invalid("safe context tokens must be positive")
    if candidate.configured_context_tokens <= 0:
        raise _invalid("configured context tokens must be positive")
    if (
        candidate.safe_context_tokens
        > candidate.configured_context_tokens
    ):
        raise _invalid(
            "safe context tokens cannot exceed configured context tokens"
        )
    if candidate.safe_context_tokens > base.safe_context_tokens:
        raise _invalid(
            "safe context tokens cannot exceed the validated baseline"
        )
    if (
        candidate.configured_context_tokens
        > base.configured_context_tokens
    ):
        raise _invalid(
            "configured context tokens cannot exceed the registered baseline"
        )
    if (
        candidate.max_concurrency <= 0
        or candidate.max_concurrency > base.max_concurrency
    ):
        raise _invalid(
            "max concurrency must be positive and cannot exceed the baseline"
        )
    if not set(candidate.tasks).issubset(set(base.tasks)):
        raise _invalid("tasks cannot exceed the registered capability ceiling")
    if not set(candidate.modalities).issubset(set(base.modalities)):
        raise _invalid(
            "modalities cannot exceed the registered capability ceiling"
        )
    _validate_capability_ceiling(base, candidate)
    return _config_values(candidate)


def _validate_capability_ceiling(
    base: Endpoint,
    candidate: Endpoint,
) -> None:
    baseline = base.capabilities
    current = candidate.capabilities
    if current.chat and not baseline.chat:
        raise _invalid("chat capability exceeds the registered ceiling")
    if (
        current.responses != "none"
        and current.responses != baseline.responses
    ):
        raise _invalid(
            "responses capability exceeds the registered ceiling"
        )
    tool_rank = {"none": 0, "single": 1, "parallel": 2}
    if tool_rank[current.tools] > tool_rank[baseline.tools]:
        raise _invalid("tool capability exceeds the registered ceiling")
    if current.tool_choice and not baseline.tool_choice:
        raise _invalid(
            "tool choice capability exceeds the registered ceiling"
        )
    if not set(current.tool_choice_modes).issubset(
        set(baseline.tool_choice_modes)
    ):
        raise _invalid(
            "tool choice modes exceed the registered capability ceiling"
        )
    if not set(current.structured_output).issubset(
        set(baseline.structured_output)
    ):
        raise _invalid(
            "structured output exceeds the registered capability ceiling"
        )
    if current.streaming and not baseline.streaming:
        raise _invalid(
            "streaming capability exceeds the registered ceiling"
        )


def _value_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _invalid(message: str) -> RouterError:
    return RouterError(
        message,
        status_code=400,
        code="invalid_endpoint_config",
    )
