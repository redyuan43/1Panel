import copy
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from starlette.requests import Request

from ai_router.api import _send_upstream
from ai_router.config import Registry, Settings
from ai_router.prompt_directives import (
    PromptDirectiveStore,
    prepare_prompt_directive_update,
    sanitize_prompt_directives,
)


ROOT = Path(__file__).resolve().parents[1]
ADDITIONS = {
    "tianshu": {"phrase": "按天枢协议处理", "endpoint_id": "zhipu-glm-5.3"},
    "liuguang": {"phrase": "按流光协议处理", "endpoint_id": "cloud-deepseek-v4-flash"},
}


def test_add_routes_preserves_existing_pins_and_matches_both_protocols(tmp_path):
    settings = Settings(ROOT / "config/defaults.yaml", tmp_path / "missing.yaml")
    current = settings.section("routing")["prompt_directives"]
    current["enabled"] = True
    proposed = copy.deepcopy(current)
    proposed["routes"].update(ADDITIONS)
    updated, changes = prepare_prompt_directive_update(current, proposed)
    assert updated["revision"] == current["revision"] + 1
    assert {change["directive_id"] for change in changes} == set(ADDITIONS)
    assert all(updated["routes"][key] == route for key, route in current["routes"].items())
    assert updated["retired_phrases"] == current["retired_phrases"]
    registry = Registry(ROOT / "config/registry.yaml")
    for key, route in ADDITIONS.items():
        assert registry.by_id(route["endpoint_id"]) is not None
        for protocol, field in [("chat", "messages"), ("responses", "input")]:
            result = sanitize_prompt_directives(
                {"model": "auto", field: [{"role": "user", "content": route["phrase"] + "\n检查代码"}]},
                protocol, updated,
            )
            assert result.directive.id == key
            assert result.directive.endpoint_id == route["endpoint_id"]
            assert result.body[field][0]["content"] == "检查代码"
    unchanged, changes = prepare_prompt_directive_update(updated, updated)
    assert unchanged == updated
    assert changes == []


def test_duplicate_new_phrase_rejected(tmp_path):
    current = Settings(ROOT / "config/defaults.yaml", tmp_path / "missing.yaml").section("routing")["prompt_directives"]
    proposed = copy.deepcopy(current)
    proposed["routes"]["tianshu"] = dict(ADDITIONS["tianshu"], phrase=current["routes"]["beichen"]["phrase"])
    with pytest.raises(ValueError, match="unique"):
        prepare_prompt_directive_update(current, proposed)


def test_randomize_seven_directives(tmp_path):
    store = PromptDirectiveStore(tmp_path / "phrases.sqlite3")
    ids = ["rilun", "beichen", "qinglan", "yuheng", "tianshu", "liuguang", "reset"]
    result = store.suggest(ids)
    assert set(result) == set(ids)
    assert len(set(result.values())) == 7


@pytest.mark.parametrize("endpoint_id", ["zhipu-glm-5.3", "cloud-deepseek-v4-flash"])
@pytest.mark.parametrize("protocol", ["chat", "responses"])
def test_reasoning_effort_reaches_litellm_without_being_dropped(endpoint_id, protocol):
    endpoint = Registry(ROOT / "config/registry.yaml").by_id(endpoint_id)
    # GLM uses Responses-to-Chat; also exercise that conversion for this transport test.
    decision = SimpleNamespace(endpoint=endpoint, upstream_api_base=None, native_or_adapter="adapter")
    identity = SimpleNamespace(inject=lambda body, kind: copy.deepcopy(body))
    body = {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "reasoning_effort": "low"}
    if protocol == "responses":
        body = {"model": "auto", "input": "hello", "reasoning": {"effort": "low"}}

    async def scenario():
        async def capture(request):
            payload = json.loads(request.content)
            assert "reasoning_effort" not in payload
            assert payload["extra_body"]["reasoning_effort"] == "low"
            assert payload["model"] == endpoint_id
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            runtime = SimpleNamespace(internal_client=client, internal_base_url="http://litellm", internal_api_key="test")
            request = Request({"type": "http", "headers": []})
            response = await _send_upstream(runtime, request, body, api_kind=protocol, decision=decision, identity=identity)
            await response.aclose()

    asyncio.run(scenario())
