"""Admin-only cost views. No upstream requests or changes to routing policy."""
import asyncio
from datetime import datetime
import math
import os
import sqlite3

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from .content_audit import ArchiveReader
from .cost_analysis import analyze
from .cost_bill import MAX_UPLOAD, parse_xlsx
from .costs import BJT, CostLedger, MODELS
from .errors import RouterError


def install_cost_routes(app, authorized_runtime):
    def current(request):
        runtime=authorized_runtime(request)
        path=getattr(runtime.route_traces,"database_path",None)
        if not path:
            raise RouterError("费用账本暂不可用",status_code=503,code="cost_ledger_unavailable")
        return runtime,CostLedger(path)

    def filters(since,until,model=None,client_id=None,conversation_id=None,measurement=None):
        now=datetime.now(BJT)
        since=since if since is not None else now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()
        until=until if until is not None else now.timestamp()+1
        if not all(math.isfinite(x) and 0<=x<=253402214400 for x in [since,until]) or since>=until or until-since>367*86400:
            raise RouterError("时间范围须为一年以内的有效区间",status_code=400,code="invalid_cost_filter")
        if model and model not in MODELS or measurement and measurement not in {"pending","unknown","measured"}:
            raise RouterError("不支持的模型或用量状态",status_code=400,code="invalid_cost_filter")
        return dict(since=since,until=until,model=model,client_id=client_id,conversation_id=conversation_id,measurement=measurement)

    def response(value):
        return JSONResponse(value,headers={"Cache-Control":"no-store"})

    @app.get("/api/costs/summary")
    async def summary(request:Request,since:float|None=None,until:float|None=None,model:str|None=None,client_id:str|None=None,conversation_id:str|None=None):
        _,ledger=current(request)
        return response(await asyncio.to_thread(ledger.summary,**filters(since,until,model,client_id,conversation_id)))

    @app.get("/api/costs/requests")
    async def requests(request:Request,since:float|None=None,until:float|None=None,model:str|None=None,client_id:str|None=None,conversation_id:str|None=None,
                       measurement:str|None=None,request_id:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),sort:str="cost"):
        _,ledger=current(request)
        if sort not in {"cost","time"}:
            raise RouterError("排序参数不合法",status_code=400,code="invalid_cost_filter")
        return response(await asyncio.to_thread(ledger.requests,limit=limit,offset=offset,sort=sort,request_id=request_id,
                                               **filters(since,until,model,client_id,conversation_id,measurement)))

    @app.get("/api/costs/reconciliation")
    async def reconciliation(request:Request,since:float|None=None,until:float|None=None,model:str|None=None):
        _,ledger=current(request)
        value=filters(since,until,model)
        # Reconciliation always covers whole Beijing calendar days.
        start=datetime.fromtimestamp(value['since'],BJT).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        end=datetime.fromtimestamp(value['until']-0.001,BJT).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()+86400
        return response(await asyncio.to_thread(ledger.reconciliation,since=start,until=end,model=model))

    @app.post("/api/costs/import")
    async def import_bill(request:Request):
        runtime,ledger=current(request)
        if request.headers.get("content-type","").split(";")[0] not in {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","application/octet-stream"}:
            raise RouterError("请直接上传 XLSX 文件",status_code=415,code="invalid_cost_bill")
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>MAX_UPLOAD:
                raise RouterError("账单超过 5 MiB",status_code=413,code="cost_bill_too_large")
        try:
            rows=await asyncio.to_thread(parse_xlsx,bytes(data))
            result=await asyncio.to_thread(ledger.import_bill,rows)
        except ValueError as exc:
            raise RouterError(str(exc),status_code=400,code="invalid_cost_bill") from exc
        runtime.audit.write("cost_bill_imported",**result)
        return response(result)

    @app.get("/api/costs/requests/{request_id}")
    async def detail(request:Request,request_id:str):
        runtime,ledger=current(request)
        items=await asyncio.to_thread(ledger.requests,since=0,until=253402214400,request_id=request_id,sort="time")
        if not items["items"]:
            raise RouterError("未找到 GLM 上游计费尝试",status_code=404,code="cost_request_not_found")
        try:
            def inspect():
                reader=ArchiveReader(os.environ.get("AI_ROUTER_TRAINING_DB_PATH","/training/conversations.sqlite3"),os.environ.get("AI_ROUTER_TRAINING_KEY_PATH","/training/training.key"))
                return analyze(ledger,reader,request_id)
            analysis=await asyncio.to_thread(inspect)
        except (OSError,ValueError,KeyError,sqlite3.Error):
            analysis={"state":"unavailable","findings":[],"measurement":"characters"}
        runtime.audit.write("cost_request_inspected",request_id=request_id)
        return response({**items,"analysis":analysis})
