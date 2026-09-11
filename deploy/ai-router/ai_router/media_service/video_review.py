from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageOps

from .contracts import MediaError


VERDICTS = {"PASS", "CONDITIONAL_PASS", "FAIL"}
SCORE_FIELDS = (
    "identity",
    "prompt_adherence",
    "temporal_coherence",
    "anatomy_geometry",
    "camera",
    "text_brand",
    "audio",
    "segment_boundary",
)


def _assistant_text(value: dict) -> str:
    choices = value.get("choices", [])
    content = (
        choices[0].get("message", {}).get("content", "")
        if choices
        else ""
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _json_objects(text: str) -> list[dict]:
    candidates = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    )
    decoder = json.JSONDecoder()
    results = []
    seen = set()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            for index, character in enumerate(candidate):
                if character != "{":
                    continue
                try:
                    value, _ = decoder.raw_decode(candidate[index:])
                except ValueError:
                    continue
                if isinstance(value, dict):
                    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
                    if encoded not in seen:
                        seen.add(encoded)
                        results.append(value)
            continue
        if isinstance(value, dict):
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if encoded not in seen:
                seen.add(encoded)
                results.append(value)
    return results


async def command(*args: str, seconds: int = 120, stderr: bool = False) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if stderr else asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, error = await asyncio.wait_for(process.communicate(), seconds)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise MediaError("invalid_video", "Video quality analysis failed.", 502)
    return error if stderr else stdout


def _fraction(value: str | None) -> float:
    if not value:
        return 0.0
    numerator, _, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


async def _boundary_differences(
    path: Path,
    *,
    frame_rate: float,
    frame_count: int,
    boundaries: list[float],
) -> list[dict]:
    results = []
    frame_bytes = 64 * 64
    for seconds in boundaries:
        after = int(round(seconds * frame_rate))
        before = after - 1
        if before < 0 or after >= frame_count:
            continue
        frames = await command(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
            "-vf", f"select=eq(n\\,{before})+eq(n\\,{after}),scale=64:64,format=gray",
            "-frames:v", "2", "-f", "rawvideo", "-",
        )
        if len(frames) < frame_bytes * 2:
            continue
        first = frames[:frame_bytes]
        second = frames[frame_bytes:frame_bytes * 2]
        difference = sum(abs(left - right) for left, right in zip(first, second)) / (
            frame_bytes * 255
        )
        results.append({
            "boundary_seconds": float(seconds),
            "before_frame": before,
            "after_frame": after,
            "normalized_frame_difference": round(difference, 6),
        })
    return results


async def _audio_loudness(path: Path, duration: float) -> dict | None:
    report = (await command(
        "ffmpeg", "-v", "info", "-nostdin", "-i", str(path),
        "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
        seconds=max(120, int(duration * 4) + 30),
        stderr=True,
    )).decode(errors="replace")
    integrated = re.findall(r"\bI:\s*(-?(?:[0-9.]+|inf))\s+LUFS", report)
    peaks = re.findall(r"\bPeak:\s*(-?(?:[0-9.]+|inf))\s+dBFS", report)

    def number(value: str) -> float | None:
        try:
            result = float(value)
        except ValueError:
            return None
        return result if result not in {float("inf"), float("-inf")} else None

    if not integrated and not peaks:
        return None
    return {
        "integrated_lufs": number(integrated[-1]) if integrated else None,
        "true_peak_dbfs": number(peaks[-1]) if peaks else None,
    }


