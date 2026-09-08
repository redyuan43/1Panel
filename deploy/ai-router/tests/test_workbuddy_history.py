"""CPU contracts for immutable WorkBuddy history overlays."""
from __future__ import annotations
import copy
import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
import zlib
from pathlib import Path
from cryptography.fernet import Fernet
import pytest
from ai_router.workbuddy_history import prepare, reconcile, WorkBuddyHistory

def req(dynamic="A", user="first", model="siyuan/auto", client="workbuddy-public"):
    return {"model": model, "messages":[{"role":"system","content":f"stable\n<workbuddy_dynamic_context>\n{dynamic}\n</workbuddy_dynamic_context>\n"},{"role":"user","content":user}],"tools":[{"type":"function","function":{"name":"Read","parameters":{"type":"object"}}}]}

def test_prepare_returns_bare_body_and_dynamic_block():
    raw=req()
    move,bare,block=prepare(raw,"workbuddy-public")
    assert move.moved and block == "<workbuddy_dynamic_context>\nA\n</workbuddy_dynamic_context>"
    assert bare["messages"][1]["content"] == "first"
    assert raw["messages"][0]["content"].startswith("stable")

def test_new_user_turn_does_not_relocate_old_dynamic_block():
    old=req("old","first")
    fresh=req("new","first")
    fresh["messages"].append({"role":"user","content":"second"})
    value, report = reconcile(fresh,"workbuddy-public",old,prepare(old,"workbuddy-public")[0].body)
    assert report["dynamic_update_appended"]
    assert value["messages"][1]["content"].startswith("<workbuddy_dynamic_context>\nold")
    assert value["messages"][2]["content"].startswith("<workbuddy_dynamic_context>\nnew")

def test_same_dynamic_context_is_not_duplicated():
    old=req("same","first")
    fresh=copy.deepcopy(old)
    value, report = reconcile(fresh,"workbuddy-public",old,prepare(old,"workbuddy-public")[0].body)
    assert not report["dynamic_update_appended"]
    assert value["messages"] == prepare(old,"workbuddy-public")[0].body["messages"]

def test_tool_continuation_gets_synthetic_user_without_changing_tool_history():
    old=req("old","question")
    fresh=req("new","question")
    call={"id":"call-1","type":"function","function":{"name":"Read","arguments":"{\"path\":\"a\"}"}}
    old["messages"].append({"role":"assistant","content":None,"tool_calls":[call]})
    old["messages"].append({"role":"tool","tool_call_id":"call-1","content":"result"})
    fresh["messages"]=copy.deepcopy(old["messages"])
    fresh["messages"][0]["content"]=fresh["messages"][0]["content"].replace("old","new")
    value, report = reconcile(fresh,"workbuddy-public",old,prepare(old,"workbuddy-public")[0].body)
    assert report["dynamic_update_appended"]
    assert value["messages"][-1]["role"] == "user"
    assert value["messages"][-2]["tool_call_id"] == "call-1"
    assert value["messages"][-2]["content"] == "result"
    assert value["messages"][-3]["tool_calls"][0]["id"] == "call-1"

@pytest.mark.parametrize("mutate", ["history", "system", "positions"])
def test_modified_raw_history_or_stable_system_does_not_match(mutate):
    old=req("old","first")
    fresh=copy.deepcopy(old)
    if mutate == "history": fresh["messages"][1]["content"] = "tampered"
    elif mutate == "system": fresh["messages"][0]["content"] = fresh["messages"][0]["content"].replace("stable","changed")
    else: fresh["messages"].insert(1,{"role":"assistant","content":"extra"})
    assert reconcile(fresh,"workbuddy-public",old,prepare(old,"workbuddy-public")[0].body) is None

def test_tools_schema_choice_and_ui_metadata_are_preserved():
    old=req("old","first")
    old.update({"tool_choice":{"type":"function","function":{"name":"Read"}},"ui":{"expanded":True}})
    fresh=copy.deepcopy(old)
    fresh["ui"]["expanded"] = False
    value, _ = reconcile(fresh,"workbuddy-public",old,prepare(old,"workbuddy-public")[0].body)
    assert value["tools"] == old["tools"]
    assert value["tool_choice"] == old["tool_choice"]
    assert value["ui"] == fresh["ui"]

def test_scope_model_and_reset_are_isolated():
    old=req("old")
    assert prepare(old,"other-client") is None
    assert prepare(req(model="other/model"),"workbuddy-public") is not None
    history=WorkBuddyHistory(":memory:")
    value, report = history.restore(old,"workbuddy-public",reset=True)
    assert value is None and report["reason"] == "reset"

def test_corrupt_or_missing_archive_fails_open_to_original_input(monkeypatch,tmp_path):
    history=WorkBuddyHistory(tmp_path/"index.sqlite3",archive_path="missing",key_path="missing-key")
    raw=req()
    value, report = __import__("asyncio").run(history.apply(raw,"workbuddy-public"))
    assert value == raw
    assert report["status"] == "failed"
    assert report["reorder_bypassed"] is True

