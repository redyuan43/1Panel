from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
from pathlib import Path

import httpx
from PIL import Image
import pytest

from ai_router.media_service.contracts import MediaError, video_request
from ai_router.media_service.providers import H3Provider
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore
from ai_router.media_service.video_review import (
    SiyuanReviewer,
    SCORE_FIELDS,
    _assistant_text,
    _json_objects,
    contact_sheet,
    review_evidence_image,
    seam_contact_sheet,
    technical_review,
)
from ai_router.media_service.video_workflows import (
    build_prompt_package,
    classify_segmentation,
    managed_stages,
    options as video_options,
    prompt_package_hash,
)


def png(color: str = "green") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 96), color).save(output, format="PNG")
    return output.getvalue()


def mp4(path: Path, *, portrait: bool = False) -> bytes:
    width, height = ((480, 864) if portrait else (864, 480))
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc2=s={width}x{height}:r=24:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", "-y", str(path),
        ],
        check=True,
    )
    return path.read_bytes()


class FakeImage:
    async def generate(self, body, state, checkpoint, cwd):
        checkpoint({"thread_id": cwd.name, "turn_id": "one", "submitted": True})
        return {"data": png()}


class Stream:
    def __init__(self, data: bytes):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self.data


class FakeManagedH3:
    def __init__(self, data: bytes):
        self.data = data
        self.created = []
        self.executions = {}

    async def options(self):
        return {"contract_version": 1, "workflow_contract_version": 2}

    async def create_execution(self, body, assets):
        execution_id = f"exec_{len(self.created) + 1}"
        self.created.append({"execution_id": execution_id, "body": body, "assets": assets})
        self.executions[execution_id] = {
            "execution_id": execution_id,
            "status": "completed",
            "progress": 100,
            "output_id": f"raw_{execution_id}",
            "lane_id": ("fast", "main", "preview")[(len(self.created) - 1) % 3],
            "gpu_uuid": f"gpu-{len(self.created)}",
            "actual_duration": 1.0,
            "frame_count": 24,
        }
        return self.executions[execution_id]

    async def get_execution(self, execution_id):
        return self.executions[execution_id]

    async def download_execution(self, execution_id):
        return Stream(self.data)

    async def cancel_execution(self, execution_id, operation_id):
        self.executions[execution_id]["status"] = "cancelled"
        return self.executions[execution_id]


class FakeReviewer:
    async def review(
        self,
        path,
        *,
        output_id,
        artifact_sha256,
        prompt_package,
        expected,
        reference_sheets=None,
        technical=None,
    ):
        return {
            "review_id": "rev_" + output_id,
            "output_id": output_id,
            "artifact_sha256": artifact_sha256,
            "prompt_hash": prompt_package["prompt_hash"],
            "technical": {"duration_seconds": 1.0},
            "semantic": {
                "status": "completed",
                "verdict": "PASS",
                "confidence": 0.9,
                "scores": {name: 90 for name in SCORE_FIELDS},
                "issues": [],
                "revised_prompt": "",
                "recommended_action": "approve",
            },
            "manual_review_required": False,
        }


def service(tmp_path: Path, video: bytes):
    store = MediaStore(tmp_path)
    store.configure({
        "enabled": True,
        "codex_ready": True,
        "h3_ready": True,
        "min_free_bytes": 0,
    })
    result = MediaService(
        store,
        codex=FakeImage(),
        qwen=FakeImage(),
        h3=FakeManagedH3(video),
    )
    result.reviewer = FakeReviewer()
    return result


