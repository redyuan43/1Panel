#!/usr/bin/env python3
"""Validate the repository's Codex context ownership and references."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote


REQUIRED_METADATA = {
    "status": str,
    "last_verified_at": str,
    "verified_commit": str,
    "runtime_verification": str,
    "authoritative_sources": list,
}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "<!-- context-meta":
        raise ValueError("missing context-meta JSON opener")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "-->")
    except StopIteration as exc:
        raise ValueError("missing context-meta JSON closer") from exc
    return json.loads("\n".join(lines[1:end]))


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("last_verified_at must include a timezone")
    return parsed


def markdown_targets(path: Path) -> list[str]:
    return [match.group(1).strip() for match in MARKDOWN_LINK.finditer(path.read_text(encoding="utf-8"))]


def resolve_markdown_target(path: Path, raw: str) -> Path | None:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    target = unquote(target.split("#", 1)[0]).strip()
    if not target:
        return None
    return (path.parent / target).resolve()


def changed_since(root: Path, base: str) -> set[str]:
    if not base or set(base) == {"0"}:
        return set()
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def validate_repository(
    root: Path,
    config_path: Path,
    *,
    changed_files: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[list[str], list[str]]:
    root = root.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    now = now or datetime.now(timezone.utc)

    if config.get("version") != 1:
        errors.append("docs/context-governance.json: unsupported version")

    instruction_paths = [root / item for item in config.get("instruction_files", [])]
    missing_instructions = [str(path.relative_to(root)) for path in instruction_paths if not path.is_file()]
    if missing_instructions:
        errors.append("missing instruction files: " + ", ".join(missing_instructions))

    instruction_bytes = sum(path.stat().st_size for path in instruction_paths if path.is_file())
    budget = int(config.get("instruction_budget_bytes", 24576))
    if instruction_bytes > budget:
        errors.append(f"instruction chain is {instruction_bytes} bytes; budget is {budget}")

    for literal in config.get("forbidden_instruction_literals", []):
        for path in instruction_paths:
            if path.is_file() and literal in path.read_text(encoding="utf-8"):
                errors.append(f"{path.relative_to(root)} duplicates dynamic configuration literal {literal!r}")

    governed_markdown = list(instruction_paths)
    documents = config.get("documents", [])
    freshness_days = int(config.get("freshness_warning_days", 30))
    for document in documents:
        relative = Path(document["path"])
        path = root / relative
        governed_markdown.append(path)
        if not path.is_file():
            errors.append(f"{relative}: missing governed document")
            continue
        try:
            metadata = parse_frontmatter(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: {exc}")
            continue
        for key, expected_type in REQUIRED_METADATA.items():
            value = metadata.get(key)
            if not isinstance(value, expected_type) or value in ("", []):
                errors.append(f"{relative}: invalid or missing metadata {key}")
        commit = metadata.get("verified_commit", "")
        if isinstance(commit, str) and not COMMIT.fullmatch(commit):
            errors.append(f"{relative}: verified_commit must be a full lowercase SHA-1")
        try:
            verified = parse_timestamp(metadata.get("last_verified_at", ""))
        except (TypeError, ValueError) as exc:
            errors.append(f"{relative}: invalid last_verified_at: {exc}")
        else:
            age = now.astimezone(timezone.utc) - verified.astimezone(timezone.utc)
            if age.days > freshness_days:
                warnings.append(f"{relative}: runtime snapshot is {age.days} days old")
        sources = metadata.get("authoritative_sources", [])
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, str) or not source.strip():
                    errors.append(f"{relative}: authoritative_sources contains an invalid entry")
                    continue
                resolved = (path.parent / source).resolve()
                if not resolved.exists():
                    errors.append(f"{relative}: missing authoritative source {source}")

        if changed_files is not None:
            triggered = sorted(
                changed
                for changed in changed_files
                if any(fnmatch.fnmatch(changed, pattern) for pattern in document.get("triggers", []))
            )
            if triggered and relative.as_posix() not in changed_files:
                errors.append(
                    f"{relative}: must change with governed sources: " + ", ".join(triggered)
                )

    seen: set[Path] = set()
    for path in governed_markdown:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for raw in markdown_targets(path):
            target = resolve_markdown_target(path, raw)
            if target is not None and not target.exists():
                errors.append(f"{path.relative_to(root)}: broken Markdown link {raw}")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--config", default="docs/context-governance.json")
    parser.add_argument("--base", default="")
    parser.add_argument("--changed-file", action="append", default=[])
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    config_path = (root / args.config).resolve()
    changed: set[str] | None = None
    if args.base:
        changed = changed_since(root, args.base)
    if args.changed_file:
        changed = (changed or set()) | set(args.changed_file)

    errors, warnings = validate_repository(root, config_path, changed_files=changed)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"context governance: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print(f"context governance: ok ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
