import asyncio
import copy
import json
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from ai_router.config import Registry
from ai_router.endpoint_tokens import EndpointTokenCounter
from ai_router.errors import NoEligibleModelError
from ai_router.routing_modes import (DEFAULTS, FLASH, PerformanceRouter,
                                     flash_order_for_window, in_work_window,
                                     resolve, validate)
from ai_router.store import InMemoryStateStore
from ai_router.types import Evaluation, RequestCapabilities, ConversationState
from ai_router.cache_audit import OutputClock
from test_intelligent_v2 import FakeHealth, status_for, v2_settings, v2_registry
from ai_router.policy import RoutingPolicy


def run(coro):
    return asyncio.run(coro)


def setup(tmp_path, mode="efficiency"):
    registry = v2_registry(tmp_path)
    for endpoint in registry.endpoints:
        if endpoint.cloud:
            endpoint.metadata["routing_quality_validated"] = True
    settings = v2_settings(tmp_path)
    cloud = settings.section("cloud")
    cloud["allowed_models"] = [e.public_model for e in registry.endpoints if e.cloud]
    settings.write_runtime({"cloud": cloud})
    store = InMemoryStateStore()
    health = FakeHealth({e.id: status_for(e) for e in registry.endpoints})
    policy = RoutingPolicy(registry, settings, health, store)
    options = resolve({"objectives": {"enabled": True, "mode": mode}})
    return policy, registry, options


def request(policy, options, **kwargs):
    values = dict(requested_model="auto", evaluation=Evaluation("general",None,1,"test"),
                  prompt_tokens=1000, output_reserve_tokens=1024, modalities={"text"},
                  has_tools=False, conversation=None, client_id="test", routing_options=options)
    values.update(kwargs)
    return policy.choose(**values)


def test_defaults_account_inheritance_and_invalid_settings():
    assert not resolve({})["enabled"]
    assert resolve({"objectives":{"enabled":True}}, "quality")["source"] == "account"
    assert resolve({"objectives":{"local_only":True}}, "quality", False)["local_only"]
    for patch in [{"mode":"bad"}, {"enabled":1}, {"performance":{"slowdown_ratio":float("nan")}},
                  {"flash_order":{"general":[]}}, {"unexpected":True}]:
        with pytest.raises(ValueError):
            validate(patch)


def test_cost_uses_local_quality_uses_pro(tmp_path):
    policy, _, options = setup(tmp_path,"cost")
    assert not run(request(policy,options)).endpoint.cloud
    options["mode"]="quality"
    assert run(request(policy,options)).endpoint.id == "cloud-deepseek-v4-pro"


def test_quality_code_astra_advisory_is_candidate_specific(tmp_path):
    policy, _, options = setup(tmp_path,"quality")
    evaluation=Evaluation("code",None,1,"test",route_profile="code")
    required=RequestCapabilities("chat",output_token_limit=True)
    d=run(request(policy,options,evaluation=evaluation,required_capabilities=required))
    assert d.endpoint.id=="codex-pro-gpt-6-astra" and d.output_token_limit_advisory
    assert required.output_token_limit
    options["allow_advisory_output_limit"]=False
    d=run(request(policy,options,evaluation=evaluation,required_capabilities=required))
    assert d.endpoint.id=="cloud-deepseek-v4-pro" and not d.output_token_limit_advisory


def test_quality_unavailable_falls_to_flash_and_strict_local(tmp_path):
    policy, registry, options = setup(tmp_path,"quality")
    for eid in set(sum(options["quality_order"].values(),[])):
        policy.health.status_values[eid]=status_for(registry.by_id(eid),healthy=False)
    assert run(request(policy,options)).endpoint.id=="cloud-deepseek-v4-flash"
    options["quality_flash_fallback"]=False
    with pytest.raises(NoEligibleModelError):
        run(request(policy,options))
    options["mode"]="cost"; options["local_only"]=True
    d=run(request(policy,options))
    assert not d.endpoint.cloud


def test_local_only_blocks_explicit_cloud(tmp_path):
    policy, registry, options = setup(tmp_path,"quality")
    options["local_only"]=True
    with pytest.raises(NoEligibleModelError):
        run(request(policy,options,requested_model=registry.by_id("cloud-deepseek-v4-pro").public_model))


