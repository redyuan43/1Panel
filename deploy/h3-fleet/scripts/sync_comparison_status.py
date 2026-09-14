from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time


REMOTE = '''import json,pathlib,time,urllib.request,subprocess
root=pathlib.Path("/mnt/ivan-ext4-offload/h3-fleet/evidence/optimization-20260909")
latest={}
for path in root.glob("*/report.json"):
 try:
  report=json.loads(path.read_text())
  report_order_at=report.get("started_at") or path.stat().st_mtime
  candidates=[report]
  if isinstance(report.get("tasks"),list):
   candidates=[]
   for task in report["tasks"]:
    state="running" if task.get("submit_attempted") else "prepared"
    if task.get("status")=="collected":state="generated_pending_quality_review" if report.get("lease_released") and (report.get("both_unloaded") or report.get("all_unloaded")) else "finalizing"
    clean=report.get("lease_released") is True and (report.get("both_unloaded") is True or report.get("all_unloaded") is True) and all(report[key] is True for key in ("both_unloaded","all_unloaded") if key in report) and not any(report.get(key) for key in ("reconciliation_error","cleanup_error","cleanup_errors","retained_lease","lease_hold_error"))
    retained=report.get("status")=="failed" and task.get("status")=="collected" and not any(task.get(key) for key in ("error","reconciliation_error","cleanup_error","cleanup_errors","lease_hold_error")) and clean
    not_run=report.get("status")=="failed" and task.get("submit_attempted") is False and task.get("reconciliation")=="never submitted; empty queue/history" and clean
    if report.get("status") in {"failed","needs_reconciliation"} and not retained:state=report["status"]
    if not_run:state="not_run"
    candidates.append(dict(task,status=state,started_at=report.get("started_at",0),
     batch_replica=task.get("case")=="A4_C05",
     batch_status=report.get("status"),batch_error=report.get("error"),retained_from_failed_batch=retained,
     isolated_url=task.get("endpoint"),baseline={"isolated":{"gpu_uuid":task.get("gpu_uuid")}},
     lease_released=report.get("lease_released",False),error=None if retained or not_run else report.get("error"),run_id=report.get("run_id")))
  for candidate in candidates:
   case=candidate.get("case")
   if case not in {"R0","A4","A8","B8","C0","C1","D4","A4_C0","A4_C05","A4_C1"}:continue
   candidate["report_order_at"]=report_order_at
   if report_order_at>latest.get(case,{}).get("report_order_at",0):latest[case]=candidate
 except (ValueError,OSError):continue
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
queues={}
for port in (18188,18189,18190,18191):
 endpoint="http://127.0.0.1:"+str(port)
 try:
  with opener.open(endpoint+"/queue",timeout=3) as response:queues[endpoint]=json.load(response)
 except (OSError,ValueError):pass
running={item[1] for queue in queues.values() for item in queue["queue_running"]}
records=[]
for case,report in latest.items():
 identifier=report.get("prompt_id") or report.get("job",{}).get("upstream_prompt_id")
 status=report.get("status", "unknown")
 endpoint=report.get("isolated_url","http://127.0.0.1:18188")
 queue=queues.get(endpoint,{"queue_running":[],"queue_pending":[]})
 if identifier in {item[1] for item in queue["queue_running"]}:status="running"
 elif identifier in {item[1] for item in queue["queue_pending"]}:status="queued"
 elif status.startswith("generated"):status="completed"
 elif status in {"running","submitting"}:
  status="reconciling"
  if endpoint in queues:
   try:
    with opener.open(endpoint+"/history/"+identifier,timeout=3) as response:history=json.load(response)
    terminal=history.get(identifier,{}).get("status",{}).get("status_str")
    status="finalizing" if terminal=="success" else "failed" if terminal=="error" else "reconciling"
   except (OSError,ValueError):pass
 records.append({"id":case,"status":status,"prompt_id":identifier,"started_at":report.get("submitted_at"),
  "worker_seconds":report.get("execution",{}).get("execution_seconds"),"error":report.get("error"),
  "lease_released":report.get("lease_released",False),
 "gpu_uuid":report.get("baseline",{}).get("isolated",{}).get("gpu_uuid"),
  "endpoint":endpoint,"lane":{"http://127.0.0.1:18188":"fast","http://127.0.0.1:18189":"main","http://127.0.0.1:18190":"preview"}.get(endpoint) if "batch_replica" in report else None,
  "run_id":report.get("run_id"),
  "submit_attempted":report.get("submit_attempted"),
  "batch_status":report.get("batch_status"),"batch_error":report.get("batch_error"),
  "retained_from_failed_batch":report.get("retained_from_failed_batch",False),
  "replica":report.get("batch_replica",False),
  "admission_reason":report.get("admission_reason")})
matrix=subprocess.run(["systemctl","is-active","h3-comparison-A8-C1-20260909.service"],capture_output=True,text=True,timeout=5)
if matrix.stdout.strip()=="active" and "C1" not in latest:
 records.append({"id":"C1","status":"scheduled","prompt_id":None,"started_at":None})
followup=subprocess.run(["systemctl","is-active","h3-comparison-C1-after-B8-20260910.service"],capture_output=True,text=True,timeout=5)
if followup.stdout.strip()=="active" and latest.get("C1",{}).get("run_id")!="pink20260909-C1-s1-r3":
 records=[record for record in records if record["id"]!="C1"]
 records.append({"id":"C1","status":"scheduled","prompt_id":None,"started_at":None,
  "previous_error":latest.get("C1",{}).get("error")})
retry=subprocess.run(["systemctl","is-active","h3-comparison-B8-after-C1-20260910.service"],capture_output=True,text=True,timeout=5)
if retry.stdout.strip()=="active" and latest.get("B8",{}).get("run_id")!="pink20260909-B8-s1-r3":
 records=[record for record in records if record["id"]!="B8"]
 records.append({"id":"B8","status":"scheduled","prompt_id":None,"started_at":None,
  "previous_error":latest.get("B8",{}).get("error")})
finalcase=subprocess.run(["systemctl","is-active","h3-comparison-D4-after-B8-20260910.service"],capture_output=True,text=True,timeout=5)
if finalcase.stdout.strip()=="active" and latest.get("D4",{}).get("run_id")!="pink20260909-D4-s1":
 records=[record for record in records if record["id"]!="D4"]
 records.append({"id":"D4","status":"scheduled","prompt_id":None,"started_at":None})
print(json.dumps({"observed_at":time.time(),"cases":records,"running_ids":sorted(running)}))
'''


def poll():
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "ivan", "python3 -"],
                            input=REMOTE, text=True, capture_output=True, timeout=20, check=True)
    payload = json.loads(result.stdout)
    payload["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload["available"] = True
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            payload = poll()
        except (subprocess.SubprocessError, ValueError) as error:
            payload = {"available": False, "error": type(error).__name__, "observed_at": time.time(), "cases": []}
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        temporary.replace(args.output)
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
