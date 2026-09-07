import copy

import pytest

from ai_router.prefix_affinity import PrefixAffinityRepository
from ai_router.protocol import move_workbuddy_dynamic_context


CLIENT_ID = "workbuddy-qwen36-shared"
START = "<workbuddy_dynamic_context>"
END = "</workbuddy_dynamic_context>"


def _body(dynamic: str, user_content):
    return {
        "model": "siyuan/qwen36-shared",
        "messages": [
            {
                "role": "system",
                "content": (
                    "stable tools and instructions\n\n"
                    f"{START}\n{dynamic}\n{END}\n"
                ),
            },
            {"role": "user", "content": user_content},
        ],
    }


def test_moves_dynamic_context_before_a_text_question() -> None:
    moved = move_workbuddy_dynamic_context(
        _body("memory=session-a", "question"),
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is True
    assert moved.body["messages"][0]["content"] == (
        "stable tools and instructions"
    )
    assert moved.body["messages"][1]["content"] == (
        f"{START}\nmemory=session-a\n{END}\n\nquestion"
    )
    assert moved.moved_chars == len(
        f"{START}\nmemory=session-a\n{END}"
    )
    assert moved.mode == "tagged"


def test_moves_dynamic_context_before_multimodal_user_content() -> None:
    image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,redacted"},
    }
    moved = move_workbuddy_dynamic_context(
        _body(
            "memory=session-b",
            [{"type": "text", "text": "describe"}, image],
        ),
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is True
    assert moved.body["messages"][1]["content"] == [
        {
            "type": "text",
            "text": f"{START}\nmemory=session-b\n{END}",
        },
        {"type": "text", "text": "describe"},
        image,
    ]


def test_unapproved_client_is_unchanged() -> None:
    body = _body("memory=session-a", "question")
    moved = move_workbuddy_dynamic_context(
        body,
        "chat",
        client_id="other-client",
    )

    assert moved.moved is False
    assert moved.body == body


def test_malformed_or_non_tail_marker_is_unchanged() -> None:
    body = _body("memory=session-a", "question")
    body["messages"][0]["content"] += "\ncontent after marker"
    moved = move_workbuddy_dynamic_context(
        body,
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is False
    assert moved.body == body


def test_dynamic_sessions_share_the_same_reusable_prefix() -> None:
    first = move_workbuddy_dynamic_context(
        _body("memory=session-a", "first question"),
        "chat",
        client_id=CLIENT_ID,
    ).body
    second = move_workbuddy_dynamic_context(
        _body("memory=session-b", "second question"),
        "chat",
        client_id=CLIENT_ID,
    ).body

    first_prefix = PrefixAffinityRepository.reusable_prefix_body(
        first,
        "chat",
    )
    second_prefix = PrefixAffinityRepository.reusable_prefix_body(
        second,
        "chat",
    )
    assert first_prefix == second_prefix
    assert first_prefix["messages"] == [
        {
            "role": "system",
            "content": "stable tools and instructions",
        }
    ]


def _raw_body(
    workspace_memory: str,
    tool_search_description: str,
    user_content,
):
    return {
        "model": "siyuan/qwen36-shared",
        "messages": [
            {
                "role": "system",
                "content": (
                    "<memory_system>\n"
                    "stable global rules\n\n"
                    "# Layer 3 \u2014 Workspace Memory (read/write)"
                    "\n\n"
                    f"{workspace_memory}\n\n"
                    "volatile maintenance state\n\n"
                    "</memory_system>\n\n"
                    "stable final instructions"
                ),
            },
            {"role": "user", "content": user_content},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Agent",
                    "description": f"agents: {tool_search_description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Skill",
                    "description": f"skills: {tool_search_description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {"type": "string"},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ToolSearch",
                    "description": f"tools: {tool_search_description}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                        },
                    },
                },
            },
        ],
    }


def test_raw_workbuddy_sessions_share_the_same_reusable_prefix() -> None:
    first = move_workbuddy_dynamic_context(
        _raw_body(
            "workspace memory A",
            "deferred tools A",
            "first question",
        ),
        "chat",
        client_id=CLIENT_ID,
    )
    second = move_workbuddy_dynamic_context(
        _raw_body(
            "workspace memory B",
            "deferred tools B",
            "second question",
        ),
        "chat",
        client_id=CLIENT_ID,
    )

    assert first.moved is True
    assert second.moved is True
    assert first.mode == "raw_workbuddy"
    assert second.mode == "raw_workbuddy"
    first_prefix = PrefixAffinityRepository.reusable_prefix_body(
        first.body,
        "chat",
    )
    second_prefix = PrefixAffinityRepository.reusable_prefix_body(
        second.body,
        "chat",
    )
    assert first_prefix == second_prefix
    assert "workspace memory A" not in str(first_prefix)
    assert "deferred tools A" not in str(first_prefix)
    assert "workspace memory A" in first.body["messages"][-1]["content"]
    assert "deferred tools A" in first.body["messages"][-1]["content"]
    assert [
        tool["function"]["name"]
        for tool in first.body["tools"]
    ] == ["Read", "Agent", "Skill", "ToolSearch"]
    assert [
        tool["function"]["parameters"]
        for tool in first.body["tools"]
    ] == [
        tool["function"]["parameters"]
        for tool in _raw_body(
            "workspace memory A",
            "deferred tools A",
            "first question",
        )["tools"]
    ]


