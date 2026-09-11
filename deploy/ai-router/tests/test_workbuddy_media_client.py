from __future__ import annotations

import base64
from contextlib import redirect_stdout
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener

import pytest


SKILL = Path(__file__).resolve().parents[1] / "integrations/workbuddy/siyuan-media"
PUBLIC_FILES = {"SKILL.md", "references/usage.md", "scripts/media.py", "scripts/install.py"}
FAKE_KEY = "test-only-not-a-real-key-0123456789abcdef"
ACCESS_TICKET = "test-only-download-ticket-abcdef123456"
ENCODED_IMAGE = base64.b64encode(b"test-only-image-content-never-print-this").decode()
ARTIFACT = b"\x89PNG\r\n\x1a\nlocal-test-artifact-not-a-generated-image"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    previous_umask = os.umask(0o077)
    os.umask(previous_umask)
    media = load_module("workbuddy_media_test_subject", SKILL / "scripts/media.py")
    monkeypatch.setitem(sys.modules, "media", media)
    installer = load_module("workbuddy_media_installer_test_subject", SKILL / "scripts/install.py")
    yield SimpleNamespace(media=media, installer=installer)
    os.umask(previous_umask)


class LocalRouter:
    """An actual HTTP peer with durable-in-fixture idempotency and no providers."""

    def __init__(self):
        self.requests = []
        self.effects = []
        self.receipts = {}
        self.jobs = {}
        self.drop_next = False
        self.reply = self.default_reply
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.dispatch()

            def do_POST(self):
                self.dispatch()

            def dispatch(self):
                request = SimpleNamespace(
                    method=self.command, path=self.path, headers=self.headers,
                    body=self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
                peer.requests.append(request)
                reply = peer.reply(request)
                if reply is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status, value, headers = reply
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json" if not isinstance(value, bytes)
                                 else "application/octet-stream")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

    def default_reply(self, request):
        headers = {"X-Request-ID": "test-request-123"}
        if request.method == "POST":
            idem = request.headers.get("Idempotency-Key")
            fingerprint = (request.path, request.body)
            if idem in self.receipts:
                original, job_id = self.receipts[idem]
                if original != fingerprint:
                    return 409, {"error": {"code": "idempotency_conflict"}}, headers
            else:
                self.effects.append(request)
                kind = "vid" if "/videos" in request.path else "img"
                job_id = request.path.split("/")[3] if "/stages/" in request.path else f"{kind}_{len(self.effects)}"
                self.receipts[idem] = (fingerprint, job_id)
                self.jobs.setdefault(job_id, {"id": job_id, "status": "queued", "stages": []})
            if self.drop_next:
                self.drop_next = False
                return None
            return 202, self.jobs[job_id], headers
        if request.path == "/v1/media/options":
            return 200, {"enabled": True, "models": ["siyuan-image", "siyuan-video"]}, headers
        if request.path.startswith("/v1/media/outputs/"):
            return 200, ARTIFACT, headers
        if request.path.endswith("/outputs"):
            job = self.jobs.get(request.path.split("/")[3], {})
            return 200, {"data": [job["output"]] if job.get("output") else []}, headers
        job = self.jobs.get(request.path.rsplit("/", 1)[-1])
        return (200, job, headers) if job else (404, {"error": {"code": "not_found"}}, headers)


@pytest.fixture
def servers():
    running = []

    def create():
        peer = LocalRouter()
        running.append(peer)
        return peer

    yield create
    for peer in reversed(running):
        peer.close()


@pytest.fixture
def configured(modules, servers, tmp_path, monkeypatch):
    peer = servers()
    # The only network allowlist change is in this imported test instance.
    monkeypatch.setattr(modules.media, "ALLOWED_ORIGINS", {peer.origin})
    config = tmp_path / "private/config.json"
    modules.media.configure_credentials(config, {
        "base_url": peer.origin, "client_id": "test-user", "api_key": FAKE_KEY,
    })
    return SimpleNamespace(media=modules.media, installer=modules.installer, peer=peer,
                           config=config, client=modules.media.Client(config))


def invoke(env, *arguments):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = env.media.main(["--config", str(env.config), *map(str, arguments)])
    text = stdout.getvalue()
    return code, json.loads(text), text


def read_receipt(env, operation):
    return json.loads((env.client.root / operation / "receipt.json").read_text())


def assert_no_secrets(value):
    text = value if isinstance(value, str) else json.dumps(value)
    for secret in (FAKE_KEY, ENCODED_IMAGE, ACCESS_TICKET):
        assert secret not in text
    assert "access=" not in text


def published_output(**overrides):
    return {
        "id": "out_delivery", "output_id": "out_reviewed",
        "bytes": len(ARTIFACT), "sha256": hashlib.sha256(ARTIFACT).hexdigest(),
        "content_type": "image/png",
        **overrides,
    }


def parse_multipart(request):
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + request.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
        + request.body
    )
    assert message.is_multipart()
    return [
        (part.get_param("name", header="content-disposition"), part.get_filename(),
         part.get_content_type(), part.get_payload(decode=True))
        for part in message.iter_parts()
    ]


def test_fixed_origin_and_disabled_environment_proxies(configured, servers, monkeypatch):
    env, proxy = configured, servers()
    for variable in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(variable, proxy.origin)
    for variable in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(variable, "")
    code, result, stdout = invoke(env, "doctor")
    assert code == 0 and result["reachable"] is True
    assert len(env.peer.requests) == 1
    request = env.peer.requests[0]
    assert request.path == "/v1/media/options"
    assert request.headers["Authorization"] == "Bearer " + FAKE_KEY
    assert request.headers["Host"] == env.peer.origin.removeprefix("http://")
    assert proxy.requests == []
    assert_no_secrets(stdout)
    # A control request proves this local proxy trap is reachable and functional.
    proxy.reply = lambda request: (200, {"proxy": True}, {})
    with build_opener(ProxyHandler({"http": proxy.origin})).open(
        Request(env.peer.origin + "/v1/media/options"), timeout=3,
    ) as response:
        assert json.load(response) == {"proxy": True}
    assert len(proxy.requests) == 1
    assert proxy.requests[0].headers.get("Authorization") is None


