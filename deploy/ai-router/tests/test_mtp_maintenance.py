import importlib.util
import json
from pathlib import Path
import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "guard-agx-translation.py"
SPEC = importlib.util.spec_from_file_location("translation_guard", PATH)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


@pytest.mark.parametrize("was_active", [False, True])
def test_release_restores_only_translation_and_is_idempotent(tmp_path, monkeypatch, was_active):
    dropin = tmp_path / "guard.conf"
    content = "[Unit]\nConditionPathExists=/run/test-allow\n"
    dropin.write_text(content)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"dropin": content, "was_active": was_active, "released": False}))
    calls = []
    monkeypatch.setattr(guard, "DROPIN", dropin)
    monkeypatch.setattr(guard, "run", lambda *args: calls.append(args))
    assert guard.release(state)["released"]
    assert not dropin.exists()
    assert ("systemctl", "daemon-reload") in calls
    assert (("systemctl", "start", guard.UNIT) in calls) is was_active
    before = list(calls)
    assert guard.release(state)["released"]
    assert calls == before
    assert not any("gateway" in str(call) for call in calls)


def test_release_preserves_concurrent_config_change(tmp_path, monkeypatch):
    dropin = tmp_path / "guard.conf"
    dropin.write_text("another operator")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"dropin": "our config", "was_active": True, "released": False}))
    monkeypatch.setattr(guard, "DROPIN", dropin)
    with pytest.raises(RuntimeError, match="changed concurrently"):
        guard.release(state)
    assert dropin.read_text() == "another operator"
