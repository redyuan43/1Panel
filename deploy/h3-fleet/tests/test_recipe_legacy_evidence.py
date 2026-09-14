from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from app import recipe_legacy_evidence as legacy
from app.recipes import RecipeCatalog
from scripts import import_recipe_legacy_evidence as importer


GPU = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"


def write_reference(path, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def selector(reference, *pointer):
    return {"reference": reference, "pointer": list(pointer)}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "persistent_path", lambda value: Path(value))
    monkeypatch.setattr(importer, "persistent_path", lambda value: Path(value))

    def make(recipe_id="A4", batch=False):
        root = tmp_path / recipe_id
        root.mkdir()
        catalog = RecipeCatalog()
        recipe = catalog.get(recipe_id)
        graph, binding = catalog.build(recipe_id, "a pink advertisement", 42, "video/legacy/test")
        workflow = write_reference(root / "workflow.json", graph)
        video = root / "video.mp4"
        video.write_bytes(b"fixture video, never used with real ffmpeg")
        artifact = {"path": str(video), "sha256": legacy.file_sha256(video)}
        identifier = "00ee52ca-9feb-427c-b5d7-8a32150588f2"
        history = {identifier: {
            "prompt": [0, identifier, graph, {"client_id": "owned"}],
            "status": {"completed": True, "status_str": "success", "messages": [
                ["execution_start", {"prompt_id": identifier, "timestamp": 100000}],
                ["execution_cached", {"prompt_id": identifier, "nodes": []}],
                ["execution_success", {"prompt_id": identifier, "timestamp": 700000}],
            ]},
            "outputs": {"13": {"images": [{"filename": "test_00001_.mp4", "subfolder": "video/legacy", "type": "output"}]}},
        }}
        probe = {"streams": [
            {"codec_type": "video", "width": 480, "height": 864, "nb_read_frames": "362", "avg_frame_rate": "24/1"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "32000", "duration": "15.075"},
        ], "format": {"size": str(video.stat().st_size), "duration": "15.083333", "tags": {"prompt": json.dumps(graph)}}}
        proof = {"artifact": artifact, "started_at": 800, "finished_at": 801,
                 "ffprobe": {"argv": legacy.probe_command(video), "returncode": 0, "stderr": "", "probe": probe},
                 "full_decode": {"argv": legacy.decode_command(video), "returncode": 0, "stderr": ""}}
        task = {"case": recipe_id, "prompt_id": identifier, "client_id": "owned", "prefix": "video/legacy/test",
                "submit_attempted": True, "workflow_sha256": workflow["sha256"],
                "artifact_sha256": artifact["sha256"], "isolated_gpu_uuid": GPU, "isolated_unit": "old.service",
                "execution": {"sampler_nodes": ["10"], "cached_nodes": [], "started_at": 100, "finished_at": 700,
                              "execution_seconds": 600},
                "media": {"ok": True, "width": 480, "height": 864, "fps": 24, "frame_count": 362}}
        identity = {"Id": "old.service", "MainPID": "123", "gpu_uuid": GPU}
        if batch:
            task.update(status="collected", gpu_uuid=GPU, unit="old.service",
                        submitted_workflow_sha256=workflow["sha256"], reconciliation="owned terminal history; empty queue")
            report = {"status": "generated_pending_quality_review_drained", "lease_released": True,
                      "both_unloaded": True, "finished_at": 750, "tasks": [task],
                      "identities": {"workers": {recipe_id: identity}}}
        else:
            report = {**task, "status": "generated_pending_quality_review", "lease_released": True,
                      "finished_at": 750, "isolated_unload_confirmed": True, "baseline": {"isolated": identity}}
        sources = {legacy.ALIASES[name]: checksum for name, checksum in recipe["runtime_source"].get("source_sha256", {}).items()}
        sources.update({recipe["runtime_source"]["plugin_directory"] + "/" + name: checksum
                        for name, checksum in recipe["runtime_source"].get("plugin_python_files", {}).items()})
        source_manifest = {"status": "prepared_not_imported_not_started", "target": "/old/runtime",
                           "files": [{"path": name, "sha256": checksum} for name, checksum in sources.items()]}
        argv = ["/usr/bin/python3", "/old/runtime/main.py", "--port", "18188", "--reserve-vram", "3", "--disable-pinned-memory"]
        weights = {item["filename"]: item.get("sha256") or "a" * 64
                   for item in [*recipe["weights"], *recipe.get("metadata_files", [])]}
        runtime_source = write_reference(root / "launch.json", {"argv": argv, **identity, "weights": weights})
        document = {"schema_version": 1, "qualification": legacy.QUALIFICATION, "recipe_id": recipe_id,
                    "recipe_version": binding["recipe_version"], "recipe_digest": binding["recipe_digest"],
                    "expected_runtime_version": "f" * 64, "gpu_uuid": GPU, "upstream_prompt_id": identifier,
                    "single_task_only": True, "parallel_validated": False, "ws_validated": False,
                    "source_report": write_reference(root / "report.json", report), "workflow": workflow,
                    "history": write_reference(root / "history.json", history), "artifact": artifact,
                    "media_probe": write_reference(root / "probe.json", probe),
                    "artifact_validation": write_reference(root / "decode.json", proof),
                    "runtime": {"manifest": write_reference(root / "manifest.json", source_manifest),
                                "argv": selector(runtime_source, "argv"), "gpu_uuid": selector(runtime_source, "gpu_uuid"),
                                "unit": selector(runtime_source, "Id"), "pid": selector(runtime_source, "MainPID")},
                    "weights": [{"filename": name, "sha256_source": selector(runtime_source, "weights", name)} for name in weights]}
        backend = {"gpu_uuid": GPU, "runtime_version": "f" * 64, "runtime_root": "/new/runtime", "pid": 456,
                   "argv": ["/other/python3", "/new/runtime/main.py", "--port=18489", *argv[4:]],
                   "runtime_files": [{"path": "/new/runtime/" + name, "sha256": checksum} for name, checksum in sources.items()],
                   "weight_files": [{"filename": name, "sha256": checksum} for name, checksum in weights.items()]}
        backend["cmdline_sha256"] = hashlib.sha256(("\0".join(backend["argv"]) + "\0").encode()).hexdigest()
        return document, backend, catalog
    return make