@pytest.mark.parametrize("suffix", [
    "/v1/../admin", "//external.invalid/v1/images", "http://external.invalid/v1/images",
    "/v1/images?next=http://external.invalid", "/v1/images/%2e%2e/admin",
    "/v1/images#fragment", "/v1/images/\r\nHost: external.invalid",
])
def test_non_media_paths_never_send_authorization(configured, suffix):
    with pytest.raises(configured.media.ClientError, match="media API paths"):
        configured.client.request("GET", suffix)
    assert configured.peer.requests == []


@pytest.mark.parametrize("base", [
    "http://external.invalid", "http://127.0.0.1:4001",
    "http://127.0.0.1:4000@external.invalid", "http://127.0.0.1:4000/path",
    "http://127.0.0.1:4000?next=external.invalid", "https://127.0.0.1:4000",
])
def test_config_rejects_unlisted_origins_before_writing_key(modules, tmp_path, base):
    assert modules.media.ALLOWED_ORIGINS == {
        "http://ai-x10drg.taild500c8.ts.net:4000", "http://127.0.0.1:4000",
    }
    with pytest.raises(modules.media.ClientError) as error:
        modules.media.configure_credentials(tmp_path / "private/config.json", {
            "base_url": base, "api_key": FAKE_KEY,
        })
    assert error.value.code == "untrusted_origin"
    assert not (tmp_path / "private/router-key").exists()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirects_never_forward_bearer(configured, servers, status, method):
    sink = servers()
    configured.peer.reply = lambda request: (
        status, {}, {"Location": sink.origin + "/v1/images/redirected"},
    )
    with pytest.raises(configured.media.ClientError) as error:
        configured.client.request(method, "/v1/images", b"{}" if method == "POST" else None)
    assert error.value.code == "redirect_refused"
    assert len(configured.peer.requests) == 1
    assert sink.requests == []


def test_stdout_and_receipt_strip_private_response_fields(configured):
    env = configured
    env.peer.reply = lambda request: (202, {
        "id": "img_private", "status": "completed", "provider_state": {"api_key": FAKE_KEY},
        "output": published_output(
            content_url=env.peer.origin + "/v1/media/outputs/out_delivery/content?access=" + ACCESS_TICKET,
            source_path="/private/server/path", access=ACCESS_TICKET, authorization=FAKE_KEY,
        ),
        "data": [{"b64_json": ENCODED_IMAGE, "url": "https://external.invalid/?access=" + ACCESS_TICKET}],
        "error": {"code": "example", "message": "Credential " + FAKE_KEY},
    }, {"X-Request-ID": "safe-request-id"})
    code, value, stdout = invoke(env, "image", "--operation-id", "private", "--prompt", "A cup")
    assert code == 0
    assert value["id"] == "img_private" and value["output"]["id"] == "out_delivery"
    assert "data" not in value and "provider_state" not in value
    assert_no_secrets(stdout)
    receipt = env.client.root / "private/receipt.json"
    assert_no_secrets(receipt.read_text())
    assert_no_secrets((receipt.parent / "request.body").read_text())
    assert env.peer.requests[0].headers["Prefer"] == "respond-async"


@pytest.mark.parametrize("destination", ["stdout", "receipt"])
def test_untrusted_request_id_cannot_leak_key(configured, destination):
    env = configured
    env.peer.reply = lambda request: (
        202, {"id": "img_header", "status": "queued"}, {"X-Request-ID": "echo-" + FAKE_KEY},
    )
    code, _, stdout = invoke(env, "image", "--operation-id", "header", "--prompt", "A cup")
    assert code == 0
    observed = stdout if destination == "stdout" else (env.client.root / "header/receipt.json").read_text()
    assert_no_secrets(observed)


@pytest.mark.parametrize("location", ["error", "stage_text"])
@pytest.mark.parametrize("url_prefix", [
    "http://127.0.0.1/v1/media/outputs/out_delivery/content?access=",
    "HTTP://127.0.0.1/v1/media/outputs/out_delivery/content?access=",
    "HtTpS://127.0.0.1/v1/media/outputs/out_delivery/content?token=",
    "/v1/media/outputs/out_delivery/content?access=",
    "/v1/media/outputs/out_delivery/content?version=1&%61ccess=",
    "/v1/media/outputs/out_delivery/content?API_KEY=",
    "/v1/media/outputs/out_delivery/content?authorization=",
], ids=["absolute", "uppercase", "mixedcase-token", "relative", "encoded-access",
        "relative-api-key", "relative-authorization"])
def test_embedded_download_ticket_is_not_printed(configured, location, url_prefix):
    env = configured
    url = url_prefix + ACCESS_TICKET
    if location == "error":
        env.peer.reply = lambda request: (403, {"error": {"code": "denied", "message": "See " + url}}, {})
    else:
        env.peer.jobs["vid_text"] = {
            "id": "vid_text", "status": "in_progress",
            "stages": [{"id": "context_ir", "status": "awaiting_approval",
                        "output": {"id": "out_text", "text": "Preview " + url}}],
        }
    code, _, stdout = invoke(env, "status", "--job-id", "vid_text")
    assert code == (2 if location == "error" else 0)
    assert_no_secrets(stdout)


@pytest.mark.parametrize("url_prefix", [
    "HTTP://127.0.0.1/v1/media/outputs/out_delivery/content?access=",
    "/v1/media/outputs/out_delivery/content?access=",
], ids=["uppercase", "relative"])
def test_url_ticket_in_request_id_is_not_persisted(configured, url_prefix):
    env = configured
    env.peer.reply = lambda request: (
        202, {"id": "img_header", "status": "queued"},
        {"X-Request-ID": url_prefix + ACCESS_TICKET},
    )
    code, result, stdout = invoke(env, "image", "--operation-id", "url-header", "--prompt", "A cup")
    assert code == 0 and result["id"] == "img_header"
    assert_no_secrets(stdout)
    assert_no_secrets(read_receipt(env, "url-header"))


@pytest.mark.parametrize("text", [
    "See HTTPS://example.invalid/reference?version=2 for the scene.",
    "Read /v1/media/outputs/out_delivery/content without a bearer ticket.",
    "Use /home/user/notes.txt and keep the supplied prompt unchanged.",
])
def test_url_redaction_preserves_nonsecret_text(configured, text):
    assert configured.client.redact({"text": text}) == {"text": text}


