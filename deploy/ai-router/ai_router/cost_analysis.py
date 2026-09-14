"""On-demand structural cost evidence; persist counts and field names only."""
from collections import Counter
import json
from contextlib import closing

from .costs import money
from .prefix_break import stage_bodies


def size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(",",":")))


def analyze(ledger, reader, request_id):
    with closing(ledger.connect()) as db:
        cached=db.execute("SELECT payload_json FROM cost_analyses WHERE request_id=?",(request_id,)).fetchone()
        if cached and json.loads(cached[0]).get("version") == 1:
            return json.loads(cached[0])
        trace=db.execute("SELECT payload_json FROM route_traces WHERE request_id=?",(request_id,)).fetchone()
        prefix=db.execute("SELECT payload_json FROM prefix_breaks WHERE request_id=? ORDER BY attempt DESC LIMIT 1",(request_id,)).fetchone()
    result={"version":1,"request_id":request_id,"findings":[],"measurement":"characters","state":"unavailable"}
    if not trace:
        return result
    trace=json.loads(trace[0])
    archive=reader.read(request_id)
    if not archive:
        return result
    bodies=stage_bodies(archive)
    number=max((a.get("number",0) for a in trace.get("attempts",[])),default=1)
    forwarded=bodies.get("forwarded_"+str(number)) or bodies.get("effective")
    if not forwarded:
        return result
    roles=Counter()
    for message in forwarded.get("messages",[]):
        roles[str(message.get("role","unknown"))]+=size(message)
    result.update(state="available",attempt=number,total_chars=size(forwarded),role_chars=dict(roles),tools_chars=size(forwarded.get("tools",[])),
                  top_tools=sorted([{"name":t.get("function",{}).get("name",""),"chars":size(t)} for t in forwarded.get("tools",[])],key=lambda x:x["chars"],reverse=True)[:10])
    previous_id=json.loads(prefix[0]).get("previous_request_id") if prefix else None
    if previous_id:
        prior=reader.read(previous_id)
        old_bodies=stage_bodies(prior) if prior else {}
        old_raw,new_raw=old_bodies.get("after_directives"),bodies.get("after_directives")
        old_forwarded=next((old_bodies[k] for k in reversed(list(old_bodies)) if k.startswith("forwarded_")),None)
        if old_raw and new_raw and old_forwarded:
            raw_messages=old_raw.get("messages",[])
            stable=(bool(raw_messages) and raw_messages==new_raw.get("messages",[])[:len(raw_messages)]
                    and old_raw.get("tools")==new_raw.get("tools"))
            old_messages=old_forwarded.get("messages",[])
            fields=[]
            if old_forwarded.get("tools")!=forwarded.get("tools"):
                fields.append("tools")
            if old_messages!=forwarded.get("messages",[])[:len(old_messages)]:
                fields.append("historical_messages")
            with closing(ledger.connect()) as db:
                old_trace=db.execute("SELECT payload_json FROM route_traces WHERE request_id=?",(previous_id,)).fetchone()
            old_trace=json.loads(old_trace[0]) if old_trace else {}
            same_model=bool(old_trace and old_trace.get("selected_model")==trace.get("selected_model"))
            same_config=all(old_trace.get(k) and old_trace.get(k)==trace.get(k) for k in ("settings_fingerprint","registry_fingerprint"))
            result.update(previous_request_id=previous_id,raw_prefix_unchanged=stable,forwarded_changed_fields=fields,same_config=same_config,
                          system_chars=sum(size(m) for m in forwarded.get("messages",[]) if m.get("role") in {"system","developer"}),
                          historical_chars=sum(size(m) for m in forwarded.get("messages",[])[:len(old_messages)] if m.get("role") not in {"system","developer"}),
                          new_message_chars=sum(size(m) for m in forwarded.get("messages",[])[len(old_messages):] if m.get("role") not in {"system","developer"}))
            if stable and fields and same_model and same_config:
                rows=ledger.records(since=0,until=2**40,request_id=request_id)
                priced=next((r for r in rows if r["attempt"]==number and r["measurement"]=="measured"),None)
                upper=(priced["uncached_tokens"]*(priced["rates"]["input"]-priced["rates"]["cached"])) if priced else None
                result["findings"].append({"state":"confirmed","code":"router_prefix_changed","label":"已确认 Router 改变原有前缀",
                                           "previous_request_id":previous_id,"fields":fields,"saving_upper_bound_cny":money(upper),
                                           "note":"上限不是已实现节省；新增内容及上游缓存淘汰仍可能收费"})
            elif fields:
                result["findings"].append({"state":"review","code":"prefix_change_unconfirmed",
                    "label":"前缀变化归因待核对（配置或原始历史不同）", "previous_request_id":previous_id,"fields":fields})
    if trace.get("status") in {"succeeded","failed","interrupted"} and prefix:
        with closing(ledger.connect()) as db,db:
            db.execute("INSERT OR REPLACE INTO cost_analyses VALUES (?,?)",(request_id,json.dumps(result,ensure_ascii=False)))
    return result
