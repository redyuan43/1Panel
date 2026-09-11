from __future__ import annotations

import json
import re

import httpx
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.background import BackgroundTask

from .h3_mcp_management import permits


MAX_ASSET_BYTES = 128 * 1024 * 1024
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def routes(gateway, configuration, enabled):
    async def relay(request):
        runtime = gateway.host_app.state.runtime
        writing = request.method == "POST"
        if writing and (not enabled("AI_ROUTER_H3_MCP_WRITES_ENABLED") or getattr(runtime, "draining", False)
                        or not await permits(runtime, "writes")):
            return JSONResponse({"detail": "asset uploads paused"}, status_code=503, headers=NO_STORE)
        metadata = request.headers.get("x-h3-upload-metadata", "")
        if writing:
            try:
                value = json.loads(metadata)
                length = value["size"]
                if type(length) is not int or not 0 < length <= MAX_ASSET_BYTES or len(metadata) > 4096:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                return JSONResponse({"detail": "invalid upload declaration"}, status_code=400, headers=NO_STORE)
            suffix = "assets/uploads"
        elif "operation_id" in request.path_params:
            value = request.path_params["operation_id"]
            if not SAFE.fullmatch(value):
                return JSONResponse({"detail": "invalid upload operation"}, status_code=400, headers=NO_STORE)
            suffix = "assets/uploads/" + value
        else:
            value = request.path_params["asset_id"]
            if not re.fullmatch(r"asset_[a-f0-9]{32}", value):
                return JSONResponse({"detail": "invalid asset"}, status_code=400, headers=NO_STORE)
            suffix = "assets/" + value + "/content"

        async def bounded_body():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > length:
                    raise ValueError("upload exceeds declaration")
                yield chunk

        base, secret = configuration()
        headers = {"Authorization": "Bearer " + secret, "X-H3-Connector-Owner": request.state.h3_owner}
        if writing:
            headers["X-H3-Upload-Metadata"] = metadata
            headers["Content-Type"] = "application/octet-stream"
        elif request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(150, connect=5), trust_env=False,
                                   follow_redirects=False, transport=getattr(gateway.host_app.state, "h3_mcp_transport", None))
        try:
            response = await client.send(client.build_request(request.method, "/api/router/connector/" + suffix,
                                         headers=headers, content=bounded_body() if writing else None), stream=True)
            if response.status_code >= 300:
                await response.aclose()
                await client.aclose()
                return JSONResponse({"detail": "asset request rejected; reconcile the original upload operation"},
                                    status_code=response.status_code, headers=NO_STORE)

            async def close():
                await response.aclose()
                await client.aclose()

            async def chunks():
                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                finally:
                    await close()

            forwarded = {key: response.headers[key] for key in ("content-type", "content-length", "content-range", "accept-ranges") if key in response.headers}
            return StreamingResponse(chunks(), status_code=response.status_code, headers={**forwarded, **NO_STORE}, background=BackgroundTask(close))
        except (httpx.HTTPError, ValueError):
            await client.aclose()
            return JSONResponse({"detail": "upload outcome unknown; query original operation before retry"}, status_code=503, headers=NO_STORE)

    return [Route("/h3/assets/uploads", relay, methods=["POST"]),
            Route("/h3/assets/uploads/{operation_id}", relay, methods=["GET"]),
            Route("/h3/assets/{asset_id}/content", relay, methods=["GET", "HEAD"])]
