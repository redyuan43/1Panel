import asyncio
import json

import pytest

from ai_router.prefill_admission import AdmissionPolicy, CAPABILITY, can_spill, configured_policy
from ai_router.scheduler import Scheduler
from ai_router.store import InMemoryStateStore


def policy():
    return AdmissionPolicy({"enabled": True, "groups": {
        "v100": {"mode": "cache", "capacity": 6, "deployments": ["v100-a", "v100-b"]},
        "amd": {"mode": "idle", "capacity": 1, "deployments": ["amd-a", "amd-b"]},
    }})


def test_default_off():
    assert AdmissionPolicy({}).lease_target("x", 6) == ("x", 6)


def test_enabled_configuration_requires_startup_capability_verification():
    assert not configured_policy({}).enabled
    assert configured_policy({"enabled": True, "groups": {
        "v100": {"mode": "cache", "capacity": 6, "deployments": ["v100"]}}}).enabled


def test_capability_requires_exact_enabled_version_group_capacity():
    contract = {"capability": CAPABILITY, "enabled": True,
                "service_group": "v100", "max_concurrency": 6,
                "lookup_protocol": "lookup-admission-v1", "cleanup_healthy": True}
    policy().verify("v100-a", contract)
    for key, wrong in [("enabled", False), ("capability", "old"),
                       ("service_group", "other"), ("max_concurrency", 1)]:
        with pytest.raises(ValueError):
            policy().verify("v100-a", {**contract, key: wrong})


@pytest.mark.parametrize("capability", [None, [], "old-backend", 1])
def test_malformed_capability_fails_as_contract_mismatch(capability):
    with pytest.raises(ValueError, match="capability mismatch"):
        policy().verify("v100-a", capability)


@pytest.mark.parametrize("config", [
    {"groups": {}},
    {"groups": []},
    {"groups": {"a": None}},
    {"groups": {1: {"mode": "idle", "deployments": ["x"]}}},
    {"groups": {"a": {"mode": "idle", "deployments": "xyz"}}},
    {"groups": {"a": {"mode": "idle", "capacity": 2, "deployments": ["x"]}}},
    {"groups": {"a": {"mode": "guess", "deployments": ["x"]}}},
    {"groups": {"a": {"mode": "idle", "deployments": ["x"]},
                "b": {"mode": "idle", "deployments": ["x"]}}},
])
def test_invalid_group_configuration_rejected(config):
    with pytest.raises(ValueError):
        AdmissionPolicy({"enabled": True, **config})


def test_group_name_cannot_shadow_unrelated_cloud_deployment():
    from ai_router.prefill_admission import AttemptWindow
    window = AttemptWindow(policy(), 10)
    excluded = set()
    window.dispatch("v100-a", excluded)
    # Physical group IDs and unrelated deployment IDs are separate namespaces.
    window.dispatch("v100", excluded)


def test_only_typed_versioned_409_counts_as_capacity():
    payload = json.dumps({"error": {"code": "prefill_admission_busy"}}).encode()
    headers = {"x-prefill-admission": CAPABILITY}
    assert policy().is_busy("v100-a", 409, headers, payload)
    for deployment, status, h, body in [
        ("amd-a", 409, headers, payload), ("v100-a", 500, headers, payload),
        ("v100-a", 409, {}, payload), ("v100-a", 409, headers, b"invalid"),
        ("v100-a", 409, headers, b'{"error":{"code":"other"}}'),
    ]:
        assert not policy().is_busy(deployment, status, h, body)


def test_exclusion_covers_physical_aliases():
    excluded = set()
    policy().exclude_group("v100-a", excluded)
    assert excluded == {"v100-a", "v100-b"}


@pytest.mark.parametrize("override", [
    {"requested_model": "explicit"}, {"required_endpoint_id": "bound"},
    {"directive": True}, {"pinned": True}, {"output_started": True},
])
def test_fixed_routes_and_output_forbid_spill(override):
    options = dict(requested_model="auto", required_endpoint_id=None,
                   directive=False, pinned=False, output_started=False)
    assert can_spill(**options)
    assert not can_spill(**{**options, **override})


def test_two_instances_share_idle_group_and_release():
    async def run():
        store = InMemoryStateStore()
        first = Scheduler(store, instance_id="one", admission=policy())
        second = Scheduler(store, instance_id="two", admission=policy())
        a = await first.begin_request(None)
        b = await second.begin_request(None)
        results = await asyncio.gather(
            first.try_acquire_deployment_candidates(a, ("amd-a",), capacity=4),
            second.try_acquire_deployment_candidates(b, ("amd-b",), capacity=4))
        assert sum(value is not None for value in results) == 1
        await a.release()
        await b.release()
        assert await second.try_acquire_deployment_candidates(b, ("amd-b",), capacity=4) == "amd-b"
        await b.release()
    asyncio.run(run())


def test_v100_preserves_six_shared_slots():
    async def run():
        scheduler = Scheduler(InMemoryStateStore(), admission=policy())
        leases = [await scheduler.begin_request(None) for _ in range(7)]
        results = [await scheduler.try_acquire_deployment_candidates(
            lease, ("v100-a" if i % 2 else "v100-b",), capacity=1)
                   for i, lease in enumerate(leases)]
        assert sum(value is not None for value in results) == 6
        for lease in leases:
            await lease.release()
    asyncio.run(run())
