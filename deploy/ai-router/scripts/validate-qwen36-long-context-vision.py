#!/usr/bin/env python3
"""Validate a Qwen3.6 long-context image request without storing its body."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import time
from urllib.parse import urlparse
import zlib

import httpx


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", checksum)
    )


def red_png_data_url(size: int = 64) -> tuple[str, str]:
    scanlines = b"".join(
        b"\x00" + b"\xff\x00\x00" * size
        for _ in range(size)
    )
    image = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )
    return (
        "data:image/png;base64,"
        + base64.b64encode(image).decode("ascii"),
        hashlib.sha256(image).hexdigest(),
    )


def wait_idle(
    client: httpx.Client,
    root: str,
    headers: dict[str, str],
) -> list[dict]:
    deadline = time.monotonic() + 180
    while True:
        response = client.get(root + "/slots", headers=headers)
        response.raise_for_status()
        slots = response.json()
        if (
            isinstance(slots, list)
            and slots
            and all(item.get("is_processing") is False for item in slots)
        ):
            return slots
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "endpoint remained busy; no inference submitted"
            )
        time.sleep(2)


def save_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default="siyuan/qwen36-shared")
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--context", type=int, default=262144)
    parser.add_argument("--target-min-tokens", type=int, default=250000)
    parser.add_argument("--repeat-lines", type=int, default=7500)
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    parsed = urlparse(args.base_url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "http" or not (
        hostname in {"localhost", "127.0.0.1"}
        or hostname.endswith(".taild500c8.ts.net")
    ):
        parser.error(
            "only direct local or project Tailscale endpoints are allowed"
        )
    if not args.execute:
        parser.error("live inference requires --execute")
    if args.context < 8192 or args.context > 262144:
        parser.error("context must be between 8192 and 262144")
    if args.target_min_tokens + args.max_tokens > args.context:
        parser.error("target plus output reserve exceeds context")
    if args.output.exists():
        parser.error("output report already exists")

    os.umask(0o077)
    api_key = (
        os.environ.get(args.api_key_env, "").strip()
        if args.api_key_env
        else ""
    )
    if args.api_key_env and not api_key:
        parser.error(f"missing API key environment: {args.api_key_env}")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    base = args.base_url.rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    api_base = base if base.endswith("/v1") else base + "/v1"

    unit = (
        "这是固定系统上下文，用于验证长上下文视觉边界。"
        "规则：忽略前文重复内容，只识别最后图片颜色。"
        "编号%05d。\n"
    )
    system = "".join(unit % index for index in range(args.repeat_lines))
    image_url, image_sha256 = red_png_data_url()
    body = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "只回答这张纯色图片的颜色。",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            },
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    report = {
        "version": 1,
        "kind": "qwen36-long-context-vision-boundary",
        "worker": args.worker,
        "model": args.model,
        "configured_context_tokens": args.context,
        "target_min_tokens": args.target_min_tokens,
        "repeat_lines": args.repeat_lines,
        "max_tokens": args.max_tokens,
        "prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "image_sha256": image_sha256,
        "passed": False,
    }

    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(2700, connect=10),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            slots = wait_idle(client, root, headers)
            if len(slots) != 1 or int(slots[0].get("n_ctx", 0)) < args.context:
                raise RuntimeError(
                    "worker does not expose one slot at requested context"
                )
            models = client.get(api_base + "/models", headers=headers)
            models.raise_for_status()
            model_ids = [
                str(item.get("id", ""))
                for item in models.json().get("data", [])
                if isinstance(item, dict)
            ]
            if args.model not in model_ids:
                raise RuntimeError("served model alias does not match")

            inference_started = time.monotonic()
            first_token_seconds = None
            content_parts: list[str] = []
            usage: dict = {}
            timings: dict = {}
            done = False
            with client.stream(
                "POST",
                api_base + "/chat/completions",
                headers=headers,
                json=body,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        continue
                    event = json.loads(payload)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    if isinstance(event.get("timings"), dict):
                        timings = event["timings"]
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        part = delta.get("content") or ""
                        if part:
                            if first_token_seconds is None:
                                first_token_seconds = (
                                    time.monotonic() - inference_started
                                )
                            content_parts.append(part)

            content = "".join(content_parts).strip()
            prompt_tokens = int(
                usage.get("prompt_tokens")
                or timings.get("prompt_n")
                or 0
            )
            completion_tokens = int(
                usage.get("completion_tokens")
                or timings.get("predicted_n")
                or 0
            )
            cached_tokens = int(
                (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens",
                    0,
                )
            )
            report.update(
                {
                    "prompt_tokens": prompt_tokens,
                    "cached_tokens": cached_tokens,
                    "completion_tokens": completion_tokens,
                    "ttft_seconds": first_token_seconds,
                    "inference_seconds": (
                        time.monotonic() - inference_started
                    ),
                    "done": done,
                    "output_sha256": hashlib.sha256(
                        content.encode()
                    ).hexdigest(),
                    "output_chars": len(content),
                    "vision_correct": (
                        "红" in content.lower()
                        or "red" in content.lower()
                    ),
                }
            )
            report["passed"] = bool(
                done
                and report["vision_correct"]
                and prompt_tokens >= args.target_min_tokens
                and prompt_tokens + completion_tokens <= args.context
            )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    report["wall_seconds"] = time.monotonic() - started
    save_report(args.output, report)
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "worker",
                    "prompt_tokens",
                    "cached_tokens",
                    "completion_tokens",
                    "ttft_seconds",
                    "inference_seconds",
                    "vision_correct",
                    "passed",
                    "error",
                )
                if key in report
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
