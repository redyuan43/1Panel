#!/usr/bin/env python3
"""Prewarm captured Qwen3.6 prefixes without exposing template contents."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlparse

import httpx


_FINGERPRINT_FIELDS = (
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "reasoning",
    "chat_template_kwargs",
)


def _private_file(path: Path, label: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(
            f"{label} must not be accessible by group or other"
        )


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _request_body(template: dict, suffix: str) -> dict:
    if template.get("api_kind") != "chat":
        raise ValueError(
            "v1 prewarm currently supports chat templates only"
        )
    body = copy.deepcopy(template["request"])
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("captured template has no reusable messages")
    body["messages"] = [
        *messages,
        {"role": "user", "content": suffix},
    ]
    body["model"] = str(template["requested_model"])
    body["max_tokens"] = 8
    body["temperature"] = 0
    body["stream"] = False
    body["cache_prompt"] = True
    for field in _FINGERPRINT_FIELDS:
        if body.get(field) != template["request"].get(field):
            raise ValueError(
                f"prewarm changed prefix fingerprint field: {field}"
            )
    return body


def _prewarm_one(
    base_url: str,
    api_key: str,
    template: dict,
    index: int,
) -> dict:
    body = _request_body(
        template,
        f"Prefix prewarm probe {index}. Reply with OK.",
    )
    started = time.monotonic()
    with httpx.Client(
        timeout=httpx.Timeout(900, connect=10),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
    value = response.json()
    message = (value.get("choices") or [{}])[0].get("message") or {}
    content = str(
        message.get("content")
        or message.get("reasoning_content")
        or ""
    )
    usage = value.get("usage") or {}
    return {
        "request_id": response.headers.get("X-Request-ID"),
        "worker": response.headers.get("X-1Panel-Route-Deployment"),
        "affinity": response.headers.get("X-1Panel-Affinity"),
        "elapsed_seconds": time.monotonic() - started,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": (
            usage.get("prompt_tokens_details") or {}
        ).get("cached_tokens"),
        "output_sha256": hashlib.sha256(
            content.encode()
        ).hexdigest(),
        "output_chars": len(content),
        "status_code": response.status_code,
    }


def _successful(record: dict) -> bool:
    return bool(
        record.get("status_code") == 200
        and record.get("request_id")
        and record.get("worker")
    )


def _reuse_ratio(record: dict) -> float:
    prompt_tokens = int(record.get("prompt_tokens") or 0)
    cached_tokens = int(record.get("cached_tokens") or 0)
    return (
        cached_tokens / prompt_tokens
        if prompt_tokens > 0
        else 0.0
    )


def _passed(
    records: list[dict],
    verification_records: list[dict],
    copies: int,
) -> bool:
    return bool(
        len(records) == copies
        and all(_successful(item) for item in records)
        and len(verification_records) == copies
        and all(
            _successful(item)
            and item.get("affinity") == "prefix-hit"
            and _reuse_ratio(item) >= 0.95
            for item in verification_records
        )
        and len(
            {
                str(item["worker"])
                for item in verification_records
            }
        )
        == copies
    )


def _run_batch(
    base_url: str,
    api_key: str,
    template: dict,
    copies: int,
    *,
    offset: int,
) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=copies
    ) as pool:
        futures = {
            pool.submit(
                _prewarm_one,
                base_url,
                api_key,
                template,
                offset + index,
            ): index
            for index in range(copies)
        }
        results: dict[int, dict] = {}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:
                record = {
                    "probe_index": offset + index,
                    "status_code": None,
                    "request_id": None,
                    "worker": None,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                }
                if isinstance(exc, httpx.HTTPStatusError):
                    record["status_code"] = exc.response.status_code
                    record["request_id"] = exc.response.headers.get(
                        "X-Request-ID"
                    )
                    try:
                        error = exc.response.json().get("error") or {}
                    except Exception:
                        error = {}
                    if isinstance(error, dict):
                        record["error_code"] = error.get("code")
                        record["error_message"] = str(
                            error.get("message") or ""
                        )[:500]
                results[index] = record
        return [results[index] for index in range(copies)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:4000/v1",
    )
    parser.add_argument("--copies", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
    }:
        parser.error("prewarm must use the local Router endpoint")
    if not args.execute:
        parser.error("live prewarm requires --execute")
    if not 1 <= args.copies <= 4:
        parser.error("copies must be between 1 and 4")
    if args.output.exists():
        parser.error("output report already exists")
    _private_file(args.template, "template")
    _private_file(args.key_file, "key file")
    template = json.loads(args.template.read_text(encoding="utf-8"))
    api_key = args.key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        parser.error("key file is empty")

    base_url = args.base_url.rstrip("/")
    started = time.monotonic()
    report = {
        "version": 1,
        "kind": "qwen36-prefix-prewarm",
        "client_id": template.get("client_id"),
        "requested_model": template.get("requested_model"),
        "prefix_key": template.get("prefix_key"),
        "prefix_tokens": template.get("prefix_tokens"),
        "copies": args.copies,
        "records": [],
        "verification_records": [],
        "passed": False,
    }
    try:
        report["records"] = _run_batch(
            base_url,
            api_key,
            template,
            args.copies,
            offset=0,
        )
        report["verification_records"] = _run_batch(
            base_url,
            api_key,
            template,
            args.copies,
            offset=args.copies,
        )
        report["passed"] = _passed(
            report["records"],
            report["verification_records"],
            args.copies,
        )
        failures = [
            item
            for item in (
                report["records"]
                + report["verification_records"]
            )
            if item.get("error")
        ]
        if failures:
            report["error"] = failures[0]["error"]
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    report["wall_seconds"] = time.monotonic() - started
    os.umask(0o077)
    _save(args.output, report)
    print(
        json.dumps(
            {
                "client_id": report.get("client_id"),
                "prefix_tokens": report.get("prefix_tokens"),
                "workers": [
                    item.get("worker")
                    for item in report["verification_records"]
                ],
                "passed": report["passed"],
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
