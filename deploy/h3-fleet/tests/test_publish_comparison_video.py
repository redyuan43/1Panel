import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_comparison_video as publisher


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test video")
    report = {
        "case": "A8", "status": "generated_pending_quality_review",
        "lease_released": True, "isolated_unload_confirmed": True,
        "media": {"ok": True, "frame_count": 362, "fps": 24, "width": 480,
                  "height": 864, "duration_seconds": 362 / 24},
        "execution": {"execution_seconds": 100}, "prompt_id": "test-prompt",
        "isolated_gpu_uuid": "test-gpu",
        "artifact_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    destination = tmp_path / "gallery"
    destination.mkdir()
    (destination / "index.json").write_text(json.dumps({"cases": [{"id": "A8"}]}))
    streams = [{"codec_type": "video", "nb_read_frames": "362", "avg_frame_rate": "24/1",
                "width": 480, "height": 864}, {"codec_type": "audio"}]
    monkeypatch.setattr(publisher.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps({"streams": streams})))
    return report, report_path, video, destination


def test_publish_is_idempotent_and_never_approves_quality(candidate):
    report, report_path, video, destination = candidate
    for _ in range(2):
        result = publisher.publish(report_path, video, destination)
        assert result["review_status"] == "pending_human_review"
        assert result["artifact_sha256"] == report["artifact_sha256"]
    assert (destination / "A8.mp4").read_bytes() == video.read_bytes()


@pytest.mark.parametrize("case", ["A4_C0", "A4_C05", "A4_C1"])
def test_append_new_case_preserves_existing(candidate, case):
    report, report_path, video, destination = candidate
    report["case"] = case
    report_path.write_text(json.dumps(report))
    for _ in range(2):
        publisher.publish(report_path, video, destination)
    records = json.loads((destination / "index.json").read_text())["cases"]
    assert records[0] == {"id": "A8"}
    assert len(records) == 2 and records[1]["id"] == case


@pytest.mark.parametrize("field,value", [("status", "running"), ("lease_released", False),
                                        ("isolated_unload_confirmed", False),
                                        ("artifact_sha256", "wrong"), ("case", "../../bad")])
def test_rejects_unverified_report(candidate, field, value):
    report, _, video, _ = candidate
    report[field] = value
    with pytest.raises(ValueError):
        publisher.validate_report(report, video)


def test_never_replaces_another_published_video(candidate):
    _, report_path, video, destination = candidate
    (destination / "A8.mp4").write_bytes(b"earlier result")
    with pytest.raises(ValueError, match="refusing to replace"):
        publisher.publish(report_path, video, destination)
    assert (destination / "A8.mp4").read_bytes() == b"earlier result"


def test_decode_failure_does_not_publish(candidate, monkeypatch):
    _, report_path, video, destination = candidate
    def fail(*args, **kwargs):
        raise publisher.subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(publisher.subprocess, "run", fail)
    with pytest.raises(publisher.subprocess.CalledProcessError):
        publisher.publish(report_path, video, destination)
    assert not (destination / "A8.mp4").exists()


@pytest.mark.parametrize("placeholders", [False, True])
def test_new_c1_c0_complete_ten_cards_without_touching_original_seven_or_c05(candidate, placeholders):
    report, report_path, video, destination = candidate
    protected_ids = ["R0", "A4", "A8", "B8", "C0", "C1", "D4", "A4_C05"]
    protected_records = [{"id": case, "label": "original " + case,
                          "video": "comparison-results/" + case + ".mp4",
                          "prompt_id": "original-" + case, "review_status": "pending_human_review"}
                         for case in protected_ids]
    for case in protected_ids:
        (destination / (case + ".mp4")).write_bytes(("original " + case).encode())
    files_before = {case: ((destination / (case + ".mp4")).read_bytes(),
                          (destination / (case + ".mp4")).stat().st_mtime_ns) for case in protected_ids}
    records = protected_records + ([{"id": "A4_C1"}, {"id": "A4_C0"}] if placeholders else [])
    (destination / "index.json").write_text(json.dumps({"cases": records}))
    for case in ("A4_C1", "A4_C0", "A4_C1", "A4_C0"):
        report.update(case=case, prompt_id="new-" + case)
        report_path.write_text(json.dumps(report))
        publisher.publish(report_path, video, destination)
    published = json.loads((destination / "index.json").read_text())["cases"]
    assert len(published) == 10
    assert len({record["id"] for record in published}) == 10
    assert published[:8] == protected_records
    assert files_before == {case: ((destination / (case + ".mp4")).read_bytes(),
                                   (destination / (case + ".mp4")).stat().st_mtime_ns) for case in protected_ids}
    assert all(record["review_status"] == "pending_human_review" for record in published)


@pytest.mark.parametrize("case", ["A4_C1", "A4_C0", "A4_C05"])
def test_new_case_refuses_different_existing_video_without_changing_manifest(candidate, case):
    report, report_path, video, destination = candidate
    report["case"] = case
    report_path.write_text(json.dumps(report))
    target = destination / (case + ".mp4")
    target.write_bytes(b"protected older video")
    index = destination / "index.json"
    before = index.read_bytes()
    with pytest.raises(ValueError, match="refusing to replace"):
        publisher.publish(report_path, video, destination)
    assert target.read_bytes() == b"protected older video"
    assert index.read_bytes() == before
