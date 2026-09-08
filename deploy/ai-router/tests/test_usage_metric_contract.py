"""Independent cache evidence contracts, synthetic CPU fixtures only."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock
import httpx

_PARENT=Path(__file__).resolve().parents[1]
_DEFAULT_WORK=_PARENT if (_PARENT/'ai_router').is_dir() else _PARENT/'worktree/deploy/ai-router'
WORK=Path(os.environ.get('AI_CACHE_USAGE_WORK',_DEFAULT_WORK))
sys.path.insert(0,str(WORK))
from ai_router import api
from ai_router.cache_audit import CacheAudit, metrics
from ai_router.config import Registry
from ai_router.identity import IdentityProfile
from ai_router.route_trace import DecisionTrace
from ai_router.usage_evidence import usage_measurement, token_count


def usage(total=100,cached=75,shape='chat'):
    return {'prompt_tokens' if shape=='chat' else 'input_tokens':total,
            'prompt_tokens_details' if shape=='chat' else 'input_tokens_details':{'cached_tokens':cached}}

def evidence(value):
    return {'input_tokens':999,'cached_prompt_tokens':None,'cache_measurement_source':'upstream_usage',
            'backend_usage':usage_measurement(usage=value)}

def trace(evidences,status='succeeded'):
    return {'request_id':'synthetic-request','started_at':1000,'completed_at':None if status=='running' else 1002,
            'status':status,'attempts':[{'number':i+1,'steps':[{'node_id':'upstream_request','evidence':e}]} for i,e in enumerate(evidences)]}

def native(**overrides):
    item={'request_id':'synthetic-request','operation_id':'synthetic-op','kind':'foreground','attempt':1,
          'terminal':True,'status':'completed','status_code':200,
          'timings':{'prompt_n':20,'cache_n':100},'cache':{'fixed_tokens':100,'prime_tokens':100}}
    item.update(overrides)
    return item

class MetricContract(unittest.TestCase):
    def unknown(self,row,status='unknown'):
        self.assertEqual(row['cache_status'],status)
        self.assertEqual(row['cache_measurement'],'unavailable')
        for field in ('backend_cached_tokens','backend_reuse_ratio','uncached_input_tokens'):
            self.assertIsNone(row[field],field)

    def test_chat_responses_nested_and_zero_measurements(self):
        for shape in ('chat','responses'):
            for total,cached,state,ratio in ((100,75,'hit',.75),(100,0,'miss',0),(0,0,'miss',None),(100,100,'hit',1)):
                for envelope in ('dict','top','nested'):
                    with self.subTest(shape=shape,total=total,cached=cached,envelope=envelope):
                        value=usage(total,cached,shape)
                        measured=usage_measurement(usage=value) if envelope=='dict' else usage_measurement(json.dumps({'usage':value} if envelope=='top' else {'response':{'usage':value}}).encode())
                        row=metrics(trace([{'backend_usage':measured}]),[])
                        self.assertEqual(row['cache_status'],state)
                        self.assertEqual(row['cache_measurement'],'measured')
                        self.assertEqual(row['backend_usage_source'],'upstream_usage')
                        self.assertEqual(row['backend_input_tokens'],total)
                        self.assertEqual(row['backend_cached_tokens'],cached)
                        self.assertEqual(row['backend_reuse_ratio'],ratio)
                        self.assertEqual(row['uncached_input_tokens'],total-cached)

    def test_partial_usage_does_not_borrow_router_estimate(self):
        for value in ({},{'prompt_tokens':100},{'input_tokens':100},
                      {'prompt_tokens_details':{'cached_tokens':75}},{'input_tokens_details':{'cached_tokens':0}}):
            with self.subTest(value=value):
                measured=usage_measurement(usage=value)
                self.assertEqual(measured['state'],'missing')
                self.unknown(metrics(trace([evidence(value)]),[]))

    def test_invalid_counts_and_conflicting_aliases_are_unknown(self):
        bad=(None,True,False,-1,float('nan'),float('inf'),float('-inf'),'75',.5,[],{},2**53,2**100)
        cases=[]
        for field in ('total','cached'):
            for value in bad: cases.append(usage(value,75) if field=='total' else usage(100,value))
        cases += [usage(100,101),{**usage(),'input_tokens':101},
                  {**usage(),'input_tokens_details':{'cached_tokens':74}},
                  {**usage(),'cache_read_input_tokens':None}]
        for value in cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(usage_measurement(usage=value)['state'],'invalid')
                self.unknown(metrics(trace([evidence(value)]),[]))
        compatible={**usage(),'input_tokens':100,'cache_read_input_tokens':75,'input_tokens_details':{'cached_tokens':75}}
        self.assertEqual(usage_measurement(usage=compatible)['state'],'complete')

    def test_retry_final_missing_invalid_and_success_stay_separate(self):
        earlier=evidence(usage(100,90))
        for last in ({},evidence({}),evidence({'prompt_tokens':100}),evidence(usage(100,-1))):
            with self.subTest(last=last):
                self.unknown(metrics(trace([earlier,last]),[native(attempt=1)]))
        final=evidence(usage(200,0))
        row=metrics(trace([earlier,final]),[native(attempt=1)])
        self.assertEqual(row['cache_status'],'miss')
        self.assertEqual(row['backend_input_tokens'],200)
        self.assertEqual(row['backend_cached_tokens'],0)

    def test_cancel_failed_running_do_not_prove_hit(self):
        for status in ('interrupted','cancelled','failed','running'):
            with self.subTest(status=status):
                self.unknown(metrics(trace([evidence(usage())],status),[native()]),'running' if status=='running' else 'unknown')

    def test_foreign_requests_and_background_do_not_supply_foreground_counts(self):
        self.unknown(metrics(trace([{}]),[native(request_id='other-request')]))
        self.unknown(metrics(trace([{}]),[native(kind='prewarm')]))
        self.unknown(metrics(trace([{},{}]),[native(attempt=1),native(attempt=2,kind='prewarm')]))

    def test_native_terminal_requires_known_success_status(self):
        for status in (None,'unknown','running','failed','interrupted'):
            with self.subTest(native_status=status):
                self.unknown(metrics(trace([{}]),[native(status=status)]))

    def test_global_counter_delta_is_estimated_only(self):
        for cached in (0,75):
            e={'input_tokens':100,'cached_prompt_tokens':cached,'cache_measurement_source':'backend_counter_delta','backend_usage':{'state':'missing'}}
            row=metrics(trace([e]),[])
            self.assertEqual(row['cache_status'],'estimated')
            self.assertEqual(row['cache_measurement'],'estimated')
            self.assertEqual(row['estimated_cached_tokens'],cached)
            self.assertIsNone(row['backend_cached_tokens'])
            self.assertIsNone(row['backend_reuse_ratio'])
        for e in ({'input_tokens':100,'cached_prompt_tokens':101,'cache_measurement_source':'backend_counter_delta'},
                  {'cached_prompt_tokens':75,'cache_measurement_source':'backend_counter_delta'}):
            self.unknown(metrics(trace([e]),[]))

    def test_native_backend_reuse_remains_distinct_from_preparation_adjusted_net(self):
        row=metrics(trace([{}]),[native()])
        self.assertEqual(row['backend_cached_tokens'],100)
        self.assertAlmostEqual(row['backend_reuse_ratio'],100/120)
        self.assertEqual(row['total_prefill_tokens'],120)
        self.assertEqual(row['fixed_reuse_ratio'],0)
        self.assertEqual(row['net_cache_ratio'],0)

    def test_summary_backend_ratio_excludes_unknown_estimated_failed_running(self):
        rows=[metrics(trace([evidence(usage(100,75))]),[]),metrics(trace([evidence(usage(100,0))]),[]),
              metrics(trace([evidence({})]),[]),metrics(trace([evidence(usage())],'failed'),[]),
              metrics(trace([evidence(usage())],'running'),[]),
              metrics(trace([{'input_tokens':100,'cached_prompt_tokens':75,'cache_measurement_source':'backend_counter_delta'}]),[])]
        summary=CacheAudit.summarize({'items':rows,'total':len(rows),'truncated':False,'since':0,'until':2000})
        self.assertEqual(summary['metrics']['backend_reuse_ratio']['n'],2)
        self.assertEqual(summary['metrics']['backend_reuse_ratio']['median'],.375)

class BoundaryStream(httpx.AsyncByteStream):
    def __init__(self,frames,cancel=False): self.frames=frames; self.cancel=cancel; self.closed=False
    async def __aiter__(self):
        for frame in self.frames: yield frame
        if self.cancel: raise asyncio.CancelledError()
    async def aclose(self): self.closed=True

class FinalizationContract(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self,frames,*,cancel=False,api_kind='chat',adapter=False,allow_protocol_error=False):
        stream=BoundaryStream(frames,cancel)
        upstream=httpx.Response(200,stream=stream)
        upstream.extensions['internal_cache_usage']=api_kind=='chat'
        endpoint=Registry(WORK/'config/registry.yaml').by_id('ai-qwen38-27b')
        decision=api.RouteDecision(endpoint=endpoint,requested_model=endpoint.public_model,task='general',
            prompt_tokens=999,output_reserve_tokens=16,reason='explicit_model',affinity='explicit',score=1)
        decision.native_or_adapter='adapter' if adapter else 'native'
        current=SimpleNamespace(compactor=None,conversations=None,training=None)
        finalizer=SimpleNamespace(begin_stream=mock.AsyncMock(),finish_stream=mock.Mock())
        audit_mock=mock.AsyncMock()
        parts=[]
        protocol_error_seen=False
        with mock.patch.object(api,'persist_history',new=mock.AsyncMock()),mock.patch.object(api,'_audit',new=audit_mock),mock.patch.object(api,'_run_stream_resource_finalizer',new=mock.AsyncMock()):
            try:
                async for part in api._stream_response(current,upstream,resource_finalizer=finalizer,
                    client_id='synthetic',key_id='synthetic',request_id='synthetic-request',conversation_id=None,
                    decision=decision,state=None,body={'stream':True},api_kind=api_kind,training_token=None,
                    started_at=time.monotonic()-.01,cache_snapshot=None,
                    identity=IdentityProfile.from_settings({'enabled':False}),identifiers=()): parts.append(part)
            except httpx.RemoteProtocolError:
                protocol_error_seen=True
                self.assertTrue(allow_protocol_error,'unexpected protocol error')
            except asyncio.CancelledError:
                self.assertTrue(cancel,'unexpected cancellation')
            else:
                self.assertFalse(cancel,'cancellation did not propagate')
        if allow_protocol_error:
            self.assertTrue(protocol_error_seen,'expected protocol error did not propagate')
        self.assertTrue(stream.closed)
        self.assertEqual(audit_mock.await_count,1)
        return b''.join(parts),audit_mock.await_args.kwargs

    def frame(self,value): return b'data: '+json.dumps(value).encode()+b'\n\n'

    async def test_cancellation_after_usage_cannot_complete_with_200(self):
        for finished in (False,True):
            with self.subTest(finish_reason_seen=finished):
                frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'stop' if finished else None}]}),
                        self.frame({'choices':[],'usage':usage()})]
                public,final=await self.run_stream(frames,cancel=True)
                print('cancel_boundary='+json.dumps({'finish_reason_seen':finished,'audited_status_code':final['status_code'],'usage_complete':usage_measurement(usage=final['usage'])['state']=='complete'}))
                self.assertEqual(final['status_code'],499,'CancelledError must not turn provisional usage into a successful cache hit')

    async def test_clean_eof_with_usage_requires_upstream_finish_marker(self):
        for adapter in (False,True):
            for finished in (False,True):
                with self.subTest(adapter=adapter,finish_reason_seen=finished):
                    frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'stop' if finished else None}]}),
                            self.frame({'choices':[],'usage':usage()})]
                    public,final=await self.run_stream(frames,api_kind='responses' if adapter else 'chat',adapter=adapter,allow_protocol_error=adapter and not finished)
                    print('eof_boundary='+json.dumps({'adapter':adapter,'finish_reason_seen':finished,'audited_status_code':final['status_code']}))
                    self.assertEqual(final['status_code'],200 if finished else 499,'adapter-generated terminal cannot prove missing upstream completion')

    async def test_native_responses_failed_or_incomplete_cannot_become_success_at_done(self):
        for state in ('completed','incomplete','failed'):
            for done in (False,True):
                with self.subTest(response_state=state,done=done):
                    frames=[self.frame({'type':'response.'+state,'response':{'id':'synthetic-response','status':state,'usage':usage(100,75,'responses'),'output':[]}})]
                    if done: frames.append(b'data: [DONE]\n\n')
                    public,final=await self.run_stream(frames,api_kind='responses')
                    print('responses_terminal='+json.dumps({'state':state,'done':done,'audited_status_code':final['status_code']}))
                    if state=='completed': self.assertEqual(final['status_code'],200)
                    else: self.assertIs(final.get('usage_complete'),False,'incomplete/failed semantic state must survive trailing DONE')

    async def test_responses_adapter_length_finish_is_incomplete(self):
        frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'length'}]}),
                self.frame({'choices':[],'usage':usage()}),b'data: [DONE]\n\n']
        public,final=await self.run_stream(frames,api_kind='responses',adapter=True)
        self.assertIn(b'response.incomplete',public)
        self.assertIs(final.get('usage_complete'),False,'adapter incomplete semantic status must survive trailing DONE')

    async def test_chat_error_envelope_keeps_usage_incomplete_after_done(self):
        for error in ({'error':{'message':'synthetic-stream-error'}},
                      {'type':'error','message':'synthetic-stream-error'}):
            with self.subTest(error_type=error.get('type','envelope')):
                frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'stop'}]}),
                        self.frame({'choices':[],'usage':usage()}),self.frame(error),b'data: [DONE]\n\n']
                public,final=await self.run_stream(frames)
                self.assertEqual(final['usage'],usage(),'error must not erase already collected counters')
                self.assertIs(final.get('usage_complete'),False,'error envelope must remain sticky after DONE')
                self.assertIn(b'synthetic-stream-error',public,'public error frame must remain visible')
                self.assertEqual(public.count(b'[DONE]'),1)

    async def test_responses_adapter_rejects_chat_error_before_synthetic_completion(self):
        for error in ({'error':{'message':'synthetic-stream-error'}},
                      {'type':'error','message':'synthetic-stream-error'}):
            with self.subTest(error_type=error.get('type','envelope')):
                frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'stop'}]}),
                        self.frame({'choices':[],'usage':usage()}),self.frame(error),b'data: [DONE]\n\n']
                public,final=await self.run_stream(frames,api_kind='responses',adapter=True,allow_protocol_error=True)
                self.assertEqual(final['status_code'],499,'adapter error must not produce a successful audit')
                self.assertEqual(final['usage'],usage(),'raw usage observed before error remains available for diagnosis')
                self.assertNotIn(b'response.completed',public,'adapter must not synthesize successful completion')
                self.assertNotIn(b'[DONE]',public,'adapter must not synthesize normal DONE after upstream error')

    async def test_responses_adapter_preserves_raw_missing_and_invalid_input(self):
        for value in ({'prompt_tokens_details':{'cached_tokens':0}},usage(100,75),usage(-1,0)):
            with self.subTest(value=value):
                frames=[self.frame({'choices':[{'delta':{'content':'synthetic'},'finish_reason':'stop'}]}),
                        self.frame({'choices':[],'usage':value}),b'data: [DONE]\n\n']
                public,final=await self.run_stream(frames,api_kind='responses',adapter=True)
                self.assertEqual(final['usage'],value,'adapter audit must see raw upstream usage without fabricated input0')
                self.assertEqual(usage_measurement(usage=final['usage']),usage_measurement(usage=value))

class RealAuditContract(unittest.IsolatedAsyncioTestCase):
    async def test_nonstream_responses_completion_state_reaches_real_trace_and_metrics(self):
        endpoint=Registry(WORK/'config/registry.yaml').by_id('ai-qwen38-27b')
        for state in ('completed','incomplete','failed','cancelled','in_progress','queued'):
            for complete_usage in (True,False):
                with self.subTest(response_state=state,complete_usage=complete_usage):
                    actual_usage=usage(100,75,'responses') if complete_usage else {'input_tokens':100}
                    payload=json.dumps({'id':'synthetic-response','status':state,'usage':actual_usage,'output':[]}).encode()
                    decision=api.RouteDecision(endpoint=endpoint,requested_model=endpoint.public_model,task='general',
                        prompt_tokens=999,output_reserve_tokens=16,reason='explicit_model',affinity='explicit',score=1)
                    decision.trace=DecisionTrace(request_id='synthetic-request',client_id='synthetic-client',key_id='synthetic-key',
                        protocol='responses',requested_model='synthetic-model',excerpt={},instance_id='synthetic-instance',
                        boot_id='synthetic-boot',settings_hash='synthetic-settings',registry_hash='synthetic-registry')
                    current=SimpleNamespace(instance_id='synthetic-instance',boot_id='synthetic-boot',
                        audit=SimpleNamespace(write=mock.Mock()),
                        policy=SimpleNamespace(mark_deployment_recent=mock.AsyncMock()),
                        clients=SimpleNamespace(record_usage=mock.AsyncMock()),
                        health=SimpleNamespace(status=mock.AsyncMock(return_value=SimpleNamespace(cache_generation='synthetic'))),
                        prefix_affinity=SimpleNamespace(invalidate_worker=mock.AsyncMock(),record_worker=mock.AsyncMock()))
                    with mock.patch.object(api,'_prefix_cache_delta',new=mock.AsyncMock(return_value=64)),mock.patch.object(api,'_save_request_trace',new=mock.AsyncMock()) as saved:
                        await api._audit(current,request_id='synthetic-request',client_id='synthetic-client',key_id='synthetic-key',
                            conversation_id=None,decision=decision,status_code=200,started_at=time.monotonic()-.01,response_payload=payload)
                    self.assertEqual(saved.await_count,1)
                    row=metrics(decision.trace.payload,[])
                    expected=('hit' if complete_usage else 'estimated') if state=='completed' else 'unknown'
                    self.assertEqual(row['cache_status'],expected)
                    if state!='completed':
                        self.assertIsNone(row['backend_cached_tokens'])
                        self.assertIsNone(row['estimated_cached_tokens'])
                    complete_events=[call.kwargs for call in current.audit.write.call_args_list if call.args[0]=='request_completed']
                    self.assertEqual(len(complete_events),1)
                    if state!='completed': self.assertEqual(complete_events[0]['backend_usage']['state'],'incomplete')

if __name__=='__main__': unittest.main(verbosity=2)
