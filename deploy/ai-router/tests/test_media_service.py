from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from ai_router.audit import AuditLog
from ai_router.client_accounts import _validated_account
from ai_router.media_service.app import create_app
from ai_router.media_service.contracts import MediaError, QuotaExceeded, UnknownOutcome, image_request, video_request
from ai_router.media_service.gateway import router
from ai_router.media_service.providers import CodexProvider, QwenProvider
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore
from ai_router.store import InMemoryStateStore
from ai_router.types import ClientPolicy


def png():
    data = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(data, format="PNG")
    return data.getvalue()


def asset():
    return {"data": base64.b64encode(png()).decode(), "content_type": "image/png"}


class FakeImage:
    def __init__(self, quota=False):
        self.calls = 0
        self.quota = quota

    async def generate(self, body, state, checkpoint, cwd):
        self.calls += 1
        checkpoint({"thread_id": "synthetic", "submitted": True})
        if self.quota:
            raise QuotaExceeded()
        return {"data": png(), "provider": "fake"}


class FakeH3:
    def __init__(self):
        self.creates = 0
        self.actions = []
        self.receipts = {}
        self.drop_reply = False
        self.project = {
            "id": "project", "prompt_ir": "A reviewed scene", "prompt_approved": "",
            "updated_at": 1,
            "actual_duration": 4.45,
            "pipeline": [
                {"id": "context_ir", "status": "pending", "progress": 0, "run_id": None, "output_id": None},
                {"id": "preview", "status": "pending", "progress": 0, "run_id": None, "output_id": None},
            ],
        }

    async def create(self, job):
        self.creates += 1
        return self.project

    async def get(self, project_id):
        return self.project

    async def options(self):
        return {"contract_version": 1, "mode": ["t2v"]}

    async def action(self, project_id, stage, action, payload):
        key = payload["operation_id"]
        if key in self.receipts:
            assert self.receipts[key] == payload
            return self.project
        self.receipts[key] = dict(payload)
        self.actions.append((stage, action))
        item = next(item for item in self.project["pipeline"] if item["id"] == stage)
        if action == "start":
            item.update(status="awaiting_approval" if stage == "context_ir" else "running", run_id="run_" + stage,
                        output_id="output_" + stage if stage == "context_ir" else None, progress=100)
        elif action == "approve":
            item["status"] = "approved"
            if stage == "context_ir":
                self.project["prompt_approved"] = payload["prompt"]
        if self.drop_reply:
            self.drop_reply = False
            raise UnknownOutcome()
        return self.project


def service(tmp_path, **kwargs):
    store = MediaStore(tmp_path)
    store.configure({"enabled": True, "codex_ready": True, "h3_ready": True, "min_free_bytes": 0})
    return MediaService(store, codex=kwargs.get("codex") or FakeImage(),
                        qwen=kwargs.get("qwen") or FakeImage(), h3=kwargs.get("h3") or FakeH3())


def test_media_grants_are_independent_and_preserved():
    value = {"id": "dev-client", "name": "Dev", "models": ["siyuan/auto"],
             "rpm_limit": 10, "tpm_limit": 1000, "max_parallel_requests": 1}
    account = _validated_account(value, allowed_models={"siyuan/auto"}, public_model_id="siyuan/auto", existing=None)
    assert account["media_models"] == []
    granted = _validated_account({"media_models": ["siyuan-image"]}, allowed_models={"siyuan/auto"},
                                 public_model_id="siyuan/auto", existing=account)
    assert granted["models"] == ["siyuan/auto"]
    assert granted["media_models"] == ["siyuan-image"]
    changed = _validated_account({"name": "Renamed"}, allowed_models={"siyuan/auto"},
                                 public_model_id="siyuan/auto", existing=granted)
    assert changed["media_models"] == ["siyuan-image"]
    with pytest.raises(Exception):
        _validated_account({"media_models": ["qwen-image-3.0-pro"]}, allowed_models={"siyuan/auto"},
                           public_model_id="siyuan/auto", existing=account)


