#!/usr/bin/env python3
"""Install or roll back a hash-checked LMCache runtime patch; never restart services."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(args):
    bundle = Path(__file__).resolve().parent / "patches/lmcache-operation-lifecycle-v1"
    manifest = json.loads((bundle / "manifest.json").read_text())
    site = args.site_packages.resolve(strict=True)
    backup = args.backup.resolve() if args.backup else None
    if args.mode != "check" and backup is None:
        raise ValueError("--backup is required for apply/rollback")
    if backup and (backup == site or site in backup.parents):
        raise ValueError("backup must be outside site-packages")
    edits = []
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "lmcache":
            raise ValueError("invalid bundled path")
        target = site / relative
        if not target.resolve().is_relative_to(site):
            raise ValueError("target escapes site-packages")
        current = target.read_bytes() if target.exists() else None
        before = item["before_sha256"]
        after = item["after_sha256"]
        sha = digest(current) if current is not None else None
        if args.mode == "rollback":
            if sha == before:
                continue
            if sha != after:
                raise ValueError("runtime changed after installation: " + str(relative))
            original = backup / relative
            restored = original.read_bytes() if before is not None else None
            if (digest(restored) if restored is not None else None) != before:
                raise ValueError("invalid backup: " + str(relative))
            edits.append((target, current, restored))
            continue
        if sha == after:
            # Idempotent/resumed installation must retain a complete rollback
            # set, including files this invocation does not need to modify.
            if args.mode == "apply" and before is not None:
                original = backup / relative
                if not original.is_file() or digest(original.read_bytes()) != before:
                    raise ValueError("missing or invalid original backup for installed file: " + str(relative))
            continue
        if sha != before:
            raise ValueError("unsupported runtime source: " + str(relative))
        if before is None:
            updated = (bundle / item["payload"]).read_bytes()
        else:
            with tempfile.TemporaryDirectory(prefix="lmcache-patch-check-") as scratch:
                temp = Path(scratch) / "source.py"
                temp.write_bytes(current)
                result = subprocess.run(["patch", "--batch", "--fuzz=0", str(temp), str(bundle / item["payload"])],
                                        text=True, capture_output=True)
                if result.returncode:
                    raise ValueError("patch does not apply exactly: " + str(relative))
                updated = temp.read_bytes()
        if digest(updated) != after:
            raise ValueError("bundled patch hash mismatch: " + str(relative))
        compile(updated, str(target), "exec")
        edits.append((target, current, updated))
    if args.mode == "check":
        print(json.dumps({"mode": "check", "valid": True, "pending_files": len(edits), "patch": manifest["id"]}))
        return
    # Validate EVERY file first, then back up EVERY original before mutating.
    if args.mode == "apply":
        for target, old, _ in edits:
            if old is not None:
                saved = backup / target.relative_to(site)
                if saved.exists() and saved.read_bytes() != old:
                    raise ValueError("backup already contains a different runtime")
                atomic_write(saved, old)
        atomic_write(backup / "manifest.json", (bundle / "manifest.json").read_bytes())
    written = []
    try:
        for target, old, new in edits:
            if new is None:
                target.unlink()
            else:
                mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
                atomic_write(target, new, mode)
            written.append((target, old))
    except BaseException:
        for target, old in reversed(written):
            if old is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, old)
        raise
    print(json.dumps({"mode": args.mode, "changed_files": len(edits), "patch": manifest["id"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "rollback"))
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    run(parser.parse_args())
