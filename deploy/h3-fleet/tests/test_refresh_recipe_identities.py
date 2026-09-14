import copy
import hashlib
from pathlib import Path

import pytest

from scripts.refresh_recipe_identities import identify


def fixture_backend(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    argv = ["/usr/bin/python3", str(root / "main.py"), "--reserve-vram", "3"]
    command = ("\0".join(argv) + "\0").encode()
    backend = {"argv": argv, "cmdline_sha256": hashlib.sha256(command).hexdigest(), "runtime_root": str(root),
               "pid": 10, "start_ticks": "100", "recipes": {"A4_C1": {"qualification": "historical_single_completed"}}}
    process = tmp_path / "proc" / "20"
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(command)
    (process / "stat").write_text("20 (python native) " + " ".join(["0"] * 19 + ["200"] + ["0"] * 20))
    (process / "cwd").symlink_to(root)
    return backend, process


def test_identity_rebind_preserves_configuration_and_original(tmp_path):
    backend, process = fixture_backend(tmp_path)
    original = copy.deepcopy(backend)
    checked = []
    result = identify(backend, 20, proc_root=process.parent, verify=lambda item: checked.append(item))
    assert backend == original
    assert result == {**original, "pid": 20, "start_ticks": "200"}
    assert checked == [result]


def test_changed_compute_arguments_fail_closed(tmp_path):
    backend, process = fixture_backend(tmp_path)
    (process / "cmdline").write_bytes((process / "cmdline").read_bytes().replace(b"3\0", b"1\0"))
    with pytest.raises(ValueError, match="parameters changed"):
        identify(backend, 20, proc_root=process.parent, verify=lambda item: None)


def test_changed_runtime_directory_fail_closed(tmp_path):
    backend, process = fixture_backend(tmp_path)
    backend["runtime_root"] = str(tmp_path / "different")
    with pytest.raises(ValueError, match="working directory changed"):
        identify(backend, 20, proc_root=process.parent, verify=lambda item: None)


@pytest.mark.parametrize("pid", [0, -1, True])
def test_unavailable_process_fail_closed(tmp_path, pid):
    backend, process = fixture_backend(tmp_path)
    with pytest.raises(ValueError, match="not running"):
        identify(backend, pid, proc_root=process.parent, verify=lambda item: None)


def test_identity_owner_failure_propagates(tmp_path):
    backend, process = fixture_backend(tmp_path)
    def reject(item):
        raise ValueError("runtime_gpu_binding_mismatch")
    with pytest.raises(ValueError, match="gpu_binding"):
        identify(backend, 20, proc_root=process.parent, verify=reject)
