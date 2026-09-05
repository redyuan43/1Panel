from __future__ import annotations

import base64
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from .config import WorkBuddyConfig
from .remote import run_remote_python
from .sanitizer import likely_contains_secret, sanitize_text
from .util import digest_text, load_json, save_json


def catalog_workbuddy(source: WorkBuddyConfig) -> dict[str, Any]:
    traces_dir = json.dumps(source.traces_dir)
    database = json.dumps(source.database)
    sessions_dir = json.dumps(source.sessions_dir)
    script = f"""
import hashlib
import json
import sqlite3
from pathlib import Path

traces_dir = Path({traces_dir})
database = {database}
sessions_dir = Path({sessions_dir})
items = []
for path in traces_dir.rglob("*.json"):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        trace = data.get("trace") if isinstance(data, dict) else None
        spans = data.get("spans") if isinstance(data, dict) else None
        if not isinstance(trace, dict) or not isinstance(spans, list):
            continue
        generation_inputs = [
            span.get("toolInput")
            for span in spans
            if isinstance(span, dict)
            and span.get("type") == "generation"
            and isinstance(span.get("toolInput"), str)
        ]
        if not generation_inputs:
            continue
        generation_input = max(generation_inputs, key=len)
        stat = path.stat()
        items.append({{
            "relative_path": str(path.relative_to(traces_dir)).replace("\\\\", "/"),
            "trace_id": str(trace.get("traceId") or ""),
            "status": str(trace.get("status") or "").lower(),
            "started_at": trace.get("startedAt"),
            "duration_ms": trace.get("duration"),
            "total_tokens": trace.get("totalTokens"),
            "file_size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "generation_input_chars": len(generation_input),
            "generation_input_sha256": hashlib.sha256(
                generation_input.encode("utf-8")
            ).hexdigest(),
        }})
    except Exception:
        continue

counts = {{}}
try:
    connection = sqlite3.connect(f"file:{{database}}?mode=ro", uri=True)
    for table in ("sessions", "buddy_snapshots", "session_usage", "workspaces"):
        counts[table] = connection.execute(
            f"select count(*) from {{table}}"
        ).fetchone()[0]
    connection.close()
except Exception as exc:
    counts["error"] = f"{{type(exc).__name__}}: {{exc}}"

print(json.dumps({{
    "version": 1,
    "trace_count": len(items),
    "session_file_count": sum(1 for _ in sessions_dir.glob("*.json")),
    "database_counts": counts,
    "items": items,
}}))
"""
    return run_remote_python(source.ssh_host, script, timeout=300)


def _validated_relative_path(value: str) -> str:
    path = PureWindowsPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe WorkBuddy trace path: {value}")
    return str(path).replace("\\", "/")


def fetch_selected_inputs(
    source: WorkBuddyConfig,
    selection: dict[str, Any],
) -> list[dict[str, Any]]:
    paths = [
        _validated_relative_path(str(item["relative_path"]))
        for item in selection.get("items", [])
    ]
    result = []
    for offset in range(0, len(paths), 24):
        result.extend(
            _fetch_input_chunk(source, paths[offset : offset + 24])
        )
    return result


def _fetch_input_chunk(
    source: WorkBuddyConfig,
    paths: list[str],
) -> list[dict[str, Any]]:
    traces_dir = json.dumps(source.traces_dir)
    paths_json = json.dumps(paths)
    script = f"""
import base64
import json
from pathlib import Path

root = Path({traces_dir})
paths = json.loads({json.dumps(paths_json)})
result = []
for relative in paths:
    path = root.joinpath(*relative.split("/"))
    data = json.loads(path.read_text(encoding="utf-8"))
    trace = data.get("trace", {{}})
    spans = data.get("spans", [])
    inputs = [
        span.get("toolInput")
        for span in spans
        if isinstance(span, dict)
        and span.get("type") == "generation"
        and isinstance(span.get("toolInput"), str)
    ]
    if not inputs:
        continue
    value = max(inputs, key=len)
    result.append({{
        "relative_path": relative,
        "trace_id": str(trace.get("traceId") or ""),
        "status": str(trace.get("status") or "").lower(),
        "total_tokens": trace.get("totalTokens"),
        "generation_input_b64": base64.b64encode(
            value.encode("utf-8")
        ).decode("ascii"),
    }})
print(json.dumps(result))
"""
    return run_remote_python(source.ssh_host, script, timeout=300)


def _normalize_generation_input(value: str) -> tuple[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value, "truncated-or-opaque-generation-input"
    if not isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False), "json-generation-input"
    blocks = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("type") or "message")
        content = item.get("content")
        if isinstance(content, str):
            blocks.append(f"[{role}]\n{content}")
        elif content is not None:
            blocks.append(
                f"[{role}]\n{json.dumps(content, ensure_ascii=False)}"
            )
    if blocks:
        return "\n\n".join(blocks), "message-list"
    return value, "opaque-message-list"


def materialize_selection(
    source: WorkBuddyConfig,
    selection_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    selection = load_json(selection_path)
    fetched = fetch_selected_inputs(source, selection)
    selected_by_path = {
        str(item["relative_path"]): item
        for item in selection.get("items", [])
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    rejected = []
    for item in fetched:
        raw = base64.b64decode(item.pop("generation_input_b64")).decode(
            "utf-8", errors="replace"
        )
        normalized, input_format = _normalize_generation_input(raw)
        sanitized, redactions = sanitize_text(normalized)
        if likely_contains_secret(sanitized):
            rejected.append(
                {
                    "relative_path_hash": digest_text(item["relative_path"]),
                    "reason": "sanitizer-residual-secret-pattern",
                }
            )
            continue
        source_item = selected_by_path.get(item["relative_path"], {})
        case_id = digest_text(
            f"{selection.get('seed')}:{item['relative_path']}:{digest_text(raw)}"
        )[:16]
        case = {
            "version": 1,
            "case_id": case_id,
            "source": {
                "kind": "workbuddy-trace",
                "trace_id_hash": digest_text(str(item.get("trace_id", ""))),
                "relative_path_hash": digest_text(item["relative_path"]),
                "source_input_sha256": digest_text(raw),
                "reported_total_tokens": item.get("total_tokens"),
                "estimated_tokens": source_item.get("estimated_tokens"),
            },
            "input_format": input_format,
            "prompt_sha256": digest_text(sanitized),
            "prompt_chars": len(sanitized),
            "redactions": redactions,
            "prompt_text": sanitized,
        }
        path = output_dir / f"{case_id}.json"
        save_json(path, case)
        cases.append(
            {
                "case_id": case_id,
                "path": str(path),
                "prompt_sha256": case["prompt_sha256"],
                "prompt_chars": case["prompt_chars"],
                "estimated_tokens": case["source"]["estimated_tokens"],
                "redactions": redactions,
            }
        )
    manifest = {
        "version": 1,
        "seed": selection.get("seed"),
        "case_count": len(cases),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "cases": cases,
    }
    save_json(output_dir / "manifest.json", manifest)
    return manifest
