"""Pinned historical per-task completion, never WS or parallel qualification.

validate_legacy_single(entry, backend, catalog) returns a receipt or raises
ValueError. Entry is either the v1 document or a scheduling entry containing
qualification, recipe_version and evidence={path, sha256}. All file references
are absolute, persistent, SHA256-pinned local files. This module performs no
network, subprocess, model execution, configuration writes or qualification
promotion. Backend process/source/weight verification remains the caller's job.

Document fields: schema_version=1, qualification=historical_single_completed,
recipe_id, recipe_version, recipe_digest, expected_runtime_version, gpu_uuid,
upstream_prompt_id, parallel_validated=false, ws_validated=false,
single_task_only=true; references source_report, workflow, history, media_probe,
artifact, artifact_validation; runtime={manifest, argv, gpu_uuid, unit, pid}.
Runtime manifest is the original preparer's {target, files:[{path,sha256}]};
other runtime fields are selectors {reference:{path,sha256}, pointer:[keys]}.
weights is a list of {filename, sha256_source:selector}. Selectors read original
evidence instead of treating manually entered runtime/weight claims as proof.
Backend argv must contain the actual launch argv list and match cmdline_sha256;
alternatively compute_argv is the caller-verified compute-only argument list.
Only launch-location arguments may differ; compute/memory flags must match.
Failed-batch task imports require allow_completed_from_failed_batch=true and
retain the native batch error. 'single' limits qualification to one task per
backend; it does NOT assert that the historical host had no concurrent work.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import stat


QUALIFICATION = "historical_single_completed"
FORMAL = {"A4", "A4_C0", "A4_C1", "B8"}
ALIASES = {"t8_sampling.py": "custom_nodes/h3_t8_baseline/sampling.py",
           "comfy_model_base.py": "comfy/model_base.py",
           "comfy_minimax_model.py": "comfy/ldm/minimax/model.py"}
SHARED_CONTINUITY = {"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": "text_encoders",
                     "minimax_h3_video_vae_fp16.safetensors": "vae",
                     "minimax_h3_audio_vae_fp32.safetensors": "vae"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def persistent_path(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path and path != Path("/"),
            "reference must use a canonical absolute path")
    require(not any(path == root or root in path.parents for root in (Path("/tmp"), Path("/var/tmp"))),
            "reference must be persistent")
    return path


def file_sha256(path):
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def pinned_path(reference):
    path = persistent_path(reference["path"])
    require(path.is_file() and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]), "invalid pinned file")
    require(file_sha256(path) == reference["sha256"], "evidence SHA256 mismatch: " + str(path))
    return path


def pinned_json(reference):
    path = persistent_path(reference["path"])
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "evidence SHA256 mismatch: " + str(path))
    return read_json(raw)


def selected(selector):
    if selector.get("format") == "systemd_unit_execstart":
        raw = pinned_path(selector["reference"]).read_text().replace("\\\n", "")
        commands = [line.split("=", 1)[1].strip() for line in raw.splitlines() if line.strip().startswith("ExecStart=")]
        require(len(commands) == 1 and commands[0] and "%" not in commands[0] and "$" not in commands[0],
                "unsupported or ambiguous systemd ExecStart; provide captured resolved argv")
        return shlex.split(commands[0])
    value = pinned_json(selector["reference"])
    require(isinstance(selector["pointer"], list), "selector pointer must be a list")
    for key in selector["pointer"]:
        require(isinstance(key, str) or type(key) is int and key >= 0, "invalid selector key")
        value = value[key]
    return value


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def stat_snapshot(path):
    path = persistent_path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "continuity requires an ordinary non-symlink file")
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino, "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns, "mode": info.st_mode}


def validate_weight_continuity(item, declared, source, current_sources, execution_start):
    name = item["filename"]
    require(name in SHARED_CONTINUITY and item.get("continuity") == "unchanged_file_metadata_inference",
            "continuity is restricted to three catalog-null shared files")
    path = Path(declared["path"])
    require(item["stat_snapshot"]["path"] == str(path), "continuity weight path mismatch")
    snapshot = stat_snapshot(path)
    require(snapshot == item["stat_snapshot"], "weight stat changed since continuity snapshot")
    require(snapshot["mtime_ns"] / 1e9 < execution_start and snapshot["ctime_ns"] / 1e9 < execution_start,
            "weight mtime/ctime does not predate historical execution")
    paths_hash = next((entry["sha256"] for entry in source["files"] if entry["path"] == "extra_model_paths.yaml"), None)
    require(paths_hash is not None and current_sources.get("extra_model_paths.yaml") == paths_hash,
            "historical/current extra_model_paths hash differs")
    config = pinned_json(item["model_paths"])
    require(item["model_paths"]["sha256"] == paths_hash and config == source["model_search_paths"],
            "model search path content differs from original manifest")
    candidates = []
    for mapping in config.values():
        base = Path(mapping["base_path"])
        category = mapping.get(SHARED_CONTINUITY[name], "")
        require(isinstance(category, str), "unsupported historical model path mapping")
        for relative in category.splitlines():
            candidate = base / relative / name
            if candidate.is_file():
                candidates.append(candidate)
    require(len(candidates) == 1 and candidates[0] == path and candidates[0].resolve() == path,
            "historical shared weight resolution is missing/ambiguous/different")
    checksum = selected(item["sha256_source"])
    stage = pinned_json(item["sha256_source"]["reference"])
    require(positive(stage.get("prepared_at")) and stage["prepared_at"] * 1e9 >= snapshot["ctime_ns"],
            "current full-hash staging evidence missing or older than file change")
    require(checksum == declared["sha256"], "current weight hash differs from verified backend")
    require(stat_snapshot(path) == snapshot, "weight changed while checking continuity")
    return {"historical_hash_recorded": False, "historical_sha256": None,
            "continuity": "unchanged_file_metadata_inference", "current_hash_verified": True,
            "current_sha256": checksum, "current_hash_source": copy.deepcopy(item["sha256_source"]),
            "stat_snapshot": snapshot, "model_paths": copy.deepcopy(item["model_paths"])}


def runtime_arguments(value):
    argv = shlex.split(value) if isinstance(value, str) else value
    require(isinstance(argv, list) and len(argv) >= 2 and all(isinstance(item, str) for item in argv),
            "runtime argv evidence missing")
    require(re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(argv[0]).name)
            and Path(argv[1]).name == "main.py", "unsupported runtime launcher")
    ignored = {"--listen", "--port", "--input-directory", "--output-directory", "--temp-directory",
               "--user-directory", "--base-directory", "--extra-model-paths-config"}
    result = []
    index = 2
    while index < len(argv):
        argument = argv[index]
        if argument.split("=", 1)[0] in ignored:
            if "=" not in argument:
                require(index + 1 < len(argv) and not argv[index + 1].startswith("--"), "invalid launch argument")
                index += 1
        else:
            result.append(argument)
        index += 1
    return result


def source_task(report, recipe_id, allow_failed):
    require(report.get("lease_released") is True, "historical lease was not released")
    require(positive(report.get("finished_at")), "historical report is not terminal")
    require(not any(report.get(key) for key in ("cleanup_error", "cleanup_errors", "reconciliation_error",
                                               "retained_lease", "lease_hold_error")), "uncertain historical cleanup")
    if "tasks" not in report:
        require(report.get("status") == "generated_pending_quality_review" and not report.get("error"),
                "single report was not successful")
        require(report.get("isolated_unload_confirmed") is True, "historical unload not confirmed")
        require(report.get("case") == recipe_id, "source recipe mismatch")
        task = report
        identity = report["baseline"]["isolated"]
    else:
        require(report.get("status") == "generated_pending_quality_review_drained"
                or allow_failed is True and report.get("status") == "failed" and bool(report.get("error")),
                "batch is not successful; failed-batch extraction needs explicit opt-in")
        require(report.get("both_unloaded") is True or report.get("all_unloaded") is True, "batch unload not confirmed")
        require(all(report[key] is True for key in ("both_unloaded", "all_unloaded") if key in report),
                "contradictory unload confirmation")
        matches = [task for task in report["tasks"] if task.get("case") == recipe_id]
        require(len(matches) == 1, "source must contain exactly one matching task")
        task = matches[0]
        require(task.get("status") == "collected" and not task.get("error"), "task was not collected successfully")
        require(task.get("reconciliation") == "owned terminal history; empty queue", "task cleanup not confirmed")
        identity = report["identities"]["workers"][recipe_id]
    require(task.get("submit_attempted") is True, "task was not submitted")
    require(not any(task.get(key) for key in ("cleanup_error", "cleanup_errors", "reconciliation_error",
                                             "lease_hold_error")), "uncertain historical task cleanup")
    return task, identity


def validate_history(history, graph, task, sampler):
    identifier = task["prompt_id"]
    require(set(history) == {identifier}, "history prompt identity mismatch")
    record = history[identifier]
    prompt = record["prompt"]
    require(prompt[1] == identifier and prompt[2] == graph and bool(task.get("client_id"))
            and prompt[3].get("client_id") == task["client_id"], "history graph/client mismatch")
    status = record["status"]
    require(status.get("completed") is True and status.get("status_str") == "success", "history not successful")
    starts, finishes, caches = [], [], []
    for kind, detail in status["messages"]:
        require(detail.get("prompt_id") == identifier, "foreign or unowned history event")
        require(kind not in {"execution_error", "execution_interrupted"}, "history execution failed")
        if kind == "execution_start":
            starts.append(detail["timestamp"])
        elif kind == "execution_success":
            finishes.append(detail["timestamp"])
        elif kind == "execution_cached":
            require(isinstance(detail.get("nodes"), list), "cache evidence missing")
            caches.append(detail["nodes"])
    require(len(starts) == len(finishes) == 1 and positive(starts[0]) and positive(finishes[0])
            and finishes[0] > starts[0] and bool(caches), "incomplete execution history")
    cached = {str(node) for nodes in caches for node in nodes}
    require(sampler not in cached, "cached sampler cannot qualify")
    execution = task["execution"]
    require(execution["sampler_nodes"] == [sampler] and execution["cached_nodes"] == sorted(cached),
            "reported sampler/cache mismatch")
    require(abs(execution["started_at"] - starts[0] / 1000) < 0.001
            and abs(execution["finished_at"] - finishes[0] / 1000) < 0.001
            and abs(execution["execution_seconds"] - (finishes[0] - starts[0]) / 1000) < 0.001,
            "reported execution time mismatch")
    prefix = graph["13"]["inputs"]["filename_prefix"]
    require(task.get("prefix") == prefix, "output prefix mismatch")
    parent, _, name = prefix.rpartition("/")
    outputs = record["outputs"]["13"]["images"]
    require(len(outputs) == 1 and outputs[0].get("type") == "output"
            and outputs[0].get("subfolder") == parent
            and re.fullmatch(re.escape(name) + r"_\d+_\.mp4", outputs[0].get("filename", "")),
            "history output identity mismatch")


def validate_probe(probe, graph, size):
    videos = [stream for stream in probe["streams"] if stream.get("codec_type") == "video"]
    audios = [stream for stream in probe["streams"] if stream.get("codec_type") == "audio"]
    require(len(videos) == len(audios) == 1, "requires one video and one native audio stream")
    video, audio = videos[0], audios[0]
    require([video.get("width"), video.get("height"), str(video.get("nb_read_frames")), video.get("avg_frame_rate")]
            == [480, 864, "362", "24/1"], "requires full 480x864 362-frame 24fps video")
    require(audio.get("codec_name") == "aac" and audio.get("channels") == 2
            and str(audio.get("sample_rate")) == "32000" and float(audio.get("duration", 0)) >= 15,
            "native full-length audio proof missing")
    container = probe["format"]
    require(int(container["size"]) == size and abs(float(container["duration"]) - 362 / 24) < 0.01,
            "media size/duration mismatch")
    require(read_json(container["tags"]["prompt"]) == graph, "embedded video graph differs from history")


def probe_command(artifact):
    return ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(artifact)]


def decode_command(artifact):
    return ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-hwaccel", "none", "-i", str(artifact),
            "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"]


def validate_runtime(document, backend, recipe, task, identity):
    runtime = document["runtime"]
    source = pinned_json(runtime["manifest"])
    require(source.get("status") == "prepared_not_imported_not_started", "unsupported legacy source manifest")
    historical = {item["path"]: item["sha256"] for item in source["files"]}
    require(len(historical) == len(source["files"]) and bool(historical), "duplicate/empty source manifest")
    root = Path(backend["runtime_root"])
    current = {}
    for item in backend["runtime_files"]:
        relative = str(Path(item["path"]).relative_to(root))
        require(relative not in current, "duplicate current runtime source")
        current[relative] = item["sha256"]
    require(any(name.endswith(".py") for name in current), "current runtime sources missing")
    expected = {ALIASES[name]: checksum for name, checksum in recipe["runtime_source"].get("source_sha256", {}).items()}
    expected.update({recipe["runtime_source"]["plugin_directory"] + "/" + name: checksum
                     for name, checksum in recipe["runtime_source"].get("plugin_python_files", {}).items()})
    require(all(current.get(name) == historical.get(name) == checksum for name, checksum in expected.items()),
            "required recipe node source differs")
    gpu = task.get("gpu_uuid", task.get("isolated_gpu_uuid"))
    unit = task.get("unit", task.get("isolated_unit"))
    require(gpu == document["gpu_uuid"] == selected(runtime["gpu_uuid"]), "historical GPU mismatch")
    require(unit == identity["Id"] == selected(runtime["unit"]), "historical unit mismatch")
    require(str(identity["MainPID"]) == str(selected(runtime["pid"])) and int(identity["MainPID"]) > 0,
            "historical process mismatch")
    argv = selected(runtime["argv"])
    parsed = shlex.split(argv) if isinstance(argv, str) else argv
    require(isinstance(parsed, list) and len(parsed) >= 2 and Path(parsed[1]).is_absolute(),
            "historical absolute runtime entrypoint missing")
    if "argv" in backend:
        current_argv = backend["argv"]
        require(isinstance(current_argv, list) and all(isinstance(item, str) and "\0" not in item for item in current_argv),
                "current argv must be the actual process argv list")
        require(hashlib.sha256(("\0".join(current_argv) + "\0").encode()).hexdigest() == backend["cmdline_sha256"],
                "current argv does not match pinned process command")
        compute_argv = runtime_arguments(current_argv)
        if "compute_argv" in backend:
            require(backend["compute_argv"] == compute_argv, "compute argv contradicts pinned process command")
    else:
        compute_argv = backend["compute_argv"]
        require(isinstance(compute_argv, list) and all(isinstance(item, str) and "\0" not in item for item in compute_argv),
                "verified compute argv missing")
    require(runtime_arguments(argv) == compute_argv, "runtime compute/memory arguments differ")
    declared = {item["filename"]: item for item in backend["weight_files"]}
    weights = {item["filename"]: item for item in document["weights"]}
    required = {item["filename"]: item.get("sha256") for item in [*recipe["weights"], *recipe.get("metadata_files", [])]}
    require(len(weights) == len(document["weights"]) and weights.keys() == required.keys(), "incomplete/duplicate historical weights")
    classifications = {}
    for name, item in weights.items():
        checksum = selected(item["sha256_source"])
        require(isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum)
                and declared.get(name, {}).get("sha256") == checksum and (required[name] is None or required[name] == checksum),
                "historical/runtime/catalog weight mismatch: " + name)
        if item.get("continuity"):
            require(required[name] is None, "known historical SHA cannot be replaced by continuity inference")
            classifications[name] = validate_weight_continuity(item, declared[name], source, current,
                                                               task["execution"]["started_at"])
        else:
            classifications[name] = {"historical_hash_recorded": True, "historical_sha256": checksum,
                                     "current_hash_verified": True, "current_sha256": checksum,
                                     "sha256_source": copy.deepcopy(item["sha256_source"])}
    return {"unit": unit, "pid": str(identity["MainPID"]), "argv": argv,
            "runtime_root": str(Path(parsed[1]).parent), "source_manifest_root": source["target"], "weights": classifications}


def _validate(document, backend, catalog):
    require(document.get("schema_version") == 1 and document.get("qualification") == QUALIFICATION,
            "unsupported legacy evidence schema")
    require(document.get("single_task_only") is True and document.get("parallel_validated") is False
            and document.get("ws_validated") is False and not document.get("sampler_progress"),
            "historical evidence cannot assert WS/parallel qualification")
    recipe_id = document["recipe_id"]
    require(recipe_id in FORMAL, "unsupported legacy recipe")
    recipe = catalog.get(recipe_id)
    require(document["recipe_version"] == recipe["version"] and document["recipe_digest"] == recipe["recipe_digest"],
            "recipe version/digest mismatch")
    require(document["expected_runtime_version"] == backend["runtime_version"]
            and document["gpu_uuid"] == backend["gpu_uuid"]
            and re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", document["gpu_uuid"]),
            "target runtime/GPU mismatch")
    report = pinned_json(document["source_report"])
    task, identity = source_task(report, recipe_id, document.get("allow_completed_from_failed_batch"))
    require(task["prompt_id"] == document["upstream_prompt_id"], "task prompt mismatch")
    graph = pinned_json(document["workflow"])
    binding = catalog.validate(recipe_id, graph, document["recipe_version"])
    legacy_hash = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    require(task.get("submitted_workflow_sha256", task.get("workflow_sha256")) == legacy_hash,
            "native submitted workflow hash mismatch")
    validate_history(pinned_json(document["history"]), graph, task, recipe["sampling"]["sampler_node"])
    require(task["execution"]["finished_at"] <= report["finished_at"], "execution ends after report")
    media = task["media"]
    require(media.get("ok") is True and [media.get(key) for key in ("width", "height", "frame_count", "fps")]
            == [480, 864, 362, 24], "native media validation missing")
    artifact = pinned_path(document["artifact"])
    require(document["artifact"]["sha256"] == task["artifact_sha256"], "native artifact SHA mismatch")
    validate_probe(pinned_json(document["media_probe"]), graph, artifact.stat().st_size)
    proof = pinned_json(document["artifact_validation"])
    require(proof.get("artifact") == document["artifact"], "decode proof artifact mismatch")
    require(positive(proof.get("started_at")) and positive(proof.get("finished_at"))
            and proof["finished_at"] >= proof["started_at"], "decode proof timestamps missing")
    for name, command in (("ffprobe", probe_command(artifact)), ("full_decode", decode_command(artifact))):
        result = proof[name]
        require(result.get("argv") == command and type(result.get("returncode")) is int
                and result["returncode"] == 0 and result.get("stderr") == "", "complete CPU media validation missing")
    validate_probe(proof["ffprobe"]["probe"], graph, artifact.stat().st_size)
    runtime = validate_runtime(document, backend, recipe, task, identity)
    require(file_sha256(artifact) == document["artifact"]["sha256"], "artifact changed during validation")
    return {**binding, "qualification": QUALIFICATION, "status": QUALIFICATION,
            "expected_runtime_version": backend["runtime_version"], "gpu_uuid": backend["gpu_uuid"],
            "upstream_prompt_id": task["prompt_id"], "full_video_validated": True,
            "single_task_only": True, "parallel_validated": False, "ws_validated": False,
            "historical_host_exclusive": "not_established", "all_stage_no_oom": "not_established",
            "source_status": report["status"], "source_error": report.get("error"),
            "source_task_status": task.get("status"), "historical_runtime": runtime,
            "provenance": copy.deepcopy(document)}


def reuse_historical_report(document, backend, catalog):
    recipe_id = document["recipe_id"]
    require(recipe_id in FORMAL, "unsupported historical recipe")
    recipe = catalog.get(recipe_id)
    require(document.get("qualification") == QUALIFICATION and document.get("single_task_only") is True
            and document.get("parallel_validated") is False and document.get("ws_validated") is False,
            "historical report cannot qualify concurrency")
    require(document["recipe_version"] == recipe["version"] and document["recipe_digest"] == recipe["recipe_digest"]
            and document["expected_runtime_version"] == backend["runtime_version"], "historical recipe/runtime binding mismatch")
    require(document["gpu_uuid"] == backend["gpu_uuid"] == "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536",
            "historical report only qualifies the original 4060 Ti")
    reserve = {"A4": "1", "A4_C0": "3", "A4_C1": "3", "B8": "6"}[recipe_id]
    require(runtime_arguments(backend["argv"]) == ["--reserve-vram", reserve, "--disable-pinned-memory",
            "--disable-auto-launch", "--disable-api-nodes"], "historical compute parameters changed")
    report = pinned_json(document["source_report"])
    task, identity = source_task(report, recipe_id, allow_failed=recipe_id == "A4_C1")
    require(task.get("gpu_uuid", task.get("isolated_gpu_uuid")) == backend["gpu_uuid"], "historical GPU mismatch")
    require(task["prompt_id"] == document["upstream_prompt_id"], "historical task identity mismatch")
    graph = pinned_json(document["workflow"])
    binding = catalog.validate(recipe_id, graph, document["recipe_version"])
    validate_history(pinned_json(document["history"]), graph, task, recipe["sampling"]["sampler_node"])
    artifact = pinned_path(document["artifact"])
    require(document["artifact"]["sha256"] == task["artifact_sha256"], "historical artifact mismatch")
    validate_probe(pinned_json(document["media_probe"]), graph, artifact.stat().st_size)
    media = task["media"]
    require(media.get("ok") is True and [media.get(key) for key in ("width", "height", "frame_count", "fps")]
            == [480, 864, 362, 24], "historical full video evidence missing")
    return {**binding, "qualification": QUALIFICATION, "single_task_only": True,
            "parallel_validated": False, "ws_validated": False, "verification_scope": "historical_report_reused",
            "new_inference_run": False, "new_full_decode_run": False, "historical_full_video": True,
            "historical_hashes_complete": False, "current_runtime_and_weights": "verified_by_dispatcher",
            "source_error": report.get("error"), "source_task_status": task.get("status"),
            "historical_unit": identity["Id"], "upstream_prompt_id": task["prompt_id"]}


def validate_legacy_single(entry, backend, catalog):
    try:
        document = entry
        if "evidence" in entry:
            require(entry.get("qualification") == QUALIFICATION, "wrong qualification entry")
            document = pinned_json(entry["evidence"])
            require(entry.get("recipe_version") == document["recipe_version"], "entry recipe version mismatch")
        if document.get("verification_scope") == "historical_report_reused":
            return reuse_historical_report(document, backend, catalog)
        return _validate(document, backend, catalog)
    except (OSError, KeyError, TypeError, IndexError, AttributeError, OverflowError) as error:
        raise ValueError("incomplete historical evidence: " + str(error)) from error
