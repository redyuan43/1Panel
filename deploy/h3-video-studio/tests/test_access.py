from fastapi import FastAPI
from fastapi.testclient import TestClient
import asyncio
import httpx

from app.access import install_access


def fixture_app():
    app = FastAPI()
    install_access(app)
    @app.api_route("/api/projects", methods=["GET", "POST"])
    def projects():
        return {"projects": []}
    return app


def test_cookie_session_and_cross_origin_protection(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text("test-studio-key")
    monkeypatch.setenv("H3_STUDIO_KEY_FILE", str(key_file))
    with TestClient(fixture_app()) as client:
        assert client.get("/api/projects").status_code == 401
        response = client.post("/session", headers={"Authorization": "Bearer test-studio-key"})
        assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/projects").status_code == 200
        assert client.post("/api/projects", headers={"Origin": "https://untrusted.example"}).status_code == 403


def test_tailscale_headers_are_only_trusted_from_loopback(monkeypatch):
    monkeypatch.delenv("H3_STUDIO_KEY_FILE", raising=False)
    monkeypatch.setenv("H3_STUDIO_TAILSCALE_USERS", "operator@example.test")
    with TestClient(fixture_app()) as client:
        assert client.get("/api/projects", headers={"Tailscale-User-Login": "operator@example.test"}).status_code == 401


def test_tailscale_allowlist_accepts_local_proxy_only(monkeypatch):
    monkeypatch.delenv("H3_STUDIO_KEY_FILE", raising=False)
    monkeypatch.setenv("H3_STUDIO_TAILSCALE_USERS", "operator@example.test")
    monkeypatch.setenv("H3_STUDIO_PUBLIC_ORIGIN", "https://studio.example.test")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app(), client=("127.0.0.1", 1234)),
                                     base_url="http://localhost") as client:
            assert (await client.get("/api/projects")).status_code == 401
            headers = {"Tailscale-User-Login": "operator@example.test", "Origin": "https://studio.example.test"}
            assert (await client.post("/api/projects", headers=headers)).status_code == 200
            headers["Tailscale-User-Login"] = "not-allowed@example.test"
            assert (await client.get("/api/projects", headers=headers)).status_code == 401

    asyncio.run(scenario())
