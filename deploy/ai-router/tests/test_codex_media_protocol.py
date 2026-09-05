from __future__ import annotations

import asyncio
import base64
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from websockets.frames import Frame, Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol

from ai_router.media_service import providers
from ai_router.media_service.contracts import MediaError, QuotaExceeded, UnknownOutcome, image_request
from ai_router.media_service.providers import CodexProvider, CodexRPC


def effective_response(cwd: Path) -> dict:
    return {
        "activePermissionProfile": {"id": "router_media", "extends": None},
        "approvalPolicy": "never", "cwd": str(cwd), "modelProvider": "openai",
        "runtimeWorkspaceRoots": [],
        "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                    "excludeTmpdirEnvVar": True, "excludeSlashTmp": True},
        "thread": {"id": "dedicated", "cwd": str(cwd), "ephemeral": False},
    }


class ProxyPeer:
    """In-memory byte relay endpoint speaking real RFC6455 through websockets."""

    def __init__(self):
        self.server = ServerProtocol(max_size=192 * 1024 * 1024)
        self.stdout = asyncio.StreamReader(limit=192 * 1024 * 1024)
        self.stdin = self
        self.returncode = None
        self.requests = []
        self.fragments = []
        self.terminated = False
        self.frame_sizes = []
        self.fail_method = None

    def flush(self):
        for data in self.server.data_to_send():
            if data:
                self.stdout.feed_data(data)

    def respond(self, value, fragmented=False):
        data = json.dumps(value).encode()
        if fragmented:
            self.server.send_text(data[:11], fin=False)
            self.server.send_continuation(data[11:], fin=True)
        else:
            self.server.send_text(data)
        self.flush()

    def write(self, data):
        self.server.receive_data(data)
        for event in self.server.events_received():
            if isinstance(event, Request):
                self.server.send_response(self.server.accept(event))
            elif isinstance(event, Frame) and event.opcode in (Opcode.TEXT, Opcode.CONT):
                self.frame_sizes.append(len(event.data))
                self.fragments.append(event.data)
                if event.fin:
                    value = json.loads(b"".join(self.fragments))
                    self.fragments.clear()
                    self.requests.append(value)
                    if "method" in value and "id" in value:
                        if value["method"] == self.fail_method:
                            self.respond({"id": value["id"], "error": {"code": -32000,
                                "message": "private upstream detail",
                                "data": {"codexErrorInfo": "usageLimitExceeded"}}})
                        else:
                            result = {"account": {"type": "chatgpt"}} if value["method"] == "account/read" else {}
                            self.respond({"id": value["id"], "result": result}, fragmented=True)
        self.flush()

    async def drain(self):
        pass

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.stdout.feed_eof()

    async def wait(self):
        return self.returncode


def mock_proxy(monkeypatch, peer):
    async def spawn(*args, **kwargs):
        assert args == ("codex", "app-server", "proxy")
        assert "env" not in kwargs
        return peer
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


def test_proxy_websocket_handshake_fragmentation_ping_and_tool_denial(monkeypatch):
    async def scenario():
        peer = ProxyPeer()
        mock_proxy(monkeypatch, peer)
        rpc = CodexRPC()
        try:
            await rpc.start()
            account = await rpc.call("account/read", {"refreshToken": False})
            assert account == {"account": {"type": "chatgpt"}}
            peer.server.send_ping(b"keepalive")
            peer.flush()
            peer.respond({"id": 37, "method": "item/permissions/requestApproval",
                          "params": {"permissions": {"filesystem": {"read": ["/"]}}}})
            await rpc.call("thread/read", {"threadId": "only-owned-probe"})
            assert next(value for value in peer.requests if value.get("id") == 37)["error"]["code"] == -32601
            assert peer.requests[0]["method"] == "initialize"
            assert peer.requests[1]["method"] == "initialized"
        finally:
            await rpc.close()
        assert peer.terminated
    asyncio.run(scenario())


def test_proxy_large_messages_use_bounded_frames(monkeypatch):
    async def scenario():
        peer = ProxyPeer()
        mock_proxy(monkeypatch, peer)
        rpc = CodexRPC()
        try:
            await rpc.start()
            await rpc.call("test", {"input": "a" * (5 * 1024 * 1024)})
            assert max(peer.frame_sizes) <= 4 * 1024 * 1024
            assert len(peer.requests[-1]["params"]["input"]) == 5 * 1024 * 1024
            rpc.MAX_REQUEST_BYTES = 100
            with pytest.raises(MediaError) as exc:
                await rpc.call("test", {"input": "a" * 100})
            assert exc.value.code == "codex_request_too_large"
            assert not rpc.pending
        finally:
            await rpc.close()
    asyncio.run(scenario())


