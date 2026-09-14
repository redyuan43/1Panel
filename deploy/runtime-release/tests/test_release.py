from __future__ import annotations

import json
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_release",
    ROOT / "release.py",
)
runtime_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runtime_release)
prepare = runtime_release.prepare
verify = runtime_release.verify


def command(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def fixture(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    command("git", "init", "-q", cwd=repository)
    command("git", "config", "user.email", "test@example.com", cwd=repository)
    command("git", "config", "user.name", "Test", cwd=repository)
    (repository / "app").mkdir()
    (repository / "app/main.py").write_text("committed = True\n")
    (repository / "app/release-manifest.json").write_text('{"nested": true}\n')
    (repository / "tool.py").write_text("tool = True\n")
    command("git", "add", ".", cwd=repository)
    command("git", "commit", "-qm", "fixture", cwd=repository)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "runtime.sh").write_text("#!/bin/sh\nexit 0\n")
    (snapshot / "runtime.sh").chmod(0o755)
    (snapshot / "helper.txt").write_text("helper\n")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundles": {
                    "test": {
                        "components": [
                            {
                                "name": "app",
                                "kind": "git",
                                "source": "app",
                                "target": "app",
                                "include": ["**"],
                                "required_files": ["main.py"],
                            },
                            {
                                "name": "snapshot",
                                "kind": "snapshot",
                                "source_key": "runtime",
                                "target": "runtime",
                                "include": ["*"],
                                "required_files": ["runtime.sh"],
                            },
                            {
                                "name": "external",
                                "kind": "external",
                                "source_key": "runtime",
                                "expected_root": str(snapshot),
                                "include": ["helper.txt"],
                                "required_files": ["helper.txt"],
                            },
                        ]
                    }
                },
            }
        )
    )
    return repository, snapshot, profiles, commit


def build(tmp_path: Path):
    repository, snapshot, profiles, commit = fixture(tmp_path)
    output = tmp_path / "release"
    manifest = prepare(
        repository=repository,
        revision=commit,
        profiles_path=profiles,
        bundle="test",
        source_roots={"runtime": snapshot},
        output=output,
    )
    return repository, snapshot, profiles, commit, output, manifest


def test_prepare_reads_git_commit_not_dirty_worktree(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    (repository / "app/main.py").write_text("dirty = True\n")
    output = tmp_path / "release"
    manifest = prepare(
        repository=repository,
        revision=commit,
        profiles_path=profiles,
        bundle="test",
        source_roots={"runtime": snapshot},
        output=output,
    )
    assert (output / "app/main.py").read_text() == "committed = True\n"
    assert manifest["git_commit"] == commit
    snapshot_component = next(
        item for item in manifest["components"] if item["name"] == "snapshot"
    )
    assert snapshot_component["source_commit"] is None
    assert verify(output)["release_sha256"] == manifest["release_sha256"]


def test_verify_detects_content_and_file_set_drift(tmp_path: Path) -> None:
    *_, output, _manifest = build(tmp_path)
    path = output / "runtime/runtime.sh"
    path.chmod(0o755)
    path.write_text("changed\n")
    with pytest.raises(ValueError, match="release file drift"):
        verify(output)
    path.write_text("#!/bin/sh\nexit 0\n")
    output.chmod(0o755)
    (output / "extra").write_text("extra\n")
    with pytest.raises(ValueError, match="file set drift"):
        verify(output)


def test_prepare_refuses_existing_release(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    output = tmp_path / "release"
    output.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        prepare(
            repository=repository,
            revision=commit,
            profiles_path=profiles,
            bundle="test",
            source_roots={"runtime": snapshot},
            output=output,
        )


def test_prepare_refuses_missing_required_file(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    (snapshot / "runtime.sh").unlink()
    with pytest.raises(ValueError, match="missing required files"):
        prepare(
            repository=repository,
            revision=commit,
            profiles_path=profiles,
            bundle="test",
            source_roots={"runtime": snapshot},
            output=tmp_path / "release",
        )


def test_profile_rejects_path_escape(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    value = json.loads(profiles.read_text())
    value["bundles"]["test"]["components"][0]["target"] = "../escape"
    profiles.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="remain relative"):
        prepare(
            repository=repository,
            revision=commit,
            profiles_path=profiles,
            bundle="test",
            source_roots={"runtime": snapshot},
            output=tmp_path / "release",
        )


def test_selected_snapshot_symlink_is_rejected(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    (snapshot / "runtime.sh").unlink()
    (snapshot / "runtime.sh").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        prepare(
            repository=repository,
            revision=commit,
            profiles_path=profiles,
            bundle="test",
            source_roots={"runtime": snapshot},
            output=tmp_path / "release",
        )


def test_verify_rejects_added_symlink(tmp_path: Path) -> None:
    *_, output, _manifest = build(tmp_path)
    output.chmod(0o755)
    (output / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="release symlink"):
        verify(output)


def test_nested_manifest_is_covered_by_release_hashes(tmp_path: Path) -> None:
    *_, output, _manifest = build(tmp_path)
    nested = output / "app/release-manifest.json"
    nested.chmod(0o644)
    nested.write_text('{"nested": false}\n')
    with pytest.raises(ValueError, match="release file drift"):
        verify(output)


def test_external_component_drift_is_checked_on_request(tmp_path: Path) -> None:
    _repository, snapshot, _profiles, _commit, output, _manifest = build(
        tmp_path
    )
    assert verify(output, external_components=["external"])[
        "external_components"
    ] == ["external"]
    (snapshot / "helper.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="external component drift"):
        verify(output, external_components=["external"])


def test_external_component_must_match_runtime_path(tmp_path: Path) -> None:
    repository, snapshot, profiles, commit = fixture(tmp_path)
    unexpected = tmp_path / "unexpected"
    unexpected.mkdir()
    value = json.loads(profiles.read_text())
    value["bundles"]["test"]["components"][2]["expected_root"] = str(
        unexpected
    )
    profiles.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="does not match runtime"):
        prepare(
            repository=repository,
            revision=commit,
            profiles_path=profiles,
            bundle="test",
            source_roots={"runtime": snapshot},
            output=tmp_path / "release",
        )
