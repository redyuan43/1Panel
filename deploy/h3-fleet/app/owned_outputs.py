"""Resolve only completed, Fleet-owned outputs inside a pinned output root."""
from __future__ import annotations

from pathlib import Path


def owned_output(root_value: str, filename: str, subfolder: str) -> Path:
    root = Path(root_value)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError("persistent output root is unavailable")
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("invalid owned output filename")
    relative = Path(subfolder) / filename
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("invalid owned output subfolder")
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("owned output cannot be a symlink")
    if not target.is_file() or not target.resolve().is_relative_to(root):
        raise ValueError("owned output is unavailable")
    return target
