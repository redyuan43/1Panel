"""Verify real delivered video privacy and packet fidelity against private sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import httpx
from dotenv import dotenv_values


ROOT = Path.home() / ".local/state/ai-router-acceptance/20260904-media-deploy"
JOB = "vid_f76b81b2cdc142ed8c9d8353ea236d31"


def probe(path, *args):
    return json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
    ))


def packets(path):
    values = probe(path, "-show_packets", "-show_data_hash", "sha256",
                   "-show_entries", "packet=stream_index,data_hash")["packets"]
    return {index: [row["data_hash"] for row in values if row["stream_index"] == index]
            for index in {row["stream_index"] for row in values}}


def main():
    os.umask(0o077)
    env = dotenv_values(Path.home() / ".config/ai-router-media/acceptance.env")
    directory = ROOT / "video-privacy"
    directory.mkdir(mode=0o700, exist_ok=True)
    report = {"job_id": JOB, "real_http": True, "outputs": []}
    with sqlite3.connect("file:/opt/1panel/ai-router/media/media.sqlite3?mode=ro", uri=True) as db:
        private = {json.loads(row[0])["id"]: json.loads(row[0])
                   for row in db.execute("SELECT value FROM artifacts WHERE job_id=?", (JOB,))}
    headers = {"Authorization": "Bearer " + env["MEDIA_CLIENT_KEY"]}
    with httpx.Client(base_url="http://127.0.0.1:4000", headers=headers, trust_env=False, timeout=30) as client:
        response = client.get("/v1/videos/" + JOB)
        assert response.status_code == 200, response.status_code
        for stage in response.json()["stages"]:
            output = stage.get("output")
            if not output or output["content_type"] != "video/mp4":
                continue
            raw = private[output["id"]]
            response = client.get(output["content_url"])
            assert response.status_code == 200, response.status_code
            path = directory / (stage["id"] + ".mp4")
            path.write_bytes(response.content)
            sha = hashlib.sha256(response.content).hexdigest()
            source = Path(raw["source_path"])
            assert hashlib.sha256(source.read_bytes()).hexdigest() == raw["source_sha256"]
            assert sha == output["sha256"] == raw["sha256"]
            assert output["id"] != output["output_id"] == stage["output_id"]
            assert not any(key.startswith("source_") for key in output)
            original_packets, delivered_packets = packets(source), packets(path)
            assert original_packets == delivered_packets
            info = probe(path, "-show_streams", "-show_format")
            tags = [info["format"].get("tags", {}), *(item.get("tags", {}) for item in info["streams"])]
            forbidden = {"prompt", "workflow", "comment", "description", "api_key", "password"}
            assert not any(forbidden.intersection(section) for section in tags)
            found = [marker.decode() for marker in (b"/home/admin", b"api_key", b"workflow", b"minimax")
                     if marker in response.content.lower()]
            assert not found, found
            old = client.get(f"/v1/media/outputs/{output['output_id']}/content")
            assert old.status_code in {404, 410}, old.status_code
            report["outputs"].append({
                "stage": stage["id"], "version": output["output_id"], "artifact_id": output["id"],
                "delivery_sha256": sha, "source_sha256": raw["source_sha256"],
                "source_retained": True, "source_fields_exposed": False,
                "packets_equal": True, "packet_counts": {key: len(value) for key, value in delivered_packets.items()},
                "public_tags": tags, "private_markers_found": found, "old_raw_http_status": old.status_code,
                "request_id": response.headers.get("x-request-id"),
            })
    report["passed"] = len(report["outputs"]) >= 2
    (directory / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