@pytest.mark.parametrize("body,edit", [
    ({"prompt": "x", "mask": "ignored"}, False), ({"prompt": "x", "n": 2}, False),
    ({"prompt": "x", "images": [asset()]}, False), ({"prompt": "x"}, True),
    ({"prompt": "x", "images": [asset()] * 6}, True),
    ({"prompt": "x", "images": [{"path": "/etc/passwd"}]}, True),
])
def test_invalid_image_inputs(body, edit):
    with pytest.raises(MediaError):
        image_request(body, edit)


@pytest.mark.parametrize("body", [
    {"prompt": "x", "duration": 16}, {"prompt": "x", "mode": "hybrid"},
    {"prompt": "x", "mode": "i2v"}, {"prompt": "x", "audio_policy": "lock_source"},
    {"prompt": "x", "assets": {"reference_video": {"data": "bad"}}},
])
def test_invalid_video_inputs(body):
    with pytest.raises(MediaError):
        video_request(body)


def test_image_idempotency_ownership_and_restart_archive(tmp_path):
    async def scenario():
        svc = service(tmp_path)
        job = svc.submit("alice", "image", {"prompt": "x"}, "same", "r1")
        assert svc.submit("alice", "image", {"prompt": "x"}, "same", "r2")["id"] == job["id"]
        with pytest.raises(MediaError, match="different parameters"):
            svc.submit("alice", "image", {"prompt": "y"}, "same", "r3")
        await svc._image(job)
        result = svc.store.get(job["id"], "alice")
        assert result["status"] == "completed"
        assert result["output"]["width"] == 32
        assert svc.codex.calls == 1
        public = svc.public(result)
        assert "provider" not in public and "owner" not in public and "path" not in public["output"]
        assert base64.b64decode(public["data"][0]["b64_json"]) == png()
        reopened = MediaStore(tmp_path).get(job["id"], "alice")
        assert reopened["status"] == "completed"
        assert Path(reopened["output"]["path"]).read_bytes() == png()
        with pytest.raises(MediaError) as error:
            svc.store.get(job["id"], "bob")
        assert error.value.status == 404
        await svc.close()
    asyncio.run(scenario())


def test_fallback_only_on_quota_and_preserves_reference_limits(tmp_path):
    async def scenario():
        codex, qwen = FakeImage(quota=True), FakeImage()
        svc = service(tmp_path, codex=codex, qwen=qwen)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        await svc._image(job)
        assert svc.store.get(job["id"])["fallback_applied"] is True
        assert qwen.calls == 1
        job = svc.submit("alice", "image", {"prompt": "x", "images": [asset()] * 4}, "two", "r2", edit=True)
        await svc._image(job)
        assert svc.store.get(job["id"])["error"]["code"] == "fallback_incompatible"
        assert qwen.calls == 1
        await svc.close()
    asyncio.run(scenario())


def test_paid_reservation_is_atomic_and_idempotent(tmp_path):
    store = MediaStore(tmp_path)
    store.reserve_paid("one", 1)
    store.reserve_paid("one", 1)
    with pytest.raises(MediaError) as error:
        store.reserve_paid("two", 1)
    assert error.value.code == "paid_media_limit"
    store.release_paid("one")
    store.release_paid("one")
    store.reserve_paid("two", 1)


@pytest.mark.parametrize("code", [
    "fallback_incompatible",
    "qwen_not_configured",
    "qwen_request_failed",
])
def test_deterministic_qwen_rejection_releases_paid_reservation(tmp_path, code):
    class RejectedQwen:
        async def generate(self, body, state, checkpoint, cwd):
            if code == "qwen_request_failed":
                checkpoint({"submitted": True})
            raise MediaError(code, "Rejected.", 503)

    async def scenario():
        svc = service(tmp_path, qwen=RejectedQwen())
        job = svc.submit(
            "alice", "image",
            {"model": "qwen-image-3.0-pro", "prompt": "x"},
            "one", "r1",
        )
        await svc._image(job)
        assert svc.store.get(job["id"])["error"]["code"] == code
        svc.store.reserve_paid("next", 1)
        await svc.close()

    asyncio.run(scenario())


