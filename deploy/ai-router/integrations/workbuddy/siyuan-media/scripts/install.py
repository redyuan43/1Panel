#!/usr/bin/env python3
"""Install the public skill locally. Configure credentials only via local stdin."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from media import ClientError, atomic_write, configure_credentials, secure_dir, state_home


FILES = ("SKILL.md", "references/usage.md", "scripts/media.py", "scripts/install.py")


def restore_windows_skill_inheritance(directory):
    if os.name != "nt":
        return
    root = str(Path(directory).absolute()).replace("'", "''")
    script = r"""
$ErrorActionPreference = 'Stop'
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$items = @((Get-Item -LiteralPath $root -Force)) + @(Get-ChildItem -LiteralPath $root -Force -Recurse)
foreach ($item in $items) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused' }
}
& icacls $root /reset /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Skill ACL reset failed' }
& icacls $root /setowner ('*' + $owner.Value) /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Skill owner update failed' }
foreach ($item in $items) {
    $acl = Get-Acl -LiteralPath $item.FullName
    $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    $readable = @($rules | Where-Object {
        $_.IdentityReference.Value -eq $owner.Value -and $_.AccessControlType -eq 'Allow' `
            -and ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::ReadAndExecute) `
                -eq [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
    })
    if ($acl.AreAccessRulesProtected -or $readable.Count -eq 0 `
        -or $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $owner.Value) {
        throw 'Desktop skill readability verification failed'
    }
}
"""
    command = base64.b64encode(("$root = '" + root + "'\n" + script).encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ClientError("skill_acl_failed", "Cannot make the public skill readable by the desktop user.")


def protect_windows_directory(directory):
    if os.name != "nt":
        return
    # Replace the DACL; adding grants alone preserves other explicit Windows ACEs.
    root = str(Path(directory).absolute()).replace("'", "''")
    script = r"""
$ErrorActionPreference = 'Stop'
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$items = @((Get-Item -LiteralPath $root -Force)) + @(Get-ChildItem -LiteralPath $root -Force -Recurse)
foreach ($item in $items) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused' }
    if ($item.PSIsContainer) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($owner, $system)) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inherit, [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
    $actual = Get-Acl -LiteralPath $item.FullName
    $rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
    if ($actual.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $owner.Value `
        -or !$actual.AreAccessRulesProtected -or $rules.Count -ne 2) { throw 'ACL verification failed' }
    foreach ($rule in $rules) {
        if ($rule.IdentityReference.Value -notin @($owner.Value, $system.Value) `
            -or $rule.AccessControlType -ne 'Allow' -or $rule.FileSystemRights -ne 'FullControl') {
            throw 'Unexpected ACL rule'
        }
    }
}
"""
    script = "$root = '" + root + "'\n" + script
    command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ClientError("credential_acl_failed", "Cannot restrict the private credential directory.")


def install(source, destination, expected=None):
    source, destination = Path(source).resolve(), Path(destination).expanduser().absolute()
    if destination.is_symlink():
        raise ClientError("unsafe_install_path", "The skill destination cannot be a symbolic link.")
    if destination.exists():
        existing = {str(path.relative_to(destination)).replace("\\", "/")
                    for path in destination.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
        if existing - set(FILES):
            raise ClientError("existing_skill_changed", "The existing skill contains additional files; nothing was replaced.")
        for name in FILES:
            target = destination / name
            known = (expected or {}).get(name)
            if (target.is_symlink() or target.exists() and target.read_bytes() != (source / name).read_bytes()
                    and hashlib.sha256(target.read_bytes()).hexdigest() != known):
                raise ClientError("existing_skill_changed", "The existing skill differs; nothing was replaced.")
    if source == destination:
        return {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in FILES}
    hashes = {}
    for name in FILES:
        data = (source / name).read_bytes()
        target = destination / name
        if not target.exists() or target.read_bytes() != data:
            atomic_write(target, data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def main():
    os.umask(0o077)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=Path.home() / ".workbuddy" / "skills" / "siyuan-media")
    parser.add_argument("--config", type=Path, default=state_home() / "config.json")
    parser.add_argument("--credentials-stdin", action="store_true")
    parser.add_argument("--upgrade-manifest", type=Path,
                        help="Operator-provided public hashes of the previous installation.")
    args = parser.parse_args()
    try:
        source = Path(__file__).resolve().parents[1]
        expected = json.loads(args.upgrade_manifest.read_text("utf-8")) if args.upgrade_manifest else None
        if expected is not None and (not isinstance(expected, dict) or set(expected) != set(FILES)):
            raise ClientError("invalid_upgrade_manifest", "The previous installation manifest is invalid.")
        hashes = install(source, args.skill_dir, expected)
        restore_windows_skill_inheritance(args.skill_dir)
        result = {"installed": True, "skill_directory": str(args.skill_dir),
                  "files": hashes, "application_restarted": False,
                  "desktop_acl_verified": os.name == "nt"}
        if args.credentials_stdin:
            raw = sys.stdin.buffer.read(16385)
            if len(raw) > 16384:
                raise ClientError("invalid_configuration", "Credential input is too large.")
            value = json.loads(raw)
            root = secure_dir(args.config.parent)
            protect_windows_directory(root)
            result["credentials"] = configure_credentials(args.config, value)
            protect_windows_directory(root)
            result["credentials"]["acl_verified"] = os.name == "nt"
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ClientError as exc:
        print(json.dumps(exc.public(), ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": {"code": "installation_failed",
                                    "message": "Local installation failed; no credentials were returned."}}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
