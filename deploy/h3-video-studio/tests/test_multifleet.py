from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.fleet import SubmissionUnknown
from app.fleet_routes import RouteStore
from app.multifleet import MultiFleetClient, Node, read_nodes


def snapshot(active=0, ids=(), slots=1):
    return {"available": True, "sampled_at": time.time(), "resources_ok": True,
            "counts_complete": True, "exclusive_window": False, "active": active,
            "active_execution_ids": list(ids), "queued": 0,
            "lanes": [{"id": "main", "name": "GPU", "status": "idle"}],
            "limits": {"short_preview": 1, "short_quality": 1, "long": 1},
            "recipe_capacity": {"A4": {"recipe_version": "v1", "available_slots": slots,
                                       "eligible_lanes": ["main"], "reasons": []}}}


class Client:
    def __init__(self, url, key):
        self.url, self.state = url, snapshot()
        self.calls, self.jobs = [], {}
        self.unknown = False

    def capacity(self):
        return copy.deepcopy(self.state)

    def recipe_catalog(self):
        return {"enabled": True, "recipes": [{"recipe_id": "A4", "version": "v1"}]}

    def _request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if path.startswith("/api/jobs/by-execution/"):
            identifier = path.rsplit("/", 1)[1]
            if identifier in self.jobs:
                return dict(self.jobs[identifier])
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        if path == "/prompt":
            if self.unknown:
                raise TimeoutError("lost response")
            identifier = payload["extra_data"]["h3"]["execution_id"]
            job = {"execution_id": identifier, "prompt_id": "prompt-" + identifier, "status": "running"}
            self.jobs[identifier] = job
            return dict(job)
        return {}

    def _download_output(self, job, path, started):
        path.write_bytes(b"video")
        return {"execution_id": job["execution_id"], "source": self.url}

    def begin_batch(self, schedule):
        return {"owner": schedule}

    def end_batch(self, schedule):
        return {"released": True}


@pytest.fixture
def cluster(tmp_path):
    nodes = [Node("ivan", "http://ivan.ts.net:8789", "/private/ivan", 2, 20, True, ("quality",)),
             Node("ivan-u24", "http://ivan-u24.ts.net:8789", "/private/u24", 1, 10, True, ("quality",))]
    return MultiFleetClient(nodes, "ivan", tmp_path / "routes.sqlite3", client_factory=Client)


def module(project_id="p", target="auto", **stage):
    project = {"id": project_id, "recipe_id": "A4", "stages": {"preview": {"status": "queued", "target_node": target, **stage}}}
    return SimpleNamespace(_require_project=lambda _: copy.deepcopy(project),
        _set_stage=lambda p, s, **values: project["stages"][s].update(values),
        _cancelled=lambda p, s: project["stages"][s].get("cancel_requested", False),
        recipe_scope=lambda p, s: True, uses_turbo_preview=lambda p: True, project=project)


def submit(cluster, fake):
    with cluster.execution_scope(fake, fake.project["id"], "preview"):
        stage = fake.project["stages"]["preview"]
        cluster.submit_stage({"1": {}}, stage["execution_id"], "preview", "preview")
    return fake.project["stages"]["preview"]


def test_auto_uses_two_hosts_before_second_3060(cluster):
    a, b = submit(cluster, module("a")), submit(cluster, module("b"))
    assert (a["node_id"], b["node_id"]) == ("ivan-u24", "ivan")
    assert len(cluster.routes.active()) == 2
    # The second 3060 only becomes available when Fleet reports it admissible.
    cluster.clients["ivan"].state = snapshot(1, [b["execution_id"]], 1)
    c = submit(cluster, module("c"))
    assert c["node_id"] == "ivan"
    assert len(cluster.routes.active()) == 3
    d = module("d")
    d.project["stages"]["preview"]["cancel_requested"] = True
    with pytest.raises(RuntimeError, match="取消"):
        submit(cluster, d)
    assert len(cluster.routes.active()) == 3