def change_reference(document, key, change):
    reference = document[key]
    value = legacy.pinned_json(reference)
    change(value)
    document[key] = write_reference(Path(reference["path"]), value)


@pytest.mark.parametrize("recipe_id", ["A4", "A4_C0", "A4_C1", "B8"])
def test_formal_history_only_never_promotes_parallel(evidence, recipe_id):
    document, backend, catalog = evidence(recipe_id, batch=recipe_id.startswith("A4_"))
    before = copy.deepcopy(document)
    receipt = legacy.validate_legacy_single(document, backend, catalog)
    assert receipt["qualification"] == "historical_single_completed"
    assert receipt["full_video_validated"] is True
    assert receipt["parallel_validated"] is receipt["ws_validated"] is False
    assert receipt["historical_host_exclusive"] == "not_established"
    assert receipt["all_stage_no_oom"] == "not_established"
    assert receipt["historical_runtime"]["pid"] == "123"
    assert document == before


def test_failed_batch_completed_task_explicit_optin_preserves_error(evidence):
    document, backend, catalog = evidence("A4_C1", batch=True)
    change_reference(document, "source_report", lambda value: value.update(status="failed", error="other task failed"))
    with pytest.raises(ValueError, match="opt-in"):
        legacy.validate_legacy_single(document, backend, catalog)
    document["allow_completed_from_failed_batch"] = True
    receipt = legacy.validate_legacy_single(document, backend, catalog)
    assert receipt["source_status"] == "failed"
    assert receipt["source_error"] == "other task failed"
    assert receipt["source_task_status"] == "collected"