def test_accurate_count_prevents_context_migration(tmp_path):
    policy, _, options = setup(tmp_path)
    conv=ConversationState(conversation_id="conversation",public_model="auto",task="general",last_seen=time.time(),endpoint_id="ai-qwen38-27b",
                           deployment_id="ai-qwen38-27b",tier_rank=20)
    d=run(request(policy,options,conversation=conv,prompt_tokens=260000,
                  candidate_prompt_tokens={"ai-qwen38-27b":128000}))
    assert d.endpoint.id=="ai-qwen38-27b" and d.prompt_tokens==128000


def test_observe_only_preserves_old_selection(tmp_path):
    policy, _, options = setup(tmp_path,"quality")
    options["observe_only"]=True
    assert not run(request(policy,options)).endpoint.cloud


def test_quality_preserves_explicit_model_and_directive(tmp_path):
    policy, registry, options = setup(tmp_path,"quality")
    model=registry.by_id("ai-qwen38-27b").public_model
    assert run(request(policy,options,requested_model=model)).endpoint.id=="ai-qwen38-27b"
    e=Evaluation("general",None,1,"test",required_endpoint_id="ai-qwen38-27b")
    assert run(request(policy,options,evaluation=e)).endpoint.id=="ai-qwen38-27b"


def trace(rid, endpoint, first=10, decode=30, cache="warm", output=300):
    return {"request_id":rid,"client_id":"test","conversation_id":"conversation","status":"succeeded","endpoint_id":endpoint,
            "performance_observation":{"endpoint_id":endpoint,"input_tokens":1000,"reasoning":"unknown","cache":cache,
                                       "output_tokens":output,"decode_tps":decode,"first_output_seconds":first}}


def test_severe_wait_moves_next_turn_and_cooldown(tmp_path):
    async def scenario():
        policy, _, options = setup(tmp_path)
        perf=options["performance"]
        await policy.performance.observe(trace("slow","ai-qwen38-27b",first=181),perf)
        conv=ConversationState(conversation_id="conversation",public_model="auto",task="general",last_seen=time.time(),endpoint_id="ai-qwen38-27b",
                               deployment_id="ai-qwen38-27b",tier_rank=20)
        d=await request(policy,options,conversation=conv)
        assert d.endpoint.id=="cloud-deepseek-v4-flash" and d.reason=="efficiency_severe_wait"
        conv=replace(conv,endpoint_id=d.endpoint.id,tier_rank=d.endpoint.tier_rank)
        await policy.performance.observe(trace("cloud","cloud-deepseek-v4-flash",first=190),perf)
        d=await request(policy,options,conversation=conv)
        assert d.endpoint.id=="cloud-deepseek-v4-flash" and d.reason=="efficiency_cooldown"
    run(scenario())


def test_sustained_slowdown_and_cold_target_samples(tmp_path):
    async def scenario():
        policy, _, options = setup(tmp_path)
        p=options["performance"]
        for i in range(5):
            await policy.performance.observe(trace("base"+str(i),"ai-qwen38-27b",decode=50),p)
            await policy.performance.observe(trace("flash"+str(i),"cloud-deepseek-v4-flash",decode=100,cache="cold",first=1),p)
        for i in range(3):
            await policy.performance.observe(trace("slow"+str(i),"ai-qwen38-27b",decode=5),p)
        conv=ConversationState(conversation_id="conversation",public_model="auto",task="general",last_seen=time.time(),endpoint_id="ai-qwen38-27b",
                               deployment_id="ai-qwen38-27b",tier_rank=20)
        d=await request(policy,options,conversation=conv)
        assert d.endpoint.cloud and d.reason in {"efficiency_measured_gain","efficiency_sustained_slowdown"}
    run(scenario())


def test_performance_bound_dedup_and_unmeasured(tmp_path):
    async def scenario():
        store=InMemoryStateStore();p=PerformanceRouter(store);config=copy.deepcopy(DEFAULTS["performance"])
        config["max_samples"]=5
        for i in range(9):await p.observe(trace(str(i),"ai",decode=None),config)
        await p.observe(trace("8","ai",decode=None),config)
        samples=await p.samples("ai",1000,"unknown","warm",config)
        assert len(samples)==5 and all(s["decode_tps"] is None for s in samples)
    run(scenario())


