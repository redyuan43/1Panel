"""Accounting contracts: no inference, reservations, or live state."""
import copy
from datetime import datetime
from decimal import Decimal
import io
import json
import sqlite3
from types import SimpleNamespace
import zipfile
from xml.sax.saxutils import escape

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from ai_router.costs import CostLedger, attempt_records, consume_trace, money, price_at
from ai_router.cost_bill import parse_xlsx
from ai_router.cost_control import install_cost_routes
from ai_router.errors import RouterError
from ai_router.route_trace import DecisionTrace, RouteTraceStore


def stamp(value="2026-09-09T12:00:00+08:00"):
    return datetime.fromisoformat(value).timestamp()


def trace(request_id="cost-test", *, timestamp=None, status="succeeded", output=10, model="zhipu/glm-5.3-flash"):
    now = timestamp or stamp()
    item = DecisionTrace(request_id=request_id, instance_id="test", boot_id="test", client_id="test-client",
                         protocol="chat", requested_model=model, key_id="test", excerpt={}, settings_hash="test", registry_hash="test").payload
    item.update(started_at=now, updated_at=now+1, completed_at=now+1 if status != "running" else None,
                status=status, selected_model=model, conversation_id="test-conversation")
    evidence = {"selected_model":model, "backend_usage":{"state":"complete", "input_tokens":1000, "cached_tokens":750},
                "output_tokens":output, "output_tokens_measured":True}
    item["attempts"] = [{"number":1, "started_at":now, "steps":[{"node_id":"upstream_request", "timestamp":now,
                            "status":"passed" if status == "succeeded" else "running" if status == "running" else "error", "evidence":evidence}]}]
    return item


@pytest.fixture
def ledger(tmp_path):
    path=tmp_path/"traces.sqlite3"
    RouteTraceStore(path)
    return CostLedger(path)


def save(ledger, item):
    with ledger.connect() as db:
        consume_trace(db,item)


def test_prices_switch_at_beijing_midnight_and_no_unpriced_history():
    before=next(attempt_records(trace(timestamp=stamp("2026-09-09T23:59:59+08:00"))))
    after=next(attempt_records(trace(timestamp=stamp("2026-09-10T00:00:00+08:00"))))
    assert money(before["total_nano"]) == "0.000200250"
    assert after["total_nano"] == before["total_nano"]*2
    assert after["price_version"] != before["price_version"]
    assert price_at("glm-5.3-flash", stamp("2026-09-01T23:59:59+08:00")) is None
    assert next(attempt_records(trace(model="zhipu/glm-5.3")))["total_nano"] == 3780000


@pytest.mark.parametrize("status", ["failed","interrupted","running"])
def test_failure_cancel_and_running_without_usage_stay_unknown(ledger,status):
    item=trace(status=status)
    item["attempts"][0]["steps"][0]["evidence"]={"selected_model":item["selected_model"],"input_tokens":999999,"output_reserve_tokens":65536}
    save(ledger,item)
    result=ledger.requests(since=0,until=2**40)["items"][0]
    assert result["measurement"] == ("pending" if status=="running" else "unknown")
    assert result["total_cny"] is None and result["input_tokens"] is None


@pytest.mark.parametrize("change", ["missing_cache","too_many_cached","missing_output","legacy_zero","invalid_output","overflow"])
def test_incomplete_or_invalid_usage_never_becomes_zero_cost(change):
    item=trace(output=0)
    ev=item["attempts"][0]["steps"][0]["evidence"]
    if change=="missing_cache": ev["backend_usage"].pop("cached_tokens")
    if change=="too_many_cached": ev["backend_usage"]["cached_tokens"]=1001
    if change=="missing_output": ev["output_tokens_measured"]=False
    if change=="legacy_zero": ev.pop("output_tokens_measured")
    if change=="invalid_output": ev["output_tokens"]=True
    if change=="overflow": ev["output_tokens"]=2**53-1
    row=next(attempt_records(item))
    assert row["measurement"]=="unknown" and row.get("total_nano") is None


def test_explicit_zero_output_and_legacy_positive_are_measured():
    item=trace(output=0)
    assert next(attempt_records(item))["measurement"]=="measured"
    item=trace()
    item["attempts"][0]["steps"][0]["evidence"].pop("output_tokens_measured")
    assert next(attempt_records(item))["measurement"]=="measured"