def test_proxy_typed_quota_only_and_disconnect(monkeypatch):
    async def scenario():
        peer = ProxyPeer()
        mock_proxy(monkeypatch, peer)
        rpc = CodexRPC()
        try:
            await rpc.start()
            peer.fail_method = "limited"
            with pytest.raises(QuotaExceeded):
                await rpc.call("limited", {})
            future = asyncio.get_running_loop().create_future()
            rpc.pending["lost"] = future
            peer.stdout.feed_eof()
            with pytest.raises(UnknownOutcome):
                await future
            rpc.pending.clear()
            assert (await rpc.events.get())["method"] == "disconnected"
        finally:
            await rpc.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("section", "key", "value"), [
    ("activePermissionProfile", "id", ":full-access"),
    ("activePermissionProfile", "extends", ":full-access"),
    ("sandbox", "type", "dangerFullAccess"),
    ("sandbox", "networkAccess", True),
    ("sandbox", "networkAccess", None),
    ("sandbox", "writableRoots", ["/tmp"]),
    ("sandbox", "excludeSlashTmp", False),
    ("sandbox", "excludeTmpdirEnvVar", False),
    ("thread", "cwd", "/home/ai"),
    ("thread", "ephemeral", True),
    (None, "approvalPolicy", "on-request"),
    (None, "runtimeWorkspaceRoots", ["/home/ai"]),
    (None, "modelProvider", "other"),
])
def test_effective_permissions_fail_closed(tmp_path, section, key, value):
    response = effective_response(tmp_path)
    (response[section] if section else response)[key] = value
    with pytest.raises(MediaError) as exc:
        CodexProvider._verify_permissions(response, tmp_path)
    assert exc.value.code == "codex_permissions_unverified"


def test_config_explicit_profile_no_legacy_no_trust_persistence(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('sandbox_mode="danger-full-access"\n'
                      '[mcp_servers.local]\ncommand="sensitive-command"\n'
                      '[plugins."existing@market"]\nenabled=true\n')
    before = config.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(home))
    params = CodexProvider._thread_params(tmp_path)
    settings = params["config"]
    assert params["permissions"] == "router_media"
    assert "sandbox" not in params
    assert "sandbox_workspace_write" not in settings
    assert settings["projects"][str(tmp_path)]["trust_level"] == "untrusted"
    assert settings["mcp_servers"] == {"local": {"enabled": False}}
    assert settings["plugins"] == {"existing@market": {"enabled": False}}
    assert settings["features"]["hooks"] is False
    assert settings["features"]["shell_tool"] is False
    assert settings["features"]["image_generation"] is True
    assert settings["memories"]["generate_memories"] is False
    assert settings["permissions"]["router_media"]["filesystem"] == {
        ":minimal": "read", str(tmp_path): "write",
        str(CodexProvider._skill_path().parent): "read"}
    assert config.read_bytes() == before
    assert CodexProvider._verify_permissions(effective_response(tmp_path), tmp_path)["network_access"] is False


def fake_provider_rpc(monkeypatch, cwd, response=None, account="chatgpt", recovery=None,
                      complete_image=True, disconnected=False, errors=None, turn_gate=None,
                      image_items=None, completed_turn=None, interrupt_recovery=None):
    calls = []

    class RPC:
        process = True

        def __init__(self):
            self.events = asyncio.Queue()
            self.interrupted = False

        async def start(self):
            pass

        async def call(self, method, params):
            calls.append((method, copy.deepcopy(params)))
            if method in (errors or {}):
                raise errors[method]
            if method == "account/read":
                return {"account": {"type": account}}
            if method == "thread/start":
                return response if response is not None else effective_response(cwd)
            if method == "thread/read":
                return interrupt_recovery if self.interrupted and interrupt_recovery is not None else recovery
            if method == "turn/interrupt":
                self.interrupted = True
                return {}
            if method == "turn/start":
                if turn_gate:
                    turn_gate[0].set()
                    await turn_gate[1].wait()
                if complete_image:
                    items = image_items if image_items is not None else [{
                            "type": "imageGeneration", "status": "completed",
                            "result": base64.b64encode(b"fixture-result").decode()}]
                    for item in items:
                        self.events.put_nowait({"method": "item/completed", "params": {
                            "threadId": "dedicated", "turnId": "one", "item": item}})
                elif disconnected:
                    self.events.put_nowait({"method": "disconnected"})
                if completed_turn is not None:
                    self.events.put_nowait({"method": "turn/completed", "params": {
                        "threadId": "dedicated", "turn": completed_turn}})
                return {"turn": {"id": "one"}}
            return {}

        async def close(self):
            calls.append(("closed", {}))

    monkeypatch.setattr(providers, "CodexRPC", RPC)
    return calls


