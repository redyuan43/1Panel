from __future__ import annotations

import ctypes
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request


FILENAME = "minimax_h3_ref2va_int8_convrot.safetensors"
SHA256 = "9eef934046a0671bc8a5daf87100705e1478419c574cfde70c50fbe6885f76a9"
SIZE = 34038894550
URL = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/" + FILENAME
ROOT = Path("/media/ivan/55FF-1534/h3-models/diffusion_models")


def main(stream=None):
    if not ROOT.is_dir():
        raise ValueError("registered_model_directory_missing")
    target = ROOT / FILENAME
    temporary = ROOT / (".h3-ref2va-" + SHA256 + ".partial")
    receipt_path = ROOT / (".h3-ref2va-" + SHA256 + ".receipt.json")
    previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    recovered = bool(stream is not None and previous and previous.get("status") == "failed_preserved_for_reconciliation"
                     and previous.get("received_bytes") == 0 and previous.get("expected_sha256") == SHA256 and not temporary.exists())
    if target.exists() or temporary.exists() or receipt_path.exists() and not recovered:
        raise ValueError("existing_download_requires_reconciliation_not_overwrite")
    if shutil.disk_usage(ROOT).free - SIZE < 40 * 1024**3:
        raise ValueError("disk_safety_floor_would_be_crossed")
    receipt = {"filename": FILENAME, "source_url": URL, "expected_sha256": SHA256, "expected_size": SIZE,
               "started_at": time.time(), "status": "downloading", "gpu_submissions": 0, "model_qualified": False}
    receipt["source_route"] = "ai_existing_proxy_stream" if stream is not None else "direct"
    if recovered:
        receipt["prior_attempt"] = previous
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    checksum = hashlib.sha256()
    count = 0
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with (opener.open(URL, timeout=60) if stream is None else contextlib.nullcontext(stream)) as response, temporary.open("xb") as destination:
            declared = response.headers.get("content-length") if stream is None else None
            if declared is not None and int(declared) != SIZE:
                raise ValueError("download_size_changed")
            while chunk := response.read(4 * 1024**2):
                count += len(chunk)
                if count > SIZE or shutil.disk_usage(ROOT).free < 40 * 1024**3:
                    raise ValueError("download_or_disk_safety_limit")
                destination.write(chunk)
                checksum.update(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if count != SIZE or checksum.hexdigest() != SHA256:
            raise ValueError("download_integrity_failure")
        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(target), 1) != 0:
            raise OSError(ctypes.get_errno(), "exclusive_publication_failed")
        receipt.update(status="downloaded_and_verified", size=count, sha256=checksum.hexdigest(), finished_at=time.time())
    except Exception as error:
        receipt.update(status="failed_preserved_for_reconciliation", received_bytes=count, error_type=type(error).__name__, finished_at=time.time())
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
