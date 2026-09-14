from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from fastapi import HTTPException, Request
import anyio


def verify_assets(assets, binding, input_root):
    if not isinstance(assets, dict) or not 1 <= len(assets) <= 5:
        raise ValueError("invalid input asset binding")
    names = []
    evidence = {}
    for role, asset in assets.items():
        if role not in {"first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"}:
            raise ValueError("invalid asset role")
        if not isinstance(asset, dict) or set(asset) != {"asset_id", "sha256", "comfy_name", "size"}:
            raise ValueError("invalid immutable asset metadata")
        if not re.fullmatch(r"asset_[a-f0-9]{32}", asset["asset_id"]) or asset["comfy_name"].split(".")[0] != asset["asset_id"]:
            raise ValueError("input filename does not bind its asset id")
        if binding["input_roles"].get(role) != asset["comfy_name"] or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]):
            raise ValueError("input graph or asset hash mismatch")
        path = Path(input_root) / asset["comfy_name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != asset["size"]:
            raise ValueError("input asset is missing or changed")
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != asset["sha256"]:
            raise ValueError("input content hash mismatch")
        names.append(asset["comfy_name"])
        evidence[role] = dict(asset)
    if sorted(names) != sorted(binding["input_filenames"]):
        raise ValueError("graph does not consume all bound assets")
    return evidence


def asset_root(fleet):
    return fleet.store.path.parent / "input-assets"


def verified_memory(assets, root):
    if "reference_video" not in assets:
        return {}
    from .input_memory import verified_input_memory
    return verified_input_memory(assets, root)


def materialize(fleet, backend, contract):
    assets = verify_assets(contract["assets"], contract, asset_root(fleet))
    if not backend.get("input_root"):
        raise ValueError("registered backend input directory unavailable")
    declared = Path(backend["input_root"])
    root = declared.resolve(strict=True)
    if not root.is_dir() or not declared.is_absolute() or root != declared:
        raise ValueError("registered backend input directory unavailable")
    if backend.get("pid"):
        arguments = (Path("/proc") / str(backend["pid"]) / "cmdline").read_bytes().split(b"\0")
        positions = [index for index, value in enumerate(arguments) if value == b"--input-directory"]
        if len(positions) != 1 or positions[0] + 1 >= len(arguments) or Path(os.fsdecode(arguments[positions[0] + 1])) != root:
            raise ValueError("input directory does not match the pinned running command")
    for asset in assets.values():
        target = root / asset["comfy_name"]
        if target.is_symlink():
            raise ValueError("input target is a symlink")
        if not target.exists():
            descriptor, filename = tempfile.mkstemp(prefix=".h3-input-", dir=root)
            try:
                with os.fdopen(descriptor, "wb") as destination, (asset_root(fleet) / asset["comfy_name"]).open("rb") as source:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.link(filename, target)
            except FileExistsError:
                pass
            finally:
                Path(filename).unlink(missing_ok=True)
    verify_assets(assets, contract, root)


def install(fleet, app, authenticate):
    upload_lock = asyncio.Lock()

    @app.api_route("/api/router/input-assets/{filename}", methods=["GET", "PUT"])
    async def input_asset(filename: str, request: Request):
        authenticate(request)
        if not re.fullmatch(r"asset_[a-f0-9]{32}\.(?:png|jpe?g|webp|mp4|mov|mkv|webm|wav|mp3|m4a|aac|flac|ogg)", filename):
            raise HTTPException(400, "invalid immutable input filename")
        root = asset_root(fleet)
        target = root / filename
        if request.method == "GET":
            if target.is_symlink() or not target.is_file():
                raise HTTPException(404, "asset not found")
            def receipt():
                hasher = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        hasher.update(chunk)
                return {"filename": filename, "sha256": hasher.hexdigest(), "size": target.stat().st_size, "state": "ready"}
            return await asyncio.to_thread(receipt)
        checksum = request.query_params.get("sha256", "")
        size_text = request.query_params.get("size", "")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum) or not size_text.isdigit() or not 0 < int(size_text) <= 128 * 1024**2:
            raise HTTPException(413, "invalid asset size or digest")
        async with upload_lock:
            if target.exists() or target.is_symlink():
                raise HTTPException(409, "immutable input exists; query and compare its receipt")
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
            minimum = max(40, fleet.policy.data["resources"].get("min_offload_free_gib", 40)) * 1024**3
            if shutil.disk_usage(root).free - int(size_text) < minimum:
                raise HTTPException(507, "offload disk safety floor crossed")
            descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=root)
            try:
                with os.fdopen(descriptor, "wb") as destination:
                    hasher = hashlib.sha256()
                    received = 0
                    with anyio.fail_after(120):
                        async for chunk in request.stream():
                            received += len(chunk)
                            if received > int(size_text):
                                raise HTTPException(413, "asset exceeds declared size")
                            hasher.update(chunk)
                            destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                if received != int(size_text) or hasher.hexdigest() != checksum:
                    raise HTTPException(400, "uploaded asset digest mismatch")
                os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {"filename": filename, "sha256": checksum, "size": int(size_text), "state": "ready"}

    @app.post("/api/router/multimodal-workflow")
    async def prepare(request: Request):
        authenticate(request)
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"profile_id", "profile_version", "input_sha256", "assets", "prompt"}:
            raise HTTPException(400, "invalid multimodal workflow preparation")
        try:
            if body["profile_id"] not in fleet.recipes.catalog.entries:
                raise ValueError("multimodal profile is not registered")
            if not re.fullmatch(r"[a-f0-9]{64}", body["input_sha256"]):
                raise ValueError("input version digest required")
            binding = fleet.recipes.catalog.validate(body["profile_id"], body["prompt"], body["profile_version"])
            backends = [backend for backend in fleet.recipes.backends.values() if fleet.recipes.qualifications(body["profile_id"], backend)]
            if not backends:
                raise ValueError("multimodal runtime qualification unavailable")
            assets = await asyncio.to_thread(verify_assets, body["assets"], binding, asset_root(fleet))
            input_memory = await asyncio.to_thread(verified_memory, assets, asset_root(fleet))
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise HTTPException(409, "multimodal preparation rejected: " + str(error)) from error
        return {"binding": {**binding, "assets": assets, "input_sha256": body["input_sha256"],
                            "verified_input_memory": input_memory}, "enabled": fleet.recipes.enabled}
