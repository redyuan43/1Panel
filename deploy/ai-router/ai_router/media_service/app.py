from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .contracts import MAX_UPLOAD_BYTES, MediaError
from .service import MediaService
from .storage import MediaStore


def create_app(service: MediaService | None = None, *, run_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service or MediaService(MediaStore(
            os.environ.get("AI_ROUTER_MEDIA_ROOT", "/opt/1panel/ai-router/media"),
        ))
        if run_worker:
            await app.state.service.start()
        yield
        await app.state.service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    if service:
        app.state.service = service

    @app.middleware("http")
    async def protect(request, call_next):
        key = os.environ.get("AI_ROUTER_MEDIA_INTERNAL_KEY", "")
        if not key or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
            return JSONResponse({"error": {"code": "invalid_api_key", "message": "Authentication required."}}, status_code=401)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": {"code": "invalid_content_length"}}, status_code=400)
        if length > MAX_UPLOAD_BYTES * 4 // 3 + 65536:
            return JSONResponse({"error": {"code": "media_too_large"}}, status_code=413)
        return await call_next(request)

    @app.exception_handler(MediaError)
    async def error_handler(request, exc):
        return JSONResponse(exc.payload(), status_code=exc.status)

    def principal(request):
        admin = request.headers.get("x-media-admin") == "true"
        owner = request.headers.get("x-media-owner")
        if not owner:
            raise MediaError("invalid_principal", "Trusted owner context is required.", 401)
        return owner, admin

    def current(request) -> MediaService:
        return request.app.state.service

    @app.get("/health")
    async def health(request: Request):
        return {"ok": True, "contract_version": 1, "workflow_contract_version": 2}

    @app.get("/settings")
    async def settings(request: Request):
        _, admin = principal(request)
        if not admin:
            raise MediaError("forbidden", "Administrator access required.", 403)
        return current(request).store.settings()

    @app.put("/settings")
    async def configure(request: Request):
        _, admin = principal(request)
        if not admin:
            raise MediaError("forbidden", "Administrator access required.", 403)
        return current(request).store.configure(await request.json())

    @app.get("/options")
    async def options(request: Request):
        return await current(request).options(request.headers.get("x-media-models", "").split(","))

    @app.post("/jobs/{kind}")
    async def submit(kind: str, request: Request):
        owner, admin = principal(request)
        if kind not in {"image", "video"}:
            raise MediaError("invalid_media_kind", "Unknown media kind.")
        value = await request.json()
        model = value.get("model", "siyuan-image" if kind == "image" else "siyuan-video")
        allowed = request.headers.get("x-media-models", "").split(",")
        if not admin and model not in allowed:
            raise MediaError("media_forbidden", "Media model is not permitted.", 403)
        job = current(request).submit(
            owner, kind, value, request.headers.get("idempotency-key", uuid4().hex),
            request.headers.get("x-request-id", uuid4().hex), edit=request.query_params.get("edit") == "true",
        )
        return JSONResponse(current(request).public(job, internal=admin), status_code=202)

    @app.get("/jobs")
    async def listing(request: Request):
        owner, admin = principal(request)
        page = current(request).store.list(None if admin else owner, request.query_params.get("kind"),
                                           after=request.query_params.get("after"))
        page["data"] = [current(request).public(job, internal=admin, include_data=False) for job in page["data"]]
        # Listing must never expand Base64 for the entire history.
        for item in page["data"]:
            item.pop("data", None)
        return page

    @app.get("/jobs/{job_id}")
    async def get(job_id: str, request: Request):
        owner, admin = principal(request)
        job = current(request).store.get(job_id, None if admin else owner)
        return current(request).public(job, internal=admin)

    @app.delete("/jobs/{job_id}")
    async def delete(job_id: str, request: Request):
        owner, admin = principal(request)
        job = current(request).store.get(job_id, None if admin else owner)
        if job["kind"] == "image" and not admin:
            raise MediaError("forbidden", "Only administrators may delete images.", 403)
        if job["status"] not in {"completed", "failed", "cancelled"}:
            raise MediaError("media_busy", "Only terminal tasks may be deleted.", 409)
        current(request).store.update(job_id, deleted=True)
        return {"id": job_id, "deleted": True}

    @app.get("/jobs/{job_id}/outputs")
    async def outputs(job_id: str, request: Request):
        owner, admin = principal(request)
        current(request).store.get(job_id, None if admin else owner)
        return {"data": [value for output in current(request).store.outputs(job_id)
                         if (value := current(request).public_output(output, internal=admin)) is not None]}

    @app.post("/jobs/{job_id}/stages/{stage}/{action}")
    async def action(job_id: str, stage: str, action: str, request: Request):
        owner, admin = principal(request)
        existing = current(request).store.get(job_id, None if admin else owner)
        if existing.get("creative_workflow_id"):
            raise MediaError("managed_by_workflow", "请通过所属创作工作流操作此任务。", 409)
        if action in {"start", "regenerate"} and not admin and (
            "siyuan-video" not in request.headers.get("x-media-models", "").split(",")
        ):
            raise MediaError("media_forbidden", "Video generation is not permitted.", 403)
        idem = request.headers.get("idempotency-key")
        if not idem or len(idem) > 128:
            raise MediaError("idempotency_required", "Stage operations require an Idempotency-Key.")
        job = await current(request).action(job_id, stage, action, await request.json(), idem, None if admin else owner,
                                            request.headers.get("x-request-id"))
        return current(request).public(job, internal=admin)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_image(job_id: str, request: Request):
        owner, admin = principal(request)
        job = await current(request).cancel_image(job_id, None if admin else owner)
        return current(request).public(job, internal=admin)

    @app.post("/jobs/{job_id}/purge")
    async def purge(job_id: str, request: Request):
        _, admin = principal(request)
        if not admin:
            raise MediaError("forbidden", "Administrator access required.", 403)
        body = await request.json()
        return current(request).purge(job_id, body.get("confirm", ""))

    @app.api_route("/outputs/{output_id}/content", methods=["GET", "HEAD"])
    async def content(output_id: str, request: Request):
        owner, admin = principal(request)
        output = current(request).store.artifact(output_id)
        job = current(request).store.get(output["job_id"], None if admin else owner)
        path = current(request).output_path(job, output)
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                     "video/mp4": ".mp4", "text/plain": ".txt"}.get(output["content_type"], "")
        return FileResponse(path, media_type=output["content_type"],
                            filename=output_id + extension,
                            headers={"Cache-Control": "private, no-store"})

    from .creative_api import install_internal
    install_internal(app, current, principal)
    return app


app = create_app()
