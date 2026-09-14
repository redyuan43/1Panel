import hashlib
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_parallel_comparison as exporter
import watch_a4_realism_publication as watcher


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "batch"
    root.mkdir()
    tasks = []
    for index, case in enumerate(exporter.ALLOWED_CASES, 1):
        folder = root / case
        folder.mkdir()
        video = folder / "video.mp4"
        video.write_bytes(case.encode())
        tasks.append({"case": case, "status": "collected",
                      "prompt_id": f"00000000-0000-0000-0000-{index:012d}",
                      "gpu_uuid": f"GPU-00000000-0000-0000-0000-{index:012d}",
                      "endpoint": f"http://127.0.0.1:{18188 + index}",
                      "artifact_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                      "media": {"ok": True, "frame_count": 362, "fps": 24, "width": 480,
                                "height": 864, "duration_seconds": 362 / 24},
                      "execution": {"started_at": 10, "finished_at": 30, "execution_seconds": 20}})
    report = {"run_id": "fixed-batch", "status": "generated_pending_quality_review_drained",
              "lease_released": True, "both_unloaded": True, "finished_at": 40,
              "tasks": tasks, "original_extra_evidence": {"must": "survive"}}
    path = root / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    return root, tmp_path / "output", report


def rewrite(batch):
    root, _, report = batch
    (root / "report.json").write_text(json.dumps(report))


def test_default_and_dry_run_do_not_write(batch):
    root, output, _ = batch
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = exporter.export(root, output)
    assert result["mode"] == "check_only" and not result["published"]
    assert not output.exists()
    assert before == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_execute_preserves_raw_provenance_and_is_idempotent(batch, monkeypatch):
    root, output, original = batch
    raw = (root / "report.json").read_bytes()
    monkeypatch.setattr(watcher, "REMOTE_ROOT", str(output))
    monkeypatch.setattr(exporter.publisher, "publish", lambda *args: pytest.fail("export must not publish"))
    exporter.export(root, output, execute=True)
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in output.rglob("*") if path.is_file()}
    repeated = exporter.export(root, output, execute=True)
    assert all(item["already_present"] for item in repeated["cases"])
    assert before == {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in output.rglob("*") if path.is_file()}
    for case in exporter.ALLOWED_CASES:
        report = json.loads((output / case / "report.json").read_text())
        assert watcher.ready(report, case, case)
        assert report["artifact"] == str(output / case / "video.mp4")
        assert (output / case / "batch-report.json").read_bytes() == raw
        assert report["batch_provenance"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert json.loads(raw) == original
    assert not (output / "A4_C05").exists()


@pytest.mark.parametrize("field,value", [("status", "running"), ("status", "unagreed_progressive_success"),
                                        ("lease_released", False), ("lease_released", "true"),
                                        ("both_unloaded", False), ("finished_at", None),
                                        ("error", "failed"), ("reconciliation_error", "retained")])
def test_batch_failure_gates_precede_writes(batch, field, value):
    root, output, report = batch
    report[field] = value
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True)
    assert not output.exists()


def test_all_unloaded_alias(batch):
    root, output, report = batch
    report.pop("both_unloaded")
    report["all_unloaded"] = True
    rewrite(batch)
    assert exporter.export(root, output)["mode"] == "check_only"


@pytest.mark.parametrize("field,value", [("status", "sampled"), ("case", "A4_C05"),
                                        ("case", "../outside"), ("artifact_sha256", "wrong"),
                                        ("prompt_id", "not-an-id"), ("gpu_uuid", "short"),
                                        ("artifact", "/elsewhere/video.mp4")])
def test_task_failure_gates_precede_all_writes(batch, field, value):
    root, output, report = batch
    report["tasks"][1][field] = value
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True)
    assert not output.exists()


@pytest.mark.parametrize("field,value", [("ok", False), ("frame_count", 124), ("fps", 25),
                                        ("duration_seconds", 5), ("width", 0)])
def test_short_or_invalid_media_rejected(batch, field, value):
    root, output, report = batch
    report["tasks"][0]["media"][field] = value
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True)
    assert not output.exists()


