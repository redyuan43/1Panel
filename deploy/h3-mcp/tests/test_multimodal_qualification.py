import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


spec = importlib.util.spec_from_file_location("qualification_test", Path(__file__).resolve().parents[1] / "fleet/multimodal_qualification.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    identifier = "H3_I2V_QUALITY14"
    gate = {"enabled": True, "allow_execution_prefixes": ["studio_7732e9926474_"]}
    backend = {"id": "test", "recipes": {identifier: {"recipe_version": "v1", "qualification": "designated_acceptance",
        "acceptance": {"execution_prefix": "studio_7732e9926474_", "expires_at": 2000}}}}
    catalog = SimpleNamespace(entries={identifier: {}}, get=lambda _: {"version": "v1"})
    dispatcher = SimpleNamespace(catalog=catalog, backends={"test": backend}, control=lambda _: None, profile_key=lambda *args: "key",
        public=lambda: {"multimodal_profiles": [{"profile_id": identifier, "qualified": True}]}, qualifications=lambda *args: False,
        fleet=SimpleNamespace(store=SimpleNamespace(release_validation_gate=lambda: gate), policy=SimpleNamespace(data={"max_active_jobs": 1})))
    module.install(dispatcher)
    return dispatcher, backend, gate, identifier


def test_only_exact_designated_task_can_enter_and_is_not_formally_qualified(policy):
    dispatcher, backend, _, identifier = policy
    assert dispatcher.qualifications(identifier, backend)
    public = dispatcher.public()["multimodal_profiles"][0]
    assert not public["qualified"] and public["acceptance_tasks"] == ["7732e9926474"]
    binding = dict(width=480, height=864, frame_count=362, fps=24)
    module.check_submission(dispatcher, identifier, binding, "studio_7732e9926474_preview_1")
    with pytest.raises(HTTPException):
        module.check_submission(dispatcher, identifier, binding, "studio_7732e9926475_preview_1")
    with pytest.raises(HTTPException):
        module.check_submission(dispatcher, identifier, {**binding, "width": 864, "height": 480}, "studio_7732e9926474_preview_1")


@pytest.mark.parametrize("change", ["gate_disabled", "prefix_removed", "expired", "too_long", "parallel", "quarantine", "version"])
def test_acceptance_cannot_escape_its_isolation_or_expiry(policy, change):
    dispatcher, backend, gate, identifier = policy
    entry = backend["recipes"][identifier]
    if change == "gate_disabled": gate["enabled"] = False
    if change == "prefix_removed": gate["allow_execution_prefixes"] = []
    if change == "expired": entry["acceptance"]["expires_at"] = 999
    if change == "too_long": entry["acceptance"]["expires_at"] = 100000
    if change == "parallel": dispatcher.fleet.policy.data["max_active_jobs"] = 3
    if change == "quarantine": dispatcher.control = lambda _: {"reason": "OOM"}
    if change == "version": entry["recipe_version"] = "wrong"
    assert not dispatcher.qualifications(identifier, backend)
    assert not dispatcher.public()["multimodal_profiles"][0]["acceptance_tasks"]
