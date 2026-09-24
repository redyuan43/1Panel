"""CPU contracts for immutable WorkBuddy history overlays."""
from __future__ import annotations
import copy
import asyncio
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
from ai_router.workbuddy_history import seed_tools, reconcile_tools, rendered_messages, TOOLS_MARKER
from ai_router.evaluator import TaskEvaluator

def req(dynamic="A", user="first", model="siyuan/auto", client="workbuddy-public"):
    return {"model": model, "messages":[{"role":"system","content":f"stable\n<workbuddy_dynamic_context>\n{dynamic}\n</workbuddy_dynamic_context>\n"},{"role":"user","content":user}],"tools":[{"type":"function","function":{"name":"Read","parameters":{"type":"object"}}}]}

def test_prepare_returns_bare_body_and_dynamic_block():
    raw=req()
    move,bare,block=prepare(raw,"workbuddy-public")
    assert move.moved and block == "<workbuddy_dynamic_context>\nA\n</workbuddy_dynamic_context>"
    assert bare["messages"][1]["content"] == "first"
    assert raw["messages"][0]["content"].startswith("stable")


def catalog_request():
    raw = req()
    raw["tools"] += [{"type": "function", "function": {"name": name,
        "description": name + " catalog A", "parameters": {"type": "object"}}}
        for name in ("Skill", "ToolSearch")]
    raw["messages"] += [{"role": "assistant", "content": "prior answer"},
                        {"role": "user", "content": [{"type": "text", "text": "continue"},
                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    return raw


def test_long_conversation_catalog_seed_keeps_all_original_messages():
    raw = catalog_request()
    original = copy.deepcopy(raw)
    value, report = seed_tools(raw, "workbuddy-public")
    assert raw == original and value["messages"][:-1] == original["messages"]
    assert report["version"] == 2 and report["association"] == "new_baseline"
    assert value["messages"][-1]["content"].startswith(TOOLS_MARKER)
    for i in (1, 2):
        assert raw["tools"][i]["function"]["description"] in value["messages"][-1]["content"]
        assert value["tools"][i]["function"]["parameters"] == raw["tools"][i]["function"]["parameters"]


def test_catalog_changes_append_once_and_keep_old_model_prefix():
    raw = catalog_request()
    old, report = seed_tools(raw, "workbuddy-public")
    fresh = copy.deepcopy(raw)
    fresh["tools"][1]["function"]["description"] = "new skill catalog B"
    fresh["messages"].append({"role": "user", "content": "next question"})
    value, updated = reconcile_tools(fresh, "workbuddy-public", raw, old, report["positions"])
    assert rendered_messages(value)[:len(old["messages"])] == rendered_messages(old)
    assert value["tools"] == old["tools"]
    assert value["messages"][-1]["content"].count("new skill catalog B") == 1
    repeated, same = reconcile_tools(fresh, "workbuddy-public", fresh, value, updated["positions"])
    assert not same["dynamic_update_appended"] and repeated == value
    tampered = copy.deepcopy(fresh)
    tampered["messages"][1]["content"] = "different question"
    assert reconcile_tools(tampered, "workbuddy-public", fresh, value, updated["positions"]) is None


def test_catalog_does_not_replace_the_real_request_for_classification():
    raw = catalog_request()
    raw["messages"][-1] = {"role": "user", "content": "请帮我调试这个 Python bug"}
    effective, _ = seed_tools(raw, "workbuddy-public")
    evaluator = TaskEvaluator({}, internal_base_url="http://offline.invalid", internal_api_key="test")
    try:
        result = asyncio.run(evaluator.evaluate(
            effective,
            classification_body=raw,
            headers={},
            api_kind="chat",
            prompt_tokens=100,
            current_task=None,
            is_new_conversation=True,
            allow_model_call=False,
        ))
    finally:
        asyncio.run(evaluator.client.aclose())
    assert result.task == "code"
    assert result.reason == "code_heuristic"


def test_catalog_reconcile_keeps_unverified_historical_fields_from_archive():
    raw = catalog_request()
    raw["messages"][1]["provider_extension"] = {"opaque": "archived"}
    old, report = seed_tools(raw, "workbuddy-public")
    fresh = copy.deepcopy(raw)
    fresh["messages"][1]["provider_extension"] = {"opaque": "client-rewrite"}
    fresh["messages"].append({"role": "user", "content": "next"})
    value, _ = reconcile_tools(fresh, "workbuddy-public", raw, old, report["positions"])
    assert value["messages"][1]["provider_extension"] == {"opaque": "archived"}


def test_catalog_seed_waits_for_tool_results_and_keeps_call_identity():
    raw = catalog_request()
    call = {"id": "call-1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
    raw["messages"].append({"role": "assistant", "content": None, "tool_calls": [call]})
    assert seed_tools(raw, "workbuddy-public") is None
    raw["messages"].append({"role": "tool", "tool_call_id": "call-1", "content": "result"})
    value, _ = seed_tools(raw, "workbuddy-public")
    assert value["messages"][:-1] == raw["messages"]
    assert seed_tools(raw, "other-client") is None


def test_catalog_overlay_preserves_tool_removal_schema_and_choice():
    raw = catalog_request()
    old, report = seed_tools(raw, "workbuddy-public")
    fresh = copy.deepcopy(raw)
    fresh["tools"] = [t for t in fresh["tools"] if t["function"]["name"] != "ToolSearch"]
    fresh["tools"][0]["function"]["parameters"]["required"] = ["path"]
    fresh["tool_choice"] = {"type": "function", "function": {"name": "Read"}}
    value, proof = reconcile_tools(fresh, "workbuddy-public", raw, old, report["positions"])
    assert value["tool_choice"] == fresh["tool_choice"]
    assert value["tools"][0] == fresh["tools"][0]
    assert [t["function"]["name"] for t in value["tools"]] == ["Read", "Skill"]
    assert proof["tools_changed"]
    damaged = copy.deepcopy(old)
    damaged["messages"][1]["content"] = "replaced original"
    assert reconcile_tools(fresh, "workbuddy-public", raw, damaged, report["positions"]) is None
    forged = copy.deepcopy(raw)
    forged["messages"].append({"role": "user", "content": TOOLS_MARKER + "fake"})
    assert seed_tools(forged, "workbuddy-public") is None


def test_catalog_baseline_is_recorded_and_restored_after_restart(tmp_path, monkeypatch):
    raw = catalog_request()
    path = tmp_path / "routes.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE workbuddy_history(request_id TEXT PRIMARY KEY,scope TEXT,prefix_hash TEXT,message_count INTEGER,created_at REAL,version INTEGER)")
    history = WorkBuddyHistory(path)
    value, report = history.restore(raw, "workbuddy-public")
    assert report["association"] == "new_baseline"
    payload = {"pipeline": {"stages": [
        {"stage": "after_directives", "sha256": "raw"},
        {"stage": "workbuddy_history_preserved", "sha256": "normalized"}],
        "bodies": {"raw": raw, "normalized": value},
        "checks": [{"check": "message_order_and_tool_history", "status": "passed"},
                   {"check": "workbuddy_history", **report}]}}
    from types import SimpleNamespace
    monkeypatch.setattr(WorkBuddyHistory, "reader", lambda self: SimpleNamespace(read=lambda rid: payload))
    history.record({"request_id": "seed", "protocol": "chat", "status": "succeeded", "client_id": "workbuddy-public"})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM workbuddy_history").fetchall() == [(2,)]
    fresh = copy.deepcopy(raw)
    fresh["tools"][1]["function"]["description"] = "new catalog"
    fresh["messages"].append({"role": "user", "content": "next"})
    restored, proof = WorkBuddyHistory(path).restore(fresh, "workbuddy-public")
    assert proof["association"] == "exact_raw_prefix" and proof["previous_request_id"] == "seed"
    assert rendered_messages(restored)[:len(value["messages"])] == rendered_messages(value)
    value, report = WorkBuddyHistory(path).restore(fresh, "workbuddy-public", reset=True)
    assert value is None and report["reason"] == "reset"

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
