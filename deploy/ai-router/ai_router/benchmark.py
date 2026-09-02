from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import yaml


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    cases = value.get("cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark manifest must contain cases")
    return cases


def matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(matches(left, right) for left, right in zip(expected, actual))
        )
    return expected == actual


def run(
    *,
    base_url: str,
    api_key: str,
    models: list[str],
    cases: list[dict[str, Any]],
    disable_thinking: bool = False,
) -> dict[str, Any]:
    results = []
    totals: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=5.0)) as client:
        for model in models:
            for case in cases:
                started = time.monotonic()
                passed = False
                error = None
                content = ""
                try:
                    payload = {
                        "model": model,
                        "messages": case["messages"],
                        "temperature": 0,
                        "max_tokens": 256,
                        "response_format": {"type": "json_object"},
                    }
                    if disable_thinking:
                        payload["thinking"] = {"type": "disabled"}
                    response = client.post(
                        f"{base_url.rstrip('/')}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    passed = matches(case["expected"], json.loads(content))
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                latency_ms = round((time.monotonic() - started) * 1000, 2)
                task = str(case["task"])
                totals[model][task].append(1 if passed else 0)
                results.append(
                    {
                        "model": model,
                        "case_id": case["id"],
                        "task": task,
                        "passed": passed,
                        "latency_ms": latency_ms,
                        "error": error,
                        "content": content,
                    }
                )
    scores = {
        model: {
            task: round(sum(values) * 100 / len(values), 2)
            for task, values in tasks.items()
        }
        for model, tasks in totals.items()
    }
    return {
        "benchmark_version": 1,
        "created_at": time.time(),
        "scores": scores,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument(
        "--api-key-env",
        default="AI_ROUTER_1PANEL_API_KEY",
    )
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument(
        "--cases",
        default=str(
            Path(__file__).resolve().parents[1] / "benchmarks" / "cases.yaml"
        ),
    )
    parser.add_argument(
        "--output",
        default="/data/benchmarks/report.json",
    )
    parser.add_argument("--disable-thinking", action="store_true")
    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"{args.api_key_env} is required")
    report = run(
        base_url=args.base_url,
        api_key=api_key,
        models=args.model,
        cases=load_cases(Path(args.cases)),
        disable_thinking=args.disable_thinking,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, output)


if __name__ == "__main__":
    main()