def test_generate_checks_effective_permissions_before_any_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    calls = fake_provider_rpc(monkeypatch, tmp_path, response={"thread": {"id": "unsafe"}})
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert exc.value.code == "codex_permissions_unverified"
    assert not any(method == "turn/start" for method, _ in calls)
    assert calls[-1][0] == "closed"


def test_generate_persists_intent_then_interrupts_after_one_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    calls = fake_provider_rpc(monkeypatch, tmp_path)
    states = []
    result = asyncio.run(CodexProvider().generate(
        image_request({"prompt": "fixture"}), {}, lambda state: states.append(copy.deepcopy(state)), tmp_path))
    assert result["data"] == b"fixture-result"
    assert [state["submitted"] for state in states] == [False, True, True]
    assert states[-1]["turn_id"] == "one"
    assert ("turn/interrupt", {"threadId": "dedicated", "turnId": "one"}) in calls
    assert sum(method == "turn/start" for method, _ in calls) == 1


@pytest.mark.parametrize("account", ["apiKey", None])
def test_never_substitutes_api_key_authentication(tmp_path, monkeypatch, account):
    calls = fake_provider_rpc(monkeypatch, tmp_path, account=account)
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert exc.value.code == "codex_login_required"
    assert [method for method, _ in calls] == ["account/read", "closed"]


def test_recovery_is_owned_and_never_submits_another_turn(tmp_path, monkeypatch):
    state = {"thread_id": "dedicated", "turn_id": "one", "submitted": True}
    recovery = {"thread": {"id": "dedicated", "cwd": str(tmp_path), "turns": [{
        "id": "one", "items": [{"type": "imageGeneration", "status": "completed",
                               "result": base64.b64encode(b"original").decode()}]}]}}
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=recovery)
    result = asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), state, lambda _: None, tmp_path))
    assert result["data"] == b"original"
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]
    recovery["thread"]["cwd"] = "/different-workspace"
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), state, lambda _: None, tmp_path))
    assert exc.value.code == "codex_thread_mismatch"


@pytest.mark.parametrize("stop", ["cancel", "timeout", "lost_turn_reply", "disconnect"])
def test_local_wait_abandonment_does_not_interrupt_upstream(tmp_path, monkeypatch, stop):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))

    async def scenario():
        checkpoint_ready = asyncio.Event()
        gate = (asyncio.Event(), asyncio.Event()) if stop == "lost_turn_reply" else None
        calls = fake_provider_rpc(monkeypatch, tmp_path, complete_image=False,
                                  disconnected=stop == "disconnect", turn_gate=gate)
        states = []

        def checkpoint(state):
            states.append(copy.deepcopy(state))
            if state.get("turn_id"):
                checkpoint_ready.set()

        task = asyncio.create_task(CodexProvider().generate(
            image_request({"prompt": "fixture"}), {}, checkpoint, tmp_path))
        if stop == "disconnect":
            with pytest.raises(UnknownOutcome):
                await task
        else:
            await asyncio.wait_for(gate[0].wait() if gate else checkpoint_ready.wait(), 1)
            if stop == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert states[-1]["submitted"] is True
        assert states[-1]["thread_id"] == "dedicated"
        assert not any(method == "turn/interrupt" for method, _ in calls)
        assert calls[-1][0] == "closed"
        if stop == "lost_turn_reply":
            assert "turn_id" not in states[-1]
    asyncio.run(scenario())


def cancellation_thread(cwd, turns):
    return {"thread": {"id": "dedicated", "cwd": str(cwd), "turns": turns}}


