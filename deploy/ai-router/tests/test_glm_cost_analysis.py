"""Waste classification requires conserved raw content and compatible history."""
import copy
import json
from types import SimpleNamespace

import pytest

from ai_router.cost_analysis import analyze
from ai_router.route_trace import RouteTraceStore
from ai_router.costs import CostLedger
from test_glm_costs import trace


def archive(raw,forwarded):
    return {"pipeline":{"bodies":{"raw":raw,"sent":forwarded},"stages":[
        {"stage":"after_directives","sha256":"raw"},{"stage":"forwarded_1","sha256":"sent"}]}}


@pytest.mark.parametrize("condition",["confirmed","registry_changed","history_changed","archive_missing","normal_growth"])
def test_attribution_and_structural_sizes_do_not_store_content(tmp_path,condition):
    path=tmp_path/"traces.sqlite3";store=RouteTraceStore(path);ledger=CostLedger(path)
    prior=trace("prior");current=trace("current")
    if condition=="registry_changed":current["registry_fingerprint"]="changed"
    for item in [prior,current]:store._save(item)
    raw={"messages":[{"role":"system","content":"PRIVATE_SYSTEM"},{"role":"user","content":"PRIVATE_USER"}],"tools":[]}
    fresh=copy.deepcopy(raw);fresh["messages"].append({"role":"user","content":"PRIVATE_NEW"})
    if condition=="history_changed":fresh["messages"][1]["content"]="PRIVATE_EDIT"
    sent=copy.deepcopy(fresh)
    if condition!="normal_growth":sent["messages"][0]["content"]="PRIVATE_MODIFIED_SYSTEM"
    archives={"prior":archive(raw,raw),"current":archive(fresh,sent)}
    reader=SimpleNamespace(read=lambda identifier:None if condition=="archive_missing" else archives.get(identifier))
    with ledger.connect() as db:
        db.execute("INSERT INTO prefix_breaks VALUES (?,?,?,?)",("current",1,current["started_at"],json.dumps({"previous_request_id":"prior"})))
    result=analyze(ledger,reader,"current")
    if condition=="confirmed":
        assert result["findings"][0]["state"]=="confirmed"
        assert result["findings"][0]["saving_upper_bound_cny"]=="0.000071250"
        assert result["system_chars"]>0 and result["historical_chars"]>0 and result["new_message_chars"]>0
    else:assert all(f["state"]!="confirmed" for f in result["findings"])
    assert "PRIVATE_" not in json.dumps(result)
    with ledger.connect() as db:
        assert all("PRIVATE_" not in r[0] for r in db.execute("SELECT payload_json FROM cost_analyses"))


def test_final_attempt_analysis_is_not_attached_to_earlier_retry(tmp_path):
    path=tmp_path/"traces.sqlite3";store=RouteTraceStore(path);ledger=CostLedger(path)
    item=trace();later=copy.deepcopy(item["attempts"][0]);later["number"]=2;item["attempts"].append(later);store._save(item)
    with ledger.connect() as db:
        db.execute("INSERT INTO cost_analyses VALUES (?,?)",(item["request_id"],json.dumps({"version":1,"attempt":2,"findings":[{"state":"confirmed","code":"router_prefix_changed"}]})))
    rows=ledger.requests(since=0,until=2**40)["items"]
    assert all(f["state"]!="confirmed" for f in next(r for r in rows if r["attempt"]==1)["findings"])
    assert any(f["state"]=="confirmed" for f in next(r for r in rows if r["attempt"]==2)["findings"])
