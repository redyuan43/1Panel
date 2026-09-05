"""Real HTTP negative tests. No provider task or mock transport is created."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import httpx
from dotenv import dotenv_values


def main():
    os.umask(0o077)
    private = Path.home() / ".config/ai-router-media/acceptance.env"
    values = dotenv_values(private)
    checks = []
    with httpx.Client(base_url="http://127.0.0.1:4000", trust_env=False, timeout=30) as client:
        cases = [
            ("unauthenticated history", "GET", "/v1/images", None, None, 401),
            ("admin key cannot impersonate client", "GET", "/v1/images", "AI_ROUTER_ADMIN_KEY", None, 401),
            ("missing media grant", "POST", "/v1/images/generations", "MEDIA_OTHER_KEY", {"prompt": "must not execute"}, 403),
            ("multiple images rejected", "POST", "/v1/images/generations", "MEDIA_CLIENT_KEY",
             {"prompt": "must not execute", "n": 2}, 400),
            ("local paths rejected", "POST", "/v1/images/generations", "MEDIA_CLIENT_KEY",
             {"prompt": "must not execute", "image": "/etc/passwd"}, 400),
            ("edits require multipart", "POST", "/v1/images/edits", "MEDIA_CLIENT_KEY",
             {"prompt": "must not execute"}, 415),
            ("unknown task", "GET", "/v1/images/img_missing", "MEDIA_CLIENT_KEY", None, 404),
            ("unknown ticket", "GET", "/v1/media/outputs/out_missing/content?access=invalid", None, None, 401),
            ("invalid public paid model", "POST", "/v1/images/generations", "MEDIA_CLIENT_KEY",
             {"model": "qwen-image-3.0-pro", "prompt": "must not execute"}, 403),
        ]
        for label, method, path, key, body, expected in cases:
            headers = {"Authorization": "Bearer " + values[key]} if key else {}
            response = client.request(method, path, headers=headers, **({"json": body} if body else {}))
            checks.append({"check": label, "method": method, "path": path.partition("?")[0],
                           "status": response.status_code, "expected": expected,
                           "request_id": response.headers.get("x-request-id"),
                           "error_code": response.json().get("error", {}).get("code"),
                           "passed": response.status_code == expected})
        response = client.get("/v1/models", headers={"Authorization": "Bearer " + values["MEDIA_CLIENT_KEY"]})
        models = {item["id"] for item in response.json()["data"]}
        checks.append({"check": "public media model identity", "status": response.status_code,
                       "models": sorted(models),
                       "passed": models == {"siyuan/auto", "siyuan-image", "siyuan-video"}})
    report = {"time": time.time(), "real_http": True, "provider_submissions": 0,
              "passed": all(item["passed"] for item in checks), "checks": checks}
    root = Path.home() / ".local/state/ai-router-acceptance/20260904-media-deploy"
    (root / "api-negative-tests.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
