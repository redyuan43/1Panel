from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_router_image",
    ROOT / "build_router_image.py",
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def command(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    source = root / "deploy/ai-router"
    source.mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM scratch\n")
    (source / "payload").write_text("committed\n")
    command("git", "init", "-q", cwd=root)
    command("git", "config", "user.email", "test@example.com", cwd=root)
    command("git", "config", "user.name", "Test", cwd=root)
    command("git", "add", ".", cwd=root)
    command("git", "commit", "-qm", "fixture", cwd=root)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    return root, commit


def test_source_identity_uses_commit_instead_of_dirty_context(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    before = module.source_identity(root, commit)
    (root / "deploy/ai-router/payload").write_text("dirty\n")
    assert module.source_identity(root, commit) == before
    result = module.build(root, commit, "example:test", print_only=True)
    assert result == {
        "built": False,
        "image": "example:test",
        "source_revision": before[0],
        "source_tree_sha256": before[1],
    }


def test_unknown_revision_fails_closed(tmp_path: Path) -> None:
    root, _commit = repository(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        module.source_identity(root, "missing")
