#!/usr/bin/env python3
"""Read-only remote preflight; never invokes service control or GPU inference."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path('/home/ai/.local/state/h3-edge-fleet-candidate-20260914')
BENCH = Path('/home/ai/.local/state/h3-edge-same-benchmark-20260914')
UNITS = ['comfyui-edge.service', 'h3-video-studio.service', 'qwen38-flash-next-vllm.service']
FILES = ['/home/admin/github/qwen3.8_flash_next/scripts/serve_flash_disk_cache.sh',
         '/home/admin/github/qwen3.8_flash_next/scripts/common.sh',
         '/home/admin/github/qwen3.8_flash_next/config/flash.env',
         '/home/admin/.config/minimax/credentials.env',
         '/home/admin/.config/h3-video-studio/router.env']
source = '''import json,hashlib,subprocess,urllib.request,pathlib,time,re
units=UNITS
files=FILES
def command(*a): return subprocess.check_output(a,text=True)
def get(url):
 with urllib.request.urlopen(url,timeout=5) as response: return response.read().decode()
hashes={"unit:"+u:hashlib.sha256(command('systemctl','--user','cat',u).encode()).hexdigest() for u in units}
hashes.update({"file:"+f:hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest() for f in files})
states={u:command('systemctl','--user','show',u,'-p','ActiveState','-p','SubState') for u in units}
metrics=get('http://100.101.54.115:18300/metrics')
counts={k:re.findall(r'^vllm:num_requests_'+k+r'(?:\\{[^\\n]*\\})?\\s+([^\\s]+)',metrics,re.M) for k in ['running','waiting']}
models=json.loads(get('http://100.101.54.115:18300/v1/models'))['data']
mem=int(re.search(r'^MemAvailable:\\s+(\\d+)',pathlib.Path('/proc/meminfo').read_text(),re.M).group(1))*1024
print(json.dumps({'read_only':True,'checked_at':time.time(),'hashes':hashes,'states':states,'qwen_requests':counts,'models':[{'id':m['id'],'max_model_len':m.get('max_model_len')} for m in models],'mem_available_bytes':mem,'comfy_queue':json.loads(get('http://127.0.0.1:8188/queue')),'studio_health':json.loads(get('http://127.0.0.1:8789/api/health'))}))
'''.replace('UNITS',repr(UNITS)).replace('FILES',repr(FILES))
result=subprocess.run(['ssh','-F',str(BENCH/'ssh-lan-config'),'edge-lan','python3 -'],input=source,text=True,capture_output=True,check=True)
data=json.loads(result.stdout)
(ROOT/'edge-lifecycle-dryrun.json').write_text(json.dumps(data,indent=2)+'\n')
manifest={'units':UNITS,'files':FILES,'expected_hashes':data['hashes'],'qwen_url':'http://100.101.54.115:18300'}
(ROOT/'edge-lifecycle-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'read_only':data['read_only'],'qwen_requests':data['qwen_requests'],'models':data['models'],'mem_available_bytes':data['mem_available_bytes'],'comfy_idle':data['comfy_queue']=={'queue_running':[],'queue_pending':[]}}))
