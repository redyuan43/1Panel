from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from urllib.request import parse_http_list
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile

from .contracts import ID_PATTERN, MAX_UPLOAD_BYTES, MODELS, PUBLIC_MODELS, MediaError


class MediaAccessLogFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2].partition("?")[0]
            if path.startswith(("/v1/media/outputs/", "/api/media/outputs/")):
                record.args = (*args[:2], path, *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(MediaAccessLogFilter())


def model_descriptors(policy) -> list[dict]:
    return [
        {"id": model, "object": "model", "owned_by": "siyuan",
         "output_modalities": ["video" if model == "siyuan-video" else "image"]}
        for model in policy.media_models
        if model in MODELS and (policy.disclosure_mode != "public" or model in PUBLIC_MODELS)
    ]


@asynccontextmanager
async def connection(request: Request, principal: dict):
    key = os.environ.get("AI_ROUTER_MEDIA_INTERNAL_KEY", "")
    if not key:
        raise MediaError("media_unavailable", "Media service is not configured.", 503)
    base = os.environ.get("AI_ROUTER_MEDIA_URL", "http://127.0.0.1:14020")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in {"", "/"}:
        raise MediaError("media_unavailable", "Invalid internal media endpoint.", 503)
    headers = {
        "Authorization": "Bearer " + key, "X-Media-Owner": principal["owner"],
        "X-Media-Admin": str(principal["admin"]).lower(),
        "X-Media-Models": ",".join(principal["models"]),
        "X-Request-ID": request.state.server_request_id,
    }
    if request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = request.headers["idempotency-key"]
    async with httpx.AsyncClient(
        base_url=base, headers=headers, timeout=30, trust_env=False, follow_redirects=False,
        transport=getattr(request.app.state, "media_transport", None),
    ) as client:
        yield client


async def rpc(client, method, path, **kwargs):
    try:
        response = await client.request(method, path, **kwargs)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MediaError("media_unavailable", "Media service is temporarily unavailable.", 503) from exc
    if response.is_error:
        error = payload.get("error", {})
        raise MediaError(error.get("code", "media_request_failed"),
                         error.get("message", "Media operation failed."), response.status_code)
    return payload


def router(*, admin: bool = False) -> APIRouter:
    result = APIRouter(prefix="/api/media" if admin else "/v1")

    async def authenticate(request):
        request.state.server_request_id = uuid4().hex
        runtime = request.app.state.runtime
        if admin:
            runtime.auth.authenticate_admin(request.headers.get("authorization"))
            request.state.media_owner = "admin"
            return {"owner": "admin", "admin": True, "models": MODELS, "rpm_limit": 30}
        client = await runtime.auth.authenticate(request.headers.get("authorization"))
        request.state.disclosure_mode = client.policy.disclosure_mode
        request.state.media_owner = client.policy.id
        return {"owner": client.policy.id, "admin": False, "models": client.policy.media_models,
                "key_id": client.key_id, "rpm_limit": min(client.policy.rpm_limit, 30)}

    def endpoint(fn):
        async def guarded(request: Request):
            try:
                for name in ("job_id", "output_id", "stage"):
                    if name in request.path_params and not ID_PATTERN.fullmatch(request.path_params[name]):
                        raise MediaError("invalid_media_id", "Invalid media identifier.")
                receive = request._receive
                received = 0
                async def bounded_receive():
                    nonlocal received
                    message = await receive()
                    received += len(message.get("body", b""))
                    if received > MAX_UPLOAD_BYTES + 65536:
                        raise MediaError("media_too_large", "Request body exceeds the media limit.", 413)
                    return message
                request._receive = bounded_receive
                response = await fn(request)
                response.headers.setdefault("X-Request-ID", getattr(request.state, "server_request_id", uuid4().hex))
                response.headers.setdefault("Cache-Control", "no-store")
                return response
            except MediaError as exc:
                return JSONResponse(exc.payload(), status_code=exc.status,
                                    headers={"X-Request-ID": getattr(request.state, "server_request_id", uuid4().hex)})
            except (ValueError, TypeError):
                return JSONResponse({"error": {"code": "invalid_media_parameters", "message": "Invalid media request."}},
                                    status_code=400)
            finally:
                tracking_id = getattr(request.state, "media_tracking_id", None)
                if tracking_id:
                    await request.app.state.runtime.track_request_finished(tracking_id)
                try:
                    request.app.state.runtime.audit.write(
                        "media_http_request", request_id=getattr(request.state, "server_request_id", None),
                        client_id=getattr(request.state, "media_owner", None), method=request.method,
                        path=request.url.path, media_job_id=request.path_params.get("job_id"),
                    )
                except Exception:
                    pass
        return guarded

    async def form_body(request, *, video=False):
        content = request.headers.get("content-type", "")
        if "multipart/form-data" not in content:
            if video or request.url.path.endswith("/edits"):
                raise MediaError("multipart_required", "This endpoint requires multipart data.", 415)
            try:
                data = await request.json()
            except ValueError as exc:
                raise MediaError("invalid_json", "Invalid JSON body.") from exc
            if not isinstance(data, dict):
                raise MediaError("invalid_json", "Expected a JSON object.")
            return data
        value, assets, images, total = {}, {}, [], 0
        async with request.form(max_files=5, max_fields=30, max_part_size=MAX_UPLOAD_BYTES) as form:
            for name, item in form.multi_items():
                if isinstance(item, UploadFile):
                    data = bytearray()
                    while chunk := await item.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD_BYTES:
                            raise MediaError("media_too_large", "Combined upload limit exceeded.", 413)
                        data.extend(chunk)
                    asset = {"data": base64.b64encode(data).decode(), "content_type": item.content_type or "application/octet-stream"}
                    if video:
                        if name in assets:
                            raise MediaError("invalid_asset", "Duplicate asset field.")
                        assets[name] = asset
                    elif name in {"image", "image[]", "images"}:
                        images.append(asset)
                    else:
                        raise MediaError("invalid_asset", "Unknown image field.")
                else:
                    if name in value:
                        raise MediaError("invalid_media_parameters", "Duplicate parameter.")
                    value[name] = item
        try:
            for name in ("n", "duration", "seed"):
                if name in value:
                    value[name] = int(value[name])
            for name in ("watermark", "use_embedded_video_audio"):
                if name in value:
                    if value[name] not in {"true", "false"}:
                        raise ValueError(name)
                    value[name] = value[name] == "true"
        except ValueError as exc:
            raise MediaError("invalid_media_parameters", "Invalid numeric or boolean value.") from exc
        if video:
            value["assets"] = assets
        else:
            value["images"] = images
        return value

    async def decorate(request, principal, job):
        base = "/api/media" if admin else "/v1"
        async def decorate_output(output):
            nonce = secrets.token_urlsafe(32)
            artifact_id = output["id"]
            identity = {**principal, "output_id": artifact_id}
            if admin:
                identity["admin_digest"] = hashlib.sha256(os.environ.get("AI_ROUTER_ADMIN_KEY", "").encode()).hexdigest()
            await request.app.state.runtime.store.set_json(
                "router:media-ticket:" + hashlib.sha256(nonce.encode()).hexdigest(), identity, ttl_seconds=300,
            )
            output["content_url"] = f"{base}/media/outputs/{artifact_id}/content?access={nonce}" if not admin else (
                f"{base}/outputs/{artifact_id}/content?access={nonce}"
            )
            output["expires_at"] = int(time.time()) + 300
        if job.get("output"):
            await decorate_output(job["output"])
        for stage in job.get("stages", []):
            if stage.get("output"):
                await decorate_output(stage["output"])
            for output in stage.get("artifacts", []):
                await decorate_output(output)
        return job

    async def options(request):
        principal = await authenticate(request)
        async with connection(request, principal) as client:
            return JSONResponse(await rpc(client, "GET", "/options"))
    result.add_api_route("/options" if admin else "/media/options", endpoint(options), methods=["GET"])

    async def submit(request):
        principal = await authenticate(request)
        if getattr(request.app.state.runtime, "draining", False):
            raise MediaError("router_draining", "Router is draining; retry with the same idempotency key.", 503)
        runtime = request.app.state.runtime
        if hasattr(runtime, "track_request_started"):
            request.state.media_tracking_id = "media:" + request.state.server_request_id
            await runtime.track_request_started(request.state.media_tracking_id, request.state.server_request_id, None)
        video = request.url.path.endswith("/videos")
        respond_async = not video and any(
            preference.partition(";")[0].strip().lower() == "respond-async"
            for value in request.headers.getlist("prefer")
            for preference in parse_http_list(value)
        )
        body = await form_body(request, video=video)
        model = body.get("model", "siyuan-video" if video else "siyuan-image")
        if not principal["admin"] and model not in principal["models"]:
            raise MediaError("media_forbidden", "Media model is not permitted.", 403)
        # Apply HTTP request admission independently from long-running job slots.
        store = request.app.state.runtime.store
        if await store.increment_window(f"router:media-submit:{principal['owner']}", 1, 60) > principal["rpm_limit"]:
            raise MediaError("media_rate_limit", "Media submission limit reached.", 429)
        async with connection(request, principal) as client:
            job = await rpc(client, "POST", "/jobs/" + ("video" if video else "image"),
                            params={"edit": str(request.url.path.endswith("/edits")).lower()}, json=body)
            request.app.state.runtime.audit.write("media_request", request_id=request.state.server_request_id,
                                                  media_job_id=job["id"], client_id=principal["owner"])
            if not video and not respond_async:
                deadline = time.monotonic() + float(os.environ.get("AI_ROUTER_MEDIA_SYNC_WAIT", "750"))
                while job["status"] not in {"completed", "failed", "cancelled", "reconciling", "cancelling"}:
                    if time.monotonic() >= deadline or await request.is_disconnected():
                        raise MediaError("media_wait_timeout", "Task continues; query or retry with the same key.", 504, id=job["id"])
                    await asyncio.sleep(1)
                    job = await rpc(client, "GET", "/jobs/" + job["id"])
            job = await decorate(request, principal, job)
            if not video and job.get("status") == "completed" and body.get("response_format") == "url":
                job["data"] = [{"url": job["output"]["content_url"], "revised_prompt": job["data"][0].get("revised_prompt")}]
            return JSONResponse(job, status_code=202 if video or respond_async else (200 if job["status"] == "completed" else 503),
                                headers={"X-Request-ID": request.state.server_request_id, "Cache-Control": "no-store",
                                         **({"Preference-Applied": "respond-async"} if respond_async else {})})
    for path in ("/images/generations", "/images/edits", "/videos"):
        result.add_api_route(path, endpoint(submit), methods=["POST"])

    async def listing(request):
        principal = await authenticate(request)
        kind = "image" if "/images" in request.url.path else "video"
        async with connection(request, principal) as client:
            page = await rpc(client, "GET", "/jobs", params={"kind": kind, **(
                {"after": request.query_params["after"]} if "after" in request.query_params else {}
            )})
            page["data"] = [await decorate(request, principal, job) for job in page["data"]]
            return JSONResponse(page)
    for path in ("/images", "/videos"):
        result.add_api_route(path, endpoint(listing), methods=["GET"])

    async def output_versions(request):
        principal = await authenticate(request)
        async with connection(request, principal) as client:
            page = await rpc(client, "GET", f"/jobs/{request.path_params['job_id']}/outputs")
        for output in page["data"]:
            await decorate(request, principal, {"output": output})
        return JSONResponse(page)
    for kind in ("images", "videos"):
        result.add_api_route(f"/{kind}/{{job_id}}/outputs", endpoint(output_versions), methods=["GET"])

    async def cancel_image(request):
        principal = await authenticate(request)
        async with connection(request, principal) as client:
            return JSONResponse(await rpc(client, "POST", f"/jobs/{request.path_params['job_id']}/cancel"))
    result.add_api_route("/images/{job_id}/cancel", endpoint(cancel_image), methods=["POST"])

    async def job_action(request):
        principal = await authenticate(request)
        job_id = request.path_params["job_id"]
        async with connection(request, principal) as client:
            if request.method == "DELETE":
                return JSONResponse(await rpc(client, "DELETE", "/jobs/" + job_id))
            if request.method == "POST":
                stage = request.path_params["stage"]
                action = request.path_params["action"]
                if action in {"start", "regenerate"} and await request.app.state.runtime.store.increment_window(
                    f"router:media-submit:{principal['owner']}", 1, 60,
                ) > principal["rpm_limit"]:
                    raise MediaError("media_rate_limit", "Media submission limit reached.", 429)
                body = await request.json()
                if not isinstance(body, dict):
                    raise MediaError("invalid_json", "Expected an operation object.")
                job = await rpc(client, "POST", f"/jobs/{job_id}/stages/{stage}/{action}", json=body)
            else:
                job = await rpc(client, "GET", "/jobs/" + job_id)
            job = await decorate(request, principal, job)
            stage_id = request.path_params.get("stage")
            if stage_id:
                stage = next((item for item in job["stages"] if item["id"] == stage_id), None)
                if not stage:
                    raise MediaError("stage_not_found", "Stage is not in this pipeline.", 404)
                if request.url.path.endswith("/content"):
                    output = stage.get("output")
                    if not output:
                        raise MediaError("output_not_ready", "Stage output has not been archived.", 409)
                    return await serve_content(request, principal, output["id"])
                return JSONResponse(stage if request.method == "GET" else job)
            if request.url.path.endswith("/content"):
                if not job.get("output"):
                    raise MediaError("output_not_ready", "Final output is not available.", 409)
                return await serve_content(request, principal, job["output"]["id"])
            return JSONResponse({"data": job["stages"]} if request.url.path.endswith("/stages") else job)
    for kind in ("images", "videos"):
        result.add_api_route(f"/{kind}/{{job_id}}", endpoint(job_action), methods=["GET", "DELETE"])
        result.add_api_route(f"/{kind}/{{job_id}}/content", endpoint(job_action), methods=["GET", "HEAD"])
    for suffix in ("/stages", "/stages/{stage}", "/stages/{stage}/content"):
        result.add_api_route("/videos/{job_id}" + suffix, endpoint(job_action), methods=["GET", "HEAD"])
    result.add_api_route("/videos/{job_id}/stages/{stage}/{action}", endpoint(job_action), methods=["POST"])

    async def serve_content(request, principal, output_id):
        manager = connection(request, principal)
        client = await manager.__aenter__()
        try:
            forwarded = {key: request.headers[key] for key in ("range", "if-range") if key in request.headers}
            upstream = await client.send(client.build_request(request.method, f"/outputs/{output_id}/content", headers=forwarded), stream=True)
            if upstream.status_code not in {200, 206, 416}:
                status_code = upstream.status_code
                await upstream.aclose()
                raise MediaError("output_not_ready" if status_code in {409, 410} else "artifact_not_found",
                                 "Refresh the task to obtain an accessible output.", status_code if status_code in {409, 410} else 404)
        except BaseException:
            await manager.__aexit__(None, None, None)
            raise
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await manager.__aexit__(None, None, None)
        allowed = {"content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified", "content-disposition"}
        headers = {key: value for key, value in upstream.headers.items() if key.lower() in allowed}
        headers["Cache-Control"] = "private, no-store"
        headers["X-Content-Type-Options"] = "nosniff"
        return StreamingResponse(chunks(), status_code=upstream.status_code, headers=headers)

    async def output_content(request):
        output_id = request.path_params["output_id"]
        ticket = request.query_params.get("access")
        if ticket:
            request.state.server_request_id = uuid4().hex
            runtime = request.app.state.runtime
            principal = await runtime.store.get_json("router:media-ticket:" + hashlib.sha256(ticket.encode()).hexdigest())
            if not principal or principal.get("output_id") != output_id or principal.get("admin") != admin:
                raise MediaError("invalid_media_ticket", "Preview ticket expired or is invalid.", 401)
            if admin:
                if principal["admin_digest"] != hashlib.sha256(os.environ.get("AI_ROUTER_ADMIN_KEY", "").encode()).hexdigest():
                    raise MediaError("invalid_media_ticket", "Preview ticket was revoked.", 401)
            elif not await runtime.clients.is_key_active(principal["owner"], principal["key_id"]):
                raise MediaError("invalid_media_ticket", "Preview ticket was revoked.", 401)
        else:
            principal = await authenticate(request)
        request.state.media_owner = principal["owner"]
        return await serve_content(request, principal, output_id)
    result.add_api_route("/outputs/{output_id}/content" if admin else "/media/outputs/{output_id}/content",
                         endpoint(output_content), methods=["GET", "HEAD"])

    if admin:
        async def purge(request):
            principal = await authenticate(request)
            async with connection(request, principal) as client:
                return JSONResponse(await rpc(client, "POST", f"/jobs/{request.path_params['job_id']}/purge",
                                              json=await request.json()))
        result.add_api_route("/jobs/{job_id}/purge", endpoint(purge), methods=["POST"])
        async def settings(request):
            principal = await authenticate(request)
            async with connection(request, principal) as client:
                return JSONResponse(await rpc(client, request.method, "/settings",
                                              **({"json": await request.json()} if request.method == "PUT" else {})))
        result.add_api_route("/settings", endpoint(settings), methods=["GET", "PUT"])
    from .creative_api import install_gateway
    install_gateway(result, endpoint, authenticate, decorate, admin=admin)
    return result
