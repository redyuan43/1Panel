from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import time
from typing import Any, Iterable
from uuid import uuid4

from .compaction import extract_messages, replace_messages
from .store import StateStore


_PREFIX_BODY_FIELDS = (
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "reasoning",
    "chat_template_kwargs",
)
_MEDIA_TYPES = {
    "audio",
    "audio_url",
    "image",
    "image_url",
    "input_audio",
    "input_image",
}
_REUSABLE_ROLES = {"system", "developer"}
_SAFE_CLIENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_V3_WORKER_PREFIX = "router:prefix-affinity-worker:v3:"


@dataclass(frozen=True)
class PrefixCheckpoint:
    tokens: int
    digest: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PrefixCheckpoint":
        return cls(
            tokens=int(value.get("tokens", 0)),
            digest=str(value.get("digest", "")),
        )


@dataclass(frozen=True)
class PrefixSignature:
    exact_key: str
    legacy_key: str | None
    prefix_tokens: int
    checkpoints: tuple[PrefixCheckpoint, ...]


@dataclass(frozen=True)
class PrefixAffinityLocation:
    endpoint_id: str
    deployment_id: str
    cache_generation: str
    warmed_at: float
    last_hit_at: float
    matched_tokens: int = 0
    prefix_tokens: int = 0
    match_type: str = "exact"

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
    ) -> "PrefixAffinityLocation":
        return cls(
            endpoint_id=str(value.get("endpoint_id", "")),
            deployment_id=str(value.get("deployment_id", "")),
            cache_generation=str(value.get("cache_generation", "")),
            warmed_at=float(value.get("warmed_at", 0)),
            last_hit_at=float(value.get("last_hit_at", 0)),
            matched_tokens=int(value.get("matched_tokens", 0)),
            prefix_tokens=int(value.get("prefix_tokens", 0)),
            match_type=str(value.get("match_type", "exact")),
        )


@dataclass(frozen=True)
class PrefixAffinityRecord:
    locations: tuple[PrefixAffinityLocation, ...]
    updated_at: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PrefixAffinityRecord":
        if int(value.get("version", 0)) != 2:
            raise ValueError("unsupported prefix affinity version")
        raw_locations = value.get("locations", [])
        if not isinstance(raw_locations, list):
            raise ValueError("prefix affinity locations must be a list")
        locations = tuple(
            location
            for item in raw_locations
            if isinstance(item, dict)
            for location in (PrefixAffinityLocation.from_dict(item),)
            if location.endpoint_id and location.deployment_id
        )
        return cls(
            locations=locations,
            updated_at=float(value.get("updated_at", 0)),
        )

    def for_endpoint(
        self,
        endpoint_id: str,
    ) -> tuple[PrefixAffinityLocation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.locations
                    if item.endpoint_id == endpoint_id
                ),
                key=lambda item: (
                    -item.matched_tokens,
                    -item.last_hit_at,
                    item.deployment_id,
                ),
            )
        )


