#!/usr/bin/env python3
"""Explicitly authorized, serial protocol checks with complete local evidence."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--key-env")
    parser.add_argument("--skip-json-object", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("live requests require explicit --execute")
    os.umask(0o077)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    headers = {}
    if args.key_env:
        headers["Authorization"] = "Bearer " + os.environ[args.key_env]
    reports = []
    with httpx.Client(
        base_url=args.base_url.rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(180, connect=5),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        def check(name, path, body, verify, stream=False):
            started = time.monotonic()
            record = {"case": name, "request": body, "passed": False}
            try:
                response = client.post(path, json=body)
                record.update({
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "response": response.text,
                })
                response.raise_for_status()
                if stream:
                    events = []
                    done = False
                    for line in response.text.splitlines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                        else:
                            events.append(json.loads(data))
                    value = {"events": events, "done": done}
                else:
                    value = response.json()
                record["passed"] = bool(verify(value))
                return value
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                return None
            finally:
                record["seconds"] = time.monotonic() - started
                with (output / f"{name}.json").open("x", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                reports.append({
                    key: value for key, value in record.items()
                    if key not in {"request", "response", "headers"}
                })
                print(json.dumps(reports[-1]), flush=True)

        common = {
            "model": args.model,
            "temperature": 0,
            "max_tokens": 128,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        messages = [{"role": "user", "content": "Compute 19 + 23. Reply with only the integer."}]
        check(
            "chat", "chat/completions",
            {**common, "messages": messages},
            lambda value: value["choices"][0]["message"]["content"].strip() == "42"
            and value["choices"][0]["finish_reason"] == "stop"
            and 0 < value["usage"]["completion_tokens"] <= 128,
        )
        check(
            "chat-stream", "chat/completions",
            {**common, "messages": messages, "stream": True, "stream_options": {"include_usage": True}},
            lambda value: value["done"]
            and "".join(
                choice.get("delta", {}).get("content") or ""
                for event in value["events"] for choice in event.get("choices", [])
            ).strip() == "42"
            and any(
                choice.get("finish_reason") == "stop"
                for event in value["events"] for choice in event.get("choices", [])
            ),
            stream=True,
        )
        expected = {"ok": True, "value": 17}
        for mode in ("json_object", "json_schema"):
            if mode == "json_object" and args.skip_json_object:
                continue
            response_format = {"type": mode}
            if mode == "json_schema":
                response_format["json_schema"] = {
                    "name": "result",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                            "value": {"type": "integer"},
                        },
                        "required": ["ok", "value"],
                        "additionalProperties": False,
                    },
                }
            check(
                mode, "chat/completions",
                {
                    **common,
                    "messages": [{"role": "user", "content": 'Return JSON with ok=true and value=17.'}],
                    "response_format": response_format,
                },
                lambda value: json.loads(value["choices"][0]["message"]["content"]) == expected
                and value["choices"][0]["finish_reason"] == "stop",
            )
        tool = {
            "type": "function",
            "function": {
                "name": "lookup_marker",
                "description": "Return the private marker for the supplied location.",
                "parameters": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                    "additionalProperties": False,
                },
            },
        }
        tool_messages = [{
            "role": "user",
            "content": "Use lookup_marker to find the marker for harbor. Do not guess the marker.",
        }]
        def correct_tool(value):
            choice = value["choices"][0]
            calls = choice["message"].get("tool_calls", [])
            return (
                choice["finish_reason"] == "tool_calls"
                and len(calls) == 1
                and bool(calls[0].get("id"))
                and calls[0]["function"]["name"] == "lookup_marker"
                and json.loads(calls[0]["function"]["arguments"]) == {"location": "harbor"}
            )
        response = None
        for mode in ("auto", "required", "function"):
            response = check(
                "tool-" + mode, "chat/completions",
                {
                    **common,
                    "messages": tool_messages,
                    "tools": [tool],
                    "tool_choice": (
                        {"type": "function", "function": {"name": "lookup_marker"}}
                        if mode == "function" else mode
                    ),
                },
                correct_tool,
            )
        check(
            "tool-none", "chat/completions",
            {**common, "messages": messages, "tools": [tool], "tool_choice": "none"},
            lambda value: not value["choices"][0]["message"].get("tool_calls")
            and value["choices"][0]["message"]["content"].strip() == "42",
        )
        if response and correct_tool(response):
            assistant = copy.deepcopy(response["choices"][0]["message"])
            call = assistant["tool_calls"][0]
            check(
                "tool-replay", "chat/completions",
                {
                    **common,
                    "messages": tool_messages + [
                        assistant,
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": '{"marker":"ACCEPT_739216"}',
                        },
                        {"role": "user", "content": "Reply only with the marker returned by the tool."},
                    ],
                    "tools": [tool],
                    "tool_choice": "none",
                },
                lambda value: value["choices"][0]["message"]["content"].strip() == "ACCEPT_739216"
                and value["choices"][0]["finish_reason"] == "stop",
            )
        check(
            "tool-parallel", "chat/completions",
            {
                **common,
                "max_tokens": 256,
                "messages": [{
                    "role": "user",
                    "content": "Call lookup_marker twice in parallel: once for harbor and once for meadow.",
                }],
                "tools": [tool],
                "tool_choice": "required",
                "parallel_tool_calls": True,
            },
            lambda value: len(value["choices"][0]["message"].get("tool_calls", [])) == 2
            and {
                json.loads(call["function"]["arguments"])["location"]
                for call in value["choices"][0]["message"]["tool_calls"]
                if call["function"]["name"] == "lookup_marker"
            } == {"harbor", "meadow"}
            and value["choices"][0]["finish_reason"] == "tool_calls",
        )
        responses_body = {
            "model": args.model,
            "input": messages,
            "max_output_tokens": 128,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        def responses_text(value):
            return "".join(
                part.get("text", "")
                for item in value.get("output", [])
                if item.get("type") == "message"
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ).strip()
        check(
            "responses", "responses", responses_body,
            lambda value: value.get("status") == "completed" and responses_text(value) == "42",
        )
        check(
            "responses-stream", "responses", {**responses_body, "stream": True},
            lambda value: any(
                event.get("type") == "response.completed"
                and event.get("response", {}).get("status") == "completed"
                and responses_text(event["response"]) == "42"
                for event in value["events"]
            ),
            stream=True,
        )
        responses_tool = {"type": "function", **tool["function"]}
        responses_tool_body = {
            **responses_body,
            "input": tool_messages,
            "tools": [responses_tool],
            "tool_choice": {"type": "function", "name": "lookup_marker"},
            "max_output_tokens": 256,
        }
        def correct_responses_tool(value):
            calls = [item for item in value.get("output", []) if item.get("type") == "function_call"]
            return (
                value.get("status") == "completed"
                and len(calls) == 1 and bool(calls[0].get("call_id"))
                and calls[0]["name"] == "lookup_marker"
                and json.loads(calls[0]["arguments"]) == {"location": "harbor"}
            )
        responses_call = check(
            "responses-tool", "responses", responses_tool_body, correct_responses_tool,
        )
        if responses_call and correct_responses_tool(responses_call):
            call = next(
                item for item in responses_call["output"] if item.get("type") == "function_call"
            )
            check(
                "responses-tool-replay", "responses",
                {
                    **responses_body,
                    "tools": [responses_tool],
                    "tool_choice": "none",
                    "input": tool_messages + responses_call["output"] + [
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": '{"marker":"ACCEPT_583194"}',
                        },
                        {"role": "user", "content": "Reply only with the marker returned by the tool."},
                    ],
                },
                lambda value: value.get("status") == "completed"
                and responses_text(value) == "ACCEPT_583194",
            )
    with (output / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(reports, handle, ensure_ascii=False, indent=2)
    return 0 if all(item["passed"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
