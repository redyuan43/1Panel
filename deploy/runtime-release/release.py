#!/usr/bin/env python3
"""Prepare and verify immutable runtime releases from commits and snapshots."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


MANIFEST = "release-manifest.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"path must remain relative: {value!r}")
    normalized = str(path)
    if normalized in {".", MANIFEST}:
        raise ValueError(f"reserved release path: {value!r}")
    return normalized


def _selected(relative: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def _required(component: dict[str, Any]) -> tuple[str, ...]:
    values = tuple(
        _relative(str(item)) for item in component.get("required_files", [])
    )
    if not values:
        raise ValueError(f"required_files is missing: {component['name']}")
    if any(any(character in value for character in "*?[") for value in values):
        raise ValueError(f"required_files must be exact: {component['name']}")
    return values


def _check_required(
    component: dict[str, Any],
    selected: Iterable[str],
) -> tuple[str, ...]:
    required = _required(component)
    missing = sorted(set(required) - set(selected))
    if missing:
        raise ValueError(
            f"component is missing required files: {component['name']}: {missing}"
        )
    return required


def _write(root: Path, relative: str, content: bytes, executable: bool) -> None:
    relative = _relative(relative)
    target = root.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f"release path collision: {relative}")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    target.chmod(0o555 if executable else 0o444)


def _git(*args: str, repository: Path) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    ).stdout


def _git_commit(repository: Path, revision: str) -> str:
    return _git(
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        repository=repository,
    ).decode().strip()


def _git_component(
    staging: Path,
    repository: Path,
    commit: str,
    component: dict[str, Any],
) -> list[str]:
    source = _relative(str(component["source"]))
    target_root = _relative(str(component["target"]))
    patterns = tuple(_relative(str(item)) for item in component["include"])
    listing = _git(
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        source,
        repository=repository,
    )
    selected: list[str] = []
    selected_source: list[str] = []
    for row in listing.split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode().split()
        path = raw_path.decode()
        if object_type != "blob" or mode == "120000":
            raise ValueError(f"unsupported Git entry: {path}")
        relative = (
            Path(path).name
            if path == source
            else str(PurePosixPath(path).relative_to(source))
        )
        if not _selected(relative, patterns):
            continue
        destination = str(PurePosixPath(target_root) / relative)
        content = _git("cat-file", "blob", object_id, repository=repository)
        _write(staging, destination, content, mode == "100755")
        selected.append(destination)
        selected_source.append(relative)
    if not selected:
        raise ValueError(f"Git component selected no files: {component['name']}")
    _check_required(component, selected_source)
    return selected


def _snapshot_component(
    staging: Path,
    source_roots: dict[str, Path],
    component: dict[str, Any],
) -> list[str]:
    source_key = str(component["source_key"])
    if source_key not in source_roots:
        raise ValueError(f"snapshot source is required: {source_key}")
    source_root = source_roots[source_key].resolve(strict=True)
    target_root = _relative(str(component["target"]))
    patterns = tuple(_relative(str(item)) for item in component["include"])
    selected: list[str] = []
    selected_source: list[str] = []
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            relative = source.relative_to(source_root).as_posix()
            if _selected(relative, patterns):
                raise ValueError(f"snapshot symlink is not allowed: {relative}")
            continue
        if not source.is_file():
            continue
        relative = source.relative_to(source_root).as_posix()
        if not _selected(relative, patterns):
            continue
        destination = str(PurePosixPath(target_root) / relative)
        _write(staging, destination, source.read_bytes(), os.access(source, os.X_OK))
        selected.append(destination)
        selected_source.append(relative)
    if not selected:
        raise ValueError(
            f"snapshot component selected no files: {component['name']}"
        )
    _check_required(component, selected_source)
    return selected


def _external_files(
    source_root: Path,
    patterns: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root).as_posix()
        if not _selected(relative, patterns):
            continue
        if source.is_symlink():
            raise ValueError(f"external symlink is not allowed: {relative}")
        if not source.is_file():
            continue
        records[relative] = {
            "sha256": _digest(source.read_bytes()),
            "mode": source.stat().st_mode & 0o777,
        }
    return records


def _external_component(
    source_roots: dict[str, Path],
    component: dict[str, Any],
) -> dict[str, Any]:
    source_key = str(component["source_key"])
    if source_key not in source_roots:
        raise ValueError(f"external source is required: {source_key}")
    source_root = source_roots[source_key].resolve(strict=True)
    expected_root = Path(str(component["expected_root"])).expanduser().resolve(
        strict=True
    )
    if source_root != expected_root:
        raise ValueError(
            f"external source path does not match runtime: {source_key}: "
            f"{source_root} != {expected_root}"
        )
    patterns = tuple(_relative(str(item)) for item in component["include"])
    records = _external_files(source_root, patterns)
    if not records:
        raise ValueError(
            f"external component selected no files: {component['name']}"
        )
    required = _check_required(component, records)
    return {
        "name": str(component["name"]),
        "kind": "external",
        "source_commit": None,
        "source_root": str(source_root),
        "expected_root": str(expected_root),
        "include": list(patterns),
        "required_files": list(required),
        "external_files": records,
        "files": [],
    }


def _file_records(root: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                "release symlink is not allowed: "
                + path.relative_to(root).as_posix()
            )
        if not path.is_file() or path == root / MANIFEST:
            continue
        relative = path.relative_to(root).as_posix()
        records[relative] = {
            "sha256": _digest(path.read_bytes()),
            "mode": path.stat().st_mode & 0o777,
        }
    return records


def prepare(
    *,
    repository: Path,
    revision: str,
    profiles_path: Path,
    bundle: str,
    source_roots: dict[str, Path],
    output: Path,
) -> dict[str, Any]:
    repository = repository.resolve(strict=True)
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError(f"release already exists: {output}")
    profiles_bytes = profiles_path.read_bytes()
    profiles = json.loads(profiles_bytes)
    if profiles.get("schema_version") != 1:
        raise ValueError("unsupported profiles schema")
    try:
        components = profiles["bundles"][bundle]["components"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unknown release bundle: {bundle}") from exc
    commit = _git_commit(repository, revision)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent)
    )
    try:
        component_records = []
        for component in components:
            kind = str(component.get("kind", ""))
            if kind == "git":
                files = _git_component(staging, repository, commit, component)
                source_commit: str | None = commit
            elif kind == "snapshot":
                files = _snapshot_component(staging, source_roots, component)
                source_commit = None
            elif kind == "external":
                component_records.append(
                    _external_component(source_roots, component)
                )
                continue
            else:
                raise ValueError(f"unsupported component kind: {kind}")
            target_root = _relative(str(component["target"]))
            required_files = [
                str(PurePosixPath(target_root) / relative)
                for relative in _required(component)
            ]
            component_records.append(
                {
                    "name": str(component["name"]),
                    "kind": kind,
                    "source_commit": source_commit,
                    "files": sorted(files),
                    "required_files": required_files,
                }
            )
        files = _file_records(staging)
        identity = {
            "bundle": bundle,
            "git_commit": commit,
            "profiles_sha256": _digest(profiles_bytes),
            "components": component_records,
            "files": files,
        }
        manifest = {
            "schema_version": 1,
            **identity,
            "release_sha256": _digest(_canonical(identity)),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = staging / MANIFEST
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o444)
        for directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()),
            reverse=True,
        ):
            directory.chmod(0o555)
        staging.chmod(0o555)
        staging.rename(output)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify(
    release: Path,
    *,
    external_components: Iterable[str] = (),
) -> dict[str, Any]:
    release = release.expanduser().resolve(strict=True)
    manifest_path = release / MANIFEST
    if manifest_path.is_symlink():
        raise ValueError("release manifest symlink is not allowed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported release manifest")
    expected = manifest.get("files")
    if not isinstance(expected, dict):
        raise ValueError("release file manifest is missing")
    actual = _file_records(release)
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(f"release file set drift: missing={missing}, extra={extra}")
    for relative, record in expected.items():
        _relative(relative)
        if actual[relative] != record:
            raise ValueError(f"release file drift: {relative}")
    for component in manifest.get("components", []):
        if component.get("kind") == "external":
            continue
        missing_required = sorted(
            set(component.get("required_files", [])) - set(expected)
        )
        if missing_required:
            raise ValueError(
                f"release component is incomplete: {component.get('name')}: "
                f"{missing_required}"
            )
    identity = {
        key: manifest[key]
        for key in (
            "bundle",
            "git_commit",
            "profiles_sha256",
            "components",
            "files",
        )
    }
    release_sha256 = _digest(_canonical(identity))
    if release_sha256 != manifest.get("release_sha256"):
        raise ValueError("release identity does not match its contents")
    requested_external = set(external_components)
    available_external = {
        str(item["name"]): item
        for item in manifest.get("components", [])
        if item.get("kind") == "external"
    }
    unknown_external = sorted(requested_external - set(available_external))
    if unknown_external:
        raise ValueError(f"unknown external components: {unknown_external}")
    for name in sorted(requested_external):
        component = available_external[name]
        source_root = Path(component["source_root"]).resolve(strict=True)
        expected_root = Path(component["expected_root"]).resolve(strict=True)
        if source_root != expected_root:
            raise ValueError(f"external component path mismatch: {name}")
        actual_external = _external_files(
            source_root,
            tuple(component["include"]),
        )
        if actual_external != component["external_files"]:
            raise ValueError(f"external component drift: {name}")
        missing_required = sorted(
            set(component["required_files"]) - set(actual_external)
        )
        if missing_required:
            raise ValueError(
                f"external component is incomplete: {name}: {missing_required}"
            )
    return {
        "ok": True,
        "release": str(release),
        "git_commit": manifest["git_commit"],
        "release_sha256": release_sha256,
        "components": [item["name"] for item in manifest["components"]],
        "file_count": len(actual),
        "external_components": sorted(requested_external),
    }


def _source_argument(value: str) -> tuple[str, Path]:
    key, separator, path = value.partition("=")
    if not separator or not key or not path:
        raise argparse.ArgumentTypeError("snapshot source must use NAME=PATH")
    return key, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--repository", type=Path, required=True)
    prepare_parser.add_argument("--revision", required=True)
    prepare_parser.add_argument("--profiles", type=Path, required=True)
    prepare_parser.add_argument("--bundle", default="production")
    prepare_parser.add_argument(
        "--snapshot-source",
        action="append",
        default=[],
        type=_source_argument,
    )
    prepare_parser.add_argument(
        "--external-source",
        action="append",
        default=[],
        type=_source_argument,
    )
    prepare_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--release", type=Path, required=True)
    verify_parser.add_argument(
        "--external-component",
        action="append",
        default=[],
    )
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(
            repository=args.repository,
            revision=args.revision,
            profiles_path=args.profiles,
            bundle=args.bundle,
            source_roots=dict(args.snapshot_source + args.external_source),
            output=args.output,
        )
        output = {
            "release": str(args.output.expanduser().resolve()),
            "git_commit": result["git_commit"],
            "release_sha256": result["release_sha256"],
            "file_count": len(result["files"]),
        }
    elif args.command == "verify":
        output = verify(
            args.release,
            external_components=args.external_component,
        )
    else:
        release = args.release.expanduser().resolve(strict=True)
        manifest = json.loads((release / MANIFEST).read_text())
        output = {
            "release": str(release),
            "git_commit": manifest["git_commit"],
            "release_sha256": manifest["release_sha256"],
            "components": manifest["components"],
        }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
