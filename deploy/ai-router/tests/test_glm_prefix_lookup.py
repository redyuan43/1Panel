"""Exact prefix selection must precede the candidate cap."""
import copy
import sqlite3
import time
from types import SimpleNamespace

import pytest

from ai_router.route_trace import RouteTraceStore
from ai_router.workbuddy_history import WorkBuddyHistory, VERSION, chain, prepare
from test_workbuddy_history import req


@pytest.mark.parametrize("messages", [2, 230])
@pytest.mark.parametrize("case", ["exact", "other_client", "other_model", "modified_history", "missing_archive", "forged_hash"])
def test_longer_unrelated_histories_cannot_hide_verified_prefix(tmp_path,monkeypatch,messages,case):
    path=tmp_path/"index.sqlite3"; RouteTraceStore(path)
    old=req("old")
    old["messages"] += [{"role":"user","content":str(i)} for i in range(messages-2)]
    normalized=prepare(old,"workbuddy-public")[0].body
    fresh=copy.deepcopy(old); fresh["messages"].append({"role":"user","content":"new turn"})
    history=WorkBuddyHistory(path)
    bare=prepare(old,"workbuddy-public")[1]; hashes=chain(bare)
    client="workbuddy-qwen36-shared" if case=="other_client" else "workbuddy-public"
    model="other/model" if case=="other_model" else old["model"]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO workbuddy_history VALUES (?,?,?,?,?,?)",("match",history.scope(client,model),hashes[-1],len(hashes),time.time(),VERSION))
        db.executemany("INSERT INTO workbuddy_history VALUES (?,?,?,?,?,?)",[(f"noise-{i}",history.scope("workbuddy-public",old["model"]),f"unrelated-{i}",messages+i+10,time.time(),VERSION) for i in range(360)])
    if case=="modified_history": fresh["messages"][1]["content"]="changed"
    if case=="forged_hash": old["messages"][1]["content"]="archive does not match index"
    payload={"pipeline":{"stages":[{"stage":"after_directives","sha256":"raw"},{"stage":"workbuddy_history_preserved","sha256":"normalized"}],"bodies":{"raw":old,"normalized":normalized},"checks":[]}}
    reads=[]
    def read(identifier):
        reads.append(identifier)
        return None if case=="missing_archive" else payload
    monkeypatch.setattr(history,"reader",lambda:SimpleNamespace(read=read))
    restored,report=history.restore(fresh,"workbuddy-public")
    if case=="exact":
        assert report["previous_request_id"]=="match"
        assert restored["messages"][:len(normalized["messages"])]==normalized["messages"]
        assert restored["tools"]==normalized["tools"]
    else:
        assert restored is None and report["association"]=="unconfirmed"
    assert len(reads)<=1 and all(i=="match" for i in reads)
