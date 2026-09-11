"""Isolated Studio/browser and OpenAI media protocol acceptance fixture."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from ai_router.audit import AuditLog
from ai_router.auth import AuthManager
from ai_router.media_service.app import create_app
from ai_router.media_service.gateway import router
from ai_router.media_service.creative_chat import maybe_creative_chat
from ai_router.store import InMemoryStateStore
from test_media_video_workflows import service, mp4, Stream


def build(root):
    for name in tuple(os.environ):
        if name.startswith("AI_ROUTER_"):
            del os.environ[name]
    os.environ.update(AI_ROUTER_ADMIN_KEY="studio-preview-only", AI_ROUTER_MEDIA_INTERNAL_KEY="fixture-only", AI_ROUTER_CREATIVE_CHAT_ENABLED="1")
    root.mkdir(parents=True, exist_ok=True)
    video = root / "fixture.mp4"
    if not video.exists():
        mp4(video)
    quality = root / "quality.mp4"
    if not quality.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", "scale=1344:768", "-c:v", "libx264", "-c:a", "copy", str(quality)], check=True)
    svc = service(root / "state", video.read_bytes())
    async def download(identifier):
        profile = next(x["body"]["profile"] for x in svc.h3.created if x["execution_id"] == identifier)
        return Stream(quality.read_bytes() if profile == "quality" else svc.h3.data)
    svc.h3.download_execution = download
    svc.store.configure({"enabled": True, "codex_ready": True, "h3_ready": True, "min_free_bytes": 0, "poll_interval": 1})
    @asynccontextmanager
    async def lifespan(app):
        await svc.start()
        yield
        await svc.close()
    app = FastAPI(lifespan=lifespan)
    auth = AuthManager(None, None)
    async def authenticate(header):
        if header != "Bearer studio-preview-only":
            from ai_router.media_service.contracts import MediaError
            raise MediaError("unauthorized", "fixture key required", 401)
        return SimpleNamespace(policy=SimpleNamespace(id="studio-fixture", media_models=("siyuan-image", "siyuan-video"), disclosure_mode="public", rpm_limit=30), key_id="fixture")
    auth.authenticate = authenticate
    app.state.runtime = SimpleNamespace(auth=auth, store=InMemoryStateStore(), audit=AuditLog(root / "audit.jsonl"))
    app.state.media_transport = httpx.ASGITransport(app=create_app(svc, run_worker=False))
    app.include_router(router(admin=True))
    app.include_router(router(admin=False))
    static = Path(__file__).resolve().parents[1] / "ai_router/static"
    app.mount("/assets", StaticFiles(directory=static))
    @app.get("/__studio_preview__")
    def marker():
        return {"isolated": True, "gpu_calls": 0, "simulated_executions": len(svc.h3.created)}
    @app.get("/")
    @app.get("/media")
    def page():
        return FileResponse(static / "studio.html")
    @app.post("/v1/chat/completions")
    @app.post("/v1/responses")
    async def chat(request: Request):
        request.state.server_request_id = "fixture-chat"
        result = await maybe_creative_chat(request, await request.json(), await authenticate(request.headers.get("authorization")), "responses" if request.url.path.endswith("responses") else "chat")
        return result or JSONResponse({"ordinary_chat": True})
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=14826)
    args = parser.parse_args()
    uvicorn.run(build(args.state_dir), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