def test_output_clock_ignores_heartbeat_and_counts_reasoning(monkeypatch):
    values=iter([2.0,5.0])
    monkeypatch.setattr("ai_router.cache_audit.time.monotonic",lambda:next(values))
    clock=OutputClock("chat",0)
    # heartbeat frames do not call the clock.
    clock.feed(b": ping\n\n")
    clock.feed(b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n')
    clock.feed(b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n')
    assert clock.values=={"ttft_ms":2000,"last_output_ms":5000,"first_text_ms":5000}


def test_endpoint_counter_version_cache_and_fallback(tmp_path):
    async def scenario():
        registry=v2_registry(tmp_path)
        endpoint=replace(registry.by_id("ai-qwen38-27b"),metadata={"token_counting":{"enabled":True,"version":"v1"}})
        count=EndpointTokenCounter();await count.client.aclose()
        calls=[]
        def handler(request):
            calls.append(request)
            assert str(request.url).endswith("/tokenize")
            return httpx.Response(200,json={"count":123})
        count.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body={"messages":[{"role":"user","content":"test"}]}
        assert (await count.count(endpoint,body,"chat",999))["tokens"]==123
        assert (await count.count(endpoint,body,"chat",999))["cache_hit"]
        assert len(calls)==1
        changed=replace(endpoint,metadata={"token_counting":{"enabled":True,"version":"v2"}})
        await count.count(changed,body,"chat",999)
        assert len(calls)==2
        await count.close()
    run(scenario())


def test_render_cache_preserves_tool_schema_field_order(tmp_path):
    async def scenario():
        endpoint = replace(v2_registry(tmp_path).by_id("ai-qwen38-27b"),
                           metadata={"token_counting": {"enabled": True, "method": "render", "version": "v1"}})
        counter = EndpointTokenCounter()
        await counter.client.aclose()
        calls = []
        def handler(request):
            body = json.loads(request.content)
            fields = list(body["tools"][0]["function"]["parameters"]["properties"])
            calls.append(fields)
            return httpx.Response(200, json={"token_ids": list(range(11 if fields[0] == "alpha" else 12))})
        counter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body = {"messages": [{"role": "user", "content": "test"}],
                "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {
                    "type": "object", "properties": {"alpha": {"type": "string"}, "beta": {"type": "string"}}}}}]}
        assert (await counter.count(endpoint, body, "chat", 999))["tokens"] == 11
        reordered = copy.deepcopy(body)
        props = reordered["tools"][0]["function"]["parameters"]["properties"]
        reordered["tools"][0]["function"]["parameters"]["properties"] = dict(reversed(list(props.items())))
        assert body == reordered
        assert (await counter.count(endpoint, reordered, "chat", 999))["tokens"] == 12
        assert len(calls) == 2
        assert (await counter.count(endpoint, reordered, "chat", 999))["cache_hit"]
        await counter.close()
    run(scenario())


def test_slow_stream_only_switches_next_turn(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from ai_router.api import create_app
    from ai_router.runtime import build_runtime
    from ai_router.token_counter import SimpleTokenCounter
    monkeypatch.setenv("AI_ROUTER_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AI_ROUTER_LITELLM_MASTER_KEY", "test-internal")
    monkeypatch.setenv("AI_ROUTER_1PANEL_API_KEY", "test-legacy")
    monkeypatch.setenv("AI_ROUTER_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AI_ROUTER_ROUTE_TRACE_DB_PATH", str(tmp_path / "traces.sqlite3"))
    monkeypatch.setenv("AI_ROUTER_TRAINING_ENABLED", "false")
    policy, registry, options = setup(tmp_path)
    options.pop("source")
    options["performance"]["max_first_output_seconds"] = 1
    policy.settings.write_runtime({"routing":{"strategy":"intelligent_v2","objectives":options},"cloud":policy.settings.section("cloud")})
    endpoints=[replace(registry.by_id(eid), metadata={**registry.by_id(eid).metadata, "token_counting":{}})
               for eid in ["ai-qwen38-27b", "cloud-deepseek-v4-flash"]]
    registry=registry.with_endpoints(endpoints)
    runtime=build_runtime(settings=policy.settings, registry=registry, store=policy.store, token_counter=SimpleTokenCounter())
    run(runtime.health.client.aclose())
    runtime.health=FakeHealth({e.id:status_for(e) for e in endpoints})
    runtime.policy=RoutingPolicy(registry, runtime.settings, runtime.health, runtime.store)
    calls=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": heartbeat\n\n"
            await asyncio.sleep(1.2 if len(calls)==1 else .01)
            yield ('data: '+json.dumps({"id":"synthetic","choices":[{"index":0,"delta":{"content":"complete"},"finish_reason":None}]})+'\n\n').encode()
            yield ('data: '+json.dumps({"id":"synthetic","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":8,"total_tokens":108}})+'\n\ndata: [DONE]\n\n').encode()
    async def upstream(req):
        calls.append(json.loads(req.content)["model"])
        return httpx.Response(200,headers={"content-type":"text/event-stream"},stream=Stream())
    run(runtime.internal_client.aclose())
    runtime.internal_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_app(runtime)) as client:
        headers={"Authorization":"Bearer test-legacy", "X-1Panel-Conversation-ID":"slow-stream-lineage"}
        history=[{"role":"user","content":"List three small integers."}]
        for index in range(2):
            response=client.post("/v1/chat/completions",headers=headers,
                                 json={"model":"auto","messages":history,"stream":True,"max_tokens":128})
            assert response.status_code==200,response.text
            assert "complete" in response.text
            assert len(calls)==index+1
            history += [{"role":"assistant","content":"complete"},{"role":"user","content":"Continue with three more."}]
        assert calls==[endpoints[0].provider_model,endpoints[1].id]