def test_retries_and_duplicate_completion_notifications_do_not_double_charge(ledger):
    item=trace()
    first=copy.deepcopy(item["attempts"][0]); first["steps"][0]["evidence"]={"selected_model":item["selected_model"]}; first["steps"][0]["status"]="error"
    item["attempts"][0]["number"]=2
    item["attempts"].insert(0,first)
    save(ledger,item)
    save(CostLedger(ledger.path),copy.deepcopy(item))
    stale=copy.deepcopy(item); stale["status"]="running"; stale["updated_at"]-=1
    stale["attempts"][-1]["steps"][0]["status"]="running"
    save(ledger,stale)
    summary=ledger.summary(since=0,until=2**40)
    assert (summary["requests"],summary["attempts"],summary["measured_attempts"],summary["unknown_attempts"])==(1,2,1,1)
    assert summary["total_cny"]=="0.000200250" and summary["coverage"]==.5
    assert summary["budget_enforcement"] is False
    assert all(x["findings"][0]["code"]=="multiple_attempts" for x in ledger.requests(since=0,until=2**40)["items"])


def test_complete_error_usage_is_accounted_and_rejected_before_dispatch_is_absent():
    assert next(attempt_records(trace(status="failed")))["measurement"]=="measured"
    item=trace(); item["attempts"][0]["steps"]=[]
    assert list(attempt_records(item))==[]


def test_trace_store_records_live_pending_and_final_usage_and_backfills(ledger):
    store=RouteTraceStore(ledger.path)
    item=trace(status="running")
    store._save(item)
    assert ledger.requests(since=0,until=2**40)["items"][0]["measurement"]=="pending"
    finished=trace(); finished["updated_at"]+=1
    store._save(finished)
    assert ledger.summary(since=0,until=2**40)["total_cny"]=="0.000200250"
    with ledger.connect() as db:
        db.execute("DELETE FROM cost_trace_state")
        db.execute("DELETE FROM cost_attempts")
    assert ledger.backfill()==1 and ledger.backfill()==0
    assert ledger.summary(since=0,until=2**40)["total_cny"]=="0.000200250"


def test_cost_write_failure_keeps_audit_and_backfill_can_repair(ledger,monkeypatch):
    from ai_router import route_trace
    def broken(*args): raise sqlite3.OperationalError("synthetic failure")
    monkeypatch.setattr(route_trace,"record_cost_trace",broken)
    store=RouteTraceStore(ledger.path); store._save(trace())
    assert store._get("cost-test")["status"]=="succeeded"
    assert ledger.backfill()==1


