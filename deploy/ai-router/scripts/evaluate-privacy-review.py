#!/usr/bin/env python3
"""Serial, explicitly enabled evaluation; never changes model loading settings."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_router.privacy_review import (
    POLICY_VERSION, SYSTEM_PROMPT, classify, review_availability, validate_review_settings,
)
from ai_router.privacy_view import ReviewView


async def evaluate(args):
    corpus_bytes = Path(args.corpus).read_bytes()
    corpus = json.loads(corpus_bytes)
    cases = corpus["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["expected"] in {"normal", "internal_info"} for c in cases)
    random.Random(args.seed).shuffle(cases)
    if args.limit:
        cases = cases[:args.limit]
    settings = {
        "base_url": args.base_url, "model": args.model,
        "timeout_seconds": args.timeout, "backend": args.backend,
    }
    validate_review_settings(settings)
    if not args.execute:
        print(json.dumps(dict(Counter(c["expected"] for c in cases))))
        return 0
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    report = {
        "generator": corpus["generator"], "policy_version": POLICY_VERSION,
        "model": args.model, "backend": args.backend, "seed": args.seed, "started_at": time.time(),
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "policy_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "planned": len(cases), "results": [],
    }
    with os.fdopen(fd, "w") as handle:
        def checkpoint():
            handle.seek(0)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.truncate()
            handle.flush()

        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async def wait_available():
                deadline = time.monotonic() + args.idle_wait
                announced = False
                while True:
                    try:
                        reason = await review_availability(client, settings)
                    except (httpx.HTTPError, ValueError, TypeError, KeyError):
                        return "readiness_unavailable"
                    if reason != "backend_busy" or time.monotonic() >= deadline:
                        return reason
                    if not announced:
                        print("Backend busy; waiting without submitting inference.", flush=True)
                        announced = True
                    await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))

            # Only evaluate an already resident model; do not trigger a cold load.
            unavailable = await wait_available()
            if unavailable:
                report["aborted"] = unavailable
                checkpoint()
                return 2
            failures = 0
            for case in cases:
                # Do not queue behind a direct caller of the single-slot server.
                unavailable = await wait_available()
                if unavailable:
                    report["aborted"] = unavailable
                    checkpoint()
                    break
                result = await classify(
                    client, ReviewView(case["current_query"], case["context"]), settings,
                )
                report["results"].append({**case, **result})
                checkpoint()
                print(
                    f"{case['id']} expected={case['expected']} "
                    f"decision={result['decision']} valid={result['valid']} "
                    f"ms={result.get('elapsed_ms', 0)}", flush=True,
                )
                failures = 0 if result["valid"] else failures + 1
                if failures >= 3:
                    report["aborted"] = "three_consecutive_unavailable"
                    break
                await asyncio.sleep(args.pause)
        results = report["results"]
        latencies = sorted(r["elapsed_ms"] for r in results if "elapsed_ms" in r)
        normal = [r for r in results if r["expected"] == "normal"]
        probes = [r for r in results if r["expected"] == "internal_info"]
        report["summary"] = {
            "completed": len(results),
            "normal": len(normal), "internal_info": len(probes),
            "valid": sum(r["valid"] for r in results),
            "correct": sum(r["valid"] and r["decision"] == r["expected"] for r in results),
            "uncertain": sum(r["decision"] == "uncertain" for r in results),
            "false_positive": sum(r["decision"] == "internal_info" for r in normal),
            "false_negative": sum(r["decision"] == "normal" for r in probes),
            "probe_uncertain": sum(r["decision"] == "uncertain" for r in probes),
            "mean_ms": round(statistics.mean(latencies)) if latencies else None,
            "p95_ms": latencies[min(len(latencies)-1, int(len(latencies)*0.95))] if latencies else None,
        }
        report["passed"] = (
            not report.get("aborted") and len(results) == len(cases)
            and report["summary"]["correct"] == len(cases)
        )
        checkpoint()
    print(json.dumps(report["summary"]), flush=True)
    return 2 if report.get("aborted") else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default="http://agx.taild500c8.ts.net:11434")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--backend", choices=("ollama", "llamacpp"), default="ollama")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--pause", type=float, default=1)
    parser.add_argument("--idle-wait", type=float, default=60)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.pause < 0 or args.limit < 0 or args.idle_wait < 0:
        parser.error("pause, limit and idle-wait must be nonnegative")
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
