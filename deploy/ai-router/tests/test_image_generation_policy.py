from __future__ import annotations

import asyncio
import copy
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from ai_router.config import Settings
from ai_router.image_generation import snapshot, validate_image_generation
from ai_router.media_service.app import create_app
from ai_router.media_service.contracts import MediaError, UnknownOutcome, image_request
from ai_router.media_service.providers import CodexProvider
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore
from ai_router.policy_config import PolicyConfigManager


def endpoint(name="nx5-image", resource="nx5_gpu"):
    return {"id": name, "resource_id": resource, "url": "http://127.0.0.1:18190", "adapter": "comfy-image",
            "model": "qwen-image-2.1", "key_env": "TEST_IMAGE_EXECUTOR_KEY", "enabled": True, "qualified": True,
            "capabilities": [{"mode": "t2i", "recipe": "qwen-landscape-v1", "aspect_ratios": ["landscape", "auto"],
                              "backgrounds": ["auto", "opaque"], "max_images": 0}]}


def png():
    buf = io.BytesIO()
    Image.new("RGB", (128, 72), "green").save(buf, format="PNG")
    return buf.getvalue()


class Peer:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.drop_ack = False
        self.get_unknown = False
        self.status = "completed"
        self.posts = []
        self.outputs = png()

    def __call__(self, request):
        if request.url.path == "/capabilities":
            return httpx.Response(200, json={"ready": self.ready, "busy": self.busy, "recipes": ["qwen-landscape-v1"]})
        if request.method == "POST" and request.url.path == "/executions":
            self.posts.append(json.loads(request.content))
            if self.drop_ack:
                raise httpx.ReadTimeout("lost acknowledgement")
            return httpx.Response(202, json={"status": "queued"})
        if request.url.path.endswith("/output"):
            return httpx.Response(200, content=self.outputs)
        if self.get_unknown:
            return httpx.Response(404)
        return httpx.Response(200, json={"status": self.status})


def setup(tmp_path, monkeypatch, endpoints=None):
    monkeypatch.setenv("TEST_IMAGE_EXECUTOR_KEY", "test-only")
    peer = Peer()
    store = MediaStore(tmp_path)
    store.configure({"enabled": True, "codex_ready": True, "h3_ready": True, "min_free_bytes": 0})
    service = MediaService(store, client=httpx.AsyncClient(transport=httpx.MockTransport(peer)),
                           executors=endpoints if endpoints is not None else [endpoint()])
    return service, peer


def submit(service, *, idem="test", account=None, config=None, **body):
    return service.submit("owner", "image", {"prompt": "SIYUAN castle", "aspect_ratio": "landscape", **body},
                          idem, "request-test", policy=account, generation=snapshot(config or {}, 7))


@pytest.mark.parametrize("value", [{"allow_cloud": "yes"}, {"queue_timeout": 0}, {"queue_limit": True},
    {"local_resources": ["nx5-image", "nx5-image"]}, {"unknown": 1},
    {"default_route": "cloud_only", "allow_cloud": False}, {"paid_fallback": True, "allow_cloud": False}])
def test_invalid_policy(value):
    with pytest.raises(ValueError):
        validate_image_generation(value)


@pytest.mark.parametrize("body", [{"execution": "anything"}, {"model": "qwen-image-2.1", "execution": "cloud"},
                                    {"model": "qwen-image-3.0-pro", "execution": "local"}])
def test_explicit_channel_conflict(body):
    with pytest.raises(MediaError):
        image_request({"prompt": "x", **body})