def test_old_artifact_and_different_prompt_are_never_replaced(batch):
    root, output, report = batch
    exporter.export(root, output, execute=True)
    video = output / "A4_C1/video.mp4"
    video.write_bytes(b"older artifact")
    with pytest.raises(ValueError, match="SHA256"):
        exporter.export(root, output, execute=True)
    assert video.read_bytes() == b"older artifact"
    report["tasks"][0]["prompt_id"] = "10000000-0000-0000-0000-000000000000"
    rewrite(batch)
    with pytest.raises(ValueError, match="different prompt"):
        exporter.export(root, output, execute=True)
    assert video.read_bytes() == b"older artifact"


def test_protect_existing_c05_and_reject_symlink(batch):
    root, output, _ = batch
    output.mkdir()
    protected = output / "A4_C05"
    protected.mkdir()
    (protected / "report.json").write_text("old")
    (output / "A4_C1").symlink_to(protected, target_is_directory=True)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True)
    assert (protected / "report.json").read_text() == "old"


def test_cli_defaults_and_explicit_execute(batch, monkeypatch):
    root, output, _ = batch
    monkeypatch.setattr(exporter, "persistent_path", Path)
    arguments = ["--batch-root", str(root), "--output-root", str(output)]
    assert exporter.main(arguments) == 0 and not output.exists()
    assert exporter.main([*arguments, "--dry-run"]) == 0 and not output.exists()
    assert exporter.main([*arguments, "--execute"]) == 0 and output.exists()


@pytest.mark.parametrize("path", ["relative", "/tmp/batch", "/dev/shm/batch"])
def test_cli_requires_persistent_roots(path):
    with pytest.raises(ValueError):
        exporter.persistent_path(path)


def test_output_cannot_modify_input_tree(batch):
    root, _, _ = batch
    for output in (root, root / "nested-output"):
        with pytest.raises(ValueError, match="read-only"):
            exporter.export(root, output, execute=True)
    assert not (root / "nested-output").exists()


def add_replica(batch):
    root, _, report = batch
    replica = copy.deepcopy(report["tasks"][0])
    replica.update(case="A4_C05", prompt_id="00000000-0000-0000-0000-000000000003",
                   gpu_uuid="GPU-00000000-0000-0000-0000-000000000003",
                   endpoint="http://127.0.0.1:18190")
    folder = root / "A4_C05"
    folder.mkdir()
    (folder / "video.mp4").write_bytes(b"new replica")
    replica["artifact_sha256"] = hashlib.sha256(b"new replica").hexdigest()
    report["tasks"].append(replica)
    rewrite(batch)


def test_three_tasks_keep_replica_evidence_and_original_c05_unchanged(batch):
    root, output, _ = batch
    add_replica(batch)
    original = output / "A4_C05"
    original.mkdir(parents=True)
    (original / "report.json").write_bytes(b"original report")
    (original / "video.mp4").write_bytes(b"original video")
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
              for directory in (root, original) for path in directory.rglob("*") if path.is_file()}
    checked = exporter.export(root, output)
    assert [item["case"] for item in checked["cases"]] == ["A4_C1", "A4_C0"]
    result = exporter.export(root, output, execute=True)
    assert result["retained_batch_cases"] == checked["retained_batch_cases"]
    assert result["retained_batch_cases"][0]["artifact"] == str(root / "A4_C05/video.mp4")
    assert not result["published"]
    assert before == {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                      for directory in (root, original) for path in directory.rglob("*") if path.is_file()}
    for case in exporter.ALLOWED_CASES:
        assert (output / case / "batch-report.json").read_bytes() == (root / "report.json").read_bytes()
    assert all(item["already_present"] for item in exporter.export(root, output, execute=True)["cases"])


@pytest.mark.parametrize("failure", ["missing", "hash", "not_collected", "duplicate"])
def test_invalid_replica_blocks_export_before_writes(batch, failure):
    root, output, report = batch
    add_replica(batch)
    if failure == "missing":
        (root / "A4_C05/video.mp4").unlink()
    elif failure == "hash":
        (root / "A4_C05/video.mp4").write_bytes(b"tampered")
    elif failure == "not_collected":
        report["tasks"][-1]["status"] = "running"
    else:
        report["tasks"][-1]["case"] = "A4_C1"
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True)
    assert not output.exists()


