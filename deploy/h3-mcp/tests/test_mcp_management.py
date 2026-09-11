import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-router"))
from ai_router.errors import RouterError
from ai_router.h3_mcp_management import CLIENT, POLICY, install_management, permits, record
from ai_router.store import InMemoryStateStore


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = Path(__file__).resolve().parents[1]
studio = module_at("studio_settings_test", BASE / "studio/mcp_settings.py")
builder = module_at("auth_builder_test", BASE / "scripts/build_workbuddy_auth.py")


class Clients:
    def __init__(self):
        self.keys = []
        self.value = {"id": CLIENT, "enabled": True, "models": [], "media_models": ["siyuan-video"]}

    async def _required_account(self, owner):
        assert owner == CLIENT
        return self.value

    async def _keys_for(self, owner):
        assert owner == CLIENT
        return self.keys

    async def create_key(self, owner, label):
        assert owner == CLIENT
        key = {"key_id": uuid4().hex, "label": label, "status": "active"}
        self.keys.append(key)
        return key, "secret-displayed-once"

    async def revoke_key(self, owner, key_id):
        assert owner == CLIENT
        key = next(item for item in self.keys if item["key_id"] == key_id)
        key["status"] = "revoked"
        return key


@pytest.fixture
def manager(monkeypatch, tmp_path):
    secret = tmp_path / "key"
    secret.write_text("isolated-management-secret-at-least-32-characters")
    for name, value in {"AI_ROUTER_H3_MCP_MANAGEMENT_ENABLED": "true", "AI_ROUTER_H3_CONNECTOR_KEY_FILE": str(secret),
        "AI_ROUTER_H3_MCP_INSTANCE_ID": "control-local", "AI_ROUTER_H3_MCP_WRITES_ENABLED": "true",
        "AI_ROUTER_H3_MCP_GENERATION_ENABLED": "false", "H3_CONNECTOR_KEY_FILE": str(secret),
        "H3_STUDIO_KEY_FILE": str(secret), "H3_MCP_ADMIN_USERS": "admin@example.test",
        "H3_STUDIO_PUBLIC_ORIGIN": "https://studio.example.test", "H3_CONNECTOR_WRITES_ENABLED": "true",
        "H3_CONNECTOR_GENERATION_ENABLED": "false"}.items():
        monkeypatch.setenv(name, value)
    audit = []
    runtime = SimpleNamespace(clients=Clients(), store=InMemoryStateStore(), audit=SimpleNamespace(write=lambda *args, **kwargs: audit.append((args, kwargs))))
    app = FastAPI()
    app.state.runtime = runtime

    @app.exception_handler(RouterError)
    async def failure(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status_code)

    install_management(app)
    with TestClient(app, client=("127.0.0.1", 5678)) as client:
        yield SimpleNamespace(client=client, app=app, runtime=runtime, audit=audit,
            headers={"Authorization": "Bearer " + secret.read_text()}, secret=secret)


def write(manager, action, **value):
    return manager.client.post("/internal/h3-mcp-management/" + action,
        headers=manager.headers, json={"operation_id": str(uuid4()), **value})


def test_no_auth_and_browser_origin_rejected(manager):
    assert manager.client.get("/internal/h3-mcp-management/status").status_code == 401
    response = manager.client.get("/internal/h3-mcp-management/status", headers={**manager.headers, "Origin": "https://evil.test"})
    assert response.status_code == 403


def test_remote_source_with_internal_secret_rejected(manager):
    with TestClient(manager.app, client=("100.1.2.3", 1)) as client:
        assert client.get("/internal/h3-mcp-management/status", headers=manager.headers).status_code == 403


def test_issue_once_replay_does_not_mint_or_reveal_again(manager):
    payload = {"operation_id": str(uuid4()), "label": "WorkBuddy"}
    response = manager.client.post("/internal/h3-mcp-management/issue", headers=manager.headers, json=payload)
    assert response.status_code == 200
    assert response.json()["api_key"] == "secret-displayed-once"
    assert response.headers["cache-control"] == "no-store"
    repeat = manager.client.post("/internal/h3-mcp-management/issue", headers=manager.headers, json=payload)
    assert repeat.status_code == 409
    assert len(manager.runtime.clients.keys) == 1
    assert "secret-displayed-once" not in json.dumps(manager.audit)
    status = manager.client.get("/internal/h3-mcp-management/status", headers=manager.headers)
    assert "secret-displayed-once" not in status.text
    assert status.json()["generation_enabled"] is False


def test_revoke_only_dedicated_account_keys(manager):
    assert write(manager, "revoke", key_id="other-account-key").status_code == 404
    key = write(manager, "issue", label="mine").json()["key"]["key_id"]
    assert write(manager, "revoke", key_id=key).json()["key"]["status"] == "revoked"