def test_video_request_defaults_to_direct_ivan_and_rejects_retired_legacy(monkeypatch):
    monkeypatch.delenv("AI_ROUTER_LEGACY_H3_ENABLED", raising=False)
    quality = video_request({
        "prompt": "x",
        "creative_profile": "tvc",
        "aspect_ratio": "9:16",
    })
    assert quality["workflow_mode"] == "quality_gate"
    assert quality["creative_profile"] == "tvc"
    with pytest.raises(MediaError) as retired:
        video_request({"prompt": "x", "workflow_mode": "legacy_pipeline"})
    assert retired.value.code == "workflow_unavailable"
    assert retired.value.status == 409
    monkeypatch.setenv("AI_ROUTER_LEGACY_H3_ENABLED", "true")
    legacy = video_request({"prompt": "x", "workflow_mode": "legacy_pipeline"})
    assert legacy["workflow_mode"] == "legacy_pipeline"
    assert legacy["aspect_ratio"] == "16:9"
    try:
        video_request({"prompt": "x", "workflow_mode": "duration_ladder", "duration": 10})
    except MediaError as error:
        assert error.code == "invalid_media_parameters"
    else:
        raise AssertionError("duration ladder accepted a non-15-second target")
    try:
        video_request({
            "prompt": "x",
            "workflow_mode": "quality_gate",
            "mode": "reference",
            "assets": {
                "reference_image": {
                    "data": base64.b64encode(png()).decode(),
                    "content_type": "image/png",
                },
            },
        })
    except MediaError as error:
        assert error.code == "invalid_media_parameters"
    else:
        raise AssertionError("managed workflow silently accepted an unsupported reference mode")


def test_workflow_shapes_default_to_single_continuous_generation(monkeypatch):
    monkeypatch.delenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", raising=False)
    monkeypatch.delenv("AI_ROUTER_LEGACY_H3_ENABLED", raising=False)
    quality = video_request({"prompt": "three calm product poses", "duration": 15,
                             "workflow_mode": "quality_gate"})
    assert [stage["id"] for stage in managed_stages(quality)] == ["plan", "preview", "final"]
    decision = classify_segmentation(quality)
    assert decision.eligible is False
    assert decision.reasons == ("parallel_segmentation_shelved",)
    assert len(build_prompt_package(quality)["segments"]) == 1
    published = video_options()
    assert published["segmentation_policy"] == "disabled"
    assert published["duration_ladder_available"] is False
    assert "duration_ladder" not in published["workflow_mode"]
    assert published["workflow_mode"] == ["quality_gate"]


def test_segmentation_rules_remain_behind_explicit_feature_flag(monkeypatch):
    monkeypatch.setenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", "true")
    quality = video_request({"prompt": "three calm product poses", "duration": 15,
                             "workflow_mode": "quality_gate"})
    assert classify_segmentation(quality).eligible is True
    subway = video_request({
        "prompt": "Keep a single continuous shot with continuous camera movement and a continuous transformation.",
        "duration": 15,
        "workflow_mode": "quality_gate",
    })
    decision = classify_segmentation(subway)
    assert decision.eligible is False
    assert {"continuous_camera", "fast_cross_boundary_action"} <= set(decision.reasons)
    ladder = video_request({"prompt": "three acts", "duration": 15,
                            "workflow_mode": "duration_ladder"})
    assert [stage["id"] for stage in managed_stages(ladder)] == ["clip_5s", "clip_10s", "clip_15s"]
    assert classify_segmentation(ladder).eligible is True
    assert len(build_prompt_package(ladder)["segments"]) == 3
    no_dialogue = video_request({
        "prompt": "Three stable product beats, no dialogue and no object handoff.",
        "duration": 15,
        "workflow_mode": "quality_gate",
    })
    assert classify_segmentation(no_dialogue).eligible is True
    chinese_no_dialogue = video_request({
        "prompt": "三个稳定的商品展示段落，无对白，无物体交接。",
        "duration": 15,
        "workflow_mode": "quality_gate",
    })
    assert classify_segmentation(chinese_no_dialogue).eligible is True


def test_duration_ladder_is_unavailable_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", raising=False)
    video = mp4(tmp_path / "source.mp4")
    svc = service(tmp_path / "state", video)
    try:
        svc.submit("alice", "video", {
            "prompt": "A single continuous shot with continuous dialogue.",
            "duration": 15,
            "workflow_mode": "duration_ladder",
        }, "ladder-continuous", "request")
    except MediaError as error:
        assert error.code == "workflow_unavailable"
        assert "quality_gate" in str(error)
    else:
        raise AssertionError("duration ladder was available while segmentation was shelved")