def test_local_generation_and_private_events(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    job = submit(service)
    asyncio.run(service.direct.step(job["id"]))
    result = service.store.get(job["id"])
    assert result["status"] == "completed"
    assert len(peer.posts) == 1
    assert peer.posts[0]["request"]["prompt"] == "SIYUAN castle"
    assert Path(result["output"]["path"]).read_bytes() == peer.outputs
    public = service.public(result)
    assert public["policy_revision"] == 7 and public["execution"] == "local"
    assert "provider_state" not in public and "image_generation" not in public
    events = service.store.image_events(job["id"])["data"]
    assert any(item.get("executor") == "nx5-image" for item in events)
    assert "127.0.0.1" not in json.dumps(events)


@pytest.mark.parametrize("config,account,execution,expected", [
    ({}, {}, None, "cloud"),
    ({"allow_cloud": False}, {}, None, "local"),
    ({}, {"allow_cloud": False}, None, "local"),
    ({"default_route": "local_only"}, {}, None, "local"),
    ({}, {}, "local", "local"),
    ({"when_busy": "queue"}, {}, None, "local"),
])
def test_busy_policy_and_permissions(tmp_path, monkeypatch, config, account, execution, expected):
    service, peer = setup(tmp_path, monkeypatch)
    peer.ready, peer.busy = False, True
    job = submit(service, config=config, account=account, **({"execution": execution} if execution else {}))
    asyncio.run(service.direct.step(job["id"]))
    result = service.store.get(job["id"])
    assert (result.get("execution_kind") is None) == (expected == "cloud")
    assert not peer.posts


@pytest.mark.parametrize("action,expected", [("queue", "queued"), ("error", "failed"), ("cloud", "queued")])
def test_unavailable_is_distinct_from_busy(tmp_path, monkeypatch, action, expected):
    service, peer = setup(tmp_path, monkeypatch)
    peer.ready = False
    job = submit(service, config={"when_unavailable": action})
    asyncio.run(service.direct.step(job["id"]))
    result = service.store.get(job["id"])
    assert result["status"] == expected
    assert (result.get("execution_kind") is None) == (action == "cloud")


@pytest.mark.parametrize("account,config", [({"allow_cloud": False}, {}), ({}, {"allow_cloud": False})])
def test_explicit_cloud_cannot_bypass_permissions(tmp_path, monkeypatch, account, config):
    service, peer = setup(tmp_path, monkeypatch)
    with pytest.raises(MediaError, match="not permitted"):
        submit(service, execution="cloud", account=account, config=config)
    assert not peer.posts


def test_lost_ack_restart_preserves_original_execution(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    peer.drop_ack = True
    job = submit(service)
    asyncio.run(service.direct.step(job["id"]))
    assert service.store.get(job["id"])["status"] == "reconciling"
    replacement = MediaService(MediaStore(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(peer)), executors=[endpoint()])
    asyncio.run(replacement.direct.step(job["id"]))
    assert replacement.store.get(job["id"])["status"] == "completed"
    assert len(peer.posts) == 1


def test_unknown_result_keeps_resource_and_never_reposts(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    peer.drop_ack = True
    peer.get_unknown = True
    job = submit(service, execution="local")
    asyncio.run(service.direct.step(job["id"]))
    asyncio.run(service.direct.step(job["id"]))
    current = service.store.get(job["id"])
    assert current["status"] == "reconciling" and current["provider_state"]["endpoint"]
    assert len(peer.posts) == 1
    second = submit(service, idem="second", execution="local")
    asyncio.run(service.direct.step(second["id"]))
    assert service.store.get(second["id"])["status"] == "queued"
    assert len(peer.posts) == 1


def test_policy_snapshot_and_idempotency_survive_changes(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    job = submit(service, config={"allow_cloud": False})
    repeat = submit(service, config={"allow_cloud": True})
    assert job["id"] == repeat["id"]
    assert repeat["image_generation"]["policy"]["allow_cloud"] is False
    with pytest.raises(MediaError) as error:
        submit(service, prompt="changed")
    assert error.value.code == "idempotency_conflict"


def test_quota_and_queue_deadline(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    job = submit(service, account={"image_max_active": 1}, config={"queue_timeout": 1})
    with pytest.raises(MediaError) as error:
        submit(service, idem="second", account={"image_max_active": 1})
    assert error.value.code == "media_account_busy"
    service.store.update(job["id"], created_at=time.time() - 2)
    asyncio.run(service.direct.step(job["id"]))
    assert service.store.get(job["id"])["error"]["code"] == "media_queue_timeout"
    assert not peer.posts


def test_cancel_and_completed_race_archives_original(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    peer.drop_ack = True
    job = submit(service)
    asyncio.run(service.direct.step(job["id"]))
    asyncio.run(service.cancel_image(job["id"], "owner"))
    assert service.store.get(job["id"])["status"] == "cancelling"
    asyncio.run(service.direct.step(job["id"]))
    assert service.store.get(job["id"])["status"] == "completed"
    assert len(peer.posts) == 1


def test_two_cards_and_fifo_queue(tmp_path, monkeypatch):
    endpoints = [endpoint(), endpoint("nx6-image", "nx6_gpu")]
    service, peer = setup(tmp_path, monkeypatch, endpoints)
    peer.status = "running"
    config = {"local_resources": [e["id"] for e in endpoints], "when_busy": "queue"}
    jobs = [submit(service, idem=str(i), config=config) for i in range(3)]
    async def run():
        await asyncio.gather(*(service.direct.step(j["id"]) for j in jobs))
    asyncio.run(run())
    assert len(peer.posts) == 2
    states = [service.store.get(j["id"]) for j in jobs]
    assert {j["provider_state"]["endpoint"]["resource_id"] for j in states[:2]} == {"nx5_gpu", "nx6_gpu"}
    assert states[2]["status"] == "queued"


def test_output_corruption_fails_without_replay(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    peer.outputs = b"broken image"
    job = submit(service)
    asyncio.run(service.direct.step(job["id"]))
    assert service.store.get(job["id"])["status"] == "failed"
    asyncio.run(service.direct.step(job["id"]))
    assert len(peer.posts) == 1


def test_moderation_does_not_become_generic_or_copyright():
    with pytest.raises(MediaError) as error:
        CodexProvider._result({"status": "failed", "failure": {"code": "moderation_blocked"}})
    assert error.value.code == "image_moderation_blocked"
    with pytest.raises(MediaError) as error:
        CodexProvider._result({"status": "failed", "failure": None})
    assert "without a structured reason" in str(error.value)


def test_policy_draft_activation_rollback(tmp_path):
    settings = Settings(runtime_path=tmp_path / "settings.yaml")
    manager = PolicyConfigManager(tmp_path / "policy.sqlite3", settings)
    async def run():
        original = (await manager.snapshot())["active"]
        draft = await manager.patch_draft({"image_generation": {"allow_cloud": False}},
            expected_revision=original["revision"], expected_fingerprint=original["settings_fingerprint"], source="test")
        assert "image_generation" not in settings.value
        draft = await manager.validate_draft([], expected_revision=draft["revision"],
            expected_fingerprint=draft["settings_fingerprint"], source="test")
        await manager.activate(expected_revision=draft["revision"], expected_fingerprint=draft["settings_fingerprint"], source="test")
        assert settings.section("image_generation")["allow_cloud"] is False
        active = (await manager.snapshot())["active"]
        rollback = await manager.rollback(original["revision"], expected_active_revision=active["revision"],
            expected_active_fingerprint=active["settings_fingerprint"], source="test")
        assert rollback["status"] == "draft"
        assert settings.section("image_generation")["allow_cloud"] is False
    asyncio.run(run())

# Exercise the real Router -> trusted media service boundary with an isolated peer.
from test_media_async_prefer import media_gateway


def test_gateway_uses_active_policy_not_caller_headers(media_gateway, tmp_path):
    async def run():
        async with media_gateway() as env:
            settings = Settings(runtime_path=tmp_path / "runtime-image.yaml")
            settings.write_runtime({"image_generation": {"allow_cloud": False, "default_route": "local_only"}})
            env.runtime.settings = settings
            env.runtime.policy_config = PolicyConfigManager(tmp_path / "active-image.sqlite3", settings)
            response = await env.client.post(env.prefix + "/images/generations",
                json={"prompt": "test", "execution": "cloud"}, headers={"Prefer": "respond-async",
                    "X-Image-Generation": json.dumps(snapshot({"allow_cloud": True})),
                    "X-Media-Policy": '{"allow_cloud":true}'})
            assert response.status_code == 403, response.text
            assert env.svc.codex.calls == env.svc.qwen.calls == 0
            response = await env.client.get(env.prefix + ("/options" if env.admin else "/media/options"))
            assert response.json()["image_generation"]["allow_cloud"] is False
            assert "local_resources" not in response.json()["image_generation"]
    asyncio.run(run())


def test_receipt_access_and_events(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-internal")
    job = submit(service)
    async def run():
        await service.direct.step(job["id"])
        output = service.store.get(job["id"])["output"]
        headers = {"Authorization": "Bearer test-internal", "X-Media-Owner": "owner"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service, run_worker=False)),
                                     base_url="http://test", headers=headers) as client:
            path = f"/jobs/{job['id']}/delivery"
            body = {"artifact_id": output["id"], "sha256": output["sha256"]}
            assert (await client.post(path, json=body, headers={"X-Media-Owner": "other"})).status_code == 404
            assert (await client.post(path, json={**body, "sha256": "0" * 64})).status_code == 409
            assert (await client.post(path, json=body)).status_code == 200
            count = len(service.store.image_events(job["id"])["data"])
            assert (await client.post(path, json=body)).status_code == 200
            assert len(service.store.image_events(job["id"])["data"]) == count
            assert (await client.get(f"/jobs/{job['id']}/events")).status_code == 403
            assert (await client.get(f"/jobs/{job['id']}/events", headers={"X-Media-Admin": "true"})).status_code == 200
    asyncio.run(run())


def test_image_workflow_inherits_policy_without_public_leak(tmp_path, monkeypatch):
    service, peer = setup(tmp_path, monkeypatch)
    flow = service.creative.create("owner", {"kind": "image", "prompt": "SIYUAN", "aspect_ratio": "16:9"},
                                   "flow", policy={"allow_cloud": False}, generation=snapshot({"allow_cloud": False}, 3))
    job = service.creative.submit_image(flow, "SIYUAN", "child")
    assert job["image_generation"]["revision"] == 3
    assert job["media_policy"]["allow_cloud"] is False
    public = service.creative.public(flow)
    assert "image_generation" not in public and "media_policy" not in public


def test_comfy_lost_ack_and_recipe_snapshot_survive_restart(tmp_path):
    from ai_router.media_service.comfy_adapter import ComfyExecutor
    graph_path = tmp_path / "recipe.json"
    graph_path.write_text(json.dumps({"1": {"class_type": "Fixture", "inputs": {"prompt": "", "seed": 0, "width": 1, "height": 1}}}))
    config = {"version": 1, "comfy_url": "http://127.0.0.1:8188", "recipes": [{"id": "recipe-v1", "model": "qwen-image-2.1",
        "mode": "t2i", "graph_file": str(graph_path), "bindings": {key: [["1", key]] for key in ("prompt", "seed", "width", "height")},
        "output_node": "1", "aspect_ratios": {"landscape": [1280, 720]}, "backgrounds": ["auto"]}]}
    posted = []
    def peer(request):
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_pending": [], "queue_running": []})
        if request.url.path == "/prompt":
            posted.append(json.loads(request.content))
            raise httpx.ReadTimeout("accepted but reply lost")
        history = {"prompt-original": {"prompt": [0, "prompt-original", {}, {"siyuan_operation_id": "operation-original"}],
            "status": {"completed": True}, "outputs": {"1": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}}}}
        if request.url.path.startswith("/history"):
            return httpx.Response(200, json=history)
        if request.url.path == "/view":
            return httpx.Response(200, content=png())
        raise AssertionError(request.url.path)
    adapter = ComfyExecutor(tmp_path / "adapter", config, client=httpx.AsyncClient(transport=httpx.MockTransport(peer)))
    adapter.accept({"operation_id": "operation-original", "recipe": "recipe-v1", "request": {
        "model": "siyuan-image", "prompt": "SIYUAN", "aspect_ratio": "landscape"}})
    job = adapter.get("operation-original")
    assert job["recipe_snapshot"]["aspect_ratios"]["landscape"] == [1280, 720]
    asyncio.run(adapter.step(job))
    assert adapter.get("operation-original")["status"] == "reconciling"
    restarted = ComfyExecutor(tmp_path / "adapter", config, client=httpx.AsyncClient(transport=httpx.MockTransport(peer)))
    asyncio.run(restarted.step(restarted.get("operation-original")))
    assert restarted.get("operation-original")["status"] == "completed"
    assert len(posted) == 1
    assert posted[0]["prompt"]["1"]["inputs"]["width"] == 1280
    assert posted[0]["prompt"]["1"]["inputs"]["height"] == 720


def test_unqualified_nodes_cannot_execute_but_obey_unavailable_policy(tmp_path, monkeypatch):
    node = {**endpoint(), "enabled": False, "qualified": False}
    service, peer = setup(tmp_path, monkeypatch, [node])
    job = submit(service, config={"when_unavailable": "cloud"})
    asyncio.run(service.direct.step(job["id"]))
    assert service.store.get(job["id"]).get("execution_kind") is None
    assert not peer.posts
    with pytest.raises(MediaError) as error:
        submit(service, idem="unsupported", aspect_ratio="portrait")
    assert error.value.code == "media_no_compatible_executor"