def test_explicit_node_never_uses_other_host(cluster):
    assert submit(cluster, module(target="ivan"))["node_id"] == "ivan"
    assert not cluster.clients["ivan-u24"].calls


def test_legacy_batch_recovery_keeps_original_host(cluster):
    cluster.begin_batch("old-batch", recovery_stages=[{"execution_id": "old-execution"}])
    assert cluster.routes.batch("old-batch")["node_id"] == "ivan"


def test_batch_recovery_does_not_acquire_another_host(cluster):
    cluster.routes.begin_batch("different-batch", [cluster.nodes["ivan"]])
    with pytest.raises(SubmissionUnknown, match="不能迁移"):
        cluster.begin_batch("old-batch", recovery_stages=[{"execution_id": "old-execution"}])
    assert cluster.routes.batch("old-batch") is None


def test_batch_recovery_uses_durable_owner(cluster):
    node = cluster.nodes["ivan-u24"]
    cluster.routes.reserve("existing", [(node, {})], "A4", "batch", legacy=True)
    cluster.begin_batch("batch", recovery_stages=[{"execution_id": "existing"}])
    assert cluster.routes.batch("batch")["node_id"] == "ivan-u24"


def test_unknown_submission_survives_restart_and_cannot_repost(cluster):
    fake = module()
    cluster.clients["ivan-u24"].unknown = True
    with pytest.raises(SubmissionUnknown):
        submit(cluster, fake)
    identifier = fake.project["stages"]["preview"]["execution_id"]
    assert RouteStore(cluster.routes.path).get(identifier)["state"] == "submitting"
    fake.project["stages"]["preview"]["fleet_prepared"] = False
    with cluster.execution_scope(fake, "p", "preview"):
        with pytest.raises(SubmissionUnknown):
            cluster.submit_stage({}, identifier, "preview", "preview")
    assert sum(m == "POST" for m, *_ in cluster.clients["ivan-u24"].calls) == 1
    assert not cluster.clients["ivan"].calls


def test_old_execution_binds_disabled_legacy_node(cluster):
    fake = module(execution_id="old", fleet_pending=True)
    cluster.nodes["ivan"] = Node("ivan", "http://ivan.ts.net:8789", "/private/ivan", enabled=False)
    with cluster.execution_scope(fake, "p", "preview"):
        assert cluster.context.get()["node_id"] == "ivan"
        assert fake.project["stages"]["preview"]["fleet_prepared"] is False


def test_changed_origin_rejects_recovery(cluster):
    stage = submit(cluster, module())
    cluster.nodes["ivan-u24"] = Node("ivan-u24", "http://replacement.ts.net:8789", "/private/u24")
    with pytest.raises(SubmissionUnknown, match="地址改变"):
        cluster.execution(stage["execution_id"])


def test_completion_releases_route_and_download_stays_on_owner(cluster, tmp_path):
    stage = submit(cluster, module())
    client = cluster.clients[stage["node_id"]]
    client.jobs[stage["execution_id"]]["status"] = "completed"
    job = cluster.execution(stage["execution_id"])
    result = cluster._download_output(job, tmp_path / "clip.mp4", time.monotonic())
    assert result["source"] == client.url
    assert result["node_id"] == "ivan-u24"
    assert not cluster.routes.active()


def test_concurrent_reservations_cannot_use_same_slot(cluster):
    node = cluster.nodes["ivan-u24"]
    barrier = threading.Barrier(8)
    def reserve(i):
        barrier.wait()
        return cluster.routes.reserve(str(i), [(node, {"active": 0, "execution_ids": set(), "slots": 1})], "A4")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(r is not None for r in results) == 1


def test_submission_claim_is_atomic(cluster):
    node = cluster.nodes["ivan"]
    cluster.routes.reserve("one", [(node, {"active": 0, "execution_ids": set(), "slots": 1})], "A4")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cluster.routes.claim_submission("one"), range(8)))
    assert sum(results) == 1


