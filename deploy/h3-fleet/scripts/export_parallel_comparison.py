"""Export completed A4_C1/A4_C0 batch evidence; default/dry-run never writes.

This is a format adapter, not a generator or quality reviewer. The unchanged
publication watcher must still run the publisher's full audiovisual decode.
An optional third A4_C05 task is validated and retained in the source batch;
its existing gallery output is never replaced or exported by this adapter.
Unknown progressive terminal statuses fail closed until explicitly agreed.
Failed batches require --allow-completed-from-failed-batch and verified cleanup,
owned noncached history, matching embedded workflow and actual audiovisual probe.
Partial exports preserve the failed batch report and never claim batch success.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

import publish_comparison_video as publisher


ALLOWED_CASES = ("A4_C1", "A4_C0")
BATCH_CASES = (*ALLOWED_CASES, "A4_C05")
SUCCESS_STATUSES = {"generated_pending_quality_review_drained"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def regular_file(path):
    require(not path.is_symlink() and path.resolve() == path and path.is_file(), f"not a regular in-root file: {path}")


def completed_task_evidence(task, video):
    require(task.get("submit_attempted") is True, "collected task was not submitted")
    require(task.get("reconciliation") == "owned terminal history; empty queue", "task cleanup history is unconfirmed")
    evidence = {}
    for name in ("history.json", "media-probe.json"):
        path = video.with_name(name)
        regular_file(path)
        evidence[name] = path.read_bytes()
    history = read_json(evidence["history.json"])
    identifier = task["prompt_id"]
    require(isinstance(history, dict) and set(history) == {identifier}, "history prompt identity mismatch")
    record = history[identifier]
    prompt = record.get("prompt", [])
    require(isinstance(prompt, list) and len(prompt) >= 4 and prompt[1] == identifier
            and isinstance(prompt[2], dict) and isinstance(prompt[3], dict)
            and bool(task.get("client_id")) and prompt[3].get("client_id") == task["client_id"],
            "history workflow/client identity mismatch")
    graph = prompt[2]
    require(digest(json.dumps(graph, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
            == task.get("submitted_workflow_sha256"), "history submitted workflow hash mismatch")
    samplers = task.get("shape", {}).get("sampler_nodes", [])
    require(isinstance(samplers, list) and bool(samplers)
            and samplers == task["execution"].get("sampler_nodes")
            and all(graph.get(node, {}).get("class_type") == "SamplerCustomAdvanced" for node in samplers),
            "history sampler nodes mismatch")
    status = record.get("status", {})
    require(status.get("completed") is True and status.get("status_str") == "success", "history is not successful")
    messages = status.get("messages", [])
    require(isinstance(messages, list) and all(isinstance(message, list) and len(message) == 2
            and isinstance(message[1], dict) and message[1].get("prompt_id") == identifier for message in messages),
            "invalid history messages")
    require(not any(event in {"execution_error", "execution_interrupted"} for event, _ in messages),
            "history contains failed execution")
    starts = [body.get("timestamp") for event, body in messages if event == "execution_start"]
    ends = [body.get("timestamp") for event, body in messages if event == "execution_success"]
    cache_events = [body.get("nodes") for event, body in messages if event == "execution_cached"]
    require(len(starts) == len(ends) == 1 and positive(starts[0]) and positive(ends[0])
            and ends[0] > starts[0] and bool(cache_events)
            and all(isinstance(nodes, list) for nodes in cache_events), "history lacks noncached execution evidence")
    cached = {str(node) for nodes in cache_events for node in nodes}
    require(not cached.intersection(samplers) and sorted(cached) == task["execution"].get("cached_nodes"),
            "cached sampler cannot be exported")
    require(abs(starts[0] / 1000 - task["execution"]["started_at"]) < 0.001
            and abs(ends[0] / 1000 - task["execution"]["finished_at"]) < 0.001,
            "history execution timestamps mismatch")
    save_node = task.get("shape", {}).get("save_node")
    require(graph.get(save_node, {}).get("class_type") == "SaveVideo"
            and graph[save_node].get("inputs", {}).get("filename_prefix") == task.get("prefix"),
            "history output prefix mismatch")
    artifacts = record.get("outputs", {}).get(save_node, {}).get("images", [])
    require(isinstance(task.get("prefix"), str) and "/" in task["prefix"], "missing frozen output prefix")
    parent, prefix = task["prefix"].rsplit("/", 1)
    require(isinstance(artifacts, list) and len(artifacts) == 1 and isinstance(artifacts[0], dict),
            "history output missing")
    artifact = artifacts[0]
    filename = artifact.get("filename", "")
    require(artifact.get("type") == "output" and artifact.get("subfolder") == parent
            and filename.startswith(prefix + "_") and filename.endswith(".mp4")
            and not any(character in filename for character in ("/", "\\", ":")), "history output identity mismatch")
    actual = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
                             "-of", "json", str(video)], capture_output=True, text=True, check=True, timeout=120)
    for probe in (read_json(evidence["media-probe.json"]), read_json(actual.stdout)):
        streams = probe.get("streams", [])
        visuals = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        media = task["media"]
        require(len(visuals) == 1 and len(audio) == 1, "requires original and actual video plus audio")
        require(visuals[0].get("width") == media["width"] and visuals[0].get("height") == media["height"]
                and int(visuals[0].get("nb_read_frames", 0)) == 362
                and visuals[0].get("avg_frame_rate") == "24/1", "probe video mismatch")
        require(bool(media.get("audio_codec")) and audio[0].get("codec_name") == media["audio_codec"]
                and positive(media.get("audio_channels")) and audio[0].get("channels") == media["audio_channels"]
                and int(audio[0].get("sample_rate", 0)) == int(media.get("audio_sample_rate", 0)) > 0,
                "probe audio mismatch")
        require(abs(float(probe.get("format", {}).get("duration", 0)) - media["duration_seconds"]) < 0.1
                and read_json(probe.get("format", {}).get("tags", {}).get("prompt", "{}")) == graph,
                "media duration or embedded workflow does not match history")
    require(publisher.sha256(video) == task["artifact_sha256"], "video SHA256 changed during evidence verification")
    return evidence


def validate_existing(target, normalized, raw):
    if not target.exists() and not target.is_symlink():
        return False
    require(not target.is_symlink() and target.is_dir(), f"unsafe existing case directory: {target}")
    report_path = target / "report.json"
    provenance = target / "batch-report.json"
    video = target / "video.mp4"
    for path in (report_path, provenance, video):
        regular_file(path)
    existing = read_json(report_path.read_bytes())
    require(existing.get("prompt_id") == normalized["prompt_id"], "refusing different prompt in existing case")
    require(existing == normalized and provenance.read_bytes() == raw, "refusing different existing report/provenance")
    publisher.validate_report(existing, video)
    for name, expected in normalized.get("partial_export_evidence", {}).get("files_sha256", {}).items():
        path = target / name
        regular_file(path)
        require(digest(path.read_bytes()) == expected, "existing partial evidence changed")
    return True


def plan(batch_root, output_root, allow_completed_from_failed_batch=False):
    batch_root, output_root = Path(batch_root).resolve(), Path(output_root).resolve()
    require(batch_root != output_root and batch_root not in output_root.parents,
            "batch root must remain read-only; output cannot be inside it")
    source = batch_root / "report.json"
    regular_file(source)
    raw = source.read_bytes()
    batch = read_json(raw)
    require(isinstance(batch, dict), "batch report must be an object")
    partial = allow_completed_from_failed_batch and batch.get("status") == "failed"
    require(batch.get("status") in SUCCESS_STATUSES or partial, "unsupported/non-success batch status")
    require(batch.get("lease_released") is True, "batch lease release is not confirmed")
    require(batch.get("both_unloaded") is True or batch.get("all_unloaded") is True,
            "batch unload is not confirmed")
    require(not batch.get("reconciliation_error") and not batch.get("cleanup_error")
            and not batch.get("cleanup_errors") and not batch.get("retained_lease")
            and not batch.get("lease_hold_error"), "batch cleanup contains failure evidence")
    require(not batch.get("error") or partial, "batch contains failure evidence")
    if partial:
        require(isinstance(batch.get("error"), str) and bool(batch["error"]), "failed batch must preserve its original error")
        require(all(batch[key] is True for key in ("both_unloaded", "all_unloaded") if key in batch),
                "contradictory batch cleanup confirmation")
    require(positive(batch.get("finished_at")), "batch has no finished timestamp")
    require(isinstance(batch.get("run_id"), str) and bool(batch["run_id"]), "batch run_id missing")
    tasks = batch.get("tasks")
    require(isinstance(tasks, list) and 1 <= len(tasks) <= 3, "requires one to three A4 realism tasks")
    seen, prompts, items = set(), set(), []
    for index, task in enumerate(tasks):
        require(isinstance(task, dict), "task must be an object")
        case = task.get("case")
        require(case in BATCH_CASES and case not in seen, "requires unique A4_C1/A4_C0/A4_C05 tasks")
        seen.add(case)
        require(not task.get("reconciliation_error") and not task.get("cleanup_error")
                and not task.get("cleanup_errors") and not task.get("lease_hold_error"), "task cleanup contains failure evidence")
        if partial and task.get("status") != "collected":
            continue
        require(task.get("status") == "collected" and not task.get("error"), f"{case}: task is not successfully collected")
        prompt = task.get("prompt_id")
        require(isinstance(prompt, str) and str(uuid.UUID(prompt)) == prompt and prompt not in prompts,
                "task requires a unique canonical prompt UUID")
        prompts.add(prompt)
        require(re.fullmatch(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", task.get("gpu_uuid", "")) is not None,
                "task requires a full GPU UUID")
        media, execution = task.get("media", {}), task.get("execution", {})
        require(media.get("ok") is True and media.get("frame_count") == 362 and media.get("fps") == 24,
                "requires validated full15 media")
        require(all(type(media.get(key)) is int and media[key] > 0 for key in ("width", "height")), "invalid media dimensions")
        require(positive(media.get("duration_seconds")) and abs(media["duration_seconds"] - 362 / 24) <= 0.1,
                "invalid full15 duration")
        require(all(positive(execution.get(key)) for key in ("started_at", "finished_at", "execution_seconds")),
                "task execution evidence missing")
        require(execution["finished_at"] <= batch["finished_at"]
                and abs(execution["finished_at"] - execution["started_at"] - execution["execution_seconds"]) < 0.01,
                "execution timestamps are inconsistent")
        video = batch_root / case / "video.mp4"
        regular_file(video)
        if "artifact" in task:
            require(task["artifact"] == str(video), "task artifact does not match fixed batch path")
        exportable = case in ALLOWED_CASES
        target = output_root / case if exportable else batch_root / case
        require(target != batch_root and target not in batch_root.parents, "output would modify the input batch")
        normalized = copy.deepcopy(task)
        normalized.update(status="generated_pending_quality_review", run_id=batch["run_id"],
                          artifact=str(target / "video.mp4"), isolated_gpu_uuid=task["gpu_uuid"],
                          isolated_url=task.get("endpoint"), lease_released=batch["lease_released"],
                          isolated_unload_confirmed=batch.get("both_unloaded") is True or batch.get("all_unloaded") is True,
                          finished_at=batch["finished_at"], started_at=execution["started_at"],
                          submitted_at=task.get("submitted_at", execution["started_at"]))
        normalized["batch_provenance"] = {"source_report": str(source), "sha256": digest(raw),
                                          "preserved_report": str(target / "batch-report.json"),
                                          "batch_status": batch["status"], "task_status": task["status"], "task_index": index}
        publisher.validate_report(normalized, video)
        evidence = completed_task_evidence(task, video) if partial else {}
        if partial:
            normalized.update(batch_failed=True, partial_export=True, batch_error=batch["error"],
                              partial_export_evidence={"noncached_history_verified": True,
                                  "actual_audio_video_probe_verified": True,
                                  "files_sha256": {name: digest(data) for name, data in evidence.items()}})
        exists = validate_existing(target, normalized, raw) if exportable else False
        items.append({"case": case, "source": video, "target": target, "report": normalized,
                      "exists": exists, "exportable": exportable, "evidence": evidence})
    require(not partial or any(item["exportable"] for item in items), "failed batch has no verified collected exportable task")
    items.sort(key=lambda item: BATCH_CASES.index(item["case"]))
    return source, raw, items


def export(batch_root, output_root, execute=False, allow_completed_from_failed_batch=False):
    output_root = Path(output_root).resolve()
    source, raw, items = plan(batch_root, output_root, allow_completed_from_failed_batch)
    if execute:
        output_root.mkdir(parents=True, exist_ok=True)
        with (output_root / ".export-parallel.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            source, raw, items = plan(batch_root, output_root, allow_completed_from_failed_batch)
            for item in items:
                if item["exists"] or not item["exportable"]:
                    continue
                with tempfile.TemporaryDirectory(prefix=".export-parallel-", dir=output_root) as temporary:
                    staging = Path(temporary) / item["case"]
                    staging.mkdir()
                    shutil.copyfile(item["source"], staging / "video.mp4")
                    publisher.validate_report(item["report"], staging / "video.mp4")
                    require(source.read_bytes() == raw, "batch report changed during export")
                    (staging / "batch-report.json").write_bytes(raw)
                    for name, data in item["evidence"].items():
                        require(item["source"].with_name(name).read_bytes() == data, "partial source evidence changed during export")
                        (staging / name).write_bytes(data)
                    (staging / "report.json").write_text(json.dumps(item["report"], ensure_ascii=False, indent=2), encoding="utf-8")
                    require(not item["target"].exists() and not item["target"].is_symlink(), "target appeared during export; refusing overwrite")
                    staging.rename(item["target"])
    batch = read_json(raw)
    return {"mode": "executed" if execute else "check_only", "batch_report_sha256": digest(raw),
            "batch_status": batch["status"], "partial_export": batch["status"] == "failed",
            "skipped_tasks": [{"case": task["case"], "status": task.get("status"), "prompt_id": task.get("prompt_id")}
                              for task in batch["tasks"] if task["case"] not in {item["case"] for item in items}],
            "cases": [{"case": item["case"], "report": str(item["target"] / "report.json"),
                       "prompt_id": item["report"]["prompt_id"], "artifact_sha256": item["report"]["artifact_sha256"],
                       "already_present": item["exists"]} for item in items if item["exportable"]],
            "retained_batch_cases": [{"case": item["case"], "artifact": str(item["source"]),
                                      "prompt_id": item["report"]["prompt_id"],
                                      "artifact_sha256": item["report"]["artifact_sha256"],
                                      "source_report": str(source), "reason": "replica_evidence_only_original_gallery_unchanged"}
                                     for item in items if not item["exportable"]],
            "published": False, "quality_review": "not_performed"}


def persistent_path(value):
    path = Path(value).expanduser()
    require(path.is_absolute(), "roots must be explicit absolute persistent paths")
    resolved = path.resolve()
    require(not any(resolved == temporary or temporary in resolved.parents
                    for temporary in map(Path, ("/tmp", "/var/tmp", "/dev/shm"))), "temporary roots are not allowed")
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-completed-from-failed-batch", action="store_true",
                        help="explicitly salvage verified collected tasks after failed-batch cleanup; never declares batch success")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = export(persistent_path(args.batch_root), persistent_path(args.output_root), args.execute,
                        args.allow_completed_from_failed_batch)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"export failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
