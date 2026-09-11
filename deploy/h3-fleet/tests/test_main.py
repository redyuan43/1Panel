from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import sys

import httpx
import pytest


LANES = """[
  {
    "id": "fast",
    "url": "http://127.0.0.1:8188",
    "gpu_uuid": "GPU-fast",
    "device": "RTX 4060 Ti"
  },
  {
    "id": "main",
    "url": "http://127.0.0.1:8189",
    "gpu_uuid": "GPU-main",
    "device": "RTX 3060"
  },
  {
    "id": "preview",
    "url": "http://127.0.0.1:8190",
    "gpu_uuid": "GPU-preview",
    "device": "RTX 3060",
    "preview_only": true,
    "enabled": true
  }
]"""


def load_module(tmp_path: Path):
    os.environ["H3_FLEET_LANES"] = LANES
    os.environ["H3_FLEET_DATABASE"] = str(tmp_path / "fleet.sqlite3")
    os.environ["H3_LANE_DATA_ROOT"] = str(tmp_path / "lanes")
    os.environ["H3_ROUTER_KEY"] = "test-router-key"
    preview = tmp_path / "preview.json"
    quality = tmp_path / "quality.json"
    preview.write_text(json.dumps({
        "1": {"class_type": "UNETLoader", "inputs": {}},
        "2": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {}},
        "3": {"class_type": "RandomNoise", "inputs": {}},
        "4": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {}},
        "5": {"class_type": "SaveVideo", "inputs": {}},
    }), encoding="utf-8")
    quality.write_text(json.dumps({
        "1": {"class_type": "UNETLoader", "inputs": {}},
        "2": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {}},
        "3": {"class_type": "RandomNoise", "inputs": {}},
        "4": {"class_type": "BasicScheduler", "inputs": {}},
        "5": {"class_type": "SaveVideo", "inputs": {}},
    }), encoding="utf-8")
    os.environ["H3_TURBO_TEMPLATE"] = str(preview)
    os.environ["H3_QUALITY_TEMPLATE"] = str(quality)
    if "app.main" in sys.modules:
        module = importlib.reload(sys.modules["app.main"])
    else:
        module = importlib.import_module("app.main")
    module.fleet.store.initialize()
    module.resource_snapshot = lambda: {
        "ok": True, "timestamp": module.time.time(),
        "memory_available_bytes": 80 * 1024**3, "swap_used_bytes": 0,
        "root_available_bytes": 50 * 1024**3, "offload_available_bytes": 60 * 1024**3,
        "cgroup_current_bytes": 16 * 1024**3, "cgroup_swap_bytes": 0, "cgroup_events": {},
    }
    return module


