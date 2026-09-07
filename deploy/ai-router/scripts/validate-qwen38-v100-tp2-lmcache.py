#!/usr/bin/env python3
"""Validate Qwen3.8 TP2 LMCache correctness before and after a vLLM restart."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from uuid import uuid4

import httpx
from transformers import AutoTokenizer


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_router.training_archive import TrainingArchive  # noqa: E402


TARGETS = (2000, 20000, 40000, 45000, 48000, 50000)
MARKERS = tuple(
    f"LMCACHE_{index}_{target}_739216"
    for index, target in enumerate(TARGETS)
)
VLLM_METRICS = {
    "external_queries": "external_prefix_cache_queries_total",
    "external_hits": "external_prefix_cache_hits_total",
    "external_transfer": "prompt_tokens_by_source_total",
    "draft_tokens": "spec_decode_num_draft_tokens_total",
    "accepted_tokens": "spec_decode_num_accepted_tokens_total",
}
LMCACHE_METRICS = {
    "lookup_requested": "lmcache_mp_lookup_requested_tokens_total",
    "lookup_hits": "lmcache_mp_lookup_hit_tokens_total",
    "l1_reads": "lmcache_mp_l1_read_chunks_total",
    "l1_writes": "lmcache_mp_l1_write_chunks_total",
}


def metric(text: str, name: str, *, source: str | None = None) -> float:
    pattern = re.compile(
        rf"^{re.escape(name)}(?:\{{([^}}]*)\}})?\s+([0-9.eE+-]+)$",
        re.MULTILINE,
    )
    total = 0.0
    for labels, value in pattern.findall(text):
        if source and f'source="{source}"' not in labels:
            continue
        total += float(value)
    return total


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Qwen3.8 TP2 LMCache Acceptance",
        "",
        f"- Phase: `{report['phase']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Overall: **{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        "| Case | Result | Prompt | Completion | TTFT | Decode |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in report["records"]:
        lines.append(
            "| {case} | {passed} | {prompt} | {completion} | {ttft} | "
            "{decode} |".format(
                case=str(item.get("case", "-")).replace("|", "\\|"),
                passed="PASS" if item.get("passed") else "FAIL",
                prompt=item.get("prompt_tokens", "-"),
                completion=item.get("completion_tokens", "-"),
                ttft=(
                    f"{item['ttft_seconds']:.3f}s"
                    if item.get("ttft_seconds") is not None
                    else "-"
                ),
                decode=(
                    f"{item['decode_tokens_per_second']:.3f} tok/s"
                    if item.get("decode_tokens_per_second") is not None
                    else "-"
                ),
            )
        )
    lines.extend(
        [
            "",
            "缓存命中只作为性能证据；任意标记串线、异常重复、乱码或工具历史缺失都会使验收失败。",
            "",
        ]
    )
    return "\n".join(lines)


class Acceptance:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = args.base_url.rstrip("/")
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
        self.lmcache_url = args.lmcache_url.rstrip("/")
        self.headers = (
            {"Authorization": f"Bearer {args.api_key}"}
            if args.api_key
            else {}
        )
        self.client = httpx.Client(
            headers=self.headers,
            timeout=httpx.Timeout(3600, connect=10),
            trust_env=False,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            args.model_path,
            trust_remote_code=True,
        )
        self.records: list[dict[str, Any]] = []

    def close(self) -> None:
        self.client.close()

    def filler(self, target: int, marker: str) -> str:
        prefix = (
            f"唯一标记：{marker}。后续只能复述这个标记，不能复述其他标记。\n"
        )
        unit = (
            "这是用于前缀缓存验收的稳定事实段落，不包含新的任务指令。"
            "事实编号保持不变，读取后继续等待用户问题。\n"
        )
        value = prefix
        while len(
            self.tokenizer.encode(value, add_special_tokens=False)
        ) < target:
            value += unit
        return value

    def decode_filler(self, target: int) -> str:
        unit = (
            "系统审计应覆盖身份认证、权限边界、输入校验、状态一致性、"
            "故障恢复、日志完整性、资源限制和回滚证据。"
            "每项结论都应说明检查对象、观测结果与后续动作。\n"
        )
        value = "以下是稳定的审计背景材料，不包含输出格式指令。\n"
        while len(
            self.tokenizer.encode(value, add_special_tokens=False)
        ) < target:
            value += unit
        return value

    def messages(self, target: int, marker: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.filler(target, marker)},
            {
                "role": "user",
                "content": "只回答系统上下文中的唯一标记，不要增加解释。",
            },
        ]

    def snapshot(self) -> dict[str, Any]:
        vllm = self.client.get(self.root_url + "/metrics")
        vllm.raise_for_status()
        lm_metrics = self.client.get(self.lmcache_url + "/metrics")
        lm_metrics.raise_for_status()
        status = self.client.get(self.lmcache_url + "/status")
        status.raise_for_status()
        status_value = status.json()
        values = {
            key: metric(
                vllm.text,
                f"vllm:{metric_name}",
                source=(
                    "external_kv_transfer"
                    if key == "external_transfer"
                    else None
                ),
            )
            for key, metric_name in VLLM_METRICS.items()
        }
        values.update(
            {
                key: metric(lm_metrics.text, metric_name)
                for key, metric_name in LMCACHE_METRICS.items()
            }
        )
        values["lmcache_healthy"] = (
            status_value.get("is_healthy") is True
        )
        values["lmcache_chunk_size"] = int(
            status_value.get("chunk_size", 0)
        )
        values["lmcache_process_start"] = metric(
            lm_metrics.text,
            "process_start_time_seconds",
        )
        values["lmcache_generation"] = hashlib.sha256(
            json.dumps(
                {
                    "process_start": values["lmcache_process_start"],
                    "engine_type": status_value.get("engine_type"),
                    "chunk_size": values["lmcache_chunk_size"],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
        return values

    def request(
        self,
        *,
        name: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        ignore_eos: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        first_token_at: float | None = None
        usage: dict[str, Any] = {}
        parts: list[str] = []
        done = False
        request_id = ""
        with self.client.stream(
            "POST",
            self.api_url + "/chat/completions",
            json={
                "model": self.args.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
                "ignore_eos": ignore_eos,
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
                raw = line[5:].strip()
                if raw == "[DONE]":
                    done = True
                    continue
                event = json.loads(raw)
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    part = str((choice.get("delta") or {}).get("content") or "")
                    if part:
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                        parts.append(part)
        finished = time.monotonic()
        text = "".join(parts).strip()
        completion = int(usage.get("completion_tokens") or 0)
        decode_seconds = (
            finished - first_token_at
            if first_token_at is not None
            else None
        )
        result = {
            "case": name,
            "request_id": request_id,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": completion,
            "ttft_seconds": (
                first_token_at - started
                if first_token_at is not None
                else None
            ),
            "wall_seconds": finished - started,
            "decode_tokens_per_second": (
                max(0, completion - 1) / decode_seconds
                if decode_seconds and decode_seconds > 0
                else None
            ),
            "output": text[:300],
            "_output_text": text,
            "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "done": done,
            "passed": done,
        }
        print(
            json.dumps(
                {
                    key: value
                    for key, value in result.items()
                    if not key.startswith("_")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    def marker_request(
        self,
        index: int,
        *,
        name: str,
    ) -> dict[str, Any]:
        marker = MARKERS[index]
        result = self.request(
            name=name,
            messages=self.messages(TARGETS[index], marker),
            max_tokens=32,
        )
        full_output = result.pop("_output_text")
        foreign = [
            item for item in MARKERS
            if item != marker and item in full_output
        ]
        result["marker"] = marker
        result["foreign_markers"] = foreign
        result["passed"] = (
            result["passed"]
            and marker in full_output
            and not foreign
        )
        return result

    def prime(self) -> dict[str, Any]:
        cold_ttft: dict[str, float] = {}
        for index, target in enumerate(TARGETS):
            before = self.snapshot()
            cold = self.marker_request(
                index,
                name=f"prefix-{target}-cold",
            )
            middle = self.snapshot()
            warm = self.marker_request(
                index,
                name=f"prefix-{target}-warm",
            )
            after = self.snapshot()
            cold_ttft[str(target)] = float(
                cold["ttft_seconds"] or cold["wall_seconds"]
            )
            warm_ttft = float(
                warm["ttft_seconds"] or warm["wall_seconds"]
            )
            cold["passed"] = cold["passed"] and middle["l1_writes"] > before[
                "l1_writes"
            ]
            warm["passed"] = (
                warm["passed"]
                and (
                    target < 40000
                    or warm_ttft <= cold_ttft[str(target)] * 0.5
                )
            )
            warm["metric_delta"] = {
                key: after[key] - middle[key]
                for key in (
                    "external_queries",
                    "external_hits",
                    "external_transfer",
                    "lookup_requested",
                    "lookup_hits",
                    "l1_reads",
                )
            }
            self.records.extend((cold, warm))
        self.concurrent((3, 4, 5), "prime-concurrent")
        self.decode(512)
        self.decode(1024)
        return {
            "version": 1,
            "targets": list(TARGETS),
            "markers": list(MARKERS),
            "cold_ttft_seconds": cold_ttft,
            "lmcache_generation": self.snapshot()["lmcache_generation"],
        }

    def resume(
        self,
        state: dict[str, Any],
        startup: dict[str, Any],
    ) -> None:
        if state.get("markers") != list(MARKERS):
            raise ValueError("prime state markers do not match this validator")
        expected_generation = str(state.get("lmcache_generation", ""))
        actual_generation = str(startup.get("lmcache_generation", ""))
        self.records.append(
            {
                "case": "lmcache-survived-vllm-restart",
                "expected_generation": expected_generation,
                "actual_generation": actual_generation,
                "passed": (
                    bool(expected_generation)
                    and actual_generation == expected_generation
                ),
            }
        )
        cold_ttft = state["cold_ttft_seconds"]
        for index, target in enumerate(TARGETS):
            before = self.snapshot()
            result = self.marker_request(
                index,
                name=f"prefix-{target}-after-restart",
            )
            after = self.snapshot()
            result["metric_delta"] = {
                key: after[key] - before[key]
                for key in (
                    "external_queries",
                    "external_hits",
                    "external_transfer",
                    "lookup_requested",
                    "lookup_hits",
                    "l1_reads",
                )
            }
            ttft = float(
                result["ttft_seconds"] or result["wall_seconds"]
            )
            external_hit = (
                result["metric_delta"]["external_hits"] > 0
                or result["metric_delta"]["external_transfer"] > 0
                or result["metric_delta"]["lookup_hits"] > 0
            )
            result["passed"] = (
                result["passed"]
                and external_hit
                and (
                    target < 40000
                    or ttft <= float(cold_ttft[str(target)]) * 0.5
                )
            )
            self.records.append(result)
        self.concurrent((3, 4, 5), "resume-concurrent")

    def concurrent(self, indexes: tuple[int, ...], name: str) -> None:
        before = self.snapshot()
        results = []
        with ThreadPoolExecutor(max_workers=len(indexes)) as executor:
            futures = {
                executor.submit(
                    self.marker_request,
                    index,
                    name=f"{name}-{index}",
                ): index
                for index in indexes
            }
            for future in as_completed(futures):
                results.append(future.result())
        after = self.snapshot()
        record = {
            "case": name,
            "results": sorted(results, key=lambda item: item["marker"]),
            "metric_delta": {
                key: after[key] - before[key]
                for key in (
                    "external_hits",
                    "external_transfer",
                    "lookup_hits",
                    "l1_reads",
                )
            },
            "passed": (
                len(results) == len(indexes)
                and all(item["passed"] for item in results)
            ),
        }
        self.records.append(record)

    def decode(self, output_tokens: int) -> None:
        before = self.snapshot()
        result = self.request(
            name=f"mtp2-1500x{output_tokens}",
            messages=[
                {
                    "role": "system",
                    "content": self.decode_filler(1500),
                },
                {
                    "role": "user",
                    "content": (
                        "写一份连续、具体的系统审计清单，每项使用不同措辞，"
                        "不要提前结束。"
                    ),
                },
            ],
            max_tokens=output_tokens,
            ignore_eos=True,
        )
        after = self.snapshot()
        drafts = after["draft_tokens"] - before["draft_tokens"]
        accepted = after["accepted_tokens"] - before["accepted_tokens"]
        full_output = result.pop("_output_text")
        token_ids = self.tokenizer.encode(
            full_output,
            add_special_tokens=False,
        )
        longest_run = 0
        current_run = 0
        previous = None
        for token in token_ids:
            current_run = current_run + 1 if token == previous else 1
            longest_run = max(longest_run, current_run)
            previous = token
        result["mtp_draft_tokens"] = drafts
        result["mtp_accepted_tokens"] = accepted
        result["mtp_acceptance_rate"] = (
            accepted / drafts if drafts > 0 else 0
        )
        result["longest_identical_token_run"] = longest_run
        result["passed"] = (
            result["passed"]
            and result["completion_tokens"] >= output_tokens
            and drafts > 0
            and accepted > 0
            and longest_run <= 32
            and "\ufffd" not in full_output
            and "??????????" not in full_output
        )
        self.records.append(result)

    def workbuddy(self) -> None:
        if not self.args.router_key:
            if self.args.require_workbuddy:
                self.records.append(
                    {
                        "case": "workbuddy-history",
                        "passed": False,
                        "error": "router key is required",
                    }
                )
            return
        conversation_id = f"lmcache-workbuddy-{uuid4().hex}"
        first_marker = "WORKBUDDY_CONTEXT_739216"
        tool_marker = "WORKBUDDY_TOOL_583194"
        call_id = "call_lmcache_workbuddy"
        messages = [
            {
                "role": "system",
                "content": "完整读取所有历史消息，并严格回答最后一个用户问题。",
            },
            {
                "role": "user",
                "content": f"第一步标记是 {first_marker}，请记住。",
            },
            {"role": "assistant", "content": "已记录第一步标记。"},
            {
                "role": "user",
                "content": "调用 lookup_marker 获取工具标记。",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "lookup_marker",
                            "arguments": '{"key":"workbuddy"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"marker": tool_marker}),
            },
            {
                "role": "user",
                "content": "继续。只回答第一步标记和工具标记，用空格分隔。",
            },
        ]
        response = httpx.post(
            self.args.router_url.rstrip("/") + "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.args.router_key}",
                "X-1Panel-Conversation-ID": conversation_id,
            },
            json={
                "model": self.args.router_model,
                "messages": messages,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup_marker",
                            "description": "Return a marker",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                },
                                "required": ["key"],
                            },
                        },
                    }
                ],
                "tool_choice": "none",
                "temperature": 0,
                "max_tokens": 64,
            },
            timeout=3600,
            trust_env=False,
        )
        text = ""
        if response.is_success:
            text = str(
                response.json()["choices"][0]["message"].get("content") or ""
            )
        request_id = response.headers.get(
            "x-1panel-route-request-id",
            response.headers.get("x-request-id", ""),
        )
        database_ok = self._verify_training_record(request_id, messages)
        self.records.append(
            {
                "case": "workbuddy-history",
                "request_id": request_id,
                "conversation_id": conversation_id,
                "output": text[:300],
                "database_complete": database_ok,
                "passed": (
                    response.is_success
                    and first_marker in text
                    and tool_marker in text
                    and database_ok
                ),
            }
        )

    def _verify_training_record(
        self,
        request_id: str,
        messages: list[dict[str, Any]],
    ) -> bool:
        if (
            not request_id
            or not self.args.training_db
            or not self.args.training_key
        ):
            return not self.args.require_workbuddy
        export_path = self.args.output / "training-export.jsonl"
        archive = TrainingArchive(
            str(self.args.training_db),
            str(self.args.training_key),
        )
        asyncio.run(archive.export_jsonl(str(export_path)))
        for line in export_path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            request = value.get("request", {})
            if request.get("request_id") != request_id:
                continue
            received = request.get("received_body", {}).get("messages")
            effective = request.get("effective_body", {}).get("messages")
            return received == messages and effective == messages
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("prime", "resume"),
        required=True,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18107/v1")
    parser.add_argument("--lmcache-url", default="http://127.0.0.1:18084")
    parser.add_argument("--model", default="siyuan/qwen38-v100-196k")
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path(
            "/home/ai/model-sources/modelscope/QUASAR-QAT/"
            "Qwen3.8-27B-QUASAR-NVFP4"
        ),
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--router-url", default="http://127.0.0.1:4000")
    parser.add_argument("--router-model", default="siyuan/qwen38-v100-196k")
    parser.add_argument("--router-key", default="")
    parser.add_argument("--router-key-file", type=Path)
    parser.add_argument(
        "--training-db",
        type=Path,
        default=Path("/opt/1panel/ai-router-training/conversations.sqlite3"),
    )
    parser.add_argument(
        "--training-key",
        type=Path,
        default=Path("/opt/1panel/ai-router-training/training.key"),
    )
    parser.add_argument("--require-workbuddy", action="store_true")
    args = parser.parse_args()
    if args.api_key_file:
        args.api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if args.router_key_file:
        args.router_key = args.router_key_file.read_text(
            encoding="utf-8"
        ).strip()
    args.output.mkdir(parents=True, exist_ok=True)
    state_file = args.state_file or args.output / "cache-state.json"

    acceptance = Acceptance(args)
    try:
        startup = acceptance.snapshot()
        acceptance.records.append(
            {
                "case": "lmcache-startup",
                "passed": (
                    startup["lmcache_healthy"]
                    and startup["lmcache_chunk_size"] == 1600
                ),
                "snapshot": startup,
            }
        )
        if args.phase == "prime":
            atomic_json(state_file, acceptance.prime())
        if args.phase == "resume":
            state = json.loads(state_file.read_text(encoding="utf-8"))
            acceptance.resume(state, startup)
        acceptance.workbuddy()
        report = {
            "version": 1,
            "phase": args.phase,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "records": acceptance.records,
            "passed": all(
                bool(item.get("passed"))
                for item in acceptance.records
            ),
        }
        atomic_json(args.output / f"report-{args.phase}.json", report)
        (args.output / f"report-{args.phase}.md").write_text(
            render_markdown(report),
            encoding="utf-8",
        )
        return 0 if report["passed"] else 1
    finally:
        acceptance.close()


if __name__ == "__main__":
    raise SystemExit(main())
