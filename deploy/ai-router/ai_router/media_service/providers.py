from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .contracts import (
    MediaError,
    QuotaExceeded,
    UnknownOutcome,
    decode_asset,
    legacy_video_enabled,
)


def image_prompt(body: dict) -> str:
    return (
        f"Use case: {body['use_case']}\n"
        f"Primary request: {body['prompt']}\n"
        f"Aspect ratio: {body['aspect_ratio']}\nBackground: {body['background']}\n"
        "Generate exactly one image using image_gen. Do not run commands or access other files. "
        "For editing, change only the requested elements and preserve all other content."
    )


class CodexRPC:
    MAX_REQUEST_BYTES = 60 * 1024 * 1024

    def __init__(self):
        self.process = None
        self.reader = None
        self.websocket = None
        self.connected = None
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()

    async def start(self):
        try:
            from websockets.client import ClientProtocol
            from websockets.uri import parse_uri
        except ImportError as exc:
            raise MediaError("codex_transport_unavailable", "Codex WebSocket transport is unavailable.", 503) from exc
        self.websocket = ClientProtocol(parse_uri("ws://localhost/"), max_size=192 * 1024 * 1024)
        self.connected = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "proxy", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=192 * 1024 * 1024,
        )
        # The proxy is a byte relay to a WebSocket control socket, not JSONL stdio.
        self.websocket.send_request(self.websocket.connect())
        await self._flush()
        self.reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.connected, 10)
        await self.call("initialize", {"clientInfo": {"name": "router-media", "version": "1.0"},
                                       "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized"})

    async def _flush(self):
        for data in self.websocket.data_to_send():
            if data:
                self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def send(self, value: dict):
        payload = json.dumps(value).encode()
        if len(payload) > self.MAX_REQUEST_BYTES:
            raise MediaError("codex_request_too_large", "Image references exceed Codex transport limits.", 413)
        async with self.send_lock:
            # Stay below the control socket's frame cap for multi-reference edits.
            step = 4 * 1024 * 1024
            self.websocket.send_text(payload[:step], fin=len(payload) <= step)
            for offset in range(step, len(payload), step):
                self.websocket.send_continuation(payload[offset:offset + step],
                                                  fin=offset + step >= len(payload))
            await self._flush()

    async def call(self, method: str, params: dict) -> dict:
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(key, None)

    async def _read(self):
        from websockets.exceptions import WebSocketException
        from websockets.frames import Frame, Opcode
        from websockets.http11 import Response
        fragments = []
        try:
            while chunk := await self.process.stdout.read(65536):
                self.websocket.receive_data(chunk)
                async with self.send_lock:
                    await self._flush()
                for event in self.websocket.events_received():
                    if isinstance(event, Response):
                        if self.websocket.handshake_exc:
                            raise ValueError("WebSocket handshake rejected")
                        if not self.connected.done():
                            self.connected.set_result(True)
                    elif isinstance(event, Frame):
                        if event.opcode == Opcode.CLOSE:
                            return
                        if event.opcode in (Opcode.PING, Opcode.PONG):
                            continue
                        if event.opcode == Opcode.BINARY:
                            raise ValueError("Unexpected binary JSON-RPC message")
                        fragments.append(event.data)
                        if event.fin:
                            value = json.loads(b"".join(fragments))
                            fragments.clear()
                            if not isinstance(value, dict):
                                raise ValueError("Unexpected JSON-RPC message")
                            await self._dispatch(value)
        except (ValueError, OSError, WebSocketException, asyncio.IncompleteReadError):
            pass
        finally:
            if not self.connected.done():
                self.connected.set_exception(UnknownOutcome("Codex WebSocket handshake failed."))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(UnknownOutcome())
            await self.events.put({"method": "disconnected"})

    async def _dispatch(self, value: dict):
        if "id" in value and "method" in value:
            # Never authorize a model-requested shell, connector, or permission escalation.
            await self.send({"id": value["id"], "error": {"code": -32601, "message": "Tool request denied"}})
        elif value.get("id") in self.pending:
            future = self.pending[value["id"]]
            if not future.done():
                if "error" in value:
                    error = value["error"] if isinstance(value["error"], dict) else {}
                    data = error.get("data") or {}
                    if isinstance(data, dict) and data.get("codexErrorInfo") == "usageLimitExceeded":
                        future.set_exception(QuotaExceeded())
                    else:
                        exc = MediaError("codex_protocol_error", "Codex request failed.", 502)
                        exc.rpc_code = error.get("code")
                        future.set_exception(exc)
                else:
                    future.set_result(value.get("result", {}))
        elif "method" in value:
            await self.events.put(value)

    async def close(self):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


