"""Run the single RTX 4060 Ti video Fleet independently of u24 text services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import uvicorn


ROOT = Path.home() / ".local/state/siyuan-video-u24-production"
DATA = ROOT / "data"
GPU_UUID = "GPU-0befdd20-6ea9-4e7e-3378-635e20f42536"


def main() -> None:
    release = Path(os.environ["SIYUAN_U24_FLEET_RELEASE"]).resolve(strict=True)
    key = (ROOT / "key").read_text().strip()
    if not key:
        raise RuntimeError("u24 Fleet key is empty")
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update({
        "H3_ROUTER_KEY": key,
        "H3_FLEET_LANES": json.dumps([{
            "id": "fast", "url": "http://127.0.0.1:19388", "device": "fast",
            "gpu_uuid": GPU_UUID,
        }]),
        "H3_FLEET_DATABASE": str(DATA / "fleet.sqlite3"),
        "H3_OFFLOAD_ROOT": str(DATA),
        "H3_LANE_DATA_ROOT": str(DATA / "lanes"),
        "H3_PERSISTENT_OUTPUT_ROOTS": json.dumps({"fast": str(DATA / "fast/output")}),
        "H3_TURBO_TEMPLATE": str(ROOT / "templates/turbo4-api.json"),
        "H3_QUALITY_TEMPLATE": str(ROOT / "templates/quality14-768-api.json"),
        "H3_COMPUTE_CGROUP": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/siyuan-video-u24-workers.service",
        "H3_RECIPE_POLICY": str(ROOT / "fleet/config/recipe-scheduling.json"),
        "H3_CAPACITY_POLICY": str(release / "config/u24-video-capacity.json"),
    })
    os.chdir(release)
    sys.path.insert(0, str(release))
    uvicorn.run("app.main:app", host="100.120.143.109", port=19390, access_log=False)


if __name__ == "__main__":
    main()
