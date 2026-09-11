import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from test_connector_api import STUDIO_ROOT


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("multimodal_packaging", "scripts/build_multimodal_catalog.py")
installer = load("bridge_installer", "workbuddy/install_bridge.py")
freezer = load("fleet_freezer", "scripts/freeze_multimodal_fleet.py")


def test_catalog_does_not_claim_missing_ref_model_or_grant_qualification(tmp_path):
    names = ["minimax_h3_fl2va_int8_convrot.safetensors", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
             "minimax_h3_video_vae_fp16.safetensors", "minimax_h3_audio_vae_fp32.safetensors"]
    backend = {"runtime_version": "1" * 64, "weight_files": [{"filename": name, "sha256": "a" * 64} for name in names]}
    files = builder.build(STUDIO_ROOT, backend)
    catalog = json.loads(files["multimodal-profiles.json"])
    assert len(catalog["profiles"]) == 5
    assert len(catalog["unavailable_profiles"]) == 5
    assert all(item["qualification"] == "not_validated" and not item["parallel_validated"] for item in catalog["profiles"])
    assert all(item["reason"] == "required_weight_not_registered" for item in catalog["unavailable_profiles"])
    for item in catalog["profiles"]:
        assert item["sampling"]["steps"] == 14
        assert "ref2va" not in " ".join(weight["filename"] for weight in item["weights"])


def test_bridge_configuration_preserves_unrelated_servers_and_removes_only_h3_token():
    current = {"mcpServers": {"siyuan-h3-studio": {"type": "http", "headers": {"Authorization": "Bearer fixture-only"}},
                              "unrelated": {"url": "https://other.invalid", "disabled": True, "headers": {"Authorization": "existing"}}},
               "other_setting": 5}
    result = installer.configuration(current, "/bridge.cjs", "/private-settings.json", "/node")
    assert result["mcpServers"]["unrelated"] == current["mcpServers"]["unrelated"]
    assert result["other_setting"] == 5
    assert "headers" not in result["mcpServers"]["siyuan-h3-studio"]
    assert result["mcpServers"]["siyuan-h3-studio"]["type"] == "stdio"
    assert "Authorization" in current["mcpServers"]["siyuan-h3-studio"]["headers"]
    with pytest.raises(ValueError):
        installer.configuration({"mcpServers": {}}, "/bridge", "/settings", "/node")


@pytest.mark.parametrize("concurrent", [False, True])
def test_installer_rollback_restores_only_its_changes(tmp_path, monkeypatch, concurrent):
    private = tmp_path / "private"
    backup = private / "backup"
    backup.mkdir(parents=True)
    target = tmp_path / "mcp.json"
    before, after = b"original-settings", b"installed-settings"
    (backup / "mcp.json").write_bytes(before)
    target.write_bytes(b"concurrent-user-edit" if concurrent else after)
    installer.write_receipt(private, {"changes": [{"path": str(target), "backup": "mcp.json",
        "before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest()}]})
    monkeypatch.setattr(installer, "copy_acl", lambda *args: None)
    if concurrent:
        with pytest.raises(ValueError, match="concurrent_changes_preserved"):
            installer.restore_installation(private)
        assert target.read_bytes() == b"concurrent-user-edit"
    else:
        assert installer.restore_installation(private)["state"] == "rolled_back"
        assert target.read_bytes() == before


def test_frozen_configuration_preserves_protections_and_never_grants_new_mode_qualification():
    backends = [{"id": name, "pid": 123, "cmdline_sha256": "b" * 64, "runtime_version": "a" * 64,
                 "recipes": {"A4": {"qualification": "historical_single_completed", "vram_budget_bytes": 15 * 1024**3}}}
                for name in ("single-a4", "single-realism", "single-b8")]
    original = {"backends": backends, "static_budget_gib": 18, "reclaim_factor": 0.5, "stable_seconds": 60,
                "global_margin_gib": 2, "qualified_combinations": []}
    observed = {backend["id"]: {**backend, "unit": "h3-" + backend["id"] + ".service", "unit_sha256": "c" * 64,
                                "input_root": "/registered/state/input"} for backend in backends}
    catalog = {"profiles": [{"profile_id": "H3_I2V_QUALITY14", "version": "v1", "runtime_version_required": "a" * 64}]}
    result = freezer.configuration(original, observed, catalog)
    assert result["backends"][0]["recipes"]["H3_I2V_QUALITY14"]["qualification"] == "not_validated"
    assert "H3_I2V_QUALITY14" not in original["backends"][0]["recipes"]
    for key in ("static_budget_gib", "reclaim_factor", "stable_seconds", "global_margin_gib", "qualified_combinations"):
        assert result[key] == original[key]
    observed["single-a4"]["cmdline_sha256"] = "wrong"
    with pytest.raises(ValueError, match="identity"):
        freezer.configuration(original, observed, catalog)
