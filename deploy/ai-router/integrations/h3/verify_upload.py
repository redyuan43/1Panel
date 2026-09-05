"""Verify installed upload cleaning on existing artifacts without provider calls."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import time
from uuid import uuid4


def credential_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            name = re.sub(r"[^a-z0-9]", "", key.lower())
            if (name in {"apikey", "secret", "apisecret", "clientsecret", "accesskey", "secretkey",
                         "accesskeyid", "accesskeysecret", "authorization", "password", "token",
                         "accesstoken", "refreshtoken"} or name.endswith(("apikey", "apisecret", "clientsecret"))):
                if item not in (None, "", False, [], {}):
                    return True
            if credential_fields(item):
                return True
    elif isinstance(value, list):
        return any(credential_fields(item) for item in value)
    elif isinstance(value, str):
        try:
            nested = json.loads(value)
        except (ValueError, RecursionError):
            return False
        if isinstance(nested, (dict, list)):
            return credential_fields(nested)
    return False


def tags(probe):
    return [probe.get("format", {}).get("tags", {}),
            *[item.get("tags", {}) for group in ("streams", "chapters") for item in probe.get(group, [])]]


def contains_literal(path, secret):
    if not secret:
        raise RuntimeError("A real credential is required for the literal scan.")
    needle, tail = secret.encode(), b""
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            data = tail + chunk
            if needle in data:
                return True
            tail = data[-(len(needle) - 1):] if len(needle) > 1 else b""
    return False


def verify(project_id, prior=None):
    root = Path("/home/admin/github/h3-video-studio")
    path = root / "app/router_contract.py"
    spec = importlib.util.spec_from_file_location("upload_privacy_check", path)
    extension = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extension)

    def project():
        with sqlite3.connect(f"file:{root / 'data/studio.sqlite3'}?mode=ro", uri=True) as db:
            return json.loads(db.execute("SELECT data_json FROM projects WHERE id=?", (project_id,)).fetchone()[0])
    before = project()
    if before["stages"]["regenerate_2k"]["status"] != "pending":
        raise RuntimeError("Expected the existing 2K stage to remain pending.")
    values = {}
    credential_file = Path.home() / ".config/minimax/credentials.env"
    for raw in credential_file.read_text().splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            name, value = raw.removeprefix("export ").split("=", 1)
            parsed = shlex.split(value, comments=True)
            if len(parsed) == 1:
                values[name.strip()] = parsed[0]
    key = values.get("MINIMAX_API_KEY", "")
    if not key:
        raise RuntimeError("The actual MiniMax credential is unavailable for literal verification.")
    if prior and (prior["project_id"] != project_id or prior["extension_sha256"]
                  != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise RuntimeError("The previous evidence does not match the installed project/extension.")
    records = []
    for stage_id in ("preview", "local_768"):
        stage = before["stages"][stage_id]
        if stage["status"] not in {"approved", "awaiting_approval"}:
            raise RuntimeError("The requested existing output is not terminal.")
        output = before["router_outputs"][stage["output_id"]]
        source = Path(output["path"])
        if output["run_id"] != stage["run_id"] or source != Path(stage["artifact"]):
            raise RuntimeError("The source does not identify the immutable current H3 stage.")
        raw_tags = tags(extension._media_probe(source))
        serialized = json.dumps(raw_tags)
        scan = {
            "actual_minimax_key_available_for_check": bool(key),
            "nonempty_credential_field_present": credential_fields(raw_tags),
            "actual_minimax_key_present": bool(key and key in serialized),
            "actual_minimax_key_literal_in_mp4": contains_literal(source, key),
            "workflow_or_model_metadata_present": bool(re.search(
                r"workflow|prompt|safetensors|comfy", serialized, flags=re.I)),
        }
        if prior:
            previous = next(item for item in prior["checks"] if item["stage"] == stage_id)
            if previous["run_id"] != stage["run_id"] or previous["output_id"] != stage["output_id"]:
                raise RuntimeError("The original upstream run/output version changed.")
            proof = previous["proof"]
            target = Path(proof["upload_path"])
            if (str(source) != proof["source_path"]
                    or extension._file_sha256(source) != proof["source_sha256"]
                    or extension._file_sha256(target) != proof["upload_sha256"]
                    or source.stat().st_size != proof["source_bytes"]
                    or target.stat().st_size != proof["upload_bytes"]):
                raise RuntimeError("The original or previously verified upload artifact changed.")
            raw_probe, clean_probe = extension._media_probe(source), extension._media_probe(target)
            extension.assert_clean_metadata(clean_probe)
            if (extension.av_packet_proof(raw_probe) != proof["packet_proof"]
                    or extension.av_packet_proof(clean_probe) != proof["packet_proof"]
                    or extension.decoded_frame_proof(source) != proof["decoded_frame_proof"]
                    or extension.decoded_frame_proof(target) != proof["decoded_frame_proof"]):
                raise RuntimeError("Previously verified packet/frame evidence no longer matches.")
        else:
            target = source.parent.parent / "router-upload-private" / (
                "acceptance_" + stage_id + "_" + uuid4().hex + ".mp4")
            proof = extension.sanitize_upload(source, target)
        clean_tags = tags(extension._media_probe(target))
        clean_serialized = json.dumps(clean_tags)
        clean_scan = {
            "nonempty_credential_field_present": credential_fields(clean_tags),
            "actual_minimax_key_present": bool(key and key in clean_serialized),
            "actual_minimax_key_literal_in_mp4": contains_literal(target, key),
            "workflow_or_model_metadata_present": bool(re.search(
                r"workflow|prompt|safetensors|comfy", clean_serialized, flags=re.I)),
        }
        if any(clean_scan.values()):
            raise RuntimeError("Clean upload metadata failed the negative privacy check.")
        records.append({"stage": stage_id, "run_id": stage["run_id"], "output_id": stage["output_id"],
                        "credential_metadata_scan": scan, "upload_metadata_scan": clean_scan, "proof": proof,
                        "packet_hashes_equal": True, "decoded_frame_hashes_equal": True,
                        "original_unchanged": True, "prior_artifacts_rechecked": bool(prior)})
    if before != project():
        raise RuntimeError("H3 project state changed during the non-generating upload check.")
    return {"status": "passed", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "project_id": project_id, "extension_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "checks": records, "project_unchanged": True, "regenerate_2k_status": "pending",
            "provider_calls": 0, "generation_started": False, "approval_sent": False,
            "read_only_recheck": bool(prior)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--existing-report", type=Path, help="Recheck original copies without writing any Edge artifact")
    args = parser.parse_args()
    payload = {"project_id": args.project,
               "prior": json.loads(args.existing_report.read_text()) if args.existing_report else None}
    code = ("ns={'__name__':'upload_check'}; exec(" + repr(Path(__file__).read_text()) + ",ns); "
            "print(json.dumps(ns['verify'](**json.load(sys.stdin))))")
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "edge",
                             "/home/admin/github/h3-video-studio/.venv/bin/python -c " + shlex.quote(
                                 "import json,sys; " + code)],
                            input=json.dumps(payload), capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError("Installed upload check failed: " + result.stderr[-2000:])
    report = json.loads(result.stdout)
    from deploy_edge import private_write
    private_write(args.report, json.dumps(report, indent=2))
    print(json.dumps({"status": report["status"], "project_id": report["project_id"], "report": str(args.report),
                      "provider_calls": 0, "checks": [
                          {key: item[key] for key in ("stage", "run_id", "output_id",
                                                    "credential_metadata_scan", "upload_metadata_scan")}
                          for item in report["checks"]]}))


if __name__ == "__main__":
    main()
