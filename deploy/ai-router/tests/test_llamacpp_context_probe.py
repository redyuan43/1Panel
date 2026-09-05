from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate-llamacpp-context.py"
SPEC = importlib.util.spec_from_file_location("context_probe", PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_lookup_records_cover_both_ends_and_middle():
    expected = {f"item_{index}": f"value_{index}" for index in range(5)}
    lines = [f"ordinary {index}" for index in range(100)]
    messages = probe.build_messages(lines, expected)
    content = messages[-1]["content"].splitlines()
    assert content[0] == "LOOKUP item_0 = value_0"
    assert content[52] == "LOOKUP item_2 = value_2"
    assert content[-2] == "LOOKUP item_4 = value_4"
    assert len(lines) == 100
    assert content[-1] == "Requested keys: item_0, item_1, item_2, item_3, item_4"


def test_evidence_is_not_overwritten(tmp_path):
    path = tmp_path / "report.json"
    probe.save(path, {"passed": False})
    with pytest.raises(FileExistsError):
        probe.save(path, {"passed": True})