@pytest.mark.parametrize("key,value", [
    ("recipe_version", "old"), ("recipe_digest", "a" * 64), ("expected_runtime_version", "b" * 64),
    ("recipe_id", "A4_C05"), ("gpu_uuid", "GPU-foreign"), ("upstream_prompt_id", "foreign"),
    ("parallel_validated", True), ("ws_validated", True), ("single_task_only", False),
    ("sampler_progress", {"fabricated": True}), ("schema_version", 2),
])
def test_binding_and_scope_fail_closed(evidence, key, value):
    document, backend, catalog = evidence()
    document[key] = value
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


@pytest.mark.parametrize("missing", ["history", "artifact_validation", "runtime", "weights", "media_probe"])
def test_missing_evidence_is_valueerror(evidence, missing):
    document, backend, catalog = evidence()
    del document[missing]
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


@pytest.mark.parametrize("kind", ["cached", "foreign", "unowned", "failed", "unfinished", "client", "output", "graph", "no_cache_event"])
def test_history_rejects_invalid_or_cached_execution(evidence, kind):
    document, backend, catalog = evidence()
    def change(value):
        record = next(iter(value.values()))
        if kind == "cached":
            record["status"]["messages"][1][1]["nodes"] = ["10"]
        elif kind in {"foreign", "unowned"}:
            record["status"]["messages"][0][1]["prompt_id"] = "foreign" if kind == "foreign" else None
        elif kind == "failed":
            record["status"]["messages"].append(["execution_error", {"prompt_id": document["upstream_prompt_id"]}])
        elif kind == "unfinished":
            record["status"]["completed"] = False
        elif kind == "client":
            record["prompt"][3]["client_id"] = "foreign"
        elif kind == "output":
            record["outputs"]["13"]["images"][0]["filename"] = "other_00001_.mp4"
        elif kind == "graph":
            record["prompt"][2]["7"]["inputs"]["steps"] = 8
        else:
            del record["status"]["messages"][1]
    change_reference(document, "history", change)
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


@pytest.mark.parametrize("kind", ["short", "embedded", "decode_fail", "decode_truncated", "sha", "old_proof"])
def test_actual_video_proof_required(evidence, kind):
    document, backend, catalog = evidence()
    def change(value):
        if kind == "short":
            value["ffprobe"]["probe"]["streams"][0]["nb_read_frames"] = "120"
        elif kind == "embedded":
            value["ffprobe"]["probe"]["format"]["tags"]["prompt"] = "{}"
        elif kind == "decode_fail":
            value["full_decode"]["returncode"] = 1
        elif kind == "decode_truncated":
            value["full_decode"]["argv"].extend(["-t", "5"])
        elif kind == "old_proof":
            value["artifact"]["sha256"] = "0" * 64
    if kind == "sha":
        Path(document["artifact"]["path"]).write_bytes(b"changed")
    else:
        change_reference(document, "artifact_validation", change)
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


@pytest.mark.parametrize("kind", ["reserve", "missing_argv", "node_hash", "weight_hash", "missing_weight", "missing_node"])
def test_runtime_settings_nodes_and_weights_are_required(evidence, kind):
    document, backend, catalog = evidence()
    if kind == "reserve":
        backend["argv"][-2] = "1"
        backend["cmdline_sha256"] = hashlib.sha256(("\0".join(backend["argv"]) + "\0").encode()).hexdigest()
    elif kind == "missing_argv":
        del backend["argv"]
    elif kind == "node_hash":
        backend["runtime_files"][0]["sha256"] = "e" * 64
    elif kind == "weight_hash":
        backend["weight_files"][0]["sha256"] = "e" * 64
    elif kind == "missing_weight":
        document["weights"].pop()
    else:
        backend["runtime_files"].pop()
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


def test_pinned_evidence_entry_and_tamper(evidence, tmp_path):
    document, backend, catalog = evidence()
    entry = {"qualification": legacy.QUALIFICATION, "recipe_version": document["recipe_version"],
             "evidence": write_reference(tmp_path / "entry.json", document)}
    assert legacy.validate_legacy_single(entry, backend, catalog)["full_video_validated"]
    Path(entry["evidence"]["path"]).write_text("{}")
    with pytest.raises(ValueError, match="SHA256"):
        legacy.validate_legacy_single(entry, backend, catalog)


