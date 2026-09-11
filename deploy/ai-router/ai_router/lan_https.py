"""Read-only LAN TLS onboarding; only public certificates enter this process."""
from __future__ import annotations

import ipaddress
import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, SecretStr

router = APIRouter()


def _directory() -> Path:
    return Path(os.environ.get("AI_ROUTER_LAN_HTTPS_PUBLIC_DIR", "/data/lan-https"))


def _admin(request: Request) -> None:
    request.app.state.runtime.auth.authenticate_admin(request.headers.get("authorization"))


def _config() -> dict:
    value = json.loads((_directory() / "connection.json").read_text())
    url = urlsplit(value["base_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path != "/v1"
            or not ipaddress.ip_address(url.hostname).is_private):
        raise ValueError("invalid LAN endpoint configuration")
    return value


def _certificate(name: str) -> x509.Certificate:
    raw = (_directory() / name).read_bytes()
    if b"PRIVATE KEY" in raw:
        raise ValueError("public certificate file contains private material")
    return x509.load_pem_x509_certificate(raw)


def _summary(cert: x509.Certificate) -> dict:
    expires = cert.not_valid_after_utc
    starts = cert.not_valid_before_utc
    now = datetime.now(timezone.utc)
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": starts.isoformat(),
        "expires_at": expires.isoformat(),
        "days_remaining": max(0, (expires - now).days),
        "valid_now": starts <= now < expires,
        "sha256": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
    }


@router.get("/lan-https")
async def page():
    return FileResponse(Path(__file__).parent / "static" / "lan-https.html")


@router.get("/api/lan-https/status")
async def status(request: Request):
    _admin(request)
    try:
        config = _config()
        ca = _certificate("ca.crt")
        server = _certificate("server.crt")
        sans = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return {
            "configured": True, "base_url": config["base_url"], "model": "siyuan/auto",
            "ca": _summary(ca), "server": _summary(server),
            "ip_addresses": [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)],
            "renewal": config.get("renewal", {}),
        }
    except (OSError, ValueError, KeyError, x509.ExtensionNotFound):
        return {"configured": False, "message": "局域网 HTTPS 公钥资料尚未就绪，请联系管理员。"}


@router.get("/api/lan-https/ca.crt")
async def download_ca():
    # This is deliberately public. Return only a parsed/re-encoded CA certificate.
    try:
        cert = _certificate("ca.crt")
        if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("not a CA certificate")
        data = cert.public_bytes(serialization.Encoding.PEM)
    except (OSError, ValueError, x509.ExtensionNotFound):
        raise HTTPException(503, "信任证书暂不可用")
    return Response(data, media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="ai-router-lan-ca.crt"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class CheckInput(BaseModel):
    api_key: SecretStr | None = None
    model: str = Field(default="siyuan/auto", min_length=1, max_length=200)
    inference: bool = False


async def _probe(config: dict, value: CheckInput) -> dict:
    if value.inference and (not value.api_key or not value.api_key.get_secret_value().strip()):
        return {"ok": False, "stage": "authentication", "reason": "key_required"}
    base = config["base_url"]
    # Never accept a destination URL from the browser, and never follow redirects with a key.
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=_certificate("ca.crt").public_bytes(serialization.Encoding.PEM).decode())
    stage = "network_tls"
    results = []
    try:
        async with httpx.AsyncClient(verify=context, trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(90, connect=5)) as client:
            response = await client.get(base.removesuffix("/v1") + "/health")
            if response.status_code != 200:
                return {"ok": False, "stage": "health", "status": response.status_code}
            results.append({"stage": "network_tls", "status": 200})
            key = value.api_key.get_secret_value().strip() if value.api_key else ""
            if not key:
                return {"ok": True, "scope": "server_to_lan", "checks": results}
            headers = {"Authorization": "Bearer " + key}
            stage = "authentication"
            response = await client.get(base + "/models", headers=headers)
            if response.status_code != 200:
                return {"ok": False, "stage": stage, "status": response.status_code}
            models = [item.get("id") for item in response.json().get("data", [])]
            results.append({"stage": stage, "status": 200})
            if value.model not in models:
                return {"ok": False, "stage": "model_access", "checks": results}
            if value.inference:
                stage = "inference"
                response = await client.post(base + "/chat/completions", headers=headers, json={
                    "model": value.model,
                    "messages": [{"role": "user", "content": "Reply with exactly LAN_HTTPS_OK."}],
                    "stream": False,
                })
                if response.status_code != 200:
                    return {"ok": False, "stage": stage, "status": response.status_code}
                answer = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                if not isinstance(answer, str) or "LAN_HTTPS_OK" not in answer:
                    return {"ok": False, "stage": "model_reply"}
                results.append({"stage": stage, "status": 200,
                                "request_id": response.headers.get("x-request-id")})
            return {"ok": True, "scope": "server_to_lan", "checks": results}
    except httpx.ConnectError as error:
        cause = str(error).lower()
        # Never return upstream exception text or bodies: those can contain credentials.
        stage = "certificate" if "certificate" in cause or "ssl" in cause else "network"
        return {"ok": False, "stage": stage}
    except httpx.TimeoutException:
        return {"ok": False, "stage": stage, "reason": "timeout"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"ok": False, "stage": stage, "reason": "invalid_response"}


@router.post("/api/lan-https/check")
async def check(request: Request, value: CheckInput):
    _admin(request)
    try:
        config = _config()
        return await _probe(config, value)
    except (OSError, ValueError, ssl.SSLError):
        return {"ok": False, "stage": "configuration"}
