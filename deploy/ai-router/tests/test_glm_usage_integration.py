"""Real stream parser and audit-to-ledger path, all upstream bytes synthetic."""
import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from ai_router import api
from ai_router.config import Registry
from ai_router.costs import CostLedger
from ai_router.identity import IdentityProfile
from ai_router.route_trace import DecisionTrace, RouteTraceStore


class Stream(httpx.AsyncByteStream):
    def __init__(self,frames,cancel): self.frames,self.cancel=frames,cancel
    async def __aiter__(self):
        for frame in self.frames: yield frame
        if self.cancel: raise asyncio.CancelledError()


@pytest.mark.parametrize("stream",[False,True])
@pytest.mark.parametrize("kind",["measured","zero_output","no_output","no_usage","cancelled"])
def test_glm_final_usage_prices_actual_tokens_without_budget_reservation(tmp_path,stream,kind):
    async def run():
        endpoint=Registry(Path(__file__).resolve().parents[1]/"config/registry.yaml").by_id("zhipu-glm-5.3-flash")
        store=RouteTraceStore(tmp_path/"traces.sqlite3")
        decision=api.RouteDecision(endpoint=endpoint,requested_model=endpoint.public_model,task="general",prompt_tokens=99999,
                                   output_reserve_tokens=65536,reason="explicit_model",affinity="explicit",score=1)
        decision.trace=DecisionTrace(request_id="integration",instance_id="test",boot_id="test",client_id="test",key_id="test",protocol="chat",
                                     requested_model=endpoint.public_model,excerpt={},settings_hash="test",registry_hash="test")
        decision.trace.payload["selected_model"]=endpoint.public_model
        decision.trace.record(1,"upstream_request","running",evidence={"selected_model":endpoint.public_model})
        current=SimpleNamespace(instance_id="test",boot_id="test",settings=SimpleNamespace(section=lambda name:{}),
            audit=SimpleNamespace(write=mock.Mock()),policy=SimpleNamespace(mark_deployment_recent=mock.AsyncMock()),
            clients=SimpleNamespace(record_usage=mock.AsyncMock()),health=SimpleNamespace(status=mock.AsyncMock(return_value=SimpleNamespace(cache_generation="test"))),
            prefix_affinity=SimpleNamespace(invalidate_worker=mock.AsyncMock(),record_worker=mock.AsyncMock()),
            prefix_break_collector=SimpleNamespace(submit=lambda *args:None),compactor=None,conversations=None,training=None)
        value={"prompt_tokens":1000,"completion_tokens":10,"prompt_tokens_details":{"cached_tokens":750}}
        if kind=="zero_output": value["completion_tokens"]=0
        if kind=="no_output": value.pop("completion_tokens")
        if kind=="no_usage": value={}
        async def save(runtime,trace): await store.save(trace)
        with mock.patch.object(api,"_prefix_cache_delta",new=mock.AsyncMock(return_value=None)),mock.patch.object(api,"_save_request_trace",new=save):
            if stream:
                frame=lambda data:b'data: '+json.dumps(data).encode()+b'\n\n'
                # GLM attaches usage to its final choices frame, not a usage-only event.
                frames=[frame({"choices":[{"index":0,"delta":{"content":"test"},"finish_reason":None}]})]
                if kind!="cancelled":
                    frames.extend([frame({"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":value}),b'data: [DONE]\n\n'])
                response=httpx.Response(200,stream=Stream(frames,kind=="cancelled"))
                finalizer=SimpleNamespace(begin_stream=mock.AsyncMock(),finish_stream=mock.Mock())
                with mock.patch.object(api,"persist_history",new=mock.AsyncMock()),mock.patch.object(api,"_run_stream_resource_finalizer",new=mock.AsyncMock()):
                    try:
                        async for _ in api._stream_response(current,response,resource_finalizer=finalizer,client_id="test",key_id="test",request_id="integration",conversation_id=None,
                            decision=decision,state=None,body={"stream":True},api_kind="chat",training_token=None,started_at=time.monotonic(),cache_snapshot=None,identity=IdentityProfile.from_settings({"enabled":False}),identifiers=()):pass
                    except asyncio.CancelledError:
                        assert kind=="cancelled"
            else:
                await api._audit(current,request_id="integration",client_id="test",key_id="test",conversation_id=None,decision=decision,
                    status_code=499 if kind=="cancelled" else 200,started_at=time.monotonic(),usage_complete=kind!="cancelled",response_payload=json.dumps({"usage":value}).encode())
        row=CostLedger(store.database_path).requests(since=0,until=2**40)["items"][0]
        if kind in {"measured","zero_output"}:
            assert row["measurement"]=="measured" and row["input_tokens"]==1000
            assert row["total_cny"]==("0.000400500" if kind=="measured" else "0.000372500")
        else:
            assert row["measurement"]=="unknown" and row["total_cny"] is None
    asyncio.run(run())
