from __future__ import annotations

import hashlib
import copy
import json
from collections import OrderedDict
from pathlib import Path
from threading import Lock


GIB = 1024**3
PROBE_CACHE = OrderedDict()
PROBE_LOCK = Lock()


def inspect_reference(path):
    from .reference_video import decoded_memory_budget, inspect_video
    metadata = inspect_video(path)
    return metadata, decoded_memory_budget(metadata)


def inspector_version():
    from . import reference_video
    return hashlib.sha256(Path(reference_video.__file__).read_bytes()).hexdigest()


def file_identity(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("reference_video_file_identity_unavailable")
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def verified_input_memory(assets, root):
    video = assets.get("reference_video")
    if video is None:
        return {}
    root = Path(root)
    path = root / video["comfy_name"]
    identity = file_identity(path)
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    if identity[2] != video["size"] or hasher.hexdigest() != video["sha256"] or file_identity(path) != identity:
        raise ValueError("reference_video_content_changed")
    version = inspector_version()
    key = (str(path), video["asset_id"], video["sha256"], tuple(identity), version)
    if not PROBE_LOCK.acquire(timeout=10):
        raise ValueError("reference_video_inspector_busy")
    try:
        if key in PROBE_CACHE:
            evidence = copy.deepcopy(PROBE_CACHE[key])
            PROBE_CACHE.move_to_end(key)
        else:
            metadata, budget = inspect_reference(path)
            if file_identity(path) != identity:
                raise ValueError("reference_video_changed_during_inspection")
            if type(budget) is not int or budget < 2 * GIB:
                raise ValueError("reference_video_decode_budget_unavailable")
            evidence = {"asset_id": video["asset_id"], "sha256": video["sha256"], "size": video["size"],
                        "inspector_version": version, "metadata": metadata, "decode_budget_bytes": budget}
            PROBE_CACHE[key] = copy.deepcopy(evidence)
            if len(PROBE_CACHE) > 64:
                PROBE_CACHE.popitem(last=False)
        if file_identity(path) != identity:
            raise ValueError("reference_video_changed_during_inspection")
    finally:
        PROBE_LOCK.release()
    if any(evidence.get(key) != video[key] for key in ("asset_id", "sha256", "size")):
        raise ValueError("reference_video_evidence_asset_mismatch")
    if evidence["metadata"].get("is_cfr_24") is not True:
        raise ValueError("reference_video_requires_explicit_24fps_analysis_copy")
    return {"reference_video": evidence}


def input_increment(contract, required=False):
    asset = contract.get("assets", {}).get("reference_video")
    evidence = contract.get("verified_input_memory", {}).get("reference_video")
    if asset is None and not required:
        if evidence is not None:
            raise ValueError("unexpected_reference_video_memory_evidence")
        return 0
    if not asset or not evidence or any(evidence.get(key) != asset.get(key) for key in ("asset_id", "sha256", "size")):
        raise ValueError("reference_video_memory_evidence_required")
    budget = evidence.get("decode_budget_bytes")
    if type(budget) is not int or budget < 2 * GIB or evidence.get("metadata", {}).get("is_cfr_24") is not True:
        raise ValueError("reference_video_memory_evidence_invalid")
    return budget


def job_contract(job):
    if job is None:
        return {}
    return json.loads(job["request_json"])["extra_data"]["h3"]["contract"]


def apply_input_budget(budget, contract, model_base_bytes, *, required=False):
    extra = input_increment(contract, required=required)
    total = max(budget["candidate_budget_bytes"], model_base_bytes + extra)
    return {**budget, "candidate_budget_bytes": total, "budget_bytes": total,
            "model_base_budget_bytes": model_base_bytes, "input_decode_budget_bytes": extra,
            "verified_input_memory": contract.get("verified_input_memory", {}),
            "input_budget_basis": "bounded_decode_allocation_estimate_not_measured_peak" if extra else "no_reference_video"}


def demand_budget(contract, base_bytes):
    return base_bytes + input_increment(contract)