def test_quality_gate_exact_approval_and_one_continuous_preview_execution(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        monkeypatch.delenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", raising=False)
        video = mp4(tmp_path / "source.mp4", portrait=True)
        svc = service(tmp_path / "state", video)
        job = svc.submit("alice", "video", {
            "prompt": "Three stable product poses with clear pauses.",
            "duration": 15,
            "workflow_mode": "quality_gate",
            "aspect_ratio": "9:16",
        }, "create", "request")
        await svc._managed_video(job)
        job = svc.store.get(job["id"])
        plan = job["stages"][0]
        assert plan["status"] == "awaiting_approval"
        assert len(plan["artifacts"]) == 3
        assert plan["artifacts"][-1]["role"] == "anchor_contact_sheet"
        package = job["prompt_package"]
        assert len(package["storyboard"]) == 1
        assert package["audio_plan"]["native_ambience_crossfade_seconds"] == 0.08
        assert package["anchor_contact_sheet"]["artifact_id"] == plan["artifacts"][-1]["id"]
        assert package["prompt_hash"] == prompt_package_hash(package)
        try:
            await svc.action(job["id"], "plan", "approve", {"output_id": "stale"}, "bad", "alice")
        except MediaError as error:
            assert error.code == "stale_stage_output"
        else:
            raise AssertionError("stale plan approval was accepted")
        approved = await svc.action(
            job["id"], "plan", "approve", {"output_id": plan["output_id"]}, "approve-plan", "alice",
        )
        assert approved["stages"][0]["status"] == "approved"
        assert not svc.h3.created
        await svc.action(
            job["id"], "preview", "start", {"output_id": plan["output_id"]}, "start-preview", "alice",
        )
        await svc._managed_video(svc.store.get(job["id"]))
        current = svc.store.get(job["id"])
        preview = current["stages"][1]
        assert preview["status"] == "awaiting_approval"
        assert len(svc.h3.created) == 1
        assert {item["body"]["profile"] for item in svc.h3.created} == {"preview"}
        assert preview["output"]["boundary_seconds"] == []
        assert preview["output"]["shared_boundary_anchors"] == []
        executions = current["provider_state"]["managed_executions"][preview["run_id"]]["segments"]
        assert set(executions) == {"segment_1"}
        public = svc.public(current)
        assert "provider_state" not in public
        assert all("internal_gpu_uuid" not in str(stage) for stage in public["stages"])
        assert public["stages"][1]["execution_lanes"] == ["fast"]
        internal = svc.public(current, internal=True)
        assert internal["stages"][1]["internal_gpu_uuids"] == ["gpu-1"]
        assert public["stages"][1]["review"]["semantic"]["verdict"] == "PASS"
        try:
            await svc.action(
                job["id"],
                "preview",
                "regenerate",
                {"output_id": preview["output_id"]},
                "regen-without-review",
                "alice",
            )
        except MediaError as error:
            assert error.code == "stale_stage_output"
        else:
            raise AssertionError("regeneration without the current review was accepted")
        regenerated = await svc.action(
            job["id"],
            "preview",
            "regenerate",
            {
                "output_id": preview["output_id"],
                "review_id": preview["review"]["review_id"],
                "prompt": "Keep the approved identity and reduce temporal artifacts.",
            },
            "regen-preview",
            "alice",
        )
        regenerated_preview = regenerated["stages"][1]
        assert regenerated_preview["status"] == "queued"
        assert regenerated_preview["output_id"] is None
        assert regenerated_preview["prompt_override"].startswith("Keep the approved identity")
        await svc._managed_video(svc.store.get(job["id"]))
        regenerated_preview = svc.store.get(job["id"])["stages"][1]
        revised_prompt = regenerated_preview["prompt_override"]
        await svc.action(
            job["id"],
            "preview",
            "approve",
            {"output_id": regenerated_preview["output_id"]},
            "approve-regenerated-preview",
            "alice",
        )
        await svc.action(
            job["id"],
            "final",
            "start",
            {"output_id": regenerated_preview["output_id"]},
            "start-final",
            "alice",
        )
        await svc._managed_video(svc.store.get(job["id"]))
        final_requests = svc.h3.created[-1:]
        assert {item["body"]["prompt"] for item in final_requests} == {revised_prompt}
        assert {
            item["body"]["metadata"]["prompt_hash"]
            for item in final_requests
        } != {package["prompt_hash"]}
        await svc.close()

    asyncio.run(scenario())


def test_single_segment_quality_plan_still_approves_two_anchors(tmp_path):
    async def scenario():
        video = mp4(tmp_path / "source.mp4", portrait=True)
        svc = service(tmp_path / "state", video)
        job = svc.submit("alice", "video", {
            "prompt": (
                "Keep a single continuous shot with continuous camera movement "
                "and a continuous transformation."
            ),
            "duration": 15,
            "workflow_mode": "quality_gate",
            "aspect_ratio": "9:16",
        }, "create-single", "request-single")
        await svc._managed_video(job)
        current = svc.store.get(job["id"])
        plan = current["stages"][0]
        package = current["prompt_package"]
        assert package["segmentation"]["eligible"] is False
        assert len(package["segments"]) == 1
        assert [item["timestamp_seconds"] for item in package["anchors"]] == [0, 15]
        assert [item["role"] for item in plan["artifacts"]] == [
            "anchor",
            "anchor",
            "anchor_contact_sheet",
        ]
        assert package["prompt_hash"] == prompt_package_hash(package)
        await svc.action(
            job["id"],
            "plan",
            "approve",
            {"output_id": plan["output_id"]},
            "approve-single-plan",
            "alice",
        )
        await svc.action(
            job["id"],
            "preview",
            "start",
            {"output_id": plan["output_id"]},
            "start-single-preview",
            "alice",
        )
        await svc._managed_video(svc.store.get(job["id"]))
        assert len(svc.h3.created) == 1
        execution = svc.h3.created[0]
        assert execution["body"]["mode"] == "fl2v"
        assert set(execution["assets"]) == {"first_frame", "last_frame"}
        await svc.close()

    asyncio.run(scenario())


def test_managed_approval_recovers_a_pending_operation_receipt(tmp_path):
    async def scenario():
        video = mp4(tmp_path / "source.mp4")
        svc = service(tmp_path / "state", video)
        job = svc.submit(
            "alice",
            "video",
            {
                "prompt": "Three stable product poses.",
                "duration": 15,
                "workflow_mode": "quality_gate",
            },
            "create-receipt",
            "request-receipt",
        )
        await svc._managed_video(job)
        job = svc.store.get(job["id"])
        plan = job["stages"][0]
        operation_body = {
            "stage": "plan",
            "action": "approve",
            "output_id": plan["output_id"],
        }
        operation, created = svc.store.operation(
            job["id"],
            "approve-receipt",
            operation_body,
            "request-approve",
        )
        assert created is True
        svc._replace_stage(
            job["id"],
            "plan",
            status="approved",
            progress=100,
            internal_operation_id=operation["id"],
        )
        recovered = await svc.action(
            job["id"],
            "plan",
            "approve",
            {"output_id": plan["output_id"]},
            "approve-receipt",
            "alice",
        )
        assert recovered["stages"][0]["status"] == "approved"
        receipt, created = svc.store.operation(
            job["id"],
            "approve-receipt",
            operation_body,
            "request-approve",
        )
        assert created is False
        assert receipt["status"] == "completed"
        assert "internal_operation_id" not in svc.public(recovered)["stages"][0]
        await svc.close()

    asyncio.run(scenario())


def test_technical_failure_does_not_publish_an_approvable_stage_output(tmp_path):
    async def scenario():
        landscape_video = mp4(tmp_path / "wrong-dimensions.mp4", portrait=False)
        svc = service(tmp_path / "state", landscape_video)
        job = svc.submit("alice", "video", {
            "prompt": "A single continuous shot with continuous camera movement.",
            "duration": 15,
            "workflow_mode": "quality_gate",
            "aspect_ratio": "9:16",
        }, "create-invalid-output", "request-invalid-output")
        await svc._managed_video(job)
        plan = svc.store.get(job["id"])["stages"][0]
        await svc.action(
            job["id"],
            "plan",
            "approve",
            {"output_id": plan["output_id"]},
            "approve-invalid-output-plan",
            "alice",
        )
        await svc.action(
            job["id"],
            "preview",
            "start",
            {"output_id": plan["output_id"]},
            "start-invalid-output-preview",
            "alice",
        )
        await svc._managed_video(svc.store.get(job["id"]))
        current = svc.store.get(job["id"])
        preview = current["stages"][1]
        assert preview["status"] == "failed"
        assert preview.get("output") is None
        assert preview["error"]["code"] == "video_technical_review_failed"
        assert not [
            output
            for output in svc.store.outputs(job["id"])
            if output.get("stage") == "preview"
        ]
        await svc.close()

    asyncio.run(scenario())


def test_technical_review_rejects_truncated_video_without_audio(tmp_path):
    async def scenario():
        source = tmp_path / "truncated.mp4"
        width, height = 480, 864
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=s={width}x{height}:r=24:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-y",
                str(source),
            ],
            check=True,
        )
        evidence = await technical_review(
            source,
            expected={
                "width": width,
                "height": height,
                "fps": 24,
                "duration_seconds": 15,
                "frame_count": 360,
                "audio_required": True,
            },
        )
        assert evidence["passed"] is False
        assert {"audio", "duration", "frame_count"} <= {
            item["category"]
            for item in evidence["issues"]
            if item["severity"] == "error"
        }

    asyncio.run(scenario())


