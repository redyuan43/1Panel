"""Authorized local media deployment helpers; never print credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values, set_key


PRIVATE = Path.home() / ".config/ai-router-media"
EVIDENCE = Path.home() / ".local/state/ai-router-acceptance/20260904-media-deploy"
ROUTER_ENV = Path("/opt/1panel/ai-router/router.env")


def prepare():
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
    router = dotenv_values(ROUTER_ENV)
    target = PRIVATE / "service.env"
    current = dotenv_values(target) if target.exists() else {}
    key = current.get("AI_ROUTER_MEDIA_INTERNAL_KEY") or secrets.token_urlsafe(48)
    values = {
        "AI_ROUTER_MEDIA_INTERNAL_KEY": key,
        "AI_ROUTER_MEDIA_ROOT": "/opt/1panel/ai-router/media",
        "AI_ROUTER_H3_EXECUTOR_URL": "http://100.96.79.21:8789",
    }
    h3 = PRIVATE / "h3-key"
    if h3.exists():
        values["AI_ROUTER_H3_KEY"] = h3.read_text().strip()
    qwen = PRIVATE / "qwen.env"
    if qwen.exists():
        values.update({name: value for name, value in dotenv_values(qwen).items()
                       if name in {"AI_ROUTER_DASHSCOPE_API_KEY", "AI_ROUTER_DASHSCOPE_BASE_URL"} and value})
    for name, value in values.items():
        set_key(target, name, value)
    target.chmod(0o600)
    if router.get("AI_ROUTER_MEDIA_INTERNAL_KEY") != key:
        backup = EVIDENCE / f"router.env.before-{time.time_ns()}"
        shutil.copy2(ROUTER_ENV, backup)
        backup.chmod(0o600)
        set_key(ROUTER_ENV, "AI_ROUTER_MEDIA_INTERNAL_KEY", key)
    acceptance = PRIVATE / "acceptance.env"
    for name in ("AI_ROUTER_ADMIN_KEY", "AI_ROUTER_TAILSCALE_IP"):
        set_key(acceptance, name, router[name])
    set_key(acceptance, "AI_ROUTER_MEDIA_INTERNAL_KEY", key)
    acceptance.chmod(0o600)
    print(json.dumps({"prepared": True, "host_variables": sorted(values),
                      "router_media_key_configured": True}))


def client():
    env = dotenv_values(ROUTER_ENV)
    return env, httpx.Client(headers={"Authorization": "Bearer " + env["AI_ROUTER_ADMIN_KEY"]},
                             timeout=30, trust_env=False)


def status(action: str, instance: str):
    env, http = client()
    base = "http://" + ("127.0.0.1" if instance == "local" else env["AI_ROUTER_TAILSCALE_IP"]) + ":4000"
    with http:
        if action == "drain":
            response = http.post(base + "/internal/drain")
            response.raise_for_status()
        response = http.get(base + "/internal/status")
        response.raise_for_status()
        state = response.json()["instance"]
        report = {"instance": instance, "time": time.time(), **state}
        EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = EVIDENCE / f"{action}-{instance}-{time.time_ns()}.json"
        path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report))

def accounts():
    _, http = client()
    target = PRIVATE / "acceptance.env"
    saved = dotenv_values(target)
    specifications = (
        ("media-acceptance-20260904", "public", ["siyuan-image", "siyuan-video"], "MEDIA_CLIENT_KEY", 1),
        ("media-other-20260904", "public", [], "MEDIA_OTHER_KEY", 1),
        ("media-paid-20260904", "internal", ["qwen-image-3.0-pro"], "MEDIA_PAID_KEY", 1),
        ("media-video-review-20260908", "internal", [], "MEDIA_REVIEW_KEY", 2),
    )
    with http:
        response = http.get("http://127.0.0.1:4001/api/clients")
        response.raise_for_status()
        existing = {item["id"] for item in response.json()["clients"]}
        for name, disclosure, grants, variable, parallel in specifications:
            if name not in existing:
                response = http.post("http://127.0.0.1:4001/api/clients", json={
                    "id": name, "name": name, "enabled": True, "disclosure_mode": disclosure,
                    "models": ["siyuan/auto"], "media_models": grants,
                    "rpm_limit": 30, "tpm_limit": 100000, "max_parallel_requests": parallel,
                })
                response.raise_for_status()
            if not saved.get(variable):
                response = http.post(f"http://127.0.0.1:4001/api/clients/{name}/keys",
                                     json={"label": "Media live acceptance 2026-09-04"})
                response.raise_for_status()
                set_key(target, variable, response.json()["api_key"])
                set_key(target, variable + "_ID", response.json()["key"]["key_id"])
        target.chmod(0o600)
        saved = dotenv_values(target)
        service = PRIVATE / "service.env"
        if saved.get("MEDIA_REVIEW_KEY"):
            set_key(service, "AI_ROUTER_VIDEO_REVIEW_KEY", saved["MEDIA_REVIEW_KEY"])
            service.chmod(0o600)
        print(json.dumps({"acceptance_accounts": [row[0] for row in specifications]}))

def manifest():
    hash_code = (
        "import pathlib,hashlib,json; p=pathlib.Path('/app/ai_router');"
        "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest()"
        " for f in p.rglob('*') if f.is_file() and '__pycache__' not in str(f)}))"
    )
    report = {"time": time.time(), "services": {}, "unexpected_changes": []}
    for kind in ("api", "control"):
        baseline = json.loads(subprocess.check_output(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
             f"siyuan-router-media-base:{kind}-20260904", "-c", hash_code], text=True))
        for instance in ("local", "tail"):
            name = f"1panel-ai-router-router-{kind}-{instance}-1"
            hashes = json.loads(subprocess.check_output(["docker", "exec", name, "python", "-c", hash_code], text=True))
            changes = sorted(path for path in set(hashes) | set(baseline) if hashes.get(path) != baseline.get(path))
            allowed = {"client_accounts.py", "types.py", kind + ".py"}
            if kind == "control":
                allowed.update({"static/index.html", "static/media.html", "static/media.js", "static/media.css"})
            unexpected = [path for path in changes if path not in allowed and not path.startswith("media_service/")]
            info = json.loads(subprocess.check_output(
                ["docker", "inspect", "--format", '{"image":{{json .Image}},"state":{{json .State}}}', name], text=True))
            report["services"][name] = {**info, "hashes": hashes, "changed_from_live_baseline": changes}
            report["unexpected_changes"].extend(f"{name}:{path}" for path in unexpected)
    current = Path.home() / ".local/share/ai-router-media/current"
    report["host_release"] = str(current.resolve())
    report["host_hashes"] = {str(path.relative_to(current)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in current.rglob("*") if path.is_file() and "__pycache__" not in str(path)}
    report["host_service"] = subprocess.check_output(
        ["systemctl", "--user", "show", "media-adapter.service",
         "--property=MainPID,ActiveState,SubState,ExecMainStartTimestamp"], text=True).splitlines()
    report["passed"] = not report["unexpected_changes"] and all(
        item["state"]["Running"] for item in report["services"].values())
    target = EVIDENCE / "deployment-manifest.json"
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps({"report": str(target), "passed": report["passed"],
                      "unexpected_changes": report["unexpected_changes"],
                      "host_release": report["host_release"]}))
    if not report["passed"]:
        raise SystemExit(1)

def jobs():
    _, http = client()
    with http:
        response = http.get("http://127.0.0.1:4001/api/media/videos")
        response.raise_for_status()
        result = []
        for job in response.json()["data"]:
            item = {key: job.get(key) for key in ("id", "status", "request_id", "provider_state", "error")}
            item["stages"] = [
                {key: stage.get(key) for key in ("id", "status", "progress", "output_id")}
                for stage in job.get("stages", [])
            ]
            result.append(item)
        print(json.dumps({"time": time.time(), "videos": result}))

def admission_check():
    checkpoint = json.loads((EVIDENCE / "video-live/adapter-reload-checkpoint.json").read_text())
    values = dotenv_values(PRIVATE / "acceptance.env")
    _, admin = client()
    base = "http://127.0.0.1:4001"
    with admin, httpx.Client(base_url="http://127.0.0.1:4000", trust_env=False, timeout=30,
                             headers={"Authorization": "Bearer " + values["MEDIA_CLIENT_KEY"]}) as public:
        job = admin.get(base + "/api/media/videos/" + checkpoint["video_id"]).json()
        assert job["owner"] == "media-acceptance-20260904"
        original = admin.get(base + "/api/media/settings").json()
        try:
            response = admin.put(base + "/api/media/settings", json={**original, "videos_enabled": False})
            response.raise_for_status()
            response = public.post(f"/v1/videos/{job['id']}/stages/local_768/start",
                                   headers={"Idempotency-Key": "media-admission-disabled-20260904"},
                                   json={"output_id": "not-approved"})
            report = {"time": time.time(), "video_id": job["id"],
                      "status": response.status_code, "error": response.json().get("error"),
                      "request_id": response.headers.get("x-request-id"),
                      "passed": response.status_code == 503
                      and response.json().get("error", {}).get("code") == "media_disabled"}
        finally:
            restored = admin.put(base + "/api/media/settings", json=original)
            restored.raise_for_status()
        report["settings_restored"] = admin.get(base + "/api/media/settings").json() == original
        report["passed"] = report["passed"] and report["settings_restored"]
        (EVIDENCE / "admission-switch-real.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        if not report["passed"]:
            raise SystemExit(1)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "accounts", "manifest", "jobs", "admission-check", "status", "drain"))
    parser.add_argument("--instance", choices=("local", "tail"), default="local")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "accounts":
        accounts()
    elif args.action == "manifest":
        manifest()
    elif args.action == "jobs":
        jobs()
    elif args.action == "admission-check":
        admission_check()
    else:
        status(args.action, args.instance)


if __name__ == "__main__":
    main()
