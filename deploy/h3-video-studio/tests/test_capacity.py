import json
import time

import pytest

from app.fleet import FleetClient
from app.config import get_settings


def test_capacity_counts_busy_unknown_and_caches_without_private_data(tmp_path, monkeypatch):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    fleet.external_status_path = tmp_path / "live.json"
    health = {"lanes": [
        {"id": "fast", "device": "4060", "ok": True, "enabled": True, "active_job_count": 1},
        {"id": "main", "device": "3060", "ok": True, "enabled": True},
        {"id": "preview", "device": "3060", "ok": False, "enabled": True},
    ]}
    capacity = {"queues": [{"lane_id": name, "queued_or_running": 0} for name in ("fast", "main", "preview")],
                "active": [{"status": "running", "execution_id": "private-id"}, {"status": "queued"}],
                "resources": {"ok": True, "swap_recovery": {"ready": False}},
                "studio_preview": {"max_parallel": 3, "available": 1, "validated_parallel": True,
                                   "reason": "ram_headroom", "execution_id": "private-id"},
                "policy": {"short": {"preview": {"max_parallel": 3, "max_frames": 124},
                                     "quality": {"max_parallel": 2}}, "long": {"max_parallel": 1}}}
    calls = []

    def request(method, path, **kwargs):
        assert method == "GET" and kwargs["timeout"] == 8
        calls.append(path)
        return health if path == "/api/health" else capacity

    monkeypatch.setattr(fleet, "_request", request)
    result = fleet.capacity()
    assert [lane["status"] for lane in result["lanes"]] == ["busy", "idle", "unknown"]
    assert result["queued"] == result["active"] == 1
    assert result["limits"]["long"] == 1
    assert result["studio_preview"]["available"] == 1
    assert result["studio_preview"]["reason"] == "ram_headroom"
    assert "private-id" not in json.dumps(result)
    assert fleet.capacity() is result and len(calls) == 2

    def unavailable(*args, **kwargs):
        raise TimeoutError("private backend detail")

    monkeypatch.setattr(fleet, "_request", unavailable)
    fleet.capacity_expires = 0
    failed = fleet.capacity()
    assert not failed["available"] and failed["lanes"] == []
    assert "private backend detail" not in json.dumps(failed)


@pytest.mark.parametrize("status,age,expected,active,queued,complete", [
    ("running", 0, ["busy", "idle"], 1, 0, True),
    ("running", 60, ["unknown", "unknown"], 0, 0, False),
    ("reconciling", 0, ["unknown", "unknown"], 0, 0, False),
    ("completed", 0, ["idle", "idle"], 0, 0, True),
    ("queued", 0, ["idle", "idle"], 0, 1, True),
])
def test_external_capacity(tmp_path, status, age, expected, active, queued, complete):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    fleet.external_status_path = tmp_path / "live.json"
    record = {"id": "D4", "prompt_id": "external-id", "gpu_uuid": "gpu-fast", "status": status}
    fleet.external_status_path.write_text(json.dumps({"available": True, "observed_at": time.time() - age,
                                                     "cases": [record, record]}))
    result = {"exclusive_window": True, "active": 0, "queued": 0,
              "lanes": [{"status": "idle"}, {"status": "idle"}]}
    health = {"lanes": [{"gpu_uuid": "gpu-fast"}, {"gpu_uuid": "gpu-main"}]}
    fleet._merge_external_capacity(result, health, {"active": []})
    assert [lane["status"] for lane in result["lanes"]] == expected
    assert (result["active"], result["queued"], result["counts_complete"]) == (active, queued, complete)
    if active:
        assert result["lanes"][0]["external_tasks"] == ["D4"]


def test_missing_monitor_with_lease_never_reports_idle(tmp_path):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    fleet.external_status_path = tmp_path / "missing.json"
    result = {"exclusive_window": True, "active": 1, "queued": 0,
              "lanes": [{"status": "busy"}, {"status": "idle"}]}
    fleet._merge_external_capacity(result, {"lanes": [{}, {}]}, {"active": []})
    assert [lane["status"] for lane in result["lanes"]] == ["busy", "unknown"]
    assert result["counts_complete"] is False