def test_classify_defaults_preview(tmp_path: Path) -> None:
    module = load_module(tmp_path)
    assert module.classify({"prompt": {}}) == ("preview", "preview")


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("url", "duplicate lane URLs"),
        ("gpu_uuid", "duplicate GPU UUIDs"),
    ],
)
def test_load_lanes_rejects_duplicate_physical_lanes(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    values = json.loads(LANES)
    values[1][field] = values[0][field]
    os.environ["H3_FLEET_LANES"] = json.dumps(values)
    os.environ["H3_FLEET_DATABASE"] = str(tmp_path / "fleet.sqlite3")
    with pytest.raises(RuntimeError, match=message):
        if "app.main" in sys.modules:
            importlib.reload(sys.modules["app.main"])
        else:
            importlib.import_module("app.main")
    os.environ["H3_FLEET_LANES"] = LANES


def test_classify_quality_stage(tmp_path: Path) -> None:
    module = load_module(tmp_path)
    assert module.classify(
        {
            "prompt": {},
            "extra_data": {"h3": {"stage": "local_768", "profile": "quality"}},
        }
    ) == ("local_768", "quality")


def test_job_store_tracks_output(tmp_path: Path) -> None:
    module = load_module(tmp_path)
    store = module.JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    store.create(
        prompt_id="external",
        upstream_prompt_id="upstream",
        execution_id=None,
        request_digest=None,
        lane_id="fast",
        stage="preview",
        profile="preview",
    )
    store.update(
        "external",
        status="completed",
        output_filename="result.mp4",
        output_subfolder="",
        output_type="output",
    )
    assert store.find_output("result.mp4", "", "output")["lane_id"] == "fast"


def test_new_job_is_active_for_lane(tmp_path: Path) -> None:
    module = load_module(tmp_path)
    store = module.JobStore(tmp_path / "active.sqlite3")
    store.initialize()
    store.create(
        prompt_id="external",
        upstream_prompt_id="upstream",
        execution_id=None,
        request_digest=None,
        lane_id="fast",
        stage="preview",
        profile="preview",
    )
    assert store.active_for_lane("fast")[0]["prompt_id"] == "external"


def test_cleanup_input_files_is_scoped_to_generated_anchor_names(tmp_path: Path) -> None:
    module = load_module(tmp_path)
    generated = "h3exec_abc123_first_frame.png"
    for lane in module.fleet.lanes:
        directory = tmp_path / "lanes" / lane.id / "input"
        directory.mkdir(parents=True)
        (directory / generated).write_bytes(b"anchor")
        (directory / "customer-upload.png").write_bytes(b"keep")
    module.fleet.cleanup_input_files([generated, "customer-upload.png", "../escape.png"])
    for lane in module.fleet.lanes:
        directory = tmp_path / "lanes" / lane.id / "input"
        assert not (directory / generated).exists()
        assert (directory / "customer-upload.png").read_bytes() == b"keep"


def test_find_video_output() -> None:
    os.environ["H3_FLEET_LANES"] = LANES
    module = importlib.import_module("app.main")
    assert module.find_video_output(
        {
            "outputs": {
                "1": {
                    "videos": [
                        {
                            "filename": "h3.mp4",
                            "subfolder": "test",
                            "type": "output",
                        }
                    ]
                }
            }
        }
    ) == {"filename": "h3.mp4", "subfolder": "test", "type": "output"}


def test_systemd_units_have_per_lane_and_aggregate_memory_guards() -> None:
    root = Path(__file__).resolve().parents[1] / "systemd"
    worker = (root / "comfyui-h3@.service").read_text(encoding="utf-8")
    aggregate = (root / "h3-compute.slice").read_text(encoding="utf-8")
    fleet_service = (root / "h3-fleet.service").read_text(encoding="utf-8")
    assert "Slice=h3-compute.slice" in worker
    assert "MemoryMax=50G" in worker
    assert "OOMPolicy=stop" in worker
    assert "MemoryHigh=72G" in aggregate
    assert "MemoryMax=80G" in aggregate
    assert "MemorySwapMax=8G" in aggregate
    assert "WorkingDirectory=/mnt/ivan-ext4-offload/h3-fleet\n" in fleet_service
    assert "uvicorn app.main:app" in fleet_service


class FakeComfyClient:
    def __init__(self) -> None:
        self.submissions: list[str] = []
        self.payloads: list[dict] = []
        self.interrupts: list[str] = []
        self.queue_deletes: list[str] = []
        self.uploads: list[str] = []
        self.history: dict[str, dict] = {}
        self.pending: set[str] = set()
        self.running: set[str] = set()

    async def get(self, url: str, **kwargs):
        if url.endswith("/system_stats"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"devices": [{"name": "GPU"}]},
            )
        if "/history/" in url:
            prompt_id = url.rsplit("/", 1)[-1]
            value = self.history.get(prompt_id)
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={prompt_id: value} if value else {},
            )
        if url.endswith("/queue"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "queue_running": [[0, prompt_id] for prompt_id in self.running],
                    "queue_pending": [[0, prompt_id] for prompt_id in self.pending],
                },
            )
        raise AssertionError(url)

    async def post(self, url: str, json=None, **kwargs):
        if url.endswith("/prompt"):
            prompt_id = f"upstream-{len(self.submissions) + 1}"
            self.submissions.append(prompt_id)
            self.pending.add(prompt_id)
            self.payloads.append(json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"prompt_id": prompt_id},
            )
        if url.endswith("/interrupt"):
            self.interrupts.append(url)
            self.running.clear()
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"ok": True},
            )
        if url.endswith("/queue"):
            for prompt_id in (json or {}).get("delete", []):
                self.queue_deletes.append(prompt_id)
                self.pending.discard(prompt_id)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"ok": True},
            )
        if url.endswith("/upload/image"):
            self.uploads.append(str((json or {}).get("type")))
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"ok": True},
            )
        raise AssertionError(url)

    async def aclose(self) -> None:
        pass


def payload(execution_id: str, profile: str = "preview") -> dict:
    return {
        "prompt": {
            "1": {"class_type": "MiniMaxH3AudioConditioningT8" if profile == "preview" else "MiniMaxH3ImageToVideo",
                  "inputs": {"length": 124, "width": 864 if profile == "preview" else 1344,
                             "height": 480 if profile == "preview" else 768}},
            "2": {"class_type": "MiniMaxH3DualClockSamplerT8" if profile == "preview" else "BasicScheduler",
                  "inputs": {"steps": 6 if profile == "preview" else 14}},
        },
        "extra_data": {
            "h3": {
                "execution_id": execution_id,
                "stage": "preview" if profile == "preview" else "local_768",
                "profile": profile,
            }
        },
    }