async def technical_review(path: Path, *, expected: dict | None = None) -> dict:
    probe = json.loads(await command(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ))
    streams = probe.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"
                  and not item.get("disposition", {}).get("attached_pic")), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("invalid_video", "Stage output has no playable video stream.", 502)
    duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0)
    frame_rate = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    frame_count = int(video.get("nb_frames") or round(duration * frame_rate))
    issues = []
    expected = expected or {}
    if duration <= 0 or frame_rate <= 0 or frame_count <= 0:
        issues.append({"category": "decode", "severity": "error", "message": "Invalid duration, frame rate or frame count."})
    if audio is None:
        issues.append({
            "category": "audio",
            "severity": "error" if expected.get("audio_required") else "warning",
            "message": "No audio stream is present.",
        })
    audio_duration = (
        float(audio.get("duration") or 0)
        if audio is not None
        else None
    )
    audio_video_delta = (
        audio_duration - duration
        if audio_duration is not None and audio_duration > 0 and duration > 0
        else None
    )
    if (
        audio_video_delta is not None
        and abs(audio_video_delta) > max(0.25, duration * 0.02)
    ):
        issues.append({
            "category": "audio_video_duration",
            "severity": "error",
            "message": (
                f"Audio/video duration differs by {audio_video_delta:.3f}s "
                f"(video={duration:.3f}s, audio={audio_duration:.3f}s)."
            ),
        })
    for field, actual in (("width", int(video.get("width", 0))), ("height", int(video.get("height", 0)))):
        if expected.get(field) and actual != expected[field]:
            issues.append({
                "category": "dimensions", "severity": "error",
                "message": f"Expected {field}={expected[field]}, got {actual}.",
            })
    if expected.get("fps") and abs(frame_rate - float(expected["fps"])) > 0.02:
        issues.append({
            "category": "frame_rate", "severity": "error",
            "message": f"Expected fps={expected['fps']}, got {frame_rate:.3f}.",
        })
    expected_duration = expected.get("duration_seconds")
    if type(expected_duration) in {int, float}:
        tolerance = max(2 / max(frame_rate, 1), float(expected_duration) * 0.02)
        if abs(duration - float(expected_duration)) > tolerance:
            issues.append({
                "category": "duration",
                "severity": "error",
                "message": (
                    f"Expected duration={float(expected_duration):.3f}s, "
                    f"got {duration:.3f}s."
                ),
            })
    expected_frames = expected.get("frame_count")
    if type(expected_frames) is int and abs(frame_count - expected_frames) > 1:
        issues.append({
            "category": "frame_count",
            "severity": "error",
            "message": f"Expected frame_count={expected_frames}, got {frame_count}.",
        })
    detect = (await command(
        "ffmpeg", "-v", "info", "-nostdin", "-i", str(path),
        "-vf", "blackdetect=d=0.30:pix_th=0.10,freezedetect=n=-55dB:d=0.75",
        "-an", "-f", "null", "-", seconds=max(120, int(duration * 4) + 30), stderr=True,
    )).decode(errors="replace")
    black_ranges = [
        {"start_seconds": float(start), "end_seconds": float(end)}
        for start, end in re.findall(r"black_start:([0-9.]+).*?black_end:([0-9.]+)", detect)
    ]
    freeze_starts = [float(value) for value in re.findall(r"freeze_start: ([0-9.]+)", detect)]
    freeze_ends = [float(value) for value in re.findall(r"freeze_end: ([0-9.]+)", detect)]
    freeze_ranges = [
        {"start_seconds": start, "end_seconds": freeze_ends[index] if index < len(freeze_ends) else duration}
        for index, start in enumerate(freeze_starts)
    ]
    if black_ranges:
        issues.append({"category": "black_frames", "severity": "warning", "ranges": black_ranges})
    if freeze_ranges:
        issues.append({"category": "frozen_frames", "severity": "warning", "ranges": freeze_ranges})
    if any(item["end_seconds"] - item["start_seconds"] >= max(1.0, duration * 0.5)
           for item in black_ranges):
        issues.append({
            "category": "black_frames",
            "severity": "error",
            "message": "A black-frame range covers a substantial part of the output.",
        })
    if any(item["end_seconds"] - item["start_seconds"] >= max(1.0, duration * 0.5)
           for item in freeze_ranges):
        issues.append({
            "category": "frozen_frames",
            "severity": "error",
            "message": "A frozen-frame range covers a substantial part of the output.",
        })
    boundaries = await _boundary_differences(
        path,
        frame_rate=frame_rate,
        frame_count=frame_count,
        boundaries=[
            float(value)
            for value in expected.get("boundaries_seconds", [])
            if type(value) in {int, float}
        ],
    )
    loudness = await _audio_loudness(path, duration) if audio is not None else None
    return {
        "passed": not any(item["severity"] == "error" for item in issues),
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "fps": frame_rate,
        "frame_count": frame_count,
        "duration_seconds": duration,
        "has_audio": audio is not None,
        "audio_duration_seconds": audio_duration,
        "audio_video_duration_delta_seconds": audio_video_delta,
        "black_ranges": black_ranges,
        "freeze_ranges": freeze_ranges,
        "boundary_differences": boundaries,
        "shared_boundary_anchors": expected.get("shared_boundary_anchors", []),
        "audio_loudness": loudness,
        "issues": issues,
    }


