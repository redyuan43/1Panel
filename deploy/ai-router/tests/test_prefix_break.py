"""CPU-only contracts for bounded prefix-break diagnostics."""
from __future__ import annotations
import asyncio
import json
import sqlite3
import tempfile
import pathlib
from types import SimpleNamespace
import pytest
import ai_router.prefix_break as mod

def _trace(request_id, started, *, conversation="conv", client="public", model="m", deployment="d", status="succeeded", completed=1002):
    return {"request_id": request_id, "started_at": started, "completed_at": completed, "status": status, "client_id": client, "conversation_id": conversation, "selected_model": model, "deployment_id": deployment, "attempts": [{"number": 1}]}

def _decision(*, backend="cpu"):
    return SimpleNamespace(attempts=1, endpoint=SimpleNamespace(backend_type=backend, api_base="http://model", backend_api_key_env="KEY"), deployment_id="d", deployment_details={}, upstream_api_base=None)

def test_stage_and_tool_reports_are_metadata_only():
    old = {"tools": [{"function": {"name": "A", "description": "secret old"}}], "messages": [{"role": "user", "content": "secret prompt", "tool_calls": [{"id": "x"}]}]}
    new = {"tools": [{"function": {"name": "B", "description": "secret new"}}], "messages": [{"role": "user", "content": "secret changed", "tool_calls": [{"id": "x"}]}]}
    delta = mod.tool_delta(old, new)
    assert delta == {"previous_count": 1, "current_count": 1, "added_count": 1, "removed_count": 1, "order_changed": True, "definitions_changed": True}
    encoded = json.dumps(delta)
    assert "secret" not in encoded and "prompt" not in encoded

def test_compare_stages_uses_rendered_message_fields_only():
    old = {"messages": [{"role": "user", "content": "same", "rawUsage": "old-secret"}], "tools": []}
    new = {"messages": [{"role": "user", "content": "same", "rawUsage": "new-secret"}], "tools": []}
    result = mod.compare_stages({"normalized": old}, {"normalized": new})
    row = next(x for x in result if x["stage"] == "normalized")
    assert row["state"] == "same"

def test_inspect_uses_conversation_rows_and_attempt_numbers(monkeypatch):
  async def run():
    previous = _trace("old", 10, completed=11)
    current = _trace("new", 20)
    class Traces:
        async def conversation(self, conversation_id, *, limit):
            assert conversation_id == "conv" and limit == 100
            return [current, previous]
    runtime = SimpleNamespace(route_traces=Traces())
    collector = mod.PrefixBreakCollector(runtime)
    result = {"request_id": "new", "attempt": 1, "state": "unavailable", "token_source": "native_tokenize_reconstruction"}
    monkeypatch.setattr(mod, "ArchiveReader", lambda *paths: SimpleNamespace(read=lambda request_id: {"pipeline": {"stages": [{"stage": "forwarded_1", "sha256": request_id}], "bodies": {request_id: {"tools": [], "messages": []}}}}))
    await collector._inspect(current, _decision(backend="cpu"), result)
    assert result["previous_request_id"] == "old"
    assert result["state"] == "structural"
    assert result["reason"] == "native_tokenize_not_supported"
  asyncio.run(run())

def test_inspect_rejects_overlap_before_archive_read(monkeypatch):
  async def run():
    previous = _trace("old", 10, completed=30)
    current = _trace("new", 20)
    class Traces:
        async def conversation(self, *_args, **_kwargs): return [current, previous]
    collector = mod.PrefixBreakCollector(SimpleNamespace(route_traces=Traces()))
    result = {"request_id": "new", "attempt": 1, "state": "unavailable", "token_source": "native_tokenize_reconstruction"}
    monkeypatch.setattr(mod, "ArchiveReader", lambda *paths: (_ for _ in ()).throw(AssertionError("overlap must short-circuit")))
    await collector._inspect(current, _decision(), result)
    assert result["reason"] == "overlapping_requests"
  asyncio.run(run())

def test_submit_is_bounded_and_close_cancels_tasks():
  async def run():
    class Traces:
        async def conversation(self, *_args, **_kwargs): return []
    collector = mod.PrefixBreakCollector(SimpleNamespace(route_traces=Traces()))
    started = asyncio.Event()
    async def blocked(*_args):
        started.set(); await asyncio.Event().wait()
    collector._guard = blocked
    trace = _trace("one", 1)
    collector.submit(trace, _decision())
    await asyncio.wait_for(started.wait(), 1)
    assert len(collector.tasks) == 1
    collector.submit(_trace("two", 2), _decision())
    collector.submit(_trace("three", 3), _decision())
    assert len(collector.tasks) == 2
    await collector.close()
    assert not collector.tasks
  asyncio.run(run())

def test_inspect_counterfactual_attributes_only_tools(monkeypatch):
  async def run():
    previous = _trace("old", 10, completed=11)
    current = _trace("new", 20)
    class Traces:
        async def conversation(self, *_args, **_kwargs): return [current, previous]
    old_body = {"tools": [{"function": {"name": "A"}}], "messages": [{"role": "user", "content": "x"}]}
    new_body = {"tools": [{"function": {"name": "B"}}], "messages": [{"role": "user", "content": "x"}]}
    payloads = {"old": {"pipeline": {"stages": [{"stage": "forwarded_1", "sha256": "a"}], "bodies": {"a": old_body}}}, "new": {"pipeline": {"stages": [{"stage": "forwarded_1", "sha256": "b"}], "bodies": {"b": new_body}}}}
    class Reader:
        def __init__(self, *paths): self.paths = paths
        def read(self, request_id): return payloads[request_id]
    class Response:
        def __init__(self, tokens): self.tokens = tokens
        def raise_for_status(self): pass
        def json(self): return {"tokens": self.tokens}
    calls = []
    class Client:
        async def post(self, url, *, json, headers, timeout):
            calls.append(json)
            return Response([1, 2] if json is old_body else ([9, 2] if len(calls) == 2 else [1, 2]))
    runtime = SimpleNamespace(route_traces=Traces(), internal_client=Client())
    monkeypatch.setattr(mod, "ArchiveReader", Reader)
    result = {"request_id": "new", "attempt": 1, "state": "unavailable", "token_source": "native_tokenize_reconstruction"}
    await mod.PrefixBreakCollector(runtime)._inspect(current, _decision(backend="vllm"), result)
    assert result["state"] == "compared"
    assert result["cause_field"] == "tools"
    assert result["attribution"] == "native_tokenize_counterfactual"
    assert len(calls) == 3
  asyncio.run(run())