def test_quality_requires_separate_validation(tmp_path):
    policy, registry, options=setup(tmp_path,"quality")
    registry.by_id("cloud-deepseek-v4-pro").metadata.pop("routing_quality_validated")
    assert run(request(policy,options)).endpoint.id != "cloud-deepseek-v4-pro"


TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_ROUTING = {"objectives": {"enabled": True, "mode": "cost", "schedule": {
    "enabled": True,
    "work_flash_order": {"general": ["zhipu-glm-5.3-flash", "cloud-deepseek-v4-flash"]},
    "off_hours_flash_order": {"general": ["cloud-deepseek-v4-flash", "zhipu-glm-5.3-flash"]}}}}


def frozen_clock(monkeypatch, moment):
    """Pin routing_modes.datetime so window tests never depend on wall time."""
    import ai_router.routing_modes as modes

    class Clock:
        current = moment

        @classmethod
        def now(cls, zone=None):
            return cls.current.astimezone(zone) if zone else cls.current

    monkeypatch.setattr(modes, "datetime", Clock)
    return Clock


def test_schedule_defaults_off_change_nothing():
    assert DEFAULTS["schedule"]["enabled"] is False
    options = resolve({"objectives": {"enabled": True}})
    assert options["schedule_window"] is None and options["flash_order"] == FLASH
    assert flash_order_for_window(DEFAULTS["schedule"]) == {}
    # 两个 order 都写好，只要 enabled 为 false 就不得生效
    options = resolve({"objectives": {"enabled": True, "schedule": {"enabled": False,
        "work_flash_order": {"general": ["zhipu-glm-5.3-flash"]}}}})
    assert options["schedule_window"] is None and options["flash_order"] == FLASH


def test_schedule_window_switches_general_flash_only(monkeypatch):
    clock = frozen_clock(monkeypatch, datetime(2026, 9, 15, 10, 0, tzinfo=TZ))
    options = resolve(SCHEDULE_ROUTING)
    assert options["schedule_window"] == "work"
    assert options["flash_order"]["general"] == ["zhipu-glm-5.3-flash", "cloud-deepseek-v4-flash"]
    # 只覆盖 general；code 与 multimodal 沿用 objectives.flash_order
    assert options["flash_order"]["code"] == FLASH["code"]
    assert options["flash_order"]["multimodal"] == FLASH["multimodal"]
    clock.current = datetime(2026, 9, 15, 22, 0, tzinfo=TZ)
    options = resolve(SCHEDULE_ROUTING)
    assert options["schedule_window"] == "off_hours"
    assert options["flash_order"]["general"] == ["cloud-deepseek-v4-flash", "zhipu-glm-5.3-flash"]


