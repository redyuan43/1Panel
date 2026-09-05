#!/usr/bin/env python3
"""Operator-only packaging, provisioning and local Windows installation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "siyuan-media/scripts"))
from media import ROUTER_URL, atomic_write, encode, read_private, secure_dir
from install import FILES

CLIENT_ID = "workbuddy-ivan-laptop"
PRIVATE = Path.home() / ".config/ai-router-media/clients"
CREDENTIAL = PRIVATE / (CLIENT_ID + ".json")
EVIDENCE = Path.home() / ".local/state/ai-router-acceptance/20260905-workbuddy-media"
PACKAGE = Path.home() / ".local/share/ai-router-media/dist/siyuan-media-1.0.0.zip"
WINDOWS_ROOT = "C:/Users/Ivan/AppData/Local/SIYUAN/MediaInstaller"
WINDOWS_SKILL = "C:/Users/Ivan/.workbuddy/skills/siyuan-media"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "ivan-laptop"]
SPEC = {
    "id": CLIENT_ID, "name": "WorkBuddy Ivan laptop media", "enabled": True,
    "disclosure_mode": "public", "models": ["siyuan/auto"],
    "media_models": ["siyuan-image", "siyuan-video"],
    "rpm_limit": 30, "tpm_limit": 100000, "max_parallel_requests": 1,
    "allow_compaction": False,
}


def private_credentials():
    return json.loads(read_private(CREDENTIAL, 16384))


def report(name, value):
    secure_dir(EVIDENCE)
    atomic_write(EVIDENCE / (name + ".json"), encode(value))
    return value


def admin_client():
    env = dotenv_values("/opt/1panel/ai-router/router.env")
    return httpx.Client(base_url="http://127.0.0.1:4001", trust_env=False,
                        follow_redirects=False, timeout=30,
                        headers={"Authorization": "Bearer " + env["AI_ROUTER_ADMIN_KEY"]})


def provision():
    secure_dir(PRIVATE)
    with admin_client() as admin:
        response = admin.get("/api/clients")
        response.raise_for_status()
        account = next((item for item in response.json()["clients"] if item["id"] == CLIENT_ID), None)
        if account is None:
            response = admin.post("/api/clients", json=SPEC)
            response.raise_for_status()
            account = response.json()["client"]
        if any(account.get(key) != value for key, value in SPEC.items()):
            raise RuntimeError("Existing client policy differs; no policy or keys were changed.")
        pending = PRIVATE / (CLIENT_ID + ".pending.json")
        if CREDENTIAL.exists():
            credentials = private_credentials()
            if not any(item["key_id"] == credentials["key_id"] and item["status"] == "active"
                       for item in account["keys"]):
                raise RuntimeError("The saved client key is not active; manual reconciliation is required.")
        else:
            if pending.exists() or account.get("keys"):
                raise RuntimeError("A key may already exist; refusing to mint a duplicate.")
            atomic_write(pending, encode({"client_id": CLIENT_ID, "started_at": time.time()}))
            response = admin.post(f"/api/clients/{CLIENT_ID}/keys",
                                  json={"label": "Ivan laptop local-only media 2026-09-05"})
            response.raise_for_status()
            value = response.json()
            credentials = {"client_id": CLIENT_ID, "base_url": ROUTER_URL,
                           "key_id": value["key"]["key_id"], "api_key": value["api_key"]}
            atomic_write(CREDENTIAL, encode(credentials))
    result = {"client_id": CLIENT_ID, "key_id": credentials["key_id"],
              "private_backup": str(CREDENTIAL), "policy": SPEC, "key_returned": False}
    return report("client-provisioned", result)


def verify():
    credentials = private_credentials()
    headers = {"Authorization": "Bearer " + credentials["api_key"]}
    with httpx.Client(base_url="http://127.0.0.1:4000", headers=headers,
                      trust_env=False, follow_redirects=False, timeout=30) as client, admin_client() as admin:
        options = client.get("/v1/media/options")
        options.raise_for_status()
        models = client.get("/v1/models")
        models.raise_for_status()
        denied = client.get("http://127.0.0.1:4001/api/clients")
        other_jobs = admin.get("/api/media/images")
        other_jobs.raise_for_status()
        other = next((item for item in other_jobs.json()["data"] if item.get("owner") != CLIENT_ID), None)
        ownership = client.get("/v1/images/" + other["id"]) if other else None
        result = {"client_id": CLIENT_ID, "media_options": options.json(),
                  "catalog": [item["id"] for item in models.json()["data"]],
                  "admin_status": denied.status_code,
                  "cross_owner_status": ownership.status_code if ownership is not None else None,
                  "options_request_id": options.headers.get("x-request-id")}
        assert credentials["api_key"] not in json.dumps(result)
        report("client-authorization-check", result)
        assert denied.status_code in {401, 403}
        assert set(result["catalog"]) == {"siyuan/auto", "siyuan-image", "siyuan-video"}
        assert ownership is not None and ownership.status_code == 404
    result["passed"] = True
    return report("client-authorization", result)


def package():
    secure_dir(PACKAGE.parent)
    with zipfile.ZipFile(PACKAGE, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / "siyuan-media" / name, "siyuan-media/" + name)
    with zipfile.ZipFile(PACKAGE) as archive:
        assert set(archive.namelist()) == {"siyuan-media/" + name for name in FILES}
        if CREDENTIAL.exists():
            secret = private_credentials()["api_key"].encode()
            assert all(secret not in archive.read(name) for name in archive.namelist())
    return report("package", {"path": str(PACKAGE), "sha256": hashlib.sha256(PACKAGE.read_bytes()).hexdigest(),
                              "files": list(FILES), "credentials_included": False})


def remote(command, *, input_data=None, secret=None, timeout=90):
    result = subprocess.run(SSH + [command], input=input_data, capture_output=True, timeout=timeout)
    if secret and secret.encode() in result.stdout + result.stderr:
        raise RuntimeError("Remote output contained a credential; it was not returned or recorded.")
    if result.returncode:
        # Never return raw SSH/PowerShell stderr from a credential operation.
        raise RuntimeError("Remote operation failed; no credential output was returned.")
    return result.stdout.decode("utf-8-sig").strip()


def deploy(*, configure=True):
    credentials = private_credentials() if configure else None
    package()
    remote('powershell -NoProfile -NonInteractive -Command '
           f'"New-Item -ItemType Directory -Force -Path \'{WINDOWS_ROOT}\' | Out-Null"')
    copied = subprocess.run(["scp", "-o", "BatchMode=yes", str(PACKAGE),
                             f"ivan-laptop:{WINDOWS_ROOT}/siyuan-media-1.0.0.zip"],
                            capture_output=True, timeout=90)
    if copied.returncode:
        raise RuntimeError("Public skill package transfer failed.")
    upgrade = ""
    previous = EVIDENCE / "windows-install.json"
    if previous.exists():
        manifest = EVIDENCE / "skill-previous-files.json"
        atomic_write(manifest, encode(json.loads(previous.read_text())["files"]))
        copied = subprocess.run(["scp", "-o", "BatchMode=yes", str(manifest),
                                 f"ivan-laptop:{WINDOWS_ROOT}/skill-previous-files.json"],
                                capture_output=True, timeout=90)
        if copied.returncode:
            raise RuntimeError("Previous public file manifest transfer failed.")
        upgrade = f" --upgrade-manifest {WINDOWS_ROOT}/skill-previous-files.json"
    remote('powershell -NoProfile -NonInteractive -Command '
           f'"Expand-Archive -Force -LiteralPath \'{WINDOWS_ROOT}/siyuan-media-1.0.0.zip\' '
           f'-DestinationPath \'{WINDOWS_ROOT}/1.0.0\'"')
    value = {key: credentials[key] for key in ("api_key", "base_url", "client_id")} if configure else None
    output = remote(f'C:/Python312/python.exe -B {WINDOWS_ROOT}/1.0.0/siyuan-media/scripts/install.py '
                    + ("--credentials-stdin" if configure else "") + upgrade,
                    input_data=encode(value) if configure else None,
                    secret=credentials["api_key"] if configure else None)
    result = json.loads(output)
    assert result["installed"] is True
    if configure:
        assert result["credentials"]["configured"] is True
    else:
        result["credentials_updated"] = False
        if previous.exists() and "credentials" in json.loads(previous.read_text()):
            result["credentials"] = json.loads(previous.read_text())["credentials"]
    return report("windows-install", result)


def windows(command):
    credentials = private_credentials()
    allowed = {"doctor", "options", "new-operation"}
    if command not in allowed:
        raise RuntimeError("Unsupported diagnostics command.")
    output = remote(f"C:/Python312/python.exe -B {WINDOWS_SKILL}/scripts/media.py {command}",
                    secret=credentials["api_key"])
    return report("windows-" + command, json.loads(output))


def privacy_audit():
    script = r"""
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'SIYUAN\Media'
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$result = @()
foreach ($path in @($root, (Join-Path $root 'config.json'), (Join-Path $root 'router-key.dpapi'))) {
    $acl = Get-Acl -LiteralPath $path
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    $result += [pscustomobject]@{
        path = $path
        owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        inheritance_disabled = $acl.AreAccessRulesProtected
        grants = @($rules | ForEach-Object {
            [pscustomobject]@{sid = $_.IdentityReference.Value; rights = $_.FileSystemRights.ToString(); type = $_.AccessControlType.ToString()}
        })
    }
}
[pscustomobject]@{current_user = $owner; files = $result} | ConvertTo-Json -Depth 8 -Compress
"""
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    credentials = private_credentials()
    value = json.loads(remote("powershell -NoProfile -NonInteractive -EncodedCommand " + command,
                              secret=credentials["api_key"]))
    for item in value["files"]:
        assert item["owner"] == value["current_user"] and item["inheritance_disabled"]
        assert len(item["grants"]) == 2
        assert {rule["sid"] for rule in item["grants"]} == {value["current_user"], "S-1-5-18"}
        assert all(rule["rights"] == "FullControl" and rule["type"] == "Allow" for rule in item["grants"])
    with zipfile.ZipFile(PACKAGE) as archive:
        assert set(archive.namelist()) == {"siyuan-media/" + name for name in FILES}
        assert all(credentials["api_key"].encode() not in archive.read(name) for name in archive.namelist())
    installed = json.loads((EVIDENCE / "windows-install.json").read_text())
    assert all(hashlib.sha256((ROOT / "siyuan-media" / name).read_bytes()).hexdigest() == digest
               for name, digest in installed["files"].items())
    value.update(passed=True, package_contains_key=False, source_matches_install=True)
    return report("windows-privacy-audit", value)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "verify", "package", "deploy", "deploy-skill", "doctor", "options", "privacy-audit"))
    args = parser.parse_args()
    try:
        operation = {"provision": provision, "verify": verify, "package": package, "deploy": deploy,
                     "deploy-skill": lambda: deploy(configure=False),
                     "privacy-audit": privacy_audit}
        result = operation[args.action]() if args.action in operation else windows(args.action)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": "operator_action_failed", "exception_type": type(exc).__name__,
                          "message": "No credentials were returned. Inspect the operation state before retrying."}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
