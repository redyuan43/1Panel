#!/usr/bin/env python3
"""Run a generated black-box corpus serially; semantic verdicts require review."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import time
import urllib.error
import urllib.request
import uuid


PUBLIC_MODEL = "siyuan/auto"
PUBLIC_ROUTER_HEADERS = {
    "x-1panel-public-model",
    "x-1panel-conversation-id",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_cases(corpus: dict) -> list[dict]:
    cases = corpus["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise ValueError("corpus must contain 1-50 cases")
    seen = set()
    for case in cases:
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("case IDs must be unique nonempty strings")
        seen.add(case_id)
        if case["expected"] not in {"protect", "allow"}:
            raise ValueError("expected must be protect or allow")
        turns = case["turns"]
        if not isinstance(turns, list) or not 1 <= len(turns) <= 3:
            raise ValueError("each case must contain 1-3 turns")
        if any(not isinstance(t, str) or not t or len(t) > 4000 for t in turns):
            raise ValueError("turns must be nonempty strings up to 4000 characters")
    return cases


def parse_reply(raw: bytes, streaming: bool) -> dict:
    if not streaming:
        payload = json.loads(raw)
        message = payload.get("choices", [{}])[0].get("message", {})
        return {
            "message": message,
            "models": [payload["model"]] if "model" in payload else [],
            "complete": bool(payload.get("choices")),
            "payload": payload,
        }
    events = []
    models = set()
    content = []
    reasoning = []
    done = False
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        event = json.loads(data)
        events.append(event)
        if event.get("model"):
            models.add(event["model"])
        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
    return {
        "message": {"role": "assistant", "content": "".join(content)},
        "reasoning_content": "".join(reasoning),
        "models": sorted(models),
        "complete": done and not any("error" in event for event in events),
        "payload": events,
    }


def metadata_findings(response: dict) -> list[str]:
    findings = []
    headers = response.get("headers", {})
    for key in ("server", "x-powered-by"):
        if headers.get(key) and headers[key].lower() not in {"siyuan", PUBLIC_MODEL}:
            findings.append(f"software_header:{key}")
    for key in headers:
        if key.startswith("x-1panel-") and key not in PUBLIC_ROUTER_HEADERS:
            findings.append(f"internal_header:{key}")
    payload = response.get("payload")
    events = payload if isinstance(payload, list) else [payload]
    for field in ("system_fingerprint", "timings"):
        if any(isinstance(e, dict) and e.get(field) for e in events):
            findings.append(f"backend_metadata:{field}")
    if any(model != PUBLIC_MODEL for model in response.get("models", [])):
        findings.append("nonpublic_model")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--seed", default=uuid.uuid4().hex)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    corpus = json.loads(Path(args.corpus).read_text())
    cases = validate_cases(corpus)
    random.Random(args.seed).shuffle(cases)
    if not args.execute:
        print(f"Validated {len(cases)} cases; --execute permits live requests.")
        return 0
    key = os.environ["AI_ROUTER_PUBLIC_API_KEY"]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwriting prior evidence; reports can contain unredacted failures.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
    )
    run_id = uuid.uuid4().hex
    report = {
        "run_id": run_id,
        "seed": args.seed,
        "generator": corpus.get("generator"),
        "cases": [],
        "semantic_verdict": "requires_human_review",
    }

    def request(path, body=None, tag="catalog"):
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Request-ID": f"privacy-{run_id}-{tag}",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            args.base_url.rstrip("/") + path, data=data, headers=headers,
        )
        started = time.monotonic()
        try:
            response = opener.open(req, timeout=args.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return {
                "status": response.status,
                "headers": {k.lower(): v for k, v in response.headers.items()},
                "raw": response.read(),
                "elapsed": round(time.monotonic() - started, 3),
            }

    with os.fdopen(fd, "w") as stream:
        def checkpoint():
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.truncate()
            stream.flush()

        catalog = request("/models")
        payload = json.loads(catalog.pop("raw"))
        report["catalog"] = {**catalog, "payload": payload}
        report["catalog"]["metadata_findings"] = metadata_findings(report["catalog"])
        if catalog["status"] != 200 or [
            m.get("id") for m in payload.get("data", [])
        ] != [PUBLIC_MODEL]:
            checkpoint()
            raise RuntimeError("public catalog check failed; no inference sent")
        for index, case in enumerate(cases):
            result = {**case, "results": []}
            report["cases"].append(result)
            # Unique harness prefix isolates independent probes, not their turns.
            messages = [{
                "role": "system",
                "content": f"Privacy acceptance {run_id}/{case['id']}. Be concise.",
            }]
            for turn, prompt in enumerate(case["turns"]):
                messages.append({"role": "user", "content": prompt})
                streaming = index % 2 == 1
                try:
                    response = request("/chat/completions", {
                        "model": "auto",
                        "messages": messages,
                        "stream": streaming,
                        "temperature": 0,
                        "max_completion_tokens": 256,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }, tag=f"{case['id']}-{turn}")
                    raw = response.pop("raw")
                    parsed = parse_reply(
                        raw, streaming and response["status"] == 200,
                    )
                    response.update(parsed)
                    response["streaming"] = streaming
                    response["request_id"] = response["headers"].get("x-request-id")
                    response["metadata_findings"] = metadata_findings(response)
                    response["unexpected_router_headers"] = [
                        h for h in response["headers"]
                        if h.startswith("x-1panel-") and h not in PUBLIC_ROUTER_HEADERS
                    ]
                    result["results"].append(response)
                    checkpoint()
                    print(
                        f"{case['id']} turn={turn + 1} status={response['status']} "
                        f"seconds={response['elapsed']} "
                        f"request_id={response['request_id']}", flush=True,
                    )
                    if response["status"] != 200 or not parsed["complete"]:
                        break
                    messages.append(parsed["message"])
                except Exception as exc:
                    # Exception strings can contain URLs or credential-bearing data.
                    result["results"].append({"transport_error": type(exc).__name__})
                    checkpoint()
                    print(f"{case['id']}: {type(exc).__name__}", flush=True)
                    break
        checkpoint()
    responses = [report["catalog"]] + [
        r for case in report["cases"] for r in case["results"]
    ]
    # Zero means execution completed, never a semantic privacy guarantee.
    return 2 if any(
        r.get("metadata_findings") or r.get("transport_error")
        or r.get("status") != 200
        or r.get("complete") is False
        for r in responses
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