@pytest.mark.parametrize("location", ["error", "stage_text"])
def test_embedded_base64_image_is_not_printed(configured, location):
    env = configured
    text = "Preview data:image/png;base64," + ENCODED_IMAGE
    if location == "error":
        env.peer.reply = lambda request: (403, {"error": {"code": "denied", "message": text}}, {})
    else:
        env.peer.jobs["vid_text"] = {
            "id": "vid_text", "status": "in_progress",
            "stages": [{"id": "context_ir", "status": "awaiting_approval",
                        "output": {"id": "out_text", "text": text}}],
        }
    code, _, stdout = invoke(env, "status", "--job-id", "vid_text")
    assert code == (2 if location == "error" else 0)
    assert_no_secrets(stdout)


def test_unknown_outcome_replays_persisted_body_and_same_idempotency(configured):
    env = configured
    env.peer.drop_next = True
    code, failure, stdout = invoke(env, "image", "--operation-id", "replay", "--prompt", "A cup")
    assert code == 2 and failure["error"]["code"] == "transport_unavailable"
    assert read_receipt(env, "replay")["status"] == "outcome_unknown"
    assert len(env.peer.effects) == 1
    saved_body = (env.client.root / "replay/request.body").read_bytes()
    assert env.peer.requests[0].body == saved_body
    assert_no_secrets(stdout)
    # main() constructs a fresh Client every time; recovery uses only files on disk.
    code, accepted, stdout = invoke(env, "resume", "--operation-id", "replay")
    assert code == 0 and accepted["id"] == "img_1"
    assert len(env.peer.effects) == 1
    posts = [request for request in env.peer.requests if request.method == "POST"]
    assert len(posts) == 2
    assert posts[0].body == posts[1].body == saved_body
    assert posts[0].headers["Content-Type"] == posts[1].headers["Content-Type"]
    assert {request.headers["Idempotency-Key"] for request in posts} == {"wb-replay"}
    assert read_receipt(env, "replay")["status"] == "accepted"
    code, repeated, _ = invoke(env, "resume", "--operation-id", "replay")
    assert code == 0 and repeated["id"] == accepted["id"]
    assert env.peer.requests[-1].method == "GET"
    assert len(env.peer.effects) == 1 and len(env.peer.requests) == 3
    assert_no_secrets(stdout)
    assert_no_secrets(read_receipt(env, "replay"))


@pytest.mark.parametrize("change", ["payload", "path", "corrupt_snapshot", "owner"])
def test_conflicting_or_corrupt_replays_send_no_second_request(configured, change):
    env = configured
    env.peer.drop_next = True
    assert invoke(env, "image", "--operation-id", "conflict", "--prompt", "Original")[0] == 2
    if change == "payload":
        action = lambda: env.client.operate("conflict", "/v1/images/generations", {"prompt": "Changed"})
        expected = "operation_conflict"
    elif change == "path":
        action = lambda: env.client.operate("conflict", "/v1/images/edits", {"prompt": "Original"})
        expected = "operation_conflict"
    elif change == "corrupt_snapshot":
        (env.client.root / "conflict/request.body").write_bytes(b"changed bytes")
        action = lambda: env.client.operate("conflict")
        expected = "operation_corrupt"
    else:
        env.client.owner = "different-user"
        action = lambda: env.client.operate("conflict")
        expected = "operation_owner_mismatch"
    with pytest.raises(env.media.ClientError) as error:
        action()
    assert error.value.code == expected
    assert len(env.peer.requests) == len(env.peer.effects) == 1


def test_operation_lock_prevents_parallel_replay(configured):
    env = configured
    directory = env.media.secure_dir(env.client.root / "busy")
    with env.media.operation_lock(directory / "lock"):
        with pytest.raises(env.media.ClientError) as error:
            env.media.Client(env.config).operate("busy", "/v1/images/generations", {"prompt": "A cup"})
    assert error.value.code == "operation_busy"
    assert env.peer.requests == []


def test_multipart_edit_preserves_original_upload_on_retry(configured, tmp_path):
    env = configured
    first, second = tmp_path / "first.png", tmp_path / "second.jpg"
    first.write_bytes(ARTIFACT)
    second.write_bytes(b"test-jpeg-bytes")
    env.peer.drop_next = True
    args = ("edit", "--operation-id", "edit", "--prompt", "Make it green",
            "--image", first, "--image", second)
    assert invoke(env, *args)[0] == 2
    original = env.peer.requests[0]
    parts = parse_multipart(original)
    files = [(name, mime, data) for name, filename, mime, data in parts if filename]
    assert files == [("image", "image/png", ARTIFACT), ("image", "image/jpeg", b"test-jpeg-bytes")]
    fields = {name: data.decode() for name, filename, _, data in parts if not filename}
    assert fields["response_format"] == "url" and fields["model"] == "siyuan-image"
    assert fields["prompt"] == "Make it green" and fields["n"] == "1"
    assert str(first).encode() not in original.body
    assert invoke(env, *args)[0] == 0
    assert env.peer.requests[1].body == original.body
    assert env.peer.requests[1].headers["Content-Type"] == original.headers["Content-Type"]
    assert len(env.peer.effects) == 1
    first.write_bytes(b"changed reference")
    code, failure, _ = invoke(env, *args)
    assert code == 2 and failure["error"]["code"] == "operation_conflict"
    assert len(env.peer.requests) == 2


@pytest.mark.parametrize("with_assets", [False, True])
def test_video_always_uses_multipart_without_printing_assets(configured, tmp_path, with_assets):
    env = configured
    default = env.peer.reply

    def reply(request):
        if request.path == "/v1/media/options":
            return 200, {
                "enabled": True,
                "videos": {
                    "workflow_mode": ["quality_gate"],
                    "creative_profile": {"values": ["general"], "default": "general"},
                    "aspect_ratio": {"values": ["16:9"], "default": "16:9"},
                },
            }, {"X-Request-ID": "options-request"}
        return default(request)

    env.peer.reply = reply
    image, audio = tmp_path / "reference.png", tmp_path / "reference.wav"
    image.write_bytes(ARTIFACT)
    audio.write_bytes(b"test-audio")
    args = ["video", "--operation-id", "video", "--prompt", "A cup rotates",
            "--duration", "4", "--strategy", "safe", "--confirm-context-cost", "--watermark"]
    if with_assets:
        args.extend(["--mode", "i2v", "--audio-policy", "reference",
                     "--asset", f"reference_image={image}", "--asset", f"reference_audio={audio}"])
    code, result, stdout = invoke(env, *args)
    assert code == 0 and result["id"].startswith("vid_")
    assert env.peer.requests[0].path == "/v1/media/options"
    request = env.peer.requests[1]
    assert request.path == "/v1/videos" and request.headers["Prefer"] == "respond-async"
    parts = parse_multipart(request)
    fields = {name: data.decode() for name, filename, _, data in parts if not filename}
    assert fields["duration"] == "4" and fields["strategy"] == "safe"
    assert fields["watermark"] == "true" and fields["use_embedded_video_audio"] == "false"
    assert fields["workflow_mode"] == "quality_gate"
    assert fields["creative_profile"] == "general"
    assert fields["aspect_ratio"] == "16:9"
    files = {name: data for name, filename, _, data in parts if filename}
    assert files == ({"reference_image": ARTIFACT, "reference_audio": b"test-audio"} if with_assets else {})
    assert_no_secrets(stdout)
    assert_no_secrets(read_receipt(env, "video"))


