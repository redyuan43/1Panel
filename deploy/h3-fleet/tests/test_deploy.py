from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("h3_fleet_deploy", ROOT / "scripts/deploy.py")
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def test_archive_contains_only_delivery_files() -> None:
    encoded = deploy.archive()
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as bundle:
        names = set(bundle.getnames())
        assert names == {
            "app/__init__.py",
            "app/main.py",
            "app/workflow_builder.py",
            "requirements.txt",
            "systemd/h3-compute.slice",
            "systemd/comfyui-h3@.service",
            "systemd/h3-fleet.service",
        }
        assert all(member.isfile() for member in bundle.getmembers())
        for member in bundle.getmembers():
            extracted = bundle.extractfile(member)
            assert extracted is not None
            assert b"H3_ROUTER_KEY=" not in extracted.read()


def test_key_requires_regular_0600_file(tmp_path: Path) -> None:
    secret = tmp_path / "h3-key"
    secret.write_text("private_test-key\n", encoding="utf-8")
    secret.chmod(0o600)
    assert deploy.key(secret) == "private_test-key"
    secret.chmod(0o640)
    with pytest.raises(RuntimeError, match="regular 0600"):
        deploy.key(secret)


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8789",
        "http://100.96.79.21:8789/",
        "https://ivan.taild500c8.ts.net:8789",
    ],
)
def test_private_executor_url_accepts_private_origins(value: str) -> None:
    assert deploy.private_executor_url(value).endswith("8789")


@pytest.mark.parametrize(
    "value",
    [
        "http://8.8.8.8:8789",
        "http://example.com:8789",
        "http://user:pass@100.96.79.21:8789",
        "http://100.96.79.21:8789/api",
    ],
)
def test_private_executor_url_rejects_unsafe_origins(value: str) -> None:
    with pytest.raises(RuntimeError):
        deploy.private_executor_url(value)


def test_remote_program_compiles() -> None:
    compile(deploy.REMOTE, "<h3-fleet-remote-deploy>", "exec")
    assert 'tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")' in deploy.REMOTE
    assert 'temporary = target.parent / ("." + target.name' in deploy.REMOTE
    assert 'worker.get("CUDA_VISIBLE_DEVICES") != lane["gpu_uuid"]' in deploy.REMOTE
    assert '"/api/router/drain"' in deploy.REMOTE
    assert '"/api/router/resume"' in deploy.REMOTE
    assert "restored_verified" in deploy.REMOTE
