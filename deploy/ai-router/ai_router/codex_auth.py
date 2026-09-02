from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
REFRESH_SKEW_SECONDS = 120


class CodexAuthError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "codex_auth_error",
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relogin_required = relogin_required


@dataclass(frozen=True)
class CodexCredentials:
    alias: str
    access_token: str
    account_id: str
    auth_path: Path


class CodexAccountStore:
    def __init__(
        self,
        accounts_dir: str | Path,
        *,
        status_path: str | Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.accounts_dir = Path(accounts_dir)
        self.status_path = Path(status_path or self.accounts_dir.parent / "status.json")
        self.client = client

    def aliases(self) -> tuple[str, ...]:
        if not self.accounts_dir.exists():
            return ()
        aliases = []
        try:
            entries = tuple(self.accounts_dir.iterdir())
        except OSError:
            return ()
        for item in entries:
            try:
                if item.is_dir() and (item / "auth.json").is_file():
                    aliases.append(item.name)
            except OSError:
                continue
        return tuple(sorted(aliases))

    def credentials(
        self,
        alias: str,
        *,
        force_refresh: bool = False,
    ) -> CodexCredentials:
        auth_path = self._auth_path(alias)
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = auth_path.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            payload = self._load_auth(auth_path)
            tokens = payload["tokens"]
            access_token = str(tokens.get("access_token", "")).strip()
            if force_refresh or _jwt_expiring(access_token):
                payload = self._refresh(payload)
                self._write_json(auth_path, payload)
                tokens = payload["tokens"]
                access_token = str(tokens.get("access_token", "")).strip()
            account_id = _account_id(tokens, access_token)
            if not account_id:
                raise CodexAuthError(
                    "Codex credentials are missing the ChatGPT account id",
                    code="codex_account_id_missing",
                    relogin_required=True,
                )
            return CodexCredentials(
                alias=alias,
                access_token=access_token,
                account_id=account_id,
                auth_path=auth_path,
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def account_status(self, alias: str) -> dict[str, Any]:
        value = self._load_status()
        item = value.get("accounts", {}).get(alias, {})
        return dict(item) if isinstance(item, dict) else {}

    def mark_available(self, alias: str) -> None:
        self._update_status(
            alias,
            {
                "cooldown_until": 0,
                "last_error_code": None,
                "last_error_at": None,
            },
        )

    def mark_cooldown(
        self,
        alias: str,
        *,
        seconds: int,
        code: str,
    ) -> None:
        self._update_status(
            alias,
            {
                "cooldown_until": time.time() + max(1, seconds),
                "last_error_code": code,
                "last_error_at": time.time(),
            },
        )

    def available(self, alias: str) -> bool:
        status = self.account_status(alias)
        return float(status.get("cooldown_until", 0) or 0) <= time.time()

    def _auth_path(self, alias: str) -> Path:
        if not alias or alias in {".", ".."} or "/" in alias:
            raise CodexAuthError(
                "invalid Codex account alias",
                code="codex_account_alias_invalid",
            )
        return self.accounts_dir / alias / "auth.json"

    def _load_auth(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise CodexAuthError(
                f"Codex account is not enrolled: {path.parent.name}",
                code="codex_account_not_enrolled",
                relogin_required=True,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CodexAuthError(
                "Codex credential file is invalid",
                code="codex_auth_invalid_json",
                relogin_required=True,
            ) from exc
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            raise CodexAuthError(
                "Codex credential file is missing tokens",
                code="codex_auth_invalid_shape",
                relogin_required=True,
            )
        if not str(tokens.get("access_token", "")).strip():
            raise CodexAuthError(
                "Codex credential file is missing access_token",
                code="codex_access_token_missing",
                relogin_required=True,
            )
        return payload

    def _refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = dict(payload["tokens"])
        refresh_token = str(tokens.get("refresh_token", "")).strip()
        if not refresh_token:
            raise CodexAuthError(
                "Codex credential file is missing refresh_token",
                code="codex_refresh_token_missing",
                relogin_required=True,
            )
        client = self.client or httpx.Client(
            timeout=httpx.Timeout(20.0, connect=5.0),
        )
        owned = self.client is None
        try:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "1panel-ai-router/0.1",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                },
            )
        finally:
            if owned:
                client.close()
        if response.status_code != 200:
            code, message = _oauth_error(response)
            raise CodexAuthError(
                message,
                code=code,
                relogin_required=(
                    response.status_code in {400, 401, 403}
                    or code
                    in {
                        "invalid_grant",
                        "invalid_token",
                        "refresh_token_reused",
                    }
                ),
            )
        try:
            refreshed = response.json()
        except Exception as exc:
            raise CodexAuthError(
                "Codex token refresh returned invalid JSON",
                code="codex_refresh_invalid_json",
                relogin_required=True,
            ) from exc
        access_token = str(refreshed.get("access_token", "")).strip()
        if not access_token:
            raise CodexAuthError(
                "Codex token refresh returned no access_token",
                code="codex_refresh_missing_access_token",
                relogin_required=True,
            )
        tokens["access_token"] = access_token
        next_refresh = str(refreshed.get("refresh_token", "")).strip()
        if next_refresh:
            tokens["refresh_token"] = next_refresh
        if refreshed.get("id_token"):
            tokens["id_token"] = refreshed["id_token"]
        account_id = _account_id(tokens, access_token)
        if account_id:
            tokens["account_id"] = account_id
        result = dict(payload)
        result["auth_mode"] = "chatgpt"
        result["tokens"] = tokens
        result["last_refresh"] = datetime.now(timezone.utc).isoformat()
        return result

    def _load_status(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            return {"accounts": {}}
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
        except Exception:
            return {"accounts": {}}
        return value if isinstance(value, dict) else {"accounts": {}}

    def _update_status(self, alias: str, updates: dict[str, Any]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.status_path.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            value = self._load_status()
            accounts = value.setdefault("accounts", {})
            current = accounts.setdefault(alias, {})
            current.update(updates)
            self._write_json(self.status_path, value)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def codex_headers(credentials: CodexCredentials) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "ChatGPT-Account-ID": credentials.account_id,
        "User-Agent": "codex_cli_rs/0.0.0 (1Panel AI Router)",
        "originator": "codex_cli_rs",
    }


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _jwt_expiring(token: str) -> bool:
    expires_at = _jwt_claims(token).get("exp")
    if not isinstance(expires_at, (int, float)):
        return False
    return float(expires_at) <= time.time() + REFRESH_SKEW_SECONDS


def _account_id(tokens: dict[str, Any], access_token: str) -> str:
    stored = str(tokens.get("account_id", "")).strip()
    if stored:
        return stored
    auth_claims = _jwt_claims(access_token).get(
        "https://api.openai.com/auth",
        {},
    )
    if isinstance(auth_claims, dict):
        return str(auth_claims.get("chatgpt_account_id", "")).strip()
    return ""


def _oauth_error(response: httpx.Response) -> tuple[str, str]:
    code = "codex_refresh_failed"
    message = f"Codex token refresh failed with status {response.status_code}"
    try:
        value = response.json()
    except Exception:
        return code, message
    error = value.get("error") if isinstance(value, dict) else None
    if isinstance(error, str):
        code = error
        detail = value.get("error_description") or value.get("message")
        if detail:
            message = f"Codex token refresh failed: {detail}"
    elif isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or code)
        message = str(error.get("message") or message)
    return code, message
