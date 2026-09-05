from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from ai_router.media_service.app import create_app
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore


def probe(path, *args):
    return json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
    ))


@pytest.fixture
def tagged_video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Video archival requires ffmpeg and ffprobe")
    path = tmp_path / "tagged.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=4:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=400:duration=1", "-c:v", "libx264", "-c:a", "aac",
        "-metadata", 'prompt={"workflow":"internal-model","path":"/private/host","key":"synthetic-secret"}',
        "-metadata", "comment=private workflow", "-metadata:s:v:0", "handler_name=internal model",
        "-movflags", "use_metadata_tags", str(path),
    ], check=True)
    assert "prompt" in probe(path, "-show_format")["format"]["tags"]
    return path


def packet_hashes(path):
    packets = probe(path, "-show_packets", "-show_data_hash", "sha256",
                    "-show_entries", "packet=stream_index,data_hash")["packets"]
    return {index: [packet["data_hash"] for packet in packets if packet["stream_index"] == index]
            for index in {packet["stream_index"] for packet in packets}}


def test_video_export_removes_workflow_metadata_without_transcoding(tmp_path, tagged_video):
    async def scenario():
        svc = MediaService(MediaStore(tmp_path / "media"))
        original = tagged_video.read_bytes()
        output = await svc.archive("vid_test", "out_version", data=original, content_type="video/mp4")
        assert output["id"] != output["output_id"] == "out_version"
        assert output["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert Path(output["source_path"]).read_bytes() == original
        clean = Path(output["path"])
        assert output["sha256"] == hashlib.sha256(clean.read_bytes()).hexdigest()
        assert output["sha256"] != output["source_sha256"]
        assert packet_hashes(clean) == packet_hashes(tagged_video)
        metadata = probe(clean, "-show_format", "-show_streams")
        assert "prompt" not in metadata["format"]["tags"]
        assert "private" not in json.dumps(metadata)
        assert "synthetic-secret" not in clean.read_bytes().decode(errors="ignore")
        public = svc.public_output(output)
        assert not any(name.startswith("source_") for name in public)
        clean.unlink()
        restored = await svc.archive("vid_test", "out_version", data=original, content_type="video/mp4")
        assert restored["id"] == output["id"] and restored["sha256"] == output["sha256"]
        assert Path(restored["path"]).exists()
        await svc.close()
    asyncio.run(scenario())


def test_legacy_raw_video_is_private_and_delivery_keeps_approval_version(tmp_path, tagged_video, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-internal")
    async def scenario():
        store = MediaStore(tmp_path / "media")
        store.configure({"enabled": True, "h3_ready": True, "min_free_bytes": 0})
        svc = MediaService(store)
        job = svc.submit("alice", "video", {"prompt": "test"}, "test", "request")
        original = tagged_video.read_bytes()
        directory = store.root / "outputs" / job["id"]
        directory.mkdir(parents=True)
        raw_path = directory / "out_version"
        raw_path.write_bytes(original)
        legacy = store.save_artifact({
            "id": "out_version", "output_id": "out_version", "job_id": job["id"],
            "path": str(raw_path), "content_type": "video/mp4", "bytes": len(original),
            "sha256": hashlib.sha256(original).hexdigest(), "stage": "preview",
        })
        store.update(job["id"], status="completed", output=legacy)
        assert svc.public(store.get(job["id"]))["output"] is None
        assert store.get(job["id"])["status"] == "archiving"
        clean = await svc.archive(job["id"], "out_version", data=original, content_type="video/mp4", stage="preview")
        store.update(job["id"], status="completed", output=clean)
        headers = {"Authorization": "Bearer test-internal", "X-Media-Owner": "alice"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(svc, run_worker=False)),
                                     base_url="http://test", headers=headers) as client:
            assert (await client.get("/outputs/out_version/content")).status_code == 410
            response = await client.get(f"/outputs/{clean['id']}/content")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == clean["sha256"]
            history = (await client.get(f"/jobs/{job['id']}/outputs")).json()["data"]
            assert len(history) == 1
            assert history[0]["id"] == clean["id"]
            assert history[0]["output_id"] == "out_version"
            assert "source_path" not in history[0]
        assert raw_path.read_bytes() == original
        store.update(job["id"], deleted=True)
        svc.purge(job["id"], job["id"])
        assert not Path(clean["path"]).exists()
        assert not Path(clean["source_path"]).exists()
        assert not raw_path.exists()
        await svc.close()
    asyncio.run(scenario())
