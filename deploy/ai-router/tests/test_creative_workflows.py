import asyncio
import base64
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from ai_router.media_service.contracts import MediaError, video_request
from ai_router.media_service.creative import classify, readonly_signature
from ai_router.media_service.creative_chat import conversation_response, maybe_creative_chat
from ai_router.media_service.app import create_app
from ai_router.media_service.video_workflows import build_prompt_package
from test_media_video_workflows import service, mp4, png, Stream


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_ROUTER_VIDEO_REVIEW_KEY", raising=False)
    monkeypatch.delenv("AI_ROUTER_VIDEO_SEGMENTATION_ENABLED", raising=False)
    result = service(tmp_path / "state", mp4(tmp_path / "source.mp4"))
    quality = tmp_path / "quality.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp_path / "source.mp4"), "-vf", "scale=1344:768", "-c:v", "libx264", "-c:a", "copy", str(quality)], check=True)
    async def download(identifier):
        profile = next(x["body"]["profile"] for x in result.h3.created if x["execution_id"] == identifier)
        return Stream(quality.read_bytes() if profile == "quality" else result.h3.data)
    result.h3.download_execution = download
    return result


@pytest.mark.parametrize("text", ["只写一段视频提示词", "如何生成视频", "举例说明生成图片的流程", "分析这个视频", "不要生成视频", "write a prompt to generate video", "how to make an image", "请讨论一下制作视频", "解释这张图片", "write Python code to generate an image"])
def test_discussion_never_enters_generation(text):
    assert classify(text) is None


@pytest.mark.parametrize("text,kind", [("生成一个猫咪视频", "video"), ("用这个提示词生成视频", "video"), ("draw a photo of a flower", "image"), ("帮我画一张图片", "image"), ("画一只猫", "image"), ("draw a cat", "image")])
def test_explicit_creation(text, kind):
    assert classify(text) == kind


def test_true_text_to_video_has_no_implicit_image_generation():
    body = video_request({"prompt": "a cat", "anchor_policy": "provided"})
    assert build_prompt_package(body)["anchor_seconds"] == []
    assert body["mode"] == "t2v"


def test_idempotency_ownership_and_stale_revision(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "a cat"}, "op-one")
        assert c.create("alice", {"prompt": "a cat"}, "op-one")["id"] == w["id"]
        with pytest.raises(MediaError) as conflict:
            c.create("alice", {"prompt": "a dog"}, "op-one")
        assert conflict.value.status == 409
        with pytest.raises(MediaError) as foreign:
            c.get(w["id"], "bob")
        assert foreign.value.status == 404
        await c.process(w["id"])
        assert not svc.h3.created
        revised = await c.action(w["id"], "alice", {"revision": 1, "action": "revise", "spec": {"candidate_count": 3}}, "edit")
        assert revised["revision"] == 2
        await c.action(w["id"], "alice", {"revision": 1, "action": "revise", "spec": {"candidate_count": 3}}, "edit")
        assert c.get(w["id"])["revision"] == 2
        with pytest.raises(MediaError) as stale:
            await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        assert stale.value.code == "stale_workflow"
        assert not svc.h3.created
        await svc.close()
    asyncio.run(run())


async def drive(svc, identifier, until):
    for _ in range(30):
        await svc.creative.process(identifier)
        w = svc.creative.get(identifier)
        if w["status"] == until:
            return w
        for job in svc.store.active():
            if job["kind"] == "video":
                await svc._managed_video(job)
            else:
                await svc._image(job)
    raise AssertionError(svc.creative.get(identifier))


def test_three_samples_selection_and_automatic_final_survive_reload(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "A calm product scene", "candidate_count": 3, "aspect_ratio": "16:9"}, "create")
        await c.process(w["id"])
        assert c.get(w["id"])["status"] == "draft"
        assert not svc.h3.created
        await c.action(w["id"], "alice", {"action": "confirm", "revision": 1}, "confirm")
        w = await drive(svc, w["id"], "awaiting_selection")
        assert len(svc.h3.created) == 3
        assert all(x["body"]["mode"] == "t2v" for x in svc.h3.created)
        assert all(x["body"]["duration"] == 5 for x in svc.h3.created)
        assert not w.get("final_job_id")
        d = w["directions"][1]
        op = {"action": "select", "revision": 1, "direction_id": d["id"], "output_id": d["sample_output_id"]}
        await c.action(w["id"], "alice", op, "select")
        await c.action(w["id"], "alice", op, "select")
        from ai_router.media_service.creative import CreativeService
        svc.creative = CreativeService(svc)
        w = await drive(svc, w["id"], "completed")
        assert len(svc.h3.created) == 5
        assert [x["body"]["duration"] for x in svc.h3.created[3:]] == [15, 15]
        assert [x["body"]["profile"] for x in svc.h3.created[3:]] == ["preview", "quality"]
        assert w["authorization"]["automatic_regenerations"] == 0
        final = svc.store.get(w["final_job_id"])
        assert final["stages"][1]["approval_source"] == "workflow_policy"
        assert len(svc.creative.public(w)["jobs"]) == 4
        await svc.close()
    asyncio.run(run())


