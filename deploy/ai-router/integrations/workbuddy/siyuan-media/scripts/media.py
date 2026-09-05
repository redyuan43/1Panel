#!/usr/bin/env python3
"""Local-only credential handling and SIYUAN media operations. Python 3.10+."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


ROUTER_URL = "http://ai-x10drg.taild500c8.ts.net:4000"
ALLOWED_ORIGINS = {ROUTER_URL, "http://127.0.0.1:4000"}
ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_UPLOAD = 512 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
STAGES = ("context_ir", "preview", "proof", "local_768", "cloud_768", "regenerate_2k")
PRIVATE_FIELDS = {"api_key", "authorization", "access", "content_url", "url", "b64_json",
                  "provider_state", "provider_errors", "source_path", "source_sha256"}


class ClientError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def public(self):
        return {"error": {"code": self.code, "message": self.message, **self.details}}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ClientError("invalid_identifier", "Identifiers must contain only letters, digits, hyphens or underscores.")
    return value


def validate_base(value):
    value = str(value).rstrip("/")
    if value not in ALLOWED_ORIGINS:
        raise ClientError("untrusted_origin", "Only the configured SIYUAN Router origin is allowed.")
    return value


def state_home():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "SIYUAN" / "Media"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "siyuan-media"


def secure_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ClientError("unsafe_local_path", "A private directory cannot be a symbolic link.")
    # Windows descendants inherit the installer's explicit user/SYSTEM DACL.
    path.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def atomic_write(path, data):
    path = Path(path)
    secure_dir(path.parent)
    if path.is_symlink():
        raise ClientError("unsafe_local_path", "Refusing to replace a symbolic link.")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def dpapi(data, *, decrypt=False):
    if os.name != "nt":
        raise ClientError("dpapi_unavailable", "Windows DPAPI is unavailable on this platform.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        # UI_FORBIDDEN only: never use LOCAL_MACHINE, which permits other users.
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise ClientError("credential_protection_failed", "Windows credential protection failed.")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def configure_credentials(config_path, value):
    if not isinstance(value, dict) or set(value) - {"base_url", "client_id", "api_key"}:
        raise ClientError("invalid_configuration", "Unexpected credential configuration.")
    key = value.get("api_key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{20,512}", key):
        raise ClientError("invalid_credential", "A valid Router Client Key is required.")
    base = validate_base(value.get("base_url", ROUTER_URL))
    client_id = validate_id(value.get("client_id", "workbuddy-media"))
    config_path = Path(config_path)
    secure_dir(config_path.parent)
    method = "windows-dpapi-current-user" if os.name == "nt" else "private-file"
    secret_name = "router-key.dpapi" if os.name == "nt" else "router-key"
    if config_path.exists() or (config_path.parent / secret_name).exists():
        if config_path.is_file():
            current = Client(config_path)
            if current.key == key and current.base == base and current.owner == client_id:
                return {"configured": True, "client_id": client_id, "base_url": base,
                        "protection": current.config["protection"], "secret_returned": False}
        raise ClientError("configuration_exists", "Existing credentials were preserved; use a new private configuration directory.")
    protected = dpapi(key.encode()) if os.name == "nt" else key.encode()
    atomic_write(config_path.parent / secret_name, protected)
    atomic_write(config_path, encode({"version": 1, "base_url": base, "client_id": client_id,
                                     "protection": method, "credential_file": secret_name}))
    return {"configured": True, "client_id": client_id, "base_url": base,
            "protection": method, "secret_returned": False}


def read_private(path, maximum):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ClientError("credential_unavailable", "Private configuration is unavailable.")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ClientError("unsafe_permissions", "Private configuration must have mode 0600.")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ClientError("invalid_configuration", "Private configuration is too large.")
    return data


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("redirect_refused", "Router redirects are not followed.")


class Client:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        try:
            self.config = json.loads(read_private(self.config_path, 8192))
            self.base = validate_base(self.config["base_url"])
            self.owner = validate_id(self.config["client_id"])
            name = self.config["credential_file"]
            if name not in {"router-key", "router-key.dpapi"}:
                raise ClientError("invalid_configuration", "Unexpected credential file.")
            data = read_private(self.config_path.parent / name, 16384)
            if self.config["protection"] == "windows-dpapi-current-user":
                data = dpapi(data, decrypt=True)
            elif self.config["protection"] != "private-file" or os.name == "nt":
                raise ClientError("invalid_configuration", "Unsupported credential protection.")
            self.key = data.decode("ascii").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{20,512}", self.key):
                raise ValueError()
        except (KeyError, ValueError, UnicodeError) as exc:
            raise ClientError("invalid_configuration", "Private configuration cannot be read.") from exc
        self.root = secure_dir(self.config_path.parent / "operations")
        # Never forward the bearer credential through an environment/system proxy.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def redact(self, value):
        if isinstance(value, str):
            value = value.replace(self.key, "[REDACTED]")
            value = re.sub(r"data:[^,\s]*;base64,[A-Za-z0-9+/=_-]+",
                           "[MEDIA_DATA]", value, flags=re.IGNORECASE)
            def scrub_url(match):
                url = match.group()
                try:
                    parsed = urlsplit(url)
                    if any(key.lower() in {"access", "token", "api_key", "authorization"}
                           for key, _ in parse_qsl(parsed.query)):
                        return "[PRIVATE_MEDIA_URL]"
                except ValueError:
                    return "[INVALID_URL]"
                return url
            return re.sub(r"(?:https?://|/)[^\s<>\"']+", scrub_url, value, flags=re.IGNORECASE)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()
                    if key.lower() not in PRIVATE_FIELDS and not key.startswith("source_")}
        return value

    def summary(self, value):
        allowed = {"id", "object", "kind", "model", "status", "created_at", "updated_at",
                   "error", "actual_duration", "fallback_applied", "operation_id",
                   "progress", "output_id", "run_id", "content_type", "bytes", "sha256",
                   "width", "height", "text", "stage", "transparent", "next_cursor",
                   "local_path", "request_id"}
        def select(item):
            if isinstance(item, list):
                return [select(part) for part in item]
            if not isinstance(item, dict):
                return item
            result = {key: part for key, part in item.items() if key in allowed}
            for key in ("output", "stages"):
                if isinstance(item.get(key), (dict, list)):
                    result[key] = select(item[key])
            if isinstance(item.get("data"), list) and all(
                isinstance(part, dict) and "id" in part for part in item["data"]
            ):
                result["data"] = select(item["data"])
            return result
        return self.redact(select(value))

    def open(self, method, path, body=None, headers=None, timeout=30):
        if not re.fullmatch(r"/v1/(?:media/options|media/outputs/[A-Za-z0-9_-]+/content|"
                            r"images(?:/[A-Za-z0-9_-]+){0,3}|videos(?:/[A-Za-z0-9_-]+){0,5})", path):
            raise ClientError("invalid_api_path", "Only media API paths are allowed.")
        request = Request(self.base + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self.key, "User-Agent": "siyuan-media-skill/1",
            "Accept": "application/json", **(headers or {}),
        })
        try:
            return self.opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                exc.close()
                raise ClientError("redirect_refused", "Router redirects are not followed.")
            return exc
        except (URLError, TimeoutError, socket.timeout, OSError, ssl.SSLError) as exc:
            raise ClientError("transport_unavailable", "Router request outcome is unknown; reuse the original operation ID.") from exc

    def request(self, method, path, body=None, headers=None):
        with self.open(method, path, body, headers) as response:
            request_id = self.redact(response.headers.get("X-Request-ID"))
            data = response.read(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise ClientError("response_too_large", "Router response exceeds the client limit.")
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise ClientError("invalid_response", "Router returned a non-JSON response.") from exc
            if not isinstance(value, dict):
                raise ClientError("invalid_response", "Router returned an unexpected response.")
            if response.status >= 400 and not str(value.get("id", "")).startswith(("img_", "vid_")):
                error = value.get("error") or {}
                raise ClientError(str(error.get("code", "media_request_failed")),
                                  self.redact(str(error.get("message", "Media request failed."))),
                                  http_status=response.status, request_id=request_id)
            return value, request_id, response.status

    def get(self, job_id):
        validate_id(job_id)
        if not job_id.startswith(("img_", "vid_")):
            raise ClientError("invalid_job", "Expected an img_ or vid_ task ID.")
        kind = "images" if job_id.startswith("img_") else "videos"
        value, request_id, _ = self.request("GET", f"/v1/{kind}/{job_id}")
        return value, request_id

    def operate(self, operation_id, path=None, fields=None, files=None):
        validate_id(operation_id)
        directory = secure_dir(self.root / operation_id)
        with operation_lock(directory / "lock"):
            receipt_path = directory / "receipt.json"
            old = json.loads(receipt_path.read_text("utf-8")) if receipt_path.exists() else None
            if old and (old["owner"] != self.owner or old["base_url"] != self.base):
                raise ClientError("operation_owner_mismatch", "The saved operation belongs to another Router account.")
            if path is not None:
                body, content_type, digest = payload(fields or {}, files)
                if old and (old["path"] != path or old["digest"] != digest):
                    raise ClientError("operation_conflict", "Use the original input with this operation ID.")
                if not old:
                    atomic_write(directory / "request.body", body)
                    old = {"version": 1, "operation_id": operation_id, "owner": self.owner,
                           "base_url": self.base, "path": path, "digest": digest,
                           "body_sha256": hashlib.sha256(body).hexdigest(),
                           "content_type": content_type, "status": "prepared"}
                    atomic_write(receipt_path, encode(old))
            if old is None:
                raise ClientError("operation_not_found", "No saved operation exists with that ID.")
            if old.get("status") == "accepted" and old.get("job_id"):
                value, request_id = self.get(old["job_id"])
                return {**self.summary(value), "operation_id": operation_id, "request_id": request_id}
            body = (directory / "request.body").read_bytes()
            if hashlib.sha256(body).hexdigest() != old["body_sha256"]:
                raise ClientError("operation_corrupt", "The original request snapshot changed; no request was sent.")
            old["status"] = "outcome_unknown"
            atomic_write(receipt_path, encode(old))
            try:
                value, request_id, status = self.request("POST", old["path"], body, {
                    "Content-Type": old["content_type"], "Idempotency-Key": "wb-" + operation_id,
                    "Prefer": "respond-async",
                })
            except ClientError as exc:
                exc.details["operation_id"] = operation_id
                exc.details["retry"] = "resume the same operation; do not create another"
                raise
            if (not isinstance(value.get("id"), str) or not ID.fullmatch(value["id"])
                    or not value["id"].startswith(("img_", "vid_")) or self.key in value["id"]):
                raise ClientError("invalid_response", "The Router did not return a task ID.", operation_id=operation_id)
            old.update(status="accepted", job_id=value["id"], request_id=request_id)
            atomic_write(receipt_path, encode(old))
            return {**self.summary(value), "operation_id": operation_id,
                    "request_id": request_id, "http_status": status}

    def download(self, job_id, target, *, stage=None, artifact_id=None):
        job, _ = self.get(job_id)
        if stage:
            match = next((item for item in job.get("stages", []) if item["id"] == stage), None)
            output = (match or {}).get("output")
        elif artifact_id:
            validate_id(artifact_id)
            kind = "images" if job_id.startswith("img_") else "videos"
            history, _, _ = self.request("GET", f"/v1/{kind}/{job_id}/outputs")
            output = next((item for item in history.get("data", []) if item["id"] == artifact_id), None)
        else:
            output = job.get("output")
        if not output:
            raise ClientError("output_not_ready", "No archived output is available for that selection.")
        artifact = validate_id(output["id"])
        expected = output.get("sha256", "")
        expected_bytes = output.get("bytes")
        if not re.fullmatch("[a-f0-9]{64}", expected) or not isinstance(expected_bytes, int) or not 0 < expected_bytes <= MAX_UPLOAD:
            raise ClientError("invalid_artifact", "Artifact metadata is invalid.")
        target = Path(target).expanduser().absolute()
        if target.is_symlink() or target.exists():
            raise ClientError("output_exists", "The output path already exists; choose a new file.")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".siyuan-download-", dir=target.parent)
        digest, count = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as handle, self.open("GET", f"/v1/media/outputs/{artifact}/content") as response:
                if response.status != 200:
                    raise ClientError("download_failed", "Artifact download failed.", http_status=response.status)
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > expected_bytes:
                        raise ClientError("artifact_mismatch", "Artifact size exceeds its published size.")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if count != expected_bytes or digest.hexdigest() != expected:
                raise ClientError("artifact_mismatch", "Artifact hash or size did not match.")
            # Atomic no-clobber publication on NTFS and POSIX filesystems.
            os.link(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.redact({"id": job_id, "artifact_id": artifact, "output_id": output["output_id"],
                            "local_path": str(target), "bytes": count, "sha256": expected,
                            "content_type": output["content_type"]})


@contextlib.contextmanager
def operation_lock(path):
    with Path(path).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ClientError("operation_busy", "This operation is already running; query it instead.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def payload(fields, files=None):
    if files is None:
        body = encode(fields)
        return body, "application/json", hashlib.sha256(body).hexdigest()
    boundary = "siyuan-" + uuid4().hex
    parts, proofs, size = [], [], 0
    for name, value in fields.items():
        validate_id(name)
        text = str(value).lower() if isinstance(value, bool) else str(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + text.encode() + b"\r\n")
    for index, (name, filename) in enumerate(files):
        validate_id(name)
        source = Path(filename).expanduser()
        if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_UPLOAD:
            raise ClientError("invalid_upload", "An upload is missing, too large, or is a symbolic link.")
        data = source.read_bytes()
        size += len(data)
        if size > MAX_UPLOAD or len(data) > MAX_UPLOAD:
            raise ClientError("upload_too_large", "Combined uploads exceed the limit.")
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        proofs.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "content_type": media_type})
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="asset-{index}"\r\n'
                      f"Content-Type: {media_type}\r\n\r\n".encode(), data, b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary, hashlib.sha256(encode([fields, proofs])).hexdigest()


def prompt(args):
    value = Path(args.prompt_file).read_text("utf-8") if args.prompt_file else args.prompt
    if not value or not 1 <= len(value.strip()) <= 16000:
        raise ClientError("invalid_prompt", "Supply a prompt of 1-16000 characters.")
    return value.strip()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=state_home() / "config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("options")
    sub.add_parser("new-operation")
    listing = sub.add_parser("list")
    listing.add_argument("--kind", choices=("image", "video"), required=True)
    for name in ("status", "outputs", "wait", "download"):
        command = sub.add_parser(name)
        command.add_argument("--job-id", required=True)
        if name == "wait":
            command.add_argument("--seconds", type=int, default=30, choices=range(0, 121))
        if name == "download":
            selection = command.add_mutually_exclusive_group()
            selection.add_argument("--stage", choices=STAGES)
            selection.add_argument("--artifact-id")
            command.add_argument("--output", required=True)
    for name in ("image", "edit", "video"):
        command = sub.add_parser(name)
        command.add_argument("--operation-id", required=True)
        text = command.add_mutually_exclusive_group(required=True)
        text.add_argument("--prompt")
        text.add_argument("--prompt-file")
        if name in {"image", "edit"}:
            command.add_argument("--use-case", choices=("photo", "product", "ui", "infographic", "illustration", "logo"), default="photo")
            command.add_argument("--aspect-ratio", choices=("auto", "square", "landscape", "portrait"), default="auto")
            command.add_argument("--background", choices=("auto", "transparent", "opaque"), default="auto")
            if name == "edit":
                command.add_argument("--image", action="append", required=True)
        else:
            command.add_argument("--strategy", choices=("fast", "safe", "cloud"), default="fast")
            command.add_argument("--mode", choices=("t2v", "i2v", "l2v", "fl2v", "reference", "hybrid"), default="t2v")
            command.add_argument("--duration", type=int, default=4, choices=range(4, 16))
            command.add_argument("--seed", type=int, default=-1)
            command.add_argument("--audio-policy", choices=("native", "reference", "lock_source"), default="native")
            command.add_argument("--watermark", action="store_true")
            command.add_argument("--use-embedded-video-audio", action="store_true")
            command.add_argument("--asset", action="append", default=[], metavar="NAME=PATH")
            command.add_argument("--confirm-context-cost", action="store_true", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--operation-id", required=True)
    for name in ("approve", "start", "cancel"):
        command = sub.add_parser(name)
        command.add_argument("--operation-id", required=True)
        command.add_argument("--job-id", required=True)
        command.add_argument("--stage", choices=STAGES, required=name != "cancel")
        command.add_argument("--confirmed", action="store_true", required=True)
        if name != "cancel":
            command.add_argument("--output-id", required=True)
        if name == "approve":
            command.add_argument("--prompt-file")
    return parser.parse_args(argv)


def run(args, client):
    cmd = args.command
    if cmd in {"doctor", "options"}:
        value, request_id, _ = client.request("GET", "/v1/media/options")
        return client.redact({"reachable": True, "client_id": client.owner,
                              "credential_protection": client.config["protection"],
                              "request_id": request_id, "options": value})
    if cmd == "resume":
        return client.operate(args.operation_id)
    if cmd == "list":
        value, request_id, _ = client.request("GET", "/v1/" + args.kind + "s")
        return {**client.summary(value), "request_id": request_id}
    if cmd in {"status", "wait"}:
        deadline = time.monotonic() + (args.seconds if cmd == "wait" else 0)
        while True:
            value, request_id = client.get(args.job_id)
            running = any(stage["status"] in {"queued", "running", "archiving"} for stage in value.get("stages", []))
            if (cmd == "status" or time.monotonic() >= deadline or value.get("status") in {"completed", "failed", "cancelled"}
                    or value.get("stages") and not running):
                return {**client.summary(value), "request_id": request_id}
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    if cmd == "outputs":
        validate_id(args.job_id)
        kind = "images" if args.job_id.startswith("img_") else "videos"
        value, request_id, _ = client.request("GET", f"/v1/{kind}/{args.job_id}/outputs")
        return {**client.summary(value), "request_id": request_id}
    if cmd == "download":
        return client.download(args.job_id, args.output, stage=args.stage, artifact_id=args.artifact_id)
    if cmd in {"image", "edit"}:
        fields = {"model": "siyuan-image", "prompt": prompt(args), "n": 1, "response_format": "url",
                  "use_case": args.use_case, "aspect_ratio": args.aspect_ratio, "background": args.background}
        files = None
        if cmd == "edit":
            if not 1 <= len(args.image) <= 5:
                raise ClientError("invalid_references", "Editing requires 1-5 reference images.")
            for name in args.image:
                if Path(name).expanduser().stat().st_size > 10 * 1024 * 1024:
                    raise ClientError("upload_too_large", "Each reference image must be at most 10 MiB.")
            files = [("image", path) for path in args.image]
        return client.operate(args.operation_id, "/v1/images/" + ("edits" if cmd == "edit" else "generations"), fields, files)
    if cmd == "video":
        fields = {"model": "siyuan-video", "name": "WorkBuddy video", "prompt": prompt(args), "mode": args.mode,
                  "strategy": args.strategy, "duration": args.duration, "seed": args.seed,
                  "audio_policy": args.audio_policy, "watermark": args.watermark,
                  "use_embedded_video_audio": args.use_embedded_video_audio}
        files = []
        for item in args.asset:
            name, separator, path = item.partition("=")
            if not separator:
                raise ClientError("invalid_asset", "Use --asset NAME=PATH.")
            files.append((name, path))
        return client.operate(args.operation_id, "/v1/videos", fields, files)
    validate_id(args.job_id)
    is_image = args.job_id.startswith("img_")
    if is_image:
        if cmd != "cancel" or args.stage:
            raise ClientError("invalid_action", "Images support cancellation without a stage.")
        path, fields = f"/v1/images/{args.job_id}/cancel", {}
    else:
        if not args.job_id.startswith("vid_") or not args.stage:
            raise ClientError("invalid_action", "Video actions require a video ID and a stage.")
        path = f"/v1/videos/{args.job_id}/stages/{args.stage}/{cmd}"
        fields = {} if cmd == "cancel" else {"output_id": validate_id(args.output_id)}
        if cmd == "approve" and args.prompt_file:
            if args.stage != "context_ir":
                raise ClientError("invalid_action", "Only Context IR accepts an edited prompt.")
            fields["prompt"] = Path(args.prompt_file).read_text("utf-8")
    return client.operate(args.operation_id, path, fields)


def main(argv=None):
    os.umask(0o077)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    client = None
    try:
        args = arguments(argv)
        if args.command == "new-operation":
            result = {"operation_id": uuid4().hex}
        else:
            client = Client(args.config)
            result = run(args, client)
        print(json.dumps(client.redact(result) if client else result, ensure_ascii=False))
        return 0
    except ClientError as exc:
        result = exc.public()
        print(json.dumps(client.redact(result) if client else result, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "local_client_error",
                                    "message": "Local media operation failed; no credentials were returned."}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
