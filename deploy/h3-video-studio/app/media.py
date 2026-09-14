from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
from pathlib import Path

from fastapi import UploadFile


ALLOWED_EXTENSIONS = {
    "first_frame": {".png", ".jpg", ".jpeg", ".webp"},
    "last_frame": {".png", ".jpg", ".jpeg", ".webp"},
    "reference_image": {".png", ".jpg", ".jpeg", ".webp"},
    "reference_video": {".mp4", ".mov", ".mkv", ".webm"},
    "reference_audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"},
}


def save_asset(
    upload: UploadFile,
    *,
    key: str,
    project_id: str,
    project_dir: Path,
    comfy_input: Path,
) -> dict:
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS[key]:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS[key]))
        raise ValueError(f"{key} 文件格式不支持，可用格式：{allowed}")
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{key}{extension}"
    destination = assets_dir / filename
    with destination.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)
    if destination.stat().st_size == 0:
        raise ValueError(f"{key} 文件为空。")

    metadata = _probe(destination)
    if key == "reference_video":
        duration = float(metadata.get("duration") or 0)
        if duration <= 0 or duration > 15.5:
            raise ValueError("参考视频时长必须在 0 到 15 秒之间。")
    if key == "reference_audio":
        duration = float(metadata.get("duration") or 0)
        if duration <= 0 or duration > 15.5:
            raise ValueError("参考音频时长必须在 0 到 15 秒之间。")

    comfy_input.mkdir(parents=True, exist_ok=True)
    comfy_name = f"h3studio_{project_id}_{filename}"
    shutil.copy2(destination, comfy_input / comfy_name)
    ir_path = _make_ir_proxy(destination, key, assets_dir)
    return {
        "name": upload.filename or filename,
        "path": str(destination),
        "ir_path": str(ir_path),
        "comfy_name": comfy_name,
        "mime": upload.content_type or mimetypes.guess_type(destination.name)[0],
        "size": destination.stat().st_size,
        "metadata": metadata,
    }


def _make_ir_proxy(source: Path, key: str, assets_dir: Path) -> Path:
    if key == "reference_video":
        destination = assets_dir / "reference_video_ir.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            "scale='min(1280,iw)':-2:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        _run(command, "参考视频分析副本生成失败")
        return destination
    if key == "reference_audio":
        destination = assets_dir / "reference_audio_ir.m4a"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(destination),
        ]
        _run(command, "参考音频分析副本生成失败")
        return destination
    return source


def _probe(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,width,height,r_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"size": path.stat().st_size}
    payload = json.loads(completed.stdout or "{}")
    metadata: dict = {
        "size": int(payload.get("format", {}).get("size") or path.stat().st_size),
    }
    duration = payload.get("format", {}).get("duration")
    if duration:
        metadata["duration"] = round(float(duration), 3)
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video":
            metadata["width"] = stream.get("width")
            metadata["height"] = stream.get("height")
            metadata["fps"] = stream.get("r_frame_rate")
            break
    return metadata


def validate_2k_source(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,avg_frame_rate,nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("无法读取 768P 视频规格。") from error
    streams = json.loads(completed.stdout or "{}").get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise ValueError("官方 2K 输入必须同时包含视频和音轨。")
    metadata = {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _parse_rate(str(video.get("avg_frame_rate") or "0/1")),
        "frames": int(video.get("nb_read_frames") or 0),
    }
    validate_2k_spec(metadata)
    return metadata


def validate_2k_spec(metadata: dict) -> None:
    width = int(metadata["width"])
    height = int(metadata["height"])
    frames = int(metadata["frames"])
    fps = float(metadata["fps"])
    if width % 32 or height % 32:
        raise ValueError("官方 2K 输入的宽高必须都是 32 的倍数。")
    if width * height > 768 * 1344:
        raise ValueError("官方 2K 输入超过 H3 768P 面积上限。")
    if abs(fps - 24.0) > 0.01:
        raise ValueError("官方 2K 输入必须是 24fps。")
    if frames < 107 or frames > 362 or (frames - 107) % 17:
        raise ValueError("官方 2K 输入帧数必须是 107–362，并以 17 帧递增。")


def _parse_rate(value: str) -> float:
    if "/" not in value:
        return float(value)
    numerator, denominator = value.split("/", 1)
    return float(numerator) / max(float(denominator), 1)


def _run(command: list[str], label: str) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(f"{label}：{error.stderr[-800:]}") from error
