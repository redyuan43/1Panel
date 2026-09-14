import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_router.client_accounts import ClientAccountManager
from ai_router.control import create_app
from ai_router.errors import AuthenticationError
from ai_router.memory_index import MemoryIndex, MemorySource
from ai_router.store import InMemoryStateStore


def test_admin_history_routes_enforce_account_and_exclusion(tmp_path):
    key = Fernet.generate_key().decode()
    settings = SimpleNamespace(runtime_path=tmp_path / "runtime.yaml")
    accounts = ClientAccountManager(InMemoryStateStore(), settings, key)
    asyncio.run(accounts.create_account(dict(id="alice", name="Alice", models=["siyuan/auto"],
        rpm_limit=10, tpm_limit=10000, max_parallel_requests=1,
        history_owner_confirmed=False, history_recall_enabled=True), allowed_models={"siyuan/auto"}))
    asyncio.run(accounts.create_account(dict(id="bob", name="Bob", models=["siyuan/auto"],
        rpm_limit=10, tpm_limit=10000, max_parallel_requests=1), allowed_models={"siyuan/auto"}))
    def authenticate(value):
        if value != "Bearer test-admin":
            raise AuthenticationError()
    runtime = SimpleNamespace(settings=settings, clients=accounts, state_encryption_key=key,
        auth=SimpleNamespace(authenticate_admin=authenticate), audit=Mock(), start=AsyncMock())
    headers = {"Authorization": "Bearer test-admin"}
    path = tmp_path / "history-memory.sqlite3"
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/clients/alice/history-memory").status_code == 401
        result = client.get("/api/clients/alice/history-memory", headers=headers)
        assert result.json() == {"state": "not_initialized", "indexing_enabled": True}
        assert not path.exists()
        index = MemoryIndex(path, key)
        index.add([MemorySource("alice", "conversation", "request", "message", "user", "E_PRIVATE_782 原文", 1)])
        hit = index.search("alice", "E_PRIVATE_782", cloud=False)[0]
        source_url = "/api/clients/alice/history-memory/sources/" + hit.source_id
        assert client.get(source_url, headers=headers).json()["source"]["text"] == "E_PRIVATE_782 原文"
        assert client.get(source_url.replace("alice", "bob"), headers=headers).status_code == 404
        assert client.post("/api/clients/alice/history-memory/exclusions", headers=headers,
            json={"conversation_id": "conversation", "excluded": "true"}).status_code == 400
        assert client.post("/api/clients/alice/history-memory/exclusions", headers=headers,
            json={"conversation_id": "conversation", "excluded": True}).status_code == 200
        assert client.get(source_url, headers=headers).status_code == 404
        assert client.get("/api/clients/alice/history-memory", headers=headers).json()["excluded_conversations"] == 1
    assert any(call.args[0] == "history_memory_source_viewed" for call in runtime.audit.write.call_args_list)
