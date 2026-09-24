"""Run the production Ivan video Fleet against the qualified RTX 3060 pair."""
import argparse
import json
import os
from pathlib import Path
import sys

import uvicorn


CASE = "siyuan-video-production"
parser = argparse.ArgumentParser()
parser.add_argument("role", choices=["ivan"])
args = parser.parse_args()
root = Path.home() / ".local/state" / CASE
os.environ["H3_ROUTER_KEY"] = (root / "key").read_text().strip()
host = "100.96.79.21"
data = Path("/media/ivan/44A2C0D9A2C0D11A") / CASE
lanes = [
    {"id": "main", "url": "http://127.0.0.1:19388", "device": "main",
     "gpu_uuid": "GPU-08c21842-c266-7f7d-6e5d-d494d4c20c4f"},
    {"id": "preview", "url": "http://127.0.0.1:19389", "device": "preview",
     "gpu_uuid": "GPU-b9ca94d5-6180-2d81-bb33-5ad04722f492", "preview_only": True},
]
template_base = root / "templates"
os.environ.update({
    "H3_FLEET_LANES": json.dumps(lanes),
    "H3_FLEET_DATABASE": str(data / "fleet.sqlite3"),
    "H3_OFFLOAD_ROOT": str(data),
    "H3_LANE_DATA_ROOT": str(data / "lanes"),
    "H3_TURBO_TEMPLATE": str(template_base / "turbo4-api.json"),
    "H3_QUALITY_TEMPLATE": str(template_base / "quality14-768-api.json"),
    "H3_COMPUTE_CGROUP": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/siyuan-video-ivan-workers.service",
    "H3_RECIPE_POLICY": str(root / "fleet/config/recipe-scheduling.json"),
    "H3_CAPACITY_POLICY": str(root / "fleet/config/capacity.json"),
})
data.mkdir(parents=True, exist_ok=True)
release = Path(os.environ["SIYUAN_VIDEO_FLEET_RELEASE"])
os.chdir(release)
sys.path.insert(0, str(release))
uvicorn.run("app.main:app", host=host, port=19390, access_log=False)
