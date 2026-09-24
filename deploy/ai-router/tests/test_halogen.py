from copy import deepcopy
from types import SimpleNamespace
import pytest
from ai_router.halogen import health_status
import asyncio
from unittest.mock import AsyncMock

ENDPOINT = SimpleNamespace(id='halogen', provider_model='halogen-qwen3.8-flash-next', safe_context_tokens=131072)
HEALTH = {'status':'ok','model':ENDPOINT.provider_model,'version':{'match':True,'api':'0.12.3'},
          'engine':{'responds':True},'in_flight':0,'queued':0,'context':262144,'slots':4,'busy':False}

def test_idle_does_not_claim_more_context_or_cache_generation():
    value=health_status(ENDPOINT,HEALTH,1)
    assert value.healthy and value.load_headroom==1
    assert value.eligible_context_tokens==131072 and not value.cache_generation

@pytest.mark.parametrize('field,value',[('in_flight',1),('queued',1),('busy',True)])
def test_occupied_is_capacity_not_health_failure(field,value):
    status=health_status(ENDPOINT,{**HEALTH,field:value},1)
    assert status.healthy and status.load_headroom==0

@pytest.mark.parametrize('field,value',[('in_flight',True),('queued',-1),('context',0),('busy',None),('model','other'),('version',{'match':False})])
def test_invalid_or_wrong_backend_fails_closed(field,value):
    with pytest.raises(ValueError): health_status(ENDPOINT,{**HEALTH,field:value},1)

@pytest.mark.parametrize('idle',[True,False])
def test_dispatch_rechecks_external_occupancy_without_pool_membership(idle):
    from ai_router.api import _wait_for_selected_deployment
    endpoint=SimpleNamespace(id='halogen',backend_type='halogen')
    status=SimpleNamespace(healthy=True,load_headroom=int(idle),detail={})
    health=AsyncMock(return_value=status)
    current=SimpleNamespace(policy=SimpleNamespace(local_pool=None),health=SimpleNamespace(status=health))
    result=asyncio.run(_wait_for_selected_deployment(current,SimpleNamespace(endpoint=endpoint),endpoint.id,timeout_seconds=0))
    assert result is idle
    health.assert_awaited_once_with(endpoint,force_refresh=True)

def test_two_router_instances_share_single_halogen_capacity():
    from ai_router.scheduler import Scheduler
    from ai_router.store import InMemoryStateStore
    async def check():
        store=InMemoryStateStore()
        first,second=Scheduler(store),Scheduler(store)
        a,b=await first.begin_request(None),await second.begin_request(None)
        try:
            assert await first.try_acquire_deployment_candidates(a,('amd-halogen-qwen38-256k',),capacity=1)
            assert await second.try_acquire_deployment_candidates(b,('amd-halogen-qwen38-256k',),capacity=1) is None
            await a.release()
            assert await second.try_acquire_deployment_candidates(b,('amd-halogen-qwen38-256k',),capacity=1)
        finally:
            await a.release(); await b.release()
    asyncio.run(check())

@pytest.mark.parametrize('protocol',['chat','responses'])
def test_auto_can_select_new_identity_without_changing_old_bindings(tmp_path,protocol):
    from dataclasses import replace
    from pathlib import Path
    from ai_router.config import Registry, endpoint_from_dict, load_yaml
    from ai_router.policy import RoutingPolicy
    from ai_router.types import Evaluation, RequestCapabilities
    from tests.test_core import FakeHealth, healthy, settings
    root=Path(__file__).resolve().parents[1]
    endpoint=replace(endpoint_from_dict(load_yaml(root/'config/halogen-endpoint.yaml')),enabled=True,auto_candidate=True)
    original=Registry(root/'config/registry.yaml')
    old=original.by_id('amd-qwen38-rocmfpx-128k')
    registry=original.with_endpoints([endpoint])
    policy=RoutingPolicy(registry,settings(tmp_path),FakeHealth({endpoint.id:healthy(endpoint.id,context=262144)}))
    choice=asyncio.run(policy.choose(requested_model='auto',evaluation=Evaluation('general',None,1.0,'test'),
        prompt_tokens=100,output_reserve_tokens=100,modalities={'text'},has_tools=False,
        required_capabilities=RequestCapabilities(protocol=protocol),conversation=None))
    assert choice.endpoint.id==endpoint.id and old.id!=endpoint.id
    assert original.by_id(old.id)==old

def test_release_fragment_matches_canonical_disabled_registration():
    from pathlib import Path
    from ai_router.config import Registry,endpoint_from_dict,load_yaml
    root=Path(__file__).resolve().parents[1]
    fragment=endpoint_from_dict(load_yaml(root/'config/halogen-endpoint.yaml'))
    assert Registry(root/'config/registry.yaml').by_id(fragment.id)==fragment
    assert not fragment.enabled and not fragment.auto_candidate
