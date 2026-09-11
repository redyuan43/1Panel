"""Authenticated adapters; browser and chat share the same durable operations."""
import asyncio
import base64
import json
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile

from .contracts import MediaError, MAX_IMAGE_BYTES


def install_internal(app, current, principal):
    def access(request, workflow=None, kind=None):
        owner, admin = principal(request)
        models = request.headers.get("x-media-models", "").split(",")
        if kind and not admin and "siyuan-" + kind not in models:
            raise MediaError("media_forbidden", "当前账户未获此媒体能力授权。", 403)
        return owner, admin

    @app.post("/creative/assets")
    async def upload(request: Request):
        owner, admin = access(request)
        if not admin and not set(request.headers.get("x-media-models", "").split(",")) & {"siyuan-image", "siyuan-video"}:
            raise MediaError("media_forbidden", "当前账户未获媒体授权。", 403)
        return current(request).creative.upload(owner, await request.json())

    @app.api_route("/creative/workflows", methods=["GET", "POST"])
    async def collection(request: Request):
        service = current(request).creative
        owner, admin = access(request)
        if request.method == "GET":
            return {"data": [service.public(w, admin=admin) for w in service.listing(None if admin else owner)]}
        value = await request.json()
        if not isinstance(value, dict):
            raise MediaError("invalid_workflow", "创作参数必须是对象。")
        access(request, kind=value.get("kind", "video"))
        workflow = service.create(owner, value, request.headers.get("idempotency-key"))
        return JSONResponse(service.public(workflow, admin=admin), status_code=202)

    @app.get("/creative/workflows/{workflow_id}")
    async def get(workflow_id: str, request: Request):
        owner, admin = access(request)
        service = current(request).creative
        return service.public(service.get(workflow_id, None if admin else owner), admin=admin)

    @app.post("/creative/workflows/{workflow_id}/actions")
    async def action(workflow_id: str, request: Request):
        owner, admin = access(request)
        service = current(request).creative
        workflow = service.get(workflow_id, None if admin else owner)
        body = await request.json()
        if not isinstance(body, dict):
            raise MediaError("invalid_operation", "操作内容必须是对象。")
        if body.get("action") not in {"cancel"}:
            access(request, kind=workflow["spec"]["kind"])
        if body.get("convert_references") or workflow["spec"].get("direction_images") or ((body.get("action") == "add_direction" or body.get("action") == "rerun" and body.get("prompt") is not None) and any(d.get("image_job_id") for d in workflow["directions"])):
            access(request, kind="image")
        if not isinstance(body.get("spec", {}), dict):
            raise MediaError("invalid_workflow", "方案修改必须是对象。")
        if body.get("spec", {}).get("kind"):
            access(request, kind=body["spec"]["kind"])
        result = await service.action(workflow_id, None if admin else owner, body, request.headers.get("idempotency-key"))
        return service.public(result, admin=admin)

    @app.get("/creative/workflows/{workflow_id}/events")
    async def events(workflow_id: str, request: Request):
        owner, admin = access(request)
        current(request).creative.get(workflow_id, None if admin else owner)
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            raise MediaError("invalid_event_cursor", "事件游标无效。")
        with current(request).store.connect() as db:
            rows = db.execute("SELECT sequence,created,value FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 100", (workflow_id, after)).fetchall()
        return {"data": [{"id": r[0], "created_at": r[1], **json.loads(r[2])} for r in rows]}


def install_gateway(router, endpoint, authenticate, decorate, *, admin):
    from .gateway import connection, rpc
    prefix = "" if admin else "/media"

    async def decorate_workflow(request, principal, value):
        for job in value.get("jobs", {}).values():
            await decorate(request, principal, job)
        return value

    async def upload(request):
        principal = await authenticate(request)
        if "multipart/form-data" in request.headers.get("content-type", ""):
            async with request.form(max_files=1, max_fields=3, max_part_size=MAX_IMAGE_BYTES) as form:
                file = form.get("file")
                if not isinstance(file, UploadFile):
                    raise MediaError("invalid_asset", "请选择图片文件。")
                data = await file.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise MediaError("media_too_large", "单张图片最大10MiB。", 413)
                value = {"data": base64.b64encode(data).decode(), "content_type": file.content_type, "name": file.filename, "role": form.get("role", "reference")}
        else:
            value = await request.json()
        async with connection(request, principal) as client:
            return JSONResponse(await rpc(client, "POST", "/creative/assets", json=value), status_code=201)
    router.add_api_route(prefix + "/assets", endpoint(upload), methods=["POST"])

    async def workflow(request):
        principal = await authenticate(request)
        identifier = request.path_params.get("workflow_id")
        path = "/creative/workflows" + ("/" + identifier if identifier else "")
        if request.method == "POST":
            if getattr(request.app.state.runtime, "draining", False):
                raise MediaError("router_draining", "服务正在切换，请用原操作标识重试。", 503)
            if not request.headers.get("idempotency-key"):
                raise MediaError("idempotency_required", "操作需要Idempotency-Key。")
            if await request.app.state.runtime.store.increment_window(f"router:media-submit:{principal['owner']}", 1, 60) > principal["rpm_limit"]:
                raise MediaError("media_rate_limit", "提交过于频繁。", 429)
            body = await request.json()
            if identifier:
                path += "/actions"
                if request.url.path.endswith("/messages"):
                    body = {**body, "action": "message"}
        async with connection(request, principal) as client:
            value = await rpc(client, request.method, path, **({"json": body} if request.method == "POST" else {}))
            if "data" in value:
                value["data"] = [await decorate_workflow(request, principal, w) for w in value["data"]]
            else:
                value = await decorate_workflow(request, principal, value)
        return JSONResponse(value, status_code=202 if request.method == "POST" else 200)
    router.add_api_route(prefix + "/workflows", endpoint(workflow), methods=["GET", "POST"])
    router.add_api_route(prefix + "/workflows/{workflow_id}", endpoint(workflow), methods=["GET"])
    for suffix in ("actions", "messages"):
        router.add_api_route(prefix + "/workflows/{workflow_id}/" + suffix, endpoint(workflow), methods=["POST"])

    async def events(request):
        principal = await authenticate(request)
        identifier = request.path_params["workflow_id"]
        try:
            after = int(request.headers.get("last-event-id") or request.query_params.get("after", "0"))
        except ValueError:
            raise MediaError("invalid_event_cursor", "事件游标无效。")
        async with connection(request, principal) as client:
            await rpc(client, "GET", "/creative/workflows/" + identifier)
        async def stream():
            nonlocal after
            # Bounded stream; the client reconnects with Last-Event-ID. Reads
            # never advance a workflow or create a generation operation.
            for _ in range(25):
                if await request.is_disconnected():
                    break
                await authenticate(request)
                async with connection(request, principal) as client:
                    value = await rpc(client, "GET", f"/creative/workflows/{identifier}/events", params={"after": after})
                for item in value["data"]:
                    after = item["id"]
                    yield f"id: {after}\nevent: workflow\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    router.add_api_route(prefix + "/workflows/{workflow_id}/events", endpoint(events), methods=["GET"])
