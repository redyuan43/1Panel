#!/usr/bin/env python3
"""Pinned response-only fix; apply/check/rollback without importing vLLM."""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import tempfile

VERSION = "1.5.0"
ORIGINAL_SHA256 = "bf051be6b517820003c02b81f5c579c4ecda4c9dc7886e3da0a7cae62b240a69"
REPLACEMENTS = (
    (b"if self.enable_prompt_tokens_details and num_cached_tokens:",
     b"if self.enable_prompt_tokens_details and num_cached_tokens is not None:"),
    (b"if self.enable_prompt_tokens_details and final_res.num_cached_tokens:",
     b"if self.enable_prompt_tokens_details and final_res.num_cached_tokens is not None:"),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def transform(data):
    if digest(data) != ORIGINAL_SHA256:
        raise ValueError("unsupported serving.py hash; refusing to patch")
    for old, new in REPLACEMENTS:
        if data.count(old) != 1:
            raise ValueError("response guard is not unique")
        data = data.replace(old, new)
    compile(data, "serving.py", "exec")
    return data


def atomic_write(path, data):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(root, action):
    tree = ast.parse((root / "_version.py").read_text())
    versions = [node.value.value for node in ast.walk(tree)
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)]
    if versions != [VERSION]:
        raise ValueError("unsupported vLLM version")
    target = root / "entrypoints/openai/chat_completion/serving.py"
    backup = target.with_suffix(".py.cache-usage-original")
    current = target.read_bytes()
    original = current if digest(current) == ORIGINAL_SHA256 else backup.read_bytes()
    patched = transform(original)
    if current not in (original, patched):
        raise ValueError("source changed independently; refusing overwrite")
    if backup.exists() and backup.read_bytes() != original:
        raise ValueError("original backup mismatch")
    if action == "apply":
        if not backup.exists():
            atomic_write(backup, original)
        atomic_write(target, patched)
    elif action == "rollback":
        atomic_write(target, original)
    elif current != patched:
        raise ValueError("cache usage response patch is not installed")
    return {"action": action, "version": VERSION, "sha256": digest(target.read_bytes())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "check", "rollback"))
    parser.add_argument("--package-root", required=True, type=Path)
    args = parser.parse_args()
    print(run(args.package_root, args.action))