def test_uncertain_qwen_outcome_keeps_paid_reservation(tmp_path):
    class UncertainQwen:
        async def generate(self, body, state, checkpoint, cwd):
            checkpoint({"submitted": True})
            raise UnknownOutcome()

    async def scenario():
        svc = service(tmp_path, qwen=UncertainQwen())
        job = svc.submit(
            "alice", "image",
            {"model": "qwen-image-3.0-pro", "prompt": "x"},
            "one", "r1",
        )
        await svc._image(job)
        assert svc.store.get(job["id"])["status"] == "reconciling"
        with pytest.raises(MediaError) as error:
            svc.store.reserve_paid("next", 1)
        assert error.value.code == "paid_media_limit"
        await svc.close()

    asyncio.run(scenario())


def test_video_output_approval_start_and_lost_reply(tmp_path):
    async def scenario():
        h3 = FakeH3()
        svc = service(tmp_path, h3=h3)
        job = svc.submit("alice", "video", {"prompt": "x"}, "same", "r1")
        await svc._video(job)
        current = svc.store.get(job["id"])
        output = current["stages"][0]["output"]
        assert output["text"] == "A reviewed scene"
        assert Path(output["path"]).read_text() == output["text"]
        assert h3.actions == [("context_ir", "start")]
        with pytest.raises(MediaError) as error:
            await svc.action(job["id"], "context_ir", "approve", {"output_id": "old"}, "bad", "alice")
        assert error.value.code == "stale_stage_output"
        h3.drop_reply = True
        with pytest.raises(UnknownOutcome):
            await svc.action(job["id"], "context_ir", "approve", {"output_id": output["id"], "prompt": "Edited"}, "approve", "alice")
        result = await svc.action(job["id"], "context_ir", "approve", {"output_id": output["id"], "prompt": "Edited"}, "approve", "alice")
        assert result["stages"][0]["status"] == "approved"
        assert h3.actions.count(("context_ir", "approve")) == 1
        assert ("preview", "start") not in h3.actions
        await svc.action(job["id"], "preview", "start", {"output_id": output["id"]}, "start", "alice")
        assert ("preview", "start") in h3.actions
        assert Path(output["path"]).exists()
        await svc.close()
    asyncio.run(scenario())


def test_missing_context_receipt_reconciles_without_new_project(tmp_path):
    async def scenario():
        h3 = FakeH3()
        h3.drop_reply = True
        svc = service(tmp_path, h3=h3)
        job = svc.submit("alice", "video", {"prompt": "x"}, "same", "r1")
        with pytest.raises(UnknownOutcome):
            await svc._video(job)
        await svc._video(svc.store.get(job["id"]))
        assert h3.creates == 1
        assert len(h3.actions) == 1
        await svc.close()
    asyncio.run(scenario())


def test_video_provider_heartbeat_refreshes_router_job(tmp_path):
    async def scenario():
        h3 = FakeH3()
        svc = service(tmp_path, h3=h3)
        job = svc.submit("alice", "video", {"prompt": "x"}, "same", "r1")
        await svc._video(job)
        first = svc.store.get(job["id"])
        assert first["provider_updated_at"] == 1
        h3.project["updated_at"] = 2
        await svc._video(first)
        second = svc.store.get(job["id"])
        assert second["provider_updated_at"] == 2
        assert second["updated_at"] > first["updated_at"]
        await svc.close()

    asyncio.run(scenario())


def test_codex_typed_quota_failure():
    with pytest.raises(QuotaExceeded):
        CodexProvider._result({"status": "failed", "failure": {"type": "usageLimitExceeded"}})
    with pytest.raises(MediaError) as error:
        CodexProvider._result({"status": "failed", "failure": None})
    assert not isinstance(error.value, QuotaExceeded)