def review_evidence_image(sheets: list[Path]) -> bytes:
    if not sheets:
        raise MediaError("invalid_video_review", "Video review has no visual evidence.", 502)
    panels = []
    for sheet in sheets:
        with Image.open(sheet) as source:
            image = ImageOps.contain(
                source.convert("RGB"),
                (1200, 1800),
                Image.Resampling.LANCZOS,
            )
        name = sheet.name.lower()
        label = (
            "BOUNDARY SEAMS"
            if "seam" in name
            else "APPROVED ANCHORS"
            if "anchor" in name
            else "TIMELINE CONTACT SHEET"
        )
        panel = Image.new("RGB", (1200, image.height + 44), "white")
        ImageDraw.Draw(panel).text((12, 14), label, fill="black")
        panel.paste(image, ((1200 - image.width) // 2, 44))
        panels.append(panel)
    canvas = Image.new(
        "RGB",
        (1200, sum(panel.height for panel in panels) + 8 * (len(panels) - 1)),
        "white",
    )
    top = 0
    for panel in panels:
        canvas.paste(panel, (0, top))
        top += panel.height + 8
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False)
    return output.getvalue()


async def contact_sheet(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(".part.png")
    try:
        await command(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
            "-vf", "fps=1,scale=320:-2,tile=4x4:padding=4:margin=4",
            "-frames:v", "1", "-y", str(temporary), seconds=180,
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


async def seam_contact_sheet(
    path: Path,
    destination: Path,
    boundaries: list[float],
    *,
    frame_rate: float = 24,
) -> Path | None:
    valid = [value for value in boundaries if value > 0]
    if not valid:
        return None
    cells = []
    for boundary in valid:
        for offset in (-2, -1, 0, 1, 2):
            timestamp = max(0.0, boundary + offset / frame_rate)
            data = await command(
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            )
            with Image.open(io.BytesIO(data)) as source:
                cells.append(source.convert("RGB"))
    if not cells:
        return None
    columns = 5
    width = max(image.width for image in cells)
    height = max(image.height for image in cells)
    rows = (len(cells) + columns - 1) // columns
    canvas = Image.new("RGB", (columns * width, rows * height), "black")
    for index, image in enumerate(cells):
        left = (index % columns) * width + (width - image.width) // 2
        top = (index // columns) * height + (height - image.height) // 2
        canvas.paste(image, (left, top))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(".part.png")
    try:
        canvas.save(temporary, format="PNG", optimize=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


class SiyuanReviewer:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def review(
        self,
        evidence: dict,
        sheets: Path | list[Path],
        prompt_package: dict,
    ) -> dict:
        endpoint = os.environ.get(
            "AI_ROUTER_VIDEO_REVIEW_URL",
            "http://127.0.0.1:4000/v1/chat/completions",
        )
        key = os.environ.get("AI_ROUTER_VIDEO_REVIEW_KEY", "")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not key:
            return {
                "status": "manual_required",
                "verdict": "CONDITIONAL_PASS",
                "confidence": 0.0,
                "scores": {name: 0 for name in SCORE_FIELDS},
                "issues": [{"category": "reviewer", "severity": "warning",
                            "message": "Internal SIYUAN reviewer is not configured."}],
                "revised_prompt": "",
                "recommended_action": "manual_review",
                "review_model": "siyuan/auto",
                "internal_route": {},
            }
        sheets = [sheets] if isinstance(sheets, Path) else list(sheets)
        evidence_image = review_evidence_image(sheets)
        schema = {
            "verdict": "PASS|CONDITIONAL_PASS|FAIL",
            "confidence": "number 0..1",
            "scores": {name: "integer 0..100" for name in SCORE_FIELDS},
            "issues": [{
                "category": "string", "severity": "info|warning|error",
                "start_seconds": "number|null", "end_seconds": "number|null",
                "message": "string",
            }],
            "revised_prompt": "string",
            "recommended_action": "approve|manual_review|regenerate",
        }
        instruction = (
            "Act as an independent video quality reviewer. Use only the supplied prompt package, "
            "technical evidence and the single composite evidence image. Its labeled panels contain "
            "the chronological timeline, boundary seams and approved anchors. Return one JSON object matching "
            f"this schema exactly: {json.dumps(schema, separators=(',', ':'))}. "
            "Do not approve on the customer's behalf. Identify visible defects with time ranges "
            "when evidence permits. A contact sheet cannot prove perfect motion or audio sync, so "
            "lower confidence and request manual review when those properties are uncertain.\n"
            f"Prompt package: {json.dumps(prompt_package, ensure_ascii=False)}\n"
            f"Technical evidence: {json.dumps(evidence, ensure_ascii=False)}"
        )
        base_payload = {
            "model": "siyuan/auto",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(evidence_image).decode(),
                        },
                    },
                ],
            }],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        attempts = []
        for attempt in range(2):
            payload = {
                **base_payload,
                "reasoning_effort": "medium" if attempt == 0 else "low",
                "max_tokens": 3000 if attempt == 0 else 4000,
            }
            if attempt:
                payload["messages"] = [{
                    **base_payload["messages"][0],
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                instruction
                                + "\nThis is a format-repair retry. Return compact JSON only, "
                                "without Markdown fences or explanatory text."
                            ),
                        },
                        base_payload["messages"][0]["content"][1],
                    ],
                }]
            from .creative import readonly_signature
            # A real long-video review took 248s. A shorter caller timeout lost
            # its successful response and incorrectly reconciled GPU execution.
            try:
                response = await self.client.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + key,
                             "X-Siyuan-Media-Read-Only": readonly_signature(payload, os.environ.get("AI_ROUTER_MEDIA_INTERNAL_KEY", ""))},
                    json=payload,
                    timeout=360,
                )
            except httpx.HTTPError as exc:
                raise MediaError("video_review_failed", "SIYUAN评审连接中断或超时；原视频已保留，可重新检查，无需重新生成。", 502) from exc
            route = {
                name: value
                for name in (
                    "x-request-id",
                    "x-1panel-route-request-id",
                    "x-1panel-route-node",
                    "x-1panel-route-model",
                    "x-1panel-route-deployment",
                    "x-1panel-route-reason",
                )
                if (value := response.headers.get(name))
            }
            attempt_record = {
                "attempt": attempt + 1,
                "status": response.status_code,
                "route": route,
            }
            attempts.append(attempt_record)
            if response.status_code >= 400:
                continue
            try:
                value = response.json()
            except ValueError:
                continue
            for report in _json_objects(_assistant_text(value)):
                try:
                    validated = self.validate(report)
                except MediaError:
                    continue
                validated["review_model"] = "siyuan/auto"
                validated["internal_route"] = route
                validated["internal_attempts"] = attempts
                return validated

        last = attempts[-1]
        if last["status"] >= 400:
            raise MediaError(
                "video_review_failed",
                "SIYUAN video review request failed.",
                502,
                upstream_status=last["status"],
                request_id=last["route"].get("x-request-id"),
                review_attempts=attempts,
            )
        raise MediaError(
            "invalid_video_review",
            "SIYUAN returned an invalid review.",
            502,
            request_id=last["route"].get("x-request-id"),
            review_attempts=attempts,
        )

    @staticmethod
    def validate(report: dict) -> dict:
        fields = {
            "verdict",
            "confidence",
            "scores",
            "issues",
            "revised_prompt",
            "recommended_action",
        }
        if (
            not isinstance(report, dict)
            or set(report) != fields
            or report.get("verdict") not in VERDICTS
        ):
            raise MediaError("invalid_video_review", "Review verdict is invalid.", 502)
        confidence = report.get("confidence")
        scores = report.get("scores")
        issues = report.get("issues")
        if type(confidence) not in {int, float} or not 0 <= confidence <= 1:
            raise MediaError("invalid_video_review", "Review confidence is invalid.", 502)
        if not isinstance(scores, dict) or set(scores) != set(SCORE_FIELDS) or any(
            type(scores[name]) is not int or not 0 <= scores[name] <= 100 for name in SCORE_FIELDS
        ):
            raise MediaError("invalid_video_review", "Review scores are invalid.", 502)
        issue_fields = {
            "category",
            "severity",
            "start_seconds",
            "end_seconds",
            "message",
        }
        if not isinstance(issues, list) or any(
            not isinstance(item, dict)
            or set(item) - issue_fields
            or not isinstance(item.get("category"), str)
            or not item["category"].strip()
            or item.get("severity") not in {"info", "warning", "error"}
            or not isinstance(item.get("message"), str)
            or not item["message"].strip()
            or any(
                item.get(name) is not None
                and (
                    type(item.get(name)) not in {int, float}
                    or item[name] < 0
                )
                for name in ("start_seconds", "end_seconds")
            )
            or (
                item.get("start_seconds") is not None
                and item.get("end_seconds") is not None
                and item["end_seconds"] < item["start_seconds"]
            )
            for item in issues
        ):
            raise MediaError("invalid_video_review", "Review issues are invalid.", 502)
        recommendation = report.get("recommended_action")
        if not isinstance(report.get("revised_prompt", ""), str) or recommendation not in {
            "approve", "manual_review", "regenerate",
        }:
            raise MediaError("invalid_video_review", "Review recommendation is invalid.", 502)
        conflict = (
            report["verdict"] == "PASS" and recommendation != "approve"
        ) or (
            report["verdict"] == "FAIL" and recommendation == "approve"
        )
        return {
            **report,
            "status": "completed",
            "conclusion_conflict": conflict,
        }


