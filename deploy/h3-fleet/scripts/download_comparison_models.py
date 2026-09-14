from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import quote


SPECS = (
    ("a_lightx2v", "lightx2v/Minimax-h3-Turbo", "2f015e66b37c585cea9dc4ae6f1850ea8788e742", (
        "minimax_h3_fl2v_turbo_4step_v1.2_768p_comfyui_bf16.safetensors",
        "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
    )),
    ("b_vdn", "t8star/Vdn-Minimax-H3-Comfy", "d6aa5dfc669a749dc9364a36f3b6d4d1545a53e8", (
        "diffusion_models/OpenVDN/vdn-minimax-h3/stage-dmd-step-250/linear_branch/model.safetensors",
        "diffusion_models/OpenVDN/vdn-minimax-h3/stage-dmd-step-250/adapters/default/adapter_model.safetensors",
        "diffusion_models/OpenVDN/vdn-minimax-h3/stage-dmd-step-250/adapters/turbo/adapter_model.safetensors",
    )),
    ("c_realism", "fal/MiniMax-H3-Realism-People-LoRA", "b6e6b9f683c80c38684dbb1adcd566fc69a711fb", (
        "h3-realism-people-t2v-i2v-r2v.safetensors",
    )),
    ("d_fasth3", "Kijai/MiniMax-H3-experimental", "f4cac997f880e93cf6940af61ee8d58ef31ff7f3", (
        "minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors",
    )),
)

MIRROR_SPECS = {
    "a_lightx2v": ("lightx2v/Minimax-h3-Turbo", "c6ff59d4f054c70bee73cfbb2ee43505e18699f1"),
    "b_vdn": ("OpenVDN/vdn-minimax-h3", "3674e3a5183da3782bdd7e57b83adb8b99a759ba"),
    "d_fasth3": ("Kijai/MiniMax-H3-experimental", "cf077746e92340c65cf13b54f6b4066b23a0b74b"),
}


def mirror_identity(record):
    group = record.get("group")
    if group not in MIRROR_SPECS:
        raise ValueError("group is not approved for direct ModelScope access")
    spec = next(spec for spec in SPECS if spec[0] == group)
    if record.get("repo") != spec[1] or record.get("revision") != spec[2] or record.get("filename") not in spec[3]:
        raise ValueError("mirror original identity differs from pinned plan")
    repo, revision = MIRROR_SPECS[group]
    filename = record["filename"]
    if group == "b_vdn":
        filename = filename.removeprefix("diffusion_models/OpenVDN/vdn-minimax-h3/")
    url = f"https://modelscope.cn/api/v1/models/{repo}/repo?Revision={revision}&FilePath={quote(filename, safe='')}"
    return repo, revision, filename, url