def test_qwen_async_contract_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_DASHSCOPE_BASE_URL", "https://workspace.cn-beijing.maas.aliyuncs.com")
    monkeypatch.setenv("AI_ROUTER_DASHSCOPE_API_KEY", "test")
    async def scenario():
        posts = []
        def handler(request):
            if request.method == "POST":
                posts.append(request)
                assert request.headers["x-dashscope-async"] == "enable"
                return httpx.Response(200, json={"output": {"task_id": "task"}})
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED",
                                                        "results": [{"url": "https://example.aliyuncs.com/result.png"}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            state = {}
            provider = QwenProvider(client)
            body = image_request({"prompt": "x"})
            result = await provider.generate(body, state, state.update, tmp_path)
            assert result["url"].endswith("result.png")
            await provider.generate(body, state, state.update, tmp_path)
            assert len(posts) == 1
    asyncio.run(scenario())


def test_internal_api_auth_range_and_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "internal-test")
    async def scenario():
        svc = service(tmp_path)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        await svc._image(job)
        output = svc.store.get(job["id"])["output"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(svc, run_worker=False)), base_url="http://test") as client:
            assert (await client.get("/jobs")).status_code == 401
            headers = {"Authorization": "Bearer internal-test", "X-Media-Owner": "bob"}
            assert (await client.get("/jobs/" + job["id"], headers=headers)).status_code == 404
            headers["X-Media-Owner"] = "alice"
            response = await client.get(f"/outputs/{output['id']}/content", headers={**headers, "Range": "bytes=0-7"})
            assert response.status_code == 206 and response.content == png()[:8]
            assert response.headers["content-range"].startswith("bytes 0-7/")
            assert response.headers["content-disposition"].endswith('.png"')
        await svc.close()
    asyncio.run(scenario())


def test_gateway_sync_image_and_revocable_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "internal-test")
    async def scenario():
        svc = service(tmp_path)
        await svc.start()
        cache = InMemoryStateStore()
        class Auth:
            async def authenticate(self, authorization):
                return SimpleNamespace(policy=ClientPolicy("alice", "", ("siyuan/auto",), 10, 1000, 1,
                                                            media_models=("siyuan-image",)), key_id="key")
        class Clients:
            active = True
            async def is_key_active(self, owner, key):
                return self.active
        clients = Clients()
        app = FastAPI()
        app.state.runtime = SimpleNamespace(auth=Auth(), clients=clients, store=cache, audit=AuditLog(tmp_path / "audit.jsonl"))
        app.state.media_transport = httpx.ASGITransport(app=create_app(svc, run_worker=False))
        app.include_router(router())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/images/generations", json={"prompt": "x", "response_format": "url"},
                                         headers={"Idempotency-Key": "same"})
            assert response.status_code == 200, response.text
            value = response.json()
            assert "provider" not in value and "provider_state" not in value
            url = value["data"][0]["url"]
            download = await client.get(url, headers={"Range": "bytes=0-7"})
            assert download.status_code == 206 and download.content == png()[:8]
            clients.active = False
            assert (await client.get(url)).status_code == 401
        await svc.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [
    MediaError("policy_rejection", "Rejected.", 422), UnknownOutcome(),
])
def test_non_quota_errors_never_trigger_paid_fallback(tmp_path, failure):
    class FailingImage:
        async def generate(self, *args):
            raise failure
    async def scenario():
        qwen = FakeImage()
        svc = service(tmp_path, codex=FailingImage(), qwen=qwen)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        await svc._image(job)
        assert qwen.calls == 0
        assert svc.store.get(job["id"])["status"] in {"failed", "reconciling"}
        await svc.close()
    asyncio.run(scenario())


def test_queued_cancel_and_explicit_purge_preserve_idempotency_tombstone(tmp_path):
    async def scenario():
        svc = service(tmp_path)
        queued = svc.submit("alice", "image", {"prompt": "cancel"}, "cancel", "r1")
        assert (await svc.cancel_image(queued["id"], "alice"))["status"] == "cancelled"
        assert svc.codex.calls == 0
        job = svc.submit("alice", "image", {"prompt": "generate"}, "generate", "r2")
        await svc._image(job)
        path = Path(svc.store.get(job["id"])["output"]["path"])
        with pytest.raises(MediaError):
            svc.purge(job["id"], job["id"])
        svc.store.update(job["id"], deleted=True)
        with pytest.raises(MediaError):
            svc.purge(job["id"], "wrong")
        svc.purge(job["id"], job["id"])
        assert not path.exists()
        with pytest.raises(MediaError) as error:
            svc.submit("alice", "image", {"prompt": "generate"}, "generate", "r3")
        assert error.value.status == 410
        await svc.close()
    asyncio.run(scenario())


