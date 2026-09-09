"""Isolated browser fixture. No real model calls or production state."""
import asyncio
from pathlib import Path
import uvicorn
from fastapi.responses import JSONResponse
from ui_preview import prepare
from ai_router.control import create_app

async def main():
    runtime = await prepare(Path("/tmp/fixed-routing-ui"))
    for rid, match in [
        ("preview-ai-hit", {"status":"verified", "source":"verified_history_v5", "reason":"unique_history_match", "semantic_items":7, "candidate_count":1, "inherited_endpoint_id":"ai-qwen38-27b"}),
        ("preview-ai-unknown", {"status":"unconfirmed", "source":"history", "reason":"shared_opening_only"})]:
        trace = await runtime.route_traces.get(rid)
        trace["history_match"] = match
        trace["local_pool"] = {"policy":"fixed_continuation_v1", "selection":"keep_verified_device" if match["status"]=="verified" else "spread_new_conversations"}
        await asyncio.to_thread(runtime.route_traces._save, trace)
    app = create_app(runtime)
    @app.middleware("http")
    async def no_actions(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"error":{"code":"preview_read_only"}},status_code=403)
        return await call_next(request)
    @app.get("/__ui_preview__")
    async def marker():
        return {"isolated":True,"model_calls":"disabled","fixture":"fixed-routing"}
    try:
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=14819, access_log=False, log_level="warning")).serve()
    finally:
        await runtime.close()

if __name__ == "__main__":
    asyncio.run(main())
