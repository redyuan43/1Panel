from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import shutil
import threading
import time

from download_comparison_models import apply_mirrors, cached_manifest, download_file, exclusive_lock, sha256


def destination_relative(record):
    if record["group"] == "a_lightx2v":
        return "loras/" + record["filename"]
    if record["group"] == "b_vdn":
        return record["filename"]
    if record["group"] == "d_fasth3":
        return "diffusion_models/" + record["filename"]
    raise ValueError("unsupported staging group")


def remaining_bytes(root, records):
    required = 0
    for record in records:
        target = root / destination_relative(record)
        incoming = root / "incoming" / record["group"] / record["filename"]
        partial = incoming.with_name(incoming.name + ".part")
        present = [path for path in (target, incoming, partial) if path.exists()]
        if len(present) > 1:
            raise ValueError("multiple artifacts for one staging destination")
        owned = present[0].stat().st_size if present else 0
        if owned > record["bytes"]:
            raise ValueError("owned artifact exceeds pinned size")
        required += record["bytes"] - owned
    return required


def run(args):
    records = apply_mirrors(cached_manifest(args.manifest), args.mirrors)
    records = [record for record in records if "mirror_url" in record]
    if len(records) != 6:
        raise ValueError("expected the six pinned mirrored weights")
    required = remaining_bytes(args.root, records)
    if shutil.disk_usage(args.root).free < required + 40 * 1024**3:
        raise RuntimeError("staging cannot preserve 40GiB reserve")
    state = {"started_at": time.time(), "status": "planned", "files": records}
    lock = threading.Lock()

    def save():
        temporary = args.root / "staging-report.json.next"
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(args.root / "staging-report.json")

    save()
    if not args.execute:
        return
    state["status"] = "downloading"

    def fetch(record):
        relative = destination_relative(record)
        destination = args.root / "incoming" / record["group"]
        target = args.root / relative
        if target.exists():
            if target.stat().st_size != record["bytes"] or sha256(target) != record["expected_sha256"]:
                raise ValueError("existing staged artifact mismatch")
        else:
            def progress(amount):
                with lock:
                    record.update(status="downloading", downloaded_bytes=amount, updated_at=time.time())
                    save()

            downloaded = download_file(record, destination, progress)
            if sha256(downloaded) != record["expected_sha256"]:
                raise ValueError("staging SHA256 mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            downloaded.rename(target)
        with lock:
            record.update(status="verified", path=str(target), finished_at=time.time())
            save()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch, record): record for record in records}
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as error:
                with lock:
                    futures[future].update(status="failed", error=str(error))
                    save()
    state["status"] = "verified" if all(record.get("status") == "verified" for record in records) else "failed"
    state["finished_at"] = time.time()
    save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--mirrors", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.root.is_dir():
        raise ValueError("staging root must already exist")
    with exclusive_lock(args.root / "staging.lock"):
        run(args)


if __name__ == "__main__":
    main()