@pytest.mark.parametrize("explicit", [False, True])
def test_video_negotiates_modern_workflow_options(configured, explicit):
    env = configured
    default = env.peer.reply

    def reply(request):
        if request.path == "/v1/media/options":
            return 200, {
                "enabled": True,
                "videos": {
                    "workflow_mode": ["quality_gate"],
                    "creative_profile": {"values": ["general", "product"], "default": "general"},
                    "aspect_ratio": ["9:16", "16:9"],
                    "defaults": {"aspect_ratio": "9:16"},
                },
            }, {"X-Request-ID": "options-request"}
        return default(request)

    env.peer.reply = reply
    args = ["video", "--operation-id", "modern", "--prompt", "A product rotates",
            "--confirm-context-cost"]
    if explicit:
        args.extend(["--workflow-mode", "quality_gate", "--creative-profile", "product",
                     "--aspect-ratio", "16:9"])
    code, result, _ = invoke(env, *args)
    assert code == 0 and result["id"].startswith("vid_")
    request = env.peer.effects[0]
    fields = {name: data.decode() for name, filename, _, data in parse_multipart(request) if not filename}
    assert fields["workflow_mode"] == "quality_gate"
    assert fields["creative_profile"] == ("product" if explicit else "general")
    assert fields["aspect_ratio"] == ("16:9" if explicit else "9:16")


@pytest.mark.parametrize("explicit", [False, True])
def test_router_without_direct_ivan_workflow_fails_closed(configured, explicit):
    args = [
        "video", "--operation-id", "modern-on-legacy", "--prompt", "A scene",
        "--confirm-context-cost",
    ]
    if explicit:
        args.extend(["--workflow-mode", "quality_gate"])
    code, result, _ = invoke(configured, *args)
    assert code == 2 and result["error"]["code"] == "unsupported_media_option"
    assert [request.path for request in configured.peer.requests] == ["/v1/media/options"]
    assert configured.peer.effects == []


def test_historical_edge_video_cannot_be_advanced(configured):
    configured.peer.jobs["vid_legacy"] = {
        "id": "vid_legacy",
        "status": "failed",
        "workflow_mode": "legacy_pipeline",
        "stages": [
            {
                "id": "preview",
                "status": "approved",
                "output_id": "out_preview",
                "output": published_output(output_id="out_preview"),
            },
            {"id": "local_768", "status": "failed", "output_id": None},
        ],
    }
    code, result, _ = invoke(
        configured,
        "start",
        "--operation-id",
        "retired",
        "--job-id",
        "vid_legacy",
        "--stage",
        "local_768",
        "--output-id",
        "out_preview",
        "--confirmed",
    )
    assert code == 2
    assert result["error"]["code"] == "workflow_unavailable"
    assert [request.method for request in configured.peer.requests] == ["GET"]
    assert configured.peer.effects == []


@pytest.mark.parametrize("selection", ["final", "stage", "history"])
def test_download_uses_delivery_id_fixed_origin_and_verifies_hash(configured, servers, tmp_path, selection):
    env, sink = configured, servers()
    output = published_output(content_url=sink.origin + "/anything?access=" + ACCESS_TICKET)
    env.peer.jobs["vid_download"] = {
        "id": "vid_download", "status": "completed", "output": output,
        "stages": [{"id": "preview", "status": "approved", "output": output}],
    }
    target = tmp_path / "download.png"
    args = ["download", "--job-id", "vid_download", "--output", target]
    if selection == "stage":
        args.extend(["--stage", "preview"])
    elif selection == "history":
        args.extend(["--artifact-id", "out_delivery"])
    code, value, stdout = invoke(env, *args)
    assert code == 0 and target.read_bytes() == ARTIFACT
    assert value["sha256"] == hashlib.sha256(ARTIFACT).hexdigest()
    assert value["artifact_id"] == "out_delivery" and value["output_id"] == "out_reviewed"
    assert env.peer.requests[-1].path == "/v1/media/outputs/out_delivery/content"
    assert all(request.headers["Authorization"] == "Bearer " + FAKE_KEY for request in env.peer.requests)
    assert all("access=" not in request.path for request in env.peer.requests)
    assert sink.requests == []
    assert not list(tmp_path.glob(".siyuan-download-*"))
    assert_no_secrets(stdout)


@pytest.mark.parametrize("mismatch", ["hash", "short", "long"])
def test_bad_download_is_not_published(configured, tmp_path, mismatch):
    env = configured
    changes = {
        "hash": {"sha256": "0" * 64},
        "short": {"bytes": len(ARTIFACT) + 1},
        "long": {"bytes": len(ARTIFACT) - 1},
    }
    env.peer.jobs["img_bad"] = {"id": "img_bad", "output": published_output(**changes[mismatch])}
    target = tmp_path / "bad.png"
    code, failure, _ = invoke(env, "download", "--job-id", "img_bad", "--output", target)
    assert code == 2 and failure["error"]["code"] == "artifact_mismatch"
    assert not target.exists() and not list(tmp_path.glob(".siyuan-download-*"))


@pytest.mark.parametrize("existing", ["file", "symlink", "race"])
def test_download_never_clobbers_user_file(configured, tmp_path, existing):
    env = configured
    env.peer.jobs["img_existing"] = {"id": "img_existing", "output": published_output()}
    target, original = tmp_path / "chosen.png", tmp_path / "original.png"
    original.write_bytes(b"KEEP")
    if existing == "file":
        target.write_bytes(b"KEEP")
    elif existing == "symlink":
        target.symlink_to(original)
    else:
        default = env.peer.reply

        def race(request):
            if request.path.startswith("/v1/media/outputs/"):
                target.write_bytes(b"KEEP")
            return default(request)

        env.peer.reply = race
    code, _, stdout = invoke(env, "download", "--job-id", "img_existing", "--output", target)
    assert code == 2
    assert target.read_bytes() == original.read_bytes() == b"KEEP"
    if existing != "race":
        assert len(env.peer.requests) == 1
    assert not list(tmp_path.glob(".siyuan-download-*"))
    assert_no_secrets(stdout)


