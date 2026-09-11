from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4


SERVER = "siyuan-h3-studio"
ENDPOINT = "https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3"


def configuration(current, bridge, settings, node):
    result = copy.deepcopy(current)
    servers = result.get("mcpServers")
    if not isinstance(servers, dict) or SERVER not in servers:
        raise ValueError("existing_h3_connection_required")
    result["mcpServers"][SERVER] = {"type": "stdio", "command": str(node), "args": [str(bridge), str(settings)],
                                  "disabled": False, "timeout": 180000}
    return result


def secure_directory(path):
    identity = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True, encoding="utf-8").strip()
    sid = next(csv.reader([identity]))[1]
    if not sid.startswith("S-1-") or not all(part.isdigit() for part in sid.split("-")[1:]):
        raise ValueError("windows_identity_unavailable")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   check=True, capture_output=True, timeout=15)


def copy_acl(source, target):
    def literal(path):
        encoded = base64.b64encode(str(path).encode()).decode()
        return "([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')))"
    command = "$ErrorActionPreference='Stop'; Get-Acl -LiteralPath " + literal(source) + " | Set-Acl -LiteralPath " + literal(target)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(command.encode("utf-16le")).decode()], check=True, capture_output=True, timeout=15)


def write_receipt(private, receipt):
    temporary = private / (".receipt-" + uuid4().hex)
    temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, private / "installation.json")


def restore_installation(private):
    receipt = json.loads((private / "installation.json").read_text(encoding="utf-8"))
    conflicts = []
    for item in reversed(receipt["changes"]):
        target = Path(item["path"])
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if actual == item["before_sha256"]:
            continue
        if actual != item["after_sha256"]:
            conflicts.append(str(target))
            continue
        if item["backup"] is None:
            target.unlink()
        else:
            raw = (private / "backup" / item["backup"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["before_sha256"]:
                conflicts.append(str(target))
                continue
            temporary = target.with_name(".h3-restore-" + uuid4().hex)
            temporary.write_bytes(raw)
            copy_acl(target, temporary)
            os.replace(temporary, target)
    receipt.update(installed=False, state="rollback_conflict" if conflicts else "rolled_back",
                   conflicts=conflicts, rollback_at=time.time())
    write_receipt(private, receipt)
    if conflicts:
        raise ValueError("concurrent_changes_preserved_manual_reconciliation_required")
    return {"installed": False, "state": "rolled_back", "gpu_submissions": 0}


def install(bundle, expected_sha256, apply=False):
    if os.name != "nt":
        raise ValueError("workbuddy_bridge_installer_requires_windows")
    root = Path(os.environ["USERPROFILE"]) / ".workbuddy"
    source = root / "mcp.json"
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("mcp_configuration_changed_reinspect_before_install")
    current = json.loads(raw.decode("utf-8-sig"))
    existing = current.get("mcpServers", {}).get(SERVER, {})
    if existing.get("url") != ENDPOINT or existing.get("type") != "http":
        raise ValueError("inspect_existing_connection_before_migration")
    authorization = existing.get("headers", {}).get("Authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization[7:]) < 32:
        raise ValueError("existing_dedicated_credential_required")
    node = shutil.which("node")
    if not node:
        raise ValueError("node_runtime_not_found")
    bundle_files = {name: (bundle / name).read_bytes() for name in ("bridge.cjs", "configure_bridge.py", "SKILL.md", "multimodal.md")}
    version = hashlib.sha256(json.dumps({name: hashlib.sha256(content).hexdigest() for name, content in bundle_files.items()}, sort_keys=True).encode()).hexdigest()
    private = root / "h3-bridge"
    release = private / "releases" / version
    settings = private / "bridge-settings.json"
    credential = private / "private-token.json"
    updated = configuration(current, release / "bridge.cjs", settings, node)
    skills = list((root / "plugins/cache/h3-studio-private/siyuan-h3-connect").glob("*/skills/h3-studio/SKILL.md"))
    if len(skills) != 1 or skills[0].is_symlink():
        raise ValueError("installed_h3_skill_identity_ambiguous")
    receipt = {"installed": False, "server": SERVER, "bridge_sha256": hashlib.sha256(bundle_files["bridge.cjs"]).hexdigest(),
               "version": version, "before_config_sha256": expected_sha256, "skill": str(skills[0]), "other_servers_preserved": True}
    if not apply:
        return receipt
    if private.exists() or release.exists():
        raise ValueError("existing_bridge_requires_reconciliation_not_reinstallation")
    private.mkdir()
    secure_directory(private)
    backup = private / "backup"
    backup.mkdir()
    (backup / "mcp.json").write_bytes(raw)
    (backup / "SKILL.md").write_bytes(skills[0].read_bytes())
    reference = skills[0].parent / "references/multimodal.md"
    if reference.is_symlink():
        raise ValueError("installed_skill_reference_is_symlink")
    if reference.exists():
        (backup / "multimodal.md").write_bytes(reference.read_bytes())
    release.mkdir(parents=True)
    for name, data in bundle_files.items():
        (release / name).write_bytes(data)
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
    if any(character in str(interpreter) + str(release) for character in '%"\r\n'):
        raise ValueError("unsafe_private_launcher_path")
    launcher = private / "configure-h3.cmd"
    launcher.write_bytes(('@echo off\r\n"' + str(interpreter) + '" "' + str(release / "configure_bridge.py") + '"\r\n').encode("utf-8"))
    credential.write_text(json.dumps({"token": authorization[7:]}) + "\n", encoding="utf-8")
    settings.write_text(json.dumps({"endpoint": ENDPOINT, "credential_file": str(credential),
        "attachment_roots": [str(root / "clipboard-images")]}, indent=2) + "\n", encoding="utf-8")
    temporary = source.with_name(".h3-mcp-" + uuid4().hex + ".json")
    encoded = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
    receipt["changes"] = [{"path": str(target), "backup": name if target.exists() else None,
                            "before_sha256": hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None,
                            "after_sha256": hashlib.sha256(data).hexdigest()}
                           for target, name, data in ((source, "mcp.json", encoded), (skills[0], "SKILL.md", bundle_files["SKILL.md"]),
                                                      (reference, "multimodal.md", bundle_files["multimodal.md"]))]
    receipt["state"] = "prepared"
    write_receipt(private, receipt)
    try:
        temporary.write_bytes(encoded)
        copy_acl(source, temporary)
        if source.read_bytes() != raw:
            raise ValueError("mcp_configuration_changed_during_install")
        os.replace(temporary, source)
        skills[0].write_bytes(bundle_files["SKILL.md"])
        references = skills[0].parent / "references"
        references.mkdir(exist_ok=True)
        reference.write_bytes(bundle_files["multimodal.md"])
        receipt.update(installed=True, state="installed", installed_at=time.time(), after_config_sha256=hashlib.sha256(encoded).hexdigest(),
                       credential_location="private local file; not returned to tools", private_configuration_launcher=str(launcher),
                       client_reconnect_required=True, gpu_submissions=0)
        write_receipt(private, receipt)
        return receipt
    except Exception:
        restore_installation(private)
        raise
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.rollback:
            if not arguments.apply or os.name != "nt":
                raise ValueError("explicit_windows_rollback_required")
            result = restore_installation(Path(os.environ["USERPROFILE"]) / ".workbuddy/h3-bridge")
        else:
            result = install(arguments.bundle, arguments.expected_config_sha256, arguments.apply)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        raise SystemExit("H3 bridge installation not completed; inspect private backup and receipt, do not repeat blindly") from None