def test_worker_singleton_lock(tmp_path):
    async def scenario():
        first = service(tmp_path)
        second = service(tmp_path)
        await first.start()
        with pytest.raises(RuntimeError, match="another media worker"):
            await second.start()
        await first.close()
        await second.start()
        await second.close()
    asyncio.run(scenario())


def test_codex_adapter_uses_persistent_isolated_thread_and_stops_after_one_image(tmp_path, monkeypatch):
    from ai_router.media_service import providers
    calls = []
    class RPC:
        process = True
        def __init__(self):
            self.events = asyncio.Queue()
        async def start(self):
            pass
        async def call(self, method, params):
            calls.append((method, params))
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "thread/start":
                return {
                    "thread": {"id": "isolated", "cwd": str(tmp_path), "ephemeral": False},
                    "cwd": str(tmp_path), "modelProvider": "openai", "approvalPolicy": "never",
                    "activePermissionProfile": {"id": "router_media", "extends": None},
                    "runtimeWorkspaceRoots": [],
                    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                                "excludeTmpdirEnvVar": True, "excludeSlashTmp": True},
                }
            if method == "turn/start":
                self.events.put_nowait({"method": "item/completed", "params": {"threadId": "isolated",
                    "item": {"type": "imageGeneration", "status": "completed", "result": base64.b64encode(png()).decode()}}})
                return {"turn": {"id": "one"}}
            return {}
        async def close(self):
            pass
    monkeypatch.setattr(providers, "CodexRPC", RPC)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    async def scenario():
        states = []
        result = await CodexProvider().generate(image_request({"prompt": "x"}), {}, states.append, tmp_path)
        assert result["data"] == png()
        params = next(params for method, params in calls if method == "thread/start")
        assert params["ephemeral"] is False
        assert params["config"]["features"]["shell_tool"] is False
        assert params["config"]["permissions"]["router_media"]["filesystem"][str(tmp_path)] == "write"
        assert ("turn/interrupt", {"threadId": "isolated", "turnId": "one"}) in calls
        assert states[-1]["thread_id"] == "isolated"
    asyncio.run(scenario())


def test_archive_recovery_rejects_changed_content_before_replacement(tmp_path):
    async def scenario():
        svc = service(tmp_path)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        await svc._image(job)
        job = svc.store.get(job["id"])
        output = job["output"]
        path = Path(output["path"])
        path.unlink()
        with pytest.raises(MediaError) as error:
            svc.public(job)
        assert error.value.status == 409
        with pytest.raises(MediaError, match="differs"):
            await svc.archive(job["id"], output["id"], data=b"not the original image")
        assert not path.exists()
        await svc._image(svc.store.get(job["id"]))
        assert path.read_bytes() == png()
        assert svc.store.get(job["id"])["recovery_outputs"] == []
        await svc.close()
    asyncio.run(scenario())


def test_old_context_output_is_recovered_without_restarting_stage(tmp_path):
    async def scenario():
        svc = service(tmp_path)
        job = svc.submit("alice", "video", {"prompt": "x"}, "one", "r1")
        await svc._video(job)
        old = await svc.archive(job["id"], "out_previous_context", data=b"Old prompt",
                                content_type="text/plain", text="Old prompt", stage="context_ir")
        Path(old["path"]).unlink()
        with pytest.raises(MediaError):
            svc.output_path(svc.store.get(job["id"]), old)
        before = len(svc.h3.actions)
        await svc.tick()
        assert Path(old["path"]).read_text() == "Old prompt"
        assert len(svc.h3.actions) == before
        await svc.close()
    asyncio.run(scenario())