def apply_mirrors(records, path):
    audit = json.loads(path.read_text())
    if audit.get("schema") != "h3.modelscope-mirror-audit.v1" or not isinstance(audit.get("records"), list):
        raise ValueError("invalid mirror audit schema")
    indexed = {(record["repo"], record["revision"], record["filename"]): record for record in records}
    if len(indexed) != len(records):
        raise ValueError("duplicate download destination")
    urls = {}
    for entry in audit["records"]:
        original = entry["planned_huggingface"]
        key = (original["repo"], original["revision"], original["filename"])
        record = indexed.get(key)
        if record is None or key in urls:
            raise ValueError("unknown or duplicate mirror record")
        if entry.get("group") != record["group"] or entry.get("status") != "sha256_and_size_match" or entry.get("recommend_alternate_source") is not True:
            raise ValueError("mirror is not approved by audit")
        if type(original.get("size_bytes")) is not int or original["size_bytes"] != record["bytes"] or original.get("sha256") != record["expected_sha256"]:
            raise ValueError("mirror original size or SHA256 differs from plan")
        if not isinstance(record["expected_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["expected_sha256"]):
            raise ValueError("mirror requires full pinned SHA256")
        repo, revision, filename, url = mirror_identity(record)
        mirror = entry["modelscope"]
        if any(mirror.get(field) != expected for field, expected in (
            ("repo", repo), ("revision", revision), ("filename", filename), ("download_url", url),
            ("size_bytes", record["bytes"]), ("sha256", record["expected_sha256"]),
        )) or type(mirror.get("size_bytes")) is not int:
            raise ValueError("mirror URL, identity, size or SHA256 differs from pinned audit contract")
        urls[key] = url
    return [{**record, **({"mirror_url": urls[key]} if key in urls else {})}
            for record in records for key in [(record["repo"], record["revision"], record["filename"])]]


@contextmanager
def exclusive_lock(path):
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"download already locked: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def validate_response(response, offset, size):
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ValueError("encoded download body is not resumable")
    if response.status == 206:
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
        if not match or tuple(map(int, match.groups())) != (offset, size - 1, size):
            raise ValueError("server did not honor exact pinned Content-Range")
    elif response.status != 200 or offset or response.headers.get("Content-Range"):
        raise ValueError("server did not honor exact resume offset")
    content_length = response.headers.get("Content-Length")
    if content_length is not None and (not content_length.isdecimal() or int(content_length) != size - offset):
        raise ValueError("Content-Length differs from pinned remaining size")


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("unsafe repository filename")
    return name


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def selected_files(group, entries, required):
    names = set(required)
    for entry in entries:
        name = safe_name(entry["rfilename"])
        if name in {"README.md", "LICENSE", "LICENSE.txt", "NOTICE", "MODEL_MANIFEST.json"}:
            names.add(name)
        if group == "b_vdn" and "/stage-dmd-step-250/" in name and name.endswith(".json"):
            names.add(name)
    indexed = {entry["rfilename"]: entry for entry in entries}
    if not set(required).issubset(indexed):
        raise ValueError("pinned repository is missing required weights")
    result = []
    for name in sorted(names):
        entry = indexed[name]
        size = entry.get("size")
        if not isinstance(size, int) or size < 0:
            raise ValueError("unknown file size")
        if name not in required and size > 2 * 1024**2:
            raise ValueError("unexpectedly large metadata file")
        expected = entry.get("lfs", {}).get("sha256")
        if name in required and (not expected or len(expected) != 64):
            raise ValueError("required weight has no upstream SHA256")
        result.append({"filename": safe_name(name), "bytes": size, "expected_sha256": expected})
    return result


def manifest():
    result = []
    for group, repo, revision, required in SPECS:
        url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
        with urllib.request.urlopen(url, timeout=60) as response:
            metadata = json.load(response)
        if metadata.get("sha") != revision:
            raise ValueError("upstream revision mismatch")
        for record in selected_files(group, metadata["siblings"], required):
            result.append({"group": group, "repo": repo, "revision": revision, **record})
    return result


def cached_manifest(path):
    cached = json.loads(path.read_text())["files"]
    destinations = [(record["group"], record["filename"]) for record in cached]
    if len(destinations) != len(set(destinations)):
        raise ValueError("duplicate cached download destination")
    result = []
    for group, repo, revision, required in SPECS:
        records = [record for record in cached if record["group"] == group]
        if any(record["repo"] != repo or record["revision"] != revision for record in records):
            raise ValueError("cached manifest revision mismatch")
        entries = [{"rfilename": record["filename"], "size": record["bytes"],
                    "lfs": {"sha256": record.get("expected_sha256")}} for record in records]
        for record in selected_files(group, entries, required):
            result.append({"group": group, "repo": repo, "revision": revision, **record})
    return result


def download_file(record, destination, progress):
    target = destination / safe_name(record["filename"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(target.with_name(target.name + ".part.lock")):
        return download_file_locked(record, target, progress)


def download_file_locked(record, target, progress):
    if target.is_file() and target.stat().st_size == record["bytes"]:
        if not record["expected_sha256"] or sha256(target) == record["expected_sha256"]:
            return target
        raise ValueError("existing completed weight hash mismatch")
    partial = target.with_name(target.name + ".part")
    url = (f"https://huggingface.co/{record['repo']}/resolve/{record['revision']}/"
           + quote(record["filename"], safe="/") + "?download=true")
    open_url = urllib.request.urlopen
    if "mirror_url" in record:
        expected_url = mirror_identity(record)[3]
        if record["mirror_url"] != expected_url:
            raise ValueError("unapproved direct mirror URL")
        url = expected_url
        open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    for attempt in range(2):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > record["bytes"]:
            raise ValueError("partial download exceeds expected size")
        if offset == record["bytes"]:
            break
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-{record['bytes'] - 1}"
        try:
            with open_url(urllib.request.Request(url, headers=headers), timeout=60) as response:
                validate_response(response, offset, record["bytes"])
                with partial.open("ab" if offset else "wb") as handle:
                    while chunk := response.read(1024**2):
                        if offset + len(chunk) > record["bytes"]:
                            raise ValueError("download exceeds pinned file size")
                        handle.write(chunk)
                        handle.flush()
                        offset += len(chunk)
                        progress(offset)
            if offset != record["bytes"]:
                raise OSError("download ended before pinned file size")
            break
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if attempt:
                raise
            time.sleep(2)
    if partial.stat().st_size != record["bytes"]:
        raise ValueError("partial download size mismatch")
    if record["expected_sha256"] and sha256(partial) != record["expected_sha256"]:
        raise ValueError("partial download hash mismatch")
    partial.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--mirror-manifest", type=Path,
                        help="strictly matched ModelScope audit; only approved mirrors bypass proxies")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(args.root / ".download.lock"):
        run(args)


def run(args):
    try:
        records = cached_manifest(args.manifest_file) if args.manifest_file else manifest()
        if args.mirror_manifest:
            records = apply_mirrors(records, args.mirror_manifest)
        destinations = [(record["group"], record["filename"]) for record in records]
        if len(destinations) != len(set(destinations)):
            raise ValueError("duplicate download destination")
    except Exception as error:
        (args.root / "preflight-error.json").write_text(json.dumps({
            "status": "failed", "at": time.time(), "error_type": type(error).__name__,
            "phase": "pinned_metadata_lookup"}))
        raise
    priority = {"c_realism": 0, "a_lightx2v": 1, "b_vdn": 2, "d_fasth3": 3}
    records.sort(key=lambda record: (record["filename"].endswith(".safetensors"), priority[record["group"]], record["bytes"]))
    total = sum(record["bytes"] for record in records)
    if shutil.disk_usage(args.root).free < total * 2 + 50 * 1024**3:
        raise RuntimeError("download disk cannot preserve temporary-file and free-space budget")
    state = {"started_at": time.time(), "status": "preflight", "total_bytes": total, "files": records}
    lock = threading.Lock()

    def save():
        temporary = args.root / "download-report.json.next"
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(args.root / "download-report.json")

    save()
    if not args.execute:
        print(json.dumps(state))
        return
    def fetch(record):
        destination = args.root / "models" / record["group"]
        with lock:
            record.update(status="downloading", started_at=time.time())
            save()
        try:
            def progress(downloaded):
                with lock:
                    record.update(downloaded_bytes=downloaded, last_progress_at=time.time())
                    save()

            path = download_file(record, destination, progress)
            digest = sha256(path)
            if path.stat().st_size != record["bytes"]:
                raise ValueError("download size mismatch")
            if record["expected_sha256"] and digest != record["expected_sha256"]:
                raise ValueError("download hash mismatch")
            with lock:
                record.update(status="verified", path=str(path), sha256=digest, finished_at=time.time())
                save()
        except Exception as error:
            with lock:
                record.update(status="failed", error=type(error).__name__ + ": " + str(error)[:500],
                              finished_at=time.time())
                save()
            raise

    state["status"] = "downloading"
    save()
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch, record) for record in records]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as error:
                errors.append(type(error).__name__)
    state.update(status="failed" if errors else "verified", finished_at=time.time())
    save()
    print(json.dumps({key: value for key, value in state.items() if key != "files"}))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
