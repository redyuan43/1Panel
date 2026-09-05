#!/usr/bin/env python3
"""Operator acceptance through the installed Windows CLI, with private receipts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from manage import EVIDENCE, WINDOWS_SKILL, private_credentials, remote, report
from media import atomic_write, encode, secure_dir

STATE = EVIDENCE / "windows-live-state.json"
OUTPUT = "C:/Users/Ivan/Pictures/SIYUAN-Media-Acceptance-20260905"
PROMPTS = {
    "image": "A clear studio product photograph of one jade green ceramic mug with a small red square on its front, on a plain white table and background. No text.",
    "edit": "Change only the mug ceramic body from jade green to bright yellow. Preserve the small red square, handle, framing and white background. No text.",
    "video": "A single green ceramic mug standing still on a clean white table. The camera slowly pushes closer in a steady four second studio product shot. No text, no people, no music.",
}


def state():
    secure_dir(EVIDENCE)
    return json.loads(STATE.read_text()) if STATE.exists() else {"version": 1, "jobs": {}}


def save(value):
    atomic_write(STATE, encode(value))


def call(arguments, name, *, allow_error=False):
    credentials = private_credentials()
    command = subprocess.list2cmdline(["C:/Python312/python.exe", "-B",
                                      WINDOWS_SKILL + "/scripts/media.py", *arguments])
    started = time.monotonic()
    # Capture the full child output privately, before deciding what can be returned.
    from manage import SSH
    child = subprocess.run(SSH + [command], capture_output=True, timeout=90)
    raw = child.stdout + child.stderr
    assert credentials["api_key"].encode() not in raw
    output = json.loads(child.stdout.decode("utf-8-sig"))
    serialized = json.dumps(output)
    assert "b64_json" not in serialized and "?access=" not in serialized
    assert child.returncode == 0 or allow_error
    value = {"elapsed_seconds": round(time.monotonic() - started, 3),
             "exit_code": child.returncode, "output": output}
    report("windows-live-" + name, value)
    return value


def create(kind):
    value = state()
    item = value["jobs"].setdefault(kind, {"operation_id": uuid4().hex})
    save(value)
    if item.get("id"):
        return call(["status", "--job-id", item["id"]], kind + "-status")
    arguments = [kind, "--operation-id", item["operation_id"], "--prompt", PROMPTS[kind]]
    if kind == "edit":
        assert value["jobs"]["image"].get("downloaded")
        arguments += ["--image", OUTPUT + "/generated.png", "--aspect-ratio", "square", "--use-case", "product"]
    elif kind == "image":
        arguments += ["--aspect-ratio", "square", "--use-case", "product"]
    else:
        arguments += ["--strategy", "fast", "--duration", "4", "--confirm-context-cost"]
    result = call(arguments, kind + "-create")
    item.update(id=result["output"]["id"], create=result)
    save(value)
    assert result["output"]["http_status"] == 202
    return result


def wait(kind):
    value = state()
    item = value["jobs"][kind]
    result = call(["wait", "--job-id", item["id"], "--seconds", "30"], kind + "-wait")
    item["latest"] = result["output"]
    save(value)
    return result


def download(kind):
    value = state()
    item = value["jobs"][kind]
    if kind == "video":
        arguments = ["download", "--job-id", item["id"], "--stage", "context_ir",
                     "--output", OUTPUT + "/context-ir.txt"]
        filename = "context-ir.txt"
    else:
        filename = "generated.png" if kind == "image" else "edited.png"
        arguments = ["download", "--job-id", item["id"], "--output", OUTPUT + "/" + filename]
    result = call(arguments, kind + "-download")
    item["downloaded"] = result["output"]
    save(value)
    duplicate = call(arguments, kind + "-download-no-clobber", allow_error=True)
    assert duplicate["exit_code"] == 2
    assert duplicate["output"]["error"]["code"] == "output_exists"
    destination = EVIDENCE / filename
    copied = subprocess.run(["scp", "-o", "BatchMode=yes", f"ivan-laptop:{OUTPUT}/{filename}",
                             str(destination)], capture_output=True, timeout=90)
    assert copied.returncode == 0
    return {**result, "no_clobber_passed": True, "local_evidence": str(destination)}


def replay(kind):
    value = state()
    item = value["jobs"][kind]
    result = call(["resume", "--operation-id", item["operation_id"]], kind + "-replay")
    assert result["output"]["id"] == item["id"]
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "wait", "download", "replay"))
    parser.add_argument("kind", choices=("image", "edit", "video"))
    args = parser.parse_args()
    try:
        result = {"create": create, "wait": wait, "download": download, "replay": replay}[args.action](args.kind)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": "windows_acceptance_failed", "exception_type": type(exc).__name__,
                          "state": str(STATE), "message": "Reuse the original operation ID; no credentials were returned."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