def test_restore_reads_real_encrypted_archive_and_index(tmp_path):
    raw = req("old", "first")
    old_move = prepare(raw, "workbuddy-public")[0]
    old_normalized = old_move.body
    fresh = req("old", "first")
    archive_db, key_path, index_db = tmp_path / "archive.sqlite3", tmp_path / "training.key", tmp_path / "index.sqlite3"
    key = Fernet.generate_key(); key_path.write_bytes(key)
    payload = {"pipeline": {"stages": [
        {"stage": "after_directives", "sha256": "raw", "archived": True},
        {"stage": "workbuddy_history_preserved", "sha256": "normalized", "archived": True}],
        "bodies": {"raw": raw, "normalized": old_normalized}, "checks": []}}
    index_key = hmac.new(base64.urlsafe_b64decode(key), b"1panel-ai-router-training-index-v1", hashlib.sha256).digest()
    record_key = hmac.new(index_key, b"request:old-request", hashlib.sha256).hexdigest()
    encrypted = Fernet(key).encrypt(zlib.compress(json.dumps(payload).encode()))
    with sqlite3.connect(archive_db) as db:
        db.execute("create table training_records(request_hash text primary key, payload_ciphertext blob)")
        db.execute("insert into training_records values (?,?)", (record_key, encrypted))
    from ai_router.workbuddy_history import chain
    with sqlite3.connect(index_db) as db:
        db.execute("create table workbuddy_history(request_id text, scope text, prefix_hash text, message_count integer, created_at real, version integer)")
        db.execute("insert into workbuddy_history values (?,?,?,?,?,?)", ("old-request", WorkBuddyHistory.scope("workbuddy-public", fresh["model"]), chain(prepare(raw,"workbuddy-public")[1])[-1], len(old_normalized["messages"]), 9999999999, 1))
    history = WorkBuddyHistory(index_db, archive_path=archive_db, key_path=key_path)
    value, report = history.restore(fresh, "workbuddy-public")
    assert value is not None and report["previous_request_id"] == "old-request"
    assert report["association"] == "exact_raw_prefix"

def test_tool_call_id_or_name_change_rejects_exact_history_match():
    old = req("old", "question")
    call = {"id": "call-1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
    old["messages"] += [{"role": "assistant", "content": None, "tool_calls": [call]}, {"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    for mutation in ("id", "name"):
        fresh = copy.deepcopy(old)
        if mutation == "id":
            fresh["messages"][2]["tool_calls"][0]["id"] = "call-2"
            fresh["messages"][3]["tool_call_id"] = "call-2"
        else:
            fresh["messages"][2]["tool_calls"][0]["function"]["name"] = "Write"
        assert reconcile(fresh, "workbuddy-public", old, prepare(old, "workbuddy-public")[1]) is None

def test_synthetic_update_then_next_user_keeps_prior_prefix_position():
    old = req("old", "first")
    continuation = copy.deepcopy(old)
    continuation["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "call-1", "content": "result"}]
    continuation["messages"][0]["content"] = continuation["messages"][0]["content"].replace("old", "new")
    first, report = reconcile(continuation, "workbuddy-public", old, prepare(old, "workbuddy-public")[0].body)
    assert report["dynamic_update_appended"]
    followup = copy.deepcopy(continuation)
    followup["messages"].append({"role": "user", "content": "next"})
    followup["messages"][0]["content"] = followup["messages"][0]["content"].replace("new", "latest")
    second, second_report = reconcile(followup, "workbuddy-public", continuation, first, report["positions"])
    assert second_report["history_preserved"]
    assert second_report["dynamic_update_appended"]
    assert second["messages"][0]["content"] == first["messages"][0]["content"]
    assert second["messages"][-1]["content"].startswith("<workbuddy_dynamic_context>")


def test_raw_lineage_aliases_are_separate_from_transformed_history():
    import asyncio
    from types import SimpleNamespace
    from starlette.requests import Request
    from ai_router.api import _lineage_context
    calls=[]
    class Conversations:
        async def lineage_context(self, **kwargs):
            calls.append(kwargs)
            return kwargs
    raw=req()
    raw["messages"].append({"role":"assistant","content":"answer"})
    request=Request({"type":"http","headers":[]})
    current=SimpleNamespace(conversations=Conversations())
    asyncio.run(_lineage_context(current,request,raw,"chat","workbuddy-public",raw_identity_namespace=True))
    assert calls[0]["identities"]
    assert all(x.startswith("wb-raw-v1:") for x in calls[0]["identities"])
    asyncio.run(_lineage_context(current,request,raw,"chat","other-client"))
    assert not any(x.startswith("wb-raw-v1:") for x in calls[1]["identities"])
