import copy
import time
from dataclasses import replace

import pytest

from test_multifleet import cluster, module


@pytest.fixture
def acceptance(cluster):
    client = cluster.clients["ivan-u24"]
    client.state["recipe_capacity"] = {"profile": {"recipe_version": "v1", "available_slots": 0,
        "eligible_lanes": [], "reasons": ["release_validation_gate"]}}
    client.catalog = {"enabled": True, "recipes": [], "multimodal_profiles": [
        {"profile_id": "profile", "version": "v1", "qualified": False, "acceptance_tasks": ["abc123abc123"]}]}
    client.fresh_reads = []
    def capacity(*, fresh=False):
        client.fresh_reads.append(fresh)
        return copy.deepcopy(client.state)
    client.capacity = capacity
    client.catalog_reads = 0
    def catalog():
        client.catalog_reads += 1
        return copy.deepcopy(client.catalog)
    client.recipe_catalog = catalog
    return cluster, client


def admit(cluster, **changes):
    args = dict(node=cluster.nodes["ivan-u24"], target="ivan-u24", project_id="abc123abc123",
                capability="profile", version="v1")
    args.update(changes)
    return cluster.acceptance_admission(**args)


def test_acceptance_reserves_only_own_single_slot_and_posts_original_execution(acceptance):
    cluster, client = acceptance
    fake = module("abc123abc123", "ivan-u24")
    fake.project["execution_profile"] = {"profile_id": "profile", "version": "v1"}
    fake.recipe_scope = lambda p, s: False
    with cluster.execution_scope(fake, "abc123abc123", "preview"):
        execution = fake.project["stages"]["preview"]["execution_id"]
        assert execution.startswith("studio_abc123abc123_preview_")
        assert len(cluster.routes.active()) == 1
        assert cluster.capacity()["recipe_capacity"]["profile"]["available_slots"] == 0
        admission = admit(cluster)
        assert cluster.routes.reserve("second", [(cluster.nodes["ivan-u24"], admission)], "profile", None) is None
        with pytest.raises(RuntimeError, match="执行编号"):
            cluster._request("POST", "/prompt", {"extra_data": {"h3": {"execution_id": "wrong"}}})
        assert not client.calls
        cluster._request("POST", "/prompt", {"extra_data": {"h3": {"execution_id": execution}}})
        assert len(client.calls) == 1
    assert True in client.fresh_reads and client.catalog_reads >= 1
    assert cluster.routes.get(execution)["state"] == "submitted"


@pytest.mark.parametrize("field,value", [("project_id", "other"), ("version", "wrong"),
    ("target", "auto"), ("target", "ivan"), ("capability", "A4")])
def test_wrong_task_version_or_implicit_target_never_gets_exception(acceptance, field, value):
    cluster, client = acceptance
    assert admit(cluster, **{field: value}) is None
    assert not client.calls


@pytest.mark.parametrize("changes", [{"enabled": False}, {"max_parallel": 2}])
def test_disabled_or_multilane_node_rejected(acceptance, changes):
    cluster, client = acceptance
    assert admit(cluster, node=replace(cluster.nodes["ivan-u24"], **changes)) is None
    assert not client.fresh_reads


@pytest.mark.parametrize("field,value", [("available", False), ("resources_ok", False),
    ("counts_complete", False), ("exclusive_window", True), ("active", 1), ("active", False),
    ("queued", 1), ("sampled_at", 0)])
def test_resource_counts_lease_activity_and_staleness_fail_closed(acceptance, field, value):
    cluster, client = acceptance
    client.state[field] = value
    assert admit(cluster) is None
    assert not client.calls


@pytest.mark.parametrize("reasons", [[], ["runtime_hard_stop_requires_operator_reconciliation"],
    ["release_validation_gate", "disk_budget"], ["fleet_draining"], ["no_qualified_idle_backend"]])
def test_only_release_gate_reason_is_accepted(acceptance, reasons):
    cluster, client = acceptance
    client.state["recipe_capacity"]["profile"]["reasons"] = reasons
    assert admit(cluster) is None


def test_catalog_is_read_again_and_revocation_applies(acceptance):
    cluster, client = acceptance
    assert admit(cluster)
    client.catalog["multimodal_profiles"][0]["acceptance_tasks"] = []
    assert admit(cluster) is None
    assert client.catalog_reads == 2
    assert client.fresh_reads == [True, True]
    assert cluster.eligible(cluster.nodes["ivan-u24"], client.state, "profile", "v1") is None


def test_fresh_capacity_bypasses_cached_success(tmp_path, monkeypatch):
    from app.fleet import FleetClient
    client = FleetClient("http://unused", str(tmp_path / "key"))
    client.capacity_cache = {"available": True}
    client.capacity_expires = time.monotonic() + 60
    calls = []
    def offline(*args, **kwargs):
        calls.append(args)
        raise OSError("offline")
    monkeypatch.setattr(client, "_request", offline)
    assert client.capacity()["available"] is True
    assert not calls
    assert client.capacity(fresh=True)["available"] is False
    assert calls == [("GET", "/api/health")]
