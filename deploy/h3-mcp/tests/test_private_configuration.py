import importlib.util
import io
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("private_configuration", Path(__file__).resolve().parents[1] / "workbuddy/configure_bridge.py")
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


@pytest.fixture
def private(tmp_path):
    (tmp_path / "private-token.json").write_text(json.dumps({"token": "previous-fixture-token"}))
    (tmp_path / "bridge-settings.json").write_text(json.dumps({"endpoint": configuration.ENDPOINT,
        "credential_file": str(tmp_path / "private-token.json")}))
    (tmp_path / "installation.json").write_text(json.dumps({"installed": True}))
    return tmp_path


def test_private_configuration_checks_readonly_auth_and_never_returns_token(private):
    checked = []
    result = configuration.save_token(private, "new-fixture-token-" + "x" * 40, verify=checked.append)
    assert checked == ["new-fixture-token-" + "x" * 40]
    assert result == {"saved": True, "client_reconnect_required": True, "gpu_submissions": 0}
    assert "new-fixture" not in json.dumps(result)
    assert json.loads((private / "private-token.json").read_bytes())["token"] == checked[0]


def test_failed_authentication_preserves_previous_private_credential(private):
    before = (private / "private-token.json").read_bytes()
    def rejected(token):
        raise ValueError("rejected")
    with pytest.raises(ValueError):
        configuration.save_token(private, "x" * 40, verify=rejected)
    assert (private / "private-token.json").read_bytes() == before
    assert not list(private.glob(".token-*"))


def test_concurrent_token_rotation_is_not_overwritten(private):
    def rotate(token):
        (private / "private-token.json").write_bytes(b"concurrent-user-update")
    with pytest.raises(ValueError, match="changed_during"):
        configuration.save_token(private, "x" * 40, verify=rotate)
    assert (private / "private-token.json").read_bytes() == b"concurrent-user-update"
    assert not list(private.glob(".token-*"))


def test_private_configuration_cannot_send_token_to_a_different_origin(private):
    path = private / "bridge-settings.json"
    data = json.loads(path.read_text())
    data["endpoint"] = "https://unrelated.invalid"
    path.write_text(json.dumps(data))
    checked = []
    with pytest.raises(ValueError, match="not_installed"):
        configuration.save_token(private, "x" * 40, verify=checked.append)
    assert not checked


def test_two_configuration_windows_cannot_write_concurrently(private):
    def competing_window(token):
        with pytest.raises(OSError):
            configuration.save_token(private, "y" * 40, verify=lambda token: None)
    configuration.save_token(private, "x" * 40, verify=competing_window)
    assert json.loads((private / "private-token.json").read_bytes())["token"] == "x" * 40
    configuration.save_token(private, "z" * 40, verify=lambda token: None)
    assert json.loads((private / "private-token.json").read_bytes())["token"] == "z" * 40


def test_authentication_probe_never_calls_generation_or_fleet_capacity(monkeypatch):
    requests = []
    class Opener:
        def open(self, request, timeout):
            value = json.loads(request.data)
            requests.append(value)
            assert request.full_url == configuration.ENDPOINT
            result = {} if value["id"] == 1 else {"tools": [{"name": name} for name in (
                "h3_capabilities", "h3_save_draft", "h3_start_preview")]}
            return io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": value["id"], "result": result}).encode())
    monkeypatch.setattr(configuration.urllib.request, "build_opener", lambda *handlers: Opener())
    configuration.check_token("x" * 40)
    assert [request["method"] for request in requests] == ["initialize", "tools/list"]


def test_credentials_never_follow_redirects():
    with pytest.raises(ValueError, match="redirect_refused"):
        configuration.NoCredentialRedirect().redirect_request(None, None, 302, "Redirect", {}, "https://unrelated.invalid")
