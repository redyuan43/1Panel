#!/usr/bin/env python3
"""Run an explicitly authorized, serial long-context retrieval acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import time
from urllib.parse import urlparse

import httpx


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def wait_idle(client: httpx.Client, base: str) -> list[dict]:
    deadline = time.monotonic() + 120
    while True:
        response = client.get(base + "/slots")
        response.raise_for_status()
        slots = response.json()
        if slots and all(item.get("is_processing") is False for item in slots):
            return slots
        if time.monotonic() >= deadline:
            raise RuntimeError("endpoint remained busy; no inference submitted")
        time.sleep(2)


def build_messages(lines: list[str], expected: dict[str, str]) -> list[dict]:
    records = list(lines)
    # Spread unpredictable values across the complete input, including both ends.
    for position, (key, value) in reversed(list(enumerate(expected.items()))):
        offset = int(len(lines) * position / (len(expected) - 1))
        records.insert(offset, f"LOOKUP {key} = {value}")
    return [
        {
            "role": "system",
            "content": (
                "Read the entire record collection. Return exactly one JSON object "
                "mapping each requested LOOKUP key to its exact value. Ignore ordinary "
                "records. Do not explain and do not guess missing values."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(records) + "\nRequested keys: " + ", ".join(expected),
        },
    ]


def prompt_tokens(client: httpx.Client, base: str, messages: list[dict]) -> int:
    response = client.post(
        base + "/apply-template",
        json={"messages": messages, "chat_template_kwargs": {"enable_thinking": False}},
    )
    response.raise_for_status()
    prompt = response.json()["prompt"]
    response = client.post(
        base + "/tokenize",
        json={"content": prompt, "add_special": True, "parse_special": True},
    )
    response.raise_for_status()
    return len(response.json()["tokens"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or not (
        parsed.hostname in {"localhost", "127.0.0.1"}
        or (parsed.hostname or "").endswith(".taild500c8.ts.net")
    ):
        parser.error("only direct local or project Tailscale endpoints are allowed")
    if not args.execute:
        parser.error("live inference requires explicit --execute authorization")
    if args.context < 8192 or args.context > 262144:
        parser.error("context must be between 8192 and 262144")
    os.umask(0o077)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    base = args.base_url.rstrip("/")
    report = {"model": args.model, "context": args.context, "passed": False}
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(1800, connect=5),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            models = client.get(base + "/v1/models")
            models.raise_for_status()
            if args.model not in [item["id"] for item in models.json()["data"]]:
                raise RuntimeError("served model does not match requested artifact")
            slots = wait_idle(client, base)
            save(output / "preflight.json", {"models": models.json(), "slots": slots})
            if len(slots) != 1 or slots[0]["n_ctx"] < args.context:
                raise RuntimeError("requires a single slot with sufficient context")
            seed = secrets.randbits(64)
            rng = random.Random(seed)
            lines = [
                f"record {index:06d}: code {rng.getrandbits(48):012x}; "
                f"quantity {rng.randrange(10000)}; status archived."
                for index in range(args.context // 12)
            ]
            expected = {
                f"item_{position}": secrets.token_hex(6)
                for position in range(5)
            }
            # Leave the requested completion budget and a small template margin.
            target = args.context - 640
            count = min(len(lines), max(1, target // 26))
            for attempt in range(8):
                messages = build_messages(lines[:count], expected)
                measured = prompt_tokens(client, base, messages)
                print(f"prepare attempt={attempt} tokens={measured} target={target}", flush=True)
                if target - 128 <= measured <= target:
                    break
                delta = int((target - measured - 32) / max(1, measured / count))
                count = max(1, min(len(lines), count + (delta or (-1 if measured > target else 1))))
            if not target - 256 <= measured <= target:
                raise RuntimeError(f"could not size prompt safely: {measured}")
            body = {
                "model": args.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 512,
                "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False},
                "cache_prompt": False,
            }
            save(output / "request.json", body)
            report.update({
                "seed": seed,
                "expected": expected,
                "measured_prompt_tokens": measured,
                "request_sha256": hashlib.sha256(
                    json.dumps(body, sort_keys=True).encode()
                ).hexdigest(),
            })
            wait_idle(client, base)
            text = ""
            usage = {}
            finish = None
            done = False
            first_token_seconds = None
            inference_started = time.monotonic()
            print(f"inference start: context={args.context} prompt={measured}", flush=True)
            with client.stream("POST", base + "/v1/chat/completions", json=body) as response:
                response.raise_for_status()
                with (output / "response.sse").open("x", encoding="utf-8") as raw:
                    for line in response.iter_lines():
                        raw.write(line + "\n")
                        raw.flush()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                            continue
                        event = json.loads(data)
                        if "error" in event:
                            raise RuntimeError(str(event["error"]))
                        if event.get("usage"):
                            usage = event["usage"]
                        for choice in event.get("choices", []):
                            part = choice.get("delta", {}).get("content") or ""
                            if part and first_token_seconds is None:
                                first_token_seconds = time.monotonic() - inference_started
                            text += part
                            finish = choice.get("finish_reason") or finish
            content = text.strip()
            if content.startswith("```") and content.endswith("```"):
                content = "\n".join(content.splitlines()[1:-1])
            actual = json.loads(content)
            actual_tokens = int(usage.get("prompt_tokens", 0))
            report.update({
                "actual": actual,
                "usage": usage,
                "finish_reason": finish,
                "done": done,
                "first_token_seconds": first_token_seconds,
                "inference_seconds": time.monotonic() - inference_started,
                "passed": (
                    actual == expected and done and finish == "stop"
                    and abs(actual_tokens - measured) <= 16
                    and actual_tokens + int(usage.get("completion_tokens", 0)) <= args.context
                ),
            })
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["wall_seconds"] = time.monotonic() - started
    save(output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
