#!/usr/bin/env python3
"""Build the Router image only from an exact Git commit archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


SOURCE = "deploy/ai-router"


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    ).stdout


def source_identity(repository: Path, revision: str) -> tuple[str, str]:
    repository = repository.resolve(strict=True)
    commit = _git(
        repository,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
    ).decode().strip()
    listing = _git(
        repository,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        SOURCE,
    )
    if not listing:
        raise ValueError("Router source is missing from the selected commit")
    return commit, hashlib.sha256(listing).hexdigest()


def _safe_extract(archive: Path, output: Path) -> Path:
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise ValueError(f"unsafe Git archive member: {member.name}")
        bundle.extractall(output)
    context = output / SOURCE
    if not (context / "Dockerfile").is_file():
        raise ValueError("Router Dockerfile is missing from Git archive")
    return context


def build(
    repository: Path,
    revision: str,
    image: str,
    *,
    print_only: bool = False,
) -> dict[str, str | bool]:
    repository = repository.resolve(strict=True)
    commit, tree_sha256 = source_identity(repository, revision)
    result: dict[str, str | bool] = {
        "built": False,
        "image": image,
        "source_revision": commit,
        "source_tree_sha256": tree_sha256,
    }
    if print_only:
        return result
    with tempfile.TemporaryDirectory(prefix="ai-router-build-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "archive",
                    "--format=tar",
                    commit,
                    SOURCE,
                ],
                check=True,
                stdout=output,
            )
        context = _safe_extract(archive, root / "context")
        subprocess.run(
            [
                "docker",
                "build",
                "--build-arg",
                f"SOURCE_REVISION={commit}",
                "--build-arg",
                f"SOURCE_TREE_SHA256={tree_sha256}",
                "--tag",
                image,
                str(context),
            ],
            check=True,
        )
    inspected = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image])
    )[0]
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if labels.get("org.opencontainers.image.revision") != commit:
        raise RuntimeError("built image revision label does not match")
    if labels.get("org.opencontainers.image.source-tree-sha256") != tree_sha256:
        raise RuntimeError("built image source tree label does not match")
    result["built"] = True
    result["image_id"] = str(inspected["Id"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.repository,
                args.revision,
                args.image,
                print_only=args.print_only,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
