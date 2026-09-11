from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.request
from contextlib import contextmanager
from uuid import uuid4


ENDPOINT = "https://ai-x10drg.taild500c8.ts.net:4001/mcp/h3"


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("credential_redirect_refused")


@contextmanager
def exclusive_update(private):
    path = private / ".configuration.lock"
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("private_configuration_identity_changed")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def check_token(token):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoCredentialRedirect())
    for identifier, method, parameters in (
        (1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "h3-private-configuration", "version": "1"}}),
        (2, "tools/list", {}),
    ):
        body = json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": parameters}).encode()
        request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
        with opener.open(request, timeout=15) as response:
            content = response.read(2 * 1024**2 + 1)
        if len(content) > 2 * 1024**2:
            raise ValueError("invalid_authentication_response")
        result = json.loads(content)
        if result.get("id") != identifier or result.get("error") or not isinstance(result.get("result"), dict):
            raise ValueError("authentication_failed")
        if identifier == 2:
            tools = result["result"].get("tools", [])
            if not isinstance(tools, list) or not {"h3_capabilities", "h3_save_draft", "h3_start_preview"} <= {
                    item.get("name") for item in tools if isinstance(item, dict)}:
                raise ValueError("video_tools_unavailable")


def save_token(private, token, verify=check_token):
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{32,512}", token):
        raise ValueError("invalid_private_token")
    private = Path(private)
    credential = private / "private-token.json"
    settings_path = private / "bridge-settings.json"
    receipt_path = private / "installation.json"
    for path in (private, credential, settings_path, receipt_path):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("private_configuration_identity_changed")
    configuration = json.loads(settings_path.read_text(encoding="utf-8"))
    installed = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (configuration.get("endpoint") != ENDPOINT or Path(configuration.get("credential_file", "")) != credential
            or installed.get("installed") is not True):
        raise ValueError("local_component_not_installed")
    with exclusive_update(private):
        before = credential.read_bytes()
        verify(token)
        temporary = private / (".token-" + uuid4().hex + ".json")
        try:
            with temporary.open("xb") as handle:
                handle.write((json.dumps({"token": token}) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            if credential.read_bytes() != before:
                raise ValueError("credential_changed_during_configuration")
            os.replace(temporary, credential)
        finally:
            temporary.unlink(missing_ok=True)
    return {"saved": True, "client_reconnect_required": True, "gpu_submissions": 0}


def main():
    if os.name != "nt":
        raise SystemExit("This private form is for the installed Windows component only.")
    import tkinter as tk
    from tkinter import messagebox
    from queue import Empty, SimpleQueue
    from threading import Thread

    root = tk.Tk()
    root.title("H3 本机连接设置")
    root.resizable(False, False)
    tk.Label(root, text="从 H3 设置页创建专用 Token，在此填写。\n不会显示旧 Token，也不会把凭证发到聊天。", padx=24, pady=16).pack()
    value = tk.StringVar()
    entry = tk.Entry(root, textvariable=value, show="*", width=58)
    entry.pack(padx=24, pady=8)

    results = SimpleQueue()

    def worker(token):
        try:
            save_token(Path(os.environ["USERPROFILE"]) / ".workbuddy/h3-bridge", token)
        except Exception:
            results.put(False)
        else:
            results.put(True)

    def poll():
        try:
            saved = results.get_nowait()
        except Empty:
            root.after(100, poll)
            return
        if saved:
            messagebox.showinfo("已保存", "已验证认证并保存到受保护的本机文件。\n请在 WorkBuddy 的 H3 MCP 上点击重连。\n只检查了工具列表，没有启动视频。")
            root.destroy()
            return
        messagebox.showerror("未保存", "凭证或连接检查未通过，原配置保持不变。请检查专用 Token、Tailscale 连接和组件安装状态。")
        button.configure(state="normal")

    def save():
        button.configure(state="disabled")
        token = value.get()
        value.set("")
        Thread(target=worker, args=(token,), daemon=True).start()
        root.after(100, poll)

    button = tk.Button(root, text="检查连接并保存 Token · 不生成视频", command=save)
    button.pack(padx=24, pady=16)
    entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    main()
