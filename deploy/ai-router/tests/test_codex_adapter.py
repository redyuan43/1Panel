from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from ai_router.codex_adapter import CodexGateway, create_app
from ai_router.codex_auth import CodexAccountStore


def _jwt(*, expires_at: int, account_id: str = "acct-test") -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'exp': expires_at, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
    )


def _write_auth(
    root: Path,
    *,
    access_token: str,
    refresh_token: str = "refresh-old",
) -> Path:
    path = root / "accounts" / "primary" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "account_id": "acct-test",
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def test_codex_account_refresh_rotates_token_atomically(
    tmp_path: Path,
) -> None:
    auth_path = _write_auth(
        tmp_path,
        access_token=_jwt(expires_at=int(time.time()) - 60),
    )
    refreshed_token = _jwt(expires_at=int(time.time()) + 3600)

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://auth.openai.com/oauth/token"
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": refreshed_token,
                "refresh_token": "refresh-new",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        store = CodexAccountStore(
            tmp_path / "accounts",
            client=client,
        )
        credentials = store.credentials("primary")

    assert credentials.access_token == refreshed_token
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "refresh-new"
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_codex_account_scan_ignores_inaccessible_entries(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "accounts" / "primary"
    primary.mkdir(parents=True)
    primary.chmod(0o000)
    try:
        store = CodexAccountStore(tmp_path / "accounts")
        assert store.aliases() == ()
    finally:
        primary.chmod(0o700)


def test_codex_adapter_catalog_chat_tools_and_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_auth(
        tmp_path,
        access_token=_jwt(expires_at=int(time.time()) + 3600),
    )
    captured: list[dict] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["chatgpt-account-id"] == "acct-test"
        assert request.headers["originator"] == "codex_cli_rs"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "context_window": 400000,
                        }
                    ]
                },
            )
        payload = json.loads(request.content)
        captured.append(payload)
        wire_name = payload["tools"][0]["name"]
        return httpx.Response(
            200,
            json={
                "id": "resp-sol",
                "output": [
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "encrypted_content": "sealed",
                    },
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "done"}
                        ],
                    },
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": wire_name,
                        "arguments": "{}",
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    monkeypatch.setenv("AI_ROUTER_CODEX_ADAPTER_KEY", "adapter-key")
    store = CodexAccountStore(tmp_path / "accounts")
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    gateway = CodexGateway(accounts=store, client=async_client)
    app = create_app(gateway)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.json()["safe_context_tokens"] == 272000
        response = client.post(
            "/v1/accounts/primary/chat/completions",
            headers={
                "Authorization": "Bearer adapter-key",
                "X-1Panel-Conversation-ID": "conversation-1",
            },
            json={
                "model": "gpt-5.6-sol",
                "messages": [
                    {"role": "system", "content": "Be precise."},
                    {"role": "user", "content": "Inspect the repo."},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "git.status",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                    }
                ],
            },
        )

    asyncio.run(async_client.aclose())
    assert health.json()["workers"][0]["ready"] is True
    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "done"
    assert message["tool_calls"][0]["function"]["name"] == "git.status"
    assert message["codex_reasoning_items"][0]["encrypted_content"] == "sealed"
    assert message["codex_message_items"][0]["id"] == "msg_1"
    assert captured[0]["model"] == "gpt-5.6-sol"
    assert captured[0]["reasoning"]["effort"] == "medium"
    assert captured[0]["prompt_cache_key"].startswith("pck_")
    assert captured[0]["tools"][0]["name"] != "git.status"
    assert captured[0]["tools"][0]["name"].replace("_", "").isalnum()


def test_codex_adapter_streams_chat_and_reasoning_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_auth(
        tmp_path,
        access_token=_jwt(expires_at=int(time.time()) + 3600),
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"models": [{"slug": "gpt-5.6-sol"}]},
            )
        stream = "\n\n".join(
            [
                'data: {"type":"response.created","response":{"id":"resp-stream"}}',
                'data: {"type":"response.output_text.delta","delta":"hello"}',
                (
                    'data: {"type":"response.completed","response":'
                    '{"id":"resp-stream","output":['
                    '{"id":"rs_1","type":"reasoning",'
                    '"encrypted_content":"sealed"},'
                    '{"id":"msg_1","type":"message","role":"assistant",'
                    '"status":"completed","content":['
                    '{"type":"output_text","text":"hello"}]}],'
                    '"usage":{"input_tokens":3,"output_tokens":1,'
                    '"total_tokens":4}}}'
                ),
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200,
            content=stream.encode(),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setenv("AI_ROUTER_CODEX_ADAPTER_KEY", "adapter-key")
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    gateway = CodexGateway(
        accounts=CodexAccountStore(tmp_path / "accounts"),
        client=async_client,
    )
    app = create_app(gateway)
    with TestClient(app) as client:
        response = client.post(
            "/v1/accounts/primary/chat/completions",
            headers={"Authorization": "Bearer adapter-key"},
            json={
                "model": "gpt-5.6-sol",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    asyncio.run(async_client.aclose())
    assert response.status_code == 200
    assert '"content":"hello"' in response.text
    assert "codex_reasoning_items" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


def test_codex_responses_string_input_is_normalized_to_a_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_auth(
        tmp_path,
        access_token=_jwt(expires_at=int(time.time()) + 3600),
    )
    captured = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"models": [{"slug": "gpt-5.6-sol"}]},
            )
        captured.update(json.loads(request.content))
        response = {
            "id": "resp-string",
            "output": [],
        }
        return httpx.Response(
            200,
            content=(
                "event: response.output_item.done\n"
                "data: "
                + json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "ok"}
                            ],
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n\n"
                + "event: response.completed\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": response,
                    },
                    separators=(",", ":"),
                )
                + "\n\n"
            ).encode(),
        )

    monkeypatch.setenv("AI_ROUTER_CODEX_ADAPTER_KEY", "adapter-key")
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    gateway = CodexGateway(
        accounts=CodexAccountStore(tmp_path / "accounts"),
        client=async_client,
    )
    with TestClient(create_app(gateway)) as client:
        response = client.post(
            "/v1/accounts/primary/responses",
            headers={"Authorization": "Bearer adapter-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "hello",
                "max_output_tokens": 64,
                "temperature": 0,
            },
        )
    asyncio.run(async_client.aclose())
    assert response.status_code == 200
    assert captured["input"] == [
        {"role": "user", "content": "hello"}
    ]
    assert response.json()["output"][0]["content"][0]["text"] == "ok"
    assert "max_output_tokens" not in captured
    assert "temperature" not in captured