def test_multireference_requires_explicit_conversion(svc):
    async def run():
        c = svc.creative
        asset = c.upload("alice", {"data": base64.b64encode(png()).decode(), "content_type": "image/png", "role": "subject"})
        assert "path" not in asset
        with pytest.raises(MediaError):
            c.create("bob", {"prompt": "cat", "asset_ids": [asset["id"]]}, "create")
        w = c.create("alice", {"prompt": "cat", "asset_ids": [asset["id"]]}, "create")
        await c.process(w["id"])
        with pytest.raises(MediaError) as error:
            await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        assert error.value.code == "reference_conversion_required"
        assert not svc.h3.created
        await c.action(w["id"], "alice", {"revision": 1, "action": "confirm", "convert_references": True}, "convert")
        w = await drive(svc, w["id"], "awaiting_frames")
        assert not svc.h3.created
        assert w["authorization"]["direction_images"]
        await svc.close()
    asyncio.run(run())


def test_failed_quality_gate_does_not_render_final(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "calm scene", "aspect_ratio": "16:9"}, "create")
        await c.process(w["id"])
        await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        w = await drive(svc, w["id"], "awaiting_selection")
        d = w["directions"][0]
        await c.action(w["id"], "alice", {"revision": 1, "action": "select", "direction_id": d["id"], "output_id": d["sample_output_id"]}, "select")
        original = svc.reviewer.review
        async def uncertain(*args, **kwargs):
            review = await original(*args, **kwargs)
            review["manual_review_required"] = True
            return review
        svc.reviewer.review = uncertain
        w = await drive(svc, w["id"], "needs_attention")
        assert len(svc.h3.created) == 2
        assert svc.store.get(w["final_job_id"])["stages"][2]["status"] == "pending"
        await c.process(w["id"])
        assert len(svc.h3.created) == 2
        await svc.close()
    asyncio.run(run())


