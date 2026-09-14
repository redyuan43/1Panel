import json

import pytest

from scripts.prepare_multinode_release import prepare


def trees(tmp_path):
    roots = [tmp_path / name for name in ("work", "base", "live")]
    for root in roots:
        (root / "app").mkdir(parents=True)
    return roots


def test_candidate_preserves_live_features_and_only_applies_our_delta(tmp_path):
    work, base, live = trees(tmp_path)
    original = "alpha\n" + "unchanged\n" * 15 + "omega\n"
    (base / "app/main.py").write_text(original)
    (work / "app/main.py").write_text(original.replace("alpha", "our_change"))
    (live / "app/main.py").write_text(original.replace("omega", "live_feature"))
    (live / "app/main.py").chmod(0o444)
    (live / "app/firstframe.py").write_text("preserve = True\n")
    (live / "private.env").write_text("SECRET=do-not-copy")
    output = tmp_path / "candidate"
    result = prepare(work, base, live, output)
    assert "our_change" in (output / "app/main.py").read_text()
    assert "live_feature" in (output / "app/main.py").read_text()
    assert (output / "app/firstframe.py").exists()
    assert not (output / "private.env").exists()
    assert len(result["changes"]) == 1
    assert not json.loads((output / "multinode-manifest.json").read_text())["deployed"]


def test_conflict_stops_candidate_instead_of_overwriting_live(tmp_path):
    work, base, live = trees(tmp_path)
    for root, content in ((work, "ours"), (base, "base"), (live, "live")):
        (root / "app/main.py").write_text(content + "\n")
    with pytest.raises(ValueError, match="requires review"):
        prepare(work, base, live, tmp_path / "candidate")
    assert (live / "app/main.py").read_text() == "live\n"


def test_connector_adaptation_preserves_edge_and_generates_runtime_schema(tmp_path):
    from pathlib import Path
    from scripts.prepare_multinode_release import adapt_live_connector
    root=Path(__file__).resolve().parents[2]
    (tmp_path/'app').mkdir();(tmp_path/'frontend').mkdir()
    source=(root/'h3-mcp/studio/connector_api.py').read_bytes()
    (tmp_path/'app/connector_api.py').write_bytes(source)
    (tmp_path/'frontend/app.js').write_text('target_node: selectedTargetNode(stageId)\n')
    (tmp_path/'app/connector_schema.json').write_text('[]')
    adapt_live_connector(tmp_path)
    schema=json.loads((tmp_path/'app/connector_schema.json').read_text())
    tool=next(x for x in schema if x['name']=='h3_start_preview')
    assert tool['inputSchema']['properties']['target_node']['enum']==['auto','ivan','ivan-u24','edge']
    assert (tmp_path/'app/connector_api.py').read_bytes()==source
    assert adapt_live_connector(tmp_path)==[]


def test_connector_adaptation_upgrades_old_enum_in_runtime_too(tmp_path):
    from pathlib import Path
    from scripts.prepare_multinode_release import adapt_live_connector
    root=Path(__file__).resolve().parents[2]
    (tmp_path/'app').mkdir();(tmp_path/'frontend').mkdir()
    source=(root/'h3-mcp/studio/connector_api.py').read_text().replace('["auto", "ivan", "ivan-u24", "edge"]','["auto", "ivan", "ivan-u24"]')
    (tmp_path/'app/connector_api.py').write_text(source)
    (tmp_path/'frontend/app.js').write_text('target_node: selectedTargetNode(stageId)\n')
    adapt_live_connector(tmp_path)
    assert '["auto", "ivan", "ivan-u24", "edge"]' in (tmp_path/'app/connector_api.py').read_text()
    tool=next(x for x in json.loads((tmp_path/'app/connector_schema.json').read_text()) if x['name']=='h3_start_preview')
    assert 'edge' in tool['inputSchema']['properties']['target_node']['enum']
