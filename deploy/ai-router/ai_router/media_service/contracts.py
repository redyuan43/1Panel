from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
from typing import Any

from PIL import Image


MODELS = ("siyuan-image", "siyuan-video", "qwen-image-3.0-pro")
PUBLIC_MODELS = MODELS[:2]
TERMINAL = {"completed", "failed", "cancelled"}
USE_CASES = ("photo", "product", "ui", "infographic", "illustration", "logo")
RATIOS = ("auto", "square", "landscape", "portrait")
BACKGROUNDS = ("auto", "transparent", "opaque")
VIDEO_WORKFLOW_MODES = ("quality_gate", "duration_ladder", "legacy_pipeline")
VIDEO_CREATIVE_PROFILES = (
    "auto",
    "general",
    "ecommerce",
    "social_commerce",
    "short_drama",
    "dynamic_comic",
    "tvc",
    "ai_ad",
    "seeding",
)
VIDEO_ASPECT_RATIOS = ("16:9", "9:16")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_PIXELS = 40_000_000
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class MediaError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details: Any):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


class QuotaExceeded(MediaError):
    def __init__(self):
        super().__init__("media_quota_exhausted", "Image generation allowance is exhausted.", 429)


class UnknownOutcome(MediaError):
    def __init__(self, message: str = "Upstream outcome requires reconciliation."):
        super().__init__("media_outcome_unknown", message, 503)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def image_info(data: bytes) -> dict:
    if len(data) > 32 * 1024 * 1024:
        raise MediaError("media_too_large", "Generated image is too large.", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("pixel limit")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            content_type = Image.MIME.get(image.format, "application/octet-stream")
            if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ValueError("unsupported image format")
            return {
                "width": image.width, "height": image.height,
                "transparent": image.convert("RGBA").getchannel("A").getextrema()[0] < 255,
                "content_type": content_type,
            }
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MediaError("invalid_image", "Image cannot be decoded.", 422) from exc


def decode_asset(value: dict, *, image: bool = False) -> bytes:
    if not isinstance(value, dict) or set(value) - {"data", "content_type", "name"}:
        raise MediaError("invalid_asset", "Assets must contain uploaded data only.")
    try:
        data = base64.b64decode(value["data"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaError("invalid_asset", "Invalid Base64 asset.") from exc
    if not data or len(data) > (MAX_IMAGE_BYTES if image else MAX_UPLOAD_BYTES):
        raise MediaError("media_too_large", "Upload exceeds its size limit.", 413)
    if image:
        image_info(data)
    return data


def image_request(value: dict, edit: bool = False) -> dict:
    allowed = {"model", "prompt", "use_case", "aspect_ratio", "background", "response_format", "n", "images"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise MediaError("invalid_media_parameters", "Unsupported image parameters.")
    result = {
        "model": "siyuan-image", "use_case": "photo", "aspect_ratio": "auto",
        "background": "auto", "response_format": "b64_json", "n": 1, "images": [], **value,
    }
    if result["model"] not in ("siyuan-image", "qwen-image-3.0-pro"):
        raise MediaError("model_not_found", "Image model is not available.", 404)
    if not isinstance(result.get("prompt"), str) or not 1 <= len(result["prompt"].strip()) <= 16000:
        raise MediaError("invalid_prompt", "Prompt must contain 1-16000 characters.")
    for name, choices in (
        ("use_case", USE_CASES), ("aspect_ratio", RATIOS), ("background", BACKGROUNDS),
        ("response_format", ("b64_json", "url")), ("n", (1,)),
    ):
        if result[name] not in choices or (name == "n" and type(result[name]) is not int):
            raise MediaError("invalid_media_parameters", f"Invalid {name}.")
    images = result["images"]
    if not isinstance(images, list) or len(images) > 5 or (edit and not images) or (not edit and images):
        raise MediaError("invalid_reference_images", "Editing requires 1-5 images; generation requires none.")
    for asset in images:
        asset["content_type"] = image_info(decode_asset(asset, image=True))["content_type"]
    result["prompt"] = result["prompt"].strip()
    return result


def video_request(value: dict) -> dict:
    allowed = {
        "model", "name", "prompt", "mode", "strategy", "duration", "seed", "audio_policy",
        "watermark", "use_embedded_video_audio", "assets", "workflow_mode",
        "creative_profile", "aspect_ratio",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise MediaError("invalid_media_parameters", "Unsupported video parameters.")
    result = {
        "model": "siyuan-video", "name": "Video", "mode": "t2v", "strategy": "fast",
        "duration": 4, "seed": -1, "audio_policy": "native", "watermark": False,
        "use_embedded_video_audio": False, "assets": {},
        # An omitted value means an older client. New clients explicitly send
        # quality_gate so legacy callers keep their existing pipeline.
        "workflow_mode": "legacy_pipeline", "creative_profile": "auto",
        "aspect_ratio": "16:9", **value,
    }
    if result["model"] != "siyuan-video":
        raise MediaError("model_not_found", "Video model is not available.", 404)
    if not isinstance(result.get("prompt"), str) or not 1 <= len(result["prompt"].strip()) <= 16000:
        raise MediaError("invalid_prompt", "Prompt must contain 1-16000 characters.")
    if not isinstance(result["name"], str) or len(result["name"]) > 200:
        raise MediaError("invalid_media_parameters", "Invalid project name.")
    for field, options in (
        ("mode", ("t2v", "i2v", "l2v", "fl2v", "reference", "hybrid")),
        ("strategy", ("fast", "safe", "cloud")), ("audio_policy", ("native", "reference", "lock_source")),
        ("workflow_mode", VIDEO_WORKFLOW_MODES),
        ("creative_profile", VIDEO_CREATIVE_PROFILES),
        ("aspect_ratio", VIDEO_ASPECT_RATIOS),
    ):
        if result[field] not in options:
            raise MediaError("invalid_media_parameters", f"Invalid {field}.")
    if type(result["duration"]) is not int or not 4 <= result["duration"] <= 15:
        raise MediaError("invalid_media_parameters", "Duration must be 4-15 seconds.")
    if type(result["seed"]) is not int or not -1 <= result["seed"] < 2**63:
        raise MediaError("invalid_media_parameters", "Invalid seed.")
    for field in ("watermark", "use_embedded_video_audio"):
        if type(result[field]) is not bool:
            raise MediaError("invalid_media_parameters", f"Invalid {field}.")
    assets = result["assets"]
    if not isinstance(assets, dict) or set(assets) - {
        "first_frame", "last_frame", "reference_image", "reference_video", "reference_audio",
    }:
        raise MediaError("invalid_asset", "Unknown reference asset.")
    total = 0
    for name, asset in assets.items():
        total += len(decode_asset(asset, image=name not in {"reference_video", "reference_audio"}))
        if name not in {"reference_video", "reference_audio"}:
            asset["content_type"] = image_info(decode_asset(asset, image=True))["content_type"]
        elif asset.get("content_type") not in {"video/mp4", "video/webm", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/flac"}:
            raise MediaError("invalid_asset", "Unsupported audio or video MIME type.")
    if total > MAX_UPLOAD_BYTES:
        raise MediaError("media_too_large", "Combined uploads are too large.", 413)
    required = {
        "i2v": {"first_frame"}, "l2v": {"last_frame"}, "fl2v": {"first_frame", "last_frame"},
        "hybrid": {"first_frame", "last_frame", "reference_image"},
    }.get(result["mode"], set())
    if not required <= assets.keys():
        raise MediaError("missing_reference", "Required reference assets are missing.")
    if result["mode"] == "reference":
        if not {"reference_image", "reference_video"} & assets.keys():
            raise MediaError("missing_reference", "Reference mode needs an image or video.")
        if {"reference_video", "reference_audio"} <= assets.keys():
            raise MediaError("invalid_reference", "Separate audio cannot be combined with a reference video.")
    if result["use_embedded_video_audio"] and "reference_video" not in assets:
        raise MediaError("missing_reference", "Embedded audio requires a reference video.")
    if result["audio_policy"] == "lock_source" and not {"first_frame", "reference_audio"} <= assets.keys():
        raise MediaError("missing_reference", "Audio locking requires a first frame and source audio.")
    if result["strategy"] == "cloud" and (result["mode"] == "hybrid" or result["audio_policy"] == "lock_source"):
        raise MediaError("invalid_media_parameters", "This mode does not support cloud acceleration.")
    if result["workflow_mode"] != "legacy_pipeline" and result["strategy"] == "cloud":
        raise MediaError(
            "invalid_media_parameters",
            "Managed customer workflows use Ivan local generation; cloud is available only in the legacy pipeline.",
        )
    if result["workflow_mode"] != "legacy_pipeline" and (
        result["mode"] not in {"t2v", "i2v", "l2v", "fl2v"}
        or result["audio_policy"] != "native"
    ):
        raise MediaError(
            "invalid_media_parameters",
            "Managed Ivan workflows currently support t2v/i2v/l2v/fl2v with native audio; "
            "use legacy_pipeline for reference, hybrid, or source-locked audio.",
        )
    if result["workflow_mode"] == "duration_ladder" and result["duration"] != 15:
        raise MediaError(
            "invalid_media_parameters",
            "The duration ladder produces approximately 5, 10 and 15 seconds and requires duration=15.",
        )
    return result


def validate_settings(value: dict) -> dict:
    defaults = {
        "enabled": False, "images_enabled": True, "videos_enabled": True,
        "paid_fallback": True, "daily_paid_images": 20, "queue_limit": 10,
        "queue_timeout": 120, "image_timeout": 600, "poll_interval": 5,
        "min_free_bytes": 5 * 1024**3, "codex_ready": False, "h3_ready": False,
    }
    if not isinstance(value, dict) or set(value) - defaults.keys():
        raise MediaError("invalid_media_settings", "Unknown media setting.")
    result = {**defaults, **value}
    for name in ("enabled", "images_enabled", "videos_enabled", "paid_fallback", "codex_ready", "h3_ready"):
        if type(result[name]) is not bool:
            raise MediaError("invalid_media_settings", f"{name} must be boolean.")
    for name, low, high in (
        ("daily_paid_images", 0, 10000), ("queue_limit", 1, 100),
        ("queue_timeout", 1, 600), ("image_timeout", 1, 3600),
        ("poll_interval", 1, 60), ("min_free_bytes", 0, 1024**5),
    ):
        number = result[name]
        if type(number) is not int or not math.isfinite(number) or not low <= number <= high:
            raise MediaError("invalid_media_settings", f"Invalid {name}.")
    return result
