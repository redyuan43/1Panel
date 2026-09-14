import copy
import json

import pytest

from scripts.prepare_multinode_bundle import GIB, GPU_MAIN, GPU_SECOND, GPU_FAST, prepare


def inventory():
    return {"memory": {"MemTotal": 64 * GIB, "MemAvailable": 50 * GIB},
            "storage": {"/": {"free": 20 * GIB}},
            "gpus": [{"uuid": gpu, "name": "test"} for gpu in (GPU_MAIN, GPU_SECOND)],
            "packages": [{"name": "torch", "version": "2.12.1"}],
            "fleet_packages": [{"name": "fastapi", "version": "0.141.1"}],
            "weights": [{"source": "/source/vae/model.safetensors", "filename": "model.safetensors",
                         "sha256": "a" * 64, "size": 100}],
            "runtimes": [{"id": "single-a4", "runtime_root": "/source/runtime-a4"}],
            "capacity_policy": {"short": {"preview": {}, "quality": {}}, "long": {}, "resources": {}}}


def test_bundle_keeps_qualification_closed_and_uses_physical_ram(tmp_path):
    source = inventory()
    target = copy.deepcopy(source)
    target["gpus"] = [{"uuid": GPU_FAST, "name": "4060 Ti"}]
    report = prepare(source, {"ivan": source, "ivan-u24": target}, tmp_path / "bundle")
    assert report["ivan"]["cgroup_limit_bytes"] == 48 * GIB
    assert report["ivan"]["resource_preflight_reasons"] == ["root_disk_headroom"]
    assert report["ivan"]["active_policy_max_parallel"] == 1
    assert not report["ivan-u24"]["gpu_inference_started"]
    policy = json.loads((tmp_path / "bundle/ivan/recipe-policy.json").read_text())
    assert policy["enabled"] is False and policy["qualified_combinations"] == []
    workers = json.loads((tmp_path / "bundle/ivan/worker-specs.json").read_text())
    assert len(workers) == 2 and workers[0]["gpu_uuid"] != workers[1]["gpu_uuid"]
    script = (tmp_path / "bundle/copy-to-ivan-u24.sh").read_text()
    assert "--delete" not in script and "systemctl" not in script
    assert "--exclude=/models/" in script


def test_bundle_rejects_missing_physical_gpu(tmp_path):
    source = inventory()
    source["gpus"] = source["gpus"][:1]
    with pytest.raises(ValueError, match="GPU inventory"):
        prepare(source, {"ivan": source}, tmp_path / "bundle")