@pytest.mark.parametrize("external", [None, "", "published"])
def test_external_monitor_uses_same_config_as_static_routes(tmp_path, monkeypatch, external):
    monkeypatch.setenv("H3_STUDIO_ROOT", str(tmp_path / "frozen-release"))
    monkeypatch.delenv("H3_STUDIO_COMPARISON_RESULTS", raising=False)
    if external is not None:
        monkeypatch.setenv("H3_STUDIO_COMPARISON_RESULTS", str(tmp_path / external) if external else "")
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    assert fleet.external_status_path == get_settings().comparison_results_root / "live.json"
    expected = tmp_path / "published/live.json" if external else tmp_path / "frozen-release/frontend/comparison-results/live.json"
    assert fleet.external_status_path == expected
    expected.parent.mkdir(parents=True)
    expected.write_text(json.dumps({"available": True, "observed_at": time.time(), "cases": [
        {"id": "B8", "prompt_id": "external", "gpu_uuid": "gpu-fast", "status": "running"}]}))
    result = {"exclusive_window": False, "active": 0, "queued": 0, "lanes": [{"status": "idle"}]}
    fleet._merge_external_capacity(result, {"lanes": [{"gpu_uuid": "gpu-fast"}]}, {"active": []})
    assert result["active"] == 1 and result["lanes"][0]["status"] == "busy"


@pytest.fixture
def displayed_capacity(tmp_path):
    fleet = FleetClient("http://unused", str(tmp_path / "key"))
    fleet.external_status_path = tmp_path / "missing.json"
    health = {"lanes": [{"id": "fast", "device": "4060", "ok": True, "enabled": True}]}
    snapshot = {"queues": [{"lane_id": "fast", "queued_or_running": 0}], "active": [], "resources": {"ok": True},
        "policy": {"max_active_jobs": 1, "short": {"preview": {"max_parallel": 3, "max_frames": 362},
            "quality": {"max_parallel": 2}}, "long": {"max_parallel": 3}},
        "studio_preview": {"max_parallel": 3, "available": 3, "validated_parallel": True, "reason": None},
        "recipe_capacity": {"A4": {"available_slots": 3, "eligible_lanes": ["fast"], "recipe_version": "v1", "reasons": []}}}
    fleet._request = lambda method, path, **kwargs: health if path == "/api/health" else snapshot
    return fleet, snapshot


@pytest.mark.parametrize("maximum,active", [(1, 0), (1, 1), (2, 0), (3, 1)])
def test_displayed_capacity_respects_global_active_limit(displayed_capacity, maximum, active):
    fleet, snapshot = displayed_capacity
    snapshot["policy"]["max_active_jobs"] = maximum
    snapshot["active"] = [{"status": "running"} for unused in range(active)]
    result = fleet.capacity()
    assert result["limits"] == {"short_preview": maximum, "short_quality": min(maximum, 2),
                                "short_frames": 362, "long": maximum}
    assert result["studio_preview"]["max_parallel"] == maximum
    assert result["studio_preview"]["available"] == maximum - active
    assert result["studio_preview"]["validated_parallel"] is (maximum > 1)
    assert result["recipe_capacity"]["A4"]["available_slots"] == maximum - active
    assert snapshot["studio_preview"]["max_parallel"] == 3
    assert snapshot["recipe_capacity"]["A4"]["available_slots"] == 3


def test_absent_global_limit_preserves_legacy_three_slot_display(displayed_capacity):
    fleet, snapshot = displayed_capacity
    del snapshot["policy"]["max_active_jobs"]
    assert fleet.capacity()["limits"]["short_preview"] == 3


def test_global_limit_never_increases_tighter_server_admission(displayed_capacity):
    fleet, snapshot = displayed_capacity
    snapshot["studio_preview"].update(available=0, reason="swap_hard_limit")
    snapshot["recipe_capacity"]["A4"]["available_slots"] = 0
    result = fleet.capacity()
    assert result["studio_preview"]["available"] == 0
    assert result["studio_preview"]["reason"] == "swap_hard_limit"
    assert result["recipe_capacity"]["A4"]["available_slots"] == 0


@pytest.mark.parametrize("invalid", [None, True, 0, -1, 4, "1"])
def test_invalid_global_limit_does_not_invent_capacity(displayed_capacity, invalid):
    fleet, snapshot = displayed_capacity
    snapshot["policy"]["max_active_jobs"] = invalid
    result = fleet.capacity()
    assert result["available"] is False and "limits" not in result
