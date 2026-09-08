from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/activate-h3-managed-media.py"
SPEC = importlib.util.spec_from_file_location("managed_media_activation", SCRIPT)
assert SPEC and SPEC.loader
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


def write_candidate(root: Path) -> Path:
    candidate = root / "candidate"
    for relative in activation.REQUIRED_RELEASE_FILES:
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "pass\n", encoding="utf-8")
    lines = []
    for path in sorted(candidate.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(candidate).as_posix()}")
    (candidate / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return candidate


def test_validate_candidate_requires_exact_hashes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(activation, "RELEASES", tmp_path)
    candidate = write_candidate(tmp_path)
    report = activation.validate_candidate(candidate)
    assert report["file_count"] == len(activation.REQUIRED_RELEASE_FILES)
    assert len(report["manifest_sha256"]) == 64
    target = candidate / "ai_router/media_service/service.py"
    target.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        activation.validate_candidate(candidate)


def test_validate_candidate_rejects_symlink(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(activation, "RELEASES", tmp_path)
    candidate = write_candidate(tmp_path)
    link = tmp_path / "linked-candidate"
    link.symlink_to(candidate, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic link"):
        activation.validate_candidate(link)


def test_active_work_only_reports_executing_stages() -> None:
    jobs = [
        {
            "id": "waiting",
            "kind": "video",
            "status": "in_progress",
            "stages": [{"id": "preview", "status": "awaiting_approval"}],
        },
        {
            "id": "running",
            "kind": "video",
            "status": "in_progress",
            "stages": [{"id": "preview", "status": "running"}],
        },
        {"id": "image", "kind": "image", "status": "queued", "stages": []},
    ]
    assert activation.active_work(jobs) == [
        {"id": "running", "status": "in_progress", "stages": ["preview"]},
        {"id": "image", "status": "queued", "stages": []},
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://100.96.79.21:8789",
        "http://8.8.8.8:8789",
        "http://100.96.79.21:8789/api",
    ],
)
def test_private_executor_rejects_unapproved_origins(value: str) -> None:
    with pytest.raises(RuntimeError):
        activation.private_executor(value)


def test_default_main_inspects_without_restarting(monkeypatch, tmp_path) -> None:
    candidate = tmp_path / "candidate"
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        activation,
        "inspect",
        lambda _: {
            "candidate": {"path": str(candidate)},
            "current_release": "old",
            "service": {"ActiveState": "active"},
            "active_work": [],
            "credential_readiness": {"media_internal": True},
        },
    )
    monkeypatch.setattr(
        activation.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("inspect mode must not restart a service")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--candidate",
            str(candidate),
            "--report",
            str(report),
        ],
    )
    assert activation.main() == 0
    assert report.is_file()
