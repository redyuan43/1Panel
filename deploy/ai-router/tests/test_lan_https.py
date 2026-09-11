import asyncio
import json
import ssl
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ai_router import lan_https


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_LAN_HTTPS_PUBLIC_DIR", str(tmp_path))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test LAN CA")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow() - timedelta(days=1))
            .not_valid_after(datetime.utcnow() + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    data = cert.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "ca.crt").write_bytes(data)
    import ipaddress
    server = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(datetime.utcnow() - timedelta(days=1))
              .not_valid_after(datetime.utcnow() + timedelta(days=10))
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("192.168.2.66"))]), critical=False)
              .sign(key, hashes.SHA256()))
    (tmp_path / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "connection.json").write_text(json.dumps({"base_url": "https://192.168.2.66:4000/v1"}))
    def auth(value):
        if value != "Bearer admin":
            raise HTTPException(401)
    app = FastAPI()
    app.state.runtime = SimpleNamespace(auth=SimpleNamespace(authenticate_admin=auth))
    app.include_router(lan_https.router)
    return TestClient(app), tmp_path, data


def test_download_is_public_but_status_and_check_require_admin(site):
    client, _, data = site
    response = client.get("/api/lan-https/ca.crt")
    assert response.status_code == 200
    assert response.content == data
    assert "attachment" in response.headers["content-disposition"]
    assert client.get("/api/lan-https/status").status_code == 401
    assert client.post("/api/lan-https/check", json={}).status_code == 401
    status = client.get("/api/lan-https/status", headers={"Authorization": "Bearer admin"}).json()
    assert status["configured"] and status["server"]["valid_now"]
    assert status["ip_addresses"] == ["192.168.2.66"]


def test_download_rejects_private_material_and_traversal(site):
    client, root, _ = site
    (root / "ca.crt").write_bytes((root / "ca.crt").read_bytes() + b"-----BEGIN PRIVATE KEY-----\nsecret")
    response = client.get("/api/lan-https/ca.crt")
    assert response.status_code == 503
    assert "secret" not in response.text
    assert client.get("/api/lan-https/server.key").status_code == 404


def test_missing_or_invalid_configuration_is_safe(site):
    client, root, _ = site
    (root / "connection.json").write_text('{"base_url":"https://example.com/v1"}')
    assert not client.get("/api/lan-https/status", headers={"Authorization": "Bearer admin"}).json()["configured"]
    response = client.post("/api/lan-https/check", headers={"Authorization": "Bearer admin"}, json={})
    assert response.json()["stage"] == "configuration"


@pytest.mark.parametrize("failure,expected", [
    ("auth", "authentication"), ("access", "model_access"),
    ("inference", "inference"), ("reply", "model_reply"),
    ("certificate", "certificate"), ("network", "network"),
])
def test_check_failure_classification_without_secret_leak(site, monkeypatch, failure, expected):
    client, _, _ = site
    original = httpx.AsyncClient
    def upstream(request):
        if failure == "certificate":
            raise httpx.ConnectError("SSL certificate failure SECRET", request=request)
        if failure == "network":
            raise httpx.ConnectError("connection refused SECRET", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        assert request.headers["authorization"] == "Bearer SECRET"
        if request.url.path == "/v1/models":
            if failure == "auth":
                return httpx.Response(401, text="SECRET")
            return httpx.Response(200, json={"data": [{"id": "other" if failure == "access" else "siyuan/auto"}]})
        if failure == "inference":
            return httpx.Response(422, text="SECRET")
        return httpx.Response(200, json={"choices": [{"message": {"content": "unexpected"}}]})
    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(upstream), **kwargs)
    monkeypatch.setattr(lan_https.httpx, "AsyncClient", factory)
    response = client.post("/api/lan-https/check", headers={"Authorization": "Bearer admin"},
                           json={"api_key": "SECRET", "inference": True})
    assert response.json()["stage"] == expected
    assert "SECRET" not in response.text


def test_check_sends_only_to_configured_host_and_inference_is_opt_in(site, monkeypatch):
    client, _, _ = site
    calls = []
    original = httpx.AsyncClient
    def upstream(request):
        calls.append(request)
        assert request.url.host == "192.168.2.66"
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "siyuan/auto"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "LAN_HTTPS_OK"}}]})
    monkeypatch.setattr(lan_https.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    headers = {"Authorization": "Bearer admin"}
    assert client.post("/api/lan-https/check", headers=headers,
                       json={"api_key": "SECRET", "base_url": "https://attacker.invalid"}).json()["ok"]
    assert [r.method for r in calls] == ["GET", "GET"]
    calls.clear()
    response = client.post("/api/lan-https/check", headers=headers, json={"api_key": "SECRET", "inference": True})
    assert response.json()["ok"]
    assert calls[-1].method == "POST"


def test_inference_requires_key_before_network(site, monkeypatch):
    client, _, _ = site
    def forbidden(**kwargs):
        raise AssertionError("network must not be called")
    monkeypatch.setattr(lan_https.httpx, "AsyncClient", forbidden)
    result = client.post("/api/lan-https/check", headers={"Authorization": "Bearer admin"},
                         json={"inference": True}).json()
    assert result == {"ok": False, "stage": "authentication", "reason": "key_required"}
