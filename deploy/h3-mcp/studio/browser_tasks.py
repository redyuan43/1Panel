from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool


def install(module, connector):
    from .connector_assets import install_routes
    from .connector_api import _enabled
    from .mcp_settings import administrator
    api = APIRouter(prefix="/api/h3-browser")

    def owner(request, task_id=None):
        if not administrator(request):
            raise HTTPException(403, "需要工作室管理员身份")
        task_id = task_id or request.query_params.get("task_id")
        if task_id:
            project = module.STORE.get(task_id)
            if not project or not project.get("connector_owner"):
                raise HTTPException(409, "历史项目不自动转换，请创建新任务保留旧记录")
            return project["connector_owner"]
        identity = request.headers.get("tailscale-user-login", "studio-administrator")
        return "studio-web-" + hashlib.sha256(identity.encode()).hexdigest()[:24]

    install_routes(api, connector.assets, owner, lambda: _enabled("H3_CONNECTOR_WRITES_ENABLED"))

    @api.get("/operations/{operation_id}")
    async def operation(operation_id: str, request: Request):
        result = await run_in_threadpool(connector.call, "h3_get_task", {"operation_id": operation_id}, owner(request))
        project = module._public_project(module.STORE.get(result["task_id"]))
        project["operation_receipt"] = result["receipt"]
        return project

    @api.post("/call/{tool}")
    async def call(tool: str, request: Request):
        if tool not in {"h3_save_draft", "h3_confirm_prompt", "h3_start_preview", "h3_review_preview", "h3_cancel_task"}:
            raise HTTPException(404, "未知任务操作")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise HTTPException(413, "任务参数过大")
        try:
            arguments = json.loads(body)
        except ValueError as error:
            raise HTTPException(400, "任务参数无效") from error
        if not isinstance(arguments, dict):
            raise HTTPException(400, "任务参数无效")
        identity = owner(request, arguments.get("task_id"))
        if tool != "h3_cancel_task":
            try:
                secret = Path(os.environ["H3_CONNECTOR_KEY_FILE"]).read_text().strip()
                async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False,
                    transport=getattr(module.app.state, "mcp_management_transport", None)) as client:
                    response = await client.get("http://127.0.0.1:4001/internal/h3-mcp-management/status", headers={"Authorization": "Bearer " + secret})
                response.raise_for_status()
                policy = response.json()
                if policy.get("writes_enabled") is not True or tool == "h3_start_preview" and policy.get("generation_enabled") is not True:
                    raise HTTPException(403, "视频或草稿入口已关闭；没有提交 GPU 任务，请核对设置页")
            except (OSError, KeyError, ValueError, httpx.HTTPError) as error:
                raise HTTPException(503, "接单策略不可核实；未提交任务") from error
        result = await run_in_threadpool(connector.call, tool, arguments, identity)
        project = module.STORE.get(result["task_id"])
        return module._public_project(project)

    module.app.include_router(api)