def test_approve_and_start_are_separate_versioned_operations(configured):
    env = configured
    env.peer.jobs["vid_review"] = {
        "id": "vid_review", "status": "in_progress",
        "stages": [
            {"id": "plan_custom", "status": "approved",
             "output": published_output(id="out_plan_delivery", output_id="out_plan")},
            {"id": "preview_custom", "status": "awaiting_approval",
             "output": published_output()},
            {"id": "final_custom", "status": "pending"},
        ],
    }
    code, _, _ = invoke(env, "approve", "--job-id", "vid_review", "--stage", "preview_custom",
                        "--output-id", "out_reviewed", "--operation-id", "approve", "--confirmed")
    assert code == 0
    assert [(r.method, r.path) for r in env.peer.requests] == [
        ("GET", "/v1/videos/vid_review"),
        ("POST", "/v1/videos/vid_review/stages/preview_custom/approve"),
    ]
    assert json.loads(env.peer.requests[1].body) == {"output_id": "out_reviewed"}
    assert invoke(env, "resume", "--operation-id", "approve")[0] == 0
    assert len(env.peer.effects) == 1 and env.peer.requests[-1].method == "GET"
    env.peer.jobs["vid_review"]["stages"][1]["status"] = "approved"
    code, _, _ = invoke(env, "start", "--job-id", "vid_review", "--stage", "final_custom",
                        "--output-id", "out_reviewed", "--operation-id", "start", "--confirmed")
    assert code == 0
    assert [request.path for request in env.peer.effects] == [
        "/v1/videos/vid_review/stages/preview_custom/approve",
        "/v1/videos/vid_review/stages/final_custom/start",
    ]
    assert [request.headers["Idempotency-Key"] for request in env.peer.effects] == ["wb-approve", "wb-start"]
    assert json.loads(env.peer.effects[1].body) == {"output_id": "out_reviewed"}


def test_regenerate_binds_current_output_and_review(configured):
    env = configured
    env.peer.jobs["vid_review"] = {
        "id": "vid_review", "status": "in_progress",
        "stages": [{
            "id": "preview_custom", "status": "awaiting_approval",
            "output": {
                **published_output(),
                "review": {
                    "review_id": "rev_current",
                    "semantic": {
                        "verdict": "FAIL",
                        "scores": {"identity": 62, "motion": 48},
                        "issues": [{"start_seconds": 2.1, "end_seconds": 2.8,
                                    "message": "Hand distortion"}],
                        "revised_prompt": "Keep both hands anatomically stable.",
                    },
                },
            },
        }],
    }
    code, value, _ = invoke(
        env, "status", "--job-id", "vid_review",
    )
    assert code == 0
    review = value["stages"][0]["output"]["review"]
    assert review["semantic"]["scores"] == {"identity": 62, "motion": 48}
    assert review["semantic"]["issues"][0]["start_seconds"] == 2.1
    code, _, _ = invoke(
        env, "regenerate", "--job-id", "vid_review", "--stage", "preview_custom",
        "--output-id", "out_reviewed", "--review-id", "rev_current",
        "--apply-suggestion", "--operation-id", "regenerate", "--confirmed",
    )
    assert code == 0
    assert env.peer.effects[-1].path == "/v1/videos/vid_review/stages/preview_custom/regenerate"
    assert json.loads(env.peer.effects[-1].body) == {
        "output_id": "out_reviewed", "review_id": "rev_current", "apply_suggestion": True,
    }


def test_plan_regeneration_uses_output_without_review(configured, tmp_path):
    env = configured
    prompt = tmp_path / "revised.txt"
    prompt.write_text("Preserve all four approved anchor states.", encoding="utf-8")
    env.peer.jobs["vid_plan"] = {
        "id": "vid_plan",
        "status": "in_progress",
        "stages": [{
            "id": "plan",
            "status": "awaiting_approval",
            "output": published_output(output_id="out_plan"),
        }],
    }
    code, _, _ = invoke(
        env,
        "regenerate",
        "--job-id", "vid_plan",
        "--stage", "plan",
        "--output-id", "out_plan",
        "--prompt-file", str(prompt),
        "--operation-id", "regenerate-plan",
        "--confirmed",
    )
    assert code == 0
    assert json.loads(env.peer.effects[-1].body) == {
        "output_id": "out_plan",
        "prompt": "Preserve all four approved anchor states.",
    }


@pytest.mark.parametrize("changed", ["output", "review"])
def test_regenerate_rejects_stale_review_binding(configured, changed):
    env = configured
    env.peer.jobs["vid_review"] = {
        "id": "vid_review", "status": "in_progress",
        "stages": [{
            "id": "preview", "status": "awaiting_approval",
            "output": {**published_output(), "review": {"review_id": "rev_current"}},
        }],
    }
    output_id = "out_old" if changed == "output" else "out_reviewed"
    review_id = "rev_old" if changed == "review" else "rev_current"
    code, value, _ = invoke(
        env, "regenerate", "--job-id", "vid_review", "--stage", "preview",
        "--output-id", output_id, "--review-id", review_id,
        "--operation-id", "stale", "--confirmed",
    )
    assert code == 2 and value["error"]["code"] == "stale_review"
    assert env.peer.effects == []


@pytest.mark.parametrize("action", ["approve", "start", "regenerate", "video"])
def test_mutations_require_explicit_confirmation_flags(configured, action):
    if action == "video":
        arguments = ["video", "--prompt", "A cup", "--operation-id", "unconfirmed"]
    else:
        arguments = [action, "--job-id", "vid_review", "--stage", "preview",
                     "--output-id", "out_reviewed", "--operation-id", "unconfirmed"]
        if action == "regenerate":
            arguments.extend(["--review-id", "rev_reviewed"])
    with pytest.raises(SystemExit) as error:
        configured.media.arguments(arguments)
    assert error.value.code == 2
    assert configured.peer.requests == []