@pytest.mark.parametrize("known_turn", [True, False])
def test_explicit_cancel_resolves_and_interrupts_only_owned_active_turn(tmp_path, monkeypatch, known_turn):
    state = {"thread_id": "dedicated", "submitted": True}
    if known_turn:
        state["turn_id"] = "one"
    recovery = cancellation_thread(tmp_path, [
        {"id": "earlier", "status": "completed", "items": []},
        {"id": "one", "status": "inProgress", "items": []}])
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=recovery)
    receipt = asyncio.run(CodexProvider().cancel(state, tmp_path))
    assert receipt == {"thread_id": "dedicated", "turn_id": "one",
                       "turn_status": "inProgress", "interrupt_requested": True}
    assert calls == [
        ("thread/read", {"threadId": "dedicated", "includeTurns": True}),
        ("turn/interrupt", {"threadId": "dedicated", "turnId": "one"}), ("closed", {}),
    ]


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
@pytest.mark.parametrize("known_turn", [True, False])
def test_explicit_cancel_does_not_interrupt_terminal_turns(tmp_path, monkeypatch, status, known_turn):
    state = {"thread_id": "dedicated", **({"turn_id": "one"} if known_turn else {})}
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(
        tmp_path, [{"id": "one", "status": status, "items": []}]))
    receipt = asyncio.run(CodexProvider().cancel(state, tmp_path))
    assert receipt["turn_status"] == status
    assert receipt["interrupt_requested"] is False
    assert [method for method, _ in calls] == ["thread/read", "closed"]


@pytest.mark.parametrize("field,value", [("id", "somebody-elses-thread"), ("cwd", "/different-workspace")])
def test_explicit_cancel_rejects_foreign_thread_before_interrupt(tmp_path, monkeypatch, field, value):
    recovery = cancellation_thread(tmp_path, [{"id": "one", "status": "inProgress"}])
    recovery["thread"][field] = value
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=recovery)
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().cancel({"thread_id": "dedicated", "turn_id": "one"}, tmp_path))
    assert exc.value.code == "codex_thread_mismatch"
    assert [method for method, _ in calls] == ["thread/read", "closed"]


@pytest.mark.parametrize("turns,turn_id", [
    ([], None),
    ([{"id": "one", "status": "unknown"}], None),
    ([{"status": "inProgress"}], None),
    ([{"id": "other", "status": "inProgress"}], "original"),
    ([{"id": "one", "status": "inProgress"}, {"id": "two", "status": "inProgress"}], None),
    ([{"id": "one", "status": "completed"}, {"id": "two", "status": "failed"}], None),
    ([{"id": "one", "status": "inProgress"}, {"id": "one", "status": "inProgress"}], "one"),
    (None, None), ({}, None), ([None], None),
])
def test_explicit_cancel_unknown_history_never_guesses_or_creates_a_turn(tmp_path, monkeypatch, turns, turn_id):
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, turns))
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().cancel({"thread_id": "dedicated", "turn_id": turn_id}, tmp_path))
    assert [method for method, _ in calls] == ["thread/read", "closed"]


@pytest.mark.parametrize("state", [{}, {"thread_id": None}, {"thread_id": 123},
                                  {"thread_id": "dedicated", "turn_id": False}])
def test_explicit_cancel_requires_a_valid_original_identity(tmp_path, monkeypatch, state):
    calls = fake_provider_rpc(monkeypatch, tmp_path)
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().cancel(state, tmp_path))
    assert not calls


@pytest.mark.parametrize("method", ["thread/read", "turn/interrupt"])
def test_explicit_cancel_unknown_acknowledgement_stays_recoverable(tmp_path, monkeypatch, method):
    calls = fake_provider_rpc(monkeypatch, tmp_path, errors={method: asyncio.TimeoutError()},
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "inProgress"}]))
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().cancel({"thread_id": "dedicated", "turn_id": "one"}, tmp_path))
    assert not any(name in {"thread/start", "turn/start"} for name, _ in calls)
    assert calls[-1][0] == "closed"


def test_explicit_cancel_never_targets_a_newer_turn_when_original_is_terminal(tmp_path, monkeypatch):
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, [
        {"id": "one", "status": "interrupted"}, {"id": "two", "status": "inProgress"}]))
    receipt = asyncio.run(CodexProvider().cancel({"thread_id": "dedicated", "turn_id": "one"}, tmp_path))
    assert receipt["turn_id"] == "one"
    assert receipt["interrupt_requested"] is False
    assert [method for method, _ in calls] == ["thread/read", "closed"]


