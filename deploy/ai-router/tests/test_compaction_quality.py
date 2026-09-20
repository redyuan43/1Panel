import asyncio
import json
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet
import pytest

from ai_router.compaction import CapsuleCipher, ContextCompactor, SummaryWork, SummaryResponseError
from ai_router.errors import CompactionUnavailableError


class Counter:
    def count_request(self, body, kind):
        return len(json.dumps(body, ensure_ascii=False)) // 4 + 1


def compactor(client=None):
    return ContextCompactor(Counter(), CapsuleCipher(Fernet.generate_key().decode()),
        internal_base_url="http://isolated.invalid", internal_api_key="", model_id="summary",
        client=client or AsyncMock())


def test_model_window_allows_single_summary_over_old_32k_limit():
    async def scenario():
        value = compactor()
        messages = [{"role": "user", "content": "history " * 20000}]
        value._summarize = AsyncMock(return_value={"facts": ["preserved"]})
        assert value.token_counter.count_request(value._summary_request(messages, None), "chat") > 32768
        result = await value._summarize_bounded(messages, target=None, input_budget=100000)
        assert result["facts"] == ["preserved"]
        value._summarize.assert_awaited_once_with(messages, target=None)
    asyncio.run(scenario())


@pytest.mark.parametrize("finish,content,reason", [("length", '{"facts":["partial"]}', "incomplete_response"),
    ("content_filter", '{"facts":["partial"]}', "incomplete_response"), ("stop", '{}', "empty_handoff"),
    ("stop", '{"facts":"invalid type"}', "invalid_fields"), ("stop", '[]', "non_object"),
    ("stop", 'broken JSON', "invalid_json"), ("stop", None, "invalid_content")])
