from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse


def install_access(app):
    def secret():
        path = os.environ.get("H3_STUDIO_KEY_FILE", "")
        key = Path(path).read_text().strip() if path else ""
        if path and not key:
            raise RuntimeError("工作室访问密钥为空")
        return key

    def signature(value, key):
        return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()

    @app.post("/session")
    async def session(request: Request):
        key = secret()
        if not key or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
            return JSONResponse({"detail": "工作室认证失败"}, status_code=401)
        expires = str(int(time.time()) + 3600)
        response = JSONResponse({"ok": True})
        response.set_cookie("h3-studio-session", expires + "." + signature(expires, key),
                            max_age=3600, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/api/"):
            key = secret()
            users = {value.strip() for value in os.environ.get("H3_STUDIO_TAILSCALE_USERS", "").split(",") if value.strip()}
            tailnet = bool(users and request.client and request.client.host in {"127.0.0.1", "::1"}
                           and request.headers.get("tailscale-user-login", "") in users)
            if key or users:
                bearer = hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key)
                bearer = bool(key and bearer)
                expires, separator, digest = request.cookies.get("h3-studio-session", "").partition(".")
                cookie = (key and separator and expires.isdigit() and int(expires) > time.time()
                          and hmac.compare_digest(digest, signature(expires, key)))
                if not bearer and not cookie and not tailnet:
                    return JSONResponse({"detail": "需要工作室访问密钥"}, status_code=401)
                origin = request.headers.get("origin")
                expected_origin = os.environ.get("H3_STUDIO_PUBLIC_ORIGIN", str(request.base_url).rstrip("/"))
                if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != expected_origin:
                    return JSONResponse({"detail": "不允许跨来源操作"}, status_code=403)
        return await call_next(request)