@pytest.mark.parametrize("field,value", [("client_id", "admin"), ("url", "http://evil.test"), ("models", ["*"])])
def test_cannot_expand_grants_or_forward_arbitrary_requests(manager, field, value):
    assert write(manager, "issue", **{field: value}).status_code == 400
    assert not manager.runtime.clients.keys


def test_policy_cannot_override_release_generation_gate(manager):
    response = write(manager, "policy", revision="initial", writes=True, generation=True)
    assert response.status_code == 409
    paused = write(manager, "policy", revision="initial", writes=False, generation=False)
    assert paused.status_code == 200
    assert write(manager, "policy", revision="initial", writes=True, generation=False).status_code == 409
    resumed = write(manager, "policy", revision=paused.json()["revision"], writes=True, generation=False)
    assert resumed.status_code == 200


def test_unknown_operation_remains_blocked(manager):
    operation = str(uuid4())
    manager.client.portal.call(manager.runtime.store.set_json, "router:h3-management:operation:" + operation, {"state": "started"})
    response = manager.client.post("/internal/h3-mcp-management/issue", headers=manager.headers, json={"operation_id": operation})
    assert response.status_code == 409
    assert not manager.runtime.clients.keys


def test_gate_and_observation_use_shared_store(manager):
    manager.client.portal.call(manager.runtime.store.set_json, POLICY, {"writes": False})
    assert manager.client.portal.call(permits, manager.runtime, "writes") is False
    manager.client.portal.call(record, manager.runtime, CLIENT, "authentication")
    value = manager.client.get("/internal/h3-mcp-management/status", headers=manager.headers)
    assert value.json()["last_authentication"]["at"] > 0


def test_refuse_grant_drift_and_excessive_keys(manager):
    manager.runtime.clients.value["models"] = ["*"]
    assert write(manager, "issue").status_code == 409
    manager.runtime.clients.value["models"] = []
    for index in range(5):
        assert write(manager, "issue", label=str(index)).status_code == 200
    assert write(manager, "issue").status_code == 409


def request(headers, client="127.0.0.1"):
    return Request({"type": "http", "client": (client, 123), "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]})


def test_studio_requires_explicit_admin_identity_and_csrf(manager):
    assert studio.administrator(request({"tailscale-user-login": "admin@example.test"}))
    assert not studio.administrator(request({"tailscale-user-login": "ordinary@example.test"}))
    assert not studio.administrator(request({"tailscale-user-login": "admin@example.test"}, "100.1.2.3"))
    assert not studio.administrator(request({"tailscale-user-login": "admin@example.test", "origin": "https://evil.test"}))
    assert not studio.administrator(request({"authorization": "Bearer video-client-token"}))


def test_studio_relays_only_approved_actions_and_no_auto_retry(manager):
    app = FastAPI()
    app.state.mcp_management_transport = httpx.ASGITransport(app=manager.app)
    studio.install_mcp_settings(app)
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        headers = {"tailscale-user-login": "admin@example.test", "origin": "https://studio.example.test"}
        status = client.get("/api/mcp-admin/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["studio"]["healthy"] is True
        assert client.get("/api/mcp-admin/status").status_code == 403
        assert client.post("/api/mcp-admin/shell", headers=headers, json={}).status_code == 404
        result = client.post("/api/mcp-admin/issue", headers=headers, json={"operation_id": str(uuid4()), "label": "browser"})
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"


def test_native_auth_package_uses_dependency_token_form_not_global_config(tmp_path):
    source = BASE.parent / "ai-router/integrations/workbuddy/h3-studio"
    files = builder.package_files(source)
    metadata = json.loads(files[".codebuddy-plugin/plugin.json"])
    assert metadata["dependencies"]["mcpServers"] == "./.mcp.json"
    entry = json.loads(files[".mcp.json"])["mcpServers"]["siyuan-h3-studio"]
    assert entry["type"] == "http"
    assert entry["headers"]["Authorization"] == "Bearer ${H3_ACCESS_TOKEN}"
    assert entry["x-workbuddy"]["auth"]["type"] == "token"
    assert entry["x-workbuddy"]["auth"]["tokenSchema"]["fields"][0]["type"] == "password"
    target = tmp_path / "auth.zip"
    builder.build(target, source)
    with pytest.raises(FileExistsError):
        builder.build(target, source)


def test_frontend_never_persists_tokens_or_calls_generation():
    script = (BASE / "frontend/mcp-settings.js").read_text()
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "h3_start_preview" not in script
    assert "innerHTML" not in script
    assert 'element("token").value = ""' in script