def test_install_copies_only_four_public_files_and_is_idempotent(modules, tmp_path):
    source, target = tmp_path / "source", tmp_path / "installed"
    for name in PUBLIC_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((SKILL / name).read_bytes())
    (source / "router-key").write_text(FAKE_KEY)
    (source / "private-config.json").write_text(json.dumps({"api_key": FAKE_KEY}))
    hashes = modules.installer.install(source, target)
    assert set(hashes) == PUBLIC_FILES == set(modules.installer.FILES)
    installed = {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()}
    assert installed == PUBLIC_FILES
    for name in installed:
        assert (target / name).read_bytes() == (source / name).read_bytes()
        assert hashes[name] == hashlib.sha256((target / name).read_bytes()).hexdigest()
        assert FAKE_KEY.encode() not in (target / name).read_bytes()
    before = {name: (target / name).stat().st_mtime_ns for name in installed}
    assert modules.installer.install(source, target) == hashes
    assert before == {name: (target / name).stat().st_mtime_ns for name in installed}


@pytest.mark.parametrize("returncode", [0, 1])
def test_public_windows_skill_acl_is_separate_from_credentials(modules, tmp_path, monkeypatch, returncode):
    target = tmp_path / "Ivan's skills" / "siyuan-media"
    calls = []
    monkeypatch.setattr(modules.installer, "os", SimpleNamespace(name="nt"))

    def execute(command, **kwargs):
        calls.append(command)
        assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        script = base64.b64decode(command[4]).decode("utf-16-le")
        assert "$root = '" + str(target.absolute()).replace("'", "''") + "'" in script
        assert "& icacls $root /reset /T /Q" in script
        assert "ReadAndExecute" in script and "AreAccessRulesProtected" in script
        assert "router-key" not in script and "SIYUAN\\Media" not in script
        assert kwargs == {"capture_output": True, "timeout": 60}
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(modules.installer, "subprocess", SimpleNamespace(run=execute))
    if returncode:
        with pytest.raises(modules.media.ClientError) as error:
            modules.installer.restore_windows_skill_inheritance(target)
        assert error.value.code == "skill_acl_failed"
    else:
        modules.installer.restore_windows_skill_inheritance(target)
    assert len(calls) == 1


def test_installer_does_not_report_success_when_desktop_acl_fails(modules, tmp_path, monkeypatch):
    target = tmp_path / "installed"
    monkeypatch.setattr(sys, "argv", ["install.py", "--skill-dir", str(target)])
    def fail(path):
        assert path == target
        raise modules.media.ClientError("skill_acl_failed", "Desktop access failed.")
    monkeypatch.setattr(modules.installer, "restore_windows_skill_inheritance", fail)
    output = io.StringIO()
    with redirect_stdout(output):
        code = modules.installer.main()
    assert code == 2
    assert json.loads(output.getvalue())["error"]["code"] == "skill_acl_failed"
    assert '"installed": true' not in output.getvalue()


@pytest.mark.parametrize("difference", ["modified", "extra", "symlink"])
def test_install_refuses_foreign_or_modified_destination_without_writes(modules, tmp_path, difference):
    target = tmp_path / "installed"
    if difference == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    else:
        modules.installer.install(SKILL, target)
        if difference == "modified":
            (target / "scripts/media.py").write_bytes(b"USER MODIFICATION")
        else:
            (target / "user-notes.txt").write_bytes(b"KEEP")
    before = {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    with pytest.raises(modules.media.ClientError) as error:
        modules.installer.install(SKILL, target)
    assert error.value.code in {"existing_skill_changed", "unsafe_install_path"}
    after = {str(path.relative_to(target)): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    assert after == before


def test_in_place_install_rejects_extra_private_files(modules, tmp_path):
    target = tmp_path / "installed"
    modules.installer.install(SKILL, target)
    (target / "router-key").write_text(FAKE_KEY)
    with pytest.raises(modules.media.ClientError) as error:
        modules.installer.install(target, target)
    assert error.value.code == "existing_skill_changed"
    assert (target / "router-key").read_text() == FAKE_KEY


@pytest.fixture
def upgrade_skill(modules, tmp_path):
    old, new, target = tmp_path / "old", tmp_path / "new", tmp_path / "installed"
    for name in modules.installer.FILES:
        for source, version in ((old, "old"), (new, "new")):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            # Leave one file unchanged to verify that upgrades do not rewrite it.
            path.write_text(name + ":" + (version if name != "SKILL.md" else "unchanged"))
    expected = modules.installer.install(old, target)
    return SimpleNamespace(old=old, new=new, target=target, expected=expected)


def installed_snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def test_upgrade_requires_exact_previous_hash_and_is_idempotent(modules, upgrade_skill):
    skill = upgrade_skill
    before = installed_snapshot(skill.target)
    expected = dict(skill.expected)
    hashes = modules.installer.install(skill.new, skill.target, expected=expected)
    assert set(hashes) == PUBLIC_FILES
    assert expected == skill.expected
    for name in PUBLIC_FILES:
        assert (skill.target / name).read_bytes() == (skill.new / name).read_bytes()
        assert hashes[name] == hashlib.sha256((skill.new / name).read_bytes()).hexdigest()
    after = installed_snapshot(skill.target)
    assert after["SKILL.md"] == before["SKILL.md"]
    assert hashes != expected
    # Repeating the same upgrade with the original manifest must not rewrite current files.
    assert modules.installer.install(skill.new, skill.target, expected=expected) == hashes
    assert installed_snapshot(skill.target) == after


@pytest.mark.parametrize("case", [
    "no_expected", "empty_expected", "missing_hash", "wrong_hash", "new_hash",
    "user_modified", "extra_file", "symlink_file",
])
def test_upgrade_rejects_unapproved_changes_before_any_write(modules, upgrade_skill, tmp_path, case):
    skill = upgrade_skill
    expected = dict(skill.expected)
    # Fail on the last file so a partial-update implementation would be detected.
    last = modules.installer.FILES[-1]
    if case == "no_expected":
        expected = None
    elif case == "empty_expected":
        expected = {}
    elif case == "missing_hash":
        expected.pop(last)
    elif case == "wrong_hash":
        expected[last] = "0" * 64
    elif case == "new_hash":
        expected[last] = hashlib.sha256((skill.new / last).read_bytes()).hexdigest()
    elif case == "user_modified":
        (skill.target / last).write_text("USER MODIFICATION")
    elif case == "extra_file":
        (skill.target / "user-notes.txt").write_text("KEEP")
    else:
        outside = tmp_path / "outside.py"
        outside.write_bytes((skill.target / last).read_bytes())
        (skill.target / last).unlink()
        (skill.target / last).symlink_to(outside)
    before = installed_snapshot(skill.target)
    with pytest.raises(modules.media.ClientError) as error:
        modules.installer.install(skill.new, skill.target, expected=expected)
    assert error.value.code == "existing_skill_changed"
    assert installed_snapshot(skill.target) == before


@pytest.mark.parametrize("manifest_kind", ["valid", "absent", "wrong_hash", "missing_file", "extra_file", "not_object"])
def test_installer_cli_upgrade_manifest_is_opt_in(modules, upgrade_skill, tmp_path, manifest_kind):
    skill = upgrade_skill
    manifest = tmp_path / "previous-public-files.json"
    expected = dict(skill.expected)
    if manifest_kind == "wrong_hash":
        expected[modules.installer.FILES[-1]] = "0" * 64
    elif manifest_kind == "missing_file":
        expected.pop(modules.installer.FILES[-1])
    elif manifest_kind == "extra_file":
        expected["unexpected.txt"] = "0" * 64
    elif manifest_kind == "not_object":
        expected = list(expected)
    manifest.write_text(json.dumps(expected))
    before = installed_snapshot(skill.target)
    args = [sys.executable, "-B", str(SKILL / "scripts/install.py"),
            "--skill-dir", str(skill.target), "--config", str(tmp_path / "unused/config.json")]
    if manifest_kind != "absent":
        args.extend(["--upgrade-manifest", str(manifest)])
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)},
    )
    value = json.loads(result.stdout)
    if manifest_kind == "valid":
        assert result.returncode == 0, result.stdout + result.stderr
        assert value["installed"] is True and set(value["files"]) == PUBLIC_FILES
        for name in PUBLIC_FILES:
            assert (skill.target / name).read_bytes() == (SKILL / name).read_bytes()
            assert value["files"][name] == hashlib.sha256((SKILL / name).read_bytes()).hexdigest()
    else:
        assert result.returncode == 2
        assert value["error"]["code"] == (
            "existing_skill_changed" if manifest_kind in {"absent", "wrong_hash"} else "invalid_upgrade_manifest"
        )
        assert installed_snapshot(skill.target) == before
    assert not (tmp_path / "unused").exists()
    assert_no_secrets(result.stdout + result.stderr)