@pytest.fixture
def failed_batch(batch, monkeypatch):
    root, output, report = batch
    add_replica(batch)
    report.update(status="failed", error="RuntimeError: history does not prove successful non-cached sampling",
                  peak_parallel=1, parallel_validated=False)
    report["tasks"][1].update(status="submitted", submit_attempted=True)
    report["tasks"][2].update(status="admission_waiting", submit_attempted=False)
    task = report["tasks"][0]
    task.update(submit_attempted=True, reconciliation="owned terminal history; empty queue",
                client_id="owned-client", prefix="video/failed-batch/" + task["case"],
                shape={"sampler_nodes": ["10"], "save_node": "13"})
    graph = {"10": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
             "13": {"class_type": "SaveVideo", "inputs": {"filename_prefix": task["prefix"]}}}
    task["submitted_workflow_sha256"] = exporter.digest(json.dumps(graph, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
    task["execution"].update(sampler_nodes=["10"], cached_nodes=[])
    task["media"].update(audio_codec="aac", audio_channels=2, audio_sample_rate="32000")
    identifier = task["prompt_id"]
    history = {identifier: {"prompt": [0, identifier, graph, {"client_id": task["client_id"]}, ["13"]],
        "status": {"completed": True, "status_str": "success", "messages": [
            ["execution_start", {"prompt_id": identifier, "timestamp": 10000}],
            ["execution_cached", {"prompt_id": identifier, "nodes": []}],
            ["execution_success", {"prompt_id": identifier, "timestamp": 30000}]]},
        "outputs": {"13": {"images": [{"type": "output", "subfolder": "video/failed-batch",
                                       "filename": task["case"] + "_00001_.mp4"}]}}}}
    probe = {"streams": [{"codec_type": "video", "width": 480, "height": 864,
                           "nb_read_frames": "362", "avg_frame_rate": "24/1"},
                          {"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "32000"}],
             "format": {"duration": str(362 / 24), "tags": {"prompt": json.dumps(graph)}}}
    (root / "A4_C1/history.json").write_text(json.dumps(history))
    (root / "A4_C1/media-probe.json").write_text(json.dumps(probe))
    rewrite(batch)

    def probe_only(command, **kwargs):
        assert command[0] == "ffprobe"
        assert command[-1] == str(root / "A4_C1/video.mp4")
        return SimpleNamespace(stdout=json.dumps(probe))

    monkeypatch.setattr(exporter.subprocess, "run", probe_only)
    return batch, history, probe


def test_partial_requires_explicit_flag_and_preserves_failed_batch_verbatim(failed_batch):
    batch, _, _ = failed_batch
    root, output, original = batch
    raw = (root / "report.json").read_bytes()
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="non-success"):
        exporter.export(root, output, execute=True)
    assert not output.exists()
    checked = exporter.export(root, output, allow_completed_from_failed_batch=True)
    assert not output.exists()
    assert checked["partial_export"] and checked["batch_status"] == "failed"
    result = exporter.export(root, output, execute=True, allow_completed_from_failed_batch=True)
    assert [item["case"] for item in result["cases"]] == ["A4_C1"]
    assert {item["case"] for item in result["skipped_tasks"]} == {"A4_C0", "A4_C05"}
    assert not (output / "A4_C0").exists() and not (output / "A4_C05").exists()
    normalized = json.loads((output / "A4_C1/report.json").read_text())
    assert normalized["partial_export"] is True and normalized["batch_failed"] is True
    assert normalized["batch_error"] == original["error"]
    assert normalized["batch_provenance"]["batch_status"] == "failed"
    assert (output / "A4_C1/batch-report.json").read_bytes() == raw
    for name in ("history.json", "media-probe.json"):
        assert (output / "A4_C1" / name).read_bytes() == (root / "A4_C1" / name).read_bytes()
    assert before == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert exporter.export(root, output, execute=True, allow_completed_from_failed_batch=True)["cases"][0]["already_present"]


@pytest.mark.parametrize("field,value", [("status", "running"), ("status", "needs_reconciliation"),
    ("lease_released", False), ("both_unloaded", False), ("all_unloaded", False),
    ("cleanup_error", "failed unload"), ("cleanup_errors", ["failed unload"]),
    ("reconciliation_error", "unknown queue"), ("retained_lease", {"owner": "old"}),
    ("lease_hold_error", "failed to hold lease"),
    ("finished_at", None), ("error", "")])
def test_partial_still_requires_terminal_and_clean_cleanup(failed_batch, field, value):
    batch, _, _ = failed_batch
    root, output, report = batch
    report[field] = value
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True, allow_completed_from_failed_batch=True)
    assert not output.exists()


@pytest.mark.parametrize("failure", ["missing_history", "foreign_prompt", "foreign_client", "graph_hash", "cached_sampler",
    "missing_cache_event", "not_success", "interrupted", "foreign_message", "timing", "output_prefix", "shape",
    "media_hash", "no_original_audio", "no_actual_audio", "actual_audio_codec", "actual_prompt", "actual_duration",
    "unsubmitted", "task_cleanup_error", "other_task_cleanup_error", "no_collected"])
def test_partial_rejects_unproven_task_before_writes(failed_batch, failure):
    batch, history, probe = failed_batch
    root, output, report = batch
    task = report["tasks"][0]
    record = history[task["prompt_id"]]
    if failure == "missing_history":
        (root / "A4_C1/history.json").unlink()
    elif failure == "foreign_prompt":
        record["prompt"][1] = "foreign"
    elif failure == "foreign_client":
        record["prompt"][3]["client_id"] = "foreign"
    elif failure == "graph_hash":
        task["submitted_workflow_sha256"] = "a" * 64
    elif failure == "cached_sampler":
        record["status"]["messages"][1][1]["nodes"] = ["10"]
    elif failure == "missing_cache_event":
        record["status"]["messages"].pop(1)
    elif failure == "not_success":
        record["status"]["status_str"] = "error"
    elif failure == "interrupted":
        record["status"]["messages"].append(["execution_interrupted", {"prompt_id": task["prompt_id"]}])
    elif failure == "foreign_message":
        record["status"]["messages"][0][1]["prompt_id"] = "foreign"
    elif failure == "timing":
        record["status"]["messages"][0][1]["timestamp"] += 100
    elif failure == "output_prefix":
        record["outputs"]["13"]["images"][0]["subfolder"] = "foreign"
    elif failure == "shape":
        task["shape"]["sampler_nodes"] = []
    elif failure == "media_hash":
        task["artifact_sha256"] = "bad"
    elif failure == "no_original_audio":
        original_probe = copy.deepcopy(probe)
        original_probe["streams"].pop()
        (root / "A4_C1/media-probe.json").write_text(json.dumps(original_probe))
    elif failure == "no_actual_audio":
        probe["streams"].pop()
    elif failure == "actual_audio_codec":
        probe["streams"][1]["codec_name"] = "mp3"
    elif failure == "actual_prompt":
        probe["format"]["tags"]["prompt"] = "{}"
    elif failure == "actual_duration":
        probe["format"]["duration"] = "5"
    elif failure == "unsubmitted":
        task["submit_attempted"] = False
    elif failure == "task_cleanup_error":
        task["cleanup_error"] = "cannot unload"
    elif failure == "other_task_cleanup_error":
        report["tasks"][1]["reconciliation_error"] = "unknown outcome"
    else:
        task["status"] = "submitted"
    if failure != "missing_history":
        (root / "A4_C1/history.json").write_text(json.dumps(history))
    rewrite(batch)
    with pytest.raises(ValueError):
        exporter.export(root, output, execute=True, allow_completed_from_failed_batch=True)
    assert not output.exists()


def test_partial_cli_dry_run_and_evidence_tamper_rejection(failed_batch, monkeypatch):
    batch, _, _ = failed_batch
    root, output, _ = batch
    monkeypatch.setattr(exporter, "persistent_path", Path)
    args = ["--batch-root", str(root), "--output-root", str(output), "--allow-completed-from-failed-batch"]
    assert exporter.main([*args, "--dry-run"]) == 0 and not output.exists()
    assert exporter.main([*args, "--execute"]) == 0
    (output / "A4_C1/history.json").write_text("tampered")
    assert exporter.main([*args, "--execute"]) == 1
