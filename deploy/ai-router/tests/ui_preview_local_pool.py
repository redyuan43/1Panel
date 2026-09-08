"""Isolated local scheduling UI fixture; mock transport forbids inference."""
import asyncio, json
from pathlib import Path
from ui_preview import prepare
from ai_router.control import create_app
import uvicorn

async def main():
    runtime = await prepare(Path("/tmp/local-pool-preview"))
    trace = await runtime.route_traces.get("preview-local")
    trace["local_pool"] = {"group":"local-peers", "selection":"spread_new_conversations", "candidates":[
        {"endpoint_id":"ai-qwen38-27b","available":True,"running":0,"capacity":4,"recent_conversations":0},
        {"endpoint_id":"edge-qwen38-flash","available":True,"running":0,"capacity":1,"recent_conversations":1},
        {"endpoint_id":"amd-qwen38-rocmfpx-128k","available":False,"running":1,"capacity":1,"recent_conversations":1}],
        "costs":[{"endpoint_id":"ai-qwen38-27b","cache_assumption":"cold","sample_count":5,"queue_s":0,"total_s":30},
        {"endpoint_id":"edge-qwen38-flash","cache_assumption":"hot","sample_count":2,"queue_s":None,"total_s":None,"unavailable_reason":"insufficient_matching_samples"}]}
    await asyncio.to_thread(runtime.route_traces._save, trace)
    app = create_app(runtime)
    server=uvicorn.Server(uvicorn.Config(app,host="0.0.0.0",port=14809,log_level="warning",access_log=False))
    try: await server.serve()
    finally: await runtime.close()

if __name__ == "__main__": asyncio.run(main())