def test_three_preview_jobs_use_three_distinct_lanes(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        results = [
            await module.submit_prompt(payload(f"preview-{index}"))
            for index in range(3)
        ]
        assert [item["h3_lane"] for item in results] == ["fast", "main", "preview"]

    asyncio.run(scenario())


def test_quality_jobs_exclude_preview_lane(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        module.fleet.client = FakeComfyClient()
        assert (await module.submit_prompt(payload("quality-1", "quality")))["h3_lane"] == "fast"
        assert (await module.submit_prompt(payload("quality-2", "quality")))["h3_lane"] == "main"
        queued = await module.submit_prompt(payload("quality-3", "quality"))
        assert queued["queued"] is True
        assert queued["h3_lane"] is None
        job = module.fleet.store.get(queued["prompt_id"])
        assert job["status"] == "queued"
        assert job["request_json"]

    asyncio.run(scenario())


def test_execution_id_replay_and_conflict(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        client = FakeComfyClient()
        module.fleet.client = client
        first = await module.submit_prompt(payload("same-execution"))
        replay = await module.submit_prompt(payload("same-execution"))
        assert replay["prompt_id"] == first["prompt_id"]
        assert replay["idempotent_replay"] is True
        assert len(client.submissions) == 1
        changed = payload("same-execution")
        changed["prompt"]["1"]["inputs"] = {"seed": 2}
        with pytest.raises(module.HTTPException) as error:
            await module.submit_prompt(changed)
        assert error.value.status_code == 409

    asyncio.run(scenario())


def test_queued_execution_dispatches_after_lane_is_released(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        client = FakeComfyClient()
        module.fleet.client = client
        first = await module.submit_prompt(payload("quality-1", "quality"))
        second = await module.submit_prompt(payload("quality-2", "quality"))
        queued = await module.submit_prompt(payload("quality-3", "quality"))
        assert queued["queued"] is True
        module.fleet.store.update(first["prompt_id"], status="cancelled")
        dispatched = await module.fleet.refresh_job(
            module.fleet.store.get(queued["prompt_id"])
        )
        assert dispatched["status"] == "submitted"
        assert dispatched["lane_id"] == "fast"
        assert len(client.submissions) == 3
        assert module.fleet.store.get(second["prompt_id"])["lane_id"] == "main"

    asyncio.run(scenario())


def test_cancel_rejects_stale_job_version(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        client = FakeComfyClient()
        module.fleet.client = client
        submitted = await module.submit_prompt(payload("cancel-me"))
        job = module.fleet.store.get(submitted["prompt_id"])
        with pytest.raises(module.HTTPException) as error:
            await module.cancel_job(
                submitted["prompt_id"], {"expected_version": job["version"] + 1}
            )
        assert error.value.status_code == 409
        cancelled = await module.cancel_job(
            submitted["prompt_id"], {"expected_version": job["version"]}
        )
        assert cancelled["status"] == "cancelled"
        assert client.queue_deletes == [job["upstream_prompt_id"]]
        assert not client.interrupts

    asyncio.run(scenario())


def test_running_cancel_uses_interrupt_and_confirms_queue_release(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        client = FakeComfyClient()
        module.fleet.client = client
        submitted = await module.submit_prompt(payload("cancel-running"))
        job = module.fleet.store.get(submitted["prompt_id"])
        client.pending.remove(job["upstream_prompt_id"])
        client.running.add(job["upstream_prompt_id"])
        cancelled = await module.cancel_job(submitted["prompt_id"], {})
        assert cancelled["status"] == "cancelled"
        assert len(client.interrupts) == 1
        assert not client.queue_deletes

    asyncio.run(scenario())


def test_router_execution_contract_submits_directly_to_three_lanes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        disk = type("DiskUsage", (), {"free": 50 * 1024**3})()
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: disk)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-router-key"},
        ) as client:
            options = await client.get("/api/router/options")
            assert options.status_code == 200
            assert options.json()["workflow_contract_version"] == 2
            assert (await client.get(
                "/api/router/options",
                headers={"Authorization": "Bearer wrong"},
            )).status_code == 401
            assert (await client.post(
                "/prompt",
                headers={"Authorization": "Bearer wrong"},
                json=payload("unauthorized"),
            )).status_code == 401
            assert (await client.get(
                "/api/health",
                headers={"Authorization": "Bearer wrong"},
            )).status_code == 200
            drained = await client.post("/api/router/drain")
            assert drained.status_code == 200
            assert drained.json()["draining"] is True
            rejected = await client.post("/api/router/executions", data={
                "operation_id": "drained",
                "profile": "preview",
                "mode": "t2v",
                "prompt": "A stable product shot.",
                "duration": "5",
                "seed": "1",
                "audio_policy": "native",
                "aspect_ratio": "9:16",
                "watermark": "false",
            })
            assert rejected.status_code == 503
            module.fleet.draining = False

            async def create(index):
                return await client.post("/api/router/executions", data={
                    "operation_id": f"direct-{index}",
                    "profile": "preview",
                    "mode": "t2v",
                    "prompt": "A stable product shot.",
                    "duration": "5",
                    "seed": str(index),
                    "audio_policy": "native",
                    "aspect_ratio": "9:16",
                    "watermark": "false",
                })

            results = await asyncio.gather(*(create(index) for index in range(3)))
            assert [response.status_code for response in results] == [200, 200, 200]
            assert [response.json()["lane_id"] for response in results] == [
                "fast", "main", "preview",
            ]
            assert all(response.json()["actual_duration"] == 124 / 24 for response in results)
            assert len(fake.payloads) == 3
            for payload_value in fake.payloads:
                workflow = payload_value["prompt"]
                assert workflow["2"]["inputs"]["width"] == 480
                assert workflow["2"]["inputs"]["height"] == 864
                assert workflow["4"]["inputs"]["steps"] == 6

    asyncio.run(scenario())


def test_public_health_does_not_expose_active_request_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        disk = type("DiskUsage", (), {"free": 50 * 1024**3})()
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: disk)
        module.fleet.store.create(
            prompt_id="health-redaction",
            upstream_prompt_id="upstream-health",
            execution_id="health-redaction",
            request_digest="digest",
            lane_id="fast",
            stage="preview",
            profile="preview",
            request_data={"prompt": {"secret": "customer prompt must not leak"}},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/health")
        assert response.status_code == 200
        assert "customer prompt must not leak" not in response.text
        fast = next(item for item in response.json()["lanes"] if item["id"] == "fast")
        assert fast["active_job_count"] == 1
        assert "active_jobs" not in fast

    asyncio.run(scenario())


def test_output_proxy_streams_upstream_video(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        store = module.fleet.store
        store.create(
            prompt_id="stream-output",
            upstream_prompt_id="upstream-stream",
            execution_id="stream-output",
            request_digest="digest",
            lane_id="fast",
            stage="preview",
            profile="preview",
        )
        store.update(
            "stream-output",
            status="completed",
            output_filename="result.mp4",
            output_subfolder="",
            output_type="output",
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/view"
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4", "content-length": "11"},
                stream=httpx.ByteStream(b"video-bytes"),
            )

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        module.fleet.client = upstream
        response = await module.view("result.mp4")
        assert isinstance(response, module.StreamingResponse)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["content-length"] == "11"
        assert b"".join([chunk async for chunk in response.body_iterator]) == b"video-bytes"
        await upstream.aclose()

    asyncio.run(scenario())


def test_router_execution_contract_accepts_concurrent_large_anchor_uploads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        disk = type("DiskUsage", (), {"free": 50 * 1024**3})()
        monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: disk)
        anchor = b"\x89PNG\r\n\x1a\n" + b"a" * (1536 * 1024)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-router-key"},
        ) as client:
            async def create(index):
                return await client.post(
                    "/api/router/executions",
                    data={
                        "operation_id": f"anchored-{index}",
                        "profile": "preview",
                        "mode": "fl2v",
                        "prompt": "Keep both approved boundary anchors.",
                        "duration": "5",
                        "seed": str(index),
                        "audio_policy": "native",
                        "aspect_ratio": "9:16",
                        "watermark": "false",
                    },
                    files={
                        "first_frame": ("first.png", anchor, "image/png"),
                        "last_frame": ("last.png", anchor + bytes([index]), "image/png"),
                    },
                )

            results = await asyncio.gather(*(create(index) for index in range(3)))

        assert [response.status_code for response in results] == [200, 200, 200]
        assert {
            response.json()["lane_id"]
            for response in results
        } == {"fast", "main", "preview"}
        assert len(fake.uploads) == 18
        assert all(
            payload_value["prompt"]["2"]["inputs"]["first_frame"]
            for payload_value in fake.payloads
        )
        assert all(
            payload_value["prompt"]["2"]["inputs"]["last_frame"]
            for payload_value in fake.payloads
        )

    asyncio.run(scenario())


def test_completed_job_without_video_is_failed(tmp_path: Path) -> None:
    async def scenario():
        module = load_module(tmp_path)
        fake = FakeComfyClient()
        module.fleet.client = fake
        submitted = await module.submit_prompt(payload("missing-video"))
        job = module.fleet.store.get(submitted["prompt_id"])
        fake.history[job["upstream_prompt_id"]] = {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {},
        }
        refreshed = await module.fleet.refresh_job(job)
        assert refreshed["status"] == "error"

    asyncio.run(scenario())
