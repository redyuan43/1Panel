"""Isolated GLM cost UI fixture; mock transport rejects model calls."""
import argparse
import asyncio
from pathlib import Path
import time

import uvicorn
from fastapi.responses import JSONResponse

from ui_preview import prepare
from test_glm_costs import trace, xlsx, bill_rows
from ai_router.control import create_app


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--state-dir",type=Path,required=True)
    parser.add_argument("--port",type=int,default=14819)
    args=parser.parse_args()
    runtime=await prepare(args.state_dir.resolve())
    for i in range(38):
        item=trace(f"cost-preview-{i:03d}")
        item["conversation_id"]="cost-conversation-a" if i<35 else "cost-conversation-b"
        item["client_id"]="cost-client-a" if i<35 else "cost-client-b"
        if i>=35:
            item["selected_model"]="zhipu/glm-5.3"
            item["attempts"][0]["steps"][0]["evidence"]["selected_model"]=item["selected_model"]
        await asyncio.to_thread(runtime.route_traces._save,item)
    pending=trace("cost-preview-pending",status="running",timestamp=time.time())
    pending["attempts"][0]["steps"][0]["evidence"]={"selected_model":pending["selected_model"]}
    await asyncio.to_thread(runtime.route_traces._save,pending)
    unknown=trace("cost-preview-unknown",status="interrupted",timestamp=time.time())
    unknown["attempts"][0]["steps"][0]["evidence"]={"selected_model":unknown["selected_model"]}
    await asyncio.to_thread(runtime.route_traces._save,unknown)
    (args.state_dir/"synthetic-bill.xlsx").write_bytes(xlsx(bill_rows()))
    app=create_app(runtime)

    @app.middleware("http")
    async def isolate(request,call_next):
        if request.method not in {"GET","HEAD","OPTIONS"} and request.url.path not in {"/api/costs/import","/__complete__"}:
            return JSONResponse({"error":{"message":"Preview action disabled"}},status_code=403)
        return await call_next(request)

    @app.get("/__ui_preview__")
    async def marker(): return {"isolated":True,"model_calls":"disabled"}

    @app.post("/__complete__")
    async def complete():
        item=trace("cost-preview-pending",timestamp=pending["started_at"])
        item["updated_at"]=time.time()+1
        await asyncio.to_thread(runtime.route_traces._save,item)
        return {"completed":True}

    try:
        await uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=args.port,log_level="warning",access_log=False)).serve()
    finally:
        await runtime.close()


if __name__=="__main__": asyncio.run(main())
