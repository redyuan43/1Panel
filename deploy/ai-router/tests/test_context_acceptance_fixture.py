import json
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("context_acceptance_fixture",
    Path(__file__).resolve().parents[1] / "scripts/context_acceptance.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
build_fixture, RULE = fixture_module.build_fixture, fixture_module.RULE


class TestCounter:
    def count_request(self, body, kind):
        return len(json.dumps(body, ensure_ascii=False)) // 4


def test_corpus_is_measured_deterministic_and_keeps_corrections_and_tool_pair():
    first = build_fixture(TestCounter(), 10_000)
    second = build_fixture(TestCounter(), 10_000)
    assert first == second
    assert 10_000 <= first["router_tokens"] < 10_100
    assert len(first["questions"]) == 40
    assert sum("superseded" in item for item in first["questions"]) == 4
    messages = first["body"]["messages"]
    assert messages[0]["content"] == RULE
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]
    assert "E_FINAL_782" in messages[-1]["content"]
    for question in first["questions"]:
        assert any(question["service"] in str(item) and question["answer"] in str(item) for item in messages[:-2])


@pytest.mark.parametrize("limit", [True, 9999, 400001, "339000"])
def test_corpus_rejects_unbounded_or_invalid_target(limit):
    with pytest.raises(ValueError):
        build_fixture(TestCounter(), limit)
