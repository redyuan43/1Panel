from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .config import Settings, client_policies
from .errors import AuthenticationError, RouterError
from .store import StateStore
from .types import ClientPolicy


CLIENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
KEY_PREFIX = "sk-1panel"
USAGE_RETENTION_SECONDS = 31 * 24 * 60 * 60
DISCLOSURE_MODES = {"public", "internal"}


class ClientAccountManager:
    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        state_key: str,
    ) -> None:
        self.store = store
        self.settings = settings
        self._pepper = _derive_pepper(state_key)
        self._last_touch: dict[str, float] = {}

    async def bootstrap_legacy(self) -> list[dict[str, str]]:
        token = uuid4().hex
        acquired = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            acquired = await self.store.acquire_lock(
                "router:client-bootstrap-lock",
                token,
                15,
            )
            if acquired:
                break
            await asyncio.sleep(0.1)
        if not acquired:
            raise RouterError(
                "client account bootstrap is busy",
                status_code=503,
                code="client_bootstrap_busy",
            )

        imported: list[dict[str, str]] = []
        try:
            for policy in client_policies(self.settings):
                account = await self.store.get_json(
                    _account_key(policy.id)
                )
                now = time.time()
                if account is None:
                    account = {
                        "id": policy.id,
                        "name": policy.id,
                        "enabled": True,
                        "models": list(policy.models),
                        "rpm_limit": policy.rpm_limit,
                        "tpm_limit": policy.tpm_limit,
                        "max_parallel_requests": (
                            policy.max_parallel_requests
                        ),
                        "allow_compaction": policy.allow_compaction,
                        "disclosure_mode": policy.disclosure_mode,
                        "source": "legacy_env",
                        "created_at": now,
                        "updated_at": now,
                    }
                    await self.store.set_json(
                        _account_key(policy.id),
                        account,
                    )

                supplied = os.environ.get(policy.key_env, "").strip()
                if not supplied:
                    continue
                digest = self._digest(supplied)
                key_name = _key_digest_key(digest)
                if await self.store.get_json(key_name) is not None:
                    continue
                key_id = (
                    "legacy-"
                    + hashlib.sha256(
                        (
                            f"{policy.id}:{policy.key_env}:{digest}"
                        ).encode("utf-8")
                    ).hexdigest()[:12]
                )
                await self.store.set_json(
                    key_name,
                    {
                        "key_id": key_id,
                        "client_id": policy.id,
                        "label": f"Legacy {policy.key_env}",
                        "hint": _key_hint(supplied),
                        "source": "legacy_env",
                        "status": "active",
                        "digest": digest,
                        "created_at": now,
                        "revoked_at": None,
                    },
                )
                imported.append(
                    {"client_id": policy.id, "key_id": key_id}
                )
            await self.store.set_json(
                "router:client-bootstrap-state",
                {"completed_at": time.time(), "version": 1},
            )
            return imported
        finally:
            await self.store.release_lock(
                "router:client-bootstrap-lock",
                token,
            )

    async def authenticate(self, supplied: str) -> tuple[ClientPolicy, str]:
        try:
            key = await self.store.get_json(
                _key_digest_key(self._digest(supplied))
            )
            if not key or key.get("status") != "active":
                raise AuthenticationError()
            account = await self.store.get_json(
                _account_key(str(key.get("client_id", "")))
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise RouterError(
                "client authentication store is unavailable",
                status_code=503,
                code="auth_store_unavailable",
            ) from exc

        if not account or not account.get("enabled", False):
            raise AuthenticationError()
        policy = _policy_from_account(account)
        key_id = str(key["key_id"])
        await self.touch_key(key_id)
        return policy, key_id

    async def touch_key(self, key_id: str) -> None:
        now = time.time()
        if now - self._last_touch.get(key_id, 0) < 60:
            return
        self._last_touch[key_id] = now
        try:
            await self.store.set_json(
                _last_used_key(key_id),
                {"key_id": key_id, "last_used_at": now},
            )
        except Exception:
            self._last_touch.pop(key_id, None)

    async def create_account(
        self,
        value: dict[str, Any],
        *,
        allowed_models: set[str],
        public_model_id: str = "siyuan/auto",
    ) -> dict[str, Any]:
        account = _validated_account(
            value,
            allowed_models=allowed_models,
            public_model_id=public_model_id,
            existing=None,
        )
        token = uuid4().hex
        if not await self.store.acquire_lock(
            "router:client-account-mutation",
            token,
            10,
        ):
            raise RouterError(
                "client account update is busy",
                status_code=409,
                code="client_update_busy",
            )
        try:
            if await self.store.get_json(_account_key(account["id"])):
                raise RouterError(
                    "client account already exists",
                    status_code=409,
                    code="client_exists",
                )
            await self.store.set_json(
                _account_key(account["id"]),
                account,
            )
        finally:
            await self.store.release_lock(
                "router:client-account-mutation",
                token,
            )
        return _public_account(account, [], _empty_usage())

    async def update_account(
        self,
        client_id: str,
        value: dict[str, Any],
        *,
        allowed_models: set[str],
        public_model_id: str = "siyuan/auto",
    ) -> dict[str, Any]:
        current = await self._required_account(client_id)
        account = _validated_account(
            value,
            allowed_models=allowed_models,
            public_model_id=public_model_id,
            existing=current,
        )
        await self.store.set_json(_account_key(client_id), account)
        keys = await self._keys_for(client_id)
        usage = await self.usage_24h(client_id)
        return _public_account(account, keys, usage)

    async def create_key(
        self,
        client_id: str,
        label: str,
    ) -> tuple[dict[str, Any], str]:
        account = await self._required_account(client_id)
        if not account.get("enabled", False):
            raise RouterError(
                "disabled client accounts cannot create API keys",
                status_code=409,
                code="client_disabled",
            )
        normalized_label = str(label).strip()
        if not 1 <= len(normalized_label) <= 80:
            raise RouterError(
                "key label must contain between 1 and 80 characters",
                status_code=400,
                code="invalid_key_label",
            )
        key_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        plaintext = f"{KEY_PREFIX}-{key_id}-{secret}"
        now = time.time()
        record = {
            "key_id": key_id,
            "client_id": client_id,
            "label": normalized_label,
            "hint": _key_hint(plaintext),
            "source": "generated",
            "status": "active",
            "digest": self._digest(plaintext),
            "created_at": now,
            "revoked_at": None,
        }
        await self.store.set_json(
            _key_digest_key(str(record["digest"])),
            record,
        )
        return _public_key(record, None), plaintext

    async def revoke_key(
        self,
        client_id: str,
        key_id: str,
    ) -> dict[str, Any]:
        await self._required_account(client_id)
        records = await self.store.list_json("router:client-key-digest:")
        record = next(
            (
                item
                for item in records
                if item.get("client_id") == client_id
                and item.get("key_id") == key_id
            ),
            None,
        )
        if record is None:
            raise RouterError(
                "client API key was not found",
                status_code=404,
                code="client_key_not_found",
            )
        if record.get("status") == "revoked":
            raise RouterError(
                "client API key is already revoked",
                status_code=409,
                code="client_key_revoked",
            )
        digest_key = _key_digest_key(str(record["digest"]))
        record["status"] = "revoked"
        record["revoked_at"] = time.time()
        await self.store.set_json(digest_key, record)
        last_used = await self.store.get_json(_last_used_key(key_id))
        return _public_key(record, last_used)

    async def list_accounts(self) -> list[dict[str, Any]]:
        accounts = await self.store.list_json("router:client-account:")
        keys = await self.store.list_json("router:client-key-digest:")
        last_used_values = await self.store.list_json(
            "router:client-key-last-used:"
        )
        last_used = {
            str(item.get("key_id")): item
            for item in last_used_values
            if item.get("key_id")
        }
        result = []
        for account in accounts:
            client_id = str(account.get("id", ""))
            account_keys = [
                _public_key(item, last_used.get(str(item.get("key_id"))))
                for item in keys
                if item.get("client_id") == client_id
            ]
            account_keys.sort(
                key=lambda item: float(item.get("created_at", 0)),
                reverse=True,
            )
            result.append(
                _public_account(
                    account,
                    account_keys,
                    await self.usage_24h(client_id),
                )
            )
        return sorted(result, key=lambda item: str(item["id"]))

    async def record_usage(
        self,
        *,
        client_id: str,
        key_id: str,
        status_code: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        await self.store.increment_counters(
            f"router:client-usage:{client_id}:{bucket}",
            {
                "requests": 1,
                "input_tokens": max(0, int(input_tokens)),
                "output_tokens": max(0, int(output_tokens)),
                "errors": 1 if status_code >= 400 else 0,
            },
            USAGE_RETENTION_SECONDS,
        )
        await self.touch_key(key_id)

    async def usage_24h(self, client_id: str) -> dict[str, int]:
        now = datetime.now(timezone.utc).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        result = _empty_usage()
        for offset in range(24):
            bucket = (now - timedelta(hours=offset)).strftime("%Y%m%d%H")
            values = await self.store.get_counters(
                f"router:client-usage:{client_id}:{bucket}"
            )
            for key in result:
                result[key] += int(values.get(key, 0))
        return result

    async def _required_account(self, client_id: str) -> dict[str, Any]:
        account = await self.store.get_json(_account_key(client_id))
        if account is None:
            raise RouterError(
                "client account was not found",
                status_code=404,
                code="client_not_found",
            )
        return account

    async def _keys_for(self, client_id: str) -> list[dict[str, Any]]:
        records = await self.store.list_json("router:client-key-digest:")
        result = []
        for record in records:
            if record.get("client_id") != client_id:
                continue
            last_used = await self.store.get_json(
                _last_used_key(str(record.get("key_id", "")))
            )
            result.append(_public_key(record, last_used))
        return result

    def _digest(self, supplied: str) -> str:
        return hmac.new(
            self._pepper,
            supplied.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _validated_account(
    value: dict[str, Any],
    *,
    allowed_models: set[str],
    public_model_id: str,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RouterError(
            "client account must be a JSON object",
            status_code=400,
            code="invalid_client",
        )
    client_id = (
        str(existing["id"])
        if existing
        else str(value.get("id", "")).strip()
    )
    if not CLIENT_ID_PATTERN.fullmatch(client_id):
        raise RouterError(
            "client ID must use 2-63 lowercase letters, digits, or hyphens",
            status_code=400,
            code="invalid_client_id",
        )
    name = str(
        value.get(
            "name",
            existing.get("name", "") if existing else "",
        )
    ).strip()
    if not 1 <= len(name) <= 80:
        raise RouterError(
            "client name must contain between 1 and 80 characters",
            status_code=400,
            code="invalid_client_name",
        )
    models_value = value.get(
        "models",
        existing.get("models", []) if existing else [],
    )
    if not isinstance(models_value, list):
        raise RouterError(
            "client models must be an array",
            status_code=400,
            code="invalid_client_models",
        )
    models = tuple(dict.fromkeys(str(item).strip() for item in models_value))
    if not models or any(not item for item in models):
        raise RouterError(
            "client models must not be empty",
            status_code=400,
            code="invalid_client_models",
        )
    invalid_models = sorted(set(models) - allowed_models)
    if invalid_models:
        raise RouterError(
            "client models contain unknown model IDs",
            status_code=400,
            code="invalid_client_models",
            details={"models": invalid_models},
        )
    if "*" in models:
        models = ("*",)
    disclosure_mode = str(
        value.get(
            "disclosure_mode",
            (
                existing.get("disclosure_mode", "internal")
                if existing
                else "public"
            ),
        )
    ).strip().lower()
    if disclosure_mode not in DISCLOSURE_MODES:
        raise RouterError(
            "client disclosure_mode must be public or internal",
            status_code=400,
            code="invalid_disclosure_mode",
        )
    if disclosure_mode == "public" and models != (public_model_id,):
        raise RouterError(
            "public clients must only use the public model",
            status_code=400,
            code="invalid_client_models",
        )
    try:
        rpm_limit = int(
            value.get(
                "rpm_limit",
                existing.get("rpm_limit") if existing else 0,
            )
        )
        tpm_limit = int(
            value.get(
                "tpm_limit",
                existing.get("tpm_limit") if existing else 0,
            )
        )
        max_parallel = int(
            value.get(
                "max_parallel_requests",
                (
                    existing.get("max_parallel_requests")
                    if existing
                    else 0
                ),
            )
        )
    except (TypeError, ValueError) as exc:
        raise RouterError(
            "client limits must be integers",
            status_code=400,
            code="invalid_client_limits",
        ) from exc
    if rpm_limit <= 0 or tpm_limit <= 0 or max_parallel <= 0:
        raise RouterError(
            "client limits must be positive",
            status_code=400,
            code="invalid_client_limits",
        )
    now = time.time()
    return {
        "id": client_id,
        "name": name,
        "enabled": bool(
            value.get(
                "enabled",
                existing.get("enabled", True) if existing else True,
            )
        ),
        "models": list(models),
        "rpm_limit": rpm_limit,
        "tpm_limit": tpm_limit,
        "max_parallel_requests": max_parallel,
        "allow_compaction": bool(
            value.get(
                "allow_compaction",
                existing.get("allow_compaction", False)
                if existing
                else False,
            )
        ),
        "disclosure_mode": disclosure_mode,
        "source": (
            str(existing.get("source", "managed"))
            if existing
            else "managed"
        ),
        "created_at": (
            float(existing.get("created_at", now))
            if existing
            else now
        ),
        "updated_at": now,
    }


def _policy_from_account(value: dict[str, Any]) -> ClientPolicy:
    return ClientPolicy(
        id=str(value["id"]),
        key_env="",
        models=tuple(str(item) for item in value.get("models", [])),
        rpm_limit=int(value["rpm_limit"]),
        tpm_limit=int(value["tpm_limit"]),
        max_parallel_requests=int(value["max_parallel_requests"]),
        allow_compaction=bool(
            value.get("allow_compaction", False)
        ),
        disclosure_mode=str(
            value.get("disclosure_mode", "internal")
        ),
    )


def _public_account(
    account: dict[str, Any],
    keys: list[dict[str, Any]],
    usage: dict[str, int],
) -> dict[str, Any]:
    return {
        "id": str(account["id"]),
        "name": str(account.get("name", account["id"])),
        "enabled": bool(account.get("enabled", False)),
        "models": list(account.get("models", [])),
        "rpm_limit": int(account.get("rpm_limit", 0)),
        "tpm_limit": int(account.get("tpm_limit", 0)),
        "max_parallel_requests": int(
            account.get("max_parallel_requests", 0)
        ),
        "allow_compaction": bool(
            account.get("allow_compaction", False)
        ),
        "disclosure_mode": str(
            account.get("disclosure_mode", "internal")
        ),
        "source": str(account.get("source", "managed")),
        "created_at": float(account.get("created_at", 0)),
        "updated_at": float(account.get("updated_at", 0)),
        "keys": keys,
        "usage_24h": usage,
    }


def _public_key(
    record: dict[str, Any],
    last_used: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "key_id": str(record["key_id"]),
        "label": str(record.get("label", "")),
        "hint": str(record.get("hint", "")),
        "source": str(record.get("source", "generated")),
        "status": str(record.get("status", "revoked")),
        "created_at": float(record.get("created_at", 0)),
        "last_used_at": (
            float(last_used.get("last_used_at", 0))
            if last_used
            else None
        ),
        "revoked_at": (
            float(record["revoked_at"])
            if record.get("revoked_at")
            else None
        ),
    }


def _derive_pepper(value: str) -> bytes:
    try:
        material = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise ValueError(
            "AI_ROUTER_STATE_KEY must be valid URL-safe base64"
        ) from exc
    return hmac.new(
        material,
        b"1panel-ai-router-client-key-v1",
        hashlib.sha256,
    ).digest()


def _key_hint(value: str) -> str:
    if len(value) <= 16:
        return f"{value[:4]}...{value[-4:]}"
    return f"{value[:14]}...{value[-4:]}"


def _empty_usage() -> dict[str, int]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "errors": 0,
    }


def _account_key(client_id: str) -> str:
    return f"router:client-account:{client_id}"


def _key_digest_key(digest: str) -> str:
    return f"router:client-key-digest:{digest}"


def _last_used_key(key_id: str) -> str:
    return f"router:client-key-last-used:{key_id}"