def test_duration_ladder_audio_mismatch_is_not_approvable_when_lab_flag_is_enabled(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        monkeypatch.setenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", "true")
        video = mp4(tmp_path / "source.mp4")
        svc = service(tmp_path / "state", video)
        job = svc.submit("alice", "video", {
            "prompt": "A three-part tabletop demonstration.",
            "duration": 15,
            "workflow_mode": "duration_ladder",
        }, "create", "request")
        await svc._managed_video(job)
        first = svc.store.get(job["id"])["stages"][0]
        assert first["status"] == "awaiting_approval"
        assert len(svc.h3.created) == 1
        await svc.action(job["id"], "clip_5s", "approve", {"output_id": first["output_id"]}, "a1", "alice")
        await svc.action(job["id"], "clip_10s", "start", {"output_id": first["output_id"]}, "s2", "alice")
        await svc._managed_video(svc.store.get(job["id"]))
        second = svc.store.get(job["id"])["stages"][1]
        assert second["status"] == "failed"
        assert second["error"]["code"] == "video_technical_review_failed"
        assert len(svc.h3.created) == 2
        await svc.close()

    asyncio.run(scenario())


def test_siyuan_review_schema_is_strict():
    valid = {
        "verdict": "PASS",
        "confidence": 0.9,
        "scores": {name: 80 for name in SCORE_FIELDS},
        "issues": [],
        "revised_prompt": "",
        "recommended_action": "approve",
    }
    assert SiyuanReviewer.validate(valid)["status"] == "completed"
    assert SiyuanReviewer.validate(valid)["conclusion_conflict"] is False
    invalid = {**valid, "scores": {**valid["scores"], "identity": 101}}
    try:
        SiyuanReviewer.validate(invalid)
    except MediaError as error:
        assert error.code == "invalid_video_review"
    else:
        raise AssertionError("invalid SIYUAN score was accepted")
    invalid_issue = {
        **valid,
        "issues": [{
            "category": "identity",
            "severity": "warning",
            "message": "Face drifts.",
            "unexpected": True,
        }],
    }
    try:
        SiyuanReviewer.validate(invalid_issue)
    except MediaError as error:
        assert error.code == "invalid_video_review"
    else:
        raise AssertionError("review issue with an unknown field was accepted")
    conflicting = {**valid, "recommended_action": "regenerate"}
    assert SiyuanReviewer.validate(conflicting)["conclusion_conflict"] is True


def test_siyuan_review_uses_router_auto_model_and_records_route(tmp_path, monkeypatch):
    async def scenario():
        sheet = tmp_path / "sheet.png"
        sheet.write_bytes(png())
        anchor_sheet = tmp_path / "anchors.png"
        anchor_sheet.write_bytes(png("red"))
        expected = {
            "verdict": "PASS",
            "confidence": 0.9,
            "scores": {name: 80 for name in SCORE_FIELDS},
            "issues": [],
            "revised_prompt": "",
            "recommended_action": "approve",
        }

        async def handler(request):
            assert request.url == httpx.URL("http://127.0.0.1:4000/v1/chat/completions")
            assert request.headers["authorization"] == "Bearer reviewer-test-key"
            body = json.loads(request.content)
            assert body["model"] == "siyuan/auto"
            assert body["reasoning_effort"] == "medium"
            assert body["response_format"] == {"type": "json_object"}
            assert body["max_tokens"] == 3000
            images = [
                item
                for item in body["messages"][0]["content"]
                if item["type"] == "image_url"
            ]
            assert len(images) == 1
            encoded = images[0]["image_url"]["url"].partition(",")[2]
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as composite:
                assert composite.width == 1200
                assert composite.height > 200
            return httpx.Response(
                200,
                headers={
                    "X-Request-ID": "review-request-1",
                    "X-1Panel-Route-Node": "ivan",
                    "X-1Panel-Route-Deployment": "ivan-qwen-review",
                },
                json={
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(expected),
                        },
                    }],
                },
            )

        monkeypatch.setenv("AI_ROUTER_VIDEO_REVIEW_KEY", "reviewer-test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                report = await SiyuanReviewer(client).review(
                    {"passed": True},
                    [sheet, anchor_sheet],
                    {"prompt_hash": "prompt-1"},
                )
        assert report["review_model"] == "siyuan/auto"
        assert report["internal_route"] == {
            "x-request-id": "review-request-1",
            "x-1panel-route-node": "ivan",
            "x-1panel-route-deployment": "ivan-qwen-review",
        }

    asyncio.run(scenario())