def test_internal_api_auth_grants_and_foreign_access(svc, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-private-key")
    async def run():
        app = create_app(svc, run_worker=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/creative/workflows")).status_code == 401
            headers = {"Authorization": "Bearer test-private-key", "X-Media-Owner": "alice", "X-Media-Models": "siyuan-video", "Idempotency-Key": "create"}
            denied = await client.post("/creative/workflows", headers=headers, json={"kind": "image", "prompt": "cat", "start": True})
            assert denied.status_code == 403
            response = await client.post("/creative/workflows", headers=headers, json={"prompt": "cat"})
            assert response.status_code == 202
            identifier = response.json()["id"]
            assert (await client.get("/creative/workflows/" + identifier, headers={**headers, "X-Media-Owner": "bob"})).status_code == 404
        await svc.close()
    asyncio.run(run())


def test_openai_responses_and_trusted_readonly(svc, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_CREATIVE_CHAT_ENABLED", "true")
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-key")
    w = svc.creative.public(svc.creative.create("alice", {"prompt": "cat"}, "create"))
    for kind in ("chat", "responses"):
        response = conversation_response(w, {"model": "siyuan/auto"}, kind)
        result = json.loads(response.body)
        assert result["media"]["generation_completed"] is False
        assert result["media"]["workflow_id"] == w["id"]
    async def run():
        body = {"model": "siyuan/auto", "messages": [{"role": "user", "content": "生成一张图片"}]}
        request = SimpleNamespace(headers={"x-siyuan-media-read-only": readonly_signature(body, "test-key")})
        assert await maybe_creative_chat(request, body, SimpleNamespace(), "chat") is None
        await svc.close()
    asyncio.run(run())


def test_add_branch_and_rerun_keep_other_artifacts(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "calm sea", "aspect_ratio": "16:9"}, "create")
        await c.process(w["id"])
        await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        w = await drive(svc, w["id"], "awaiting_selection")
        first = dict(w["directions"][0])
        await c.action(w["id"], "alice", {"revision": 1, "action": "add_direction", "prompt": "same sea, close up"}, "add")
        w = await drive(svc, w["id"], "awaiting_selection")
        assert len(svc.h3.created) == 2
        assert w["directions"][0] == first
        assert w["revision"] == 2
        await c.action(w["id"], "alice", {"revision": 2, "action": "rerun", "direction_id": "direction_2"}, "rerun")
        w = await drive(svc, w["id"], "awaiting_selection")
        assert len(svc.h3.created) == 3
        assert w["directions"][0] == first
        assert w["authorization"]["source"] == "user_rerun_direction"
        assert w["history"]
        await svc.close()
    asyncio.run(run())


def test_explicit_quality_override_binds_review_and_output(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "sea", "aspect_ratio": "16:9"}, "create")
        await c.process(w["id"])
        await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        w = await drive(svc, w["id"], "awaiting_selection")
        d = w["directions"][0]
        await c.action(w["id"], "alice", {"revision": 1, "action": "select", "direction_id": d["id"], "output_id": d["sample_output_id"]}, "select")
        original = svc.reviewer.review
        async def uncertain(*args, **kwargs):
            review = await original(*args, **kwargs)
            review["manual_review_required"] = True
            return review
        svc.reviewer.review = uncertain
        w = await drive(svc, w["id"], "needs_attention")
        stage = svc.store.get(w["final_job_id"])["stages"][1]
        with pytest.raises(MediaError):
            await c.action(w["id"], "alice", {"revision": 1, "action": "resume", "output_id": "old", "review_id": stage["review"]["review_id"]}, "bad")
        svc.reviewer.review = original
        await c.action(w["id"], "alice", {"revision": 1, "action": "resume", "output_id": stage["output_id"], "review_id": stage["review"]["review_id"]}, "resume")
        w = await drive(svc, w["id"], "completed")
        assert len(svc.h3.created) == 3
        assert svc.store.get(w["final_job_id"])["status"] == "completed"
        assert svc.store.get(w["final_job_id"])["stages"][1]["review"]["manual_review_required"]
        await svc.close()
    asyncio.run(run())


def test_recheck_reuses_archived_video_without_generation_or_fake_approval(svc):
    async def run():
        c = svc.creative
        w = c.create("alice", {"prompt": "sea", "aspect_ratio": "16:9"}, "create")
        await c.process(w["id"])
        await c.action(w["id"], "alice", {"revision": 1, "action": "confirm"}, "confirm")
        w = await drive(svc, w["id"], "awaiting_selection")
        d = w["directions"][0]
        original = svc.reviewer.review
        async def uncertain(*args, **kwargs):
            value = await original(*args, **kwargs)
            value["manual_review_required"] = True
            return value
        svc.reviewer.review = uncertain
        await c.action(w["id"], "alice", {"revision": 1, "action": "select", "direction_id": d["id"], "output_id": d["sample_output_id"]}, "select")
        w = await drive(svc, w["id"], "needs_attention")
        stage = svc.store.get(w["final_job_id"])["stages"][1]
        operation = {"revision": 1, "action": "recheck", "output_id": stage["output_id"], "review_id": stage["review"]["review_id"]}
        with pytest.raises(MediaError):
            await c.action(w["id"], "alice", {**operation, "output_id": "old"}, "bad-review")
        await c.action(w["id"], "alice", operation, "recheck")
        await c.action(w["id"], "alice", operation, "recheck")
        await c.process(w["id"])
        assert len(svc.h3.created) == 2
        assert c.get(w["id"])["review_history"][0]["review"]["manual_review_required"]
        assert not c.get(w["id"]).get("manual_reviewed_outputs")
        await c.process(w["id"])
        assert c.get(w["id"])["status"] == "needs_attention"
        svc.reviewer.review = original
        await c.action(w["id"], "alice", operation, "recheck-success")
        await c.process(w["id"])
        assert len(svc.h3.created) == 2
        completed = await drive(svc, w["id"], "completed")
        assert len(svc.h3.created) == 3 and len(completed["review_history"]) == 2
        assert not completed.get("manual_reviewed_outputs")
        await svc.close()
    asyncio.run(run())


def test_chat_public_api_ui_share_task_and_download(svc, tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from ai_router.media_service.gateway import router
    from ai_router.store import InMemoryStateStore
    from ai_router.audit import AuditLog
    monkeypatch.setenv("AI_ROUTER_CREATIVE_CHAT_ENABLED", "1")
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-key")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "test-admin")
    async def run():
        app = FastAPI()
        async def authenticate(header):
            assert header == "Bearer alice-key"
            return SimpleNamespace(policy=SimpleNamespace(id="alice", media_models=("siyuan-image", "siyuan-video"), disclosure_mode="public", rpm_limit=100), key_id="test-client")
        from ai_router.auth import AuthManager
        auth = AuthManager(None, None)
        auth.authenticate = authenticate
        app.state.runtime = SimpleNamespace(auth=auth, store=InMemoryStateStore(), audit=AuditLog(tmp_path / "audit.jsonl"))
        app.state.media_transport = httpx.ASGITransport(app=create_app(svc, run_worker=False))
        app.include_router(router(admin=True))
        app.include_router(router(admin=False))
        @app.post("/v1/chat/completions")
        @app.post("/v1/responses")
        async def chat(request: Request):
            request.state.server_request_id = "creative-acceptance"
            response = await maybe_creative_chat(request, await request.json(), await authenticate(request.headers.get("authorization")), "responses" if request.url.path.endswith("responses") else "chat")
            return response or JSONResponse({"ordinary_chat": True})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer alice-key"}) as client:
            for text in ("只写一段视频提示词", "举例说明生成图片", "分析这个视频", "如何生成视频", "不要生成图片"):
                r = await client.post("/v1/chat/completions", json={"model": "siyuan/auto", "messages": [{"role": "user", "content": text}]})
                assert r.json()["ordinary_chat"]
            assert not svc.h3.created and not svc.creative.listing()
            body = {"model": "siyuan/auto", "messages": [{"role": "user", "content": "生成一个15秒横屏海边视频"}]}
            first = (await client.post("/v1/chat/completions", json=body)).json()
            identifier = first["media"]["workflow_id"]
            assert (await client.post("/v1/chat/completions", json=body)).json()["media"]["workflow_id"] == identifier
            await svc.creative.process(identifier)
            public = (await client.get("/v1/media/workflows/" + identifier)).json()
            admin = (await client.get("/api/media/workflows/" + identifier, headers={"Authorization": "Bearer test-admin"})).json()
            assert public["id"] == admin["id"] and public["status"] == admin["status"] == "draft"
            for text in ("Explain how to confirm and start this video", "讨论确认方案的流程", "只写提示词，不要生成", "举例说明如何取消任务"):
                discussion = await client.post("/v1/chat/completions", json={"model": "siyuan/auto", "messages": [{"role": "user", "content": text}], "media": {"workflow_id": identifier, "revision": 1}})
                assert discussion.json()["ordinary_chat"]
                assert svc.creative.get(identifier)["status"] == "draft" and not svc.h3.created
            identified = await client.post("/v1/responses", json={"model": "siyuan/auto", "input": "查看工作流 " + identifier + " 的进度"})
            assert identified.json()["media"]["workflow_id"] == identifier
            body["messages"] += [{"role": "assistant", "content": first["choices"][0]["message"]["content"]}, {"role": "user", "content": "确认方案，生成样片"}]
            confirmation = await client.post("/v1/chat/completions", json=body)
            assert confirmation.json()["media"]["status"] == "sampling"
            w = await drive(svc, identifier, "awaiting_selection")
            sample = w["directions"][0]
            for text in ("选择第一和第二个继续", "选择1和2", "两个都选", "继续 " + identifier + " 和 wf_" + "f" * 32):
                with pytest.raises(MediaError) as error:
                    await client.post("/v1/responses", json={"model": "siyuan/auto", "input": text, "media": {"workflow_id": identifier, "revision": 1}})
                assert error.value.code == "workflow_target_ambiguous"
                assert svc.creative.get(identifier)["status"] == "awaiting_selection"
            ambiguous = await client.post("/v1/responses", json={"model": "siyuan/auto", "input": "继续生成15秒视频", "media": {"workflow_id": identifier, "revision": 1}})
            assert ambiguous.json()["media"]["status"] == "awaiting_selection"
            assert len(svc.h3.created) == 1
            direct = await client.post(f"/v1/videos/{sample['sample_job_id']}/stages/final/start", headers={"Idempotency-Key": "bypass"}, json={"output_id": sample["sample_output_id"]})
            assert direct.status_code == 409
            choose = {"model": "siyuan/auto", "input": "选择第一个继续", "media": {"workflow_id": identifier, "revision": 1}, "stream": True}
            stream = await client.post("/v1/responses", json=choose)
            assert "response.output_item.added" in stream.text and "response.completed" in stream.text
            restored = await client.post("/v1/responses", json={"model": "siyuan/auto", "input": "查看进度", "previous_response_id": stream.headers["X-Media-Response-ID"]})
            assert restored.json()["media"]["workflow_id"] == identifier
            await drive(svc, identifier, "completed")
            public = (await client.get("/v1/media/workflows/" + identifier)).json()
            final = public["jobs"][public["final_job_id"]]["stages"][-1]["output"]
            content = await client.get(f"/v1/media/outputs/{final['id']}/content")
            assert content.status_code == 200 and len(content.content) > 1000
            assert len(svc.h3.created) == 3
            asset = svc.creative.upload("alice", {"data": base64.b64encode(png()).decode(), "content_type": "image/png", "role": "product"})
            replacement = {"model": "siyuan/auto", "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "替换成这些参考素材，重新整理方向"},
                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png()).decode()},
            ]}], "media": {"workflow_id": identifier, "revision": 1, "asset_ids": [asset["id"]]}}
            changed = await client.post("/v1/responses", json=replacement)
            assert changed.json()["media"]["revision"] == 2
            current = svc.creative.get(identifier)
            assert [a["role"] for a in svc.creative.assets(current)] == ["product", "reference"]
            assert current["history"][0]["final_job_id"] == public["final_job_id"]
            assert len(svc.h3.created) == 3 and current["status"] == "planning"
            replay = await client.post("/v1/responses", json=replacement)
            assert replay.json()["media"]["revision"] == 2
        await svc.close()
    asyncio.run(run())


