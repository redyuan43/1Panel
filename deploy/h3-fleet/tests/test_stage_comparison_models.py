import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("comparison_stage", SCRIPTS / "stage_comparison_models.py")
STAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STAGE)


@pytest.mark.parametrize("group,name,expected", [
    ("a_lightx2v", "a.safetensors", "loras/a.safetensors"),
    ("b_vdn", "diffusion_models/OpenVDN/branch.safetensors", "diffusion_models/OpenVDN/branch.safetensors"),
    ("d_fasth3", "d.safetensors", "diffusion_models/d.safetensors"),
])
def test_destination_layout(group, name, expected):
    assert STAGE.destination_relative({"group": group, "filename": name}) == expected


def test_unknown_group_rejected():
    with pytest.raises(ValueError):
        STAGE.destination_relative({"group": "unknown", "filename": "bad"})


def test_resume_budget_counts_only_missing_bytes(tmp_path):
    record = {"group": "a_lightx2v", "filename": "a.safetensors", "bytes": 10}
    partial = tmp_path / "incoming/a_lightx2v/a.safetensors.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"1234")
    assert STAGE.remaining_bytes(tmp_path, [record]) == 6


def test_duplicate_artifacts_rejected(tmp_path):
    record = {"group": "a_lightx2v", "filename": "a.safetensors", "bytes": 10}
    for relative in ("incoming/a_lightx2v/a.safetensors.part", "loras/a.safetensors"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"1234")
    with pytest.raises(ValueError):
        STAGE.remaining_bytes(tmp_path, [record])
