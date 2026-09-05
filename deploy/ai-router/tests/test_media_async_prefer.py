from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ai_router.audit import AuditLog
from ai_router.auth import AuthManager
from ai_router.errors import AuthenticationError, RouterError
from ai_router.media_service.app import create_app
from ai_router.media_service.gateway import router
from ai_router.store import InMemoryStateStore
from ai_router.types import ClientPolicy
from test_media_service import png, service


@pytest.fixture(params=[False, True], ids=["public", "admin"])
def media_gateway(request, tmp_path, monkeypatch):
    admin = request.param
    monkeypatch.setenv("AI_ROUTER_MEDIA_INTERNAL_KEY", "internal-test")
    monkeypatch.setenv("AI_ROUTER_MEDIA_URL", "http://127.0.0.1:14020")
    monkeypatch.setenv("AI_ROUTER_ADMIN_KEY", "admin-test")
    monkeypatch.setenv("AI_ROUTER_MEDIA_SYNC_WAIT", "10")

    @asynccontextmanager
    async def session():
        svc = service(tmp_path)
        requests = []
        policies = {
            token: ClientPolicy(owner, "", ("siyuan/auto",), 30, 1000, 1,
                                disclosure_mode="public", media_models=models)
            for token, owner, models in (
                ("client-test", "alice", ("siyuan-image", "siyuan-video")),
                ("other-test", "bob", ("siyuan-image", "siyuan-video")),
                ("no-media-test", "ungranted", ()),
            )
        }

        async def authenticate(token):
            if token not in policies:
                raise AuthenticationError()
            return policies[token], token + "-id"

        runtime = SimpleNamespace(
            auth=AuthManager(None, SimpleNamespace(authenticate=authenticate)),
            store=InMemoryStateStore(), audit=AuditLog(tmp_path / "audit.jsonl"),
            track_request_started=AsyncMock(), track_request_finished=AsyncMock(),
            draining=False,
        )
        internal = httpx.ASGITransport(app=create_app(svc, run_worker=False))

        async def handle(request):
            requests.append(request)
            return await internal.handle_async_request(request)

        app = FastAPI()
        app.state.runtime = runtime
        app.state.media_transport = httpx.MockTransport(handle)
        app.include_router(router(admin=admin))

        @app.exception_handler(RouterError)
        async def router_error(request, exc):
            return JSONResponse({"error": {"code": exc.code, "message": str(exc)}},
                                status_code=exc.status_code)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer admin-test" if admin else "Bearer client-test"},
        ) as client:
            try:
                yield SimpleNamespace(
                    client=client, app=app, svc=svc, requests=requests, runtime=runtime,
                    admin=admin, owner="admin" if admin else "alice",
                    prefix="/api/media" if admin else "/v1",
                )
            finally:
                await svc.close()
                await internal.aclose()

    return session


