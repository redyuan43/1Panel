"""Video admission and recovery on the image-policy production baseline."""
import asyncio
import base64
import importlib.util
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from ai_router.media_service.contracts import MediaError
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore
from ai_router.media_service.video_direct import SINGLE, load_pool
from test_media_async_prefer import media_gateway
from test_media_service import FakeImage, png


def endpoint():
    return {"id": "ivan-h3", "adapter": "h3", "url": "http://100.96.79.21:19390",
            "key_env": "VIDEO_TEST_KEY", "resource_id": "ivan_h3_pool", "model": "minimax-h3",
            "enabled": True, "qualified": True, "priority": 100, "max_parallel": 2,
            "capabilities": [{"mode": "i2v", "profile": "quality",
                              "recipe_id": "h3-i2va-480p15-3060-v1",
                              "durations": [15], "aspect_ratios": ["16:9"]}]}


def service(tmp_path, handler):
    store = MediaStore(tmp_path)
    store.configure({"enabled": True, "videos_enabled": True, "h3_ready": False, "min_free_bytes": 0})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = MediaService(store, client=client, codex=FakeImage(), qwen=FakeImage(), executors=[])
    svc.video_direct.executors = [endpoint()]
    return svc


def body(**changes):
    return {"model": "siyuan-video", "workflow_mode": SINGLE, "prompt": "one scene",
            "mode": "i2v", "duration": 15, "aspect_ratio": "16:9",
            "assets": {"first_frame": {"data": base64.b64encode(png()).decode(),
                                       "content_type": "image/png"}}, **changes}


def test_exact_video_options_and_account_admission(tmp_path):
    async def scenario():
        svc = service(tmp_path, lambda request: httpx.Response(503))
        options = await svc.options(["siyuan-video"])
        assert options["single_generation"]["video_capabilities"] == [
            {"mode": "i2v", "duration": 15, "aspect_ratio": "16:9"}]
        assert (await svc.options(["minimax-h3"]))["single_generation"]["video_capabilities"] == options["single_generation"]["video_capabilities"]
        assert (await svc.options(["siyuan-image"]))["single_generation"]["video_capabilities"] == []
        for change in ({"duration": 14}, {"aspect_ratio": "9:16"}, {"mode": "t2v", "assets": {}}):
            with pytest.raises(MediaError) as error:
                svc.submit("alice", "video", body(**change), json.dumps(change), "req")
            assert error.value.code == "media_no_compatible_executor"
            assert error.value.status == 422
        svc.video_direct.executors = [{**endpoint(), "qualified": False}]
        with pytest.raises(MediaError) as unavailable:
            svc.submit("alice", "video", body(), "unqualified", "req")
        assert unavailable.value.code == "media_no_compatible_executor"
        assert unavailable.value.status == 503
        svc.video_direct.executors = [endpoint()]
        with pytest.raises(MediaError) as error:
            svc.submit("alice", "video", body(), "quota", "req", policy={"video_max_seconds": 10})
        assert error.value.code == "media_duration_limit"
        job = svc.submit("alice", "video", body(), "same", "req", policy={"video_max_active": 1})
        assert job["execution_kind"] == SINGLE and job["stages"] == []
        svc.video_direct.executors = []
        assert svc.submit("alice", "video", body(), "same", "req")["id"] == job["id"]
        await svc.close()
    asyncio.run(scenario())


def test_two_slots_no_duplicate_post_and_confirmed_queue_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_TEST_KEY", "test-only")
    posts, options_reads = [], []
    statuses = {}

    def handler(request):
        path = request.url.path
        if path.endswith("/options"):
            options_reads.append(1)
            return httpx.Response(200, json={"contract_version": 1, "workflow_contract_version": 2,
                                             "mode": ["i2v"], "managed_recipes": ["h3-i2va-480p15-3060-v1"]})
        if path.endswith("/capacity"):
            return httpx.Response(200, json={"active": [], "queues": [],
                                             "policy": {"quality480_i2v": {"max_parallel": 2}}})
        if request.method == "POST" and path.endswith("/cancel"):
            identifier = path.split("/")[-2]
            statuses[identifier] = "cancelled"
            return httpx.Response(200, json={"status": "cancelling"})
        if request.method == "POST":
            fields = request.content.decode(errors="ignore")
            identifier = next(item for item in statuses if item in fields)
            posts.append(identifier)
            return httpx.Response(202, json={"status": "submitted"})
        identifier = path.split("/")[-1]
        return httpx.Response(200, json={"status": statuses[identifier]})

    async def scenario():
        svc = service(tmp_path, handler)
        jobs = [svc.submit("alice", "video", body(prompt=str(i)), str(i), "req") for i in range(3)]
        statuses.update({job["id"]: "queued" for job in jobs})
        await asyncio.gather(*(svc.video_direct.step(job["id"]) for job in jobs))
        assert len(posts) == 2 and len(set(posts)) == 2
        assert [svc.store.get(job["id"])["status"] for job in jobs].count("queued") == 1
        assert len(options_reads) == 3  # One eligibility read per queued request; none after submission marker.
        first = jobs[0]["id"]
        svc.store.update(first, created_at=time.time() - 3601)
        await svc.video_direct.step(first)
        assert svc.store.get(first)["status"] == "cancelling"
        await svc.video_direct.step(first)
        assert svc.store.get(first)["error"]["code"] == "media_queue_timeout"
        assert posts.count(first) == 1
        await svc.close()
    asyncio.run(scenario())