def test_siyuan_review_accepts_segmented_fenced_json_and_retries(tmp_path, monkeypatch):
    async def scenario():
        sheet = tmp_path / "sheet.png"
        sheet.write_bytes(png())
        expected = {
            "verdict": "CONDITIONAL_PASS",
            "confidence": 0.7,
            "scores": {name: 80 for name in SCORE_FIELDS},
            "issues": [],
            "revised_prompt": "Keep the cup geometry stable.",
            "recommended_action": "manual_review",
        }
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    headers={"X-Request-ID": "review-invalid-1"},
                    json={"choices": [{"message": {"content": "not json"}}]},
                )
            return httpx.Response(
                200,
                headers={"X-Request-ID": "review-repair-2"},
                json={
                    "choices": [{
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "```json\n" + json.dumps(expected) + "\n```",
                                },
                            ],
                        },
                    }],
                },
            )

        monkeypatch.setenv("AI_ROUTER_VIDEO_REVIEW_KEY", "reviewer-test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            report = await SiyuanReviewer(client).review(
                {"passed": True},
                sheet,
                {"prompt_hash": "prompt-1"},
            )
        assert len(requests) == 2
        assert requests[0]["reasoning_effort"] == "medium"
        assert requests[1]["reasoning_effort"] == "low"
        assert requests[1]["max_tokens"] == 4000
        assert "format-repair retry" in requests[1]["messages"][0]["content"][0]["text"]
        assert report["verdict"] == "CONDITIONAL_PASS"
        assert report["internal_route"]["x-request-id"] == "review-repair-2"
        assert [item["status"] for item in report["internal_attempts"]] == [200, 200]

    asyncio.run(scenario())


