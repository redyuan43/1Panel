from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "integrations/workbuddy/skillhub-video-reference"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def test_skillhub_archive_matches_manifest_and_is_non_executable() -> None:
    manifest = json.loads((ARCHIVE / "MANIFEST.json").read_text(encoding="utf-8"))
    policy = manifest["archive_policy"]
    assert policy["requested_directory_count"] == 19
    assert policy["supplemental_directory_count"] == 1
    assert policy["captured_directory_count"] == 20
    assert policy["captured_file_count"] == 112
    assert len(manifest["builtin_capabilities"]) + policy["requested_directory_count"] == 22

    skills = {item["directory"]: item for item in manifest["skills"]}
    directories = {
        path.name
        for path in (ARCHIVE / "raw").iterdir()
        if path.is_dir()
    }
    assert set(skills) == directories
    assert sum(item["capture_class"] == "requested" for item in skills.values()) == 19
    assert sum(item["capture_class"] == "supplemental_alias" for item in skills.values()) == 1
    assert all("license" in item and "status" in item["license"] for item in skills.values())

    files = {
        path.relative_to(ARCHIVE).as_posix(): path
        for path in (ARCHIVE / "raw").rglob("*")
        if path.is_file()
    }
    recorded = {item["path"]: item for item in manifest["files"]}
    assert set(recorded) == set(files)
    assert len(files) == policy["captured_file_count"]
    for name, path in files.items():
        assert path.stat().st_size == recorded[name]["size"]
        assert digest(path) == recorded[name]["sha256"]
        assert not stat.S_IMODE(path.stat().st_mode) & 0o111


def test_runtime_uses_only_distilled_local_rules() -> None:
    profile = json.loads(
        (ROOT / "ai_router/media_service/prompt_profiles.json").read_text(encoding="utf-8")
    )
    assert profile["schema_version"] == 1
    assert profile["policy"]["external_api_dependency"] is False
    assert set(profile["categories"]) == {
        "h3",
        "cinematography",
        "first_last_frame",
        "commercial",
        "quality_feedback",
    }

    runtime_files = [
        *sorted((ROOT / "ai_router").rglob("*.py")),
        ROOT / "scripts/media-operations.py",
    ]
    forbidden = (
        "skillhub-video-reference/raw",
        "api.aihive",
        "api.ai-hive",
    )
    for path in runtime_files:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        assert not any(value in text for value in forbidden), path