class PrefixAffinityRepository:
    def __init__(
        self,
        store: StateStore,
        settings: Any,
        secret: str,
    ) -> None:
        self.store = store
        self.settings = settings
        self.secret = secret.encode("utf-8")

    def signature(
        self,
        body: dict[str, Any],
        api_kind: str,
        *,
        client_id: str,
        requested_model: str,
        token_ids: Iterable[int],
        modalities: set[str],
        new_conversation: bool,
        context_revision: str = "",
        legacy_body: dict[str, Any] | None = None,
    ) -> PrefixSignature | None:
        config = self.settings.section("prefix_affinity")
        tokens = tuple(int(item) for item in token_ids)
        if (
            config.get("enabled") is not True
            or not new_conversation
            or requested_model == "auto"
            or not modalities.issubset({"text", "image"})
            or len(tokens) < int(config.get("min_prompt_tokens", 2048))
        ):
            return None
        prefix_body = self.reusable_prefix_body(body, api_kind)
        if prefix_body is None or _contains_media(prefix_body):
            return None
        messages = extract_messages(prefix_body, api_kind)
        if any(
            str(item.get("role", "")).lower() not in _REUSABLE_ROLES
            for item in messages
        ):
            return None

        revision = str(config.get("revision", 3))
        scope = json.dumps(
            {
                "version": 3,
                "api_kind": api_kind,
                "client_id": client_id,
                "requested_model": requested_model,
                "context_revision": context_revision,
                "pool_revision": revision,
                "request_fields": {
                    key: body[key]
                    for key in _PREFIX_BODY_FIELDS
                    if key in body and key != "tools"
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        step = int(config.get("checkpoint_tokens", 2048))
        state = hmac.new(self.secret, scope, hashlib.sha256)
        checkpoints: list[PrefixCheckpoint] = []
        for index, token in enumerate(tokens, start=1):
            state.update(struct.pack(">q", token))
            if index % step == 0:
                checkpoints.append(
                    PrefixCheckpoint(index, state.hexdigest())
                )
        if not checkpoints or checkpoints[-1].tokens != len(tokens):
            checkpoints.append(
                PrefixCheckpoint(len(tokens), state.hexdigest())
            )
        legacy_source = legacy_body if legacy_body is not None else body
        legacy_key = self.fingerprint(
            legacy_source,
            api_kind,
            client_id=client_id,
            requested_model=requested_model,
            prefix_tokens=len(tokens),
            modalities=modalities,
            new_conversation=new_conversation,
            context_revision=context_revision,
        )
        return PrefixSignature(
            exact_key=checkpoints[-1].digest,
            legacy_key=legacy_key,
            prefix_tokens=len(tokens),
            checkpoints=tuple(checkpoints),
        )

    def fingerprint(
        self,
        body: dict[str, Any],
        api_kind: str,
        *,
        client_id: str,
        requested_model: str,
        prefix_tokens: int,
        modalities: set[str],
        new_conversation: bool,
        context_revision: str = "",
    ) -> str | None:
        config = self.settings.section("prefix_affinity")
        if (
            config.get("enabled") is not True
            or not new_conversation
            or requested_model == "auto"
            or not modalities.issubset({"text", "image"})
            or prefix_tokens < int(config.get("min_prompt_tokens", 2048))
        ):
            return None
        prefix_body = self.reusable_prefix_body(body, api_kind)
        if prefix_body is None or _contains_media(prefix_body):
            return None
        prefix = extract_messages(prefix_body, api_kind)
        if any(
            str(item.get("role", "")).lower() not in _REUSABLE_ROLES
            for item in prefix
        ):
            return None
        value = {
            "version": 2,
            "api_kind": api_kind,
            "client_id": client_id,
            "requested_model": requested_model,
            "context_revision": context_revision,
            "pool_revision": str(
                config.get(
                    "legacy_revision",
                    config.get("revision", 1),
                )
            ),
            "messages": prefix,
            "request_fields": {
                key: body[key]
                for key in _PREFIX_BODY_FIELDS
                if key in body
            },
        }
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def reusable_prefix_body(
        body: dict[str, Any],
        api_kind: str,
    ) -> dict[str, Any] | None:
        messages = extract_messages(body, api_kind)
        if (
            len(messages) < 2
            or str(messages[-1].get("role", "")).lower() != "user"
            or any(
                str(item.get("role", "")).lower()
                not in _REUSABLE_ROLES
                for item in messages[:-1]
            )
        ):
            return None
        return replace_messages(body, api_kind, messages[:-1])

    async def match(
        self,
        signature: PrefixSignature | None,
    ) -> PrefixAffinityRecord | None:
        if signature is None:
            return None
        config = self.settings.section("prefix_affinity")
        minimum = int(config.get("partial_min_tokens", 8192))
        current = {
            item.tokens: item.digest
            for item in signature.checkpoints
        }
        locations: list[PrefixAffinityLocation] = []
        v3_deployments: set[str] = set()
        updated_at = 0.0
        for key, value in await self.store.list_json_items(
            _V3_WORKER_PREFIX
        ):
            if int(value.get("version", 0)) != 3:
                continue
            deployment_id = str(
                value.get("deployment_id")
                or key.removeprefix(_V3_WORKER_PREFIX)
            )
            if deployment_id:
                v3_deployments.add(deployment_id)
            checkpoints = value.get("checkpoints", [])
            if not isinstance(checkpoints, list):
                continue
            try:
                parsed_checkpoints = tuple(
                    PrefixCheckpoint.from_dict(item)
                    for item in checkpoints
                    if isinstance(item, dict)
                )
                matched = max(
                    (
                        checkpoint.tokens
                        for checkpoint in parsed_checkpoints
                        if (
                            checkpoint.tokens > 0
                            and current.get(checkpoint.tokens)
                            == checkpoint.digest
                        )
                    ),
                    default=0,
                )
            except (TypeError, ValueError):
                continue
            try:
                stored_prefix_tokens = int(
                    value.get("prefix_tokens", 0)
                )
                warmed_at = float(value.get("warmed_at", 0))
                last_hit_at = float(value.get("last_hit_at", 0))
                record_updated_at = float(value.get("updated_at", 0))
            except (TypeError, ValueError):
                continue
            exact = bool(
                matched == signature.prefix_tokens
                and stored_prefix_tokens == signature.prefix_tokens
                and str(value.get("exact_key", ""))
                == signature.exact_key
            )
            if not exact and matched < minimum:
                continue
            location = PrefixAffinityLocation(
                endpoint_id=str(value.get("endpoint_id", "")),
                deployment_id=deployment_id,
                cache_generation=str(
                    value.get("cache_generation", "")
                ),
                warmed_at=warmed_at,
                last_hit_at=last_hit_at,
                matched_tokens=(
                    signature.prefix_tokens if exact else matched
                ),
                prefix_tokens=stored_prefix_tokens,
                match_type="exact" if exact else "partial",
            )
            if location.endpoint_id and location.deployment_id:
                locations.append(location)
                updated_at = max(
                    updated_at,
                    record_updated_at,
                )

        if not locations and signature.legacy_key:
            legacy = await self.get(signature.legacy_key)
            if legacy is not None:
                locations.extend(
                    PrefixAffinityLocation(
                        endpoint_id=item.endpoint_id,
                        deployment_id=item.deployment_id,
                        cache_generation=item.cache_generation,
                        warmed_at=item.warmed_at,
                        last_hit_at=item.last_hit_at,
                        matched_tokens=signature.prefix_tokens,
                        prefix_tokens=signature.prefix_tokens,
                        match_type="legacy_exact",
                    )
                    for item in legacy.locations
                    if item.deployment_id not in v3_deployments
                )
                updated_at = legacy.updated_at
        if not locations:
            return None
        return PrefixAffinityRecord(
            locations=tuple(
                sorted(
                    locations,
                    key=lambda item: (
                        -item.matched_tokens,
                        -item.last_hit_at,
                        item.deployment_id,
                    ),
                )
            ),
            updated_at=updated_at,
        )

    async def get(self, key: str | None) -> PrefixAffinityRecord | None:
        if not key:
            return None
        value = await self.store.get_json(self._key(key))
        if not value:
            return None
        try:
            record = PrefixAffinityRecord.from_dict(value)
        except (TypeError, ValueError):
            return None
        return record if record.locations else None

    async def capture_template(
        self,
        key: str | None,
        body: dict[str, Any],
        api_kind: str,
        *,
        client_id: str,
        requested_model: str,
        prefix_tokens: int,
    ) -> Path | None:
        config = self.settings.section("prefix_affinity")
        allowed_clients = {
            str(item)
            for item in config.get("template_client_ids", [])
        }
        if (
            not key
            or config.get("capture_templates") is not True
            or client_id not in allowed_clients
            or not _SAFE_CLIENT_ID.fullmatch(client_id)
            or prefix_tokens
            < int(config.get("template_min_tokens", 20000))
        ):
            return None
        prefix_body = self.reusable_prefix_body(body, api_kind)
        if prefix_body is None or _contains_media(prefix_body):
            return None
        template_dir = Path(
            str(config.get("template_dir", "")).strip()
        )
        if not template_dir.is_absolute():
            raise ValueError(
                "prefix affinity template directory must be absolute"
            )
        path = template_dir / client_id / f"{key}.json"
        value = {
            "version": 1,
            "client_id": client_id,
            "requested_model": requested_model,
            "api_kind": api_kind,
            "prefix_key": key,
            "prefix_tokens": int(prefix_tokens),
            "captured_at": time.time(),
            "request": prefix_body,
        }
        await asyncio.to_thread(self._write_template, path, value)
        return path

    @staticmethod
    def _write_template(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(
                    value,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def record_worker(
        self,
        signature: PrefixSignature | None,
        *,
        endpoint_id: str,
        deployment_id: str,
        cache_generation: str,
    ) -> None:
        if signature is None:
            await self.invalidate_worker(deployment_id)
            return
        now = time.time()
        ttl = int(
            self.settings.section("prefix_affinity").get(
                "ttl_seconds",
                86400,
            )
        )
        await self.store.set_json(
            self._v3_worker_key(deployment_id),
            {
                "version": 3,
                "endpoint_id": endpoint_id,
                "deployment_id": deployment_id,
                "cache_generation": cache_generation,
                "exact_key": signature.exact_key,
                "prefix_tokens": signature.prefix_tokens,
                "checkpoints": [
                    asdict(item) for item in signature.checkpoints
                ],
                "warmed_at": now,
                "last_hit_at": now,
                "updated_at": now,
            },
            ttl_seconds=ttl,
        )
        await self._remove_legacy_worker_state(deployment_id)

    async def invalidate_worker(self, deployment_id: str) -> None:
        now = time.time()
        ttl = int(
            self.settings.section("prefix_affinity").get(
                "ttl_seconds",
                86400,
            )
        )
        await self.store.set_json(
            self._v3_worker_key(deployment_id),
            {
                "version": 3,
                "deployment_id": deployment_id,
                "invalidated": True,
                "checkpoints": [],
                "prefix_tokens": 0,
                "updated_at": now,
            },
            ttl_seconds=ttl,
        )
        await self._remove_legacy_worker_state(deployment_id)

    async def save(
        self,
        key: str | None,
        *,
        endpoint_id: str,
        deployment_id: str,
        cache_generation: str,
    ) -> None:
        """Write a v2 record only for compatibility with existing callers."""
        if not key:
            return
        token = uuid4().hex
        lock_key = "router:prefix-affinity:v2:mutation-lock"
        acquired = False
        for _ in range(10):
            acquired = await self.store.acquire_lock(
                lock_key,
                token,
                ttl_seconds=5,
            )
            if acquired:
                break
            await asyncio.sleep(0.02)
        if not acquired:
            raise RuntimeError("prefix affinity mutation lock is busy")
        try:
            await self._save_legacy_locked(
                key,
                endpoint_id=endpoint_id,
                deployment_id=deployment_id,
                cache_generation=cache_generation,
            )
        finally:
            await self.store.release_lock(lock_key, token)

    async def _save_legacy_locked(
        self,
        key: str,
        *,
        endpoint_id: str,
        deployment_id: str,
        cache_generation: str,
    ) -> None:
        now = time.time()
        ttl = int(
            self.settings.section("prefix_affinity").get(
                "ttl_seconds",
                86400,
            )
        )
        worker_key = self._worker_key(deployment_id)
        previous = await self.store.get_json(worker_key)
        previous_prefix = (
            str(previous.get("prefix_key", ""))
            if isinstance(previous, dict)
            else ""
        )
        if previous_prefix and previous_prefix != key:
            await self._remove_location_locked(
                previous_prefix,
                deployment_id,
                ttl,
            )
        current = await self.get(key)
        locations = [
            item
            for item in (current.locations if current else ())
            if item.deployment_id != deployment_id
        ]
        locations.append(
            PrefixAffinityLocation(
                endpoint_id=endpoint_id,
                deployment_id=deployment_id,
                cache_generation=cache_generation,
                warmed_at=now,
                last_hit_at=now,
            )
        )
        await self.store.set_json(
            self._key(key),
            {
                "version": 2,
                "locations": [asdict(item) for item in locations],
                "updated_at": now,
            },
            ttl_seconds=ttl,
        )
        await self.store.set_json(
            worker_key,
            {
                "version": 2,
                "prefix_key": key,
                "endpoint_id": endpoint_id,
                "cache_generation": cache_generation,
                "updated_at": now,
            },
            ttl_seconds=ttl,
        )

    async def _remove_legacy_worker_state(
        self,
        deployment_id: str,
    ) -> None:
        token = uuid4().hex
        lock_key = "router:prefix-affinity:v2:mutation-lock"
        acquired = await self.store.acquire_lock(
            lock_key,
            token,
            ttl_seconds=5,
        )
        if not acquired:
            return
        try:
            worker_key = self._worker_key(deployment_id)
            previous = await self.store.get_json(worker_key)
            previous_prefix = (
                str(previous.get("prefix_key", ""))
                if isinstance(previous, dict)
                else ""
            )
            if previous_prefix:
                ttl = int(
                    self.settings.section("prefix_affinity").get(
                        "ttl_seconds",
                        86400,
                    )
                )
                await self._remove_location_locked(
                    previous_prefix,
                    deployment_id,
                    ttl,
                )
            await self.store.delete(worker_key)
        finally:
            await self.store.release_lock(lock_key, token)

    async def _remove_location_locked(
        self,
        key: str,
        deployment_id: str,
        ttl: int,
    ) -> None:
        current = await self.get(key)
        if current is None:
            return
        locations = [
            item
            for item in current.locations
            if item.deployment_id != deployment_id
        ]
        if not locations:
            await self.store.delete(self._key(key))
            return
        await self.store.set_json(
            self._key(key),
            {
                "version": 2,
                "locations": [asdict(item) for item in locations],
                "updated_at": time.time(),
            },
            ttl_seconds=ttl,
        )

    @staticmethod
    def _key(key: str) -> str:
        return f"router:prefix-affinity:v2:{key}"

    @staticmethod
    def _worker_key(deployment_id: str) -> str:
        return f"router:prefix-affinity-worker:v2:{deployment_id}"

    @staticmethod
    def _v3_worker_key(deployment_id: str) -> str:
        return f"{_V3_WORKER_PREFIX}{deployment_id}"


def _contains_media(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_media(item) for item in value)
    if not isinstance(value, dict):
        return False
    item_type = str(value.get("type", "")).lower()
    if (
        item_type in _MEDIA_TYPES
        or "image" in item_type
        or "audio" in item_type
    ):
        return True
    return any(
        _contains_media(nested)
        for nested in value.values()
        if isinstance(nested, (dict, list))
    )