class CodexProvider:
    async def generate(self, body: dict, state: dict, checkpoint: Callable, cwd: Path) -> dict:
        cwd = cwd.resolve()
        if state.get("submitted") and not state.get("thread_id"):
            raise UnknownOutcome("Submitted Codex task has no recoverable thread identity.")
        rpc = CodexRPC()
        try:
            await rpc.start()
            account = await rpc.call("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") not in {"chatgpt", "chatgptAuthTokens"}:
                raise MediaError("codex_login_required", "ChatGPT login is required.", 503)
            if state.get("thread_id"):
                result = await rpc.call("thread/read", {"threadId": state["thread_id"], "includeTurns": True})
                thread = self._owned_thread(result, state["thread_id"], cwd)
                turns = thread.get("turns", [])
                if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
                    raise UnknownOutcome("Existing Codex task has invalid turn history.")
                if state.get("turn_id"):
                    turns = [turn for turn in turns if turn.get("id") == state["turn_id"]]
                elif len(turns) > 1:
                    raise UnknownOutcome("Existing Codex task has ambiguous turn history.")
                for turn in reversed(turns):
                    items = turn.get("items", [])
                    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                        raise UnknownOutcome("Existing Codex task has invalid image history.")
                    images = [item for item in items if item.get("type") == "imageGeneration"]
                    completed = next((item for item in reversed(images) if item.get("status") == "completed"), None)
                    if completed is not None:
                        if turn.get("status") == "inProgress" and isinstance(turn.get("id"), str) and turn["id"]:
                            await self._stop_after_image(rpc, state["thread_id"], turn["id"])
                        return self._result(completed)
                    if turn.get("status") == "interrupted":
                        self._raise_turn_failure(turn)
                    failed = next((item for item in reversed(images) if item.get("status") == "failed"), None)
                    if failed is not None:
                        if turn.get("status") == "inProgress":
                            if not isinstance(turn.get("id"), str) or not turn["id"]:
                                raise UnknownOutcome("Failed Codex image has no authoritative turn identity.")
                            await self._stop_after_image(rpc, state["thread_id"], turn["id"],
                                                         require_terminal=True, cwd=cwd)
                        elif turn.get("status") not in {"failed", "completed"}:
                            raise UnknownOutcome("Failed Codex image has no authoritative terminal turn.")
                        return self._result(failed, turn.get("error"))
                    if turn.get("status") in {"failed", "completed"}:
                        self._raise_turn_failure(turn)
                raise UnknownOutcome("Existing Codex task has no recoverable terminal image yet.")
            thread = await rpc.call("thread/start", self._thread_params(cwd))
            self._verify_permissions(thread, cwd)
            state = {**state, "thread_id": thread["thread"]["id"], "submitted": False}
            checkpoint(state)
            inputs = [{"type": "text", "text": "$imagegen\n" + image_prompt(body)}]
            skill = str(self._skill_path())
            inputs.append({"type": "skill", "name": "imagegen", "path": skill})
            for asset in body["images"]:
                inputs.append({"type": "image", "url": f"data:{asset['content_type']};base64,{asset['data']}"})
            # Persist intent before dispatch: a lost reply must never create another turn.
            state["submitted"] = True
            checkpoint(state)
            turn = await rpc.call("turn/start", {
                "threadId": state["thread_id"], "clientUserMessageId": cwd.name, "input": inputs,
            })
            state["turn_id"] = turn["turn"]["id"]
            checkpoint(state)
            while True:
                event = await rpc.events.get()
                if event["method"] == "disconnected":
                    raise UnknownOutcome()
                params = event.get("params", {})
                if params.get("threadId") != state["thread_id"]:
                    continue
                item = params.get("item", {})
                if event["method"] == "item/completed" and item.get("type") == "imageGeneration":
                    await self._stop_after_image(rpc, state["thread_id"], state["turn_id"],
                                                 require_terminal=item.get("status") != "completed", cwd=cwd)
                    return self._result(item)
                if event["method"] == "turn/completed":
                    self._raise_turn_failure(params.get("turn") or {})
        finally:
            # A timeout or service shutdown abandons the relay, not the upstream turn.
            await rpc.close()

    async def cancel(self, state: dict, cwd: Path) -> dict:
        thread_id = state.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise UnknownOutcome("Codex cancellation has no recoverable thread identity.")
        turn_id = state.get("turn_id")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise UnknownOutcome("Codex cancellation has an invalid turn identity.")
        rpc = CodexRPC()
        try:
            try:
                await rpc.start()
                result = await rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})
            except (MediaError, OSError, asyncio.TimeoutError) as exc:
                raise UnknownOutcome("Codex cancellation could not read the original task.") from exc
            thread = self._owned_thread(result, thread_id, cwd)
            turns = thread.get("turns")
            if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
                raise UnknownOutcome("Codex cancellation has no authoritative turn history.")
            if turn_id:
                candidates = [turn for turn in turns if turn.get("id") == turn_id]
            else:
                candidates = [turn for turn in turns if turn.get("status") == "inProgress"]
                if not candidates and len(turns) == 1:
                    candidates = turns
            if len(candidates) != 1:
                raise UnknownOutcome("Codex cancellation cannot identify exactly one original turn.")
            turn = candidates[0]
            turn_id = turn.get("id")
            status = turn.get("status")
            if not isinstance(turn_id, str) or not turn_id or status not in {
                "inProgress", "completed", "interrupted", "failed",
            }:
                raise UnknownOutcome("Codex cancellation has an unknown turn state.")
            requested = status == "inProgress"
            if requested:
                try:
                    await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                except (MediaError, OSError, asyncio.TimeoutError) as exc:
                    raise UnknownOutcome("Codex cancellation acknowledgement is unknown.") from exc
            return {"thread_id": thread_id, "turn_id": turn_id,
                    "turn_status": status, "interrupt_requested": requested}
        finally:
            await rpc.close()

    @staticmethod
    def _owned_thread(response: dict, thread_id: str, cwd: Path) -> dict:
        thread = response.get("thread") if isinstance(response, dict) else None
        if not isinstance(thread, dict):
            raise UnknownOutcome("Codex did not return authoritative thread metadata.")
        if thread.get("id") != thread_id or thread.get("cwd") != str(cwd.resolve()):
            raise MediaError("codex_thread_mismatch", "Codex task does not match its media workspace.", 409)
        return thread

    @staticmethod
    async def _stop_after_image(rpc: CodexRPC, thread_id: str, turn_id: str,
                                *, require_terminal: bool = False, cwd: Path | None = None):
        if require_terminal and cwd is None:
            raise UnknownOutcome("Failed Codex image has no workspace identity for reconciliation.")
        try:
            await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except Exception as exc:
            if require_terminal:
                raise UnknownOutcome("Failed Codex image interruption is unconfirmed.") from exc
            # A successful image remains usable even if final turn cleanup races.
            return
        if require_terminal:
            try:
                response = await rpc.call("thread/read", {"threadId": thread_id, "includeTurns": True})
                thread = CodexProvider._owned_thread(response, thread_id, cwd)
            except Exception as exc:
                raise UnknownOutcome("Failed Codex image terminal state is unconfirmed.") from exc
            turns = thread.get("turns")
            if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
                raise UnknownOutcome("Failed Codex image has no authoritative turn history.")
            original = [turn for turn in turns if turn.get("id") == turn_id]
            if len(original) != 1 or original[0].get("status") not in {"completed", "interrupted", "failed"}:
                raise UnknownOutcome("Failed Codex image original turn may still be running.")

    @staticmethod
    def _raise_turn_failure(turn: dict):
        if turn.get("status") == "interrupted":
            raise MediaError("media_cancelled", "Image turn was interrupted.", 409)
        if turn.get("status") not in {"failed", "completed"}:
            raise UnknownOutcome("Codex turn has no authoritative terminal state.")
        error = turn.get("error") or {}
        if isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded":
            raise QuotaExceeded()
        if turn.get("status") == "failed":
            raise MediaError("image_generation_failed", "Codex image turn failed.", 502)
        raise MediaError("image_not_generated", "Codex completed without an image.", 502)

    @staticmethod
    def _result(item: dict, turn_error: dict | None = None) -> dict:
        if ((item.get("failure") or {}).get("type") == "usageLimitExceeded"
                or isinstance(turn_error, dict) and turn_error.get("codexErrorInfo") == "usageLimitExceeded"):
            raise QuotaExceeded()
        if item.get("status") != "completed" or not item.get("result"):
            raise MediaError("image_generation_failed", "Image generation failed.", 502)
        try:
            data = base64.b64decode(item["result"], validate=True)
        except (ValueError, TypeError) as exc:
            raise MediaError("invalid_image_result", "Invalid generated image data.", 502) from exc
        return {"data": data, "revised_prompt": item.get("revisedPrompt"), "provider": "codex"}

    @staticmethod
    def _skill_path() -> Path:
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        return Path(os.environ.get("AI_ROUTER_IMAGEGEN_SKILL", str(root / "skills/.system/imagegen/SKILL.md"))).resolve()

    @classmethod
    def _thread_params(cls, cwd: Path) -> dict:
        cwd = cwd.resolve()
        return {
            "cwd": str(cwd), "ephemeral": False, "approvalPolicy": "never",
            "permissions": "router_media", "runtimeWorkspaceRoots": [str(cwd)],
            "environments": [], "dynamicTools": [], "selectedCapabilityRoots": [],
            "modelProvider": "openai", "allowProviderModelFallback": False,
            "historyMode": "legacy", "config": cls._isolated_config(cwd),
            "developerInstructions": (
                "Call imagegen exactly once and stop after that call. No iterations, filesystem "
                "exploration, commands, connectors, network access, or permission requests. "
                "Use only the supplied image references. Never use an API key or read credentials."
            ),
        }

    @staticmethod
    def _verify_permissions(response: dict, cwd: Path) -> dict:
        expected_cwd = str(cwd.resolve())
        sandbox = response.get("sandbox") or {}
        profile = response.get("activePermissionProfile") or {}
        thread = response.get("thread") or {}
        roots = response.get("runtimeWorkspaceRoots")
        # Validate the server's effective result, never just the requested config.
        if (not all(isinstance(value, dict) for value in (sandbox, profile, thread))
                or profile.get("id") != "router_media" or profile.get("extends") is not None
                or response.get("approvalPolicy") != "never"
                or response.get("modelProvider") != "openai"
                or response.get("cwd") != expected_cwd
                or thread.get("cwd") != expected_cwd
                or thread.get("ephemeral") is not False
                or not thread.get("id")
                or roots not in ([], [expected_cwd])
                or sandbox.get("type") != "workspaceWrite"
                or sandbox.get("networkAccess") is not False
                or sandbox.get("excludeTmpdirEnvVar") is not True
                or sandbox.get("excludeSlashTmp") is not True
                or sandbox.get("writableRoots") not in ([], [expected_cwd])):
            raise MediaError("codex_permissions_unverified",
                             "Codex did not confirm the required media isolation; no generation was submitted.", 503)
        return {"profile": "router_media", "cwd": expected_cwd, "network_access": False,
                "extra_writable_roots": False, "temporary_directories_writable": False}

    @staticmethod
    def _isolated_config(cwd: Path) -> dict:
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        config_file = root / "config.toml"
        config = tomllib.loads(config_file.read_text()) if config_file.exists() else {}
        return {
            # Prevent app-server from auto-persisting trust for each new writable cwd.
            "projects": {str(cwd.resolve()): {"trust_level": "untrusted"}},
            "default_permissions": "router_media",
            "permissions": {"router_media": {
                "filesystem": {":minimal": "read", str(cwd): "write",
                               str(CodexProvider._skill_path().parent): "read"},
                "network": {"enabled": False},
            }},
            "features": {
                "image_generation": True, "shell_tool": False, "unified_exec": False,
                "apply_patch_freeform": False, "apps": False, "plugins": False,
                "multi_agent": False, "multi_agent_v2": False, "collab": False,
                "web_search": False, "standalone_web_search": False, "browser_use": False,
                "computer_use": False, "js_repl": False, "code_mode": False,
                "code_mode_only": False, "code_mode_host": False,
                "memories": False, "memory_tool": False, "hooks": False,
                "codex_hooks": False, "plugin_hooks": False, "goals": False,
                "request_permissions": False, "request_permissions_tool": False,
                "skill_mcp_dependency_install": False, "skill_env_var_dependency_prompt": False,
            },
            "mcp_servers": {name: {"enabled": False} for name in config.get("mcp_servers", {})},
            "plugins": {name: {"enabled": False} for name in config.get("plugins", {})},
            "apps": {"_default": {"enabled": False}},
            "memories": {"generate_memories": False, "use_memories": False},
            "notify": [], "project_doc_max_bytes": 0,
            "web_search": "disabled",
        }


class QwenProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def generate(self, body: dict, state: dict, checkpoint: Callable, cwd: Path) -> dict:
        if len(body["images"]) > 3 or body["background"] == "transparent":
            raise MediaError("fallback_incompatible", "Paid fallback cannot preserve these image requirements.", 422)
        base = os.environ.get("AI_ROUTER_DASHSCOPE_BASE_URL", "").rstrip("/")
        parsed = urlparse(base)
        key = os.environ.get("AI_ROUTER_DASHSCOPE_API_KEY", "")
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".maas.aliyuncs.com") or parsed.path or not key:
            raise MediaError("qwen_not_configured", "Qwen workspace endpoint and credential are required.", 503)
        headers = {"Authorization": f"Bearer {key}", "X-DashScope-Async": "enable"}
        task_id = state.get("task_id")
        if not task_id:
            if state.get("submitted"):
                raise UnknownOutcome()
            content = [{"image": f"data:{asset['content_type']};base64,{asset['data']}"} for asset in body["images"]]
            content.append({"text": image_prompt(body)})
            checkpoint({**state, "submitted": True})
            try:
                response = await self.client.post(
                    base + "/api/v1/services/aigc/image-generation/generation", headers=headers,
                    json={"model": "qwen-image-3.0-pro", "input": {"messages": [{"role": "user", "content": content}]},
                          "parameters": {"n": 1}}, timeout=30,
                )
            except httpx.HTTPError as exc:
                raise UnknownOutcome() from exc
            if response.status_code >= 400:
                raise MediaError("qwen_request_failed", "Qwen rejected the image request.", 502)
            task_id = response.json().get("output", {}).get("task_id")
            if not task_id:
                raise UnknownOutcome()
            checkpoint({"task_id": task_id, "submitted": True})
        while True:
            response = await self.client.get(base + f"/api/v1/tasks/{task_id}", headers=headers, timeout=30)
            response.raise_for_status()
            output = response.json().get("output", {})
            if output.get("task_status") == "SUCCEEDED":
                urls = [item.get("url") for item in output.get("results", [])]
                urls += [part.get("image") for choice in output.get("choices", [])
                         for part in choice.get("message", {}).get("content", [])]
                url = next((item for item in urls if item), None)
                return {"url": url, "provider": "qwen"} if url else self._invalid_result()
            if output.get("task_status") in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise MediaError("qwen_generation_failed", "Qwen image task failed.", 502)
            await asyncio.sleep(3)

    @staticmethod
    def _invalid_result():
        raise MediaError("invalid_image_result", "Qwen returned no image.", 502)


