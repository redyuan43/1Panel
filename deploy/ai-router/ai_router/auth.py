from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from .config import Settings, client_policies
from .errors import AuthenticationError
from .types import ClientPolicy


@dataclass(frozen=True)
class AuthenticatedClient:
    policy: ClientPolicy


class AuthManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def authenticate(self, authorization: str | None) -> AuthenticatedClient:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthenticationError()
        supplied = authorization.split(" ", 1)[1].strip()
        for policy in client_policies(self.settings):
            expected = os.environ.get(policy.key_env, "")
            if expected and hmac.compare_digest(supplied, expected):
                return AuthenticatedClient(policy)
        raise AuthenticationError()

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