async def submit_image(env, edit, *, headers=None, **body):
    body = {"prompt": "A green cup", **body}
    path = env.prefix + ("/images/edits" if edit else "/images/generations")
    if edit:
        return await env.client.post(path, data=body, files={"image": ("cup.png", png(), "image/png")},
                                     headers=headers)
    return await env.client.post(path, json=body, headers=headers)


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("response_format", ["b64_json", "url"])
def test_async_returns_queued_job_without_polling(media_gateway, monkeypatch, edit, response_format):
    monkeypatch.setenv("AI_ROUTER_MEDIA_SYNC_WAIT", "0")

    async def scenario():
        async with media_gateway() as env:
            headers = {"Prefer": "respond-async", "Idempotency-Key": "queued"}
            first = await submit_image(env, edit, headers=headers, response_format=response_format)
            repeat = await submit_image(env, edit, headers=headers, response_format=response_format)
            for response in (first, repeat):
                assert response.status_code == 202, response.text
                assert response.headers["Preference-Applied"] == "respond-async"
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["X-Request-ID"]
                job = response.json()
                assert job["status"] == "queued"
                assert job["object"] == "image"
                assert "data" not in job and "b64_json" not in response.text
                assert "output" not in job
                assert ("provider_state" in job) == env.admin
            assert first.json()["id"] == repeat.json()["id"]
            assert len(env.svc.store.list(env.owner, "image")["data"]) == 1
            assert [(r.method, r.url.path) for r in env.requests] == [("POST", "/jobs/image")] * 2
            for upstream in env.requests:
                assert upstream.url.params["edit"] == str(edit).lower()
                assert upstream.headers["Idempotency-Key"] == "queued"
                assert upstream.headers["X-Media-Owner"] == env.owner
                assert upstream.headers["X-Media-Admin"] == str(env.admin).lower()
            assert env.svc.codex.calls == env.svc.qwen.calls == 0
            assert env.svc.runner is None
            assert env.runtime.track_request_started.await_count == 2
            assert env.runtime.track_request_finished.await_count == 2
            for started, finished in zip(env.runtime.track_request_started.await_args_list,
                                         env.runtime.track_request_finished.await_args_list):
                assert started.args[0] == finished.args[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("response_format", ["b64_json", "url"])
def test_completed_idempotent_async_replay_stays_202(media_gateway, edit, response_format):
    async def scenario():
        async with media_gateway() as env:
            headers = {"Prefer": "respond-async", "Idempotency-Key": "completed"}
            first = await submit_image(env, edit, headers=headers, response_format=response_format)
            assert first.status_code == 202, first.text
            job_id = first.json()["id"]
            # Complete only the local fake provider; no worker or external generation runs.
            await env.svc._image(env.svc.store.get(job_id))
            assert env.svc.codex.calls == 1
            replay = await submit_image(env, edit, headers=headers, response_format=response_format)
            assert replay.status_code == 202, replay.text
            assert replay.headers["Preference-Applied"] == "respond-async"
            job = replay.json()
            assert job["id"] == job_id and job["status"] == "completed"
            assert env.svc.codex.calls == 1
            assert all(r.method == "POST" for r in env.requests)
            assert len(env.svc.store.list(env.owner, "image")["data"]) == 1
            assert job["output"]["content_url"].startswith(env.prefix + "/")
            if response_format == "url":
                assert "b64_json" not in job["data"][0]
                assert job["data"][0]["url"] == job["output"]["content_url"]
            else:
                assert base64.b64decode(job["data"][0]["b64_json"]) == png()
            synchronous = await submit_image(
                env, edit, headers={"Idempotency-Key": "completed"}, response_format=response_format,
            )
            assert synchronous.status_code == 200, synchronous.text
            assert "Preference-Applied" not in synchronous.headers
            assert synchronous.json()["id"] == job_id
            assert env.svc.codex.calls == 1
            if response_format == "url":
                assert "b64_json" not in synchronous.json()["data"][0]
                assert synchronous.json()["data"][0]["url"] == synchronous.json()["output"]["content_url"]
            else:
                assert synchronous.json()["data"] == job["data"]
            download = await env.client.get(env.prefix + f"/images/{job_id}/content")
            assert download.status_code == 200 and download.content == png()

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("prefer", [None, "wait=2", "respond-async-no",
                                  'return="x,respond-async,y"', "respond-async=false"])
def test_default_or_unrelated_preference_keeps_sync_polling(media_gateway, edit, prefer):
    async def scenario():
        async with media_gateway() as env:
            calls = []

            async def handle(request):
                calls.append((request.method, request.url.path))
                return httpx.Response(202 if request.method == "POST" else 200, json={
                    "id": "img_sync", "object": "image",
                    "status": "queued" if request.method == "POST" else "completed",
                    **({"data": [{"b64_json": base64.b64encode(png()).decode()}]}
                       if request.method == "GET" else {}),
                })

            env.app.state.media_transport = httpx.MockTransport(handle)
            response = await submit_image(env, edit, headers={"Prefer": prefer} if prefer is not None else {})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "completed"
            assert base64.b64decode(response.json()["data"][0]["b64_json"]) == png()
            assert "Preference-Applied" not in response.headers
            assert calls == [("POST", "/jobs/image"), ("GET", "/jobs/img_sync")]

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
def test_sync_timeout_keeps_job_for_async_idempotent_retry(media_gateway, monkeypatch, edit):
    monkeypatch.setenv("AI_ROUTER_MEDIA_SYNC_WAIT", "0")

    async def scenario():
        async with media_gateway() as env:
            headers = {"Idempotency-Key": "timeout"}
            response = await submit_image(env, edit, headers=headers)
            assert response.status_code == 504, response.text
            assert response.json()["error"]["code"] == "media_wait_timeout"
            assert "Preference-Applied" not in response.headers
            retry = await submit_image(env, edit, headers={**headers, "Prefer": "respond-async"})
            assert retry.status_code == 202, retry.text
            assert retry.json()["id"] == response.json()["error"]["id"]
            assert retry.json()["status"] == "queued"
            assert "data" not in retry.json()
            assert len(env.svc.store.list(env.owner, "image")["data"]) == 1
            assert [(r.method, r.url.path) for r in env.requests] == [("POST", "/jobs/image")] * 2
            assert env.svc.codex.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("prefer", [
    [("Prefer", " ReSpOnD-AsYnC ")],
    [("Prefer", "wait=5, respond-async; note=accepted")],
    [("Prefer", 'return="x,y"'), ("Prefer", "respond-async")],
])
def test_async_preference_tokens_and_repeated_headers(media_gateway, monkeypatch, prefer):
    monkeypatch.setenv("AI_ROUTER_MEDIA_SYNC_WAIT", "0")

    async def scenario():
        async with media_gateway() as env:
            response = await submit_image(env, False, headers=prefer)
            assert response.status_code == 202, response.text
            assert response.headers["Preference-Applied"] == "respond-async"
            assert response.json()["status"] == "queued"
            assert len(env.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("prefer", [None, "respond-async"])
@pytest.mark.parametrize("token", [None, "invalid-test", "no-media-test"])
def test_auth_and_media_grants_remain_required(media_gateway, edit, prefer, token):
    async def scenario():
        async with media_gateway() as env:
            env.client.headers.pop("Authorization")
            headers = {}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            if prefer is not None:
                headers["Prefer"] = prefer
            response = await submit_image(env, edit, headers=headers)
            forbidden = token == "no-media-test" and not env.admin
            assert response.status_code == (403 if forbidden else 401), response.text
            assert response.json()["error"]["code"] == ("media_forbidden" if forbidden else "invalid_api_key")
            assert "Preference-Applied" not in response.headers
            assert env.requests == []
            assert env.svc.store.list(None, "image")["data"] == []
            assert env.svc.codex.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("prefer", [None, "respond-async"])
@pytest.mark.parametrize("gate", ["draining", "rate_limit"])
def test_admission_errors_are_not_accepted_jobs(media_gateway, edit, prefer, gate):
    async def scenario():
        async with media_gateway() as env:
            if gate == "draining":
                env.runtime.draining = True
            else:
                await env.runtime.store.increment_window(f"router:media-submit:{env.owner}", 30, 60)
            response = await submit_image(env, edit, headers={"Prefer": prefer} if prefer else {})
            assert response.status_code == (503 if gate == "draining" else 429), response.text
            assert response.json()["error"]["code"] == (
                "router_draining" if gate == "draining" else "media_rate_limit"
            )
            assert "Preference-Applied" not in response.headers
            assert env.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
def test_async_idempotency_conflict_is_still_409(media_gateway, edit):
    async def scenario():
        async with media_gateway() as env:
            headers = {"Prefer": "respond-async", "Idempotency-Key": "conflict"}
            first = await submit_image(env, edit, headers=headers)
            assert first.status_code == 202, first.text
            changed = await submit_image(env, edit, headers=headers, prompt="Different cup")
            assert changed.status_code == 409, changed.text
            assert "Preference-Applied" not in changed.headers
            assert len(env.svc.store.list(env.owner, "image")["data"]) == 1
            assert env.svc.codex.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("edit", [False, True], ids=["generate", "edit"])
@pytest.mark.parametrize("prefer", [None, "respond-async"])
def test_terminal_failed_jobs_preserve_sync_status(media_gateway, edit, prefer):
    async def scenario():
        async with media_gateway() as env:
            headers = {"Prefer": "respond-async", "Idempotency-Key": "failed"}
            first = await submit_image(env, edit, headers=headers)
            assert first.status_code == 202, first.text
            env.svc.store.update(first.json()["id"], status="failed",
                                 error={"code": "media_test_failure", "message": "Synthetic failure."})
            headers = {"Idempotency-Key": "failed", **({"Prefer": prefer} if prefer else {})}
            response = await submit_image(env, edit, headers=headers)
            assert response.status_code == (202 if prefer else 503), response.text
            assert ("Preference-Applied" in response.headers) == bool(prefer)
            assert response.json()["status"] == "failed"
            assert response.json()["error"]["code"] == "media_test_failure"
            assert "data" not in response.json()
            assert len(env.requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("prefer", [None, "respond-async"])
def test_video_keeps_existing_202_flow(media_gateway, prefer):
    async def scenario():
        async with media_gateway() as env:
            response = await env.client.post(
                env.prefix + "/videos", data={"prompt": "A rotating cup", "duration": "4"},
                files={"reference_image": ("cup.png", png(), "image/png")},
                headers={"Prefer": prefer} if prefer else {},
            )
            assert response.status_code == 202, response.text
            assert response.json()["object"] == "video"
            assert "Preference-Applied" not in response.headers
            assert [(r.method, r.url.path) for r in env.requests] == [("POST", "/jobs/video")]
            assert env.svc.h3.creates == 0

    asyncio.run(scenario())