def test_review_json_helpers_ignore_non_json_text():
    expected = {"verdict": "PASS"}
    value = {
        "choices": [{
            "message": {
                "content": [{"type": "text", "text": "Result:\n" + json.dumps(expected)}],
            },
        }],
    }
    assert _json_objects(_assistant_text(value)) == [expected]


def test_review_evidence_combines_all_panels_into_one_image(tmp_path):
    timeline = tmp_path / "timeline-contact-sheet.png"
    seam = tmp_path / "boundary-seams.png"
    anchors = tmp_path / "anchor-contact-sheet.png"
    timeline.write_bytes(png("green"))
    seam.write_bytes(png("red"))
    anchors.write_bytes(png("blue"))
    data = review_evidence_image([timeline, seam, anchors])
    with Image.open(io.BytesIO(data)) as image:
        assert image.width == 1200
        assert image.height > 3 * 96


def test_technical_review_rejects_audio_video_duration_mismatch(tmp_path):
    async def scenario():
        source = tmp_path / "audio-tail.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=480x864:r=24:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=2",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-y",
                str(source),
            ],
            check=True,
        )
        evidence = await technical_review(
            source,
            expected={
                "width": 480,
                "height": 864,
                "fps": 24,
                "duration_seconds": 1,
                "frame_count": 24,
                "audio_required": True,
            },
        )
        assert evidence["passed"] is False
        assert evidence["audio_video_duration_delta_seconds"] > 0.9
        assert "audio_video_duration" in {
            item["category"]
            for item in evidence["issues"]
            if item["severity"] == "error"
        }

    asyncio.run(scenario())