@pytest.mark.parametrize("previous_install", [False, True], ids=["fresh", "upgrade"])
@pytest.mark.parametrize("configure", [False, True], ids=["public-only", "with-credentials"])
def test_manage_passes_only_previous_public_hashes_to_installer(
    modules, upgrade_skill, tmp_path, monkeypatch, previous_install, configure,
):
    # Isolate manage.py's imports and every external interaction before calling deploy().
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "install", modules.installer)
    manager = load_module("workbuddy_manage_test_subject", SKILL.parent / "manage.py")
    evidence = tmp_path / "operator-evidence"
    evidence.mkdir()
    package = tmp_path / "public-skill.zip"
    package.write_bytes(b"fake public package")
    expected = dict(upgrade_skill.expected)
    if previous_install:
        (evidence / "windows-install.json").write_text(json.dumps({
            "installed": True, "files": expected, "credentials": {"configured": True},
        }))
    monkeypatch.setattr(manager, "EVIDENCE", evidence)
    monkeypatch.setattr(manager, "PACKAGE", package)
    monkeypatch.setattr(manager, "PRIVATE", tmp_path / "unused-private")
    monkeypatch.setattr(manager, "CREDENTIAL", tmp_path / "unused-private/config.json")
    credentials = {"api_key": FAKE_KEY, "base_url": modules.media.ROUTER_URL, "client_id": "test-user"}
    monkeypatch.setattr(manager, "private_credentials", lambda: dict(credentials) if configure
                        else pytest.fail("A public-only deployment must not read or resend credentials"))
    monkeypatch.setattr(manager, "package", lambda: {"credentials_included": False})
    monkeypatch.setattr(manager, "admin_client", lambda: pytest.fail("No Router access is allowed"))
    copied, commands = [], []
    installed = {"installed": True, "files": expected}
    if configure:
        installed["credentials"] = {"configured": True}

    def copy_public(args, **kwargs):
        assert args[:3] == ["scp", "-o", "BatchMode=yes"]
        assert Path(args[3]).is_relative_to(tmp_path)
        assert FAKE_KEY not in " ".join(args)
        copied.append(args)
        return SimpleNamespace(returncode=0)

    def remote(command, **kwargs):
        commands.append((command, kwargs))
        assert FAKE_KEY not in command
        if "scripts/install.py" in command:
            if configure:
                assert json.loads(kwargs["input_data"]) == credentials
                assert kwargs["secret"] == FAKE_KEY
            else:
                assert kwargs["input_data"] is None and kwargs["secret"] is None
            assert ("--credentials-stdin" in command) == configure
            assert ("--upgrade-manifest" in command) == previous_install
            if previous_install:
                assert command.endswith("--upgrade-manifest " + manager.WINDOWS_ROOT + "/skill-previous-files.json")
                assert len(copied) == 2
            return json.dumps(installed)
        assert kwargs == {}
        return ""

    monkeypatch.setattr(manager, "subprocess", SimpleNamespace(run=copy_public))
    monkeypatch.setattr(manager, "remote", remote)
    result = manager.deploy(configure=configure)
    if not configure:
        installed["credentials_updated"] = False
        if previous_install:
            installed["credentials"] = {"configured": True}
    assert result == installed
    assert len(copied) == (2 if previous_install else 1)
    assert sum("scripts/install.py" in command for command, _ in commands) == 1
    manifest = evidence / "skill-previous-files.json"
    if previous_install:
        assert json.loads(manifest.read_text()) == expected
        assert Path(copied[1][3]) == manifest
        assert_no_secrets(manifest.read_text())
    else:
        assert not manifest.exists()
    assert json.loads((evidence / "windows-install.json").read_text()) == installed
    assert_no_secrets(result)
    assert not manager.PRIVATE.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential modes are checked on Linux")