@pytest.mark.parametrize("status", ["inProgress", "interrupted"])
def test_recovery_observes_the_original_turn_without_reissuing_or_interrupting(tmp_path, monkeypatch, status):
    state = {"thread_id": "dedicated", "turn_id": "one", "submitted": True}
    calls = fake_provider_rpc(monkeypatch, tmp_path,
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": status, "items": []}]))
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), state, lambda _: None, tmp_path))
    assert exc.value.code == ("media_outcome_unknown" if status == "inProgress" else "media_cancelled")
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]


def test_recovery_returns_already_completed_image_even_after_explicit_interrupt(tmp_path, monkeypatch):
    calls = fake_provider_rpc(monkeypatch, tmp_path,
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "interrupted", "items": [
            {"type": "imageGeneration", "status": "completed",
             "result": base64.b64encode(b"completed-before-cancel").decode()}]}]))
    result = asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
        {"thread_id": "dedicated", "submitted": True}, lambda _: None, tmp_path))
    assert result["data"] == b"completed-before-cancel"
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]


def test_recovery_stops_original_active_turn_after_observing_one_completed_image(tmp_path, monkeypatch):
    calls = fake_provider_rpc(monkeypatch, tmp_path,
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "inProgress", "items": [
            {"type": "imageGeneration", "status": "completed",
             "result": base64.b64encode(b"completed-while-relay-disconnected").decode()}]}]))
    result = asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
        {"thread_id": "dedicated", "submitted": True}, lambda _: None, tmp_path))
    assert result["data"] == b"completed-while-relay-disconnected"
    assert calls == [
        ("account/read", {"refreshToken": False}),
        ("thread/read", {"threadId": "dedicated", "includeTurns": True}),
        ("turn/interrupt", {"threadId": "dedicated", "turnId": "one"}),
        ("closed", {}),
    ]


def test_recovery_does_not_recreate_a_submitted_task_missing_thread_identity(tmp_path, monkeypatch):
    calls = fake_provider_rpc(monkeypatch, tmp_path)
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().generate(
            image_request({"prompt": "fixture"}), {"submitted": True}, lambda _: None, tmp_path))
    assert not calls


@pytest.mark.parametrize("status", ["inProgress", "completed", "failed", "interrupted"])
@pytest.mark.parametrize("reverse_items", [True, False])
def test_recovery_completed_image_precedes_failed_item_cancel_and_turn_quota(
        tmp_path, monkeypatch, status, reverse_items):
    items = [
        {"type": "imageGeneration", "status": "completed", "result": base64.b64encode(b"keep-this-image").decode()},
        {"type": "imageGeneration", "status": "failed", "failure": {"type": "usageLimitExceeded"}},
    ]
    if reverse_items:
        items.reverse()
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, [{
        "id": "one", "status": status, "items": items, "error": {"codexErrorInfo": "usageLimitExceeded"},
    }]))
    result = asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
        {"thread_id": "dedicated", "turn_id": "one", "submitted": True}, lambda _: None, tmp_path))
    assert result["data"] == b"keep-this-image"
    assert sum(method == "turn/interrupt" for method, _ in calls) == (status == "inProgress")
    assert not any(method in {"thread/start", "turn/start"} for method, _ in calls)


@pytest.mark.parametrize("item_quota", [True, False])
@pytest.mark.parametrize("turn_quota", [True, False])
def test_recovery_interrupted_turn_precedes_failed_image_and_quota(
        tmp_path, monkeypatch, item_quota, turn_quota):
    item = {"type": "imageGeneration", "status": "failed"}
    if item_quota:
        item["failure"] = {"type": "usageLimitExceeded"}
    turn = {"id": "one", "status": "interrupted", "items": [item]}
    if turn_quota:
        turn["error"] = {"codexErrorInfo": "usageLimitExceeded"}
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, [turn]))
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
            {"thread_id": "dedicated", "turn_id": "one", "submitted": True}, lambda _: None, tmp_path))
    assert exc.value.code == "media_cancelled"
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]


