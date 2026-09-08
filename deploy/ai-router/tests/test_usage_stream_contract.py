"""Synthetic CPU contract; run only after implementation-ready notification."""
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
from starlette.requests import Request

_PARENT=Path(__file__).resolve().parents[1]
_DEFAULT_WORK=_PARENT if (_PARENT/'ai_router').is_dir() else _PARENT/'worktree/deploy/ai-router'
WORK=Path(os.environ.get('AI_CACHE_USAGE_WORK',_DEFAULT_WORK))
sys.path.insert(0,str(WORK))
from ai_router import api
from ai_router.identity import IdentityProfile
from ai_router.config import Registry

class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts): self.parts=parts; self.closed=False
    async def __aiter__(self):
        for part in self.parts: yield part
    async def aclose(self): self.closed=True

class StreamContract(unittest.IsolatedAsyncioTestCase):
    def decision(self, target='ai-qwen38-27b'):
        endpoint=Registry(WORK/'config/registry.yaml').by_id(target)
        return api.RouteDecision(endpoint=endpoint,requested_model=endpoint.public_model,
            task='general',prompt_tokens=999,output_reserve_tokens=16,
            reason='explicit_model',affinity='explicit',score=1)

    async def sent(self, body, target='ai-qwen38-27b', api_kind='chat', adapter=False):
        recorded=[]
        async def handle(request):
            recorded.append(json.loads(request.content))
            return httpx.Response(200,content=b'data: [DONE]\n\n')
        decision=self.decision(target)
        decision.upstream_api_base='http://synthetic.invalid/v1'
        decision.native_or_adapter='adapter' if adapter else 'native'
        request=Request({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[]})
        request.state.server_request_id='synthetic-request'
        profile=IdentityProfile.from_settings({'enabled':False})
        original=copy.deepcopy(body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            current=SimpleNamespace(internal_client=client,internal_api_key='synthetic',training=None)
            response=await api._send_upstream(current,request,body,api_kind=api_kind,
                decision=decision,identity=profile)
            await response.aclose()
        self.assertEqual(body,original,'internal usage injection must not mutate client body')
        return recorded[0]

    async def test_target_chat_forces_internal_usage_preserves_options_and_body(self):
        for original_options in (None,{'include_usage':False,'continuous_usage_stats':False},
                                 {'include_usage':True,'continuous_usage_stats':False}):
            with self.subTest(options=original_options):
                body={'model':'synthetic','stream':True,'messages':[{'role':'user','content':'synthetic'}]}
                if original_options is not None: body['stream_options']=original_options
                actual=await self.sent(body)
                self.assertIs(actual.get('stream_options',{}).get('include_usage'),True)
                if original_options and 'continuous_usage_stats' in original_options:
                    self.assertIs(actual['stream_options']['continuous_usage_stats'],False)

    async def test_target_responses_adapter_obtains_chat_usage(self):
        actual=await self.sent({'model':'synthetic','stream':True,'input':'synthetic'},api_kind='responses',adapter=True)
        self.assertIs(actual.get('stream_options',{}).get('include_usage'),True)

    async def test_nonstream_does_not_gain_stream_options(self):
        actual=await self.sent({'model':'synthetic','messages':[{'role':'user','content':'synthetic'}]})
        self.assertNotIn('stream_options',actual)

    async def streamed(self, parts, include_usage):
        decision=self.decision()
        decision.native_or_adapter='native'
        decision.trace=None
        finalizer=SimpleNamespace(begin_stream=mock.AsyncMock(),finish_stream=mock.Mock())
        stream=Chunks(parts)
        body={'stream':True,'messages':[{'role':'user','content':'synthetic'}]}
        if include_usage is not None: body['stream_options']={'include_usage':include_usage}
        audit_mock=mock.AsyncMock()
        async def handle(request): return httpx.Response(200,stream=stream)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        current=SimpleNamespace(compactor=None,conversations=None,training=None,internal_client=client,internal_api_key='synthetic')
        request=Request({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[]})
        request.state.server_request_id='synthetic-request'
        decision.upstream_api_base='http://synthetic.invalid/v1'
        upstream=await api._send_upstream(current,request,body,api_kind='chat',decision=decision,identity=IdentityProfile.from_settings({'enabled':False}))
        with mock.patch.object(api,'persist_history',new=mock.AsyncMock()),mock.patch.object(api,'_audit',new=audit_mock),mock.patch.object(api,'_run_stream_resource_finalizer',new=mock.AsyncMock()):
            chunks=[part async for part in api._stream_response(current,upstream,resource_finalizer=finalizer,
                client_id='synthetic-client',key_id='synthetic-key',request_id='synthetic-request',
                conversation_id=None,decision=decision,state=None,body=body,api_kind='chat',
                training_token=None,started_at=time.monotonic()-.01,cache_snapshot=None,
                identity=IdentityProfile.from_settings({'enabled':False}),identifiers=())]
        await client.aclose()
        self.assertTrue(stream.closed)
        self.assertEqual(audit_mock.await_count,1)
        return b''.join(chunks),audit_mock.await_args.kwargs

    async def test_private_usage_survives_frame_filtering_chunk_matrix(self):
        usage={'prompt_tokens':100,'completion_tokens':1,'total_tokens':101,'prompt_tokens_details':{'cached_tokens':75}}
        values=[{'choices':[{'index':0,'delta':{'content':'合成'},'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
            {'choices':[],'usage':usage}]
        for separator in (b'\n',b'\r\n'):
            frames=[b'data: '+json.dumps(x,ensure_ascii=False).encode()+separator*2 for x in values]
            frames.append(b'data: [DONE]'+separator*2)
            blob=b''.join(frames)
            for shape,parts in [('coalesced',[blob]),('per_frame',frames),('single_byte',[bytes([v]) for v in blob])]:
                for include in (None,False,True):
                    with self.subTest(separator=repr(separator),shape=shape,include=include):
                        public,final=await self.streamed(parts,include)
                        events=[json.loads(line[5:].strip()) for line in public.decode().splitlines()
                                if line.startswith('data:') and line[5:].strip()!='[DONE]']
                        self.assertEqual(final['usage'],usage)
                        self.assertEqual(final['status_code'],200)
                        self.assertEqual(public.count(b'[DONE]'),1)
                        self.assertEqual(''.join(v.get('choices',[{}])[0].get('delta',{}).get('content','') for v in events if v.get('choices')),'合成')
                        usage_events=[v for v in events if v.get('choices')==[] and isinstance(v.get('usage'),dict)]
                        self.assertEqual(len(usage_events),1 if include is True else 0)

if __name__=='__main__': unittest.main(verbosity=2)
