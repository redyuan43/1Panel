import copy
import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.input_memory import demand_budget
from app.throughput import GIB
from test_recipe_dispatch import runtime


@pytest.fixture
def reference_case(runtime, monkeypatch):
    module, _ = runtime
    dispatcher = module.fleet.recipes
    identifier = "H3_REFERENCE_VIDEO_QUALITY14"
    original_get = dispatcher.catalog.get
    dispatcher.catalog.entries[identifier] = {"version": "v1", "asset_roles": {"reference_video": "20"}}
    monkeypatch.setattr(dispatcher.catalog, "get", lambda name: dispatcher.catalog.entries[identifier] if name == identifier else original_get(name))
    backend = next(iter(dispatcher.backends.values()))
    backend["recipes"][identifier] = copy.deepcopy(backend["recipes"]["A4"])
    asset = {"asset_id": "asset_" + "a" * 32, "sha256": "b" * 64, "size": 100}
    evidence = {**asset, "decode_budget_bytes": 5 * GIB, "metadata": {"is_cfr_24": True}}
    contract = {"assets": {"reference_video": asset}, "verified_input_memory": {"reference_video": evidence}}
    job = {"request_json": json.dumps({"extra_data": {"h3": {"contract": contract}}})}
    return module, dispatcher, identifier, backend, contract, job


def test_actual_candidate_and_demand_include_same_reference_decode_increment(reference_case):
    _, dispatcher, identifier, backend, contract, job = reference_case
    result = dispatcher.candidate(identifier, backend, job=job)
    assert result["model_base_budget_bytes"] == 24 * GIB
    assert result["candidate_budget_bytes"] == result["budget_bytes"] == demand_budget(contract, 24 * GIB) == 29 * GIB
    assert result["input_decode_budget_bytes"] == 5 * GIB
    with pytest.raises(ValueError, match="evidence_required"):
        dispatcher.candidate(identifier, backend)


def test_actual_history_preserves_total_floor_without_adding_decode_repeatedly(reference_case):
    module, dispatcher, identifier, backend, _, job = reference_case
    first = dispatcher.candidate(identifier, backend, job=job)
    stored = module.fleet.store.create(prompt_id=uuid4().hex, upstream_prompt_id="", execution_id=uuid4().hex,
        request_digest="fixture", lane_id="", stage="preview", profile="preview", status="completed", request_data={})
    module.fleet.store.update(stored["prompt_id"], recipe_id=identifier, backend_json="{}", worker_peak_bytes=GIB,
        admission_json=json.dumps({**first, "worker_baseline_bytes": GIB}))
    second = dispatcher.candidate(identifier, backend, job=job)
    assert second["candidate_budget_bytes"] == first["candidate_budget_bytes"]
    module.fleet.store.update(stored["prompt_id"], worker_peak_bytes=40 * GIB)
    measured = dispatcher.candidate(identifier, backend, job=job)
    assert measured["candidate_budget_bytes"] >= 39 * GIB + max(2 * GIB, (39 * GIB + 9) // 10)
    assert measured["model_base_budget_bytes"] == first["model_base_budget_bytes"]


def test_large_reference_budget_cannot_bypass_actual_memory_admission(reference_case):
    module, dispatcher, identifier, backend, contract, job = reference_case
    contract["verified_input_memory"]["reference_video"]["decode_budget_bytes"] = 90 * GIB
    job["request_json"] = json.dumps({"extra_data": {"h3": {"contract": contract}}})
    candidate = dispatcher.candidate(identifier, backend, job=job)
    assert candidate["candidate_budget_bytes"] == 114 * GIB
    decision = dispatcher.memory.decide(dispatcher.snapshot, candidate, [], limits=module.fleet.policy.data["resources"],
                                         now=dispatcher.snapshot["timestamp"], vram_free_bytes=16 * GIB)
    assert decision["admission"] != "allow"


def test_warm_cleanup_keeps_the_specific_reference_job_evidence(reference_case, monkeypatch):
    _, dispatcher, identifier, backend, _, job = reference_case
    job = {**job, "recipe_id": identifier}
    checked = []
    def decision(*args, **kwargs):
        checked.append(args[5])
        return {"reasons": ["gpu_vram_headroom"]}
    monkeypatch.setattr(dispatcher, "decision", decision)
    monkeypatch.setattr(dispatcher, "control", lambda *args: {"recipe_id": "previous"})
    monkeypatch.setattr(dispatcher, "unload", AsyncMock(return_value=True))
    assert asyncio.run(dispatcher.cleanup_blocking_warm_backends([job], [backend], [], {backend["id"]: {}}))
    assert checked == [job]
    assert dispatcher.unload.await_count >= 1