def test_schedule_window_boundaries_days_and_timezone():
    schedule = {"enabled": True, "timezone": "Asia/Shanghai", "work_windows": [
        {"days": ["MO", "TU", "WE", "TH", "FR"], "ranges": ["09:00-12:00", "14:00-18:00"]}]}
    for hour, minute, expected in [(8, 59, False), (9, 0, True), (11, 59, True), (12, 0, False),
                                   (13, 59, False), (14, 0, True), (17, 59, True), (18, 0, False),
                                   (23, 59, False)]:
        assert in_work_window(schedule, datetime(2026, 9, 15, hour, minute, tzinfo=TZ)) is expected
    assert in_work_window(schedule, datetime(2026, 9, 14, 9, 0, tzinfo=TZ)) is True
    for day in (19, 20):
        assert in_work_window(schedule, datetime(2026, 9, day, 10, 0, tzinfo=TZ)) is False
    weekend = {"enabled": True, "timezone": "Asia/Shanghai",
               "work_windows": [{"days": ["SA", "SU"], "ranges": ["10:00-12:00"]}]}
    assert in_work_window(weekend, datetime(2026, 9, 19, 11, 0, tzinfo=TZ)) is True
    assert in_work_window(weekend, datetime(2026, 9, 19, 13, 0, tzinfo=TZ)) is False
    # 时区敏感：同一 UTC 时刻，北京落在窗内而 UTC 落在窗外
    moment = datetime(2026, 9, 15, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert in_work_window(schedule, moment) is True
    assert in_work_window({**schedule, "timezone": "UTC"}, moment) is False
    assert in_work_window({**schedule, "enabled": False}, moment) is None


def test_schedule_validation_rejects_invalid_configuration():
    base = {"enabled": True, "mode": "efficiency",
            "flash_order": {"general": ["a"], "code": ["a"], "multimodal": ["a"]},
            "quality_order": {"general": ["a"], "code": ["a"], "multimodal": ["a"]}}
    validate({**base, "schedule": {"enabled": True,
        "work_windows": [{"days": ["MO"], "ranges": ["09:00-12:00"]}],
        "work_flash_order": {"general": ["a", "b"]}, "off_hours_flash_order": {}}})
    for patch in [
            {"schedule": {"enabled": True, "bogus": 1}},
            {"schedule": {"enabled": "yes"}},
            {"schedule": {"enabled": True, "timezone": "Mars/Olympus"}},
            {"schedule": {"enabled": True, "timezone": "   "}},
            {"schedule": {"enabled": True, "work_windows": []}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["FUNDAY"], "ranges": ["09:00-12:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO", "MO"], "ranges": ["09:00-12:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["18:00-09:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["9am-6pm"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["9:00-12:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["09:99-12:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["24:00-24:00"]}]}},
            {"schedule": {"enabled": True, "work_windows": [{"days": ["MO"], "ranges": ["09:00-24:01"]}]}},
            {"schedule": {"enabled": True, "work_flash_order": {"video": ["a"]}}},
            {"schedule": {"enabled": True, "work_flash_order": {"general": []}}},
            {"schedule": {"enabled": True, "work_flash_order": {"general": ["a", "a"]}}},
            {"schedule_window": "work"}]:
        with pytest.raises(ValueError):
            validate({**base, **patch})

    validate({**base, "schedule": {"enabled": True,
        "work_windows": [{"days": ["MO"], "ranges": ["00:00-24:00"]}]}})


def test_schedule_partial_override_merge_semantics():
    from ai_router.routing_modes import settings_value
    schedule = settings_value({"objectives": {"enabled": True, "schedule": {
        "enabled": True, "timezone": "UTC", "work_flash_order": {"general": ["g1", "d1"]}}}})["schedule"]
    assert schedule["timezone"] == "UTC"
    assert schedule["work_flash_order"] == {"general": ["g1", "d1"]}
    assert schedule["off_hours_flash_order"] == {}
    # 未提供的键保留默认：默认工作窗口仍在
    assert schedule["work_windows"][0]["days"] == ["MO", "TU", "WE", "TH", "FR"]


def test_schedule_window_selects_cloud_flash_endpoint(tmp_path, monkeypatch):
    clock = frozen_clock(monkeypatch, datetime(2026, 9, 15, 10, 0, tzinfo=TZ))
    policy, registry, _ = setup(tmp_path, "cost")
    local = {e.id for e in registry.endpoints if not e.cloud}
    decision = run(request(policy, resolve(SCHEDULE_ROUTING), excluded_endpoint_ids=local))
    assert decision.endpoint.id == "zhipu-glm-5.3-flash" and decision.reason == "cost_flash_fallback"
    clock.current = datetime(2026, 9, 15, 22, 0, tzinfo=TZ)
    decision = run(request(policy, resolve(SCHEDULE_ROUTING), excluded_endpoint_ids=local))
    assert decision.endpoint.id == "cloud-deepseek-v4-flash"