class VideoReviewer:
    def __init__(self, client: httpx.AsyncClient):
        self.semantic = SiyuanReviewer(client)

    async def review(
        self,
        path: Path,
        *,
        output_id: str,
        artifact_sha256: str,
        prompt_package: dict,
        expected: dict | None = None,
        reference_sheets: list[Path] | None = None,
        technical: dict | None = None,
    ) -> dict:
        technical = technical or await technical_review(path, expected=expected)
        if not technical["passed"]:
            raise MediaError("video_technical_review_failed", "Video failed its technical quality gate.", 502)
        directory = path.parent / "reviews"
        timeline = await contact_sheet(path, directory / f"{output_id}-contact-sheet.png")
        seam = await seam_contact_sheet(
            path,
            directory / f"{output_id}-seams.png",
            [item["boundary_seconds"] for item in technical["boundary_differences"]],
            frame_rate=technical["fps"],
        )
        sheets = [
            timeline,
            *([seam] if seam else []),
            *(reference_sheets or []),
        ]
        semantic = await self.semantic.review(technical, sheets, prompt_package)
        payload = {
            "schema_version": 1,
            "output_id": output_id,
            "artifact_sha256": artifact_sha256,
            "prompt_hash": prompt_package.get("prompt_hash"),
            "technical": technical,
            "semantic": semantic,
            "manual_review_required": semantic.get("confidence", 0) < 0.75
            or semantic.get("verdict") != "PASS"
            or semantic.get("conclusion_conflict", False),
            "contact_sheet_path": str(timeline),
            "internal_evidence_paths": [str(sheet) for sheet in sheets],
        }
        payload["review_id"] = "rev_" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload
