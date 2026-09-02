from __future__ import annotations

import base64
import binascii
import io
import json
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import RouterError


@dataclass(frozen=True)
class ImageInputs:
    embedded: int = 0
    remote: int = 0

    @property
    def total(self) -> int:
        return self.embedded + self.remote


def inspect_image_inputs(body: dict[str, Any]) -> ImageInputs:
    embedded, remote = _inspect_value(body)
    return ImageInputs(embedded=embedded, remote=remote)


def normalize_ai_images(
    body: dict[str, Any],
    *,
    max_dimension: int,
    max_source_pixels: int,
) -> tuple[dict[str, Any], int]:
    value = json.loads(json.dumps(body))
    resized = [0]
    _normalize_value(
        value,
        max_dimension=max_dimension,
        max_source_pixels=max_source_pixels,
        resized=resized,
    )
    return value, resized[0]


def _inspect_value(
    value: Any,
    *,
    image_context: bool = False,
) -> tuple[int, int]:
    if isinstance(value, list):
        embedded = 0
        remote = 0
        for item in value:
            item_embedded, item_remote = _inspect_value(
                item,
                image_context=image_context,
            )
            embedded += item_embedded
            remote += item_remote
        return embedded, remote
    if isinstance(value, dict):
        item_type = str(value.get("type", "")).lower()
        media_type = str(value.get("media_type", "")).lower()
        current_context = (
            image_context
            or "image" in item_type
            or media_type.startswith("image/")
        )
        embedded = int(
            current_context
            and item_type == "base64"
            and isinstance(value.get("data"), str)
        )
        remote = 0
        for key, item in value.items():
            if key == "data" and embedded:
                continue
            child_context = (
                current_context
                or key in {"image", "image_url"}
            )
            item_embedded, item_remote = _inspect_value(
                item,
                image_context=child_context,
            )
            embedded += item_embedded
            remote += item_remote
        return embedded, remote
    if not isinstance(value, str) or not image_context:
        return 0, 0
    lowered = value.strip().lower()
    if lowered.startswith("data:image/"):
        return 1, 0
    if lowered.startswith(("http://", "https://")):
        return 0, 1
    return 0, 0


def _normalize_value(
    value: Any,
    *,
    max_dimension: int,
    max_source_pixels: int,
    resized: list[int],
    image_context: bool = False,
) -> Any:
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _normalize_value(
                item,
                max_dimension=max_dimension,
                max_source_pixels=max_source_pixels,
                resized=resized,
                image_context=image_context,
            )
        return value
    if isinstance(value, dict):
        item_type = str(value.get("type", "")).lower()
        media_type = str(value.get("media_type", "")).lower()
        current_context = (
            image_context
            or "image" in item_type
            or media_type.startswith("image/")
        )
        if (
            current_context
            and item_type == "base64"
            and media_type.startswith("image/")
            and isinstance(value.get("data"), str)
        ):
            normalized, changed_size = _normalize_data_url(
                f"data:{media_type};base64,{value['data']}",
                max_dimension=max_dimension,
                max_source_pixels=max_source_pixels,
            )
            header, encoded = normalized.split(",", 1)
            value["media_type"] = header[5:].split(";", 1)[0]
            value["data"] = encoded
            if changed_size:
                resized[0] += 1
            return value
        for key, item in list(value.items()):
            child_context = (
                current_context
                or key in {"image", "image_url"}
            )
            value[key] = _normalize_value(
                item,
                max_dimension=max_dimension,
                max_source_pixels=max_source_pixels,
                resized=resized,
                image_context=child_context,
            )
        return value
    if (
        not isinstance(value, str)
        or not image_context
        or not value.strip().lower().startswith("data:image/")
    ):
        return value
    normalized, changed_size = _normalize_data_url(
        value,
        max_dimension=max_dimension,
        max_source_pixels=max_source_pixels,
    )
    if changed_size:
        resized[0] += 1
    return normalized


def _normalize_data_url(
    value: str,
    *,
    max_dimension: int,
    max_source_pixels: int,
) -> tuple[str, bool]:
    try:
        header, encoded = value.split(",", 1)
    except ValueError as exc:
        raise RouterError(
            "embedded image data URL is invalid",
            status_code=400,
            code="invalid_image_data",
        ) from exc
    if ";base64" not in header.lower():
        raise RouterError(
            "embedded images must use base64 data URLs",
            status_code=400,
            code="invalid_image_data",
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RouterError(
            "embedded image base64 is invalid",
            status_code=400,
            code="invalid_image_data",
        ) from exc
    try:
        with Image.open(io.BytesIO(payload)) as source:
            width, height = source.size
            if width <= 0 or height <= 0:
                raise ValueError("image dimensions must be positive")
            if width * height > max_source_pixels:
                raise RouterError(
                    "embedded image dimensions exceed the safety limit",
                    status_code=413,
                    code="image_dimensions_too_large",
                    details={
                        "width": width,
                        "height": height,
                        "max_source_pixels": max_source_pixels,
                    },
                )
            orientation = source.getexif().get(274, 1)
            image = ImageOps.exif_transpose(source)
            image.load()
    except RouterError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise RouterError(
            "embedded image format is invalid or unsupported",
            status_code=400,
            code="invalid_image_data",
        ) from exc

    changed_size = max(image.size) > max_dimension
    if changed_size:
        image.thumbnail(
            (max_dimension, max_dimension),
            Image.Resampling.LANCZOS,
        )
    if not changed_size and orientation in {0, 1}:
        return value, False
    if image.mode not in {"RGB", "RGBA", "L", "LA"}:
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    normalized = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{normalized}", changed_size