def test_client_saved_receipt_survives_capability_removal(tmp_path):
    path = Path(__file__).parents[1] / "integrations/comfyui/siyuan_media/client.py"
    spec = importlib.util.spec_from_file_location("video_client_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    posts, reads = [], []
    qualified = True

    def handler(request):
        if request.url.path.endswith("/media/options"):
            reads.append(1)
            return httpx.Response(200, json={"models": ["siyuan-video"], "single_generation": {
                "video_capabilities": ([{"mode": "t2v", "duration": 15, "aspect_ratio": "16:9"}]
                                       if qualified else [])}})
        if request.method == "POST":
            posts.append(1)
            return httpx.Response(202, json={"id": "vid_original"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"synthetic", headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"id": "vid_original", "status": "completed"})

    client = module.MediaClient(tmp_path, base="http://127.0.0.1:8080/v1", key="test-only",
                                transport=httpx.MockTransport(handler))
    request = {"model": "siyuan-video", "workflow_mode": SINGLE, "prompt": "one", "mode": "t2v",
               "duration": 15, "aspect_ratio": "16:9"}
    client.generate("video", request, "original")
    qualified = False
    assert client.generate("video", request, "original")[1] == "vid_original"
    assert len(posts) == len(reads) == 1
    with pytest.raises(ValueError, match="尚未验收开放"):
        client.generate("video", request, "new")
    client.close()


def test_pool_rejects_unqualified_dimensions(tmp_path):
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"version": 1, "executors": [endpoint()]}))
    assert load_pool(str(config))[0]["max_parallel"] == 2
    value = endpoint()
    value["capabilities"][0]["durations"] = [16]
    config.write_text(json.dumps({"version": 1, "executors": [value]}))
    with pytest.raises(ValueError, match="Invalid H3 qualified capability"):
        load_pool(str(config))


def test_video_gateway_forwards_trusted_account_policy_without_image_snapshot(media_gateway):
    async def scenario():
        async with media_gateway() as env:
            await env.client.post(env.prefix + "/videos", files=[("prompt", (None, "one scene"))])
            forwarded = next(request for request in env.requests if request.url.path == "/jobs/video")
            assert forwarded.headers["x-media-policy"] == "{}"
            assert "x-image-generation" not in forwarded.headers
    asyncio.run(scenario())


def test_completed_video_is_archived_and_reused_by_original_id(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("CPU video fixture requires ffmpeg")
    monkeypatch.setenv("VIDEO_TEST_KEY", "test-only")
    source = tmp_path / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=64x36:rate=4:duration=15", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=15", "-c:v", "libx264", "-c:a", "aac",
                    str(source)], check=True)
    posts = []

    def handler(request):
        path = request.url.path
        if path.endswith("/options"):
            return httpx.Response(200, json={"contract_version": 1, "workflow_contract_version": 2,
                                             "mode": ["i2v"], "managed_recipes": ["h3-i2va-480p15-3060-v1"]})
        if path.endswith("/capacity"):
            return httpx.Response(200, json={"active": [], "queues": [],
                                             "policy": {"quality480_i2v": {"max_parallel": 2}}})
        if request.method == "POST":
            posts.append(1)
            return httpx.Response(202, json={"status": "submitted"})
        if path.endswith("/output"):
            return httpx.Response(200, content=source.read_bytes(), headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"status": "completed", "actual_duration": 15.0})

    async def scenario():
        svc = service(tmp_path / "state", handler)
        job = svc.submit("alice", "video", body(), "same", "req")
        await svc.video_direct.step(job["id"])
        finished = svc.store.get(job["id"])
        assert finished["status"] == "completed" and finished["output"]["content_type"] == "video/mp4"
        assert svc.public(finished)["stages"] == []
        assert svc.submit("alice", "video", body(), "same", "req")["id"] == job["id"]
        assert posts == [1]
        await svc.close()
    asyncio.run(scenario())