def test_current_argv_must_match_pinned_process_command(evidence):
    document, backend, catalog = evidence()
    backend["cmdline_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="pinned process command"):
        legacy.validate_legacy_single(document, backend, catalog)


def test_failed_batch_task_cleanup_error_cannot_be_ignored(evidence):
    document, backend, catalog = evidence("A4_C1", batch=True)
    change_reference(document, "source_report", lambda value: value["tasks"][0].update(cleanup_error="identity lost"))
    with pytest.raises(ValueError, match="task cleanup"):
        legacy.validate_legacy_single(document, backend, catalog)


@pytest.mark.parametrize("key", ["lease_released", "isolated_unload_confirmed"])
def test_unconfirmed_cleanup_rejected(evidence, key):
    document, backend, catalog = evidence()
    change_reference(document, "source_report", lambda value: value.update({key: False}))
    with pytest.raises(ValueError):
        legacy.validate_legacy_single(document, backend, catalog)


def test_original_and_backend_not_mutated_and_dry_run_no_subprocess(evidence, monkeypatch):
    document, backend, catalog = evidence()
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("dry run subprocess"))
    before = copy.deepcopy((document, backend))
    assert importer.import_evidence(document, backend, catalog)["full_video_validated"]
    assert (document, backend) == before


def test_import_runs_cpu_decode_keeps_original_and_refuses_overwrite(evidence, tmp_path, monkeypatch):
    document, backend, catalog = evidence()
    original = Path(document["artifact"]["path"]).read_bytes()
    probe = legacy.pinned_json(document["media_probe"])
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"capture_output": True, "text": True, "timeout": 180, "check": True}
        return subprocess.CompletedProcess(argv, 0, json.dumps(probe) if argv[0] == "ffprobe" else "", "")
    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "new-import"
    document.pop("artifact_validation")
    result = importer.import_evidence(document, backend, catalog, output, execute=True)
    assert len(calls) == 2 and "none" in calls[1]
    assert result["entry"]["qualification"] == legacy.QUALIFICATION
    assert sorted(path.name for path in output.iterdir()) == ["artifact-validation.json", "entry.json", "evidence.json"]
    assert Path(document["artifact"]["path"]).read_bytes() == original
    assert legacy.validate_legacy_single(result["entry"], backend, catalog)["full_video_validated"]
    with pytest.raises(ValueError, match="overwrite"):
        importer.import_evidence(document, backend, catalog, output, execute=True)


def test_duplicate_json_keys_and_temporary_references_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        legacy.read_json('{"ok":true,"ok":false}')
    with pytest.raises(ValueError, match="persistent"):
        legacy.persistent_path("/tmp/legacy-evidence.json")
    with pytest.raises(ValueError, match="absolute"):
        legacy.persistent_path("relative.json")


@pytest.mark.parametrize("reserve", ["1", "3", "6"])
def test_compute_argv_contract_preserves_reserve(evidence, reserve):
    document, backend, catalog = evidence()
    backend["compute_argv"] = legacy.runtime_arguments(backend.pop("argv"))
    backend["compute_argv"][-2] = reserve
    if reserve == "3":
        assert legacy.validate_legacy_single(document, backend, catalog)["full_video_validated"]
    else:
        with pytest.raises(ValueError, match="arguments differ"):
            legacy.validate_legacy_single(document, backend, catalog)


