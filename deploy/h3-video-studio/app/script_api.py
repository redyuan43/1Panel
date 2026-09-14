import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .script_planner import ROLE, markdown_script
from .skill_catalog import catalog


def install_script_api(module):
    router = APIRouter(prefix="/api/scripts")

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 100000:
                raise HTTPException(413, "脚本请求过大。")
        try:
            return json.loads(raw)
        except ValueError as error:
            raise HTTPException(400, "需要合法JSON。") from error

    @router.get("/options")
    async def options():
        return {"skills": catalog(), "agent_catalog": ROLE, "capability_manifest": module.SCRIPTS.planner.capabilities(),
                "generation_mode": "preview" if getattr(module.COMFY, "simulated", False) else "real",
                "approval_starts_generation": False}

    @router.get("")
    async def listing():
        return {"scripts": [{key: plan.get(key) for key in ("id", "revision", "status", "created_at", "updated_at")}
                            | {"title": (plan.get("draft") or {}).get("title") or plan["brief"]["prompt"][:50]}
                            for plan in module.SCRIPTS.store.list()]}

    @router.post("")
    async def create(request: Request):
        try:
            return await module.SCRIPTS.create(await body(request))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/{identifier}")
    async def get(identifier: str):
        return module.SCRIPTS.public(module.SCRIPTS.get(identifier))

    @router.get("/{identifier}/export")
    async def export(identifier: str, format: str = "markdown"):
        plan = module.SCRIPTS.get(identifier)
        if not plan.get("draft") or plan.get("draft_revision") != plan["revision"]:
            raise HTTPException(409, "当前版本尚未产出可导出的脚本。")
        if format not in {"markdown", "json"}:
            raise HTTPException(400, "仅支持Markdown或JSON导出。")
        content = markdown_script(plan) if format == "markdown" else json.dumps(module.SCRIPTS.public(plan), ensure_ascii=False, indent=2)
        suffix = "md" if format == "markdown" else "json"
        return PlainTextResponse(content, headers={"Content-Disposition": f'attachment; filename="{identifier}-r{plan["revision"]}.{suffix}"'})

    @router.post("/{identifier}/{action}")
    async def action(identifier: str, action: str, request: Request):
        try:
            return await module.SCRIPTS.action(identifier, action, await body(request))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    module.app.include_router(router)