def test_reconciliation_does_not_starve_new_images(tmp_path):
    class PendingImage(FakeImage):
        async def generate(self, body, state, checkpoint, cwd):
            if body["prompt"] == "pending":
                checkpoint({"thread_id": "original", "submitted": True})
                raise UnknownOutcome()
            return await super().generate(body, state, checkpoint, cwd)
    async def scenario():
        svc = service(tmp_path, codex=PendingImage())
        pending = svc.submit("alice", "image", {"prompt": "pending"}, "pending", "r1")
        ready = svc.submit("alice", "image", {"prompt": "ready"}, "ready", "r2")
        await svc.tick()
        await svc.image_task
        assert svc.store.get(pending["id"])["status"] == "reconciling"
        await svc.tick()
        await svc.image_task
        assert svc.store.get(ready["id"])["status"] == "completed"
        await svc.close()
    asyncio.run(scenario())


def test_preview_tickets_are_removed_from_uvicorn_access_records():
    import logging
    from ai_router.media_service.gateway import MediaAccessLogFilter
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s",
                               ("client", "GET", "/v1/media/outputs/out_1/content?access=secret", "1.1", 200), None)
    assert MediaAccessLogFilter().filter(record)
    assert record.args[2] == "/v1/media/outputs/out_1/content"
    assert "secret" not in record.getMessage()


@pytest.mark.parametrize("setting", ["enabled", "videos_enabled", "h3_ready"])
def test_video_admission_switches_apply_to_new_stage_starts(tmp_path, setting):
    async def scenario():
        svc = service(tmp_path)
        job = svc.submit("alice", "video", {"prompt": "x"}, "one", "r1")
        await svc._video(job)
        before = len(svc.h3.actions)
        svc.store.configure({**svc.store.settings(), setting: False})
        with pytest.raises(MediaError) as error:
            await svc.action(job["id"], "context_ir", "start", {}, "retry", "alice")
        assert error.value.status == 503
        assert len(svc.h3.actions) == before
        await svc.close()
    asyncio.run(scenario())


def test_explicit_cancellation_does_not_fall_back_to_a_paid_image(tmp_path):
    class CancellableQuotaImage(FakeImage):
        cancels = 0
        async def cancel(self, state, cwd):
            self.cancels += 1
    async def scenario():
        codex, qwen = CancellableQuotaImage(quota=True), FakeImage()
        svc = service(tmp_path, codex=codex, qwen=qwen)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        job = svc.store.update(job["id"], status="cancelling", cancel_requested=True,
                               provider_state={"thread_id": "original", "provider": "codex", "submitted": True})
        await svc._image(job)
        assert codex.cancels == 1 and qwen.calls == 0
        assert svc.store.get(job["id"])["status"] == "cancelled"
        await svc.close()
    asyncio.run(scenario())


def test_explicit_cancel_interrupts_but_service_shutdown_only_detaches(tmp_path):
    class LongImage:
        cancels = 0
        started = None
        async def generate(self, body, state, checkpoint, cwd):
            checkpoint({"provider": "codex", "thread_id": cwd.name, "turn_id": "one", "submitted": True})
            self.started.set()
            await asyncio.Event().wait()
        async def cancel(self, state, cwd):
            self.cancels += 1
    async def scenario():
        codex = LongImage()
        codex.started = asyncio.Event()
        svc = service(tmp_path / "explicit", codex=codex)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r1")
        await svc.tick()
        await codex.started.wait()
        result = await svc.cancel_image(job["id"], "alice")
        assert result["status"] == "cancelling" and codex.cancels == 1
        await svc.close()
        detached = LongImage()
        detached.started = asyncio.Event()
        svc = service(tmp_path / "shutdown", codex=detached)
        job = svc.submit("alice", "image", {"prompt": "x"}, "one", "r2")
        await svc.tick()
        await detached.started.wait()
        await svc.close()
        assert detached.cancels == 0
        assert svc.store.get(job["id"])["status"] == "reconciling"
    asyncio.run(scenario())
