"""Isolated media UI acceptance server. No production endpoints or AI requests."""
from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ai_router.audit import AuditLog
from ai_router.auth import AuthManager
from ai_router.media_service.app import create_app as internal_app
from ai_router.media_service.gateway import router
from ai_router.media_service.service import MediaService
from ai_router.media_service.storage import MediaStore
from ai_router.store import InMemoryStateStore
from test_media_service import FakeH3, FakeImage


def build(root: Path):
    for name in tuple(os.environ):
        if name.startswith("AI_ROUTER_"):
            del os.environ[name]
    os.environ.update(AI_ROUTER_ADMIN_KEY="ui-preview-only", AI_ROUTER_MEDIA_INTERNAL_KEY="fixture-only")
    root.mkdir(parents=True, exist_ok=True)
    video = root / "fixture.mp4"
    if not video.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=320x180:rate=12", "-t", "1", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=video.read_bytes(), headers={"content-type": "video/mp4"},
    )))

    class PreviewH3(FakeH3):
        def __init__(self):
            super().__init__()
            self.template = copy.deepcopy(self.project)
            self.projects = {}

        async def create(self, job):
            key = "fixture_" + job["id"]
            if key not in self.projects:
                self.projects[key] = {**copy.deepcopy(self.template), "id": key}
            return self.projects[key]

        async def get(self, project_id):
            return self.projects[project_id]

        async def action(self, project_id, stage, action, payload):
            self.project = self.projects[project_id]
            result = await super().action(project_id, stage, action, payload)
            if stage == "context_ir" and action == "start":
                self.project["pipeline"][0]["output_id"] = project_id + "_context"
            if stage == "preview" and action == "start":
                self.project["pipeline"][1].update(status="awaiting_approval", progress=100,
                                                   output_id="fixture_preview_" + payload["operation_id"])
            return result

        async def download(self, project_id, output_id):
            return client.stream("GET", "http://fixture/output")

    svc = MediaService(MediaStore(root / "state"), client=client, codex=FakeImage(), qwen=FakeImage(), h3=PreviewH3())
    svc.store.configure({"enabled": True, "codex_ready": True, "h3_ready": True, "min_free_bytes": 0, "poll_interval": 1})
    @asynccontextmanager
    async def lifespan(app):
        await svc.start()
        yield
        await svc.close()
    app = FastAPI(lifespan=lifespan)
    app.state.runtime = SimpleNamespace(
        auth=AuthManager(None, None), store=InMemoryStateStore(), audit=AuditLog(root / "audit.jsonl"),
    )
    app.state.media_transport = httpx.ASGITransport(app=internal_app(svc, run_worker=False))
    app.include_router(router(admin=True))
    static = Path(__file__).resolve().parents[1] / "ai_router/static"
    app.mount("/assets", StaticFiles(directory=static))

    @app.get("/__ui_preview__")
    def marker():
        return {"isolated": True, "media": True}

    @app.get("/")
    @app.get("/media")
    def page():
        return FileResponse(static / "media.html")

    @app.get("/api/clients")
    def accounts(request: Request):
        app.state.runtime.auth.authenticate_admin(request.headers.get("authorization"))
        return {"clients": []}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=14822)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if args.detach:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        with (args.state_dir / "preview.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(args.state_dir),
                 "--port", str(args.port)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
            )
        print(f"Isolated preview PID {child.pid}, http://127.0.0.1:{args.port}/media")
    else:
        uvicorn.run(build(args.state_dir), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