@pytest.mark.parametrize("status", ["failed", "completed"])
@pytest.mark.parametrize("error,quota", [
    (None, False),
    ({"codexErrorInfo": "usageLimitExceeded"}, True),
    ({"message": "usageLimitExceeded"}, False),
])
@pytest.mark.parametrize("items", [[], [{"type": "imageGeneration", "status": "inProgress"}]])
def test_recovery_terminal_turn_without_completed_or_failed_image_is_not_unknown(
        tmp_path, monkeypatch, status, error, quota, items):
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, [{
        "id": "one", "status": status, "items": items, "error": error,
    }]))
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
            {"thread_id": "dedicated", "turn_id": "one", "submitted": True}, lambda _: None, tmp_path))
    expected = "media_quota_exhausted" if quota else (
        "image_generation_failed" if status == "failed" else "image_not_generated")
    assert exc.value.code == expected
    assert not isinstance(exc.value, UnknownOutcome)
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]


@pytest.mark.parametrize("quota_source", ["item", "turn", "none", "message_only"])
def test_recovery_failed_image_stops_active_turn_before_classifying_result(tmp_path, monkeypatch, quota_source):
    item = {"type": "imageGeneration", "status": "failed"}
    turn = {"id": "one", "status": "inProgress", "items": [item]}
    if quota_source == "item":
        item["failure"] = {"type": "usageLimitExceeded"}
    elif quota_source == "turn":
        turn["error"] = {"codexErrorInfo": "usageLimitExceeded"}
    elif quota_source == "message_only":
        turn["error"] = {"message": "usageLimitExceeded"}
    calls = fake_provider_rpc(monkeypatch, tmp_path, recovery=cancellation_thread(tmp_path, [turn]),
        interrupt_recovery=cancellation_thread(tmp_path, [{**turn, "status": "failed"}]))
    original_result = CodexProvider._result

    def interpret(*args):
        calls.append(("interpret_result", {}))
        return original_result(*args)

    monkeypatch.setattr(CodexProvider, "_result", staticmethod(interpret))
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
            {"thread_id": "dedicated", "turn_id": "one", "submitted": True}, lambda _: None, tmp_path))
    assert exc.value.code == ("media_quota_exhausted" if quota_source in {"item", "turn"} else "image_generation_failed")
    assert [method for method, _ in calls] == [
        "account/read", "thread/read", "turn/interrupt", "thread/read", "interpret_result", "closed"]


@pytest.mark.parametrize("item,expected", [
    ({"type": "imageGeneration", "status": "completed", "result": base64.b64encode(b"first-image").decode()}, None),
    ({"type": "imageGeneration", "status": "failed"}, "image_generation_failed"),
    ({"type": "imageGeneration", "status": "failed",
      "failure": {"type": "usageLimitExceeded"}}, "media_quota_exhausted"),
    ({"type": "imageGeneration", "status": "completed", "result": "not-base64"}, "invalid_image_result"),
])
def test_live_first_finished_image_stops_turn_before_interpretation_or_second_item(
        tmp_path, monkeypatch, item, expected):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    calls = fake_provider_rpc(monkeypatch, tmp_path, image_items=[
        item, {"type": "imageGeneration", "status": "completed",
               "result": base64.b64encode(b"must-not-use-second-image").decode()},
    ], interrupt_recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "interrupted", "items": [item]}]))
    original_result = CodexProvider._result

    def interpret(*args):
        calls.append(("interpret_result", {}))
        return original_result(*args)

    monkeypatch.setattr(CodexProvider, "_result", staticmethod(interpret))
    request = image_request({"prompt": "fixture"})
    if expected:
        with pytest.raises(MediaError) as exc:
            asyncio.run(CodexProvider().generate(request, {}, lambda _: None, tmp_path))
        assert exc.value.code == expected
    else:
        result = asyncio.run(CodexProvider().generate(request, {}, lambda _: None, tmp_path))
        assert result["data"] == b"first-image"
    expected_methods = ["account/read", "thread/start", "turn/start", "turn/interrupt"]
    if item["status"] != "completed":
        expected_methods.append("thread/read")
    assert [method for method, _ in calls] == [*expected_methods, "interpret_result", "closed"]


