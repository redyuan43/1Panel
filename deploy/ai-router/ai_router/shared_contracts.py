"""Offline behavioral probes for release preflight, never request admission.

The release tool executes this SAME probe in every baseline/candidate image.
Only synthetic data is used. Digests describe selected persistent contracts,
not whole source files, and do not replace the cross-component test suite.
Keep this module standalone so it can also probe images predating this file.
"""
import base64
import asyncio
import hashlib
import json
import inspect


def history_samples():
    opening = [{"role": "user", "content": "contract question"}]
    answer = {"role": "assistant", "content": "contract answer"}
    call = {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"b":2, "a":1}'}}
    return [
        opening,
        [*opening, answer],
        [*opening, {**answer, "reasoning": "contract thought"}],
        [*opening, {**answer, "reasoning_content": "contract thought"}],
        [*opening, {**answer, "reasoning": "A", "reasoning_content": "B"}],
        [*opening, {**answer, "content": [{"type": "text", "text": "contract answer"}]}],
        [*opening, {**answer, "tool_calls": [call], "reasoning": "contract tool thought"},
         {"role": "tool", "tool_call_id": "call-1", "content": "contract result"}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}, answer],
        [{"type": "reasoning", "id": "rs-contract", "encrypted_content": "synthetic-only", "summary": []},
         {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
         {"type": "function_call_output", "call_id": "call-1", "output": "contract result"}],
    ]


def digest(value):
    def encode(item):
        if isinstance(item, bytes):
            return {"bytes_base64": base64.b64encode(item).decode()}
        raise TypeError("unsupported probe value")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=encode).encode()
    return hashlib.sha256(raw).hexdigest()


def contract_report():
    # Absolute imports also work when the release tool injects this probe into
    # an old image. No Runtime, Redis connection, archive or model is created.
    from ai_router.history import verified_history_identity
    from ai_router import archive_queue
    from ai_router.archive_worker import OPERATIONS
    from ai_router.types import ConversationState
    from ai_router.training_archive import TrainingArchive

    from ai_router.errors import HistoryMigrationRequiredError
    from ai_router import workbuddy_history
    from ai_router import history_identity
    identities = []
    prefix_samples = []
    for messages in history_samples():
        try:
            identity = verified_history_identity(messages)
            prefix = workbuddy_history.chain({"messages": messages})
        except HistoryMigrationRequiredError as error:
            identity = prefix = "error:" + error.code
        identities.append(identity)
        prefix_samples.append(prefix)
    events = []
    for operation in sorted(OPERATIONS | {"history"}):
        event = {"version": 1, "id": "event", "operation": operation, "token": "request",
                 "created_at": 1, "kwargs": {"response_payload": b"synthetic response",
                 "received_body": {"messages": history_samples()[2]}}}
        wire = archive_queue.encode_event(event)
        restored = archive_queue.decode_event(wire)
        if restored != {**event, "response_base64": True}:
            raise ValueError("archive probe did not round-trip")
        events.append(json.loads(wire))
    legacy = {"conversation_id": "conversation", "public_model": "auto", "endpoint_id": "target",
              "tier_rank": 1, "task": "code", "last_seen": 1}
    directed = {**legacy, "branch_id": "branch", "parent_branch_id": "parent",
                "directive_id": "directive", "directive_generation": 3, "directive_endpoint_id": "target",
                "encrypted_capsule": "synthetic-only", "boundary_hash": "synthetic-only"}
    states = [ConversationState.from_dict(value).to_dict() for value in (legacy, directed)]
    for state in states:
        if ConversationState.from_dict(state).to_dict() != state:
            raise ValueError("conversation probe did not round-trip")

    async def match_history():
        from types import SimpleNamespace
        from ai_router.policy import ConversationRepository
        from ai_router.store import InMemoryStateStore
        repository = ConversationRepository(InMemoryStateStore(), SimpleNamespace(section=lambda _: {}))
        await repository.save(ConversationState.from_dict(directed))
        identity = "wb-raw-v1:" + identities[2]
        await repository.map_history("synthetic-client", (identity,), "branch")
        parent, evidence = await repository.verified_history_match("synthetic-client", (identity,))
        # A candidate that updates the writer but leaves policy on the old
        # namespace must fail preflight even when fingerprint samples agree.
        if parent is None or parent.directive_id != "directive" or parent.directive_endpoint_id != "target":
            raise ValueError("history producer/consumer probe did not preserve directive")
        return evidence

    history_match = asyncio.run(match_history())
    public_identity = getattr(history_identity, "public_history_identity", None)
    public_samples = [public_identity(
        history_samples()[0], [{"role": "assistant", "content": "public answer"}],
        client_id=client, protocol=protocol,
    ) for client, protocol in (("workbuddy-public", "chat"), ("workbuddy-public", "responses"),
                               ("other-client", "chat"))] if public_identity else None
    return {
        "probe_version": 1,
        "history": {"version": identities[0].split("-")[0], "samples_sha256": digest(identities),
                    "consumer_sha256": digest(history_match),
                    "public_samples_sha256": digest(public_samples),
                    "workbuddy_prefix_version": getattr(workbuddy_history, "PREFIX_IDENTITY_VERSION", 1),
                    "workbuddy_prefix_sha256": digest(prefix_samples)},
        "archive": {"version": getattr(archive_queue, "ARCHIVE_EVENT_VERSION", 1), "samples_sha256": digest(events),
                    "completed_public_history": "public_assistant_items" in inspect.signature(TrainingArchive.complete).parameters},
        "conversation": {"samples_sha256": digest(states)},
    }


def check_release_contracts(baseline, candidates, order):
    """Reject unsafe live combinations, including each per-role rollback.

    A semantic migration that cannot pass needs a separately reviewed bridge
    or coordinated maintenance, not an override flag on a rolling release.
    """
    roles = {"worker", "local", "tail"}
    if set(baseline) != roles or set(candidates) != roles or not set(order) <= roles:
        raise ValueError("all API and archive-worker contracts are required")

    def check(reports, stage):
        first = reports["worker"]
        if set(first) != {"probe_version", "history", "archive", "conversation"} or first["probe_version"] != 1:
            raise ValueError("invalid shared contract report")
        for role in ("local", "tail"):
            if reports[role] != first:
                raise RuntimeError(f"Shared contract mismatch at {stage}: {role}/worker; no safe rolling release")

    active = dict(baseline)
    check(active, "baseline")
    for role in order:
        previous = dict(active)
        active[role] = candidates[role]
        check(active, "replace " + role)
        check(previous, "rollback " + role)


if __name__ == "__main__":
    print(json.dumps(contract_report(), sort_keys=True))
