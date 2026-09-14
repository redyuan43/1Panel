"""Opt-in real Router processes and Redis, with a loopback-only fake model."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def serve(role, port):
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from ai_router.runtime import build_runtime
    from ai_router.token_counter import SimpleTokenCounter
    from ai_router import api, control
    module = api if role == "api" else control
    # Test-only counting. This proves transport/persistence, not model capacity.
    runtime = build_runtime(token_counter=SimpleTokenCounter())
    module.build_runtime = lambda: runtime
    uvicorn.run(module.create_app(), host="127.0.0.1", port=port, log_level="warning")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError("isolated service condition did not become true")


@pytest.mark.skipif(not os.environ.get("HISTORY_TEST_REDIS"), reason="requires disposable Redis")
@pytest.mark.parametrize("failure_mode", ["crash", "disable"])
def test_router_http_history_survives_process_restart(tmp_path, failure_mode):
    # Crash tests intentionally leave unexpired leases. Give each complete
    # runtime its own Redis DB; do not delete a lease to make the next test pass.
    redis_url = urlsplit(os.environ["HISTORY_TEST_REDIS"])
    base_db = int(redis_url.path.lstrip("/") or "0")
    assert 0 <= base_db <= 13, "disposable Redis must reserve the next two databases"
    redis_url = urlunsplit(redis_url._replace(path="/" + str(base_db + (1 if failure_mode == "crash" else 2))))
    captured = []
    summary_operations = []
    block_summary, summary_started, summary_release, summary_finished = (threading.Event() for _ in range(4))
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply({"ok": True, "data": [{"id": "fixture-model"}]})

        def reply_stream(self, events):
            data = "".join("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n"
                           for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            content = "fixture answer"
            if self.headers.get("X-1Panel-Operation-Kind") == "background_compaction":
                summary_operations.append(self.headers["X-1Panel-Operation-ID"])
                if block_summary.is_set():
                    summary_started.set()
                    summary_release.wait(120)
                    summary_finished.set()
                content = json.dumps({"facts": ["Archived diagnostic history"], "user_preferences": [],
                    "decisions": [], "open_goals": [], "tool_state": [], "key_references": []})
            try:
                if self.path.endswith("/responses"):
                    item = {"id": "msg-fixture", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": content, "annotations": []}]}
                    response = {"id": "resp-" + uuid4().hex, "object": "response", "model": "fixture-model",
                                "status": "completed", "output": [item],
                                "usage": {"input_tokens": 100, "output_tokens": 3, "total_tokens": 103}}
                    if body.get("stream"):
                        self.reply_stream([
                            {"type": "response.created", "sequence_number": 0,
                             "response": {**response, "status": "in_progress", "output": [], "usage": None}},
                            {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                             "item": {**item, "status": "in_progress", "content": []}},
                            {"type": "response.content_part.added", "sequence_number": 2, "item_id": item["id"],
                             "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                            {"type": "response.output_text.delta", "sequence_number": 3, "item_id": item["id"],
                             "output_index": 0, "content_index": 0, "delta": content},
                            {"type": "response.output_text.done", "sequence_number": 4, "item_id": item["id"],
                             "output_index": 0, "content_index": 0, "text": content},
                            {"type": "response.content_part.done", "sequence_number": 5, "item_id": item["id"],
                             "output_index": 0, "content_index": 0, "part": item["content"][0]},
                            {"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": item},
                            {"type": "response.completed", "sequence_number": 7, "response": response}])
                    else:
                        self.reply(response)
                    return
                if body.get("stream"):
                    chunk = {"id": "chat-" + uuid4().hex, "object": "chat.completion.chunk", "model": "fixture-model"}
                    self.reply_stream([{**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content},
                                                             "finish_reason": None}]},
                        {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 3, "total_tokens": 103}}, "[DONE]"])
                    return
                self.reply({"id": "fixture-" + uuid4().hex, "model": "fixture-model", "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 3, "total_tokens": 103}})
            except (BrokenPipeError, ConnectionResetError):
                # The crash scenario intentionally removes the receiving process.
                pass

    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    upstream = "http://127.0.0.1:" + str(model.server_port)
    defaults = yaml.safe_load((ROOT / "config/defaults.yaml").read_text())
    defaults["clients"] = {"policies": [{"id": "bootstrap", "key_env": "AI_ROUTER_TEST_BOOTSTRAP_KEY",
        "models": ["fixture-model"], "rpm_limit": 60, "tpm_limit": 100000, "max_parallel_requests": 2}]}
    defaults["identity"]["enabled"] = False
    defaults["evaluator"]["enabled"] = False
    defaults["compaction"].update(enabled=False, background_enabled=False, history_query_rewrite_enabled=False)
    defaults["routing"]["objectives"] = {"enabled": False}
    defaults_path = tmp_path / "defaults.yaml"
    defaults_path.write_text(yaml.safe_dump(defaults))
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump({"endpoints": [{
        "id": "fixture", "public_model": "fixture-model", "provider_model": "fixture-model", "node": "fixture",
        "api_base": upstream + "/v1", "health_url": upstream + "/health", "backend_type": "openai",
        "role": "responder", "tier": "local-general", "tier_rank": 1, "modalities": ["text"],
        "tasks": ["general"], "safe_context_tokens": 32000, "configured_context_tokens": 32000,
        "max_concurrency": 2, "enabled": True, "auto_candidate": True,
        "capabilities": {"chat": True, "responses": "native", "tools": "none", "streaming": True},
        "quality": {"general": 50}}]}))
    archive_key = tmp_path / "archive.key"
    archive_key.write_bytes(Fernet.generate_key())
    archive_key.chmod(0o600)
    admin = "isolated-" + uuid4().hex
    env = {key: value for key, value in os.environ.items() if not key.startswith("AI_ROUTER_")}
    env.update(AI_ROUTER_DEFAULTS_PATH=str(defaults_path), AI_ROUTER_RUNTIME_SETTINGS_PATH=str(tmp_path / "settings.yaml"),
        AI_ROUTER_REGISTRY_PATH=str(registry_path), AI_ROUTER_STATE_BACKEND="redis",
        AI_ROUTER_REDIS_URL=redis_url, AI_ROUTER_STATE_KEY=Fernet.generate_key().decode(),
        AI_ROUTER_ADMIN_KEY=admin, AI_ROUTER_TEST_BOOTSTRAP_KEY="isolated-" + uuid4().hex,
        AI_ROUTER_LITELLM_MASTER_KEY="isolated-internal",
        AI_ROUTER_LITELLM_URL=upstream, AI_ROUTER_TRAINING_ENABLED="true",
        AI_ROUTER_TRAINING_DB_PATH=str(tmp_path / "archive.sqlite3"), AI_ROUTER_TRAINING_KEY_PATH=str(archive_key),
        AI_ROUTER_ROUTE_TRACE_DB_PATH=str(tmp_path / "traces.sqlite3"),
        AI_ROUTER_PROMPT_DIRECTIVE_DB_PATH=str(tmp_path / "directives.sqlite3"),
        AI_ROUTER_AUDIT_PATH=str(tmp_path / "audit.jsonl"))
    processes, logs = [], []
    run_id = uuid4().hex
    def start(role, port):
        log = (tmp_path / (role + "-" + str(len(processes)) + ".log")).open("w")
        logs.append(log)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", role, str(port)],
            cwd=ROOT, env={**env, "AI_ROUTER_INSTANCE_ID": "context-test-" + run_id + "-" + role},
            stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        def ready():
            assert process.poll() is None, "Router process exited; inspect isolated log"
            return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200
        wait_until(ready)
        return process

    try:
        api_port, control_port = unused_port(), unused_port()
        api_process = start("api", api_port)
        control_process = start("control", control_port)
        control_url, api_url = f"http://127.0.0.1:{control_port}", f"http://127.0.0.1:{api_port}"
        headers = {"Authorization": "Bearer " + admin}
        account_id = "test-" + run_id[:16]
        response = httpx.post(control_url + "/api/clients", headers=headers, json={"id": account_id,
            "name": "Isolated personal test", "disclosure_mode": "internal", "models": ["fixture-model"], "rpm_limit": 60,
            "tpm_limit": 100000, "max_parallel_requests": 2, "history_owner_confirmed": True,
            "history_recall_enabled": True}, timeout=5)
        assert response.status_code == 201, response.text
        response = httpx.post(control_url + f"/api/clients/{account_id}/keys", headers=headers, json={"label": "test"})
        assert response.status_code == 201, response.text
        client_headers = {"Authorization": "Bearer " + response.json()["api_key"]}
        def send(text, conversation):
            response = httpx.post(api_url + "/v1/chat/completions",
                headers={**client_headers, "X-1Panel-Conversation-ID": conversation},
                json={"model": "fixture-model", "messages": [{"role": "user", "content": text}], "max_tokens": 32}, timeout=10)
            assert response.status_code == 200, response.text
            return response.headers["x-request-id"]
        first_id = send("E_RESTART_718 的配置值是 1739。", "source-" + run_id)
        status_url = control_url + f"/api/clients/{account_id}/history-memory"
        wait_until(lambda: httpx.get(status_url, headers=headers).json().get("chunks", 0) >= 1)
        before = httpx.get(api_url + "/internal/status", headers=headers).json()
        api_process.terminate()
        assert api_process.wait(timeout=15) in {0, -15}
        restarted = start("api", api_port)
        assert restarted.pid != api_process.pid
        second_id = send("E_RESTART_718 的配置值是什么？", "recall-" + run_id)
        forwarded = json.dumps(captured[-1], ensure_ascii=False)
        assert "<router-history-recall>" in forwarded and "1739" in forwarded and first_id in forwarded
        after = httpx.get(api_url + "/internal/status", headers=headers).json()
        assert before["instance"]["instance_id"] == after["instance"]["instance_id"]
        assert before["instance"]["boot_id"] != after["instance"]["boot_id"]
        from ai_router.content_audit import ArchiveReader
        archived = ArchiveReader(tmp_path / "archive.sqlite3", archive_key).read(second_id)
        raw = json.dumps(archived["request"]["received_body"], ensure_ascii=False)
        assert "<router-history-recall>" not in raw and "1739" not in raw
        protocol_evidence = []
        for kind, streaming in (("chat", True), ("responses", False), ("responses", True)):
            question = "E_RESTART_718 的配置值是什么？"
            body = {"model": "fixture-model", "stream": streaming}
            if kind == "chat":
                body.update(messages=[{"role": "user", "content": question}], max_tokens=32)
            else:
                body.update(input=question, max_output_tokens=32)
            response = httpx.post(api_url + ("/v1/chat/completions" if kind == "chat" else "/v1/responses"),
                headers={**client_headers, "X-1Panel-Conversation-ID": f"protocol-{kind}-{streaming}-" + run_id},
                json=body, timeout=10)
            assert response.status_code == 200, response.text
            if streaming:
                assert "text/event-stream" in response.headers["content-type"]
                assert ("[DONE]" if kind == "chat" else "response.completed") in response.text
                assert "fixture answer" in response.text
            else:
                assert response.json()["status"] == "completed"
            request_id = response.headers["x-request-id"]
            actual_forward = json.dumps(captured[-1], ensure_ascii=False)
            assert "<router-history-recall>" in actual_forward and "1739" in actual_forward and first_id in actual_forward
            reader = ArchiveReader(tmp_path / "archive.sqlite3", archive_key)
            completed = wait_until(lambda: (record if (record := reader.read(request_id))
                and record.get("response", {}).get("complete") else None))
            received = json.dumps(completed["request"]["received_body"], ensure_ascii=False)
            assert "<router-history-recall>" not in received and "1739" not in received
            protocol_evidence.append({"protocol": kind, "stream": streaming, "request_id": request_id,
                                      "archive_complete": True, "raw_history_clean": True})
        # Pause/resume via Control must reach an idle API worker without a new
        # foreground request. Add a synthetic archived event directly while
        # paused, then require indexing before sending any model request.
        wait_until(lambda: httpx.get(status_url, headers=headers).json().get("progress", {}).get("caught_up"), 20)
        indexing_settings = httpx.get(control_url + "/api/settings", headers=headers).json()["settings"]
        indexing_settings["compaction"]["history_indexing"] = {
            "enabled": False, "batch_records": 1, "max_batch_seconds": 0.1, "duty_cycle": 0.1}
        assert httpx.put(control_url + "/api/settings", headers=headers, json=indexing_settings).status_code == 200
        time.sleep(5.2)
        paused_cursor = httpx.get(status_url, headers=headers).json()["archive_cursor"]
        from ai_router.training_archive import TrainingArchive
        writer = TrainingArchive(str(tmp_path / "archive.sqlite3"), str(archive_key))
        try:
            asyncio.run(writer.begin(request_id="paused-source-" + run_id,
                conversation_id="paused-source-" + run_id, conversation_mode="stateful",
                client_id=account_id, key_id="synthetic", protocol="chat",
                received_body={"messages":[{"role":"user","content":"E_PAUSE_917 has final port 7193."}]},
                instance_id="isolated", boot_id="isolated", history_source_local_only=False))
        finally:
            writer.close()
        resume_target_cursor = ArchiveReader(tmp_path / "archive.sqlite3", archive_key).history_head()
        time.sleep(5.2)
        paused = httpx.get(status_url, headers=headers).json()
        assert paused["indexing_enabled"] is False and paused["archive_cursor"] == paused_cursor
        indexing_settings["compaction"]["history_indexing"]["enabled"] = True
        assert httpx.put(control_url + "/api/settings", headers=headers, json=indexing_settings).status_code == 200
        wait_until(lambda: httpx.get(status_url, headers=headers).json()["archive_cursor"] >= resume_target_cursor, 20)
        resumed_recall = send("What is the final port of E_PAUSE_917?", "resumed-recall-" + run_id)
        assert "7193" in json.dumps(captured[-1]) and "<router-history-recall>" in json.dumps(captured[-1])
        response = httpx.patch(control_url + f"/api/clients/{account_id}", headers=headers,
                               json={"allow_compaction": True})
        assert response.status_code == 200, response.text
        long_body = {"model": "fixture-model", "max_tokens": 32, "messages": [
            {"role": "user", "content": "historical diagnostics " * 1600},
            {"role": "assistant", "content": "historical findings " * 1600},
            *[{"role": "user" if i % 2 == 0 else "assistant", "content": "recent " + str(i)} for i in range(4)]]}
        long_headers = {**client_headers, "X-1Panel-Conversation-ID": "long-" + run_id}
        response = httpx.post(api_url + "/v1/chat/completions", headers=long_headers, json=long_body, timeout=10)
        assert response.status_code == 200, response.text
        long_request_id = response.headers["x-request-id"]
        original_limits = {"max_seconds": 30, "max_calls": 1,
                           "max_input_tokens": 50000, "max_output_tokens": 8192}
        response = httpx.put(control_url + "/api/settings", headers=headers, json={"compaction": {
            "enabled": True, "background_enabled": True, "model_id": "fixture",
            "background_limits": original_limits}}, timeout=5)
        assert response.status_code == 200, response.text
        jobs_url = control_url + f"/api/clients/{account_id}/compaction-jobs"
        response = httpx.post(jobs_url, headers=headers,
            json={"request_id": long_request_id, "target_endpoint_id": "fixture"}, timeout=5)
        assert response.status_code == 200, response.text
        job_id = response.json()["job"]["id"]
        def ready_job():
            jobs = httpx.get(jobs_url, headers=headers, timeout=5).json()["jobs"]
            return next((job for job in jobs if job["id"] == job_id and job["state"] == "ready"), None)
        ready = wait_until(ready_job, 20)
        assert ready["calls"] == len(summary_operations) == 1
        assert ready["limits"] == original_limits
        continuation = {**long_body, "messages": [*long_body["messages"],
            {"role": "assistant", "content": "fixture answer"},
            {"role": "user", "content": "Continue after the completed background summary."}]}
        followed = httpx.post(api_url + "/v1/chat/completions", headers=long_headers, json=continuation, timeout=10)
        assert followed.status_code == 200, followed.text
        followed_trace = httpx.get(control_url + "/api/route-traces/" + followed.headers["x-request-id"],
                                  headers=headers, timeout=5).json()["trace"]
        assert followed_trace["lineage_relation"] == "continuation"
        assert followed_trace["parent_branch_id"] == ready["branch"]
        assert followed_trace["background_compaction_applied_job_id"] == job_id
        assert "historical diagnostics " * 100 not in json.dumps(captured[-1])
        changed_limits = {"max_seconds": 45, "max_calls": 2,
                          "max_input_tokens": 100000, "max_output_tokens": 16384}
        updated_settings = httpx.get(control_url + "/api/settings", headers=headers, timeout=5).json()["settings"]
        updated_settings["compaction"]["background_limits"] = changed_limits
        changed = httpx.put(control_url + "/api/settings", headers=headers, json=updated_settings, timeout=5)
        assert changed.status_code == 200, changed.text
        for process in (restarted, control_process):
            process.terminate()
            assert process.wait(timeout=15) in {0, -15}
        active_api = start("api", api_port)
        active_control = start("control", control_port)
        recovered = wait_until(ready_job)
        assert recovered["id"] == job_id and recovered["calls"] == 1
        assert recovered["limits"] == original_limits
        stored_settings = yaml.safe_load((tmp_path / "settings.yaml").read_text())
        assert stored_settings["compaction"]["background_limits"] == changed_limits
        reloaded_settings = httpx.get(control_url + "/api/settings", headers=headers, timeout=5).json()["settings"]
        assert reloaded_settings["compaction"]["background_limits"] == changed_limits
        # Cover a complete worker polling interval after both processes reload.
        time.sleep(5.2)
        assert len(summary_operations) == 1
        new_job = httpx.post(jobs_url, headers=headers,
            json={"request_id": long_request_id, "target_endpoint_id": "fixture"}, timeout=5)
        assert new_job.status_code == 200, new_job.text
        assert new_job.json()["job"]["id"] != job_id
        assert new_job.json()["job"]["limits"] == changed_limits
        new_job_id = new_job.json()["job"]["id"]
        new_ready = wait_until(lambda: next((item for item in httpx.get(jobs_url, headers=headers, timeout=5).json()["jobs"]
            if item["id"] == new_job_id and item["state"] == "ready"), None), 20)
        assert new_ready["limits"] == changed_limits and new_ready["calls"] == 1
        assert len(summary_operations) == 2
        # Use a separate bounded account for fault injection: the preceding
        # source/summary/continuation calls can exceed 100K estimated TPM if
        # they happen within one wall-clock minute. Do not weaken the limiter
        # or let unrelated candidate/history state determine this crash test.
        crash_account = "crash-" + run_id[:16]
        created = httpx.post(control_url + "/api/clients", headers=headers, json={
            "id": crash_account, "name": "Isolated crash test", "disclosure_mode": "internal",
            "models": ["fixture-model"], "rpm_limit": 60, "tpm_limit": 100000,
            "max_parallel_requests": 2, "allow_compaction": True}, timeout=5)
        assert created.status_code == 201, created.text
        crash_key = httpx.post(control_url + f"/api/clients/{crash_account}/keys", headers=headers,
                              json={"label": "isolated crash"}, timeout=5)
        assert crash_key.status_code == 201, crash_key.text
        crash_headers = {"Authorization": "Bearer " + crash_key.json()["api_key"]}
        jobs_url = control_url + f"/api/clients/{crash_account}/compaction-jobs"
        crash_body = {**long_body, "messages": [
                      {"role": "user", "content": "independent crash diagnostics " * 1600},
                      *long_body["messages"][1:],
                      {"role": "user", "content": "crash recovery fixture"}]}
        crash_source = httpx.post(api_url + "/v1/chat/completions", headers=crash_headers, json=crash_body, timeout=10)
        assert crash_source.status_code == 200, crash_source.text
        block_summary.set()
        crash_job = httpx.post(jobs_url, headers=headers, json={
            "request_id": crash_source.headers["x-request-id"], "target_endpoint_id": "fixture"}, timeout=5)
        assert crash_job.status_code == 200, crash_job.text
        crash_job_id = crash_job.json()["job"]["id"]
        assert summary_started.wait(15), "summary was never dispatched"
        crash_operation = summary_operations[-1]
        if failure_mode == "crash":
            # Both runtimes host eligible workers; stop both to guarantee that
            # the actual lease holder exits, regardless of who claimed the job.
            for process in (active_api, active_control):
                process.kill()
            for process in (active_api, active_control):
                assert process.wait(timeout=10) == -9
        else:
            disabled_settings = httpx.get(control_url + "/api/settings", headers=headers).json()["settings"]
            disabled_settings["compaction"]["background_enabled"] = False
            assert httpx.put(control_url + "/api/settings", headers=headers, json=disabled_settings).status_code == 200
            # No foreground requests or restarts may refresh the worker for us.
            wait_until(lambda: next((item for item in httpx.get(jobs_url, headers=headers).json()["jobs"]
                if item["id"] == crash_job_id and item["state"] == "needs_context"), None), 12)
        summary_release.set()
        assert summary_finished.wait(5)
        recovered_api = start("api", api_port) if failure_mode == "crash" else active_api
        recovered_control = start("control", control_port) if failure_mode == "crash" else active_control
        # Use the real 60-second lease, not a shortened test-only expiry.
        unknown = wait_until(lambda: next((item for item in httpx.get(jobs_url, headers=headers, timeout=5).json()["jobs"]
            if item["id"] == crash_job_id and item["state"] == "needs_context"), None), 75)
        assert unknown["calls"] == 1 and unknown["unresolved_operation_ids"] == [crash_operation]
        assert len(summary_operations) == 3
        time.sleep(5.2)
        assert len(summary_operations) == 3
        resolved = httpx.post(jobs_url + f"/{crash_job_id}/reconcile", headers=headers, json={
            "operation_id": crash_operation, "upstream_terminal_confirmed": True, "discard_result": True,
            "evidence_reference": "synthetic-upstream-finished:" + crash_operation}, timeout=5)
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["job"]["state"] == "cancelled"
        if failure_mode == "disable":
            disabled_settings["compaction"]["background_enabled"] = True
            assert httpx.put(control_url + "/api/settings", headers=headers, json=disabled_settings).status_code == 200
        (tmp_path / "evidence.json").write_text(json.dumps({"source_request_id": first_id,
            "failure_mode": failure_mode,
            "recall_request_id": second_id, "before": before, "after": after,
            "protocol_evidence": protocol_evidence,
            "index_pause_cursor": paused_cursor, "resume_recall_request_id": resumed_recall,
            "index_resume_target_cursor": resume_target_cursor,
            "original_pid": api_process.pid, "restarted_pid": restarted.pid,
            "background_job_id": job_id, "background_source_request_id": long_request_id,
            "summary_operation_ids": summary_operations, "recovered_job": recovered,
            "saved_limits": changed_limits, "original_job_limits": original_limits,
            "new_ready_job": new_ready, "unknown_after_crash": unknown,
            "crashed_pid": active_api.pid, "crash_recovery_pid": recovered_api.pid,
            "crashed_control_pid": active_control.pid, "recovered_control_pid": recovered_control.pid,
            "resolved_crash_job": resolved.json()["job"]}, ensure_ascii=False))
    finally:
        summary_release.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        for log in logs:
            log.close()
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    assert sys.argv[1] == "--serve"
    serve(sys.argv[2], int(sys.argv[3]))
