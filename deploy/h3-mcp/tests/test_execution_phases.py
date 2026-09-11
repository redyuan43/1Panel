import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("phases_under_test", Path(__file__).resolve().parents[1] / "fleet/execution_phases.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(kind, stamp, node=None, confirmed=False):
    return {"type": kind, "received_at": stamp, "prompt_id": "owned", "data": {"node": node}, "confirmed_sampler_progress": confirmed}


@pytest.fixture
def running():
    return {"status": "running", "upstream_prompt_id": "owned", "request_json": json.dumps({"prompt": {
        "1": {"class_type": "UNETLoader"}, "2": {"class_type": "SamplerCustomAdvanced"}, "3": {"class_type": "MiniMaxH3AVDecode"}}})}


def test_model_online_is_not_model_loaded_and_sampling_requires_owned_progress(running):
    assert module.phases(running, None)["model_state"] == "unknown"
    receipt = {"prompt_id": "owned", "connected": True, "events": [event("executing", 10, "1"), event("executing", 20, "2")]}
    assert module.phases(running, receipt, now=22)["phase"] == "model_preparing"
    receipt["events"].append(event("progress", 25, "2", True))
    result = module.phases(running, receipt, now=30)
    assert result["phase"] == "sampling" and result["model_state"] == "sampling_model_loaded"
    assert result["phase_timings"] == {"model_preparing": 15, "sampling": 5}
    receipt["events"].append(event("executing", 35, "3"))
    assert module.phases(running, receipt, now=40)["phase_timings"]["decoding"] == 5


def test_unknown_or_foreign_telemetry_does_not_claim_loaded(running):
    receipt = {"prompt_id": "foreign", "events": [event("progress", 25, "2", True)]}
    assert module.phases(running, receipt, now=30)["model_state"] == "unknown"
    receipt.update(prompt_id="owned", error="disconnected")
    assert module.phases(running, receipt, now=30)["phase_timings"] == {}


@pytest.mark.parametrize("reason,phase", [("admission_waiting:backend_disabled", "backend_disabled"), ("backend_starting", "backend_starting"), ("memory_budget", "resource_waiting")])
def test_queue_states_are_not_execution_failures(reason, phase):
    assert module.phases({"status": "queued", "admission_reason": reason}, None)["phase"] == phase
