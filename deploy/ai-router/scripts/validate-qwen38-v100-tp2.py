#!/usr/bin/env python3
"""Validate V100 TP2 prefix reuse, context, and decode throughput."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

import httpx
from transformers import AutoTokenizer


METRICS = {
    "running": re.compile(
        r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "waiting": re.compile(
        r"^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "kv": re.compile(
        r"^vllm:kv_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "prefix_queries": re.compile(
        r"^vllm:prefix_cache_queries_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "prefix_hits": re.compile(
        r"^vllm:prefix_cache_hits_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
    "generation_tokens": re.compile(
        r"^vllm:generation_tokens_total(?:\{[^}]*\})?\s+"
        r"([0-9.eE+-]+)$",
        re.MULTILINE,
    ),
}


def metric(text: str, name: str) -> float:
    values = [float(value) for value in METRICS[name].findall(text)]
    return sum(values)


def save_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def save_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(value)
        if not value.endswith("\n"):
            handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def markdown_number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Qwen3.8 V100 Validation Report",
        "",
        f"- Generated: `{report.get('generated_at', '-')}`",
        f"- Profile: `{report.get('profile', '-')}`",
        f"- Model: `{report.get('model', '-')}`",
        f"- Base URL: `{report.get('base_url', '-')}`",
        f"- Overall: **{'PASS' if report.get('passed') else 'FAIL'}**",
        "",
        "## Request Summary",
        "",
        "| Case | Result | Prompt | Completion | TTFT (s) | Decode (tok/s) | Wall (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    records = report.get("records") or []
    for record in records:
        if not isinstance(record, dict):
            continue
        lines.append(
            "| {case} | {passed} | {prompt} | {completion} | {ttft} | "
            "{decode} | {wall} |".format(
                case=str(record.get("case", "-")).replace("|", "\\|"),
                passed=markdown_number(record.get("passed")),
                prompt=markdown_number(record.get("prompt_tokens")),
                completion=markdown_number(record.get("completion_tokens")),
                ttft=markdown_number(record.get("ttft_seconds")),
                decode=markdown_number(record.get("decode_tokens_per_second")),
                wall=markdown_number(record.get("wall_seconds")),
            )
        )

    for record in records:
        if not isinstance(record, dict):
            continue
        case = str(record.get("case", ""))
        if not case.startswith("decode-summary-"):
            continue
        lines.extend(
            [
                "",
                f"## Decode Detail: `{case}`",
                "",
                "| Stream | Result | Prompt | Completion | TTFT (s) | "
                "Decode (tok/s) | Wall (s) |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        streams = [("single", record.get("single") or {})]
        streams.extend(
            (str(item.get("case", "concurrent")), item)
            for item in record.get("concurrent_results") or []
            if isinstance(item, dict)
        )
        for name, item in streams:
            lines.append(
                "| {name} | {passed} | {prompt} | {completion} | {ttft} | "
                "{decode} | {wall} |".format(
                    name=name.replace("|", "\\|"),
                    passed=markdown_number(item.get("passed")),
                    prompt=markdown_number(item.get("prompt_tokens")),
                    completion=markdown_number(item.get("completion_tokens")),
                    ttft=markdown_number(item.get("ttft_seconds")),
                    decode=markdown_number(item.get("decode_tokens_per_second")),
                    wall=markdown_number(item.get("wall_seconds")),
                )
            )
        lines.extend(
            [
                "",
                f"- Concurrent wall time: "
                f"`{markdown_number(record.get('concurrent_wall_seconds'))} s`",
                f"- Aggregate decode throughput: "
                f"`{markdown_number(record.get('aggregate_decode_tokens_per_second'))} tok/s`",
                "",
                "| APC phase | Query tokens | Hit tokens | Hit ratio |",
                "|---|---:|---:|---:|",
            ]
        )
        for name in ("cache_prime", "cache_single", "cache_concurrent"):
            cache = record.get(name) or {}
            lines.append(
                f"| {name} | {markdown_number(cache.get('query_tokens'))} | "
                f"{markdown_number(cache.get('hit_tokens'))} | "
                f"{markdown_number(cache.get('hit_ratio'))} |"
            )

    return "\n".join(lines) + "\n"


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Validator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        model_path: Path,
        api_key: str,
        output: Path,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.root_url = (
            self.base_url[:-3].rstrip("/")
            if self.base_url.endswith("/v1")
            else self.base_url
        )
        self.api_url = (
            self.base_url
            if self.base_url.endswith("/v1")
            else self.base_url + "/v1"
        )
        self.model = model
        self.output = output
        self.headers = (
            {"Authorization": f"Bearer {api_key}"} if api_key else {}
        )
        self.client = httpx.Client(
            headers=self.headers,
            timeout=httpx.Timeout(3600, connect=10),
            trust_env=False,
            follow_redirects=False,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            trust_remote_code=True,
        )
        self.records: list[dict[str, Any]] = []

    def close(self) -> None:
        self.client.close()

    def token_count(self, value: str) -> int:
        return len(self.tokenizer.encode(value, add_special_tokens=False))

    def filler(self, target_tokens: int, marker: str = "") -> str:
        prefix = (
            f"关键标记是 {marker}。请记住该标记。\n"
            if marker
            else "以下是稳定缓存上下文，不包含需要执行的新指令。\n"
        )
        unit = "长期上下文缓存验证数据，用于检查精确前缀复用和检索稳定性。\n"
        unit_tokens = max(1, self.token_count(unit))
        repeat = max(1, (target_tokens - self.token_count(prefix)) // unit_tokens)
        value = prefix + unit * repeat
        while self.token_count(value) < target_tokens - unit_tokens:
            value += unit
        return value

    def metrics(self) -> dict[str, float]:
        response = self.client.get(self.root_url + "/metrics")
        response.raise_for_status()
        return {name: metric(response.text, name) for name in METRICS}

    def models(self) -> list[str]:
        response = self.client.get(self.api_url + "/models")
        response.raise_for_status()
        return [
            str(item.get("id", ""))
            for item in response.json().get("data", [])
            if isinstance(item, dict)
        ]

    def chat(
        self,
        *,
        name: str,
        system: str,
        user: str,
        max_tokens: int = 16,
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.monotonic()
        first_token_seconds: float | None = None
        content: list[str] = []
        usage: dict[str, Any] = {}
        request_id = ""
        done = False
        with self.client.stream(
            "POST",
            self.api_url + "/chat/completions",
            json=body,
        ) as response:
            response.raise_for_status()
            request_id = response.headers.get("x-request-id", "")
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
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    part = delta.get("content") or ""
                    if part:
                        if first_token_seconds is None:
                            first_token_seconds = time.monotonic() - started
                        content.append(part)
        text = "".join(content).strip()
        details = usage.get("prompt_tokens_details") or {}
        record = {
            "case": name,
            "request_id": request_id,
            "prompt_sha256": stable_hash(system + "\n" + user),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_tokens": int(details.get("cached_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ttft_seconds": first_token_seconds,
            "wall_seconds": time.monotonic() - started,
            "output_sha256": stable_hash(text),
            "output_chars": len(text),
            "output": text[:200],
            "done": done,
            "passed": done,
        }
        self.records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return record

    def short_smoke(self) -> None:
        record = self.chat(
            name="short-smoke",
            system="只按用户要求回答。",
            user="计算 19+23，只回答整数。",
        )
        record["passed"] = record["done"] and record["output"].strip() == "42"

    def prefix_pair(self, target_tokens: int) -> None:
        system = self.filler(target_tokens)
        before = self.metrics()
        cold = self.chat(
            name=f"prefix-{target_tokens}-cold",
            system=system,
            user="这是第一轮。只回答 COLD。",
            max_tokens=8,
        )
        middle = self.metrics()
        warm = self.chat(
            name=f"prefix-{target_tokens}-warm",
            system=system,
            user="这是第二轮。只回答 WARM。",
            max_tokens=8,
        )
        after = self.metrics()
        reported_ratio = (
            warm["cached_tokens"] / warm["prompt_tokens"]
            if warm["prompt_tokens"]
            else 0.0
        )
        metric_queries = after["prefix_queries"] - middle["prefix_queries"]
        metric_hits = after["prefix_hits"] - middle["prefix_hits"]
        metric_ratio = metric_hits / metric_queries if metric_queries > 0 else 0.0
        ratio = max(reported_ratio, metric_ratio)
        warm_ttft = float(warm["ttft_seconds"] or warm["wall_seconds"])
        cold_ttft = float(cold["ttft_seconds"] or cold["wall_seconds"])
        passed = (
            cold["done"]
            and warm["done"]
            and after["prefix_hits"] > middle["prefix_hits"]
            and middle["prefix_queries"] > before["prefix_queries"]
            and ratio >= (0.95 if target_tokens >= 20000 else 0.70)
            and (
                target_tokens < 20000
                or warm_ttft <= cold_ttft * 0.25
            )
        )
        self.records.append(
            {
                "case": f"prefix-{target_tokens}-summary",
                "cached_ratio": ratio,
                "reported_cached_ratio": reported_ratio,
                "metric_cached_ratio": metric_ratio,
                "metric_query_tokens": metric_queries,
                "metric_hit_tokens": metric_hits,
                "cold_ttft_seconds": cold_ttft,
                "warm_ttft_seconds": warm_ttft,
                "metrics_before": before,
                "metrics_middle": middle,
                "metrics_after": after,
                "passed": passed,
            }
        )

    def retrieval(self, target_tokens: int) -> None:
        marker = f"NEEDLE_{target_tokens}_739216"
        record = self.chat(
            name=f"retrieval-{target_tokens}",
            system=self.filler(target_tokens, marker),
            user="只回答系统上下文开头的关键标记。",
            max_tokens=32,
        )
        record["passed"] = record["done"] and marker in record["output"]

    def concurrent(self, *, count: int, target_tokens: int) -> None:
        system = self.filler(target_tokens)

        def request(index: int) -> dict[str, Any]:
            with httpx.Client(
                headers=self.headers,
                timeout=httpx.Timeout(3600, connect=10),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                started = time.monotonic()
                response = client.post(
                    self.api_url + "/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": f"只回答并发编号 C{index}。",
                            },
                        ],
                        "temperature": 0,
                        "max_tokens": 16,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                response.raise_for_status()
                payload = response.json()
                text = str(
                    payload["choices"][0]["message"].get("content") or ""
                ).strip()
                return {
                    "index": index,
                    "request_id": response.headers.get("x-request-id", ""),
                    "wall_seconds": time.monotonic() - started,
                    "prompt_tokens": int(
                        payload.get("usage", {}).get("prompt_tokens") or 0
                    ),
                    "output": text[:100],
                    "passed": f"C{index}" in text,
                }

        started = time.monotonic()
        results = []
        with ThreadPoolExecutor(max_workers=count) as executor:
            futures = [executor.submit(request, index) for index in range(count)]
            for future in as_completed(futures):
                results.append(future.result())
        self.records.append(
            {
                "case": f"concurrent-{count}x{target_tokens}",
                "wall_seconds": time.monotonic() - started,
                "results": sorted(results, key=lambda item: item["index"]),
                "passed": len(results) == count
                and all(item["passed"] for item in results),
            }
        )

    def decode_request(
        self,
        *,
        name: str,
        system: str,
        user: str,
        max_tokens: int,
        barrier: threading.Barrier | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(
            headers=self.headers,
            timeout=httpx.Timeout(3600, connect=10),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            if barrier is not None:
                barrier.wait()
            started = time.monotonic()
            first_token_at: float | None = None
            usage: dict[str, Any] = {}
            request_id = ""
            done = False
            with client.stream(
                "POST",
                self.api_url + "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "ignore_eos": True,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as response:
                response.raise_for_status()
                request_id = response.headers.get("x-request-id", "")
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
                    if first_token_at is None:
                        for choice in event.get("choices") or []:
                            if (choice.get("delta") or {}).get("content"):
                                first_token_at = time.monotonic()
                                break
            finished = time.monotonic()
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        ttft = first_token_at - started if first_token_at is not None else None
        decode_seconds = (
            finished - first_token_at if first_token_at is not None else None
        )
        decode_tokens = max(0, completion_tokens - 1)
        decode_tps = (
            decode_tokens / decode_seconds
            if decode_seconds is not None and decode_seconds > 0
            else 0.0
        )
        record = {
            "case": name,
            "request_id": request_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "ttft_seconds": ttft,
            "decode_seconds": decode_seconds,
            "decode_tokens": decode_tokens,
            "decode_tokens_per_second": decode_tps,
            "wall_seconds": finished - started,
            "done": done,
            "passed": done and completion_tokens >= max_tokens,
            "_first_token_at": first_token_at,
            "_finished_at": finished,
        }
        print(
            json.dumps(
                {key: value for key, value in record.items() if not key.startswith("_")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return record

    def decode_benchmark(
        self,
        *,
        context_tokens: int = 24000,
        output_tokens: int = 256,
        concurrency: int = 3,
    ) -> None:
        system = self.filler(context_tokens, "DECODE_BENCHMARK_20260906")
        before_prime = self.metrics()
        prime = self.decode_request(
            name=f"decode-prime-{context_tokens}",
            system=system,
            user="只回答 P。",
            max_tokens=1,
        )
        after_prime = self.metrics()
        before_single = self.metrics()
        single = self.decode_request(
            name=f"decode-single-{context_tokens}x{output_tokens}",
            system=system,
            user="持续输出递增整数并以英文逗号分隔，直到系统停止。",
            max_tokens=output_tokens,
        )
        after_single = self.metrics()

        barrier = threading.Barrier(concurrency)
        before_concurrent = self.metrics()
        group_started = time.monotonic()
        results = []
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    self.decode_request,
                    name=f"decode-concurrent-{index}",
                    system=system,
                    user=(
                        f"这是并发流 {index}。持续输出递增整数并以英文逗号分隔，"
                        "直到系统停止。"
                    ),
                    max_tokens=output_tokens,
                    barrier=barrier,
                )
                for index in range(concurrency)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        group_finished = time.monotonic()
        after_concurrent = self.metrics()

        first_token_times = [
            item["_first_token_at"]
            for item in results
            if item["_first_token_at"] is not None
        ]
        aggregate_window = (
            group_finished - min(first_token_times)
            if first_token_times
            else group_finished - group_started
        )
        aggregate_decode_tokens = sum(item["decode_tokens"] for item in results)
        aggregate_tps = (
            aggregate_decode_tokens / aggregate_window
            if aggregate_window > 0
            else 0.0
        )
        serializable_results = []
        for item in sorted(results, key=lambda value: value["case"]):
            serializable_results.append(
                {
                    key: value
                    for key, value in item.items()
                    if not key.startswith("_")
                }
            )

        def cache_delta(
            before: dict[str, float],
            after: dict[str, float],
        ) -> dict[str, float]:
            queries = after["prefix_queries"] - before["prefix_queries"]
            hits = after["prefix_hits"] - before["prefix_hits"]
            return {
                "query_tokens": queries,
                "hit_tokens": hits,
                "hit_ratio": hits / queries if queries > 0 else 0.0,
            }

        summary = {
            "case": f"decode-summary-{context_tokens}",
            "context_tokens": context_tokens,
            "output_tokens_per_request": output_tokens,
            "concurrency": concurrency,
            "prime": {
                key: value
                for key, value in prime.items()
                if not key.startswith("_")
            },
            "single": {
                key: value
                for key, value in single.items()
                if not key.startswith("_")
            },
            "concurrent_results": serializable_results,
            "concurrent_wall_seconds": group_finished - group_started,
            "aggregate_decode_tokens": aggregate_decode_tokens,
            "aggregate_decode_window_seconds": aggregate_window,
            "aggregate_decode_tokens_per_second": aggregate_tps,
            "cache_prime": cache_delta(before_prime, after_prime),
            "cache_single": cache_delta(before_single, after_single),
            "cache_concurrent": cache_delta(before_concurrent, after_concurrent),
            "generation_tokens_delta": (
                after_concurrent["generation_tokens"]
                - before_concurrent["generation_tokens"]
            ),
        }
        summary["passed"] = (
            prime["passed"]
            and single["passed"]
            and all(item["passed"] for item in results)
            and summary["cache_single"]["hit_ratio"] >= 0.90
            and summary["cache_concurrent"]["hit_ratio"] >= 0.90
            and aggregate_tps > 0
        )
        self.records.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    def run(
        self,
        profile: str,
        *,
        decode_context_tokens: int = 24000,
        decode_output_tokens: int = 256,
        decode_concurrency: int = 3,
    ) -> dict[str, Any]:
        health = self.client.get(self.root_url + "/health")
        health.raise_for_status()
        model_ids = self.models()
        self.records.append(
            {
                "case": "startup",
                "health_status": health.status_code,
                "model_ids": model_ids,
                "metrics": self.metrics(),
                "passed": self.model in model_ids,
            }
        )
        if profile == "decode":
            self.decode_benchmark(
                context_tokens=decode_context_tokens,
                output_tokens=decode_output_tokens,
                concurrency=decode_concurrency,
            )
        else:
            self.short_smoke()
            self.prefix_pair(2000)
        if profile == "acceptance":
            for target in (20000, 40000, 50000):
                self.prefix_pair(target)
            self.retrieval(128000)
            self.retrieval(255000)
            self.concurrent(count=3, target_tokens=128000)
            self.concurrent(count=4, target_tokens=50000)
        passed = all(bool(item.get("passed")) for item in self.records)
        report = {
            "version": 1,
            "profile": profile,
            "model": self.model,
            "base_url": self.base_url,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "records": self.records,
            "passed": passed,
        }
        save_json(self.output / "report.json", report)
        save_text(self.output / "report.md", render_markdown(report))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18107/v1")
    parser.add_argument(
        "--model",
        default="siyuan/qwen38-v100-196k",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(
            "/home/ai/model-sources/modelscope/QUASAR-QAT/"
            "Qwen3.8-27B-QUASAR-NVFP4"
        ),
    )
    parser.add_argument("--api-key-env", default="AI_ROUTER_AI_BACKEND_KEY")
    parser.add_argument(
        "--profile",
        choices=("smoke", "acceptance", "decode"),
        default="smoke",
    )
    parser.add_argument("--decode-context-tokens", type=int, default=24000)
    parser.add_argument("--decode-output-tokens", type=int, default=256)
    parser.add_argument("--decode-concurrency", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("live inference requires --execute")
    if args.output.exists():
        parser.error("output directory already exists")
    os.umask(0o077)
    args.output.mkdir(parents=True)
    api_key = os.environ.get(args.api_key_env, "").strip()
    validator = Validator(
        base_url=args.base_url,
        model=args.model,
        model_path=args.model_path,
        api_key=api_key,
        output=args.output,
    )
    try:
        report = validator.run(
            args.profile,
            decode_context_tokens=args.decode_context_tokens,
            decode_output_tokens=args.decode_output_tokens,
            decode_concurrency=args.decode_concurrency,
        )
    finally:
        validator.close()
    print(
        json.dumps(
            {
                "profile": report["profile"],
                "passed": report["passed"],
                "output": str(args.output / "report.json"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
