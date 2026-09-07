#!/usr/bin/env python3
"""Validate same-process Edge disk prefix-cache write, eviction, and reload."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


MODEL = "RadixArk/Qwen3.8-Flash-Next-NVFP4"
METRIC_PREFIXES = (
    "vllm:prefix_cache_",
    "vllm:external_prefix_cache_",
    "vllm:prompt_tokens",
    "vllm:generation_tokens",
    "vllm:spec_decode_",
)


def post_json(url: str, payload: dict, timeout: int = 1800):
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def token_count(base_url: str, text: str) -> int:
    with post_json(
        f"{base_url}/tokenize",
        {"model": MODEL, "prompt": text},
        timeout=120,
    ) as response:
        return int(json.load(response)["count"])


def make_prompt(base_url: str, target_tokens: int, marker: str) -> tuple[str, int]:
    line = (
        f"{marker} 稳定缓存资料：同一前缀必须保持完全一致，"
        "只用于验证 KV 缓存恢复，不得混入其他会话。编号内容。\\n"
    )
    low, high = 1, max(2, target_tokens // 8)
    while token_count(base_url, line * high) < target_tokens - 100:
        high *= 2
    while low < high:
        mid = (low + high + 1) // 2
        if token_count(base_url, line * mid) <= target_tokens - 100:
            low = mid
        else:
            high = mid - 1
    body = line * low
    prompt = (
        f"唯一会话标记是 {marker}。以下是只读资料：\\n"
        f"{body}\\n"
        f"资料结束。请只回答 {marker}，不要解释，不得回答其他标记。"
    )
    return prompt, token_count(base_url, prompt)


def scrape_metrics(base_url: str) -> dict[str, float]:
    with urllib.request.urlopen(f"{base_url}/metrics", timeout=30) as response:
        text = response.read().decode()
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.rpartition(" ")
        metric_name = key.split("{", 1)[0]
        if separator and metric_name.startswith(METRIC_PREFIXES):
            values[key] = float(raw_value)
    return values


def metric_delta(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, float]:
    return {
        key: round(value - before.get(key, 0.0), 3)
        for key, value in sorted(after.items())
        if value - before.get(key, 0.0)
    }


def sum_metric(delta: dict[str, float], metric: str) -> float:
    return sum(
        value
        for key, value in delta.items()
        if key == metric or key.startswith(metric + "{")
    )


def normalize_marker_output(value: str) -> str:
    return value.strip().strip("`\"'").rstrip("。.!！").strip()


def stream_chat(
    base_url: str, prompt: str, marker: str, max_tokens: int = 32
) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.monotonic()
    first_token_at = None
    content = []
    reasoning = []
    usage = None
    with post_json(f"{base_url}/v1/chat/completions", payload) as response:
        for raw_line in response:
            line = raw_line.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                piece = delta.get("content")
                thought = delta.get("reasoning")
                if piece:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    content.append(piece)
                if thought:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    reasoning.append(thought)
    ended = time.monotonic()
    output = "".join(content).strip()
    details = (usage or {}).get("prompt_tokens_details") or {}
    return {
        "ttft_sec": round((first_token_at or ended) - started, 4),
        "elapsed_sec": round(ended - started, 4),
        "content": output,
        "reasoning_chars": len("".join(reasoning)),
        "marker_ok": normalize_marker_output(output) == marker,
        "cached_tokens": int(details.get("cached_tokens") or 0),
        "usage": usage,
    }


def command_output(args: list[str]) -> str:
    result = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def container_pids(container: str) -> list[int]:
    root = int(
        command_output(
            [
                "sudo",
                "-n",
                "docker",
                "inspect",
                "--format",
                "{{.State.Pid}}",
                container,
            ]
        )
    )
    parent_by_pid = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
            parent_by_pid[int(entry.name)] = int(fields[3])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    selected = {root}
    changed = True
    while changed:
        changed = False
        for pid, parent in parent_by_pid.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return sorted(selected)


def process_io_snapshot(container: str) -> dict[str, int]:
    totals: defaultdict[str, int] = defaultdict(int)
    pids = container_pids(container)
    for pid in pids:
        try:
            raw = command_output(["sudo", "-n", "cat", f"/proc/{pid}/io"])
        except subprocess.CalledProcessError:
            continue
        for line in raw.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                totals[key] += int(value.strip())
    return {"process_count": len(pids), **dict(sorted(totals.items()))}


def io_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        key: value - before.get(key, 0)
        for key, value in after.items()
        if key != "process_count" and value - before.get(key, 0)
    }


def disk_snapshot(path: Path) -> dict:
    files = []
    total = 0
    if path.exists():
        for candidate in sorted(path.rglob("*")):
            if not candidate.is_file():
                continue
            stat = candidate.stat()
            total += stat.st_size
            files.append(
                {
                    "path": str(candidate),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    return {"total_file_bytes": total, "files": files}


def run_request(base_url: str, prompt: str, marker: str, phase: str) -> dict:
    metrics_before = scrape_metrics(base_url)
    result = stream_chat(base_url, prompt, marker)
    metrics_after = scrape_metrics(base_url)
    result["phase"] = phase
    result["metric_delta"] = metric_delta(metrics_before, metrics_after)
    return result


def evaluate_case(
    cold: dict,
    fillers: list[dict],
    disk_reload: dict,
    min_ttft_reduction: float = 0.5,
) -> dict:
    reload_delta = disk_reload["metric_delta"]
    external_hits = sum_metric(
        reload_delta, "vllm:external_prefix_cache_hits_total"
    )
    metric_cached = sum_metric(
        reload_delta, "vllm:prompt_tokens_cached_total"
    )
    observed_cached = max(
        float(disk_reload.get("cached_tokens") or 0),
        external_hits,
        metric_cached,
    )
    cold_ttft = float(cold["ttft_sec"])
    reload_ttft = float(disk_reload["ttft_sec"])
    ttft_reduction_ratio = (
        1.0 - reload_ttft / cold_ttft if cold_ttft > 0 else 0.0
    )
    return {
        "external_hit_tokens": external_hits,
        "observed_cached_tokens": observed_cached,
        "ttft_reduction_ratio": round(ttft_reduction_ratio, 4),
        "passed": bool(
            cold["marker_ok"]
            and disk_reload["marker_ok"]
            and all(item["marker_ok"] for item in fillers)
            and observed_cached > 0
            and external_hits > 0
            and ttft_reduction_ratio >= min_ttft_reduction
        ),
    }


def markdown_report(report: dict) -> str:
    lines = [
        "# Edge Disk Prefix Cache Acceptance",
        "",
        f"- Base URL: `{report['base_url']}`",
        f"- Container: `{report['container']}`",
        f"- Result: **{'PASS' if report['passed'] else 'FAIL'}**",
        f"- Minimum TTFT reduction: `{report['min_ttft_reduction']:.0%}`",
        "",
        "| Target | Cold TTFT | Reload TTFT | Reduction | Cached | External hit | Marker |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for case in report["cases"]:
        cold = case["cold"]
        reload = case["disk_reload"]
        lines.append(
            "| {target} | {cold:.2f}s | {reload:.2f}s | {reduction:.1%} | "
            "{cached} | {external:.0f} | {marker} |".format(
                target=case["target_tokens"],
                cold=cold["ttft_sec"],
                reload=reload["ttft_sec"],
                reduction=case["ttft_reduction_ratio"],
                cached=case["observed_cached_tokens"],
                external=case["external_hit_tokens"],
                marker="yes"
                if cold["marker_ok"] and reload["marker_ok"]
                else "no",
            )
        )
    lines.extend(
        [
            "",
            f"- Disk write bytes: `{report['io_delta'].get('write_bytes', 0)}`",
            f"- Disk read bytes: `{report['io_delta'].get('read_bytes', 0)}`",
            f"- Cache file bytes: `{report['disk_after']['total_file_bytes']}`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--disk-path", type=Path, required=True)
    parser.add_argument("--targets", default="20000,40000,50000")
    parser.add_argument("--eviction-prompt-tokens", type=int, default=50000)
    parser.add_argument("--eviction-total-tokens", type=int, default=650000)
    parser.add_argument("--min-ttft-reduction", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args()

    if ":18300" in args.base_url and not args.allow_production:
        raise SystemExit("refusing production port without --allow-production")
    if not 0 <= args.min_ttft_reduction < 1:
        raise SystemExit("--min-ttft-reduction must be in [0, 1)")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    targets = [int(value) for value in args.targets.split(",") if value]
    filler_count = (
        args.eviction_total_tokens + args.eviction_prompt_tokens - 1
    ) // args.eviction_prompt_tokens
    run_id = time.strftime("%Y%m%dT%H%M%S")
    report = {
        "run_id": run_id,
        "base_url": args.base_url,
        "container": args.container,
        "model": MODEL,
        "targets": targets,
        "eviction_prompt_tokens": args.eviction_prompt_tokens,
        "eviction_total_tokens": args.eviction_total_tokens,
        "min_ttft_reduction": args.min_ttft_reduction,
        "started_unix": time.time(),
        "metrics_before": scrape_metrics(args.base_url),
        "io_before": process_io_snapshot(args.container),
        "disk_before": disk_snapshot(args.disk_path),
        "cases": [],
    }

    all_markers: set[str] = set()
    for index, target in enumerate(targets, start=1):
        marker = f"EDGE_DISK_TARGET_{run_id}_{index:02d}_{target}"
        all_markers.add(marker)
        prompt, raw_tokens = make_prompt(args.base_url, target, marker)
        cold = run_request(args.base_url, prompt, marker, "cold")
        print(json.dumps({"target": target, **cold}, ensure_ascii=False))

        fillers = []
        for filler_index in range(1, filler_count + 1):
            filler_marker = (
                f"EDGE_DISK_FILL_{run_id}_{index:02d}_{filler_index:02d}"
            )
            all_markers.add(filler_marker)
            filler_prompt, filler_tokens = make_prompt(
                args.base_url,
                args.eviction_prompt_tokens,
                filler_marker,
            )
            filler = run_request(
                args.base_url,
                filler_prompt,
                filler_marker,
                "gpu_eviction",
            )
            filler["raw_prompt_tokens"] = filler_tokens
            fillers.append(filler)
            print(
                json.dumps(
                    {
                        "target": target,
                        "filler": filler_index,
                        **filler,
                    },
                    ensure_ascii=False,
                )
            )

        disk_reload = run_request(
            args.base_url, prompt, marker, "disk_reload"
        )
        evaluation = evaluate_case(
            cold,
            fillers,
            disk_reload,
            min_ttft_reduction=args.min_ttft_reduction,
        )
        case = {
            "target_tokens": target,
            "raw_prompt_tokens": raw_tokens,
            "marker": marker,
            "cold": cold,
            "fillers": fillers,
            "disk_reload": disk_reload,
            **evaluation,
        }
        report["cases"].append(case)
        print(json.dumps({"target_result": case}, ensure_ascii=False))

    report["metrics_after"] = scrape_metrics(args.base_url)
    report["io_after"] = process_io_snapshot(args.container)
    report["io_delta"] = io_delta(report["io_before"], report["io_after"])
    report["disk_after"] = disk_snapshot(args.disk_path)
    report["unique_marker_count"] = len(all_markers)
    report["finished_unix"] = time.time()
    report["passed"] = bool(
        all(case["passed"] for case in report["cases"])
        and report["io_delta"].get("write_bytes", 0) > 0
        and report["io_delta"].get("read_bytes", 0) > 0
        and report["disk_after"]["total_file_bytes"] > 0
    )

    json_path = args.output_dir / "report.json"
    markdown_path = args.output_dir / "REPORT.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({"report": str(json_path), "passed": report["passed"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
