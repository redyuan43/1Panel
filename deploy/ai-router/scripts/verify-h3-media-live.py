#!/usr/bin/env python3
"""Serial, resumable H3 acceptance through the real Router, never a fixture."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx


BASE = "http://127.0.0.1:4000"
DEFAULT_REPORT = Path.home() / ".local/state/ai-router-acceptance/20260904-media-deploy/video-live"
PROMPT = ("A single red cube slowly rotates on a clean white tabletop. "
          "Fixed camera, one continuous shot, clear natural lighting. "
          "A soft click is heard as the cube turns. No text or cuts.")
TERMINAL_ERRORS = {"failed", "cancelled"}
ACTIVE = {"queued", "running", "cancelling"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# This helper only reads H3 SQLite, ComfyUI queue/history, process state and GPU
# telemetry. Generation, approval and artifact downloads always use Router HTTP.
EDGE_PROBE = r'''
import hashlib,json,pathlib,sqlite3,subprocess,sys,urllib.request
p=json.load(sys.stdin)
root=pathlib.Path("/home/admin/github/h3-video-studio")
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    with opener.open("http://127.0.0.1:8188"+path,timeout=10) as response:
        return json.load(response)
def cmd(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=15)
    return {"returncode":result.returncode,"stdout":result.stdout,"stderr":result.stderr}
with sqlite3.connect("file:"+str(root/"data/studio.sqlite3")+"?mode=ro",uri=True) as db:
    projects=[json.loads(row[0]) for row in db.execute("SELECT data_json FROM projects")]
    schedules=[json.loads(row[0]) for row in db.execute("SELECT data_json FROM batch_schedules")]
    project=next((v for v in projects if v["id"]==p.get("project_id")),None)
    receipts=[]
    if project:
        for row in db.execute("SELECT operation_id,result_json FROM router_operations WHERE project_id=?",(project["id"],)):
            value=json.loads(row[1])
            receipts.append({"operation_id":row[0],"pipeline":value.get("pipeline")})
result={"host":cmd(["hostname"])["stdout"].strip(),
        "active_projects":[{"id":v["id"],"stage":k,"status":s["status"],"run_id":s.get("run_id")}
            for v in projects for k,s in v["stages"].items() if s["status"] in {"queued","running","cancelling"}],
        "active_batches":[{"id":v["id"],"status":v["status"]} for v in schedules
            if v["status"] in {"running","waiting_for_gpu","pausing","cancelling"}],
        "h3_service":cmd(["systemctl","--user","show","h3-video-studio.service","-p","MainPID","-p","ActiveState","-p","SubState"]),
        "comfy_service":cmd(["systemctl","--user","show","comfyui-edge.service","-p","MainPID","-p","ActiveState","-p","SubState"]),
        "gpu":cmd(["nvidia-smi","--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total","--format=csv,noheader"]),
        "gpu_processes":cmd(["nvidia-smi","--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory","--format=csv,noheader"]),
        "extension_sha256":hashlib.sha256((root/"app/router_contract.py").read_bytes()).hexdigest(),
        "receipts":receipts}
try:
    queue=get("/queue")
    result["queue"]={k:[str(v[1]) for v in queue.get(k,[]) if len(v)>1]
                     for k in ("queue_running","queue_pending")}
except Exception as error:
    result["queue_error"]=str(error)
if project:
    result["project"]={k:project.get(k) for k in ("id","mode","strategy","duration","actual_duration","router_managed")}
    result["project"]["stages"]={}
    for name,stage in project["stages"].items():
        result["project"]["stages"][name]={k:stage.get(k) for k in
            ("status","run_id","output_id","prompt_id","remote_task_id","task_id","workflow_template",
             "started_at","finished_at","error","usage","source_spec","cancel_requested","router_upload_source")}
    result["outputs"]={}
    for key,value in project.get("router_outputs",{}).items():
        item={k:value.get(k) for k in ("id","stage","run_id","content_type")}
        if "text" in value:
            data=value["text"].encode()
            item.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
        elif value.get("path"):
            path=pathlib.Path(value["path"])
            item["path"]=str(path)
            if path.is_file():
                h=hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b""):h.update(chunk)
                item.update(sha256=h.hexdigest(),bytes=path.stat().st_size)
        result["outputs"][key]=item
    current=project["stages"].get(p.get("stage"),{})
    prompt_id=current.get("prompt_id")
    if prompt_id:
        try:
            history=get("/history/"+prompt_id).get(prompt_id)
            if history:
                graph=history.get("prompt",[])
                graph=graph[2] if isinstance(graph,list) and len(graph)>2 and isinstance(graph[2],dict) else {}
                result["comfy_history"]={"prompt_id":prompt_id,"status":history.get("status"),
                    "outputs":history.get("outputs"),
                    "graph":[{"class_type":v.get("class_type"),"inputs":{k:val for k,val in v.get("inputs",{}).items()
                        if k in {"unet_name","ckpt_name","model_name","width","height","length","steps","noise_seed"}}}
                        for v in graph.values() if isinstance(v,dict)]}
        except Exception as error:
            result["history_error"]=str(error)
print(json.dumps(result))
'''

EDGE_UPLOAD_PROBE = r'''
import importlib.util,json,pathlib,sqlite3,sys
payload=json.load(sys.stdin)
root=pathlib.Path("/home/admin/github/h3-video-studio")
spec=importlib.util.spec_from_file_location("upload_check",root/"app/router_contract.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
with sqlite3.connect("file:"+str(root/"data/studio.sqlite3")+"?mode=ro",uri=True) as db:
    project=json.loads(db.execute("SELECT data_json FROM projects WHERE id=?",(payload["project_id"],)).fetchone()[0])
stage=project["stages"]["regenerate_2k"]
proof=stage["router_upload_source"]
previous=project["stages"][proof["source_stage"]]
source=pathlib.Path(proof["source_path"])
upload=pathlib.Path(proof["upload_path"])
directory=root/"data/projects"/project["id"]/"router-upload-private"
assert upload.parent.resolve()==directory.resolve() and upload.name.startswith("upload_")
assert stage["run_id"]==payload["run_id"]==proof["generation_run_id"]
assert previous["status"]=="approved"
assert previous["run_id"]==proof["source_run_id"] and previous["output_id"]==proof["source_output_id"]
assert str(source)==project["router_outputs"][previous["output_id"]]["path"]
assert m._file_sha256(source)==proof["source_sha256"]==payload["source_sha256"]
assert source.stat().st_size==proof["source_bytes"]
assert m._file_sha256(upload)==proof["upload_sha256"] and upload.stat().st_size==proof["upload_bytes"]
raw,clean=m._media_probe(source),m._media_probe(upload)
m.assert_clean_metadata(clean)
assert m.av_packet_proof(raw)==m.av_packet_proof(clean)==proof["packet_proof"]
assert m.decoded_frame_proof(source)==m.decoded_frame_proof(upload)==proof["decoded_frame_proof"]
print(json.dumps({"status":"passed","project_id":project["id"],"run_id":stage["run_id"],
    "provider_task_id":stage.get("task_id") or stage.get("remote_task_id"),
    "actual_hook_copy_not_acceptance_copy":True,"source_approved_version_matches":True,
    "source_original_unchanged":True,"metadata_clean":True,"packets_and_timing_equal":True,
    "decoded_frames_equal":True,"proof":proof}))
'''


class WaitState(Exception):
    """The job remains live or a deliberate human handoff is outstanding."""


class AcceptanceError(Exception):
    pass


def now():
    return dt.datetime.now().astimezone().isoformat()


def scrub(value):
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()
                if key.lower() not in {"authorization", "api_key", "access_token", "content_url", "token"}}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", value)
        value = re.sub(r"(https?://[^?\s\"']+)\?[^ \n\"']+", r"\1?[redacted]", value)
        value = re.sub(r"(?i)([?&](?:access|token|api_key)=)[^&\s\"']+", r"\1[redacted]", value)
        return value
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(scrub(value), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def env_value(path, wanted):
    if path.is_symlink() or (path.stat().st_mode & 0o777) != 0o600:
        raise AcceptanceError(f"Expected a private 0600 environment file: {path}")
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        name, value = raw.removeprefix("export ").split("=", 1)
        if name.strip() == wanted:
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise AcceptanceError(f"Invalid {wanted} environment entry")
            return parts[0]
    raise AcceptanceError(f"{wanted} is absent from {path}")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_evidence(root, job_id):
    with sqlite3.connect(f"file:{root / 'media.sqlite3'}?mode=ro", uri=True, timeout=30) as db:
        row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise AcceptanceError("Router job has no authoritative media database record")
        job = json.loads(row[0])
        result = {key: job.get(key) for key in ("id", "owner", "status", "request_id", "provider_state",
                                                "provider_errors", "sync_error", "error")}
        result["stages"] = [{key: stage.get(key) for key in ("id", "status", "run_id", "output_id")}
                            for stage in job.get("stages", [])]
        result["operations"] = [dict(idempotency_key=row[0], **json.loads(row[1]))
                                for row in db.execute("SELECT idem,value FROM operations WHERE job_id=?", (job_id,))]
        result["artifacts"] = [json.loads(row[0]) for row in db.execute(
            "SELECT value FROM artifacts WHERE job_id=?", (job_id,))]
    return result


def stage_by_id(job, stage_id):
    stage = next((item for item in job.get("stages", []) if item["id"] == stage_id), None)
    if stage is None:
        raise AcceptanceError(f"Advertised pipeline does not include {stage_id}")
    return stage


def assert_public_boundary(value):
    if isinstance(value, dict):
        if any(key.startswith("source_") for key in value):
            raise AcceptanceError("Public JSON exposed private source fields")
        for item in value.values():
            assert_public_boundary(item)
    elif isinstance(value, list):
        for item in value:
            assert_public_boundary(item)


def media_tools():
    path = Path(__file__).resolve().parents[1] / "integrations/h3/router_contract.py"
    spec = importlib.util.spec_from_file_location("h3_upload_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_identity(output, artifacts, original):
    assert_public_boundary(output)
    upstream_id, delivery_id = output["output_id"], output["id"]
    if not SAFE_ID.fullmatch(upstream_id) or not SAFE_ID.fullmatch(delivery_id):
        raise AcceptanceError("Unsafe output identity")
    local = next((item for item in artifacts if item["id"] == delivery_id), None)
    if not local or local.get("output_id") != upstream_id:
        raise AcceptanceError("Private archive does not map delivery ID to the upstream version")
    is_video = output.get("content_type") == "video/mp4"
    if is_video and (delivery_id == upstream_id or not output.get("metadata_stripped")
                     or not local.get("metadata_stripped")):
        raise AcceptanceError("Video delivery is not a distinct metadata-clean artifact")
    source = Path(local["source_path"] if is_video else local["path"])
    source_hash = local.get("source_sha256") if is_video else local["sha256"]
    source_bytes = local.get("source_bytes") if is_video else local["bytes"]
    if (source_hash != original.get("sha256") or source_bytes != original.get("bytes")
            or not source.is_file() or sha256(source) != source_hash or source.stat().st_size != source_bytes):
        raise AcceptanceError("H3 raw artifact and private source hash/length differ")
    delivery = Path(local["path"])
    if (local["sha256"] != output.get("sha256") or local["bytes"] != output.get("bytes")
            or not delivery.is_file() or sha256(delivery) != output["sha256"]
            or delivery.stat().st_size != output["bytes"]):
        raise AcceptanceError("AI clean archive differs from public delivery hash/length")
    return local, source


def rejected_raw_status(artifacts, output_id):
    legacy = next((item for item in artifacts if item["id"] == output_id), None)
    return 410 if legacy and not legacy.get("metadata_stripped") else 404


class Runner:
    def __init__(self, args):
        self.args = args
        self.root = args.report_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = (self.root / "runner.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path = self.root / "manifest.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 1, "run_label": args.run_label, "started_at": now(),
            "router_base": BASE, "jobs": {}, "coverage": {}, "events": 0,
        }
        if self.state["run_label"] != args.run_label or self.state["router_base"] != BASE:
            raise AcceptanceError("The persisted run identity must not change on resume")
        self.key = env_value(args.env_file, "MEDIA_CLIENT_KEY")
        self.other_key = env_value(args.env_file, "MEDIA_OTHER_KEY")
        if self.other_key == self.key:
            raise AcceptanceError("Ownership testing requires a distinct real client key")
        self.http = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + self.key},
                                 timeout=httpx.Timeout(45, connect=5), trust_env=False, follow_redirects=False)
        self.deadline = time.monotonic() + args.max_wait
        self.last_probe = 0.0
        self.persist()

    def close(self):
        self.http.close()
        self.lock.close()

    def persist(self):
        save(self.path, self.state)

    def event(self, kind, **value):
        self.state["events"] += 1
        record = {"at": now(), "type": kind, **value}
        save(self.root / "events" / f"{self.state['events']:06d}-{kind}.json", record)
        self.persist()
        if kind in {"artifact_verified", "adapter_resume_verified", "pipeline_completed"}:
            fields = ("stage", "output_id", "run_id", "router_job_id", "job_id", "video_id",
                      "operation_id", "prompt_id", "sha256", "bytes", "strategy")
            print(json.dumps({"at": record["at"], "event": kind,
                              **{key: value[key] for key in fields if key in value}}), flush=True)
        return record

    def api(self, method, path, *, expected=(200,), idem=None, body=None, fields=None):
        headers = {"Idempotency-Key": idem} if idem else {}
        kwargs = {"json": body} if body is not None else {}
        if fields is not None:
            kwargs["files"] = {name: (None, str(value)) for name, value in fields.items()}
        response = self.http.request(method, path, headers=headers, **kwargs)
        try:
            value = response.json()
        except ValueError:
            value = {"unparseable_response": response.text[:2000]}
        assert_public_boundary(value)
        self.event("http", method=method, path=path, idempotency_key=idem, body=body, fields=fields,
                   status=response.status_code, request_id=response.headers.get("x-request-id"), response=value)
        if response.status_code not in expected:
            raise AcceptanceError(f"{method} {path}: HTTP {response.status_code}: {scrub(value)}")
        return value

    def edge(self, job_id=None, stage=None):
        media = media_evidence(self.args.media_root, job_id) if job_id else None
        project_id = (media or {}).get("provider_state", {}).get("project_id")
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                                 "-o", "ConnectTimeout=10", "edge", "python3 -c " + shlex.quote(EDGE_PROBE)],
                                input=json.dumps({"project_id": project_id, "stage": stage}),
                                text=True, capture_output=True, timeout=100)
        if result.returncode:
            raise AcceptanceError(f"Read-only Edge evidence failed: {scrub(result.stderr[-2000:])}")
        backend = json.loads(result.stdout)
        if backend["host"] != "edge" or backend["gpu"]["returncode"] != 0:
            raise AcceptanceError("Real Edge GPU identity could not be verified")
        if project_id and (not backend.get("project") or not backend["project"].get("router_managed")):
            raise AcceptanceError("The Router job does not identify a real managed H3 project")
        event = self.event("backend", job_id=job_id, stage=stage, media=media, edge=backend)
        if job_id and stage and backend.get("project"):
            current = backend["project"]["stages"].get(stage, {})
            prompt_id = current.get("prompt_id")
            saved = next(item for item in self.state["jobs"].values() if item.get("id") == job_id)
            handle = {key: current.get(key) for key in
                      ("run_id", "prompt_id", "task_id", "remote_task_id", "status", "started_at", "finished_at")}
            handle.update(video_id=job_id, project_id=project_id, stage=stage, observed_at=event["at"],
                          operations=[{key: value.get(key) for key in ("id", "action", "status", "idempotency_key")}
                                      for value in media["operations"] if value.get("stage") == stage])
            saved.setdefault("live_handles", {})[stage] = handle
            save(self.root / "live-handles" / f"{job_id}-{stage}.json", handle)
            service = dict(line.split("=", 1) for line in backend["comfy_service"]["stdout"].splitlines() if "=" in line)
            processes = list(csv.reader(io.StringIO(backend["gpu_processes"]["stdout"])))
            gpu_pids = {row[1].strip() for row in processes if len(row) > 1}
            if (prompt_id and prompt_id in backend.get("queue", {}).get("queue_running", [])
                    and service.get("MainPID") in gpu_pids):
                saved = next(item for item in self.state["jobs"].values() if item.get("id") == job_id)
                saved.setdefault("live_gpu_evidence", {}).setdefault(current["run_id"], []).append({
                    "at": event["at"], "stage": stage, "run_id": current["run_id"],
                    "prompt_id": prompt_id, "comfy_pid": service["MainPID"],
                    "event_sequence": self.state["events"], "gpu_csv": backend["gpu"]["stdout"],
                })
                self.persist()
        self.last_probe = time.monotonic()
        return event

    def ownership(self, job_id, stage_id, output_id):
        headers = {"Authorization": "Bearer " + self.other_key}
        options = self.http.get("/v1/media/options", headers=headers)
        if options.status_code != 200:
            raise AcceptanceError("The independent ownership-test client is not valid")
        routes = [f"/v1/videos/{job_id}", f"/v1/videos/{job_id}/outputs",
                  f"/v1/videos/{job_id}/stages/{stage_id}",
                  f"/v1/videos/{job_id}/stages/{stage_id}/content",
                  f"/v1/media/outputs/{output_id}/content"]
        for path in routes:
            methods = ("GET", "HEAD") if path.endswith("/content") else ("GET",)
            for method in methods:
                response = self.http.request(method, path, headers=headers)
                self.event("ownership", method=method, path=path, status=response.status_code,
                           request_id=response.headers.get("x-request-id"), expected=404)
                if response.status_code != 404:
                    raise AcceptanceError(f"Other valid client could access {path}: HTTP {response.status_code}")
        # A deliberately nonexistent version cannot approve the task even if a
        # future ownership regression occurs in the mutation endpoint.
        route = f"/v1/videos/{job_id}/stages/{stage_id}/approve"
        response = self.http.post(route, headers={**headers, "Idempotency-Key": "ownership-" + output_id},
                                  json={"output_id": "out_not_a_real_version"})
        self.event("ownership", method="POST", path=route, status=response.status_code,
                   request_id=response.headers.get("x-request-id"), expected=404)
        if response.status_code != 404:
            raise AcceptanceError("Stage mutation did not enforce cross-client ownership")
        return {"independent_client_authenticated": True, "read_checks": len(routes),
                "mutation_status": response.status_code}

    def idle(self, job_id=None):
        evidence = self.edge(job_id)
        edge = evidence["edge"]
        own = (edge.get("project") or {}).get("id")
        foreign = [item for item in edge["active_projects"] if item["id"] != own]
        if foreign or edge["active_batches"]:
            raise WaitState(f"Edge work must finish first: {foreign or edge['active_batches']}")
        if edge.get("queue_error"):
            raise WaitState("ComfyUI queue could not be observed; no stage will be started")
        if any(edge.get("queue", {}).values()):
            raise WaitState("ComfyUI already has queued/running work; no concurrent GPU start")
        if own and any(item["id"] == own for item in edge["active_projects"]):
            raise WaitState("The existing H3 stage is still active; resume its observation")
        return evidence

    def options(self):
        options = self.api("GET", "/v1/media/options")
        self.state["options"] = options
        self.persist()
        return options

    def require_ready(self, strategy):
        options = self.options()
        video = options.get("videos", {})
        if not options.get("enabled") or not video.get("available"):
            raise WaitState("Real Router video execution has not been enabled")
        if "siyuan-video" not in options.get("models", []) or strategy not in video.get("strategy", []):
            raise AcceptanceError("The client does not advertise this video strategy")
        if "t2v" not in video.get("mode", []) or video.get("duration", {}).get("min") != 4:
            raise AcceptanceError("The advertised shortest t2v contract changed")

    def create(self, strategy):
        self.require_ready(strategy)
        saved = self.state["jobs"].get(strategy)
        if saved and saved.get("id"):
            return self.api("GET", "/v1/videos/" + saved["id"])
        self.idle()
        idem = f"{self.args.run_label}-{strategy}-create-v1"
        fields = {"model": "siyuan-video", "name": f"Router live {strategy} 4s acceptance",
                  "mode": "t2v", "strategy": strategy, "prompt": PROMPT,
                  "duration": 4, "seed": 20260904, "audio_policy": "native"}
        if not saved:
            saved = {"creation_idempotency_key": idem, "fields": fields, "intents": {}, "artifacts": {}}
            self.state["jobs"][strategy] = saved
            self.persist()
        elif saved["creation_idempotency_key"] != idem or saved["fields"] != fields:
            raise AcceptanceError("Uncertain creation must be retried with its original request")
        job = self.api("POST", "/v1/videos", expected=(202,), idem=idem, fields=fields)
        saved.update(id=job["id"], create_response=job)
        self.persist()
        print(json.dumps({"status": "accepted", "video_id": job["id"], "idempotency_key": idem,
                          "manifest": str(self.path)}), flush=True)
        replay = self.api("POST", "/v1/videos", expected=(202,), idem=idem, fields=fields)
        if replay["id"] != job["id"]:
            raise AcceptanceError("Repeated creation generated a second Router task")
        return job

    def wait(self, strategy, stage_id):
        saved = self.state["jobs"][strategy]
        while True:
            job = self.api("GET", "/v1/videos/" + saved["id"])
            stages = job.get("stages", [])
            if stages:
                saved["advertised_pipeline"] = [item["id"] for item in stages]
                saved["last_observation"] = {
                    "at": now(), "job_status": job["status"],
                    "stages": [{key: item.get(key) for key in ("id", "status", "progress", "output_id")}
                               for item in stages],
                }
                self.persist()
                stage = stage_by_id(job, stage_id)
                if stage["status"] in TERMINAL_ERRORS or job["status"] in TERMINAL_ERRORS:
                    evidence = self.edge(job["id"], stage_id)
                    target = self.root / strategy / (stage_id + "-first-fatal.json")
                    if not target.exists():
                        save(target, {"at": now(), "video_id": job["id"], "stage": stage_id,
                                      "provider_state": evidence["edge"].get("project"),
                                      "operations": evidence["media"]["operations"]})
                    raise AcceptanceError(f"{job['id']} {stage_id} is {stage['status']}")
                if time.monotonic() - self.last_probe >= self.args.probe_interval:
                    self.edge(job["id"], stage_id)
                if stage["status"] in {"awaiting_approval", "approved"} and stage.get("output"):
                    self.archive(strategy, job, stage)
                    return job
            if time.monotonic() >= self.deadline:
                self.edge(job["id"], stage_id)
                raise WaitState(f"Observation window ended; keep existing job {job['id']} and resume")
            time.sleep(self.args.poll_interval)

    def archive(self, strategy, job, stage):
        output = stage["output"]
        output_id = output["output_id"]
        delivery_id = output["id"]
        saved = self.state["jobs"][strategy]
        evidence = self.edge(job["id"], stage["id"])
        remote = evidence["edge"]["project"]["stages"][stage["id"]]
        original = evidence["edge"].get("outputs", {}).get(output_id, {})
        if remote.get("output_id") != output_id or not remote.get("run_id"):
            raise AcceptanceError("H3 run/output identity does not match the Router stage")
        local, source = archive_identity(output, evidence["media"]["artifacts"], original)
        delivery_hash = output["sha256"]
        extension = ".txt" if stage["id"] == "context_ir" else ".mp4"
        path = self.root / strategy / (stage["id"] + "-" + delivery_id + extension)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        route = f"/v1/videos/{job['id']}/stages/{stage['id']}/content"
        if not path.is_file() or sha256(path) != delivery_hash:
            temporary = path.with_suffix(extension + ".part")
            size = 0
            with self.http.stream("GET", route) as response, temporary.open("wb") as handle:
                if response.status_code != 200:
                    raise AcceptanceError(f"Router artifact download returned HTTP {response.status_code}")
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 1024**3:
                        raise AcceptanceError("Downloaded video exceeds 1 GiB")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
                self.event("download", job_id=job["id"], stage=stage["id"], route=route,
                           output_id=output_id, delivery_id=delivery_id, bytes=size,
                           request_id=response.headers.get("x-request-id"))
            if temporary.stat().st_size != output["bytes"] or sha256(temporary) != delivery_hash:
                raise AcceptanceError("Router download failed length/checksum verification")
            os.replace(temporary, path)
        with path.open("rb") as handle:
            prefix = handle.read(32)
        delivery_route = f"/v1/media/outputs/{delivery_id}/content"
        http_checks = []
        for target in (route, delivery_route):
            head = self.http.head(target)
            ranged = self.http.get(target, headers={"Range": "bytes=0-31"})
            if head.status_code != 200 or int(head.headers.get("content-length", 0)) != output["bytes"]:
                raise AcceptanceError("Router HEAD did not match the archived output")
            if (ranged.status_code != 206 or ranged.content != prefix
                    or ranged.headers.get("content-range") != f"bytes 0-31/{output['bytes']}"):
                raise AcceptanceError("Router Range did not match the real output")
            http_checks.append({"route": target, "head_status": head.status_code,
                                "range_status": ranged.status_code, "content_range": ranged.headers.get("content-range"),
                                "head_request_id": head.headers.get("x-request-id"),
                                "range_request_id": ranged.headers.get("x-request-id")})
        direct = self.http.get(delivery_route)
        if direct.status_code != 200 or hashlib.sha256(direct.content).hexdigest() != delivery_hash:
            raise AcceptanceError("Delivery-ID download differs from stage download")
        record = {"stage": stage["id"], "output_id": output_id, "delivery_id": delivery_id, "run_id": remote["run_id"],
                  "h3_project_id": evidence["edge"]["project"]["id"], "router_job_id": job["id"],
                  "sha256": delivery_hash, "bytes": output["bytes"], "download_path": str(path),
                  "source_sha256": original["sha256"], "source_bytes": original["bytes"],
                  "ai_archive_path": local["path"], "http_checks": http_checks, "backend": remote}
        record["ownership"] = self.ownership(job["id"], stage["id"], delivery_id)
        if extension == ".txt":
            text = path.read_text()
            if not text.strip() or text != output.get("text"):
                raise AcceptanceError("Context IR text differs from the Router response")
            if not remote.get("remote_task_id"):
                raise AcceptanceError("Context IR lacks a real provider task ID")
        else:
            tools = media_tools()
            raw_probe, clean_probe = tools._media_probe(source), tools._media_probe(path)
            tools.assert_clean_metadata(clean_probe)
            raw_packets, clean_packets = tools.av_packet_proof(raw_probe), tools.av_packet_proof(clean_probe)
            raw_frames, clean_frames = tools.decoded_frame_proof(source), tools.decoded_frame_proof(path)
            if raw_packets != clean_packets or raw_frames != clean_frames:
                raise AcceptanceError("Clean delivery changed audio/video packets, timing or decoded frames")
            old_route = f"/v1/media/outputs/{output_id}/content"
            old_raw = {}
            expected_raw_status = rejected_raw_status(evidence["media"]["artifacts"], output_id)
            for method in ("GET", "HEAD"):
                response = self.http.request(method, old_route)
                old_raw[method] = response.status_code
                if response.status_code != expected_raw_status:
                    raise AcceptanceError(f"Raw ID rejection expected HTTP {expected_raw_status}, got {response.status_code}")
            versions = self.api("GET", f"/v1/videos/{job['id']}/outputs")["data"]
            if (any(item["id"] == output_id for item in versions)
                    or not any(item["id"] == delivery_id and item["output_id"] == output_id for item in versions)):
                raise AcceptanceError("Public history did not hide raw IDs and retain clean version mapping")
            record["privacy"] = {"raw_matches_private_source": True, "clean_matches_served_bytes": True,
                                 "metadata_clean": True, "public_source_fields_absent": True,
                                 "old_raw_status": old_raw, "raw_history_hidden": True,
                                 "packet_proof": clean_packets, "decoded_frame_proof": clean_frames,
                                 "audio_video_packets_equal": True, "decoded_frames_equal": True,
                                 "transcoded": False}
            record["validation"] = self.validate_video(path, job, stage["id"])
            if stage["id"] in {"preview", "proof", "local_768"}:
                history = evidence["edge"].get("comfy_history", {})
                if not remote.get("prompt_id") or not history.get("status", {}).get("completed"):
                    raise AcceptanceError("Local video lacks completed real ComfyUI history")
                samples = saved.get("live_gpu_evidence", {}).get(remote["run_id"], [])
                if not samples:
                    raise AcceptanceError("Local video has no correlated live ComfyUI queue/GPU-process observation")
                record["comfy_history"] = history
                record["live_gpu_evidence"] = samples
            elif not (remote.get("task_id") or remote.get("remote_task_id")):
                raise AcceptanceError("Cloud video lacks a real provider task ID")
            if stage["id"] == "regenerate_2k":
                record["cloud_upload"] = self.verify_cloud_upload(evidence, remote)
        saved["artifacts"][delivery_id] = record
        self.state["coverage"].setdefault(stage["id"], [])
        if strategy not in self.state["coverage"][stage["id"]]:
            self.state["coverage"][stage["id"]].append(strategy)
        self.event("artifact_verified", **record)
        self.persist()
        return record

    def verify_cloud_upload(self, evidence, stage):
        proof = stage.get("router_upload_source")
        if not proof or not proof.get("metadata_clean") or proof.get("transcoded") is not False:
            raise AcceptanceError("Actual cloud stage lacks a verified clean upload receipt")
        original = evidence["edge"]["outputs"].get(proof["source_output_id"], {})
        local = next((item for item in evidence["media"]["artifacts"]
                      if item.get("output_id") == proof["source_output_id"] and item.get("metadata_stripped")), {})
        if (proof["source_sha256"] != original.get("sha256")
                or proof["source_sha256"] != local.get("source_sha256")
                or proof["generation_run_id"] != stage["run_id"]):
            raise AcceptanceError("Actual cloud source differs from the approved Router private source")
        result = subprocess.run([
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "edge",
            "/home/admin/github/h3-video-studio/.venv/bin/python -c " + shlex.quote(EDGE_UPLOAD_PROBE),
        ], input=json.dumps({"project_id": evidence["edge"]["project"]["id"], "run_id": stage["run_id"],
                             "source_sha256": local["source_sha256"]}),
            capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AcceptanceError("Actual cloud upload verification failed: " + scrub(result.stderr[-2000:]))
        value = json.loads(result.stdout)
        save(self.root / "actual-cloud-upload.json", value)
        return value

    def validate_video(self, path, job, stage):
        result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                 "-of", "json", str(path)], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise AcceptanceError("ffprobe failed: " + result.stderr)
        probe = json.loads(result.stdout)
        media_tools().assert_clean_metadata(probe)
        video = next((item for item in probe["streams"] if item["codec_type"] == "video"), None)
        audio = [item for item in probe["streams"] if item["codec_type"] == "audio"]
        if not video or not audio:
            raise AcceptanceError("Native H3 video must contain decodable video and audio streams")
        duration = float(probe["format"]["duration"])
        expected = float(job["actual_duration"])
        if abs(duration - expected) > 1.0:
            raise AcceptanceError(f"Video duration {duration} differs from declared {expected}")
        if stage in {"preview", "proof"} and (video["width"], video["height"]) != (864, 480):
            raise AcceptanceError("Preview/proof resolution differs from the real H3 workflow")
        if stage in {"local_768", "cloud_768"} and video["height"] != 768:
            raise AcceptanceError("768P stage is not 768 pixels high")
        if stage == "regenerate_2k" and (video["width"] < 1920 or video["height"] < 1080):
            raise AcceptanceError("Final upscale did not produce a higher-resolution video")
        decode = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                                 "-map", "0:v:0", "-vf", "fps=1", "-f", "framemd5", "-"],
                                capture_output=True, text=True, timeout=300)
        frames = [line.rsplit(",", 1)[-1].strip() for line in decode.stdout.splitlines()
                  if line and not line.startswith("#")]
        if decode.returncode or len(set(frames)) < 2:
            raise AcceptanceError("Full video decode/moving-frame verification failed: " + decode.stderr)
        contact = path.with_suffix(".contact.png")
        preview = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path),
                                  "-vf", "fps=1,scale=320:-1,tile=3x1", "-frames:v", "1", str(contact)],
                                 capture_output=True, text=True, timeout=120)
        if preview.returncode or not contact.is_file():
            raise AcceptanceError("Could not extract real video review frames: " + preview.stderr)
        return {"ffprobe": probe, "decoded_frame_md5": frames, "contact_sheet": str(contact),
                "video_decode_returncode": decode.returncode, "audio_streams": len(audio)}

    def delivery_checkpoint(self):
        saved = self.state["jobs"].get("fast")
        if not saved or not saved.get("id"):
            raise WaitState("Only the existing fast task may be validated")
        self.verify_adapter_resume()
        before = self.idle(saved["id"])
        job = self.api("GET", "/v1/videos/" + saved["id"])
        if stage_by_id(job, "regenerate_2k")["status"] != "pending":
            raise AcceptanceError("2K must remain pending during privacy-boundary acceptance")
        records = []
        for stage_id in ("preview", "local_768"):
            stage = stage_by_id(job, stage_id)
            if stage["status"] not in {"approved", "awaiting_approval"} or not stage.get("output"):
                raise WaitState(f"The existing {stage_id} output is not available for privacy validation")
            records.append(self.archive("fast", job, stage))
        after = self.edge(saved["id"])
        if before["edge"]["project"] != after["edge"]["project"]:
            raise AcceptanceError("Privacy verification unexpectedly changed H3 stage state")
        report = {"status": "passed", "at": now(), "video_id": job["id"],
                  "h3_project_id": after["edge"]["project"]["id"], "checks": records,
                  "generation_started": False, "approval_sent": False,
                  "regenerate_2k_status": "pending", "stage_state_unchanged": True}
        save(self.root / "delivery-privacy.json", report)
        self.state["status"] = "privacy_verified_awaiting_2k_release"
        self.persist()

    def action(self, strategy, stage, action, body):
        saved = self.state["jobs"][strategy]
        canonical = json.dumps([saved["id"], stage, action, body], sort_keys=True)
        idem = self.args.run_label + "-" + hashlib.sha256(canonical.encode()).hexdigest()[:40]
        intent = saved["intents"].setdefault(idem, {"stage": stage, "action": action, "body": body,
                                                   "recorded_at": now(), "idempotency_key": idem})
        self.persist()
        result = self.api("POST", f"/v1/videos/{saved['id']}/stages/{stage}/{action}", idem=idem, body=body)
        intent["operation_id"] = result.get("operation_id")
        intent["response"] = result
        if not intent["operation_id"]:
            raise AcceptanceError("Stage action lacks a durable Router operation ID")
        # Replaying the same operation must not dispatch another paid/GPU run.
        replay = self.api("POST", f"/v1/videos/{saved['id']}/stages/{stage}/{action}", idem=idem, body=body)
        if replay.get("operation_id") != intent["operation_id"]:
            raise AcceptanceError("Stage action replay changed its operation identity")
        self.edge(saved["id"], stage)
        self.persist()
        return replay

    def approve(self, strategy, job, stage):
        stages = job["stages"]
        index = next(i for i, item in enumerate(stages) if item["id"] == stage["id"])
        following = stages[index + 1] if index + 1 < len(stages) else None
        body = {"output_id": stage["output_id"]}
        if stage["id"] == "context_ir":
            body["prompt"] = stage["output"]["text"]
        result = self.action(strategy, stage["id"], "approve", body)
        if stage_by_id(result, stage["id"])["status"] != "approved":
            raise AcceptanceError("Approval did not persist")
        for _ in range(2):
            time.sleep(self.args.poll_interval)
            result = self.api("GET", "/v1/videos/" + job["id"])
            if following and stage_by_id(result, following["id"])["status"] != "pending":
                raise AcceptanceError("Approval improperly started the next stage")
        self.event("approval_start_separation", job_id=job["id"], stage=stage["id"],
                   output_id=stage["output_id"], following_stage=(following or {}).get("id"),
                   following_status="pending" if following else None)
        return result

    def handoff_context(self):
        self.create("fast")
        job = self.wait("fast", "context_ir")
        context = stage_by_id(job, "context_ir")
        if context["status"] != "awaiting_approval":
            raise WaitState("Context IR is already approved; use continue after confirming browser evidence")
        if any(item["status"] != "pending" for item in job["stages"][1:]):
            raise AcceptanceError("Context IR handoff is not at the required pending boundary")
        handoff = {"router_job_id": job["id"], "stage": "context_ir", "output_id": context["output_id"],
                   "status": "awaiting_approval", "successors": job["stages"][1:],
                   "browser_agent": "01a06c9e-1159-7be3-9243-b00ff6c24968", "created_at": now(),
                   "instruction": "Approve this existing context in the real Router console. Do not start preview."}
        save(self.root / "browser-handoff.json", handoff)
        self.state["status"] = "awaiting_browser_context_approval"
        self.persist()
        print(json.dumps(scrub(handoff), ensure_ascii=False), flush=True)

    def preview_checkpoint(self):
        saved = self.state["jobs"].get("fast")
        if not saved or not saved.get("id"):
            raise WaitState("The existing Context IR task is required")
        self.require_ready("fast")
        job = self.api("GET", "/v1/videos/" + saved["id"])
        context = stage_by_id(job, "context_ir")
        if context["status"] != "approved":
            raise WaitState("Wait for the browser's actual context approval")
        self.browser_evidence(job, context)
        preview = stage_by_id(job, "preview")
        if preview["status"] == "pending":
            self.idle(job["id"])
            saved["continued"] = True
            self.persist()
            self.action("fast", "preview", "start", {"output_id": context["output_id"], "new_seed": False})
        elif preview["status"] not in ACTIVE:
            raise WaitState("Preview has already reached a review/terminal state; do not regenerate it")
        while True:
            job = self.api("GET", "/v1/videos/" + saved["id"])
            preview = stage_by_id(job, "preview")
            evidence = self.edge(job["id"], "preview")
            backend = evidence["edge"]["project"]["stages"]["preview"]
            if preview["status"] in TERMINAL_ERRORS:
                raise AcceptanceError("Preview failed before the reload checkpoint")
            if backend["status"] == "running" and backend.get("prompt_id"):
                operation = next((item for item in evidence["media"]["operations"]
                                  if item["stage"] == "preview" and item["action"] == "start"
                                  and item.get("payload", {}).get("expected_output_id") == context["output_id"]), None)
                if not operation or operation["status"] != "completed":
                    raise WaitState("The stage operation has not reached its durable completed receipt")
                checkpoint = {"at": now(), "video_id": job["id"], "h3_project_id": evidence["edge"]["project"]["id"],
                              "stage": "preview", "run_id": backend["run_id"], "prompt_id": backend["prompt_id"],
                              "operation_id": operation["id"], "idempotency_key": operation["idempotency_key"],
                              "stage_status": "running", "stage_action_http_inflight": False,
                              "h3_service": evidence["edge"]["h3_service"]["stdout"],
                              "comfy_service": evidence["edge"]["comfy_service"]["stdout"],
                              "instruction": "Reload only media-adapter. Do not cancel H3 or start/approve another stage."}
                self.state["reload_checkpoint"] = checkpoint
                self.state["status"] = "waiting_media_adapter_reload"
                save(self.root / "adapter-reload-checkpoint.json", checkpoint)
                self.persist()
                print(json.dumps(checkpoint, ensure_ascii=False), flush=True)
                return
            if time.monotonic() >= self.deadline:
                raise WaitState("Continue observing the original preview before the reload checkpoint")
            time.sleep(self.args.poll_interval)

    def browser_evidence(self, job, context):
        if self.state.get("browser_approval_evidence"):
            evidence = self.state["browser_approval_evidence"]
            if evidence["video_id"] != job["id"] or evidence["output_id"] != context["output_id"]:
                raise AcceptanceError("Browser approval evidence belongs to a different task/version")
            return
        if not self.args.browser_report:
            raise WaitState("An actual browser approval report is required before starting preview")
        path = self.args.browser_report.resolve()
        report = json.loads(path.read_text())
        check = next((item for item in report.get("checks", [])
                      if item["name"] == "stage_approval_does_not_start_successor"), {})
        evidence = check.get("evidence", {})
        before = evidence.get("before", {})
        context_before = next((item for item in before.get("stages", []) if item["id"] == "context_ir"), {})
        if (report.get("status") != "passed" or not report.get("real_network") or report.get("mocked_responses")
                or report.get("assigned_jobs", {}).get("video") != job["id"]
                or check.get("status") != "passed" or before.get("id") != job["id"]
                or context_before.get("output_id") != context["output_id"]
                or evidence.get("start_requests_sent") != 0 or evidence.get("approval_requests_sent") != 1
                or evidence.get("observation_seconds", 0) < 16 or not evidence.get("operation_id")):
            raise AcceptanceError("Browser report does not prove real version-bound approval/start separation")
        observations = evidence.get("observations", [])
        if len(observations) < 2 or any(
            stage_by_id(item, "context_ir")["status"] != "approved"
            or stage_by_id(item, "preview")["status"] != "pending" for item in observations
        ):
            raise AcceptanceError("Browser observation did not preserve the pending preview")
        self.state["browser_approval_evidence"] = {
            "report": str(path), "sha256": sha256(path), "video_id": job["id"],
            "output_id": context["output_id"], "operation_id": evidence["operation_id"],
            "observation_seconds": evidence["observation_seconds"], "real_network": True,
        }
        self.persist()

    def verify_adapter_resume(self):
        checkpoint = self.state.get("reload_checkpoint")
        if not checkpoint or self.state.get("adapter_resume_verified"):
            return
        job = self.api("GET", "/v1/videos/" + checkpoint["video_id"])
        evidence = self.edge(job["id"], "preview")
        current = evidence["edge"]["project"]["stages"]["preview"]
        operation = next((item for item in evidence["media"]["operations"]
                          if item["id"] == checkpoint["operation_id"]), None)
        if (current["run_id"] != checkpoint["run_id"] or current.get("prompt_id") != checkpoint["prompt_id"]
                or evidence["edge"]["project"]["id"] != checkpoint["h3_project_id"]
                or not operation or operation["status"] != "completed"
                or evidence["edge"]["h3_service"]["stdout"] != checkpoint["h3_service"]
                or evidence["edge"]["comfy_service"]["stdout"] != checkpoint["comfy_service"]):
            raise AcceptanceError("Recovery did not preserve the original H3 run, Comfy prompt and operation receipt")
        result = {"at": now(), "video_id": job["id"], "run_id": current["run_id"],
                  "prompt_id": current["prompt_id"], "operation_id": operation["id"],
                  "router_stage_status": stage_by_id(job, "preview")["status"], "same_upstream_execution": True}
        self.state["adapter_resume_verified"] = result
        self.event("adapter_resume_verified", **result)
        self.persist()

    def drive(self, strategy, *, browser_required=False, stop_after=None):
        job = self.create(strategy)
        job = self.wait(strategy, "context_ir")
        context = stage_by_id(job, "context_ir")
        if browser_required and context["status"] != "approved":
            raise WaitState("The browser agent must approve the existing Context IR before continuation")
        if browser_required:
            self.browser_evidence(job, context)
            if any(item["status"] != "pending" for item in job["stages"][1:]) and not self.state["jobs"][strategy].get("continued"):
                raise AcceptanceError("Browser approval unexpectedly advanced downstream execution")
            self.state["jobs"][strategy]["continued"] = True
            self.persist()
        pipeline = [item["id"] for item in job["stages"]]
        if stop_after and stop_after not in pipeline:
            raise AcceptanceError("The authorized stop stage is absent; no downstream stage may start")
        self.state["jobs"][strategy]["advertised_pipeline"] = pipeline
        self.persist()
        for index, stage_id in enumerate(pipeline):
            job = self.api("GET", "/v1/videos/" + job["id"])
            stage = stage_by_id(job, stage_id)
            if stage["status"] in TERMINAL_ERRORS:
                self.edge(job["id"], stage_id)
                raise AcceptanceError(f"Existing {stage_id} failed; no automatic retry/new paid run")
            if stage["status"] == "pending":
                if index == 0:
                    raise WaitState("The accepted creation has not started Context IR yet")
                predecessor = stage_by_id(job, pipeline[index - 1])
                if predecessor["status"] != "approved":
                    raise AcceptanceError("Cannot start without the actual approved predecessor")
                self.idle(job["id"])
                self.action(strategy, stage_id, "start", {"output_id": predecessor["output_id"], "new_seed": False})
            job = self.wait(strategy, stage_id)
            stage = stage_by_id(job, stage_id)
            if stage["status"] == "awaiting_approval":
                job = self.approve(strategy, job, stage)
            if stop_after == stage_id:
                self.state["jobs"][strategy]["status"] = "branch_covered_downstream_pending"
                self.persist()
                return job
        if job["status"] != "completed":
            raise AcceptanceError("All approved stages did not produce a completed video")
        last = stage_by_id(job, pipeline[-1])["output"]
        final = self.http.get(f"/v1/videos/{job['id']}/content")
        if final.status_code != 200 or hashlib.sha256(final.content).hexdigest() != last["sha256"]:
            raise AcceptanceError("Final Router video differs from the final stage archive")
        versions = self.api("GET", f"/v1/videos/{job['id']}/outputs")
        expected = {item["output_id"] for item in job["stages"]}
        if not expected <= {item["output_id"] for item in versions["data"]}:
            raise AcceptanceError("Router output history is missing completed stage versions")
        self.state["jobs"][strategy].update(status="completed", final_output_id=last["output_id"])
        self.state["status"] = strategy + "_pipeline_completed"
        self.event("pipeline_completed", strategy=strategy, job_id=job["id"], pipeline=pipeline)
        self.persist()
        return job

    def coverage(self):
        if self.state["jobs"].get("fast", {}).get("status") != "completed":
            raise WaitState("Complete the serial 4-second fast pipeline before branch coverage")
        strategies = self.options().get("videos", {}).get("strategy", [])
        for strategy, stop in (("cloud", "cloud_768"), ("safe", "proof")):
            if strategy not in strategies:
                raise AcceptanceError(f"The authorized {strategy} strategy is not advertised")
            if strategy in self.state["coverage"].get(stop, []):
                continue
            self.drive(strategy, stop_after=stop)
        self.state["status"] = "minimum_named_stage_coverage_verified"
        self.persist()

    def first_fatal(self, exc):
        fatal = {"at": now(), "phase": self.args.phase, "error_type": type(exc).__name__,
                 "error": scrub(str(exc)), "jobs": self.state.get("jobs", {}),
                 "events_directory": str(self.root / "events")}
        target = self.root / "first-fatal.json"
        if not target.exists():
            save(target, fatal)
        self.event("failure", error_type=type(exc).__name__, error=str(exc), first_fatal=str(target))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("inspect", "context", "preview", "continue", "coverage", "observe", "delivery"), default="inspect")
    parser.add_argument("--execute", action="store_true", help="Required for phases that can submit generation")
    parser.add_argument("--run-label", default="20260904-h3-live")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".config/ai-router-media/acceptance.env")
    parser.add_argument("--browser-report", type=Path, help="Completed real browser approval/start-separation report")
    parser.add_argument("--media-root", type=Path, default=Path("/opt/1panel/ai-router/media"))
    parser.add_argument("--strategy", choices=("fast", "safe", "cloud"), default="fast", help="Existing job to observe")
    parser.add_argument("--stage", default="context_ir", help="Existing stage to observe")
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--probe-interval", type=float, default=30)
    parser.add_argument("--max-wait", type=float, default=14400, help="Observation budget, never a cancellation deadline")
    args = parser.parse_args(argv)
    if args.phase in {"context", "preview", "continue", "coverage"} and not args.execute:
        parser.error("Generation requires an explicit --execute after the coordinator releases the video gate")
    if not SAFE_ID.fullmatch(args.run_label) or len(args.run_label) > 50:
        parser.error("run-label must be a stable, safe identifier of at most 50 characters")
    if args.poll_interval < 1 or args.probe_interval < 5 or args.max_wait <= 0:
        parser.error("Invalid observation intervals")
    return args


def main(argv=None):
    args = parse_args(argv)
    os.umask(0o077)
    runner = Runner(args)
    try:
        if args.phase == "inspect":
            runner.options()
            runner.edge()
            runner.state["status"] = "inspected_no_generation"
            runner.persist()
        elif args.phase == "context":
            runner.handoff_context()
        elif args.phase == "preview":
            runner.preview_checkpoint()
        elif args.phase == "continue":
            if not runner.state["jobs"].get("fast", {}).get("id"):
                raise WaitState("Create and hand off Context IR before continuing")
            runner.verify_adapter_resume()
            runner.drive("fast", browser_required=True)
        elif args.phase == "coverage":
            runner.coverage()
        elif args.phase == "delivery":
            runner.delivery_checkpoint()
        else:
            if args.strategy not in runner.state["jobs"]:
                raise WaitState("There is no existing task to observe")
            runner.wait(args.strategy, args.stage)
        print(json.dumps({"phase": args.phase, "manifest": str(runner.path),
                          "status": runner.state.get("status"),
                          "generation_phase_explicitly_released": args.phase in {"context", "preview", "continue", "coverage"}},
                         ensure_ascii=False), flush=True)
        return 0
    except WaitState as exc:
        runner.event("waiting", reason=str(exc), phase=args.phase)
        print(json.dumps({"status": "waiting", "reason": str(exc), "manifest": str(runner.path)},
                         ensure_ascii=False), flush=True)
        return 3
    except Exception as exc:
        runner.first_fatal(exc)
        print(json.dumps({"status": "failed", "first_fatal": str(runner.root / "first-fatal.json"),
                          "error": scrub(str(exc))}, ensure_ascii=False), flush=True)
        return 1
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
