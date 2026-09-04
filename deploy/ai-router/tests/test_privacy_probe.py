import json
from pathlib import Path
import runpy
import urllib.request

import pytest


PROBE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "validate-public-privacy.py")
)


def test_generated_corpus_requires_unique_ids():
    case = {"id": "probe", "expected": "protect", "turns": ["Question"]}
    with pytest.raises(ValueError, match="unique"):
        PROBE["validate_cases"]({"cases": [case, case]})


@pytest.mark.parametrize("turns", [[], [""], ["x" * 4001], ["x"] * 4])
def test_generated_corpus_bounds(turns):
    with pytest.raises(ValueError):
        PROBE["validate_cases"]({
            "cases": [{"id": "probe", "expected": "protect", "turns": turns}],
        })


def test_privacy_sse_preserves_reasoning_and_requires_done():
    event = {
        "model": "siyuan/auto",
        "choices": [{"delta": {"content": "hello", "reasoning_content": "check"}}],
    }
    raw = ("data: " + json.dumps(event) + "\n\n").encode()
    parsed = PROBE["parse_reply"](raw, True)
    assert parsed["message"]["content"] == "hello"
    assert parsed["reasoning_content"] == "check"
    assert not parsed["complete"]
    assert PROBE["parse_reply"](raw + b"data: [DONE]\n\n", True)["complete"]


def test_privacy_sse_error_is_not_success():
    raw = b'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n'
    assert not PROBE["parse_reply"](raw, True)["complete"]


def test_privacy_json_preserves_full_payload():
    payload = {
        "model": "siyuan/auto",
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    }
    parsed = PROBE["parse_reply"](json.dumps(payload).encode(), False)
    assert parsed["payload"] == payload
    assert parsed["models"] == ["siyuan/auto"]


def test_privacy_probe_does_not_redirect_credentials():
    request = urllib.request.Request(
        "http://localhost/v1/models", headers={"Authorization": "Bearer test"},
    )
    assert PROBE["NoRedirect"]().redirect_request(
        request, None, 302, "Found", {}, "https://example.com",
    ) is None


def test_privacy_probe_checks_backend_metadata_in_stream():
    findings = PROBE["metadata_findings"]({
        "headers": {"server": "uvicorn", "x-1panel-node": "private"},
        "models": ["siyuan/auto"],
        "payload": [{"system_fingerprint": "private-build"}, {"timings": {"n": 1}}],
    })
    assert set(findings) == {
        "software_header:server",
        "internal_header:x-1panel-node",
        "backend_metadata:system_fingerprint",
        "backend_metadata:timings",
    }


def test_privacy_probe_accepts_public_metadata():
    assert not PROBE["metadata_findings"]({
        "headers": {"x-1panel-public-model": "siyuan/auto", "server": "SIYUAN"},
        "models": ["siyuan/auto"],
        "payload": {"model": "siyuan/auto", "usage": {"prompt_tokens": 10}},
    })