def test_invalid_or_truncated_summaries_are_rejected(finish, content, reason):
    async def scenario():
        def upstream(request):
            assert json.loads(request.content)["max_tokens"] == 8192
            assert "Every field must be an array" in json.loads(request.content)["messages"][0]["content"]
            return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                                                         "message": {"content": content}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            value = compactor(client)
            with pytest.raises(SummaryResponseError) as error:
                await value._summarize([{"role": "user", "content": "important source"}])
            assert error.value.status_code == 200
            assert error.value.reason_code == reason
            assert not error.value.retryable
            diagnostics = error.value.diagnostics
            assert diagnostics["response_bytes"] > 0
            assert len(diagnostics["response_sha256"]) == 64
            assert diagnostics["finish_reason"] == finish
            if reason == "invalid_json":
                assert diagnostics["validation_exception"] == "JSONDecodeError"
                assert diagnostics["json_error_position"] == 0
            elif reason == "invalid_fields":
                assert diagnostics["invalid_field_types"] == {"facts": "str"}
            elif reason == "non_object":
                assert diagnostics["value_type"] == "list"
            elif reason == "empty_handoff":
                assert diagnostics["present_handoff_fields"] == []
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [False, True])
def test_batches_keep_fitting_tool_transaction_intact(responses):
    value = compactor()
    transaction = [{"role": "assistant", "tool_calls": [{"id": "tool-1", "type": "function",
        "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tool-1", "content": "returned evidence"}]
    if responses:
        transaction = [{"type": "function_call", "call_id": "tool-1", "name": "read", "arguments": "{}"},
                       {"type": "function_call_output", "call_id": "tool-1", "output": "returned evidence"}]
    messages = [{"role": "user", "content": "older " * 400}, *transaction,
                {"role": "user", "content": "next " * 400}]
    batches = list(value._summary_batches(messages, None, 900))
    assert len(batches) > 1
    assert any(all(item in batch for item in transaction) for batch in batches)
    assert all(value.token_counter.count_request(value._summary_request(batch, None), "chat") <= 900
               for batch in batches)


def test_oversized_transaction_fragments_have_contiguous_source_offsets():
    value = compactor()
    group = [{"role": "tool", "tool_call_id": "large", "content": "中文🙂\n" * 4000}]
    batches = list(value._split_oversized_group(group, None, 900))
    original = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
    restored = ""
    for batch in batches:
        header, fragment = batch[0]["content"].split("\n", 1)
        assert f"character_offset={len(restored)}." in header
        assert "source_sha256=" in header
        restored += fragment
    assert restored == original


def test_call_budget_is_shared_across_recursive_passes():
    async def scenario():
        value = compactor()
        value._summarize = AsyncMock()
        with pytest.raises(CompactionUnavailableError, match="bounded"):
            await value._summarize_bounded([{"role": "user", "content": "source"}],
                target=None, input_budget=1024, work=SummaryWork(calls=32))
        value._summarize.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("api_kind", ["chat", "responses"])
def test_giant_recent_tool_result_can_fit_without_breaking_calls_or_continuation(api_kind):
    import copy
    import hashlib
    from ai_router.compaction import extract_messages, message_hash, replace_messages
    async def scenario():
        value = compactor()
        rules = {"role": "system", "content": "关键规则：未经确认不得部署，不能泄露凭据。"}
        user = {"role": "user", "content": "检查日志，保留错误编号，不要重启服务。"}
        large = "重复日志\n" * 20000 + "最终错误 E_FINAL_782，退出码 7"
        if api_kind == "chat":
            call = {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "read_log", "arguments": '{"path":"/srv/test.log"}'}}]}
            output = {"role": "tool", "tool_call_id": "call-1", "content": large}
        else:
            call = {"type": "function_call", "call_id": "call-1", "name": "read_log",
                    "arguments": '{"path":"/srv/test.log"}'}
            output = {"type": "function_call_output", "call_id": "call-1", "output": large}
        original = replace_messages({"model": "target"}, api_kind, [rules, user, call, output])
        untouched = copy.deepcopy(original)
        requests = []
        async def summarize(batch, **kwargs):
            requests.append(batch)
            # The provider may omit the terminal error. Raw edge evidence
            # must survive independently of the generated handoff.
            return {"facts": ["diagnostics were processed"], "key_references": ["/srv/test.log"]}
        value._summarize = summarize
        capsule = await value.compact(original, api_kind=api_kind, target_context_tokens=12000,
                                      summary_input_tokens=8000)
        assert capsule.after_tokens <= 7200 and capsule.after_tokens < capsule.before_tokens
        assert capsule.boundary_hash == message_hash(output)
        compacted = value.cipher.decrypt(capsule.encrypted_messages)
        assert compacted[0] == rules and user in compacted and call in compacted
        last = compacted[-1]
        field = "content" if api_kind == "chat" else "output"
        assert hashlib.sha256(large.encode()).hexdigest() in last[field]
        assert "E_FINAL_782" in last[field]
        assert {key: val for key, val in last.items() if key != field} == {
            key: val for key, val in output.items() if key != field}
        assert original == untouched
        assert len(requests) > 1
        assert all(value.token_counter.count_request(value._summary_request(batch, None), "chat") <= 8000
                   for batch in requests)
        tail = {"role": "user", "content": "现在解释原因，仍不要重启。"}
        appended = replace_messages(original, api_kind, [rules, user, call, output, tail])
        applied = value.apply_existing(appended, api_kind=api_kind,
            encrypted_messages=capsule.encrypted_messages, boundary_hash=capsule.boundary_hash)
        assert extract_messages(applied.body, api_kind) == [*compacted, tail]
    asyncio.run(scenario())


def test_structured_literal_values_survive_summary_and_encrypted_storage():
    async def scenario():
        facts = {"component": "alpha_beta", "code": "00123", "timeout_ms": "20",
                 "retries": 0, "enabled": False, "missing": None}
        def upstream(request):
            body = json.loads(request.content)
            assert "literal values verbatim" in body["messages"][0]["content"]
            assert "add units or descriptive words" in body["messages"][0]["content"]
            assert json.loads(body["messages"][1]["content"])[0]["content"] == json.dumps(facts)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps({"facts": [facts]})}}],
                "usage": {"completion_tokens": 64}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            value = compactor(client)
            result = await value._summarize([{"role": "user", "content": json.dumps(facts)}])
            assert value.cipher.decrypt(value.cipher.encrypt(result))["facts"] == [facts]
    asyncio.run(scenario())


def test_giant_user_instruction_is_not_silently_truncated():
    async def scenario():
        value = compactor()
        value._summarize = AsyncMock()
        body = {"messages": [{"role": "user", "content": "必须完整遵守的指令" * 10000}]}
        with pytest.raises(CompactionUnavailableError):
            await value.compact(body, api_kind="chat", target_context_tokens=4000, summary_input_tokens=8000)
        value._summarize.assert_not_awaited()
    asyncio.run(scenario())
