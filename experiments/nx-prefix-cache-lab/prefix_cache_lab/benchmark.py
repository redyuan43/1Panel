from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from .backend import DirectLlamaClient, RouterClient
from .config import LabConfig
from .remote import load_lab_api_key, restart_lab_runtime
from .util import digest_text, load_json, run_directory, save_json


SAFE_SUFFIXES = (
    "Summarize the main technical constraints in five concise bullets.",
    "Identify the three highest operational risks and explain why they matter.",
    "Propose a verification checklist without executing any external tools.",
    "Explain which assumptions should be validated before deployment.",
    "Describe a rollback strategy for the system discussed above.",
)

PROMPT_BOUNDARY = "\n\n[Prefix cache validation user request]\n"


def load_cases(path: Path) -> list[dict[str, Any]]:
    if path.is_file():
        value = load_json(path)
        if isinstance(value, dict) and "prompt_text" in value:
            return [{**value, "_path": str(path.resolve())}]
        if isinstance(value, dict) and isinstance(value.get("cases"), list):
            return [
                {
                    **load_json(Path(item["path"])),
                    "_path": str(Path(item["path"]).resolve()),
                }
                for item in value["cases"]
            ]
        raise ValueError(f"unsupported case file: {path}")
    manifest = path / "manifest.json"
    if manifest.exists():
        value = load_json(manifest)
        return [
            {
                **load_json(Path(item["path"])),
                "_path": str(Path(item["path"]).resolve()),
            }
            for item in value.get("cases", [])
        ]
    return [
        {**load_json(item), "_path": str(item.resolve())}
        for item in sorted(path.glob("*.json"))
        if item.name != "manifest.json"
    ]


def _common_prefix_tokens(client: DirectLlamaClient, prompt: str) -> int:
    return client.token_count(prompt + PROMPT_BOUNDARY)


def _request_record(
    *,
    case: dict[str, Any],
    phase: str,
    suffix: str,
    expected_prefix_tokens: int | None,
    result: Any,
    expected_output_marker: str | None = None,
    output_markers: dict[str, str] | None = None,
) -> dict[str, Any]:
    output = result.to_dict()
    content = output.pop("content")
    cached = output.get("cached_tokens")
    prompt_tokens = output.get("prompt_tokens")
    if expected_prefix_tokens and cached is not None:
        reuse_ratio = min(1.0, cached / expected_prefix_tokens)
    elif prompt_tokens and cached is not None:
        reuse_ratio = min(1.0, cached / prompt_tokens)
    else:
        reuse_ratio = None
    record = {
        "case_id": case["case_id"],
        "prompt_sha256": case["prompt_sha256"],
        "phase": phase,
        "suffix_sha256": digest_text(suffix),
        "expected_prefix_tokens": expected_prefix_tokens,
        "prefix_reuse_ratio": reuse_ratio,
        "cache_passed": reuse_ratio is not None and reuse_ratio >= 0.95,
        "content_sha256": digest_text(content),
        "content_chars": len(content),
        **output,
    }
    if expected_output_marker is not None:
        unexpected = [
            case_id
            for case_id, marker in (output_markers or {}).items()
            if case_id != case["case_id"] and marker in content
        ]
        marker_present = expected_output_marker in content
        record["output_sanity"] = {
            "expected_marker_sha256": digest_text(expected_output_marker),
            "expected_marker_present": marker_present,
            "unexpected_marker_case_ids": unexpected,
            "passed": marker_present and not unexpected,
        }
    return record