@pytest.mark.parametrize("change", [{"available": False}, {"resources_ok": False}, {"counts_complete": False},
                                   {"exclusive_window": True}, {"recipe_capacity": {}},
                                   {"recipe_capacity": {"A4": {"eligible_lanes": [], "available_slots": 2}}}])
def test_failed_gates_never_select_node(cluster, change):
    s = {**snapshot(), **change}
    assert cluster.eligible(cluster.nodes["ivan"], s, "A4") is None


def test_remote_zero_capacity_prevents_second_route(cluster):
    stage = submit(cluster, module(target="ivan"))
    cluster.clients["ivan"].state = snapshot(1, [stage["execution_id"]], 0)
    capacity = cluster.capacity()
    assert capacity["recipe_capacity"]["A4"]["available_slots"] == 1  # u24 only


def test_node_batch_lease_leaves_other_host_available(cluster):
    cluster.begin_batch("batch")
    assert cluster.routes.batch("batch")["node_id"] == "ivan-u24"
    assert submit(cluster, module())["node_id"] == "ivan"
    cluster.end_batch("batch")
    assert cluster.routes.batch("batch") is None


def test_config_rejects_arbitrary_urls_and_capacity(tmp_path):
    path = tmp_path / "nodes.json"
    good = {"id": "ivan", "url": "http://ivan.ts.net:8789", "key_file": "/private/key", "max_parallel": 1}
    for change in ({"url": "http://evil.example:8789"}, {"url": "http://key@ivan.ts.net:8789"},
                   {"max_parallel": 4}, {"key_file": "relative"}, {"enabled": "false"}):
        path.write_text(json.dumps({"version": 1, "nodes": [{**good, **change}]}))
        with pytest.raises(ValueError):
            read_nodes(path)


def test_config_represents_all_three_hosts_at_physical_capacity(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"version": 1, "legacy_node": "ivan", "nodes": [
        {"id": "ivan", "url": "http://ivan.ts.net:8789", "key_file": "/private/ivan",
         "enabled": False, "max_parallel": 2},
        {"id": "ivan-u24", "url": "http://ivan-u24.ts.net:8789", "key_file": "/private/u24",
         "enabled": True, "max_parallel": 1},
        {"id": "edge", "url": "http://edge.ts.net:18789", "key_file": "/private/edge",
         "enabled": False, "max_parallel": 1},
    ]}))
    nodes, legacy = read_nodes(path)
    assert legacy == "ivan"
    assert [(node.id, node.max_parallel) for node in nodes] == [
        ("ivan", 2), ("ivan-u24", 1), ("edge", 1),
    ]


def test_unbound_write_cannot_hit_legacy_fleet(cluster):
    with pytest.raises(RuntimeError, match="明确"):
        cluster._request("POST", "/api/router/release-validation-gate", {})
    assert not cluster.clients["ivan"].calls


def test_disabled_legacy_does_not_poison_enabled_recipe_capacity(cluster):
    from dataclasses import replace
    cluster.nodes["ivan"] = replace(cluster.nodes["ivan"], enabled=False)
    cluster.clients["ivan"].state["resources_ok"] = False
    cluster.clients["ivan"].state["recipe_capacity"]["A4"]["recipe_version"] = "retired-version"
    capacity = cluster.capacity()
    assert capacity["available"] is True and capacity["resources_ok"] is True
    assert capacity["recipe_capacity"]["A4"]["recipe_version"] == "v1"
    assert capacity["recipe_capacity"]["A4"]["available_slots"] == 1
    assert capacity["recipe_capacity"]["A4"]["eligible_lanes"] == ["ivan-u24:main"]
    assert any(n["id"] == "ivan" and not n["enabled"] for n in capacity["nodes"])


def test_disabled_healthy_node_does_not_mask_enabled_node_outage(cluster):
    from dataclasses import replace
    cluster.nodes["ivan"] = replace(cluster.nodes["ivan"], enabled=False)
    cluster.clients["ivan-u24"].state["available"] = False
    assert cluster.capacity()["available"] is False
