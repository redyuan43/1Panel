from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


NEW_CASES = {"A4_C0": "A4＋人物触发词", "A4_C05": "A4＋人物 LoRA 0.5", "A4_C1": "A4＋人物 LoRA 1.0"}
CASES = {"A4", "A8", "B8", "C1", "D4"} | NEW_CASES.keys()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_report(report, video):
    if report.get("case") not in CASES or report.get("status") != "generated_pending_quality_review":
        raise ValueError("requires a completed candidate report")
    if not report.get("lease_released") or not report.get("isolated_unload_confirmed"):
        raise ValueError("execution cleanup has not been confirmed")
    media = report.get("media", {})
    if not media.get("ok") or media.get("frame_count") != 362 or media.get("fps") != 24:
        raise ValueError("requires verified full15 media")
    if not report.get("execution", {}).get("execution_seconds", 0) > 0:
        raise ValueError("missing execution timing")
    actual = sha256(video)
    if actual != report.get("artifact_sha256"):
        raise ValueError("video SHA256 does not match execution report")


def publish(report_path, video, destination):
    report = json.loads(report_path.read_text())
    validate_report(report, video)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(video)
    ], capture_output=True, text=True, check=True, timeout=120)
    streams = json.loads(probe.stdout)["streams"]
    visuals = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    media = report["media"]
    if len(visuals) != 1 or not audio:
        raise ValueError("requires one video stream and audio")
    visual = visuals[0]
    if (int(visual.get("nb_read_frames", 0)) != 362
            or visual.get("width") != media["width"] or visual.get("height") != media["height"]
            or visual.get("avg_frame_rate") != "24/1"):
        raise ValueError("actual video does not match full15 report")
    subprocess.run([
        "ffmpeg", "-v", "error", "-xerror", "-i", str(video),
        "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"
    ], capture_output=True, check=True, timeout=180)
    with (destination / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = destination / "index.json"
        manifest = json.loads(index.read_text())
        records = [record for record in manifest["cases"] if record["id"] == report["case"]]
        if not records and report["case"] in NEW_CASES:
            record = {"id": report["case"], "label": NEW_CASES[report["case"]]}
            manifest["cases"].append(record)
            records = [record]
        if len(records) != 1:
            raise ValueError("expected exactly one matching gallery card")
        record = records[0]
        name = report["case"] + ".mp4"
        target = destination / name
        if target.exists():
            if sha256(target) != report["artifact_sha256"]:
                raise ValueError("refusing to replace an already published different video")
        else:
            temporary = destination / (name + ".next")
            shutil.copyfile(video, temporary)
            temporary.replace(target)
        record.update(
            video="comparison-results/" + name,
            state="完整音视频解码通过；效果待你人工确认",
            review_status="pending_human_review",
            width=media["width"], height=media["height"],
            duration_seconds=media["duration_seconds"],
            worker_seconds=report["execution"]["execution_seconds"],
            prompt_id=report["prompt_id"], artifact_sha256=report["artifact_sha256"],
            gpu_uuid=report["isolated_gpu_uuid"],
        )
        manifest["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        temporary = destination / "index.json.next"
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        temporary.replace(index)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.report, args.video, args.destination), ensure_ascii=False))


if __name__ == "__main__":
    main()