def _direct_request(
    client: DirectLlamaClient,
    case: dict[str, Any],
    *,
    phase: str,
    suffix: str,
    seed: int,
    max_tokens: int,
    cache_prompt: bool,
    expected_prefix_tokens: int,
    expected_output_marker: str | None = None,
    output_markers: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = client.complete(
        prompt=case["prompt_text"] + PROMPT_BOUNDARY + suffix,
        cache_prompt=cache_prompt,
        seed=seed,
        max_tokens=max_tokens,
    )
    return _request_record(
        case=case,
        phase=phase,
        suffix=suffix,
        expected_prefix_tokens=expected_prefix_tokens,
        result=result,
        expected_output_marker=expected_output_marker,
        output_markers=output_markers,
    )


def _router_request(
    client: RouterClient,
    case: dict[str, Any],
    *,
    phase: str,
    suffix: str,
    seed: int,
    max_tokens: int,
    conversation_id: str,
    expected_output_marker: str | None = None,
    output_markers: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = client.complete(
        prompt=case["prompt_text"],
        suffix=suffix,
        seed=seed,
        max_tokens=max_tokens,
        conversation_id=conversation_id,
        enable_thinking=False if expected_output_marker is not None else None,
    )
    return _request_record(
        case=case,
        phase=phase,
        suffix=suffix,
        expected_prefix_tokens=None,
        result=result,
        expected_output_marker=expected_output_marker,
        output_markers=output_markers,
    )


def run_benchmark(
    config: LabConfig,
    *,
    mode: str,
    transport: str,
    node_name: str,
    cases_path: Path,
    output_root: Path,
    seed: int,
    runs: int,
    max_tokens: int,
    apply: bool = False,
    reset_slot: bool = False,
) -> dict[str, Any]:
    if mode not in {
        "baseline",
        "warm",
        "verify",
        "interference",
        "restart",
        "isolation",
    }:
        raise ValueError(f"unsupported benchmark mode: {mode}")
    if transport not in {"direct", "router"}:
        raise ValueError(f"unsupported transport: {transport}")
    cases = load_cases(cases_path)
    if not cases:
        raise ValueError("no benchmark cases found")
    node = config.node(node_name)
    output_dir = run_directory(output_root, f"{mode}-{transport}")
    records: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []

    if mode == "restart" and transport != "direct":
        raise ValueError("restart mode must use direct transport")
    if mode == "restart" and not node.slot_cache_file:
        raise ValueError(f"{node.name} has no slot_cache_file")
    if mode == "restart" and not apply:
        lifecycle.append(
            {
                "action": "restart-lab-runtime",
                "argv": restart_lab_runtime(node, apply=False),
                "applied": False,
            }
        )
        report = {
            "version": 1,
            "mode": mode,
            "transport": transport,
            "node": node_name,
            "dry_run": True,
            "lifecycle": lifecycle,
            "records": [],
        }
        save_json(output_dir / "report.json", report)
        return report

    if transport == "direct":
        client: DirectLlamaClient | RouterClient = DirectLlamaClient(node)
    else:
        client = RouterClient(config.router)
    fatal_error = None
    try:
        selected_cases = cases[:]
        if mode == "restart":
            selected_cases = selected_cases[:1]
        if isinstance(client, DirectLlamaClient):
            erased = client.slot_action("erase")
            lifecycle.append({"action": "erase-slot", "result": erased})
        elif reset_slot:
            direct_control = DirectLlamaClient(node)
            try:
                erased = direct_control.slot_action("erase")
            finally:
                direct_control.close()
            lifecycle.append(
                {
                    "action": "erase-slot-via-direct-control",
                    "result": erased,
                }
            )
        output_markers = {
            case["case_id"]: f"CASE_{case['case_id'][:8].upper()}"
            for case in selected_cases
        }
        for case_index, case in enumerate(selected_cases):
            if not isinstance(case.get("prompt_text"), str):
                raise ValueError(f"case {case.get('case_id')} has no prompt_text")
            expected_prefix = (
                _common_prefix_tokens(client, case["prompt_text"])
                if isinstance(client, DirectLlamaClient)
                else None
            )

            def request(
                phase: str,
                suffix: str,
                cache_prompt: bool = True,
                expected_output_marker: str | None = None,
            ):
                request_seed = seed + case_index * 100 + len(records)
                if isinstance(client, DirectLlamaClient):
                    assert expected_prefix is not None
                    return _direct_request(
                        client,
                        case,
                        phase=phase,
                        suffix=suffix,
                        seed=request_seed,
                        max_tokens=max_tokens,
                        cache_prompt=cache_prompt,
                        expected_prefix_tokens=expected_prefix,
                        expected_output_marker=expected_output_marker,
                        output_markers=output_markers,
                    )
                return _router_request(
                    client,
                    case,
                    phase=phase,
                    suffix=suffix,
                    seed=request_seed,
                    max_tokens=max_tokens,
                    conversation_id=f"prefix-lab-{case['case_id']}",
                    expected_output_marker=expected_output_marker,
                    output_markers=output_markers,
                )

            if mode == "isolation":
                marker = output_markers[case["case_id"]]
                suffix = f"Reply exactly with {marker} and no other text."
                if isinstance(client, DirectLlamaClient):
                    result = client.chat_complete(
                        prompt=case["prompt_text"],
                        suffix=suffix,
                        seed=seed + case_index * 100 + len(records),
                        max_tokens=max_tokens,
                    )
                    records.append(
                        _request_record(
                            case=case,
                            phase="output-isolation",
                            suffix=suffix,
                            expected_prefix_tokens=None,
                            result=result,
                            expected_output_marker=marker,
                            output_markers=output_markers,
                        )
                    )
                else:
                    records.append(
                        request(
                            "output-isolation",
                            suffix,
                            True,
                            marker,
                        )
                    )
                continue
            if mode == "baseline":
                records.append(request("cold", SAFE_SUFFIXES[0], False))
                continue
            records.append(request("warmup", SAFE_SUFFIXES[0], True))
            if mode == "warm":
                continue
            if mode == "restart":
                assert isinstance(client, DirectLlamaClient)
                assert node.slot_cache_file is not None
                primed = client.prefill(
                    prompt=(
                        case["prompt_text"]
                        + PROMPT_BOUNDARY
                        + SAFE_SUFFIXES[0]
                    ),
                    cache_prompt=True,
                    seed=seed + case_index * 100 + len(records),
                )
                lifecycle.append(
                    {"action": "prefill-only-before-save", "result": primed}
                )
                saved = client.slot_action(
                    "save",
                    filename=node.slot_cache_file,
                )
                lifecycle.append({"action": "save-slot", "result": saved})
                client.close()
                restart_argv = restart_lab_runtime(node, apply=True)
                lifecycle.append(
                    {
                        "action": "restart-lab-runtime",
                        "argv": restart_argv,
                        "applied": True,
                    }
                )
                _wait_health(
                    node.base_url + node.health_path,
                    timeout=600,
                    api_key=load_lab_api_key(node),
                )
                client = DirectLlamaClient(node)
                restored = client.slot_action(
                    "restore",
                    filename=node.slot_cache_file,
                )
                lifecycle.append(
                    {"action": "restore-slot", "result": restored}
                )
                records.append(
                    request("post-restart-same", SAFE_SUFFIXES[0], True)
                )
                records.append(
                    request("post-restart-variant", SAFE_SUFFIXES[1], True)
                )
                continue
            if mode == "verify":
                for index in range(max(1, runs)):
                    records.append(
                        request(f"warm-{index + 1}", SAFE_SUFFIXES[index % len(SAFE_SUFFIXES)])
                    )
                continue
            unrelated = {
                "case_id": "unrelated-interference",
                "prompt_sha256": digest_text("unrelated-interference"),
                "prompt_text": (
                    "This is an unrelated short request used only to replace the "
                    "active model slot. It shares no prefix with the selected case."
                ),
            }
            if isinstance(client, DirectLlamaClient):
                unrelated_prefix = _common_prefix_tokens(
                    client, unrelated["prompt_text"]
                )
                records.append(
                    _direct_request(
                        client,
                        unrelated,
                        phase="interference",
                        suffix="Reply with interference.",
                        seed=seed + case_index * 100 + len(records),
                        max_tokens=max_tokens,
                        cache_prompt=True,
                        expected_prefix_tokens=unrelated_prefix,
                    )
                )
            else:
                records.append(
                    _router_request(
                        client,
                        unrelated,
                        phase="interference",
                        suffix="Reply with interference.",
                        seed=seed + case_index * 100 + len(records),
                        max_tokens=max_tokens,
                        conversation_id="prefix-lab-unrelated-interference",
                    )
                )
            records.append(request("post-interference", SAFE_SUFFIXES[1], True))
    except Exception as exc:
        fatal_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        client.close()

    report = {
        "version": 1,
        "created_at": time.time(),
        "mode": mode,
        "transport": transport,
        "node": node_name,
        "seed": seed,
        "runs": runs,
        "max_tokens": max_tokens,
        "case_count": len({item["case_id"] for item in records}),
        "lifecycle": lifecycle,
        "fatal_error": fatal_error,
        "summary": summarize_records(records),
        "records": records,
    }
    save_json(output_dir / "report.json", report)
    if fatal_error:
        raise RuntimeError(
            f"benchmark failed; report={output_dir / 'report.json'}; "
            f"{fatal_error['type']}: {fatal_error['message']}"
        )
    return report


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    cold = [
        item["first_token_seconds"]
        for item in records
        if item["phase"] in {"cold", "warmup"}
    ]
    warm = [
        item["first_token_seconds"]
        for item in records
        if item["phase"].startswith("warm-")
        or item["phase"] == "post-interference"
        or item["phase"].startswith("post-restart")
    ]
    ratios = [
        item["prefix_reuse_ratio"]
        for item in records
        if item.get("prefix_reuse_ratio") is not None
        and item["phase"] != "warmup"
    ]
    cache_records = [
        item
        for item in records
        if item["phase"].startswith("warm-")
        or item["phase"] == "post-interference"
        or item["phase"].startswith("post-restart")
    ]
    output_sanity = [
        item["output_sanity"]
        for item in records
        if isinstance(item.get("output_sanity"), dict)
    ]
    cold_median = statistics.median(cold) if cold else None
    warm_median = statistics.median(warm) if warm else None
    reduction = (
        1 - warm_median / cold_median
        if cold_median and warm_median is not None
        else None
    )
    return {
        "cold_ttft_median_seconds": cold_median,
        "warm_ttft_median_seconds": warm_median,
        "ttft_reduction_ratio": reduction,
        "prefix_reuse_ratio_median": statistics.median(ratios) if ratios else None,
        "cache_pass_count": sum(
            1 for item in records if item.get("cache_passed")
        ),
        "cache_fail_count": sum(
            1 for item in cache_records if not item.get("cache_passed")
        ),
        "all_cache_records_passed": bool(cache_records)
        and all(item.get("cache_passed") for item in cache_records),
        "output_sanity_pass_count": sum(
            1 for item in output_sanity if item.get("passed")
        ),
        "output_sanity_fail_count": sum(
            1 for item in output_sanity if not item.get("passed")
        ),
        "all_output_sanity_passed": bool(output_sanity)
        and all(item.get("passed") for item in output_sanity),
        "record_count": len(records),
    }


def _wait_health(
    url: str,
    *,
    timeout: int,
    api_key: str = "",
) -> None:
    deadline = time.monotonic() + timeout
    headers = (
        {"Authorization": f"Bearer {api_key}"}
        if api_key
        else None
    )
    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout(5, connect=3),
    ) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
    raise TimeoutError(f"health endpoint did not become ready: {url}")
