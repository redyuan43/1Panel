"""Independent CPU contracts for public WorkBuddy prefix normalization."""
from __future__ import annotations
import copy
import pytest
from ai_router.prefix_affinity import PrefixAffinityRepository
from ai_router.protocol import move_workbuddy_dynamic_context, stabilize_workbuddy_tools
START = "<workbuddy_dynamic_context>"
END = "</workbuddy_dynamic_context>"
def _request(model="siyuan/auto", dynamic="memory", content="question"):
    return {"model": model, "messages": [{"role": "system", "content": f"public stable rules\n\n{START}\n{dynamic}\n{END}\n"}, {"role": "user", "content": content}], "tools": [
        {"type": "function", "function": {"name": "Read", "description": "read", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "Agent", "description": "agents: changing", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "Skill", "description": "skills: changing", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "ToolSearch", "description": "tools: changing", "parameters": {"type": "object"}}},
    ]}
@pytest.mark.parametrize("model", ["siyuan/auto", "auto", "qwen36-shared"])
def test_public_workbuddy_aliases_move_dynamic_context(model):
    moved = move_workbuddy_dynamic_context(_request(model), "chat", client_id="workbuddy-public")
    assert moved.moved and moved.mode == "tagged"
    assert moved.body["model"] == model
    assert moved.body["messages"][0]["content"] == "public stable rules"
    assert moved.body["messages"][1]["content"].endswith("\n\nquestion")
def test_public_prefix_reused_across_dynamic_sessions():
    first = move_workbuddy_dynamic_context(_request(dynamic="memory A", content="one"), "chat", client_id="workbuddy-public")
    second = move_workbuddy_dynamic_context(_request(dynamic="memory B", content="two"), "chat", client_id="workbuddy-public")
    assert first.moved and second.moved
    assert PrefixAffinityRepository.reusable_prefix_body(first.body, "chat") == PrefixAffinityRepository.reusable_prefix_body(second.body, "chat")
    assert first.stable_prefix_sha256 == second.stable_prefix_sha256
def test_public_continuation_preserves_tool_ids_parameters_and_images():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}
    body = _request(content=[{"type": "text", "text": "describe"}, image])
    body["messages"].extend([{"role": "assistant", "content": None, "tool_calls": [{"id": "call-42", "type": "function", "function": {"name": "Read", "arguments": '{"path":"a"}'}}]}, {"role": "tool", "tool_call_id": "call-42", "content": "result"}])
    original = copy.deepcopy(body)
    moved = move_workbuddy_dynamic_context(body, "chat", client_id="workbuddy-public")
    assert moved.moved and moved.target_user_index == 1
    assert moved.body["messages"][1]["content"][1:] == original["messages"][1]["content"]
    assert moved.body["messages"][2:] == original["messages"][2:]
    assert moved.body["messages"][2]["tool_calls"][0]["id"] == "call-42"
    assert moved.body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"path":"a"}'
def test_public_transform_idempotent_after_continuation():
    first = move_workbuddy_dynamic_context(_request(), "chat", client_id="workbuddy-public")
    first.body["messages"].append({"role": "assistant", "content": "continuing"})
    again = move_workbuddy_dynamic_context(first.body, "chat", client_id="workbuddy-public")
    assert not again.moved and again.body == first.body
    assert again.stable_prefix_sha256 == first.stable_prefix_sha256
def test_tool_stabilizer_canonical_order_and_recursive_schema_key_sorting():
    body = _request()
    body["tools"] = list(reversed(body["tools"]))
    next(tool for tool in body["tools"] if tool["function"]["name"] == "Agent")["function"]["parameters"] = {"properties": {"z": {"type": "string"}, "a": {"type": "string"}}, "required": ["z", "a"]}
    stable, report = stabilize_workbuddy_tools(body, "chat", client_id="workbuddy-public")
    assert [t["function"]["name"] for t in stable["tools"]] == ["Agent", "Read", "Skill", "ToolSearch"]
    agent = next(tool for tool in stable["tools"] if tool["function"]["name"] == "Agent")
    assert list(agent["function"]["parameters"]["properties"]) == ["a", "z"]
    assert report["changed"] is True
def test_tool_addition_and_removal_change_prefix_identity_and_preserve_definitions():
    base = move_workbuddy_dynamic_context(_request(), "chat", client_id="workbuddy-public")
    added_body = _request(); added_body["tools"].append({"type": "function", "function": {"name": "NewTool", "description": "new", "parameters": {"type": "object"}}})
    added = move_workbuddy_dynamic_context(added_body, "chat", client_id="workbuddy-public")
    removed_body = _request(); removed_body["tools"] = removed_body["tools"][:-1]
    removed = move_workbuddy_dynamic_context(removed_body, "chat", client_id="workbuddy-public")
    assert base.moved and added.moved and removed.moved
    assert len({base.stable_prefix_sha256, added.stable_prefix_sha256, removed.stable_prefix_sha256}) == 3
    assert added.body["tools"][-1]["function"]["name"] == "NewTool"
    assert removed.body["tools"][-1]["function"]["name"] == "Skill"
@pytest.mark.parametrize("api_kind,client_id", [("responses", "workbuddy-public"), ("chat", "other-client")])
def test_public_scope_guard_rejects_non_chat_or_unapproved_client(api_kind, client_id):
    body = _request(); original = copy.deepcopy(body)
    moved = move_workbuddy_dynamic_context(body, api_kind, client_id=client_id)
    assert not moved.moved and moved.body == original