def test_save_is_attempt_scoped_and_removes_only_expired_rows():
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "traces.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE prefix_breaks (request_id TEXT NOT NULL, attempt INTEGER NOT NULL, created_at REAL NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(request_id, attempt))")
            db.execute("INSERT INTO prefix_breaks VALUES (?,?,?,?)", ("old", 1, 1.0, "{}"))
            db.execute("INSERT INTO prefix_breaks VALUES (?,?,?,?)", ("keep", 1, 2591999.0, "{}"))
        collector = mod.PrefixBreakCollector(SimpleNamespace(route_traces=SimpleNamespace(database_path=path)))
        now = mod.time.time
        mod.time.time = lambda: 2678400.0
        try:
            collector._save({"request_id": "keep", "attempt": 2})
        finally:
            mod.time.time = now
        with sqlite3.connect(path) as db:
            rows = db.execute("SELECT request_id, attempt FROM prefix_breaks ORDER BY request_id, attempt").fetchall()
        assert rows == [("keep", 1), ("keep", 2)]

def test_guard_records_invalid_tokenize_result_as_unavailable(monkeypatch):
    class Traces:
        async def conversation(self, *_args, **_kwargs):
            return [_trace("new", 20), _trace("old", 10, completed=11)]
    class Reader:
        def __init__(self, *paths): pass
        def read(self, request_id):
            return {"pipeline": {"stages": [{"stage": "forwarded_1", "sha256": request_id}], "bodies": {request_id: {"tools": [], "messages": []}}}}
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"tokens": [True]}
    class Client:
        async def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(mod, "ArchiveReader", Reader)
    async def run():
        runtime = SimpleNamespace(route_traces=Traces(), internal_client=Client())
        result = {"request_id": "new", "attempt": 1, "state": "unavailable", "token_source": "native_tokenize_reconstruction"}
        await mod.PrefixBreakCollector(runtime)._guard(_trace("new", 20), _decision(backend="vllm"))
    # _guard's save path is intentionally replaced to observe its terminal result.
    saved = []
    monkeypatch.setattr(mod.PrefixBreakCollector, "_save", lambda self, value: saved.append(value))
    asyncio.run(run())
    assert saved and saved[0]["state"] == "unavailable"
    assert saved[0]["reason"] == "ValueError"

def test_guard_converts_missing_archive_to_unavailable(monkeypatch):
    class Traces:
        async def conversation(self, *_args, **_kwargs):
            return [_trace("new", 20), _trace("old", 10, completed=11)]
    monkeypatch.setattr(mod, "ArchiveReader", lambda *paths: (_ for _ in ()).throw(FileNotFoundError("missing")))
    saved = []
    monkeypatch.setattr(mod.PrefixBreakCollector, "_save", lambda self, value: saved.append(value))
    async def run():
        runtime = SimpleNamespace(route_traces=Traces())
        await mod.PrefixBreakCollector(runtime)._guard(_trace("new", 20), _decision(backend="vllm"))
    asyncio.run(run())
    assert saved and saved[0]["state"] == "unavailable"
    assert saved[0]["reason"] == "FileNotFoundError"


def test_verified_raw_prefix_can_cross_inferred_lineage(monkeypatch):
  async def run():
    old=_trace("old",10,completed=11)
    current=_trace("new",20,conversation="different")
    current["observation"]={"content":{"checks":[{"check":"workbuddy_history","association":"exact_raw_prefix","previous_request_id":"old"}]}}
    body={"messages":[{"role":"user","content":"x"}]}
    payload={"pipeline":{"stages":[{"stage":"forwarded_1","sha256":"a"}],"bodies":{"a":body}}}
    class Traces:
      async def conversation(self,*args,**kwargs):return [current]
      async def get(self,rid):assert rid=="old";return old
    class Reader:
      def __init__(self,*args):pass
      def read(self,rid):return payload
    monkeypatch.setattr(mod,"ArchiveReader",Reader)
    result={"state":"unavailable"}
    await mod.PrefixBreakCollector(SimpleNamespace(route_traces=Traces()))._inspect(current,_decision(),result)
    assert result["previous_request_id"]=="old"
    assert result["association"]=="verified_raw_prefix"
    assert result["state"]=="structural"
  asyncio.run(run())


def test_unconfirmed_history_is_not_called_a_new_baseline():
  async def run():
    current=_trace("new",20)
    current["observation"]={"content":{"checks":[{"check":"workbuddy_history","association":"unconfirmed"}]}}
    class Traces:
      async def conversation(self,*args,**kwargs):return [current]
    result={}
    await mod.PrefixBreakCollector(SimpleNamespace(route_traces=Traces()))._inspect(current,_decision(),result)
    assert result["state"]=="unavailable"
    assert result["reason"]=="raw_history_association_unconfirmed"
  asyncio.run(run())
