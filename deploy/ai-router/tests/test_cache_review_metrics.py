import asyncio
import importlib.util
import json
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ai_router import api
from ai_router.cache_audit import metrics
from ai_router.identity import IdentityProfile

class ReviewReproductions(unittest.TestCase):
    def test_unknown_actual_input_does_not_claim_net_cache_reuse(self):
        actual_input, output = api._usage_totals(b'{"choices":[]}', None, prompt_tokens_fallback=1000)
        trace={'request_id':'synthetic-request','started_at':1,'completed_at':2,'status':'succeeded',
               'attempts':[{'number':1,'steps':[{'node_id':'upstream_request','evidence':{'input_tokens':actual_input,'output_tokens':output,'cached_prompt_tokens':None,'cache_measurement_source':'unavailable'}}]}]}
        operation={'operation_id':'synthetic-operation','request_id':'synthetic-request','attempt':1,'kind':'foreground','terminal':True,
                   'cache':{'fixed_tokens':800,'prime_tokens':0,'event':'hot'},'timings':{'prompt_n':100}}
        result=metrics(trace,[operation])
        print('unknown_input_observation='+json.dumps({k:result[k] for k in ['input_tokens','cached_tokens','fixed_reuse_ratio','net_cache_ratio','measurement']}))
        self.assertIsNone(result['net_cache_ratio'], 'no measured actual input or cache_n exists; Router estimate must not prove cache reuse')

    def test_sanitizer_finish_output_receives_first_output_timing(self):
        async def run():
            current=SimpleNamespace(compactor=None,conversations=None,training=None)
            trace=SimpleNamespace(payload={})
            decision=SimpleNamespace(trace=trace,attempts=1,native_or_adapter='native',endpoint=SimpleNamespace(public_model='synthetic-model'))
            finalizer=SimpleNamespace(begin_stream=mock.AsyncMock(),finish_stream=mock.Mock())
            # Q is a partial prefix of the hidden identifier Qwen. The real
            # sanitizer buffers it until EOF, then emits a complete public frame.
            upstream=httpx.Response(200,content=b'data: {"choices":[{"index":0,"delta":{"content":"Q"}}]}\n\n')
            profile=IdentityProfile.from_settings({'enabled':True,'public_model_id':'synthetic-public'})
            with mock.patch.object(api,'persist_history',new=mock.AsyncMock()),mock.patch.object(api,'_audit',new=mock.AsyncMock()),mock.patch.object(api,'_run_stream_resource_finalizer',new=mock.AsyncMock()):
                parts=[chunk async for chunk in api._stream_response(current,upstream,resource_finalizer=finalizer,client_id='synthetic-client',key_id='synthetic-key',request_id='synthetic-request',conversation_id=None,decision=decision,state=None,body={},api_kind='chat',training_token=None,started_at=time.monotonic()-.01,cache_snapshot=None,identity=profile,identifiers=('Qwen',))]
            output=b''.join(parts)
            self.assertIn(b'"content":"Q"',output)
            print('finish_only_observation='+json.dumps({'public_output_emitted':True,'observation':trace.payload.get('observation',{})}))
            self.assertIn('ttft_ms',trace.payload.get('observation',{}))
            self.assertIn('first_text_ms',trace.payload.get('observation',{}))
        asyncio.run(run())

if __name__=='__main__': unittest.main(verbosity=2)
