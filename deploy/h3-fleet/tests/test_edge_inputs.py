import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.edge_inputs import stage_input

NAME = 'asset_' + 'a' * 32 + '.png'


def test_offline_staging_replay_and_conflict(tmp_path):
    first = stage_input(tmp_path, NAME, b'image')
    assert stage_input(tmp_path, NAME, b'image') == first
    with pytest.raises(ValueError, match='conflict'):
        stage_input(tmp_path, NAME, b'different')
    assert (tmp_path / NAME).read_bytes() == b'image'


def test_symlink_and_path_escape_rejected(tmp_path):
    outside = tmp_path / 'other';outside.write_bytes(b'image')
    (tmp_path / NAME).symlink_to(outside)
    with pytest.raises(OSError): stage_input(tmp_path, NAME, b'image')
    with pytest.raises(ValueError): stage_input(tmp_path, '../x.png', b'image')


def test_replicate_with_worker_offline_does_not_make_http_request(tmp_path, monkeypatch):
    from test_main import load_module
    main = load_module(tmp_path)
    http = SimpleNamespace(post=AsyncMock(side_effect=AssertionError('worker must stay stopped')))
    lane = SimpleNamespace(id='fast', enabled=True)
    fake = SimpleNamespace(client=http, lanes=[lane], recipes=SimpleNamespace(backends={'edge':{
        'lane_id':'fast','unified_memory':{'kind':'gb10'},'input_root':str(tmp_path)}}))
    monkeypatch.setattr(main,'fleet',fake)
    assert asyncio.run(main.replicate_input(NAME,b'image','image/png'))[0]['ok']
    http.post.assert_not_called()