@pytest.mark.parametrize("status,error,expected", [
    ("interrupted", {"codexErrorInfo": "usageLimitExceeded"}, "media_cancelled"),
    ("failed", {"codexErrorInfo": "usageLimitExceeded"}, "media_quota_exhausted"),
    ("failed", None, "image_generation_failed"),
    ("completed", None, "image_not_generated"),
])
def test_live_terminal_turn_without_image_uses_same_failure_classification(
        tmp_path, monkeypatch, status, error, expected):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    calls = fake_provider_rpc(monkeypatch, tmp_path, complete_image=False,
        completed_turn={"id": "one", "status": status, "error": error})
    with pytest.raises(MediaError) as exc:
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert exc.value.code == expected
    assert [method for method, _ in calls] == ["account/read", "thread/start", "turn/start", "closed"]


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("interrupt_error", [
    asyncio.TimeoutError(), OSError("relay disconnected"), QuotaExceeded(),
    MediaError("codex_protocol_error", "Rejected interruption.", 502),
])
def test_failed_image_interrupt_failure_never_exposes_quota_fallback(
        tmp_path, monkeypatch, recover, interrupt_error):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    item = {"type": "imageGeneration", "status": "failed", "failure": {"type": "usageLimitExceeded"}}
    calls = fake_provider_rpc(monkeypatch, tmp_path, image_items=[item],
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "inProgress", "items": [item]}]),
        errors={"turn/interrupt": interrupt_error})
    state = {"thread_id": "dedicated", "turn_id": "one", "submitted": True} if recover else {}
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), state, lambda _: None, tmp_path))
    assert sum(method == "turn/interrupt" for method, _ in calls) == 1
    assert sum(method == "turn/start" for method, _ in calls) == (not recover)
    assert calls[-1][0] == "closed"


@pytest.mark.parametrize("confirm", ["active", "unknown", "no_turn", "different_turn", "different_owner", "read_timeout"])
def test_failed_image_interrupt_ack_requires_authoritative_terminal_readback(tmp_path, monkeypatch, confirm):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    item = {"type": "imageGeneration", "status": "failed", "failure": {"type": "usageLimitExceeded"}}
    turn = {"id": "one", "status": "inProgress" if confirm == "active" else "failed", "items": [item]}
    snapshot = cancellation_thread(tmp_path, [turn])
    if confirm == "unknown":
        turn["status"] = "unknown"
    elif confirm == "no_turn":
        snapshot["thread"]["turns"] = []
    elif confirm == "different_turn":
        turn["id"] = "some-other-turn"
    elif confirm == "different_owner":
        snapshot["thread"]["cwd"] = "/different-workspace"
    calls = fake_provider_rpc(monkeypatch, tmp_path, image_items=[item], interrupt_recovery=snapshot,
        errors={"thread/read": asyncio.TimeoutError()} if confirm == "read_timeout" else None)
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert [method for method, _ in calls] == [
        "account/read", "thread/start", "turn/start", "turn/interrupt", "thread/read", "closed"]


@pytest.mark.parametrize("status", ["failed", "completed", "interrupted"])
def test_failed_live_image_exposes_typed_quota_only_after_confirmed_terminal(tmp_path, monkeypatch, status):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    item = {"type": "imageGeneration", "status": "failed", "failure": {"type": "usageLimitExceeded"}}
    calls = fake_provider_rpc(monkeypatch, tmp_path, image_items=[item],
        interrupt_recovery=cancellation_thread(tmp_path, [{"id": "one", "status": status, "items": [item]}]))
    with pytest.raises(QuotaExceeded):
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert [method for method, _ in calls] == [
        "account/read", "thread/start", "turn/start", "turn/interrupt", "thread/read", "closed"]


@pytest.mark.parametrize("recover", [True, False])
def test_successful_image_survives_interrupt_failure_without_terminal_readback(tmp_path, monkeypatch, recover):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    item = {"type": "imageGeneration", "status": "completed",
            "result": base64.b64encode(b"preserve-real-image").decode()}
    calls = fake_provider_rpc(monkeypatch, tmp_path, image_items=[item],
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": "inProgress", "items": [item]}]),
        errors={"turn/interrupt": asyncio.TimeoutError()})
    state = {"thread_id": "dedicated", "turn_id": "one", "submitted": True} if recover else {}
    result = asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), state, lambda _: None, tmp_path))
    assert result["data"] == b"preserve-real-image"
    assert sum(method == "thread/read" for method, _ in calls) == recover
    assert calls[-1][0] == "closed"