def test_h3_provider_uses_managed_execution_contract(monkeypatch):
    async def scenario():
        requests = []

        async def handler(request):
            requests.append(request)
            assert request.headers["authorization"] == "Bearer h3-test-key"
            if request.url.path == "/api/router/options":
                return httpx.Response(
                    200,
                    json={"contract_version": 1, "workflow_contract_version": 2},
                )
            assert request.url.path == "/api/router/executions"
            assert request.method == "POST"
            payload = await request.aread()
            for expected in (
                b'name="operation_id"',
                b"video-operation-1",
                b'name="profile"',
                b"preview",
                b'name="first_frame"',
            ):
                assert expected in payload
            return httpx.Response(
                200,
                json={"execution_id": "exec_1", "status": "queued"},
            )

        monkeypatch.setenv("AI_ROUTER_H3_URL", "http://127.0.0.1:8789")
        monkeypatch.setenv("AI_ROUTER_H3_EXECUTOR_URL", "http://127.0.0.1:8789")
        monkeypatch.setenv("AI_ROUTER_H3_KEY", "h3-test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            created = await H3Provider(client).create_execution(
                {
                    "operation_id": "video-operation-1",
                    "profile": "preview",
                    "mode": "fl2v",
                    "prompt": "Keep the approved anchors.",
                    "duration": 5,
                    "seed": 1,
                    "audio_policy": "native",
                    "aspect_ratio": "9:16",
                    "metadata": {"segment_id": "segment_1"},
                },
                {
                    "first_frame": {
                        "data": base64.b64encode(png()).decode(),
                        "content_type": "image/png",
                    },
                },
            )
        assert created["execution_id"] == "exec_1"
        assert [request.url.path for request in requests] == [
            "/api/router/options",
            "/api/router/executions",
        ]

    asyncio.run(scenario())


def test_h3_provider_exposes_safe_contract_error_detail(monkeypatch):
    async def scenario():
        async def handler(request):
            return httpx.Response(
                400,
                json={"detail": "invalid execution fields"},
            )

        monkeypatch.setenv("AI_ROUTER_H3_URL", "http://127.0.0.1:8789")
        monkeypatch.setenv("AI_ROUTER_H3_EXECUTOR_URL", "http://127.0.0.1:8789")
        monkeypatch.setenv("AI_ROUTER_H3_KEY", "h3-test-key")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MediaError) as error:
                await H3Provider(client).call(
                    "POST",
                    "/executions",
                    executor=True,
                    data={"mode": "fl2v"},
                )
        assert error.value.code == "h3_request_failed"
        assert error.value.details == {
            "upstream_status": 400,
            "upstream_detail": "invalid execution fields",
        }

    asyncio.run(scenario())


def test_technical_review_and_contact_sheet(tmp_path):
    async def scenario():
        source = tmp_path / "source.mp4"
        mp4(source, portrait=True)
        evidence = await technical_review(
            source,
            expected={"width": 480, "height": 864, "fps": 24, "boundaries_seconds": [0.5]},
        )
        assert evidence["passed"] is True
        assert evidence["has_audio"] is True
        assert evidence["audio_loudness"]["integrated_lufs"] is not None
        assert evidence["boundary_differences"][0]["boundary_seconds"] == 0.5
        sheet = await contact_sheet(source, tmp_path / "sheet.png")
        assert sheet.is_file() and sheet.stat().st_size > 0
        seams = await seam_contact_sheet(
            source,
            tmp_path / "seams.png",
            [0.5],
        )
        assert seams and seams.is_file() and seams.stat().st_size > 0

    asyncio.run(scenario())