def test_production_router_hook_and_audit(svc, tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from ai_router.api import create_app as create_router
    from ai_router.auth import AuthenticatedClient
    from ai_router.types import ClientPolicy
    from ai_router.config import Registry
    from ai_router.runtime import build_runtime
    from ai_router.store import InMemoryStateStore
    from ai_router.token_counter import SimpleTokenCounter
    from test_core import settings, ROOT
    monkeypatch.setenv("AI_ROUTER_CREATIVE_CHAT_ENABLED", "1")
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-private-key")
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-litellm")
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "router-audit.jsonl"))
    async def run():
        runtime = build_runtime(settings=settings(tmp_path), registry=Registry(ROOT / "config/registry.yaml"), store=InMemoryStateStore(), token_counter=SimpleTokenCounter())
        policy = ClientPolicy(id="creative-test", key_env="UNUSED", models=("*",), rpm_limit=100, tpm_limit=100000, max_parallel_requests=3, media_models=("siyuan-image", "siyuan-video"))
        async def auth(_):
            return AuthenticatedClient(policy=policy, key_id="fixture")
        runtime.auth.authenticate = auth
        app = create_router(runtime)
        app.state.runtime = runtime
        app.state.media_transport = httpx.ASGITransport(app=create_app(svc, run_worker=False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "siyuan/auto", "messages": [{"role": "user", "content": "生成一个海边视频"}]})
            assert response.status_code == 200, response.text
            assert response.json()["media"]["status"] == "planning"
            assert response.headers["X-Request-ID"]
            assert not svc.h3.created
        assert 'media_workflow_chat' in (tmp_path / "router-audit.jsonl").read_text()
        await runtime.close()
        await svc.close()
    asyncio.run(run())


def test_long_review_timeout_is_scoped_and_has_trusted_readonly_context(tmp_path, monkeypatch):
    from ai_router.media_service.video_review import SiyuanReviewer
    monkeypatch.setenv("AI_ROUTER_VIDEO_REVIEW_KEY", "test-review-key")
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "test-internal-secret")
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(png())
    def handler(request):
        assert request.extensions["timeout"]["read"] == 360
        assert request.headers["X-Siyuan-Media-Read-Only"] == readonly_signature(json.loads(request.content), "test-internal-secret")
        raise httpx.ReadTimeout("fixture timeout", request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(MediaError) as error:
                await SiyuanReviewer(client).review({}, sheet, {})
            assert error.value.code == "video_review_failed"
            assert "无需重新生成" in str(error.value)
    asyncio.run(run())
