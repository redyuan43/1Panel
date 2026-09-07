#!/usr/bin/env python3
"""Single-node, authenticated llama.cpp prefix cache gateway.

Request bodies are forwarded unchanged. Only text requests with an unambiguous
last-user boundary use snapshots; all other requests retain native behavior.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("qwen36-prefix-cache")
HOP = {"connection", "transfer-encoding", "content-length", "keep-alive", "server", "date"}
SNAPSHOT_NAME = re.compile(r"prefix-[0-9a-f]{32}\.bin(?:\.draft|\.checkpoints)?")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


class PrefixCache:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["cache_directory"])
        self.data = self.root / "data"
        self.manifests = self.root / "manifests"
        for directory in (self.root, self.data, self.manifests):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        self.key = Path(config["api_key_file"]).read_text().strip()
        self.lock = threading.Lock()
        self.active = None
        self.pid = None
        self.fingerprint = None
        self.hashes = {}
        self.hash_path = self.root / "artifact-hashes.json"
        if self.hash_path.exists():
            try:
                self.hashes = json.loads(self.hash_path.read_text())
                if not isinstance(self.hashes, dict):
                    self.hashes = {}
            except (ValueError, OSError):
                self.hashes = {}
        self.prune(None)

    def open(self, path, raw=None, headers=None):
        request = urllib.request.Request(
            self.config["backend_url"] + path, raw,
            {**(headers or {}), "Authorization": "Bearer " + self.key},
        )
        return urllib.request.urlopen(request, timeout=self.config.get("backend_timeout", 1800))

    def post(self, path, value):
        with self.open(path, encode(value), {"Content-Type": "application/json"}) as response:
            return json.load(response)

    def runtime(self):
        pid = subprocess.check_output(
            ["systemctl", "show", self.config["backend_unit"], "-p", "MainPID", "--value"], text=True,
        ).strip()
        if not pid.isdigit() or int(pid) <= 0:
            raise RuntimeError("backend_not_running")
        files = {}
        for name, filename in self.config["runtime_files"].items():
            path = Path(filename)
            stat = path.stat()
            stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            saved = self.hashes.get(str(path))
            if not isinstance(saved, dict) or saved.get("stat") != stamp or not isinstance(saved.get("sha256"), str):
                saved = {"stat": stamp, "sha256": file_digest(path)}
                self.hashes[str(path)] = saved
            files[name] = saved["sha256"]
        if files["server_impl"] != self.config["expected_server_impl_sha256"]:
            raise RuntimeError("unvalidated_backend_library")
        proc = Path("/proc") / pid
        argv = proc.joinpath("cmdline").read_bytes()
        environment = proc.joinpath("environ").read_bytes().split(b"\0")
        selected = sorted(x.decode() for x in environment if x.startswith(
            (b"LLAMA_LAB_", b"CUDA_MODULE_LOADING=", b"LD_LIBRARY_PATH=")))
        if not {"LLAMA_LAB_KEEP_USER_CHECKPOINT=1", "LLAMA_LAB_DISK_CHECKPOINTS=1"}.issubset(selected):
            raise RuntimeError("backend_cache_features_disabled")
        maps = proc.joinpath("maps").read_text()
        if self.config["runtime_files"]["server_impl"] not in maps:
            raise RuntimeError("backend_loaded_library_mismatch")
        fingerprint = digest(encode({"version": 1, "files": files, "argv": digest(argv), "environment": selected}))
        if pid != self.pid or fingerprint != self.fingerprint:
            self.active = None
        self.pid, self.fingerprint = pid, fingerprint
        atomic_json(self.hash_path, self.hashes)
        return fingerprint

    def prefix(self, body):
        messages = body.get("messages")
        if body.get("cache_prompt") is False or not isinstance(messages, list):
            return None
        for message in messages:
            if not isinstance(message, dict):
                return None
            content = message.get("content")
            if content is not None and not isinstance(content, (str, list)):
                return None
            if isinstance(content, list) and any(not isinstance(x, dict) or x.get("type") != "text" or not isinstance(x.get("text"), str) for x in content):
                return None
        users = [m for m in messages if m.get("role") == "user"]
        if not users:
            return None
        content = users[-1].get("content")
        if content is not None and not isinstance(content, (str, list)):
            return None
        first = content if isinstance(content, str) else next(
            (x.get("text") for x in (content or []) if x.get("type") == "text" and x.get("text")), None)
        if not first:
            return None
        rendered = self.post("/apply-template", body)["prompt"]
        # Ambiguity must bypass caching instead of guessing a state boundary.
        if rendered.count(first) != 1:
            return None
        location = rendered.index(first)
        boundary = rendered.rfind("<|im_start|>user", 0, location)
        if boundary < 0:
            return None
        tokens = self.post("/tokenize", {"content": rendered[:boundary], "add_special": True, "parse_special": True})["tokens"]
        if len(tokens) < self.config.get("minimum_prefix_tokens", 4096):
            return None
        full = self.post("/tokenize", {"content": rendered, "add_special": True, "parse_special": True})["tokens"]
        if full[:len(tokens)] != tokens:
            return None
        return tokens

    def validate(self, path, key, tokens, runtime):
        manifest = json.loads(path.read_text())
        if manifest.get("version") != 1 or manifest.get("runtime") != runtime:
            raise ValueError("runtime_fingerprint_mismatch")
        if manifest.get("prefix_sha256") != key or manifest.get("prefix_tokens") != len(tokens):
            raise ValueError("prefix_mismatch")
        files = manifest.get("files", [])
        if len(files) != 3:
            raise ValueError("snapshot_files_missing")
        base = files[0]["name"]
        if [x["name"] for x in files] != [base, base + ".draft", base + ".checkpoints"]:
            raise ValueError("snapshot_companions_mismatch")
        for item in files:
            if not SNAPSHOT_NAME.fullmatch(item["name"]):
                raise ValueError("invalid_snapshot_filename")
            file = self.data / item["name"]
            if file.is_symlink() or not file.is_file() or file.stat().st_size != item["bytes"] or file_digest(file) != item["sha256"]:
                raise ValueError("snapshot_integrity_mismatch")
        return manifest

    def prune(self, keep):
        manifests = sorted(self.manifests.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        used = 0
        retained = 0
        for path in sorted(manifests, key=lambda p: p != keep):
            try:
                value = json.loads(path.read_text())
                size = sum(x["bytes"] for x in value["files"])
                if retained < self.config.get("max_snapshots", 3) and used + size <= self.config.get("max_disk_bytes", 2 * 1024**3):
                    used += size
                    retained += 1
                    continue
                path.unlink()
                for item in value["files"]:
                    if SNAPSHOT_NAME.fullmatch(item["name"]):
                        (self.data / item["name"]).unlink(missing_ok=True)
            except (ValueError, KeyError, OSError, TypeError, AttributeError):
                LOG.warning("cache_prune_skipped manifest=%s", path.name)
                path.unlink(missing_ok=True)
        referenced = set()
        for path in self.manifests.glob("*.json"):
            try:
                referenced.update(x["name"] for x in json.loads(path.read_text())["files"])
            except (ValueError, KeyError, OSError, TypeError, AttributeError):
                continue
        for path in self.data.iterdir():
            if SNAPSHOT_NAME.fullmatch(path.name) and path.name not in referenced:
                path.unlink(missing_ok=True)

    def prepare(self, body):
        started = time.monotonic()
        runtime = self.runtime()
        tokens = self.prefix(body)
        if tokens is None:
            self.active = None
            return {"event": "bypass", "seconds": time.monotonic() - started, "prime_tokens": 0}
        key = digest(encode(tokens))
        result = {"prefix_sha256": key, "fixed_tokens": len(tokens), "prime_tokens": 0}
        if self.active == (key, runtime):
            return {**result, "event": "hot", "seconds": time.monotonic() - started}
        self.active = None
        path = self.manifests / (key + ".json")
        if path.exists():
            try:
                manifest = self.validate(path, key, tokens, runtime)
                self.post("/slots/0?action=restore", {"filename": manifest["files"][0]["name"]})
                self.active = (key, runtime)
                os.utime(path, None)
                return {**result, "event": "disk", "seconds": time.monotonic() - started}
            except (ValueError, KeyError, OSError, TypeError, AttributeError, urllib.error.HTTPError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (400, 404):
                    raise
                LOG.warning("cache_miss prefix=%s reason=%s", key, str(error) if isinstance(error, ValueError) else type(error).__name__)
                # Remove only this manifest; old immutable data is pruned below.
                path.unlink(missing_ok=True)
        self.post("/slots/0?action=erase", {})
        primed = self.post("/completion", {"prompt": tokens, "n_predict": 0, "cache_prompt": True, "stream": False})
        result["prime_tokens"] = primed.get("timings", {}).get("prompt_n", len(tokens))
        name = "prefix-" + uuid.uuid4().hex + ".bin"
        try:
            saved = self.post("/slots/0?action=save", {"filename": name})
            files = []
            for suffix in ("", ".draft", ".checkpoints"):
                file = self.data / (name + suffix)
                file.chmod(0o600)
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
                files.append({"name": file.name, "bytes": file.stat().st_size, "sha256": file_digest(file)})
            if self.config.get("max_snapshots", 3) <= 0 or sum(item["bytes"] for item in files) > self.config.get("max_disk_bytes", 2 * 1024**3):
                raise ValueError("snapshot_exceeds_disk_budget")
            atomic_json(path, {"version": 1, "runtime": runtime, "prefix_sha256": key, "prefix_tokens": len(tokens), "files": files, "native_save": saved})
            self.prune(path)
            event = "miss_saved"
        except (OSError, urllib.error.HTTPError, ValueError, KeyError) as error:
            LOG.warning("cache_save_failed type=%s", type(error).__name__)
            for suffix in ("", ".draft", ".checkpoints"):
                (self.data / (name + suffix)).unlink(missing_ok=True)
            self.prune(None)
            event = "miss_memory_only"
        self.active = (key, runtime)
        return {**result, "event": event, "seconds": time.monotonic() - started}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def error_json(self, code, message):
        self.close_connection = True
        raw = encode({"error": {"message": message, "type": "prefix_gateway_error"}})
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        cache = self.server.cache
        if self.path != "/health" and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + cache.key):
            self.error_json(401, "unauthorized")
            return
        started = time.monotonic()
        raw = None
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding"):
                self.error_json(400, "unsupported_transfer_encoding")
                return
            if len(self.headers.get_all("Content-Length", [])) != 1:
                self.error_json(400, "invalid_content_length")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.close_connection = True
                self.error_json(400, "invalid_content_length")
                return
            if length < 0 or length > cache.config.get("max_request_bytes", 64 * 1024**2):
                self.close_connection = True
                self.error_json(413, "request_body_too_large")
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.error_json(400, "incomplete_request_body")
                return
        readonly = self.command == "GET" or self.path.split("?")[0] in {"/apply-template", "/tokenize", "/detokenize"}
        locked = False
        prepared = {"event": "bypass", "prime_tokens": 0}
        sent = False
        usage = None
        timing = None
        first = None
        try:
            if not readonly:
                locked = cache.lock.acquire(timeout=cache.config.get("queue_timeout", 1800))
                if not locked:
                    self.error_json(503, "cache_worker_busy")
                    return
                if self.path.split("?")[0] in {"/v1/chat/completions", "/chat/completions"}:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        self.error_json(400, "invalid_json")
                        return
                    if not isinstance(body, dict):
                        self.error_json(400, "request_body_must_be_object")
                        return
                    prepared = cache.prepare(body)
                    LOG.info("cache_prepare %s", encode(prepared).decode())
                else:
                    cache.active = None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host", "authorization"}}
            try:
                response = cache.open(self.path, raw, headers)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                self.send_response(response.status)
                for name, value in response.headers.items():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.send_header("X-Prefix-Cache", prepared["event"])
                self.end_headers()
                sent = True
                self.close_connection = True
                sse = "text/event-stream" in response.headers.get("Content-Type", "")
                while True:
                    block = response.readline() if sse else response.read(64 * 1024)
                    if not block:
                        break
                    if sse and block.startswith(b"data:") and block[5:].strip() != b"[DONE]":
                        try:
                            value = json.loads(block[5:])
                            if isinstance(value.get("usage"), dict):
                                usage = value["usage"] or usage
                            if isinstance(value.get("timings"), dict):
                                timing = value["timings"] or timing
                            if first is None and any(any((choice.get("delta") or {}).get(k) for k in ("content", "reasoning_content", "reasoning", "tool_calls")) for choice in value.get("choices", [])):
                                first = time.monotonic() - started
                        except (ValueError, AttributeError, TypeError):
                            pass
                    self.wfile.write(block)
                    self.wfile.flush()
                if response.status >= 400:
                    cache.active = None
        except (BrokenPipeError, ConnectionResetError):
            cache.active = None
            LOG.warning("client_disconnected")
        except Exception as error:
            cache.active = None
            LOG.error("request_failed type=%s", type(error).__name__)
            if not sent:
                self.error_json(502, "cache_backend_unavailable")
        finally:
            if locked:
                cache.lock.release()
            if not readonly:
                LOG.info("request_finished %s", encode({"request_sha256": digest(raw or b""), "cache": prepared, "ttft_seconds": first, "total_seconds": time.monotonic() - started, "usage": usage, "timings": timing, "total_prefill_tokens": prepared.get("prime_tokens", 0) + (timing or {}).get("prompt_n", 0)}).decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(Path(args.config).read_text())
    server = ThreadingHTTPServer((config.get("host", "0.0.0.0"), config.get("port", 8081)), Handler)
    server.daemon_threads = True
    server.cache = PrefixCache(config)
    server.serve_forever()


if __name__ == "__main__":
    main()
