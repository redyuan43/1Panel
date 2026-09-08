#!/usr/bin/env python3
"""Inspect or atomically activate an immutable managed-H3 media release."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from dotenv import dotenv_values


RELEASES = Path.home() / ".local/share/ai-router-media/releases"
CURRENT = Path.home() / ".local/share/ai-router-media/current"
PRIVATE = Path.home() / ".config/ai-router-media"
SERVICE_ENV = PRIVATE / "service.env"
ACCEPTANCE_ENV = PRIVATE / "acceptance.env"
H3_KEY = PRIVATE / "h3-key"
DEFAULT_EXECUTOR = "http://100.96.79.21:8789"
DEFAULT_REPORT = (
    Path.home()
    / ".local/state/ai-router-acceptance/20260908-h3-direct/media-activation.json"
)
REQUIRED_RELEASE_FILES = {
    "ai_router/__init__.py",
    "ai_router/media_service/__init__.py",
    "ai_router/media_service/app.py",
    "ai_router/media_service/contracts.py",
    "ai_router/media_service/gateway.py",
    "ai_router/media_service/providers.py",
    "ai_router/media_service/service.py",
    "ai_router/media_service/storage.py",
    "ai_router/media_service/video_review.py",
    "ai_router/media_service/video_workflows.py",
    "ai_router/media_service/prompt_profiles.json",
}
ACTIVE_STAGE_STATUSES = {
    "queued",
    "running",
    "reconciling",
    "cancelling",
    "archiving",
}


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def regular_private(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError(f"{path} must be a regular 0600 file")


def private_executor(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "100.96.79.21"}
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("managed H3 executor must be the approved private origin")
    return value.rstrip("/")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_candidate(candidate: Path) -> dict:
    if candidate.is_symlink():
        raise RuntimeError("candidate must not be a symbolic link")
    candidate = candidate.resolve()
    if (
        candidate.parent != RELEASES.resolve()
        or candidate.is_symlink()
        or not candidate.is_dir()
    ):
        raise RuntimeError("candidate must be a real directory directly under the release root")
    manifest_path = candidate / "MANIFEST.sha256"
    if not manifest_path.is_file():
        raise RuntimeError("candidate manifest is missing")
    recorded = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative in recorded
        ):
            raise RuntimeError("candidate manifest is invalid")
        recorded[relative] = digest
    files = {
        path.relative_to(candidate).as_posix(): path
        for path in candidate.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if set(recorded) != set(files):
        raise RuntimeError("candidate manifest does not cover the exact release")
    if not REQUIRED_RELEASE_FILES <= files.keys():
        raise RuntimeError("candidate lacks managed media runtime files")
    for relative, path in files.items():
        if path.is_symlink() or file_digest(path) != recorded[relative]:
            raise RuntimeError(f"candidate hash mismatch: {relative}")
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    return {
        "path": str(candidate),
        "manifest_sha256": file_digest(manifest_path),
        "file_count": len(files),
    }


def active_work(jobs: list[dict]) -> list[dict]:
    active = []
    for job in jobs:
        stages = [
            stage["id"]
            for stage in job.get("stages", [])
            if stage.get("status") in ACTIVE_STAGE_STATUSES
        ]
        if stages or job.get("kind") == "image" and job.get("status") in ACTIVE_STAGE_STATUSES:
            active.append({"id": job.get("id"), "status": job.get("status"), "stages": stages})
    return active


def service_jobs(secret: str) -> list[dict]:
    headers = {
        "Authorization": "Bearer " + secret,
        "X-Media-Owner": "managed-h3-activation",
        "X-Media-Admin": "true",
    }
    jobs = []
    with httpx.Client(
        base_url="http://127.0.0.1:14020",
        headers=headers,
        timeout=15,
        trust_env=False,
    ) as client:
        for kind in ("video", "image"):
            after = None
            while True:
                response = client.get(
                    "/jobs",
                    params={
                        "kind": kind,
                        **({"after": after} if after else {}),
                    },
                )
                response.raise_for_status()
                page = response.json()
                jobs.extend(page.get("data", []))
                after = page.get("next_cursor")
                if not after:
                    break
    return jobs


def merged_env() -> bytes:
    for path in (SERVICE_ENV, ACCEPTANCE_ENV, H3_KEY):
        regular_private(path)
    current = SERVICE_ENV.read_text(encoding="utf-8")
    service = dotenv_values(SERVICE_ENV)
    acceptance = dotenv_values(ACCEPTANCE_ENV)
    internal = service.get("AI_ROUTER_MEDIA_INTERNAL_KEY")
    reviewer = acceptance.get("MEDIA_REVIEW_KEY")
    h3_key = H3_KEY.read_text(encoding="utf-8").strip()
    if not internal or not reviewer or not h3_key:
        raise RuntimeError("managed media private credentials are incomplete")
    updates = {
        "AI_ROUTER_MEDIA_INTERNAL_KEY": internal,
        "AI_ROUTER_H3_EXECUTOR_URL": DEFAULT_EXECUTOR,
        "AI_ROUTER_H3_KEY": h3_key,
        "AI_ROUTER_VIDEO_REVIEW_KEY": reviewer,
        "AI_ROUTER_VIDEO_REVIEW_URL": "http://127.0.0.1:4000/v1/chat/completions",
    }
    output = []
    seen = set()
    for line in current.splitlines():
        stripped = line.strip()
        name = (
            stripped.split("=", 1)[0]
            if "=" in stripped and not stripped.startswith("#")
            else None
        )
        if name in updates:
            output.append(f"{name}={updates[name]}")
            seen.add(name)
        else:
            output.append(line)
    output.extend(f"{name}={value}" for name, value in updates.items() if name not in seen)
    return ("\n".join(output).rstrip() + "\n").encode()


def service_state() -> dict:
    raw = subprocess.check_output(
        [
            "systemctl",
            "--user",
            "show",
            "media-adapter.service",
            "-p",
            "MainPID",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "NRestarts",
        ],
        text=True,
    )
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def atomic_symlink(target: str | Path) -> None:
    temporary = CURRENT.with_name(f".{CURRENT.name}.{uuid4().hex}.link")
    try:
        os.symlink(str(target), temporary, target_is_directory=True)
        os.replace(temporary, CURRENT)
    finally:
        temporary.unlink(missing_ok=True)


def live_capabilities(secret: str) -> dict:
    headers = {
        "Authorization": "Bearer " + secret,
        "X-Media-Owner": "managed-h3-activation",
        "X-Media-Admin": "true",
        "X-Media-Models": "siyuan-image,siyuan-video",
    }
    with httpx.Client(
        base_url="http://127.0.0.1:14020",
        headers=headers,
        timeout=30,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        options = client.get("/options")
        options.raise_for_status()
    health_value = health.json()
    options_value = options.json()
    if health_value.get("workflow_contract_version") != 2:
        raise RuntimeError("media adapter did not activate workflow contract v2")
    videos = options_value.get("videos", {})
    if (
        not videos.get("available")
        or videos.get("workflow_contract_version") != 2
        or videos.get("default_workflow_mode") != "quality_gate"
    ):
        raise RuntimeError("media adapter cannot reach the Ivan managed executor")
    return {
        "health": health_value,
        "videos": {
            "available": videos.get("available"),
            "workflow_contract_version": videos.get("workflow_contract_version"),
            "default_workflow_mode": videos.get("default_workflow_mode"),
            "execution_profiles": videos.get("execution_profiles"),
        },
    }


def inspect(candidate: Path) -> dict:
    release = validate_candidate(candidate)
    regular_private(SERVICE_ENV)
    service = dotenv_values(SERVICE_ENV)
    internal = service.get("AI_ROUTER_MEDIA_INTERNAL_KEY")
    if not internal:
        raise RuntimeError("media internal credential is unavailable")
    jobs = service_jobs(internal)
    return {
        "candidate": release,
        "current_release": str(CURRENT.resolve()),
        "service": service_state(),
        "active_work": active_work(jobs),
        "credential_readiness": {
            "media_internal": True,
            "h3_key": H3_KEY.is_file(),
            "review_key": bool(dotenv_values(ACCEPTANCE_ENV).get("MEDIA_REVIEW_KEY"))
            if ACCEPTANCE_ENV.is_file()
            else False,
        },
    }


def activate(candidate: Path) -> dict:
    before = inspect(candidate)
    if before["active_work"]:
        raise RuntimeError("media adapter has active generation work")
    environment = merged_env()
    internal = dotenv_values(stream=io.StringIO(environment.decode()))
    secret = internal.get("AI_ROUTER_MEDIA_INTERNAL_KEY")
    if not secret:
        raise RuntimeError("updated media environment lacks its internal credential")
    old_target = os.readlink(CURRENT)
    old_environment = SERVICE_ENV.read_bytes()
    backup = (
        Path.home()
        / ".local/state/ai-router-acceptance/20260908-h3-direct"
        / f"media-before-{time.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    )
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_write(backup / "service.env", old_environment)
    private_write(backup / "current-target.txt", old_target.encode())
    switched = False
    report = {"before": before, "backup": str(backup)}
    try:
        atomic_symlink(candidate.resolve())
        switched = True
        private_write(SERVICE_ENV, environment)
        subprocess.run(
            ["systemctl", "--user", "restart", "media-adapter.service"],
            check=True,
            timeout=90,
        )
        deadline = time.monotonic() + 90
        while True:
            try:
                capabilities = live_capabilities(secret)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("managed media adapter did not become healthy")
                time.sleep(2)
        report.update(
            status="activated_verified_no_generation",
            after={
                "current_release": str(CURRENT.resolve()),
                "service": service_state(),
                "capabilities": capabilities,
            },
        )
        return report
    except Exception as error:
        report["first_fatal"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        try:
            private_write(SERVICE_ENV, old_environment)
            if switched:
                atomic_symlink(old_target)
            subprocess.run(
                ["systemctl", "--user", "restart", "media-adapter.service"],
                check=True,
                timeout=90,
            )
            report["rollback"] = "restored"
        except Exception as rollback_error:
            report["rollback"] = {
                "type": type(rollback_error).__name__,
                "message": str(rollback_error),
            }
        report["status"] = "failed"
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.execute:
            report = activate(args.candidate)
        else:
            report = {
                "status": "inspected",
                "time": time.time(),
                **inspect(args.candidate),
            }
    except Exception as error:
        report = {
            "status": "failed",
            "first_fatal": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
    private_write(args.report, json.dumps(report, indent=2).encode())
    print(json.dumps({
        "status": report["status"],
        "report": str(args.report),
        "current_release": report.get("current_release")
        or report.get("after", {}).get("current_release"),
        "active_work": (
            report["active_work"]
            if "active_work" in report
            else report.get("before", {}).get("active_work")
        ),
        "credential_readiness": report.get("credential_readiness")
        or report.get("before", {}).get("credential_readiness"),
        "rollback": report.get("rollback"),
        "first_fatal": report.get("first_fatal"),
    }, indent=2))
    return 0 if report["status"] in {"inspected", "activated_verified_no_generation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