def test_raw_workbuddy_multimodal_content_keeps_image_at_tail() -> None:
    image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,redacted"},
    }
    moved = move_workbuddy_dynamic_context(
        _raw_body(
            "workspace memory",
            "deferred tools",
            [{"type": "text", "text": "describe"}, image],
        ),
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is True
    assert moved.body["messages"][-1]["content"][0]["type"] == "text"
    assert moved.body["messages"][-1]["content"][-1] == image


def test_raw_workbuddy_other_model_is_unchanged() -> None:
    body = _raw_body(
        "workspace memory",
        "deferred tools",
        "question",
    )
    body["model"] = "siyuan/auto"

    moved = move_workbuddy_dynamic_context(
        body,
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is False
    assert moved.body == body


def test_raw_workbuddy_malformed_boundaries_are_unchanged() -> None:
    body = _raw_body(
        "workspace memory",
        "deferred tools",
        "question",
    )
    body["messages"][0]["content"] += (
        "\n# Layer 3 \u2014 Workspace Memory (read/write)"
    )

    moved = move_workbuddy_dynamic_context(
        body,
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is False
    assert moved.body == body


def test_raw_workbuddy_without_user_message_is_unchanged() -> None:
    body = _raw_body(
        "workspace memory",
        "deferred tools",
        "question",
    )
    body["messages"][-1]["role"] = "assistant"

    moved = move_workbuddy_dynamic_context(
        body,
        "chat",
        client_id=CLIENT_ID,
    )

    assert moved.moved is False
    assert moved.body == body


_CONTINUATIONS = [
    [],
    [{"role": "assistant", "content": "continuing"}],
    [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"a"}'}}
    ]}, {"role": "tool", "tool_call_id": "call-1", "content": "tool result"}],
]


@pytest.mark.parametrize("tail", _CONTINUATIONS)
@pytest.mark.parametrize("raw_format", [False, True])
@pytest.mark.parametrize("user_content", ["question", [
    {"type": "text", "text": "question"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,unchanged"}},
]])
def test_latest_user_normalization_preserves_continuation_and_is_idempotent(tail, raw_format, user_content):
    body = (_raw_body("workspace memory", "dynamic catalog", copy.deepcopy(user_content))
            if raw_format else _body("session memory", copy.deepcopy(user_content)))
    first = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)
    body["messages"].extend(copy.deepcopy(tail))
    original = copy.deepcopy(body)
    moved = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)

    assert moved.moved
    assert moved.target_user_index == 1
    assert moved.body["messages"][:2] == first.body["messages"]
    assert moved.body["messages"][2:] == tail
    assert moved.body.get("tools") == first.body.get("tools")
    assert moved.stable_prefix_sha256 == first.stable_prefix_sha256
    assert len(moved.stable_prefix_sha256) == 64
    assert body == original
    for _ in range(2):
        again = move_workbuddy_dynamic_context(moved.body, "chat", client_id=CLIENT_ID)
        assert not again.moved
        assert again.body == moved.body
        assert again.stable_prefix_sha256 == moved.stable_prefix_sha256
        moved = again


def test_only_latest_user_is_changed_and_prior_history_is_preserved():
    body = _raw_body("memory", "catalog", "current question")
    earlier = [{"role": "user", "content": "previous question"},
               {"role": "assistant", "content": "previous answer"}]
    body["messages"][1:1] = copy.deepcopy(earlier)
    body["messages"].extend(copy.deepcopy(_CONTINUATIONS[-1]))
    moved = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)
    assert moved.moved
    assert moved.target_user_index == 3
    assert moved.body["messages"][1:3] == earlier
    assert moved.body["messages"][4:] == body["messages"][4:]
    assert moved.body["messages"][3]["content"].endswith("current question")
    for original_tool, changed_tool in zip(body["tools"], moved.body["tools"]):
        assert original_tool["function"]["parameters"] == changed_tool["function"]["parameters"]
        assert original_tool["function"]["name"] == changed_tool["function"]["name"]


@pytest.mark.parametrize("marker", [START, END, START + "already moved" + END])
@pytest.mark.parametrize("content_list", [False, True])
def test_existing_user_marker_rejects_entire_transform_without_partial_changes(marker, content_list):
    content = [{"type": "text", "text": "question"}, {"type": "text", "text": marker}] if content_list else "question " + marker
    body = _raw_body("original memory", "original descriptions", content)
    body["messages"].extend(copy.deepcopy(_CONTINUATIONS[-1]))
    original = copy.deepcopy(body)
    moved = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)
    assert not moved.moved
    assert moved.skip_reason == "existing_user_dynamic_context"
    assert moved.target_user_index == 1
    assert moved.body == original == body


@pytest.mark.parametrize("content", [None, 123, {"unexpected": "shape"}])
def test_unsupported_latest_user_content_keeps_system_and_tools_unchanged(content):
    body = _raw_body("memory", "catalog", content)
    body["messages"].extend(copy.deepcopy(_CONTINUATIONS[-1]))
    moved = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)
    assert not moved.moved
    assert moved.skip_reason == "unsupported_user_content"
    assert moved.body == body


def test_tools_only_normalization_is_idempotent_for_tool_continuation():
    body = _raw_body("memory", "catalog", "question")
    body["messages"][0]["content"] = "stable instructions"
    body["messages"].extend(copy.deepcopy(_CONTINUATIONS[-1]))
    moved = move_workbuddy_dynamic_context(body, "chat", client_id=CLIENT_ID)
    assert moved.moved and moved.mode == "dynamic_tools"
    again = move_workbuddy_dynamic_context(moved.body, "chat", client_id=CLIENT_ID)
    assert not again.moved
    assert again.body == moved.body