def test_installer_stdin_keeps_key_outside_skill_and_stdout(tmp_path):
    target, config = tmp_path / "installed", tmp_path / "private/config.json"
    result = subprocess.run(
        [sys.executable, "-B", str(SKILL / "scripts/install.py"), "--skill-dir", str(target),
         "--config", str(config), "--credentials-stdin"],
        input=json.dumps({"base_url": "http://127.0.0.1:4000", "client_id": "test-user", "api_key": FAKE_KEY}),
        text=True, capture_output=True, timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert set(value["files"]) == PUBLIC_FILES
    assert value["credentials"]["secret_returned"] is False
    assert FAKE_KEY not in str(result.args)
    assert_no_secrets(result.stdout + result.stderr)
    assert FAKE_KEY not in config.read_text()
    key = config.parent / "router-key"
    assert key.read_text() == FAKE_KEY
    assert config.stat().st_mode & 0o777 == key.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()} == PUBLIC_FILES


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and non-Windows DPAPI guard tests")
@pytest.mark.parametrize("change", ["config_mode", "key_mode", "key_symlink", "dpapi_config"])
def test_private_credentials_fail_closed(configured, tmp_path, change):
    env, key = configured, configured.config.parent / "router-key"
    expected = "unsafe_permissions"
    if change == "config_mode":
        env.config.chmod(0o644)
    elif change == "key_mode":
        key.chmod(0o644)
    elif change == "key_symlink":
        replacement = tmp_path / "outside-key"
        replacement.write_bytes(key.read_bytes())
        replacement.chmod(0o600)
        key.unlink()
        key.symlink_to(replacement)
        expected = "credential_unavailable"
    else:
        config = json.loads(env.config.read_text())
        config["protection"] = "windows-dpapi-current-user"
        env.media.atomic_write(env.config, env.media.encode(config))
        expected = "dpapi_unavailable"
    with pytest.raises(env.media.ClientError) as error:
        env.media.Client(env.config)
    assert error.value.code == expected
    assert env.peer.requests == []


@pytest.mark.skipif(os.name == "nt", reason="This verifies non-Windows fail-closed behavior, not native DPAPI")
@pytest.mark.parametrize("decrypt", [False, True])
def test_dpapi_unavailable_does_not_return_plaintext(modules, decrypt):
    with pytest.raises(modules.media.ClientError) as error:
        modules.media.dpapi(FAKE_KEY.encode(), decrypt=decrypt)
    assert error.value.code == "dpapi_unavailable"
    assert_no_secrets(error.value.public())


def test_windows_guard_rejects_plaintext_credential_mode(configured, monkeypatch):
    # Patch only the client module's OS facade, not pathlib or the process-wide os.name.
    monkeypatch.setattr(configured.media, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    with pytest.raises(configured.media.ClientError) as error:
        configured.media.Client(configured.config)
    assert error.value.code == "invalid_configuration"
    assert configured.peer.requests == []


def test_workflow_http_create_resume_action_and_status(configured, tmp_path):
    env = configured
    workflow_id = "wf_" + "a" * 32
    workflow = {"id": workflow_id, "status": "draft", "revision": 1, "spec": {"kind": "video"}, "jobs": {}}
    effects = {}
    def reply(request):
        if request.method == "GET":
            assert request.path == "/v1/media/workflows/" + workflow_id
            return 200, workflow, {}
        key = request.headers["Idempotency-Key"]
        if key not in effects:
            effects[key] = (request.path, request.body)
            if request.path.endswith("/actions"):
                assert json.loads(request.body) == {"action": "cancel", "revision": 1}
                workflow["status"] = "cancelled"
            else:
                assert request.path == "/v1/media/workflows"
        else:
            assert effects[key] == (request.path, request.body)
        if env.peer.drop_next:
            env.peer.drop_next = False
            return None
        return 202, workflow, {}
    env.peer.reply = reply
    env.peer.drop_next = True
    code, result, _ = invoke(env, "workflow-create", "--operation-id", "wf-create", "--prompt", "Create a beach video")
    assert code == 2 and result["error"]["retry"].startswith("resume")
    code, result, _ = invoke(env, "resume", "--operation-id", "wf-create")
    assert code == 0 and result["id"] == workflow_id and len(effects) == 1
    code, result, _ = invoke(env, "resume", "--operation-id", "wf-create")
    assert code == 0 and result["revision"] == 1 and len(effects) == 1
    body = tmp_path / "cancel.json"
    body.write_text(json.dumps({"action": "cancel", "revision": 1}))
    code, result, _ = invoke(env, "workflow-action", "--operation-id", "wf-cancel", "--workflow-id", workflow_id, "--request-file", body)
    assert code == 0 and result["status"] == "cancelled" and len(effects) == 2
    code, result, _ = invoke(env, "status", "--job-id", workflow_id)
    assert code == 0 and result["status"] == "cancelled"
    assert_no_secrets(result)


def test_workflow_http_asset_upload_and_download(configured, tmp_path):
    env = configured
    workflow_id = "wf_" + "b" * 32
    image = tmp_path / "input.png"
    image.write_bytes(ARTIFACT)
    workflow = {"id": workflow_id, "status": "completed", "image_job_id": "img_child", "jobs": {
        "img_child": {"id": "img_child", "output": published_output()},
    }}
    def reply(request):
        if request.path == "/v1/media/assets":
            body = json.loads(request.body)
            assert body["role"] == "product" and base64.b64decode(body["data"]) == ARTIFACT
            return 200, {"id": "asset_test", "role": "product"}, {}
        if request.path == "/v1/media/workflows/" + workflow_id:
            return 200, workflow, {}
        assert request.path == "/v1/media/outputs/out_delivery/content"
        return 200, ARTIFACT, {}
    env.peer.reply = reply
    code, result, _ = invoke(env, "upload", "--image", image, "--role", "product")
    assert code == 0 and result["id"] == "asset_test"
    for selection in ([], ["--artifact-id", "out_delivery"]):
        target = tmp_path / ("selected.png" if selection else "final.png")
        code, result, _ = invoke(env, "download", "--job-id", workflow_id, "--output", target, *selection)
        assert code == 0 and target.read_bytes() == ARTIFACT
        assert result["id"] == workflow_id
        assert_no_secrets(result)


@pytest.mark.parametrize("path", ["/v1/media/workflows/wf_invalid/actions", "/v1/media/workflows/wf_" + "a" * 32 + "/admin", "/v1/media/assets/../options"])
def test_workflow_paths_keep_narrow_allowlist(configured, path):
    with pytest.raises(configured.media.ClientError, match="media API paths"):
        configured.client.request("GET", path)
    assert configured.peer.requests == []
