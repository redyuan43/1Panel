import copy
import time
from dataclasses import replace
import pytest
from app.multifleet import MultiFleetClient, Node
from test_multifleet import Client, module

@pytest.fixture
def edge(tmp_path):
    c=MultiFleetClient([Node('edge','http://edge.ts.net:19190','/private/edge',1,30,True,())],'edge',tmp_path/'routes.sqlite3',client_factory=Client)
    x=c.clients['edge']
    x.state.update(resources_ok=False,model_lifecycle={'state':'idle','queueable':True,'execution_id':None},preparation_guard={'eligible':True,'reasons':[],'observed_at':time.time(),'profile_ids':['profile'],'requires_model_switch':True})
    x.state['recipe_capacity']={'profile':{'recipe_version':'v1','available_slots':0,'eligible_lanes':[],'reasons':['edge_model_idle']}}
    x.catalog={'enabled':True,'multimodal_profiles':[{'profile_id':'profile','version':'v1','qualified':True,'acceptance_tasks':[]}]}
    x.capacity=lambda *,fresh=False:copy.deepcopy(x.state)
    x.recipe_catalog=lambda:copy.deepcopy(x.catalog)
    return c,x

def admit(c,**kw):
    args=dict(node=c.nodes['edge'],target='edge',project_id='project',capability='profile',version='v1');args.update(kw)
    return c.edge_preparation_admission(**args)

def test_reserve_once_without_advertising_gpu(edge):
    c,x=edge;assert admit(c)['preparation_only']
    fake=module('project','edge');fake.project['execution_profile']={'profile_id':'profile','version':'v1'};fake.recipe_scope=lambda p,s:False
    with c.execution_scope(fake,'project','preview'):
        execution=fake.project['stages']['preview']['execution_id']
        assert c.routes.get(execution)['node_id']=='edge'
        assert c.capacity()['recipe_capacity']['profile']['available_slots']==0
        assert c.routes.reserve('second',[(c.nodes['edge'],admit(c))],'profile',None) is None
        c._request('POST','/prompt',{'extra_data':{'h3':{'execution_id':execution}}})
    assert len(x.calls)==1

@pytest.mark.parametrize('key,value',[('target','auto'),('target','ivan-u24'),('capability','other'),('version','old')])
def test_wrong_target_or_profile(edge,key,value):
    assert admit(edge[0],**{key:value}) is None

@pytest.mark.parametrize('changes',[{'enabled':False},{'max_parallel':2},{'id':'ivan-u24'}])
def test_single_enabled_edge_only(edge,changes):
    c,_=edge;assert admit(c,node=replace(c.nodes['edge'],**changes)) is None

@pytest.mark.parametrize('key,value',[('active',1),('active',False),('queued',1),('counts_complete',False),('exclusive_window',True),('available',False),('sampled_at',0)])
def test_unknown_stale_active_or_leased(edge,key,value):
    c,x=edge;x.state[key]=value;assert admit(c) is None

@pytest.mark.parametrize('change',[{'state':'restoring_qwen'},{'state':'blocked'},{'state':'ready'},{'queueable':False},{'execution_id':'other'}])
def test_lifecycle_not_idle(edge,change):
    c,x=edge;x.state['model_lifecycle'].update(change);assert admit(c) is None

@pytest.mark.parametrize('change',[{'eligible':False},{'reasons':['disk_budget']},{'reasons':['temperature']},{'reasons':['swap_growth']},{'reasons':['kernel_alerts']},{'reasons':['unknown']},{'observed_at':0},{'profile_ids':[]},{'requires_model_switch':False}])
def test_nonmemory_or_unknown_fault(edge,change):
    c,x=edge;x.state['preparation_guard'].update(change);assert admit(c) is None

def test_acceptance_exact_project_and_missing_guard(edge):
    c,x=edge;p=x.catalog['multimodal_profiles'][0];p['qualified']=False;p['acceptance_tasks']=['other'];assert admit(c) is None
    p['acceptance_tasks']=['project'];assert admit(c)
    x.state.pop('preparation_guard');assert admit(c) is None

def test_capacity_shows_restore_without_available_slot(edge):
    c,x=edge;x.state['model_lifecycle']={'state':'restoring_qwen','queueable':False};v=c.capacity()
    assert v['nodes'][0]['model_lifecycle']['state']=='restoring_qwen'
    assert v['recipe_capacity']['profile']['available_slots']==0

def test_old_idle_transition_with_fresh_guard_is_valid(edge):
    c,x=edge;x.state['model_lifecycle'].update(updated_at=1,transition_started_at=1)
    assert admit(c)


def test_fleet_fixture_contract_matches_studio(edge):
    import json
    from pathlib import Path
    c,x=edge
    root=Path(__file__).parent/'edge_preparation_fixtures'
    fixture=json.loads((root/'preparation-fixture-positive.json').read_text())
    profile=fixture['preparation_guard']['profile_ids'][0]
    version=fixture['recipe_capacity'][profile]['recipe_version']
    x.state.update(model_lifecycle=fixture['model_lifecycle'],preparation_guard=fixture['preparation_guard'],recipe_capacity=fixture['recipe_capacity'])
    x.state['preparation_guard']['observed_at']=time.time()
    x.catalog['multimodal_profiles']=[{'profile_id':profile,'version':version,'qualified':True}]
    assert admit(c,capability=profile,version=version)
    x.state['preparation_guard']=json.loads((root/'preparation-fixture-negative.json').read_text())['preparation_guard']
    x.state['preparation_guard']['observed_at']=time.time()
    assert admit(c,capability=profile,version=version) is None


def test_queued_edge_details_report_switching():
    from app.fleet_progress import execution_detail
    result=execution_detail({'status':'queued','node_id':'edge','admission_reason':'admission_waiting:edge_model_stopping_qwen'}, {'phase':'resource_waiting'})
    assert '暂停 Qwen / 准备 H3' in result


def test_edge_cannot_bypass_prepare_guard_via_old_acceptance(edge):
    c,x=edge;x.state['resources_ok']=True
    x.state['recipe_capacity']['profile']['reasons']=['release_validation_gate']
    x.catalog['multimodal_profiles'][0].update(qualified=False,acceptance_tasks=['project'])
    x.state['model_lifecycle']['state']='restoring_qwen'
    assert c.acceptance_admission(c.nodes['edge'],'edge','project','profile','v1') is None
    assert admit(c) is None
