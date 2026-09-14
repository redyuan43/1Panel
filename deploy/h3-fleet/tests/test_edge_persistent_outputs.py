import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_owned_output_streams_after_worker_stops_and_rejects_other_paths(tmp_path, monkeypatch):
    from test_main import load_module
    main = load_module(tmp_path)
    (tmp_path / 'result.mp4').write_bytes(b'0123456789')
    job={'prompt_id':'a'*32,'status':'completed','backend_json':json.dumps({'id':'edge'})}
    store=SimpleNamespace(find_output=lambda name, folder, kind: job if name=='result.mp4' and folder=='' and kind=='output' else None,
                          list=lambda **kw: [])
    fake=SimpleNamespace(store=store,recipes=SimpleNamespace(backends={'edge':{'unified_memory':True,'output_root':str(tmp_path)}}),
                         client=SimpleNamespace(get=AsyncMock(side_effect=AssertionError('worker is stopped'))))
    monkeypatch.setattr(main,'fleet',fake)
    app=FastAPI();app.add_api_route('/view',main.view,methods=['GET'])
    with TestClient(app) as client:
        response=client.get('/view?filename=result.mp4',headers={'Range':'bytes=2-5'})
        assert response.status_code==206 and response.content==b'2345'
        assert client.get('/view?filename=../../etc/passwd').status_code==404
        job['status']='error'
        assert client.get('/view?filename=result.mp4').status_code==409


def test_persisted_history_survives_worker_stop(tmp_path, monkeypatch):
    from test_main import load_module
    main = load_module(tmp_path)
    job={'prompt_id':'a'*32,'upstream_prompt_id':'upstream','status':'completed'}
    directory=tmp_path/'evidence'/job['prompt_id'];directory.mkdir(parents=True)
    (directory/'terminal-runtime-receipt.json').write_text(json.dumps({'job':job,'history':{'upstream':{'status':{'status_str':'success'},'outputs':{}}}}))
    fake=SimpleNamespace(store=SimpleNamespace(get=lambda _:job),refresh_job=AsyncMock(return_value=job),
        backend_lifecycle=SimpleNamespace(edge=SimpleNamespace(path=tmp_path/'state/lifecycle.json')),
        client=SimpleNamespace(get=AsyncMock(side_effect=AssertionError('worker is stopped'))))
    monkeypatch.setattr(main,'fleet',fake)
    app=FastAPI();app.add_api_route('/history/{prompt_id}',main.history,methods=['GET'])
    with TestClient(app) as client:
        response=client.get('/history/'+job['prompt_id'])
        assert response.status_code==200 and job['prompt_id'] in response.json()
