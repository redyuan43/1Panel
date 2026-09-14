#!/usr/bin/env python3
"""按请求 ID 只读解释 Router 选模或拒绝原因。

默认只读取 route-traces SQLite。只有显式使用 --live-short 时才发送一条
固定的合成短请求；该请求没有客户端重试，也不会执行工具。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_router.config import Registry, Settings
from ai_router.route_diagnosis import diagnose_route


MARKER = "ROUTE_DIAGNOSTIC_OK"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def read_trace(path: Path, identifier: str) -> dict | None:
    """以 SQLite 只读模式查找 Router ID 或客户端请求 ID。"""
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as database:
        row = database.execute(
            "SELECT payload_json FROM route_traces WHERE request_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if row is None:
            row = database.execute(
                "SELECT payload_json FROM route_traces "
                "WHERE client_request_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (identifier,),
            ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise ValueError("route trace payload must be an object")
    return value


def _error_code(trace: dict) -> str | None:
    error = trace.get("error")
    if isinstance(error, dict):
        value = error.get("code")
        return str(value) if value else None
    return None


def _candidate_rows(trace: dict) -> list[dict]:
    rows: list[dict] = []
    for attempt in trace.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        for step in attempt.get("steps") or []:
            if not isinstance(step, dict) or step.get("node_id") != "candidate_scope":
                continue
            candidates = (step.get("evidence") or {}).get("candidates") or []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        key: item.get(key)
                        for key in (
                            "endpoint_id",
                            "node",
                            "eligible",
                            "rejection_reason",
                            "required_context_tokens",
                            "safe_context_tokens",
                            "healthy",
                            "fresh",
                            "load_headroom",
                            "quality_score",
                            "tier",
                            "cloud",
                        )
                    }
                )
    return rows


def _selection_rows(trace: dict) -> list[dict]:
    rows: list[dict] = []
    for attempt in trace.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        selection = attempt.get("selection")
        if not isinstance(selection, dict):
            continue
        rows.append(
            {
                "attempt": attempt.get("attempt"),
                **{
                    key: selection.get(key)
                    for key in (
                        "endpoint_id",
                        "deployment_id",
                        "selected_model",
                        "reason",
                        "affinity",
                    )
                },
            }
        )
    return rows


def _pre_route_explanation(trace: dict) -> dict | None:
    if trace.get("route_selected") or trace.get("attempts"):
        return None
    code = _error_code(trace)
    status = str(trace.get("status") or "")
    if status != "failed":
        verdict = (
            "请求仍在处理中，尚未进入候选筛选。"
            if status in {"", "running"}
            else f"请求状态为 {status}，审计尚未记录选模。"
        )
    else:
        labels = {
            "invalid_api_key": "请求在认证阶段被拒绝，尚未进入候选筛选。",
            "parallel_limit_exceeded": "请求在并发限流阶段被拒绝，尚未进入候选筛选。",
            "request_exceeds_tpm_limit": (
                "单次请求的输入 Token 超过账户每分钟 TPM 上限，"
                "尚未进入候选筛选。"
            ),
            "rpm_limit_exceeded": "请求在 RPM 限流阶段被拒绝，尚未进入候选筛选。",
            "tpm_limit_exceeded": "请求在 TPM 限流阶段被拒绝，尚未进入候选筛选。",
        }
        verdict = labels.get(
            code,
            f"请求在候选筛选前失败（错误码 {code or 'unknown'}）。",
        )
    return {
        "diagnosis_version": 1,
        "verdict": verdict,
        "causal_chain": [
            {
                "stage": "request_preflight",
                "title": "请求前置检查",
                "detail": verdict,
            },
            {
                "stage": "candidate_scope",
                "title": "候选资格筛选",
                "detail": "未执行",
            },
        ],
        "non_causes": (
            [
                {
                    "code": "route_not_started",
                    "label": "不是模型候选不足",
                    "evidence": "审计记录中没有选模尝试",
                }
            ]
            if status == "failed"
            else []
        ),
        "alternatives": [],
        "policy_refs": [],
    }


def build_report(
    trace: dict | None,
    *,
    identifier: str,
    settings: Settings,
    registry: Registry,
) -> dict:
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence": "route_trace_read_only",
        "lookup_id": identifier,
        "found": trace is not None,
        "source": {
            "release_revision": os.environ.get("AI_ROUTER_RELEASE_REVISION") or None,
            "route_diagnosis_sha256": hashlib.sha256(
                (ROOT / "ai_router/route_diagnosis.py").read_bytes()
            ).hexdigest(),
        },
    }
    if trace is None:
        return report
    request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
    report["request"] = {
        "request_id": trace.get("request_id"),
        "client_request_id": trace.get("client_request_id"),
        "started_at": trace.get("started_at"),
        "protocol": trace.get("protocol"),
        "task": trace.get("task"),
        "requested_model": trace.get("requested_model"),
        "prompt_tokens": request.get("prompt_tokens"),
        "output_reserve_tokens": request.get("output_reserve_tokens"),
        "required_context_tokens": request.get("required_context_tokens"),
        "modalities": request.get("modalities"),
    }
    report["outcome"] = {
        "status": trace.get("status"),
        "status_code": trace.get("status_code"),
        "route_selected": bool(trace.get("route_selected")),
        "selected_model": trace.get("selected_model"),
        "endpoint_id": trace.get("endpoint_id"),
        "deployment_id": trace.get("deployment_id"),
        "error_code": _error_code(trace),
    }
    report["selections"] = _selection_rows(trace)
    report["candidates"] = _candidate_rows(trace)
    explanation = _pre_route_explanation(trace) or diagnose_route(
        trace,
        [trace],
        settings,
        registry,
    )
    report["explanation"] = {
        key: explanation.get(key)
        for key in (
            "diagnosis_version",
            "verdict",
            "causal_chain",
            "non_causes",
            "alternatives",
            "policy_refs",
        )
    }
    return report


async def live_short(
    base_url: str,
    key: str,
    *,
    model: str = "siyuan/auto",
    timeout: float = 60,
    transport=None,
) -> dict:
    """发送一次无工具短请求；结果不保留响应正文或凭据。"""
    client_request_id = "route-diagnostic-" + uuid4().hex
    result = {
        "evidence": "single_live_request",
        "client_request_id": client_request_id,
        "request_id": None,
        "status_code": None,
        "marker_matched": False,
    }
    payload = {
        "model": model,
        "stream": False,
        "max_tokens": 32,
        "messages": [
            {
                "role": "user",
                "content": f"只回复 {MARKER}，不要解释。",
            }
        ],
    }

    async def send_once() -> None:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5)),
        ) as client:
            async with client.stream(
                "POST",
                base_url.rstrip("/") + "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer " + key,
                    "X-Request-ID": client_request_id,
                },
                json=payload,
            ) as response:
                result["request_id"] = response.headers.get("x-request-id")
                result["status_code"] = response.status_code
                if response.status_code != 200:
                    return
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 65536:
                        result["response_error"] = "response_too_large"
                        return
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                choices = value.get("choices") if isinstance(value, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices else {}
                message = choice.get("message") if isinstance(choice, dict) else {}
                content = message.get("content") if isinstance(message, dict) else None
                result["marker_matched"] = (
                    isinstance(content, str) and content.strip() == MARKER
                )
                result["finish_reason"] = (
                    choice.get("finish_reason") if isinstance(choice, dict) else None
                )

    started = time.monotonic()
    try:
        await asyncio.wait_for(send_once(), timeout=timeout)
    except (
        asyncio.TimeoutError,
        TimeoutError,
        httpx.HTTPError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        result["transport_error"] = type(exc).__name__
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def live_verdict(live: dict, trace: dict | None) -> str:
    if trace is None:
        return "audit_unconfirmed"
    if trace.get("client_request_id") != live.get("client_request_id"):
        return "audit_identity_mismatch"
    if trace.get("status") == "failed" and not trace.get("route_selected"):
        return "router_rejected"
    if not trace.get("route_selected"):
        return "route_not_confirmed"
    if (
        trace.get("status") == "succeeded"
        and live.get("status_code") == 200
        and live.get("marker_matched")
        and live.get("finish_reason") == "stop"
        and not live.get("transport_error")
        and not live.get("response_error")
    ):
        return "inference_verified"
    return "inference_not_verified"


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


async def run(args) -> dict:
    settings = Settings(args.defaults, args.settings)
    registry = Registry(args.registry)
    if not args.live_short:
        trace = read_trace(args.audit_db, args.request_id)
        return build_report(
            trace,
            identifier=args.request_id,
            settings=settings,
            registry=registry,
        )

    key = os.environ.get(args.key_env, "")
    if not key:
        raise ValueError("the selected key environment variable is empty")
    live = await live_short(
        args.base_url,
        key,
        model=args.model,
        timeout=args.timeout,
    )
    lookup_id = live.get("request_id") or live["client_request_id"]
    trace = None
    for _ in range(10):
        trace = read_trace(args.audit_db, lookup_id)
        if trace is not None and trace.get("status") in TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.5)
    report = build_report(
        trace,
        identifier=lookup_id,
        settings=settings,
        registry=registry,
    )
    report["live"] = live
    report["live"]["verdict"] = live_verdict(live, trace)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-db",
        type=Path,
        default=Path(
            os.environ.get(
                "AI_ROUTER_ROUTE_TRACE_DB_PATH",
                "/data/audit/route-traces.sqlite3",
            )
        ),
    )
    parser.add_argument("--request-id")
    parser.add_argument("--live-short", action="store_true")
    parser.add_argument("--base-url")
    parser.add_argument("--model", default="siyuan/auto")
    parser.add_argument("--key-env", default="AI_ROUTER_1PANEL_API_KEY")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument(
        "--defaults",
        type=Path,
        default=ROOT / "config/defaults.yaml",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(
            os.environ.get(
                "AI_ROUTER_RUNTIME_SETTINGS_PATH",
                "/data/settings.yaml",
            )
        ),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(
            os.environ.get(
                "AI_ROUTER_REGISTRY_PATH",
                str(ROOT / "config/registry.yaml"),
            )
        ),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.live_short:
        if args.request_id:
            parser.error("--request-id and --live-short are mutually exclusive")
        if not args.base_url:
            parser.error("--live-short requires --base-url")
    elif not args.request_id:
        parser.error("--request-id is required unless --live-short is used")
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    if args.report and args.report.exists():
        parser.error("report already exists; choose a new path")
    try:
        report = asyncio.run(run(args))
        if args.report:
            save_report(args.report, report)
            print(str(args.report))
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(
            f"诊断未完成：{type(exc).__name__}（检查参数、配置和读取权限）",
            file=sys.stderr,
        )
        return 2
    if args.live_short:
        return int(report["live"]["verdict"] != "inference_verified")
    return int(not report["found"])


if __name__ == "__main__":
    raise SystemExit(main())
