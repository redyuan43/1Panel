from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
from threading import Lock
from uuid import uuid4

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool


KINDS = {
    "first_frame": {".png", ".jpg", ".jpeg", ".webp"},
    "last_frame": {".png", ".jpg", ".jpeg", ".webp"},
    "reference_image": {".png", ".jpg", ".jpeg", ".webp"},
    "reference_video": {".mp4", ".mov", ".mkv", ".webm"},
    "reference_audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"},
}
MAX_BYTES = 128 * 1024 * 1024
OWNER_QUOTA = 1024 * 1024 * 1024
UPLOAD_LEASE_SECONDS = 600
MEDIA_FORMATS = "mov,matroska,webm,wav,mp3,flac,ogg,aac"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
REFERENCE_PROBE_LOCK = Lock()


def record(cursor):
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row is not None else None


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_reference_video(path):
    source = Path(__file__).with_name("reference_video.py")
    if not source.is_file():
        source = Path(__file__).resolve().parents[1] / "shared/reference_video.py"
    spec = importlib.util.spec_from_file_location("h3_uploaded_video_probe", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not REFERENCE_PROBE_LOCK.acquire(timeout=10):
        raise ValueError("reference video inspector busy")
    try:
        return module.inspect_video(path)
    finally:
        REFERENCE_PROBE_LOCK.release()


def probe(path, kind):
    if kind in {"first_frame", "last_frame", "reference_image"}:
        try:
            with Image.open(path) as picture:
                if picture.format not in {"PNG", "JPEG", "WEBP"} or getattr(picture, "n_frames", 1) != 1:
                    raise ValueError()
                width, height = picture.size
                if min(width, height) < 16 or max(width, height) > 8192 or width * height > 32_000_000:
                    raise ValueError()
                mime = Image.MIME[picture.format]
                picture.verify()
            with Image.open(path) as picture:
                picture.load()
            return {"mime": mime, "width": width, "height": height}
        except Exception as error:
            raise HTTPException(400, "invalid image or image decode limit exceeded") from error
    try:
        if kind == "reference_video":
            metadata = inspect_reference_video(path)
            return {**metadata, "mime": {".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime",
                                          ".mkv": "video/x-matroska"}[path.suffix],
                    "duration": metadata["video_duration_seconds"],
                    "model_input_representation": "original_cfr24" if metadata["is_cfr_24"] else "requires_explicit_24fps_input"}
        result = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", MEDIA_FORMATS, "-show_format", "-show_streams",
            "-of", "json", str(path),
        ], capture_output=True, timeout=30, check=True)
        metadata = json.loads(result.stdout)
        streams = metadata.get("streams", [])
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not 0 < duration <= 15.5 or len(streams) > 8:
            raise ValueError()
        videos = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if kind == "reference_video":
            if len(videos) != 1 or max(videos[0].get("width", 0), videos[0].get("height", 0)) > 4096:
                raise ValueError()
            width, height = videos[0]["width"], videos[0]["height"]
            if width * height > 4096 * 2160:
                raise ValueError()
        elif not audios or videos:
            raise ValueError()
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                        "-format_whitelist", MEDIA_FORMATS, "-threads", "1", "-i", str(path), "-t", "15.5", "-f", "null", "-"],
                       capture_output=True, timeout=90, check=True)
        return {"mime": {".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}[path.suffix] if videos else "audio/" +
                {".wav": "wav", ".mp3": "mpeg", ".m4a": "mp4", ".aac": "aac", ".flac": "flac", ".ogg": "ogg"}[path.suffix],
                "duration": duration, "width": videos[0]["width"] if videos else None,
                "height": videos[0]["height"] if videos else None, "has_audio": bool(audios)}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise HTTPException(400, "invalid media or media processing limit exceeded") from error


class AssetStore:
    def __init__(self, contract):
        self.contract = contract
        self.root = (contract.m.SETTINGS.data_root / "connector-assets").absolute()

    def initialize(self):
        with self.contract.connect() as database:
            database.execute("""CREATE TABLE IF NOT EXISTS connector_assets (
                asset_id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation_id TEXT NOT NULL,
                request_json TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL,
                metadata_json TEXT, UNIQUE(owner, operation_id))""")

    def metadata(self, value):
        if not isinstance(value, dict) or set(value) != {"operation_id", "kind", "filename", "size", "sha256"}:
            raise HTTPException(400, "invalid upload metadata")
        if not isinstance(value["operation_id"], str) or not IDENTIFIER.fullmatch(value["operation_id"]):
            raise HTTPException(400, "invalid upload operation")
        kind, filename = value["kind"], value["filename"]
        if kind not in KINDS or not isinstance(filename, str) or not 1 <= len(filename) <= 200:
            raise HTTPException(400, "invalid asset kind or filename")
        if any(character in filename for character in "\\/\x00\r\n") or Path(filename).suffix.lower() not in KINDS[kind]:
            raise HTTPException(400, "unsupported asset filename")
        if type(value["size"]) is not int or not 0 < value["size"] <= MAX_BYTES:
            raise HTTPException(413, "asset size exceeds limit")
        if not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
            raise HTTPException(400, "invalid asset digest")
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def reserve(self, owner, value):
        encoded = self.metadata(value)
        self.initialize()
        self.recover_expired()
        with self.contract.connect() as database:
            previous = record(database.execute("SELECT * FROM connector_assets WHERE owner=? AND operation_id=?", (owner, value["operation_id"])))
            if previous:
                if previous["request_json"] != encoded:
                    raise HTTPException(409, "upload operation conflict")
                if previous["state"] == "ready":
                    return previous["asset_id"], self.public(previous)
                raise HTTPException(409, "upload not ready; query original operation before retry")
            rows = database.execute("SELECT request_json FROM connector_assets WHERE owner=? AND state IN ('uploading','ready')", (owner,)).fetchall()
            if sum(json.loads(row[0])["size"] for row in rows) + value["size"] > OWNER_QUOTA or len(rows) >= 100:
                raise HTTPException(413, "account asset quota exceeded")
            identifier = "asset_" + uuid4().hex
            database.execute("INSERT INTO connector_assets VALUES (?,?,?,?,?,?,?)",
                             (identifier, owner, value["operation_id"], encoded, "uploading", time.time(), None))
        return identifier, None

    def paths(self, identifier, request):
        if not re.fullmatch(r"asset_[a-f0-9]{32}", identifier):
            raise HTTPException(404, "asset not found")
        directory = self.root / identifier
        if directory.is_symlink() or directory.resolve() != directory:
            raise HTTPException(404, "unsafe asset location")
        return directory, directory / ("original" + Path(request["filename"]).suffix.lower())

    def finish(self, identifier, request, temporary):
        if temporary.stat().st_size != request["size"] or file_digest(temporary) != request["sha256"]:
            raise HTTPException(400, "asset content does not match upload declaration")
        metadata = probe(temporary, request["kind"])
        _, target = self.paths(identifier, request)
        with self.contract.connect() as database:
            row = record(database.execute("SELECT state FROM connector_assets WHERE asset_id=?", (identifier,)))
            if not row or row["state"] != "uploading":
                raise HTTPException(409, "upload state changed")
            os.replace(temporary, target)
            updated = database.execute("UPDATE connector_assets SET state='ready',metadata_json=?,updated_at=? WHERE asset_id=? AND state='uploading'",
                                       (json.dumps(metadata), time.time(), identifier))
            if updated.rowcount != 1:
                raise HTTPException(409, "upload state changed")

    def fail(self, identifier):
        with self.contract.connect() as database:
            database.execute("UPDATE connector_assets SET state='failed',updated_at=? WHERE asset_id=? AND state='uploading'", (time.time(), identifier))

    def recover_expired(self):
        with self.contract.connect() as database:
            cursor = database.execute("SELECT * FROM connector_assets WHERE state='uploading' AND updated_at < ?", (time.time() - UPLOAD_LEASE_SECONDS,))
            columns = [column[0] for column in cursor.description]
            expired = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in expired:
                request = json.loads(row["request_json"])
                directory, target = self.paths(row["asset_id"], request)
                temporary = directory / ("upload" + target.suffix)
                metadata = {"recovery_reason": "upload_lease_expired", "recovered_at": time.time()}
                database.execute("UPDATE connector_assets SET state='failed',metadata_json=?,updated_at=? WHERE asset_id=? AND state='uploading'",
                                 (json.dumps(metadata), time.time(), row["asset_id"]))
                for path in (temporary, target):
                    path.unlink(missing_ok=True)
        return len(expired)

    def lookup(self, owner, *, asset_id=None, operation_id=None):
        self.initialize()
        field, value = ("asset_id", asset_id) if asset_id else ("operation_id", operation_id)
        with self.contract.connect() as database:
            row = record(database.execute("SELECT * FROM connector_assets WHERE owner=? AND " + field + "=?", (owner, value)))
            if not row:
                raise HTTPException(404, "asset not found")
            return row

    def public(self, row):
        request = json.loads(row["request_json"])
        return {"asset_id": row["asset_id"], "operation_id": row["operation_id"], "state": row["state"],
                **{key: request[key] for key in ("kind", "filename", "size", "sha256")},
                **json.loads(row["metadata_json"] or "{}")}

    def bind(self, owner, mapping):
        if not isinstance(mapping, dict) or set(mapping) - KINDS.keys():
            raise HTTPException(400, "invalid asset roles")
        assets = {}
        for kind, identifier in mapping.items():
            row = self.lookup(owner, asset_id=identifier)
            request = json.loads(row["request_json"])
            if row["state"] != "ready" or request["kind"] != kind:
                raise HTTPException(409, "asset not ready or role mismatch")
            _, path = self.paths(identifier, request)
            if path.is_symlink() or not path.is_file() or file_digest(path) != request["sha256"]:
                raise HTTPException(409, "asset integrity check failed")
            assets[kind] = {"asset_id": identifier, "name": request["filename"], "path": str(path), "ir_path": str(path),
                            "comfy_name": identifier + path.suffix, "sha256": request["sha256"], "size": request["size"],
                            "mime": json.loads(row["metadata_json"])["mime"], "metadata": json.loads(row["metadata_json"])}
        return assets


def install_routes(api, store, authenticate, can_write):
    @api.post("/assets/uploads")
    async def upload(request: Request):
        owner = authenticate(request)
        if not can_write():
            raise HTTPException(403, "connector uploads paused")
        try:
            metadata = json.loads(request.headers.get("x-h3-upload-metadata", ""))
        except (ValueError, UnicodeError):
            raise HTTPException(400, "missing upload metadata")
        identifier, receipt = await run_in_threadpool(store.reserve, owner, metadata)
        if receipt:
            return receipt
        directory, target = store.paths(identifier, metadata)
        temporary = directory / ("upload" + target.suffix)
        completed = False
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            with anyio.fail_after(120):
                with temporary.open("xb") as destination:
                    size = 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > metadata["size"]:
                            raise HTTPException(413, "upload exceeds declared size")
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
            await run_in_threadpool(store.finish, identifier, metadata, temporary)
            completed = True
            return store.public(store.lookup(owner, asset_id=identifier))
        finally:
            if not completed:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                store.fail(identifier)

    @api.get("/assets/uploads/{operation_id}")
    def lookup_upload(operation_id: str, request: Request):
        owner = authenticate(request)
        store.initialize()
        store.recover_expired()
        return store.public(store.lookup(owner, operation_id=operation_id))

    @api.api_route("/assets/{asset_id}/content", methods=["GET", "HEAD"])
    def content(asset_id: str, request: Request):
        row = store.lookup(authenticate(request), asset_id=asset_id)
        value = store.public(row)
        assets = store.bind(row["owner"], {value["kind"]: asset_id})
        return FileResponse(assets[value["kind"]]["path"], media_type=value["mime"],
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
