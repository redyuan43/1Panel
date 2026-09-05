#!/usr/bin/env python3
"""Probe shared Codex without inference by default; explicitly opt into Router image acceptance."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_router.media_service.providers import CodexProvider, CodexRPC
from ai_router.media_service.contracts import image_request


def save_report(path: Path, value: dict):
    pending = path.with_suffix(".pending")
    with pending.open("w", encoding="utf-8") as handle:
        os.chmod(pending, 0o600)
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    pending.replace(path)


def acceptance_credentials(path: Path) -> dict:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"MEDIA_CLIENT_KEY", "MEDIA_OTHER_KEY"}:
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                result[key.strip()] = parts[0]
    if not result.get("MEDIA_CLIENT_KEY") or not result.get("MEDIA_OTHER_KEY"):
        raise RuntimeError("Both private acceptance client keys are required.")
    return result


def stored_job(db_path: Path, idem: str) -> dict | None:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
        row = db.execute(
            "SELECT id, json_extract(value,'$.status'), json_extract(value,'$.request_id'),"
            "json_extract(value,'$.provider_state'), json_extract(value,'$.error'),"
            "json_extract(value,'$.created_at'), json_extract(value,'$.updated_at'),"
            "json_extract(value,'$.output') FROM jobs WHERE kind='image' AND idem=?", (idem,),
        ).fetchall()
    if len(row) > 1:
        raise RuntimeError("Ambiguous acceptance idempotency key; will not submit.")
    if not row:
        return None
    item = row[0]
    return {"job_id": item[0], "status": item[1], "request_id": item[2],
            "provider_state": json.loads(item[3] or "{}"), "error": json.loads(item[4] or "null"),
            "created_at": item[5], "updated_at": item[6],
            "output": json.loads(item[7] or "null")}


def safe_response(response: httpx.Response) -> dict:
    summary = {"http_status": response.status_code, "request_id": response.headers.get("x-request-id")}
    try:
        body = response.json()
    except ValueError:
        return summary
    summary.update({key: body[key] for key in ("id", "status", "model", "error") if key in body})
    summary["data_formats"] = [sorted(key for key in item if key in {"url", "b64_json"}) for item in body.get("data", [])]
    return summary


def image_evidence(data: bytes) -> dict:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return {"format": image.format, "width": image.width, "height": image.height,
                "mode": image.mode, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def record_visual_review(root: Path, action: str, note: str):
    report_path = root / "images" / f"{action}.json"
    report = json.loads(report_path.read_text())
    actual = image_evidence((root / "images" / f"{action}.png").read_bytes())
    if actual["sha256"] != report["artifact"]["base64"]["sha256"]:
        raise RuntimeError("The reviewed artifact no longer matches the verification report.")
    report["visual_review"] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "method": "Codex view_image inspection of the real downloaded PNG",
        "sha256": actual["sha256"], "note": note,
    }
    save_report(report_path, report)
    print(json.dumps({"report": str(report_path), "visual_review_recorded": True}))


def image_payload(action: str) -> tuple[str, dict]:
    if action == "generate":
        return "/v1/images/generations", {
            "model": "siyuan-image", "use_case": "photo", "aspect_ratio": "square", "background": "opaque",
            "response_format": "b64_json", "n": 1,
            "prompt": ("A clean studio product photograph of exactly one simple cobalt blue glazed ceramic mug "
                       "with a round handle on the right, centered on a plain light gray tabletop and matching "
                       "background. Soft natural lighting and a small soft shadow. No text, logos, props or hands."),
        }
    return "/v1/images/edits", {
        "model": "siyuan-image", "use_case": "photo", "aspect_ratio": "square", "background": "opaque",
        "response_format": "url", "n": 1,
        "prompt": ("Change only the mug's glazed ceramic color from cobalt blue to vivid emerald green. "
                   "Preserve the mug shape, right-side handle, exact camera framing, composition, highlights, "
                   "lighting, light gray background and tabletop, and shadow. Do not add or remove anything."),
    }


async def submit_image(client, action, payload, headers, root):
    endpoint, _ = image_payload(action)
    if action == "generate":
        return await client.post(endpoint, json=payload, headers=headers)
    source = root / "generate.png"
    with source.open("rb") as handle:
        return await client.post(endpoint, data={key: str(value) for key, value in payload.items()},
                                 files={"image": ("blue-mug.png", handle, "image/png")}, headers=headers)


async def verify_image_artifact(client, other_key, job, response, root, action) -> dict:
    job_id = job["job_id"]
    metadata = await client.get(f"/v1/images/{job_id}")
    metadata.raise_for_status()
    body = metadata.json()
    base64_image = base64.b64decode(body["data"][0]["b64_json"], validate=True)
    evidence = {"base64": image_evidence(base64_image), "checks": {}}
    if evidence["base64"]["format"] != "PNG":
        raise RuntimeError("Actual provider output is not PNG.")
    output = root / f"{action}.png"
    with output.open("wb") as handle:
        os.chmod(output, 0o600)
        handle.write(base64_image)
    expected_hash = job["output"]["sha256"]
    evidence["checks"]["base64_matches_archive"] = evidence["base64"]["sha256"] == expected_hash
    url = (response.json()["data"][0].get("url") if response is not None
           and response.status_code == 200 and action == "edit" else None) or body["output"]["content_url"]
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/v1/media/outputs/"):
        raise RuntimeError("Unexpected artifact URL; credentials will not be sent.")
    async with httpx.AsyncClient(base_url=str(client.base_url), trust_env=False, timeout=30) as public:
        download = await public.get(url)
        head = await public.head(url)
        partial = await public.get(url, headers={"Range": "bytes=0-63"})
    evidence["url"] = {"http_status": download.status_code, **image_evidence(download.content)}
    evidence["head"] = {"http_status": head.status_code, "content_length": head.headers.get("content-length"),
                        "body_bytes": len(head.content), "request_id": head.headers.get("x-request-id")}
    evidence["range"] = {"http_status": partial.status_code, "content_range": partial.headers.get("content-range"),
                         "bytes": len(partial.content), "request_id": partial.headers.get("x-request-id")}
    evidence["checks"].update({
        "url_matches_base64": download.status_code == 200 and download.content == base64_image,
        "head_matches_bytes": head.status_code == 200 and len(head.content) == 0
            and head.headers.get("content-length") == str(len(base64_image)),
        "range_matches_bytes": partial.status_code == 206 and partial.content == base64_image[:64],
    })
    auth_content = await client.get(f"/v1/images/{job_id}/content")
    evidence["checks"]["authenticated_content"] = auth_content.status_code == 200 and auth_content.content == base64_image
    foreign_headers = {"Authorization": "Bearer " + other_key}
    denied = {}
    for suffix in ("", "/content", "/outputs"):
        foreign = await client.get(f"/v1/images/{job_id}{suffix}", headers=foreign_headers)
        denied[suffix or "job"] = {"status": foreign.status_code, "request_id": foreign.headers.get("x-request-id")}
    direct_foreign = await client.get(parsed.path, headers=foreign_headers)
    denied["direct_output_without_ticket"] = {
        "status": direct_foreign.status_code, "request_id": direct_foreign.headers.get("x-request-id")}
    evidence["ownership"] = denied
    evidence["checks"]["other_client_denied"] = all(item["status"] in (403, 404) for item in denied.values())
    evidence["passed"] = all(evidence["checks"].values())
    return evidence


async def real_image_probe(args) -> int:
    root = args.report_dir.expanduser().resolve() / "images"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    action = args.router_action
    report_path = root / f"{action}.json"
    idem = f"media-acceptance-20260904-codex-mug-{action}-v1"
    _, payload = image_payload(action)
    credentials = acceptance_credentials(args.acceptance_env.expanduser())
    parsed_base = urlparse(args.router_base)
    if parsed_base.scheme != "http" or parsed_base.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Live acceptance only permits the explicitly authorized loopback Router.")
    original = json.loads(report_path.read_text()) if report_path.exists() else {}
    report = {**original, "action": action, "idempotency_key": idem,
              "payload": payload, "started_at": original.get("started_at", time.time()),
              "passed": False, "reference_count": 1 if action == "edit" else 0}
    account_rpc = CodexRPC()
    try:
        await account_rpc.start()
        account = (await account_rpc.call("account/read", {"refreshToken": False})).get("account") or {}
        report["account"] = {key: account[key] for key in ("type", "planType") if key in account}
        if account.get("type") not in {"chatgpt", "chatgptAuthTokens"}:
            raise RuntimeError("Live acceptance requires existing ChatGPT authentication.")
        report["billing_scope"] = "Account type/plan only; the specific quota billing bucket is not proven."
    finally:
        await account_rpc.close()
    job = stored_job(args.media_db, idem)
    previous_response = original.get("submission_response") or {}
    validation_rejected = (previous_response.get("http_status") == 400
                           and (previous_response.get("error") or {}).get("code") == "invalid_media_parameters")
    if original.get("submitted") and not job and not validation_rejected:
        raise RuntimeError("Prior submission has no visible task; investigate without resubmitting.")
    if not job and validation_rejected:
        report["rejected_without_job"] = [*original.get("rejected_without_job", []), previous_response]
    if action == "edit" and not (root / "generate.png").is_file():
        raise RuntimeError("The original generated PNG must be verified before editing.")
    async with httpx.AsyncClient(
        base_url=args.router_base, headers={"Authorization": "Bearer " + credentials["MEDIA_CLIENT_KEY"]},
        trust_env=False, timeout=httpx.Timeout(800, connect=10),
    ) as client:
        response = None
        pending = None
        if not job:
            report["submitted"] = True
            report["shared_service_before"] = service_state()
            report["config_sha256_before"] = config_digest()
            save_report(report_path, report)
            pending = asyncio.create_task(submit_image(client, action, payload, {"Idempotency-Key": idem}, root))
        observed = None
        while pending and not pending.done():
            job = stored_job(args.media_db, idem)
            if job:
                report["job"] = job
                state = (job["job_id"], job["status"], job["provider_state"].get("thread_id"))
                if state != observed:
                    observed = state
                    print(json.dumps({"action": action, **job}, ensure_ascii=True), flush=True)
                save_report(report_path, report)
            await asyncio.sleep(2)
        if pending:
            try:
                response = await pending
                report["submission_response"] = safe_response(response)
            except httpx.HTTPError as exc:
                report["submission_transport_error"] = type(exc).__name__
        job = stored_job(args.media_db, idem)
        report["job"] = job
        if not job or job["status"] != "completed":
            report["elapsed_seconds"] = time.time() - report["started_at"]
            save_report(report_path, report)
            print(json.dumps({"report": str(report_path), "job": job, "passed": False}), flush=True)
            return 1
        evidence = await verify_image_artifact(client, credentials["MEDIA_OTHER_KEY"], job, response, root, action)
        report["artifact"] = evidence
        # This repeats the same endpoint/body/key only after proving the original completed.
        replay = await submit_image(client, action, payload, {"Idempotency-Key": idem}, root)
        after_replay = stored_job(args.media_db, idem)
        report["idempotency"] = {
            "response": safe_response(replay), "same_job": replay.json().get("id") == job["job_id"],
            "same_thread": after_replay["provider_state"] == job["provider_state"],
            "same_output_hash": after_replay["output"]["sha256"] == job["output"]["sha256"],
        }
        rpc = CodexRPC()
        try:
            await rpc.start()
            read = await rpc.call("thread/read", {"threadId": job["provider_state"]["thread_id"], "includeTurns": True})
            thread = read["thread"]
            report["codex_thread"] = {
                "id": thread["id"], "cwd": thread["cwd"], "turn_count": len(thread["turns"]),
                "turns": [{"id": turn["id"], "status": turn["status"], "item_types": [
                    item["type"] for item in turn["items"]],
                    "image_generation_items": [
                        {key: value for key, value in item.items() if key != "result"}
                        for item in turn["items"] if item["type"] == "imageGeneration"
                    ]} for turn in thread["turns"]],
            }
            report["codex_thread"]["image_generation_count"] = sum(
                len(turn["image_generation_items"]) for turn in report["codex_thread"]["turns"])
        finally:
            await rpc.close()
        report["shared_service_after"] = service_state()
        report["config_sha256_after"] = config_digest()
        report["elapsed_seconds"] = time.time() - report["started_at"]
        report["generation_elapsed_seconds"] = job["updated_at"] - job["created_at"]
        report["passed"] = (evidence["passed"] and replay.status_code == 200
                            and all(report["idempotency"][key] for key in
                                    ("same_job", "same_thread", "same_output_hash"))
                            and report["codex_thread"]["turn_count"] == 1
                            and report["codex_thread"]["image_generation_count"] == 1)
        save_report(report_path, report)
        print(json.dumps({"report": str(report_path), "job_id": job["job_id"],
                          "thread_id": job["provider_state"].get("thread_id"),
                          "request_id": job["request_id"], "artifact": evidence["base64"],
                          "passed": report["passed"]}, indent=2), flush=True)
        return 0 if report["passed"] else 1


async def safe_owned_thread(rpc: CodexRPC, job: dict, cwd: Path) -> dict:
    state = job["provider_state"]
    response = await rpc.call("thread/read", {"threadId": state["thread_id"], "includeTurns": True})
    thread = response["thread"]
    if thread.get("id") != state["thread_id"] or thread.get("cwd") != str(cwd.resolve()):
        raise RuntimeError("Original thread ownership does not match the media job.")
    turns = thread.get("turns") or []
    return {
        "id": thread["id"], "cwd": thread["cwd"], "status": thread.get("status"),
        "observed_at": time.time(), "turn_count": len(turns),
        "turns": [{"id": turn["id"], "status": turn["status"],
                   "item_types": [item["type"] for item in turn.get("items", [])],
                   "image_generation_items": [
                       {key: value for key, value in item.items() if key != "result"}
                       for item in turn.get("items", []) if item.get("type") == "imageGeneration"
                   ]} for turn in turns],
        "image_generation_count": sum(
            item.get("type") == "imageGeneration" for turn in turns for item in turn.get("items", [])),
    }


def restart_window_is_active(snapshot: dict, turn_id: str) -> bool:
    return (
        snapshot.get("turn_count") == 1 and len(snapshot.get("turns", [])) == 1
        and snapshot["turns"][0].get("id") == turn_id
        and snapshot["turns"][0].get("status") == "inProgress"
        and not any(item.get("status") in {"completed", "failed"}
                    for item in snapshot["turns"][0].get("image_generation_items", []))
    )


def recovery_config_digests(db_path: Path) -> dict:
    files = {
        "codex_config": Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml",
        "media_service_env": Path.home() / ".config/ai-router-media/service.env",
        "provider_source": Path.home() /
            ".local/share/ai-router-media/current/ai_router/media_service/providers.py",
    }
    result = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in files.items()}
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
        row = db.execute("SELECT value FROM settings WHERE id=1").fetchone()
    result["media_settings"] = hashlib.sha256((row[0] if row else "").encode()).hexdigest()
    return result


async def restart_recovery_probe(args) -> int:
    root = args.report_dir.expanduser().resolve() / "images"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "restart-recovery.json"
    idem = "media-acceptance-20260904-codex-reload-recovery-v1"
    report = json.loads(path.read_text()) if path.exists() else {
        "action": "restart-recovery", "idempotency_key": idem, "started_at": time.time(),
        "post_count": 0, "restart_attempted": False, "passed": False, "observations": [],
    }
    parsed = urlparse(args.router_base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Restart acceptance only permits the authorized loopback Router.")
    credentials = acceptance_credentials(args.acceptance_env.expanduser())
    job = stored_job(args.media_db, idem)
    if report["post_count"] and not job:
        raise RuntimeError("Original submission is not visible; refusing another POST.")
    if job and not report["post_count"]:
        raise RuntimeError("An original task exists without this probe's receipt; refusing execution.")
    payload = image_request({
        "model": "siyuan-image", "use_case": "photo", "aspect_ratio": "square", "background": "opaque",
        "response_format": "b64_json", "n": 1,
        "prompt": ("A studio product photograph of exactly one white glazed ceramic mug with a cobalt blue rim "
                   "and a round handle on the right, centered on a plain light gray tabletop and matching "
                   "background. The whole mug is visible. Soft lighting, a small shadow. No text, logos, "
                   "props, hands or other objects. Generate exactly one image."),
    })
    rpc = CodexRPC()
    pending = None
    post_recorded = False
    try:
        await rpc.start()
        account = (await rpc.call("account/read", {"refreshToken": False})).get("account") or {}
        report["account"] = {key: account[key] for key in ("type", "planType") if key in account}
        if account.get("type") not in {"chatgpt", "chatgptAuthTokens"}:
            raise RuntimeError("Existing ChatGPT authentication is required.")
        report["billing_scope"] = "Account type/plan only; the specific quota billing bucket is not proven."
        async with httpx.AsyncClient(base_url=args.router_base, trust_env=False,
            headers={"Authorization": "Bearer " + credentials["MEDIA_CLIENT_KEY"]},
            timeout=httpx.Timeout(800, connect=10)) as client:
            if not job:
                report["media_before"] = service_state("media-adapter.service")
                report["shared_before"] = service_state()
                report["config_before"] = recovery_config_digests(args.media_db)
                if any(state.get("ActiveState") != "active" or state.get("SubState") != "running"
                       for state in (report["media_before"], report["shared_before"])):
                    raise RuntimeError("Both original services must already be running.")
                if report["config_before"]["provider_source"] != "75264ae0d3c4d3476453a90ca03fab6c603c954356f349868b83ea657738ea06":
                    raise RuntimeError("Deployed provider does not match the frozen release5 hash.")
                report["payload"] = payload
                report["post_count"] = 1
                report["submitted_at"] = time.time()
                save_report(path, report)
                pending = asyncio.create_task(client.post(
                    "/v1/images/generations", json=payload, headers={"Idempotency-Key": idem}))
            deadline = time.monotonic() + 800
            observed = None
            last_public_poll = 0.0
            while time.monotonic() < deadline:
                job = stored_job(args.media_db, idem)
                if pending and pending.done() and not post_recorded:
                    post_recorded = True
                    try:
                        report["submission_response"] = safe_response(await pending)
                    except httpx.HTTPError as exc:
                        report["submission_transport_error"] = type(exc).__name__
                    save_report(path, report)
                    if not job:
                        raise RuntimeError("The only POST returned without a task; no resubmission is permitted.")
                if not job:
                    await asyncio.sleep(0.25)
                    continue
                report["job"] = job
                state = job["provider_state"]
                directory = args.media_db.parent / "jobs" / job["job_id"]
                signature = (job["job_id"], job["status"], state.get("thread_id"), state.get("turn_id"))
                if signature != observed:
                    observed = signature
                    observation = {"observed_at": time.time(), "job_id": job["job_id"], "status": job["status"],
                                   "request_id": job["request_id"], "provider_state": state, "error": job["error"]}
                    report["observations"].append(observation)
                    save_report(path, report)
                    print(json.dumps(observation), flush=True)
                if state.get("thread_id") and state.get("turn_id") and not report["restart_attempted"]:
                    before = await safe_owned_thread(rpc, job, directory)
                    report["thread_before_restart"] = before
                    if not restart_window_is_active(before, state["turn_id"]):
                        report["restart_window_missed"] = True
                        raise RuntimeError("Original turn is no longer in progress; will not restart or generate another image.")
                    report["original_job_id"] = job["job_id"]
                    report["original_thread_id"] = state["thread_id"]
                    report["original_turn_id"] = state["turn_id"]
                    report["restart_attempted"] = True
                    report["restart_started_at"] = time.time()
                    save_report(path, report)
                    print(json.dumps({"event": "restart_once", "job_id": job["job_id"],
                                      "thread_id": state["thread_id"], "turn_id": state["turn_id"],
                                      "original_turn_status": "inProgress",
                                      "media_pid_before": report["media_before"]["MainPID"]}), flush=True)
                    restart = await asyncio.create_subprocess_exec(
                        "systemctl", "--user", "restart", "media-adapter.service",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    report["restart_exit_code"] = await restart.wait()
                    report["restart_finished_at"] = time.time()
                    report["media_after_restart"] = service_state("media-adapter.service")
                    report["shared_after_restart"] = service_state()
                    report["thread_after_restart"] = await safe_owned_thread(rpc, job, directory)
                    save_report(path, report)
                    print(json.dumps({"event": "restart_finished", "exit_code": report["restart_exit_code"],
                                      "media_pid_after": report["media_after_restart"]["MainPID"],
                                      "shared_pid": report["shared_after_restart"]["MainPID"],
                                      "original_turns": report["thread_after_restart"]["turns"]}), flush=True)
                if job["status"] in {"completed", "failed", "cancelled"}:
                    break
                if report["restart_attempted"] and time.monotonic() - last_public_poll >= 2:
                    last_public_poll = time.monotonic()
                    try:
                        public = await client.get(f"/v1/images/{job['job_id']}", timeout=10)
                        report["last_public_poll"] = safe_response(public)
                    except httpx.HTTPError as exc:
                        report["last_public_poll_error"] = type(exc).__name__
                    save_report(path, report)
                await asyncio.sleep(0.25 if not report["restart_attempted"] else 1)
            job = stored_job(args.media_db, idem)
            report["job"] = job
            if not job or job["status"] != "completed":
                raise RuntimeError("The original task did not complete; no retry or replacement was submitted.")
            report["artifact"] = await verify_image_artifact(
                client, credentials["MEDIA_OTHER_KEY"], job, None, root, "restart-recovery")
            report["thread_final"] = await safe_owned_thread(
                rpc, job, args.media_db.parent / "jobs" / job["job_id"])
            final_thread = report["thread_final"]
            final_turns = final_thread["turns"]
            report["media_final"] = service_state("media-adapter.service")
            report["shared_final"] = service_state()
            report["config_after"] = recovery_config_digests(args.media_db)
            report["checks"] = {
                "single_post": report["post_count"] == 1,
                "restart_once_succeeded": report["restart_attempted"] and report.get("restart_exit_code") == 0,
                "active_original_turn_before_restart": restart_window_is_active(
                    report["thread_before_restart"], report["original_turn_id"]),
                "adapter_pid_changed": report["media_before"]["MainPID"] != report["media_after_restart"]["MainPID"],
                "adapter_running": report["media_final"]["ActiveState"] == "active"
                    and report["media_final"]["SubState"] == "running",
                "shared_codex_unchanged": report["shared_before"] == report["shared_after_restart"] == report["shared_final"],
                "config_hashes_stable": report["config_before"] == report["config_after"],
                "same_job_thread_turn": job["job_id"] == report["original_job_id"]
                    and job["provider_state"]["thread_id"] == report["original_thread_id"]
                    and job["provider_state"]["turn_id"] == report["original_turn_id"]
                    and final_thread["id"] == report["original_thread_id"],
                "one_turn": len(final_turns) == 1 and final_turns[0]["id"] == report["original_turn_id"],
                "one_completed_image": final_thread["image_generation_count"] == 1
                    and any(item["status"] == "completed" for turn in final_turns
                            for item in turn["image_generation_items"]),
                "decoded_downloaded_image": report["artifact"]["passed"],
            }
            report["passed"] = all(report["checks"].values())
            report["elapsed_seconds"] = time.time() - report["submitted_at"]
            report["generation_elapsed_seconds"] = job["updated_at"] - job["created_at"]
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        report["passed"] = False
    finally:
        if pending and not post_recorded:
            try:
                report["submission_response"] = safe_response(await pending)
            except httpx.HTTPError as exc:
                report["submission_transport_error"] = type(exc).__name__
        await rpc.close()
        report["finished_at"] = time.time()
        save_report(path, report)
    print(json.dumps({"report": str(path), "passed": report["passed"], "job": report.get("job"),
                      "checks": report.get("checks"), "error": report.get("error")}, indent=2), flush=True)
    return 0 if report["passed"] else 1


def service_state(unit: str = "codex-app-server.service") -> dict:
    if unit not in {"codex-app-server.service", "media-adapter.service"}:
        raise ValueError("Only the acceptance service pair may be inspected.")
    result = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "--property=MainPID", "--property=ActiveState", "--property=SubState",
         "--property=ExecMainStartTimestampMonotonic"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def config_digest() -> str | None:
    config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    return hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None


def safe_start_response(result: dict) -> dict:
    fields = ("activePermissionProfile", "approvalPolicy", "cwd", "sandbox",
              "runtimeWorkspaceRoots", "modelProvider")
    safe = {key: result.get(key) for key in fields}
    thread = result.get("thread") or {}
    safe["thread"] = {key: thread.get(key) for key in
                      ("id", "cwd", "ephemeral", "cliVersion", "status")}
    return safe


async def probe(root: Path, compare_legacy: bool) -> dict:
    report = {
        "schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
        "generation_attempted": False, "turn_start_sent": False,
        "shared_service_before": service_state(), "config_sha256_before": config_digest(),
        "cases": [],
    }
    version = subprocess.run(["codex", "app-server", "daemon", "version"],
                             capture_output=True, text=True, timeout=10, check=True)
    report["versions"] = json.loads(version.stdout)
    if report["versions"].get("status") != "running":
        raise RuntimeError("Shared daemon is not running; will not start or restart it.")
    rpc = CodexRPC()
    try:
        await rpc.start()
        account = await rpc.call("account/read", {"refreshToken": False})
        report["auth_type"] = (account.get("account") or {}).get("type")
        report["account"] = {key: value for key, value in (account.get("account") or {}).items()
                             if key in ("type", "planType")}
        for case in (["legacy", "explicit"] if compare_legacy else ["explicit"]):
            cwd = root / f"{case}-{uuid4().hex}"
            cwd.mkdir(mode=0o700)
            params = CodexProvider._thread_params(cwd)
            if case == "legacy":
                params.pop("permissions")
                params["config"]["sandbox_workspace_write"] = {
                    "network_access": False, "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True}
            params["developerInstructions"] = "Protocol probe only. Do not generate images or execute tools."
            entry = {"case": case, "cwd": str(cwd)}
            report["cases"].append(entry)
            try:
                result = await rpc.call("thread/start", params)
                entry["start"] = safe_start_response(result)
                entry["effective_permissions"] = CodexProvider._verify_permissions(result, cwd)
                thread_id = result["thread"]["id"]
                read = await rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                thread = read.get("thread") or {}
                entry["read"] = {
                    "id": thread.get("id"), "cwd": thread.get("cwd"),
                    "full_history_requested": False,
                    "status": thread.get("status"),
                }
                if thread.get("id") != thread_id or thread.get("cwd") != str(cwd):
                    raise RuntimeError("Dedicated thread metadata mismatch.")
                entry["passed"] = True
            except Exception as exc:
                entry["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None),
                                  "rpc_code": getattr(exc, "rpc_code", None)}
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
    finally:
        await rpc.close()
        report["shared_service_after"] = service_state()
        report["config_sha256_after"] = config_digest()
        report["shared_service_unchanged"] = report["shared_service_before"] == report["shared_service_after"]
        report["global_config_unchanged"] = report["config_sha256_before"] == report["config_sha256_after"]
    report["passed"] = (report.get("auth_type") == "chatgpt"
                        and all(case.get("passed") for case in report["cases"])
                        and bool(report["cases"]) and not report.get("error")
                        and report["shared_service_unchanged"] and report["global_config_unchanged"])
    report["not_verified"] = [
        "No model turn or image generation was submitted.",
        "No kernel filesystem/network negative test or model tool invocation was performed.",
        "Image result schema and interruption/recovery require the Router live acceptance.",
    ]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=Path.home() /
                        ".local/state/ai-router-acceptance/20260904-media-deploy")
    parser.add_argument("--compare-legacy", action="store_true")
    parser.add_argument("--router-action", choices=("generate", "edit", "restart-recovery"))
    parser.add_argument("--visual-review-note", help="Record an already performed visual inspection; makes no network requests.")
    parser.add_argument("--router-base", default="http://127.0.0.1:4000")
    parser.add_argument("--acceptance-env", type=Path,
                        default=Path.home() / ".config/ai-router-media/acceptance.env")
    parser.add_argument("--media-db", type=Path, default=Path("/opt/1panel/ai-router/media/media.sqlite3"))
    args = parser.parse_args()
    if args.visual_review_note:
        if not args.router_action:
            parser.error("--visual-review-note requires --router-action")
        record_visual_review(args.report_dir.expanduser().resolve(), args.router_action, args.visual_review_note)
        return 0
    if args.router_action:
        return asyncio.run(restart_recovery_probe(args) if args.router_action == "restart-recovery" else real_image_probe(args))
    root = args.report_dir.expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = asyncio.run(probe(root, args.compare_legacy))
    path = root / f"codex-protocol-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.json"
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"report": str(path), "auth_type": report.get("auth_type"),
                      "cases": report["cases"], "generation_attempted": False}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
