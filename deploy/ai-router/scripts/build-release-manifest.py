#!/usr/bin/env python3
"""Build an auditable manifest for an AI Router release context."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


INCLUDED_PATHS = ("requirements.txt", "ai_router", "config", "benchmarks")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in INCLUDED_PATHS:
        path = root / relative
        if path.is_file():
            files.append(path)
            continue
        files.extend(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and "__pycache__" not in candidate.parts
            and candidate.suffix not in {".pyc", ".pyo"}
        )
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def source_tree_hash(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, file_hash in sorted(files.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def model_metadata(path: Path) -> dict[str, object]:
    with np.load(path, allow_pickle=False) as model:
        weights = np.asarray(model["weights"])
        bias = np.asarray(model["bias"])
    return {
        "weights_shape": list(weights.shape),
        "bias_shape": list(bias.shape),
        "sha256": sha256(path),
    }


def parse_replacement(value: str) -> tuple[str, str]:
    destination, separator, origin = value.partition("=")
    if not separator or not destination or not origin:
        raise argparse.ArgumentTypeError("replacement must be DESTINATION=ORIGIN")
    return destination, origin


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--replacement", action="append", default=[], type=parse_replacement)
    args = parser.parse_args()

    root = args.root.resolve()
    files = {
        path.relative_to(root).as_posix(): sha256(path)
        for path in release_files(root)
    }
    model_path = root / "config" / "disclosure_model.npz"
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_image": args.base_image,
        "source_revision": args.source_revision,
        "source_tree_sha256": source_tree_hash(files),
        "replacements": dict(args.replacement),
        "files": files,
        "disclosure_model": model_metadata(model_path),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=args.output.name + ".", dir=args.output.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(manifest, destination, ensure_ascii=False, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, args.output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    main()
