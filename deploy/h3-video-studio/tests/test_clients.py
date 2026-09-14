from __future__ import annotations

from pathlib import Path

from app.clients import build_multimodal_content


def asset(path: Path, mime: str) -> dict:
    return {"path": str(path), "ir_path": str(path), "mime": mime}


def test_hybrid_context_ir_excludes_extra_reference(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    reference = tmp_path / "reference.png"
    first.write_bytes(b"first")
    last.write_bytes(b"last")
    reference.write_bytes(b"reference")
    project = {
        "mode": "hybrid",
        "audio_policy": "native",
        "assets": {
            "first_frame": asset(first, "image/png"),
            "last_frame": asset(last, "image/png"),
            "reference_image": asset(reference, "image/png"),
        },
    }
    content = build_multimodal_content(project, "prompt", for_ir=True)
    assert [item.get("role") for item in content[1:]] == [
        "first_frame",
        "last_frame",
    ]


def test_reference_context_ir_includes_image_and_audio(tmp_path: Path) -> None:
    image = tmp_path / "reference.png"
    audio = tmp_path / "reference.m4a"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    project = {
        "mode": "reference",
        "audio_policy": "reference",
        "assets": {
            "reference_image": asset(image, "image/png"),
            "reference_audio": asset(audio, "audio/mp4"),
        },
    }
    content = build_multimodal_content(project, "prompt", for_ir=True)
    assert [item.get("role") for item in content[1:]] == [
        "reference_image",
        "reference_audio",
    ]
