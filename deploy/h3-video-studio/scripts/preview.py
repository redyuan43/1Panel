from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PreviewFleet:
    is_fleet = True
    simulated = True

    def __init__(self, root, delay=0.15):
        self.root = root
        self.delay = delay
        self.jobs = {}
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = self.root / "executions.json"
        if self.ledger.exists():
            self.jobs = json.loads(self.ledger.read_text())
        self.video = self.root / "synthetic-preview.mp4"
        if not self.video.exists():
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=1344x768:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000",
                "-t", "5.166667", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", str(self.video)], check=True)

    def health(self):
        return {"system": {"comfyui_version": "SIMULATED"}, "devices": [
            {"name": "模拟 fast / main / preview（无GPU调用）"}]}

    def free(self):
        return None

    def upload_assets(self, assets):
        return None

    def begin_batch(self, schedule_id):
        return None

    def end_batch(self, schedule_id):
        return None

    def submit_stage(self, workflow, execution_id, stage, profile):
        with self.lock:
            self.jobs.setdefault(execution_id, {"status": "running", "lane_id": ["fast", "main", "preview"][len(self.jobs) % 3]})
            self._save()
        return execution_id

    def _save(self):
        temporary = self.ledger.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.jobs))
        temporary.replace(self.ledger)

    def wait_execution(self, execution_id, destination, *, progress, cancelled):
        with self.lock:
            job = self.jobs.get(execution_id)
        if not job:
            from app.fleet import SubmissionUnknown
            raise SubmissionUnknown("体验执行记录缺失；未创建新任务。")
        for percent in (15, 45, 75):
            if cancelled():
                with self.lock:
                    job["status"] = "cancelled"
                    self._save()
                raise RuntimeError("模拟任务已取消。")
            progress({"status": "running", "progress": percent, "lane_id": job["lane_id"],
                      "detail": "模拟阶段推进，不调用真实GPU"})
            time.sleep(self.delay)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.video, destination)
        with self.lock:
            job["status"] = "completed"
            self._save()
        return {"execution_id": execution_id, "prompt_id": execution_id, "simulated": True, "lane_id": job["lane_id"]}


class PreviewMiniMax:
    def __init__(self, fleet):
        self.fleet = fleet

    def configured(self):
        return True

    def context_ir(self, project, *, progress, cancelled):
        return {"task_id": "simulation-context", "prompt": "【交互体验方案，无真实模型调用】\n" + project["prompt_original"], "simulated": True}

    def generate_768(self, project, destination, *, progress, cancelled):
        if cancelled():
            raise RuntimeError("模拟云端任务已取消。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.fleet.video, destination)
        return {"task_id": "simulation-cloud", "simulated": True}

    def regenerate_2k(self, project, source, destination, *, progress, cancelled):
        return self.generate_768(project, destination, progress=progress, cancelled=cancelled)


def configure_preview(root, delay=0.15):
    root = Path(root).resolve()
    os.environ["H3_STUDIO_DATA"] = str(root / "projects")
    os.environ["H3_WORKFLOW_ROOT"] = str(ROOT / "workflows")
    for name in ("H3_FLEET_URL", "H3_FLEET_KEY_FILE", "MINIMAX_API_KEY", "H3_ROUTER_KEY"):
        os.environ.pop(name, None)
    os.environ["MINIMAX_CREDENTIALS"] = str(root / "NO_CLOUD_CREDENTIALS")
    from app import main
    from app.config import get_settings
    from app.storage import BatchStore, ProjectStore
    main.SETTINGS = get_settings()
    main.STORE = ProjectStore(main.SETTINGS.database_path)
    main.BATCH_STORE = BatchStore(main.SETTINGS.database_path)
    main.SCRIPTS = main.ScriptService(main.SETTINGS.data_root / "script-plans.sqlite3")
    main.COMFY = PreviewFleet(root / "fixtures", delay)
    main.MINIMAX = PreviewMiniMax(main.COMFY)
    return main


def deny_network(*args, **kwargs):
    raise RuntimeError("体验模式禁止外部网络调用。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=14829)
    parser.add_argument("--step-delay", type=float, default=1)
    args = parser.parse_args()
    if not os.environ.get("H3_STUDIO_KEY_FILE") and not os.environ.get("H3_STUDIO_TAILSCALE_USERS"):
        parser.error("体验服务必须配置访问密钥文件或 Tailscale 用户白名单")
    main = configure_preview(args.state_dir, delay=max(0, args.step_delay))
    urllib.request.urlopen = deny_network
    import uvicorn
    uvicorn.run(main.app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
