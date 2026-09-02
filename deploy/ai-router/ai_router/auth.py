from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from .client_accounts import ClientAccountManager
from .config import Settings
from .errors import AuthenticationError
from .types import ClientPolicy


@dataclass(frozen=True)
class AuthenticatedClient:
    policy: ClientPolicy
    key_id: str


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        accounts: ClientAccountManager,
    ) -> None:
        self.settings = settings
        self.accounts = accounts

    async def authenticate(
        self,
        authorization: str | None,
    ) -> AuthenticatedClient:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthenticationError()
        supplied = authorization.split(" ", 1)[1].strip()
        policy, key_id = await self.accounts.authenticate(supplied)
        return AuthenticatedClient(policy=policy, key_id=key_id)

    def authenticate_admin(self, authorization: str | None) -> None:
        expected = os.environ.get("AI_ROUTER_ADMIN_KEY", "")
        if not expected or not authorization or not authorization.lower().startswith("bearer "):
            raise AuthenticationError()
        supplied = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(supplied, expected):
            raise AuthenticationError()

    @staticmethod
    def ensure_model_access(client: AuthenticatedClient, model: str) -> None:
        if "*" not in client.policy.models and model not in client.policy.models:
            raise AuthenticationError("API key does not permit this model")
