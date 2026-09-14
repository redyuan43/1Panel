import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("comparison_case", SCRIPTS / "run_comparison_case.py")
CASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CASE)


def workflow():
    return {"1": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
            "2": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {"width": 480, "height": 864, "length": 362}},
            "3": {"class_type": "SaveVideo", "inputs": {}}}


def test_accepts_full_duration_explicit_sampler():
    CASE.validate_input(workflow())


def test_rejects_short_video():
    graph = workflow()
    graph["2"]["inputs"]["length"] = 124
    with pytest.raises(ValueError, match="362"):
        CASE.validate_input(graph)


def test_rejects_missing_artifact():
    graph = workflow()
    del graph["3"]
    with pytest.raises(ValueError, match="save"):
        CASE.validate_input(graph)


def test_rejects_untracked_multi_sampler():
    graph = workflow()
    graph["4"] = graph["1"]
    with pytest.raises(ValueError, match="one"):
        CASE.validate_input(graph)
