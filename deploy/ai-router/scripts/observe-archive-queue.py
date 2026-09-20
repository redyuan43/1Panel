#!/usr/bin/env python3
"""Read-only natural-traffic observer; never sends inference or control writes."""
import argparse
import json
from pathlib import Path
import subprocess
import time


PROBE = r'''
import json,os,sqlite3,sys,time,urllib.request
from pathlib import Path
role,since=sys.argv[1],float(sys.argv[2])
host='127.0.0.1' if role=='local' else os.environ['AI_ROUTER_TAILSCALE_IP']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
    request=urllib.request.Request('http://'+host+':4000'+path,headers={'Authorization':'Bearer '+os.environ['AI_ROUTER_ADMIN_KEY']})
    with opener.open(request,timeout=10) as response: return json.load(response)
instance=get('/internal/status')['instance']
import asyncio
from redis.asyncio import Redis
from ai_router.archive_queue import ArchiveQueue
async def queue_status():
    queue=ArchiveQueue(Redis.from_url(os.environ['AI_ROUTER_REDIS_URL'],socket_timeout=2),None)
    try: return await queue.status()
    finally: await queue.close()
result={'instance':{k:instance.get(k) for k in ('instance_id','boot_id','draining','active_request_count')},'queue':asyncio.run(queue_status()), 'requests':[]}
path=Path(os.environ.get('AI_ROUTER_ROUTE_TRACE_DB_PATH','/data/audit/route-traces.sqlite3'))
db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
try:
    rows=db.execute("SELECT payload_json FROM route_traces WHERE started_at>=? AND status IN ('succeeded','failed','interrupted') ORDER BY started_at DESC LIMIT 500",(since,)).fetchall()
    for row in rows:
        p=json.loads(row[0])
        if p.get('boot_id')!=instance.get('boot_id'): continue
        obs=p.get('observation',{})
        request=p.get('request',{})
        start,end=p.get('started_at'),p.get('completed_at')
        total=(end-start)*1000 if isinstance(start,(int,float)) and isinstance(end,(int,float)) else None
        last=obs.get('last_output_ms')
        result['requests'].append({'request_id':p.get('request_id'),'status':p.get('status'),'protocol':p.get('protocol'),
            'endpoint_id':p.get('selected_endpoint_id') or p.get('endpoint_id'),
            'prompt_tokens':request.get('prompt_tokens'), 'total_ms':total,'ttft_ms':obs.get('ttft_ms'),
            'tail_ms':max(0,total-last) if total is not None and isinstance(last,(int,float)) else None,
            'queue_wait_ms':obs.get('queue_wait_ms'), 'upstream_dispatch_ms':obs.get('upstream_dispatch_ms'),
            'stages':obs.get('router_phase_timings',{}).get('stages',{})})
finally: db.close()
print(json.dumps(result))
'''


def snapshot(since):
    result = {"observed_at": time.time(), "since": since, "instances": {}}
    for role in ("local", "tail"):
        try:
            response = subprocess.run(["docker", "exec", "-i", "1panel-ai-router-router-api-" + role + "-1",
                "python", "-", role, str(since)], input=PROBE, text=True, capture_output=True, timeout=30, check=True)
            result["instances"][role] = json.loads(response.stdout)
        except Exception as error:
            result["instances"][role] = {"probe_error": type(error).__name__}
        value = result["instances"][role]
        queue = value.get("queue") or {}
        alerts = value["alerts"] = []
        if queue and not queue.get("worker_alive"):
            alerts.append("archive_worker_missing")
        if queue.get("oldest_age_seconds", 0) >= 30:
            alerts.append("archive_backlog_over_30s")
        if queue.get("failed_requests", 0):
            alerts.append("archive_processing_error")
        for request in value.get("requests", []):
            stages = request.get("stages", {})
            if ((request.get("ttft_ms") or 0) >= 30000
                    or (request.get("tail_ms") or 0) >= 10000
                    or stages.get("archive_enqueue", {}).get("max_ms", 0) >= 500):
                alerts.append("slow_request:" + str(request["request_id"]))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=float, default=time.time())
    parser.add_argument("--duration", type=float, default=86400)
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0, args.duration)
    seen = set()
    while True:
        result = snapshot(args.since)
        for role, value in result["instances"].items():
            requests = value.get("requests", [])
            value["sampled_completed_count"] = len(requests)
            value["requests"] = [r for r in requests if (role, r["request_id"]) not in seen]
            seen.update((role, r["request_id"]) for r in requests)
        with args.output.open("a") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({"observed_at":result["observed_at"],"instances":{
            k:{"queue":v.get("queue"),"requests":len(v.get("requests",[])),"probe_error":v.get("probe_error"),"alerts":v.get("alerts",[])}
            for k,v in result["instances"].items()}}), flush=True)
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(1,args.interval),remaining))