class H3Provider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.base = os.environ.get("AI_ROUTER_H3_URL", "http://edge.taild500c8.ts.net:8789").rstrip("/")
        self.executor_base = os.environ.get(
            "AI_ROUTER_H3_EXECUTOR_URL",
            "http://100.96.79.21:8789",
        ).rstrip("/")
        for value in (self.base, self.executor_base):
            parsed = urlparse(value)
            try:
                address = ipaddress.ip_address(parsed.hostname or "")
            except ValueError:
                address = None
            private = (
                (parsed.hostname or "").endswith(".taild500c8.ts.net")
                or parsed.hostname in {"127.0.0.1", "localhost"}
                or bool(address and (
                    address.is_loopback
                    or address in ipaddress.ip_network("100.64.0.0/10")
                ))
            )
            if parsed.scheme not in {"http", "https"} or not private or parsed.path or parsed.username or parsed.password:
                raise ValueError("H3 must be a private direct endpoint")

    async def call(self, method: str, path: str, *, executor=False, **kwargs) -> dict:
        key = os.environ.get("AI_ROUTER_H3_KEY", "")
        if not key:
            raise MediaError("h3_not_configured", "H3 contract credential is required.", 503)
        base = self.executor_base if executor else self.base
        try:
            response = await self.client.request(
                method, base + "/api/router" + path,
                headers={"Authorization": f"Bearer {key}"}, timeout=30, **kwargs,
            )
        except httpx.HTTPError as exc:
            raise UnknownOutcome() from exc
        if response.status_code == 409:
            raise MediaError("stale_stage_output", "Stage state or output version has changed.", 409)
        if response.status_code == 503:
            raise MediaError("h3_unavailable", "No eligible Ivan H3 execution lane is available.", 503)
        if response.status_code >= 400:
            detail = None
            try:
                payload = response.json()
                value = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(value, str):
                    detail = value[:300]
            except (ValueError, TypeError):
                pass
            raise MediaError(
                "h3_request_failed",
                "H3 contract request failed.",
                502,
                upstream_status=response.status_code,
                **({"upstream_detail": detail} if detail else {}),
            )
        return response.json()

    async def options(self) -> dict:
        result = await self.call("GET", "/options", executor=True)
        if result.get("contract_version") != 1:
            raise MediaError("h3_contract_mismatch", "H3 versioned media contract is required.", 503)
        return result

    @staticmethod
    def require_legacy() -> None:
        if not legacy_video_enabled():
            raise MediaError(
                "workflow_unavailable",
                "The Edge H3 pipeline is retired; use direct Ivan execution.",
                409,
            )

    async def legacy_options(self) -> dict:
        self.require_legacy()
        result = await self.call("GET", "/options")
        if result.get("contract_version") != 1:
            raise MediaError("h3_contract_mismatch", "H3 versioned media contract is required.", 503)
        return result

    async def create(self, job: dict) -> dict:
        self.require_legacy()
        await self.legacy_options()
        body = job["request"]
        legacy_fields = {
            "name", "prompt", "mode", "strategy", "duration", "seed", "audio_policy",
            "watermark", "use_embedded_video_audio",
        }
        data = {key: str(value).lower() if type(value) is bool else str(value)
                for key, value in body.items() if key in legacy_fields}
        data["operation_id"] = job["id"] + "_create"
        extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                      "video/mp4": ".mp4", "video/webm": ".webm", "audio/wav": ".wav",
                      "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/flac": ".flac"}
        files = {name: (name + extensions.get(asset["content_type"], ".bin"), decode_asset(asset), asset["content_type"])
                 for name, asset in body["assets"].items()}
        return await self.call("POST", "/projects", data=data, files=files or None)

    async def get(self, project_id: str) -> dict:
        self.require_legacy()
        return await self.call("GET", f"/projects/{project_id}")

    async def action(self, project_id: str, stage: str, action: str, operation: dict) -> dict:
        self.require_legacy()
        return await self.call("POST", f"/projects/{project_id}/stages/{stage}/{action}", json=operation)

    async def download(self, project_id: str, output_id: str):
        self.require_legacy()
        key = os.environ.get("AI_ROUTER_H3_KEY", "")
        return self.client.stream(
            "GET", self.base + f"/api/router/projects/{project_id}/outputs/{output_id}",
            headers={"Authorization": f"Bearer {key}"}, timeout=120,
        )

    async def create_execution(self, body: dict, assets: dict[str, dict]) -> dict:
        capabilities = await self.options()
        if int(capabilities.get("workflow_contract_version", 0)) < 2:
            raise MediaError("h3_contract_mismatch", "H3 managed execution contract v2 is required.", 503)
        data = {
            key: json.dumps(value, ensure_ascii=False) if key == "metadata" else (
                str(value).lower() if type(value) is bool else str(value)
            )
            for key, value in body.items()
        }
        extensions = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "video/mp4": ".mp4", "video/webm": ".webm", "audio/wav": ".wav",
            "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            "audio/flac": ".flac",
        }
        files = {
            name: (
                name + extensions.get(asset["content_type"], ".bin"),
                decode_asset(asset),
                asset["content_type"],
            )
            for name, asset in assets.items()
        }
        return await self.call(
            "POST",
            "/executions",
            executor=True,
            data=data,
            files=files or None,
        )

    async def get_execution(self, execution_id: str) -> dict:
        return await self.call("GET", f"/executions/{execution_id}", executor=True)

    async def cancel_execution(self, execution_id: str, operation_id: str) -> dict:
        return await self.call(
            "POST",
            f"/executions/{execution_id}/cancel",
            executor=True,
            json={"operation_id": operation_id},
        )

    async def download_execution(self, execution_id: str):
        key = os.environ.get("AI_ROUTER_H3_KEY", "")
        return self.client.stream(
            "GET",
            self.executor_base + f"/api/router/executions/{execution_id}/output",
            headers={"Authorization": f"Bearer {key}"},
            timeout=300,
        )
