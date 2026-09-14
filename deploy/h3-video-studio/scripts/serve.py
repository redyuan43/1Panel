from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    required = ["H3_STUDIO_KEY_FILE", "H3_STUDIO_DATA"]
    if not os.environ.get("H3_FLEET_NODES_FILE"):
        required.extend(("H3_FLEET_URL", "H3_FLEET_KEY_FILE"))
    for name in required:
        if not os.environ.get(name):
            raise SystemExit(f"真实模式缺少必需配置：{name}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=14830, proxy_headers=False, access_log=False)
