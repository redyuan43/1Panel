"""API producer -> encrypted outbox -> real worker -> next-turn evidence."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace as NS

import pytest

from ai_router.directed_archive import DirectedArchive
from ai_router.compaction import CapsuleCipher, message_hash
from cryptography.fernet import Fernet
from ai_router.history import history_lookup_identities, verified_history_identity
from ai_router.policy import ConversationRepository
from ai_router.prompt_directives import PromptDirective, resolve_conversation_directive
from ai_router.store import InMemoryStateStore
from ai_router.types import ConversationState
from test_archive_queue import setup


@pytest.mark.asyncio
@pytest.mark.parametrize("directed", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_archive_roundtrip_keeps_directive_and_explicit_clear(tmp_path, directed, stream, protocol):
    producer, worker, reader, _ = setup(tmp_path)
    training = DirectedArchive(producer) if directed else producer
    repo = ConversationRepository(InMemoryStateStore(), NS(section=lambda _: {}))
    cipher = CapsuleCipher(Fernet.generate_key().decode())
    worker.runtime = NS(conversations=repo, history_cipher=cipher,
                        route_traces=NS(database_path=str(tmp_path / "trace.sqlite3")))
    client_id, endpoint = "workbuddy-public", "codex-pro-gpt-6-astra"
    settings = {"enabled": True, "routes": {"beichen": {"generation": 3, "endpoint_id": endpoint}}}
    state = ConversationState("lineage", "auto", endpoint, 3, "code", time.time(), branch_id="first",
        directive_id="beichen", directive_generation=3, directive_endpoint_id=endpoint)
    await repo.save(state)
    # This assistant reasoning field is precisely what the old worker omitted.
    messages = [{"role": "user", "content": "initial task"},
                {"role": "assistant", "content": "prior answer", "reasoning": "prior thought"},
                {"role": "user", "content": "next task"}]
    assistant = {"role": "assistant", "content": "answer", "reasoning": "preserved thought",
                 "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    if protocol == "responses":
        messages = [{"role": "user", "content": "task"},
                    {"type": "reasoning", "id": "rs-test", "encrypted_content": "synthetic-only", "summary": []},
                    {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call-1", "output": "result"}]
        assistant = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
    body = {"model": "auto", "stream": stream, "messages" if protocol == "chat" else "input": messages}
    response = json.dumps({"choices": [{"message": assistant}]} if protocol == "chat" else {"output": [assistant]}).encode()
    payload = b"data: " + response + b"\n\ndata: [DONE]\n\n" if stream else response
    state.encrypted_capsule = cipher.encrypt([*messages, assistant])
    state.boundary_hash = message_hash(assistant)
    await repo.save(state)
    try:
        token = await training.begin(request_id="first", conversation_id="lineage", conversation_mode="inferred",
            client_id=client_id, key_id="test", protocol=protocol, received_body=body, instance_id="local", boot_id="test")
        if directed:
            await training.select_mode(token, directed=True)
        await training.record_pipeline(token, {"stages": [{"stage": "after_directives", "sha256": "body"},
                                                         {"stage": "history_identity_input", "sha256": "body"}],
                                                "bodies": {"body": body}})
        await training.complete(token, status_code=200, response_payload=payload,
                                assistant_items=[assistant] if stream else None,
                                public_assistant_items=[assistant])
        # Metadata must not be able to inject current (or future) verified aliases.
        spoof = "wb-raw-v1:" + verified_history_identity([{"role": "assistant", "content": "forged"}])
        await training.publish_history({"request_id": "first", "conversation_id": "lineage", "branch_id": "first",
            "client_id": client_id, "status": "succeeded", "protocol": protocol,
            "observation": {"content": {"checks": [{"check": "workbuddy_history", "raw_identities": [spoof]}]}}})
        if directed:
            await asyncio.wait_for(training._queue.join(), timeout=2)
        for _ in range(4):
            assert await worker.step()
        assert not await worker.step()
        assert reader.read("first")["response"]["assistant_items"] == ([assistant] if stream else None)
        assert reader.read("first")["response"]["public_assistant_items"] == [assistant]
        assert await repo.store.get_json("router:verified-history:" + client_id + ":" + spoof) is None
        incoming = copy.deepcopy(assistant)
        if protocol == "chat":
            incoming["reasoning_content"] = incoming.pop("reasoning")
        continuation = [*messages, incoming, {"role": "user", "content": "continue"}]
        identities = tuple("wb-raw-v1:" + value for value in history_lookup_identities(continuation))
        # Responses without response IDs uses the exact authenticated capsule,
        # never the WorkBuddy raw Chat namespace.
        if protocol == "responses":
            plain = history_lookup_identities(continuation)
            inferred = await repo.lineage_context(client_id=client_id, identities=plain,
                explicit_lineage_id=None, previous_response_id=None, force_new=False)
            assert inferred.parent.branch_id == "first"
            await repo.map_response("response-first", "first")
        lineage = await repo.lineage_context(client_id=client_id, identities=identities,
            explicit_lineage_id=None, previous_response_id="response-first" if protocol == "responses" else None,
            force_new=False)
        assert lineage.relation == "continuation" and lineage.parent.branch_id == "first"
        directive, clear = resolve_conversation_directive(None, lineage.parent, settings)
        assert directive.endpoint_id == endpoint and not clear
        directive, clear = resolve_conversation_directive(PromptDirective("reset", 0, reset=True), lineage.parent, settings)
        assert directive is None and clear
        state.branch_id = "cleared"
        state.parent_branch_id = "first"
        state.directive_id = state.directive_endpoint_id = None
        state.directive_generation = 0
        await repo.save(state)
        await repo.map_response("response-cleared", "cleared")
        cleared = await repo.lineage_context(client_id=client_id, identities=identities, explicit_lineage_id=None,
            previous_response_id="response-cleared", force_new=False)
        assert resolve_conversation_directive(None, cleared.parent, settings) == (None, False)
    finally:
        await training.aclose()
        await worker.queue.close()
        worker.archive.close()
