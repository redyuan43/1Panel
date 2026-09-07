from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prewarm-qwen36-prefix.py"
SPEC = importlib.util.spec_from_file_location(
    "prewarm_qwen36_prefix",
    SCRIPT,
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def template() -> dict:
    return {
        "api_kind": "chat",
        "requested_model": "siyuan/qwen36-shared",
        "request": {
            "messages": [
                {"role": "system", "content": "fixed"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": "auto",
        },
    }


def test_request_body_preserves_fingerprint_fields() -> None:
    value = template()
    original = copy.deepcopy(value)

    body = MODULE._request_body(value, "dynamic")

    assert value == original
    assert body["tools"] == value["request"]["tools"]
    assert body["tool_choice"] == "auto"
    assert "chat_template_kwargs" not in body
    assert body["messages"][-1] == {
        "role": "user",
        "content": "dynamic",
    }


def test_request_body_preserves_existing_template_kwargs() -> None:
    value = template()
    value["request"]["chat_template_kwargs"] = {
        "enable_thinking": True,
    }

    body = MODULE._request_body(value, "dynamic")

    assert body["chat_template_kwargs"] == {
        "enable_thinking": True,
    }


def test_pass_requires_two_distinct_warm_replicas() -> None:
    cold = [
        {
            "status_code": 200,
            "request_id": "cold-a",
            "worker": "worker-a",
        },
        {
            "status_code": 200,
            "request_id": "cold-b",
            "worker": "worker-b",
        },
    ]
    warm = [
        {
            "status_code": 200,
            "request_id": "warm-a",
            "worker": "worker-a",
            "affinity": "prefix-hit",
            "prompt_tokens": 1000,
            "cached_tokens": 970,
        },
        {
            "status_code": 200,
            "request_id": "warm-b",
            "worker": "worker-b",
            "affinity": "prefix-hit",
            "prompt_tokens": 1000,
            "cached_tokens": 960,
        },
    ]

    assert MODULE._passed(cold, warm, 2)
    warm[1]["worker"] = "worker-a"
    assert not MODULE._passed(cold, warm, 2)
    warm[1]["worker"] = "worker-b"
    warm[1]["cached_tokens"] = 940
    assert not MODULE._passed(cold, warm, 2)


def test_run_batch_preserves_success_when_peer_fails(monkeypatch) -> None:
    def fake_prewarm(_base_url, _api_key, _template, index):
        if index == 1:
            raise RuntimeError("peer failed")
        return {
            "request_id": f"request-{index}",
            "worker": f"worker-{index}",
            "status_code": 200,
        }

    monkeypatch.setattr(MODULE, "_prewarm_one", fake_prewarm)

    records = MODULE._run_batch(
        "http://127.0.0.1:4000/v1",
        "secret",
        template(),
        2,
        offset=0,
    )

    assert records[0]["request_id"] == "request-0"
    assert records[1]["probe_index"] == 1
    assert records[1]["status_code"] is None
    assert records[1]["error"] == "RuntimeError: peer failed"