def xlsx(rows, *, extra_header="API Key", extra_value="synthetic-secret-never-persist"):
    headers=["账单号","账期(自然日)","模型编码（推理专用）","付费类型","单价","单价单位","用量","用量单位","币种","总消费金额（结算金额加总）","请求次数 (仅API)","价格类型",extra_header]
    contents=[headers]+[r+[extra_value] for r in rows]
    xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for index,row in enumerate(contents,1):
        xml+=f'<row r="{index}">'+''.join(f'<c r="{chr(65+i)}{index}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>' for i,v in enumerate(row))+'</row>'
    xml+='</sheetData></worksheet>'
    result=io.BytesIO()
    with zipfile.ZipFile(result,"w",zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml",xml)
    return result.getvalue()


def bill_rows():
    return [["bill-input","2026-09-09","glm-5.3-flash","后付费","0.0004","千token",250,"token","CNY","0.000100000",1,"输入"],
            ["bill-cache","2026-09-09","glm-5.3-flash","后付费","0.000115","千token",750,"token","CNY","0.000086250",1,"缓存命中"],
            ["bill-output","2026-09-09","glm-5.3-flash","后付费","0.0014","千token",10,"token","CNY","0.000014000",1,"输出"],
            ["bill-rebate","2026-09-09","glm-5.3-flash","后付费(减免)",0,"千token",0,"token","CNY","-0.000010000","","输入"]]


def test_bill_import_idempotency_privacy_negative_adjustment_and_day_scope(ledger):
    rows=parse_xlsx(xlsx(bill_rows()))
    assert ledger.import_bill(rows)=={"inserted":4,"duplicates":0,"rows":4}
    assert ledger.import_bill(rows)=={"inserted":0,"duplicates":4,"rows":4}
    save(ledger,trace())
    result=ledger.reconciliation(since=stamp("2026-09-09T00:00:00+08:00"),until=stamp("2026-09-10T00:00:00+08:00"))["items"][0]
    assert result["official_gross_cny"]==result["router"]["total_cny"]=="0.000200250"
    assert result["official_net_cny"]=="0.000190250" and result["difference_cny"]=="-0.000010000"
    assert result["scope"]=="day_model" and result["comparison_complete"]
    assert "synthetic-secret" not in Path(ledger.path).read_bytes().decode(errors="ignore")
    changed=copy.deepcopy(rows); changed[0]["tokens"]+=1
    with pytest.raises(ValueError,match="同一账单标识"): ledger.import_bill(changed)


from pathlib import Path


def test_parallel_instances_import_exactly_once(ledger):
    from concurrent.futures import ThreadPoolExecutor
    rows=parse_xlsx(xlsx(bill_rows()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:CostLedger(ledger.path).import_bill(rows),range(2)))
    assert sorted(r["inserted"] for r in results)==[0,4]
    assert sorted(r["duplicates"] for r in results)==[0,4]


def test_import_conflict_rolls_back_earlier_new_rows(ledger):
    rows=parse_xlsx(xlsx(bill_rows()))
    ledger.import_bill(rows)
    new=copy.deepcopy(rows[0]); new["bill_id"]="new-bill"
    conflict=copy.deepcopy(rows[1]); conflict["tokens"]+=1
    with pytest.raises(ValueError):ledger.import_bill([new,conflict])
    with ledger.connect() as db:
        assert db.execute("SELECT count(*) FROM cost_bill_items").fetchone()[0]==4


def test_duplicate_payload_is_review_only_and_client_scoped(ledger):
    a=trace("one");a["observation"]={"content":{"stages":[{"stage":"effective","sha256":"synthetic-hash"}]}}
    b=copy.deepcopy(a);b["request_id"]="two";b["started_at"]+=20
    c=copy.deepcopy(a);c["request_id"]="other-client";c["client_id"]="other"
    for row in [a,b,c]:save(ledger,row)
    rows=ledger.requests(since=0,until=2**40)["items"]
    assert all(not f["state"]=="confirmed" for r in rows for f in r["findings"])
    assert next(r for r in rows if r["request_id"]=="one")["findings"][0]["request_ids"]==["two"]
    assert next(r for r in rows if r["request_id"]=="other-client")["findings"]==[]


@pytest.mark.parametrize("column,value",[(9,"NaN"),(9,"1"),(6,"-1"),(8,"USD"),(5,"百万token")])
def test_bill_rejects_bad_values(column,value):
    rows=bill_rows(); rows[0][column]=value
    with pytest.raises(ValueError): parse_xlsx(xlsx(rows))


def test_admin_auth_filters_pagination_import_and_details(ledger):
    save(ledger,trace())
    item=trace("second"); item["client_id"]="other"; item["conversation_id"]="different"; save(ledger,item)
    app=FastAPI()
    runtime=SimpleNamespace(route_traces=SimpleNamespace(database_path=ledger.path),audit=SimpleNamespace(write=lambda *a,**kw:None))
    def authorize(request):
        if request.headers.get("authorization")!="Bearer test":
            raise RouterError("Unauthorized",status_code=401,code="unauthorized")
        return runtime
    @app.exception_handler(RouterError)
    async def error(request,exc): return JSONResponse({"error":{"message":str(exc)}},status_code=exc.status_code)
    install_cost_routes(app,authorize)
    with TestClient(app) as client:
        for path in ("summary","requests","reconciliation","requests/cost-test"):
            assert client.get("/api/costs/"+path).status_code==401
        assert client.post("/api/costs/import",content=xlsx(bill_rows())).status_code==401
        client.headers["authorization"]="Bearer test"
        params={"since":stamp("2026-09-09T00:00:00+08:00"),"until":stamp("2026-09-10T00:00:00+08:00"),"limit":1}
        page=client.get("/api/costs/requests",params=params).json()
        assert page["total"]==2 and page["next_offset"]==1
        assert len(client.get("/api/costs/requests",params={**params,"offset":1}).json()["items"])==1
        assert client.get("/api/costs/requests",params={**params,"client_id":"other","conversation_id":"different"}).json()["total"]==1
        assert client.get("/api/costs/requests",params={**params,"model":"glm-5.3"}).json()["total"]==0
        assert client.get("/api/costs/summary?since=nan").status_code==400
        assert client.get("/api/costs/requests?limit=1000").status_code==422
        assert client.get("/api/costs/requests/cost-test").json()["analysis"]["state"]=="unavailable"
        imported=client.post("/api/costs/import",content=xlsx(bill_rows()),headers={"Content-Type":"application/octet-stream"})
        assert imported.status_code==200 and imported.json()["inserted"]==4