@pytest.mark.parametrize("status", [None, "unknown"])
def test_recovery_failed_image_with_unknown_turn_status_does_not_expose_quota(tmp_path, monkeypatch, status):
    item = {"type": "imageGeneration", "status": "failed", "failure": {"type": "usageLimitExceeded"}}
    calls = fake_provider_rpc(monkeypatch, tmp_path,
        recovery=cancellation_thread(tmp_path, [{"id": "one", "status": status, "items": [item]}]))
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}),
            {"thread_id": "dedicated", "turn_id": "one", "submitted": True}, lambda _: None, tmp_path))
    assert [method for method, _ in calls] == ["account/read", "thread/read", "closed"]


@pytest.mark.parametrize("status", [None, "unknown", "inProgress"])
def test_live_turn_completion_notification_requires_terminal_status_before_quota(tmp_path, monkeypatch, status):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-home"))
    calls = fake_provider_rpc(monkeypatch, tmp_path, complete_image=False, completed_turn={
        "id": "one", "status": status, "error": {"codexErrorInfo": "usageLimitExceeded"}})
    with pytest.raises(UnknownOutcome):
        asyncio.run(CodexProvider().generate(image_request({"prompt": "fixture"}), {}, lambda _: None, tmp_path))
    assert [method for method, _ in calls] == ["account/read", "thread/start", "turn/start", "closed"]


def acceptance_script():
    path = Path(__file__).resolve().parents[1] / "scripts/verify-codex-media.py"
    spec = importlib.util.spec_from_file_location("verify_codex_media", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_acceptance_generation_uses_actual_router_enums():
    script = acceptance_script()
    endpoint, body = script.image_payload("generate")
    assert endpoint == "/v1/images/generations"
    assert image_request(body)["aspect_ratio"] == "square"
    endpoint, body = script.image_payload("edit")
    assert endpoint == "/v1/images/edits"
    assert body["response_format"] == "url"
    assert image_request(body)["aspect_ratio"] == "square"


def test_live_acceptance_report_never_serializes_ticket_or_image_body():
    import httpx
    script = acceptance_script()
    response = httpx.Response(200, headers={"x-request-id": "complete-request-id"},
        json={"id": "img_owned", "status": "completed", "data": [
            {"url": "/content?access=private-ticket", "b64_json": "private-image-body"}]})
    result = script.safe_response(response)
    text = json.dumps(result)
    assert "private-ticket" not in text
    assert "private-image-body" not in text
    assert result["request_id"] == "complete-request-id"
    assert result["data_formats"] == [["b64_json", "url"]]


@pytest.mark.parametrize("status,image_status,expected", [
    ("inProgress", None, True), ("inProgress", "inProgress", True),
    ("inProgress", "completed", False), ("inProgress", "failed", False),
    ("completed", None, False), ("interrupted", None, False), ("failed", None, False),
])
def test_restart_acceptance_requires_the_original_unfinished_turn(status, image_status, expected):
    script = acceptance_script()
    snapshot = {"turn_count": 1, "turns": [{"id": "one", "status": status,
        "image_generation_items": [{"status": image_status}] if image_status else []}]}
    assert script.restart_window_is_active(snapshot, "one") is expected
    assert script.restart_window_is_active(snapshot, "different-turn") is False
    snapshot["turn_count"] = 2
    assert script.restart_window_is_active(snapshot, "one") is False


def test_restart_thread_evidence_requires_full_workspace_and_omits_base64(tmp_path):
    script = acceptance_script()
    job = {"job_id": "img_one", "provider_state": {"thread_id": "owned"}}
    thread = {"id": "owned", "cwd": str(tmp_path), "status": {"type": "active"}, "turns": [{
        "id": "one", "status": "inProgress", "items": [
            {"type": "imageGeneration", "id": "image_one", "status": "inProgress",
             "result": "do-not-record-image-base64"}]}]}

    class RPC:
        async def call(self, method, params):
            assert method == "thread/read"
            assert params == {"threadId": "owned", "includeTurns": True}
            return {"thread": thread}

    result = asyncio.run(script.safe_owned_thread(RPC(), job, tmp_path))
    assert result["turn_count"] == result["image_generation_count"] == 1
    assert "do-not-record-image-base64" not in json.dumps(result)
    thread["cwd"] = str(tmp_path.parent / "foreign" / tmp_path.name)
    with pytest.raises(RuntimeError, match="ownership"):
        asyncio.run(script.safe_owned_thread(RPC(), job, tmp_path))