@pytest.fixture
def stage_fixture(evidence, tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    backends, weights, sources = [], {}, {}
    argv = None
    for recipe_id, relative in importer.LEGACY_CASES.items():
        document, backend, catalog = evidence(recipe_id, batch=recipe_id.startswith("A4_"))
        source_root = Path(document["artifact"]["path"]).parent
        folder = root / relative
        shutil.copytree(source_root, folder)
        shutil.copyfile(folder / "probe.json", folder / "media-probe.json")
        if recipe_id.startswith("A4_"):
            shutil.copyfile(folder / "report.json", folder.parent / "report.json")
        backend["recipes"] = {recipe_id: {}}
        backend["compute_argv"] = legacy.runtime_arguments(backend.pop("argv"))
        backends.append(backend)
        weights.update({item["filename"]: item["sha256"] for item in backend["weight_files"]})
        manifest = legacy.pinned_json(document["runtime"]["manifest"])
        sources.update({item["path"]: item["sha256"] for item in manifest["files"]})
        argv = legacy.selected(document["runtime"]["argv"])
    write_reference(root / "runtime-prepare-r2.json", {"status": "prepared_not_imported_not_started", "target": "/old/runtime",
                                                     "files": [{"path": name, "sha256": checksum} for name, checksum in sources.items()]})
    stage = {"backends": backends, "historical_units": {"old.service": {"argv": argv}},
             "historical_weight_files": [{"filename": name, "sha256": checksum} for name, checksum in weights.items()]}
    stage_path = tmp_path / "runtime-stage.json"
    write_reference(stage_path, stage)
    return stage_path, root, catalog


def test_build_all_four_real_schema_documents_without_handwritten_fields(stage_fixture):
    stage_path, root, catalog = stage_fixture
    plans = importer.build_documents(stage_path, root, catalog)
    assert [plan["recipe_id"] for plan in plans] == ["A4", "A4_C0", "A4_C1", "B8"]
    for plan in plans:
        document = plan["document"]
        assert document["expected_runtime_version"] == plan["backend"]["runtime_version"]
        assert document["parallel_validated"] is False
        assert "artifact_validation" not in document
        with pytest.raises(ValueError, match="artifact_validation"):
            legacy.validate_legacy_single(document, plan["backend"], catalog)


@pytest.mark.parametrize("missing", ["historical_weight_files", "historical_units"])
def test_builder_never_invents_historical_runtime_facts(stage_fixture, missing):
    stage_path, root, catalog = stage_fixture
    stage = json.loads(stage_path.read_text())
    del stage[missing]
    write_reference(stage_path, stage)
    with pytest.raises(ValueError, match="missing historical|missing captured"):
        importer.build_documents(stage_path, root, catalog)


@pytest.fixture
def continuity_fixture(evidence, tmp_path):
    evidence()
    directory = tmp_path / "models" / "vae"
    directory.mkdir(parents=True)
    name = "minimax_h3_video_vae_fp16.safetensors"
    path = directory / name
    path.write_bytes(b"shared existing weight")
    mapping = {"shared": {"base_path": str(directory.parent), "vae": "vae"}}
    config = write_reference(tmp_path / "extra_model_paths.yaml", mapping)
    checksum = legacy.file_sha256(path)
    stage = write_reference(tmp_path / "stage.json", {"prepared_at": time.time() + 1, "hash": checksum})
    item = {"filename": name, "continuity": "unchanged_file_metadata_inference",
            "stat_snapshot": legacy.stat_snapshot(path), "model_paths": config,
            "sha256_source": selector(stage, "hash")}
    declared = {"filename": name, "path": str(path), "sha256": checksum}
    source = {"files": [{"path": "extra_model_paths.yaml", "sha256": config["sha256"]}], "model_search_paths": mapping}
    current = {"extra_model_paths.yaml": config["sha256"]}
    return item, declared, source, current, time.time() + 10


def test_continuity_honestly_classifies_current_hash_not_historical(continuity_fixture):
    receipt = legacy.validate_weight_continuity(*continuity_fixture)
    assert receipt["historical_hash_recorded"] is False
    assert receipt["historical_sha256"] is None
    assert receipt["continuity"] == "unchanged_file_metadata_inference"
    assert receipt["current_hash_verified"] is True


@pytest.mark.parametrize("kind", ["ctime", "stat_changed", "config_hash", "different_path", "known_weight", "stale_hash", "symlink"])
def test_continuity_fails_closed(continuity_fixture, kind, tmp_path):
    item, declared, source, current, started = continuity_fixture
    if kind == "ctime":
        started = 100
    elif kind == "stat_changed":
        Path(declared["path"]).write_bytes(b"modified after snapshot")
    elif kind == "config_hash":
        current["extra_model_paths.yaml"] = "a" * 64
    elif kind == "different_path":
        declared["path"] = str(tmp_path / "other")
    elif kind == "known_weight":
        item["filename"] = "minimax_h3_fl2va_int8_convrot.safetensors"
    elif kind == "stale_hash":
        stage = item["sha256_source"]["reference"]
        item["sha256_source"]["reference"] = write_reference(Path(stage["path"]), {"prepared_at": 1, "hash": declared["sha256"]})
    else:
        link = tmp_path / "alias.safetensors"
        link.symlink_to(declared["path"])
        declared["path"] = str(link)
        item["stat_snapshot"]["path"] = str(link)
    with pytest.raises(ValueError):
        legacy.validate_weight_continuity(item, declared, source, current, started)


def test_raw_unit_execstart_is_pinned_and_unambiguous(tmp_path, evidence):
    evidence()
    path = tmp_path / "old.service"
    path.write_text('[Service]\nExecStart=/usr/bin/python3 /old/runtime/main.py --reserve-vram 3\n')
    proof = {"reference": {"path": str(path), "sha256": legacy.file_sha256(path)}, "format": "systemd_unit_execstart"}
    assert legacy.runtime_arguments(legacy.selected(proof)) == ["--reserve-vram", "3"]
    path.write_text('[Service]\nExecStart=/bin/one\nExecStart=/bin/two\n')
    proof["reference"]["sha256"] = legacy.file_sha256(path)
    with pytest.raises(ValueError, match="ambiguous"):
        legacy.selected(proof)


def test_stage_cli_default_no_writes_or_decodes(stage_fixture, monkeypatch, capsys, tmp_path):
    stage_path, root, _ = stage_fixture
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("check must not decode"))
    output = tmp_path / "untouched"
    assert importer.main(["--runtime-stage", str(stage_path), "--evidence-root", str(root), "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["qualified"] is False and not output.exists()


def test_stage_cli_import_four_videos_without_copy_or_trial(stage_fixture, monkeypatch, capsys, tmp_path):
    stage_path, root, _ = stage_fixture
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        video = Path(argv[-1]) if argv[0] == "ffprobe" else Path(argv[argv.index("-i") + 1])
        stdout = (video.parent / "media-probe.json").read_text() if argv[0] == "ffprobe" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")
    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "imports"
    assert importer.main(["--runtime-stage", str(stage_path), "--evidence-root", str(root),
                          "--execute", "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 8 and len(result["imports"]) == 4
    assert not list(output.rglob("*.mp4"))
    assert not list(output.rglob("qualification-candidate.json"))
    for record in result["imports"]:
        assert record["entry"]["qualification"] == "historical_single_completed"
        assert record["receipt"]["parallel_validated"] is False


def test_actual_staging_schema_recipe_ids_argv_before_workers_start(stage_fixture):
    stage_path, root, catalog = stage_fixture
    stage = json.loads(stage_path.read_text())
    for backend in stage["backends"]:
        backend["recipe_ids"] = list(backend.pop("recipes"))
        backend["source_runtime_root"] = "/old/runtime"
        backend["argv"] = ["/usr/bin/python3", "/new/runtime/main.py", "--port", "18489", *backend.pop("compute_argv")]
        del backend["cmdline_sha256"]
        del backend["pid"]
    write_reference(stage_path, stage)
    plans = importer.build_documents(stage_path, root, catalog)
    assert len(plans) == 4
    assert "pid" not in plans[0]["backend"]
    assert plans[0]["backend"]["cmdline_sha256"]
    assert "cmdline_sha256" not in json.loads(stage_path.read_text())["backends"][0]


def test_separate_original_units_and_weight_receipts(stage_fixture, tmp_path):
    stage_path, root, catalog = stage_fixture
    stage = json.loads(stage_path.read_text())
    units = tmp_path / "old-units.json"
    weights = tmp_path / "old-hashes.json"
    write_reference(units, stage.pop("historical_units"))
    write_reference(weights, stage.pop("historical_weight_files"))
    write_reference(stage_path, stage)
    assert len(importer.build_documents(stage_path, root, catalog, historical_weights=weights, historical_units=units)) == 4
