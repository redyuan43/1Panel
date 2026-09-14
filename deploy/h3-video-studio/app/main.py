from __future__ import annotations

import json
import math
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .batching import (
    ACTIVE_ITEM_STATUSES,
    ACTIVE_SCHEDULE_STATUSES,
    BatchInfrastructureError,
    BatchItemError,
    LocalGpuGate,
    TIMEZONE,
    apply_missed_schedule_policy,
    check_disk_space,
    classify_batch_error,
    is_local_768_candidate,
    next_daily_run,
    parse_daily_time,
    parse_once_local,
)
from .clients import ComfyClient, MiniMaxClient
from .fleet import configured_client, SubmissionUnknown
from .access import install_access
from .config import SETTINGS, ensure_directories
from .media import save_asset, validate_2k_source
from .storage import BatchStore, ProjectStore
from .script_service import ScriptService
from .recipes import confirmed_recipe, recipe_scope, require_recipe_id
from .workflows import (
    actual_duration,
    build_workflow,
    pipeline_for,
    project_runtime_summary,
    runtime_profile,
    uses_turbo_preview,
    validate_project_config,
)


app = FastAPI(title="H3 Video Studio", version="0.1.0")
install_access(app)
STORE = ProjectStore(SETTINGS.database_path)
BATCH_STORE = BatchStore(SETTINGS.database_path)
SCRIPTS = ScriptService(SETTINGS.data_root / "script-plans.sqlite3")
COMFY = configured_client()
if getattr(COMFY, "is_fleet", False):
    COMFY.external_status_path = SETTINGS.comparison_results_root / "live.json"
MINIMAX = MiniMaxClient()
LOCAL_GPU_LOCK = threading.Lock()
GPU_GATE = LocalGpuGate()
BATCH_MUTATION_LOCK = threading.RLock()
BATCH_EXECUTION_LOCK = threading.Lock()
BATCH_RUN_ACTIVE = threading.Event()
BATCH_SCHEDULER_STOP = threading.Event()
BATCH_SCHEDULER_THREAD: threading.Thread | None = None

STAGE_IDS = {
    "context_ir",
    "preview",
    "proof",
    "local_768",
    "cloud_768",
    "regenerate_2k",
}
LOCAL_STAGES = {"preview", "proof", "local_768"}
EXPECTED_SECONDS = {
    "preview_turbo": 420,
    "preview_quality": 1000,
    "proof": 1000,
    "local_768": 4000,
}


@app.on_event("startup")
def startup() -> None:
    global BATCH_SCHEDULER_THREAD
    ensure_directories()
    STORE.initialize()
    BATCH_STORE.initialize()
    SCRIPTS.startup()
    _recover_batch_schedules()
    _mark_interrupted_stages()
    BATCH_SCHEDULER_STOP.clear()
    BATCH_SCHEDULER_THREAD = threading.Thread(
        target=_batch_scheduler_loop,
        name="h3-768p-scheduler",
        daemon=True,
    )
    BATCH_SCHEDULER_THREAD.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    BATCH_SCHEDULER_STOP.set()
    await SCRIPTS.shutdown()


@app.get("/api/health")
def health() -> dict:
    comfy_ok = False
    comfy_detail = None
    try:
        stats = COMFY.health()
        comfy_ok = True
        comfy_detail = {
            "version": stats.get("system", {}).get("comfyui_version"),
            "device": (stats.get("devices") or [{}])[0].get("name"),
        }
    except Exception as error:
        comfy_detail = {"error": str(error)}
    return {
        "ok": comfy_ok,
        "service": "h3-video-studio",
        "execution_mode": "preview" if getattr(COMFY, "simulated", False) else "ivan-fleet" if getattr(COMFY, "is_fleet", False) else "legacy-comfy",
        "comfy": comfy_detail,
        "minimaxConfigured": MINIMAX.configured(),
    }


@app.get("/api/projects")
def list_projects() -> dict:
    return {"projects": [_public_project(project) for project in STORE.list()]}


@app.get("/api/capacity")
def fleet_capacity() -> dict:
    if getattr(COMFY, "simulated", False) or not getattr(COMFY, "is_fleet", False):
        return {"available": False, "lanes": [], "reason": "当前模式没有真实三卡容量数据"}
    return COMFY.capacity()


@app.get("/api/recipes")
def recipe_catalog() -> dict:
    if not getattr(COMFY, "is_fleet", False):
        return {"enabled": False, "recipes": [], "reason": "配方调度未就绪"}
    return COMFY.recipe_catalog()


@app.get("/api/768-queue/candidates")
def list_768_candidates() -> dict:
    candidates = []
    for project in STORE.list(limit=500):
        if _active_batch_for_project(project["id"]):
            continue
        eligible, reason = is_local_768_candidate(project)
        if not eligible:
            continue
        profile = runtime_profile(project, "local_768")
        preview_stage = (
            "proof"
            if "proof" in [stage["id"] for stage in pipeline_for(project)]
            else "preview"
        )
        candidates.append(
            {
                "id": project["id"],
                "name": project["name"],
                "mode": project["mode"],
                "duration": project["actual_duration"],
                "seed": project["seed"],
                "preview_stage": preview_stage,
                "preview_artifact_url": (
                    f"/api/projects/{project['id']}/artifacts/{preview_stage}"
                    if project["stages"][preview_stage].get("artifact")
                    else None
                ),
                "estimate": profile,
                "eligible": eligible,
                "reason": reason,
                "updated_at": project["updated_at"],
            }
        )
    return {"candidates": candidates}


@app.get("/api/768-queue/schedules")
def list_batch_schedules() -> dict:
    return {
        "schedules": [
            _public_batch_schedule(schedule)
            for schedule in BATCH_STORE.list(limit=100)
        ],
        "active_batch_id": GPU_GATE.active_batch_id(),
    }


@app.get("/api/768-queue/schedules/{schedule_id}")
def get_batch_schedule(schedule_id: str) -> dict:
    return _public_batch_schedule(_require_batch_schedule(schedule_id))


@app.post("/api/768-queue/schedules")
def create_batch_schedule(payload: dict = Body(...)) -> dict:
    now = time.time()
    kind = str(payload.get("kind", "once"))
    if kind not in {"once", "daily"}:
        raise HTTPException(status_code=400, detail="定时类型必须是 once 或 daily。")
    project_ids = _unique_project_ids(payload.get("project_ids"))
    if not project_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个768P项目。")
    if kind == "once":
        try:
            next_run_at = parse_once_local(str(payload.get("once_local", "")))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if next_run_at < now:
            raise HTTPException(status_code=400, detail="单次计划时间必须晚于当前时间。")
        daily_time = None
    else:
        daily_time = str(payload.get("daily_time", "23:00"))
        try:
            parse_daily_time(daily_time)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        next_run_at = next_daily_run(daily_time, now)

    schedule_id = uuid.uuid4().hex[:12]
    schedule = {
        "id": schedule_id,
        "name": str(payload.get("name", "")).strip() or "768P 夜间批次",
        "kind": kind,
        "timezone": "Asia/Shanghai",
        "once_local": payload.get("once_local") if kind == "once" else None,
        "daily_time": daily_time,
        "status": "scheduled",
        "detail": "等待计划时间",
        "failure_policy": "smart",
        "next_run_at": next_run_at,
        "last_run_date": None,
        "items": [],
        "runs": [],
        "current_item_id": None,
        "pause_requested": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }
    with BATCH_MUTATION_LOCK:
        try:
            BATCH_STORE.save(schedule)
            schedule = _replace_schedule_projects(schedule_id, project_ids)
        except Exception as error:
            _restore_schedule_projects(schedule)
            BATCH_STORE.delete(schedule_id)
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_batch_schedule(schedule)


@app.patch("/api/768-queue/schedules/{schedule_id}")
def update_batch_schedule(schedule_id: str, payload: dict = Body(...)) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if schedule["status"] not in {"scheduled", "waiting_for_items", "paused"}:
        raise HTTPException(status_code=409, detail="运行中的批次不能修改。")
    with BATCH_MUTATION_LOCK:
        if "project_ids" in payload:
            schedule = _replace_schedule_projects(
                schedule_id,
                _unique_project_ids(payload.get("project_ids")),
            )

        def mutate(item: dict) -> None:
            if "name" in payload:
                item["name"] = str(payload["name"]).strip() or item["name"]
            if "kind" in payload:
                kind = str(payload["kind"])
                if kind not in {"once", "daily"}:
                    raise ValueError("定时类型必须是 once 或 daily。")
                item["kind"] = kind
            if item["kind"] == "once":
                once_local = str(payload.get("once_local", item.get("once_local") or ""))
                item["once_local"] = once_local
                item["daily_time"] = None
                item["next_run_at"] = parse_once_local(once_local)
            else:
                daily_time = str(payload.get("daily_time", item.get("daily_time") or "23:00"))
                parse_daily_time(daily_time)
                item["daily_time"] = daily_time
                item["once_local"] = None
                item["next_run_at"] = next_daily_run(daily_time, time.time())
            item["status"] = "scheduled" if item["items"] else "waiting_for_items"
            item["detail"] = "计划已更新"
            _refresh_scheduled_project_times(item)

        try:
            schedule = BATCH_STORE.update(schedule_id, mutate)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    return _public_batch_schedule(schedule)


@app.delete("/api/768-queue/schedules/{schedule_id}")
def delete_batch_schedule(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if any(item["status"] == "running" for item in schedule["items"]):
        raise HTTPException(status_code=409, detail="原批次任务尚未对账完成，不能删除。")
    if schedule["status"] in {"waiting_for_gpu", "running", "pausing"}:
        raise HTTPException(status_code=409, detail="请先取消运行中的批次。")
    with BATCH_MUTATION_LOCK:
        _restore_schedule_projects(schedule)
        BATCH_STORE.delete(schedule_id)
    return {"ok": True, "id": schedule_id}


@app.post("/api/768-queue/schedules/{schedule_id}/start-now")
def start_batch_now(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if not any(item["status"] == "pending" for item in schedule["items"]):
        raise HTTPException(status_code=409, detail="批次中没有待运行项目。")
    if schedule["status"] in {"waiting_for_gpu", "running", "pausing"}:
        raise HTTPException(status_code=409, detail="批次已经运行。")
    schedule = BATCH_STORE.update(
        schedule_id,
        lambda item: item.update(
            status="scheduled",
            next_run_at=time.time(),
            detail="已请求立即开始",
            pause_requested=False,
            cancel_requested=False,
        ),
    )
    return _public_batch_schedule(schedule)


@app.post("/api/768-queue/schedules/{schedule_id}/pause")
def pause_batch_schedule(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)

    def mutate(item: dict) -> None:
        if item["status"] in {"running", "waiting_for_gpu"}:
            item["status"] = "pausing"
            item["pause_requested"] = True
            item["detail"] = "当前项目完成后暂停"
        elif item["status"] == "scheduled":
            item["status"] = "paused"
            item["pause_requested"] = False
            item["detail"] = "批次已暂停"
            item["next_run_at"] = None
        else:
            raise ValueError("当前批次不能暂停。")

    try:
        schedule = BATCH_STORE.update(schedule_id, mutate)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_batch_schedule(schedule)


@app.post("/api/768-queue/schedules/{schedule_id}/resume")
def resume_batch_schedule(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if schedule["status"] != "paused":
        raise HTTPException(status_code=409, detail="只有暂停的批次可以继续。")
    schedule = BATCH_STORE.update(
        schedule_id,
        lambda item: item.update(
            status="scheduled",
            next_run_at=time.time(),
            pause_requested=False,
            cancel_requested=False,
            detail="批次即将继续",
        ),
    )
    return _public_batch_schedule(schedule)


@app.post("/api/768-queue/schedules/{schedule_id}/cancel")
def cancel_batch_schedule(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if schedule["status"] in {"completed", "cancelled"}:
        return _public_batch_schedule(schedule)
    with BATCH_MUTATION_LOCK:
        schedule = BATCH_STORE.update(
            schedule_id,
            lambda item: item.update(
                status="cancelled",
                next_run_at=None,
                cancel_requested=True,
                pause_requested=False,
                detail="批次已取消",
                finished_at=time.time(),
            ),
        )
        current = _current_batch_item(schedule)
        if current:
            _set_stage(
                current["project_id"],
                "local_768",
                cancel_requested=True,
                detail="正在取消批次中的当前任务",
            )
        _cancel_pending_batch_items(schedule_id)
        schedule = _require_batch_schedule(schedule_id)
    return _public_batch_schedule(schedule)


@app.post("/api/768-queue/schedules/{schedule_id}/retry-failed")
def retry_failed_batch_items(schedule_id: str) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if schedule["status"] in {"waiting_for_gpu", "running", "pausing"}:
        raise HTTPException(status_code=409, detail="运行中的批次不能重试失败项目。")
    retried = 0

    def mutate(item: dict) -> None:
        nonlocal retried
        for queue_item in item["items"]:
            if queue_item["status"] != "failed":
                continue
            project = STORE.get(queue_item["project_id"])
            if not project:
                continue
            if project["stages"]["local_768"].get("fleet_pending"):
                continue
            eligible, _ = is_local_768_candidate(project)
            if not eligible and project["stages"]["local_768"]["status"] != "failed":
                continue
            queue_item.update(
                status="pending",
                error=None,
                started_at=None,
                finished_at=None,
            )
            _mark_project_scheduled(project, item, queue_item)
            retried += 1
        if retried:
            item["status"] = "scheduled"
            item["next_run_at"] = (
                next_daily_run(item["daily_time"], time.time())
                if item["kind"] == "daily"
                else time.time()
            )
            item["detail"] = f"已重新加入 {retried} 个失败项目"

    schedule = BATCH_STORE.update(schedule_id, mutate)
    if not retried:
        raise HTTPException(status_code=409, detail="没有可以重试的失败项目。")
    return _public_batch_schedule(schedule)


@app.post("/api/projects")
def create_project(
    name: str = Form("未命名项目"),
    mode: str = Form(...),
    strategy: str = Form("fast"),
    prompt: str = Form(...),
    duration: int = Form(15),
    orientation: str = Form("landscape"),
    prompt_processing: str = Form("cloud"),
    seed: int = Form(-1),
    audio_policy: str = Form("native"),
    watermark: bool = Form(False),
    use_embedded_video_audio: bool = Form(False),
    script_plan_id: str | None = Form(None),
    script_plan_revision: int | None = Form(None),
    first_frame: UploadFile | None = File(None),
    last_frame: UploadFile | None = File(None),
    reference_image: UploadFile | None = File(None),
    reference_video: UploadFile | None = File(None),
    reference_audio: UploadFile | None = File(None),
    recipe_id: str | None = Form(None),
) -> dict:
    script_source = None
    if script_plan_id is not None or script_plan_revision is not None:
        if not script_plan_id or script_plan_revision is None:
            raise HTTPException(400, "需要完整的脚本ID与批准版本。")
        script_source = SCRIPTS.approved_source(script_plan_id, script_plan_revision, prompt=prompt,
                                               duration=duration, mode=mode, audio_policy=audio_policy)
    project_id = uuid.uuid4().hex[:12]
    created_at = time.time()
    project_dir = SETTINGS.data_root / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    uploads = {
        "first_frame": first_frame,
        "last_frame": last_frame,
        "reference_image": reference_image,
        "reference_video": reference_video,
        "reference_audio": reference_audio,
    }
    assets: dict[str, dict] = {}
    try:
        for key, upload in uploads.items():
            if upload is None or not upload.filename:
                continue
            assets[key] = save_asset(
                upload,
                key=key,
                project_id=project_id,
                project_dir=project_dir,
                comfy_input=SETTINGS.comfy_input,
            )
        project = {
            "id": project_id,
            "name": name.strip() or "未命名项目",
            "mode": mode,
            "strategy": strategy,
            "duration": duration,
            "orientation": orientation,
            "prompt_processing": prompt_processing,
            "actual_duration": actual_duration(duration),
            "seed": seed if seed >= 0 else secrets.randbelow(2**63 - 1),
            "audio_policy": audio_policy,
            "watermark": watermark,
            "use_embedded_video_audio": use_embedded_video_audio,
            "prompt_original": prompt.strip(),
            "prompt_ir": "",
            "prompt_approved": "",
            "script_source": script_source,
            "assets": assets,
            "stages": {stage: _new_stage() for stage in STAGE_IDS},
            "created_at": created_at,
            "updated_at": created_at,
        }
        validate_project_config(project)
        if recipe_scope(project):
            project["recipe_id"] = require_recipe_id(recipe_id if isinstance(recipe_id, str) else "A4")
        elif isinstance(recipe_id, str) and recipe_id:
            raise ValueError("四配方仅支持15秒、480×864、24fps、原生音频文生视频预览。")
        STORE.save(project)
    except ValueError as error:
        _remove_project_files(project_dir)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception:
        _remove_project_files(project_dir)
        raise
    return _public_project(project)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    return _public_project(_require_project(project_id))


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    project = _require_project(project_id)
    if _has_running_stage(project):
        raise HTTPException(status_code=409, detail="任务运行时不能删除项目。")
    if _active_batch_for_project(project_id):
        raise HTTPException(status_code=409, detail="请先将项目从768P批次中移除。")
    for asset in project.get("assets", {}).values():
        comfy_name = str(asset.get("comfy_name", ""))
        if comfy_name.startswith(f"h3studio_{project_id}_"):
            (SETTINGS.comfy_input / comfy_name).unlink(missing_ok=True)
    _remove_project_files(_project_dir(project_id))
    STORE.delete(project_id)
    return {"ok": True, "id": project_id}


@app.put("/api/projects/{project_id}/prompt")
def update_prompt(project_id: str, payload: dict = Body(...)) -> dict:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空。")

    def mutate(project: dict) -> None:
        if _has_running_stage(project):
            raise ValueError("任务运行时不能修改提示词。")
        if _active_batch_for_project(project_id):
            raise ValueError("项目已加入768P批次，不能修改提示词。")
        project["prompt_original"] = prompt
        project["prompt_ir"] = ""
        project["prompt_approved"] = ""
        project["stages"] = {stage: _new_stage() for stage in STAGE_IDS}

    try:
        project = STORE.update(project_id, mutate)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="项目不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_project(project)


@app.post("/api/projects/{project_id}/context-ir")
def start_context_ir(project_id: str) -> dict:
    project = _require_project(project_id)
    if _active_batch_for_project(project_id):
        raise HTTPException(status_code=409, detail="项目已加入768P批次。")
    stage = project["stages"]["context_ir"]
    if stage["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Context IR 已在运行。")
    def mutate(item: dict) -> None:
        if _has_running_stage(item):
            raise ValueError("已有任务正在运行。")
        item["prompt_ir"] = ""
        item["prompt_approved"] = ""
        item["stages"] = {stage_id: _new_stage() for stage_id in STAGE_IDS}
        item["stages"]["context_ir"].update(
            status="queued",
            progress=1,
            detail="等待提交 H3 Context IR",
            queued_at=time.time(),
        )

    try:
        STORE.update(project_id, mutate)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _spawn(_run_context_ir, project_id)
    return _public_project(_require_project(project_id))


@app.post("/api/projects/{project_id}/context-ir/approve")
def approve_context_ir(project_id: str, payload: dict = Body(...)) -> dict:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="优化提示词不能为空。")

    def mutate(project: dict) -> None:
        stage = project["stages"]["context_ir"]
        if stage["status"] != "awaiting_approval":
            raise ValueError("Context IR 当前不能确认。")
        project["prompt_approved"] = prompt
        stage.update(
            status="approved",
            progress=100,
            detail="优化提示词已确认",
            approved_at=time.time(),
        )
        _reset_downstream(project, keep_context=True)

    try:
        project = STORE.update(project_id, mutate)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="项目不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_project(project)


@app.post("/api/projects/{project_id}/stages/{stage_id}/start")
def start_stage(project_id: str, stage_id: str, payload: dict | None = Body(None)) -> dict:
    if stage_id not in STAGE_IDS - {"context_ir"}:
        raise HTTPException(status_code=404, detail="未知阶段。")
    project = _require_project(project_id)
    if stage_id not in {item["id"] for item in pipeline_for(project)}:
        raise HTTPException(status_code=409, detail="当前策略不包含这个阶段。")
    if not _stage_unlocked(project, stage_id):
        raise HTTPException(status_code=409, detail="请先确认前一个阶段。")
    stage = project["stages"][stage_id]
    target_node = (payload or {}).get("target_node", stage.get("target_node", "auto"))
    if getattr(COMFY, "is_multifleet", False):
        try:
            COMFY.validate_target(target_node)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if stage.get("fleet_pending") and target_node != stage.get("target_node", "auto"):
            raise HTTPException(409, "对账期间不能更换执行设备。")
    elif target_node != "auto":
        raise HTTPException(400, "当前未配置多节点执行。")
    if stage["status"] in {"queued", "running", "scheduled"}:
        raise HTTPException(status_code=409, detail="该阶段已在运行或已加入批次。")
    selected_recipe = None
    if recipe_scope(project, stage_id):
        if stage.get("fleet_pending"):
            if payload and "recipe_id" in payload and payload["recipe_id"] != project.get("recipe_id"):
                raise HTTPException(409, "对账期间不能更换配方。")
        else:
            try:
                selected_recipe = require_recipe_id((payload or {}).get("recipe_id", project.get("recipe_id")))
                confirmed_recipe(recipe_catalog(), selected_recipe)
            except (ValueError, RuntimeError) as error:
                raise HTTPException(409, str(error)) from error
    elif payload and payload.get("recipe_id") is not None:
        raise HTTPException(400, "此阶段不支持四配方。")
    manual_reserved = False
    if stage_id in LOCAL_STAGES:
        if _active_batch_for_project(project_id):
            raise HTTPException(status_code=409, detail="项目已加入768P批次。")
        try:
            if not getattr(COMFY, "is_multifleet", False):
                GPU_GATE.reserve_manual()
                manual_reserved = True
        except BatchInfrastructureError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def queue_stage(item: dict) -> None:
        current = item["stages"][stage_id]
        if current["status"] in {"queued", "running", "scheduled"}:
            raise HTTPException(status_code=409, detail="原阶段仍在执行或等待对账，不能重复提交。")
        if any(other["status"] in {"queued", "running", "scheduled"} or other.get("fleet_pending")
               for name, other in item["stages"].items() if name != stage_id):
            raise HTTPException(status_code=409, detail="项目中仍有其他阶段未完成，不能重置原任务。")
        if current.get("fleet_pending"):
            if payload and payload.get("new_seed"):
                raise HTTPException(status_code=409, detail="对账期间不能修改 Seed。")
            current.update(status="queued", detail="继续查询原执行ID，不重新生成", error=None)
            return
        if not _stage_unlocked(item, stage_id):
            raise HTTPException(status_code=409, detail="前置阶段已变化，请刷新后确认。")
        if selected_recipe:
            if current.get("artifact") or current.get("execution_id"):
                item.setdefault("stage_history", []).append({
                    **json.loads(json.dumps(current)), "id": uuid.uuid4().hex, "stage_id": stage_id,
                    "recipe_id": item.get("recipe_id"), "seed": item["seed"],
                })
            item["recipe_id"] = selected_recipe
        if payload and payload.get("new_seed"):
            item["seed"] = secrets.randbelow(2**63 - 1)
        pipeline_ids = [stage["id"] for stage in pipeline_for(item)]
        stage_index = pipeline_ids.index(stage_id)
        for downstream in pipeline_ids[stage_index + 1 :]:
            item["stages"][downstream] = _new_stage()
        item["stages"][stage_id] = {
            **_new_stage(),
            "target_node": target_node,
            "status": "queued",
            "progress": 1,
            "detail": "任务已进入队列",
            "queued_at": time.time(),
        }

    try:
        STORE.update(project_id, queue_stage)
    except Exception:
        if manual_reserved:
            GPU_GATE.release_manual()
        raise
    _spawn(_run_stage, project_id, stage_id, manual_reserved)
    return _public_project(_require_project(project_id))


@app.post("/api/projects/{project_id}/stages/{stage_id}/approve")
def approve_stage(project_id: str, stage_id: str) -> dict:
    if stage_id not in STAGE_IDS - {"context_ir"}:
        raise HTTPException(status_code=404, detail="未知阶段。")

    def mutate(project: dict) -> None:
        stage = project["stages"][stage_id]
        if stage["status"] != "awaiting_approval":
            raise ValueError("该阶段当前不能确认。")
        stage.update(
            status="approved",
            progress=100,
            detail="结果已确认",
            approved_at=time.time(),
        )

    try:
        project = STORE.update(project_id, mutate)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="项目不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_project(project)


@app.post("/api/projects/{project_id}/stages/{stage_id}/cancel")
def cancel_stage(project_id: str, stage_id: str) -> dict:
    if stage_id not in STAGE_IDS:
        raise HTTPException(status_code=404, detail="未知阶段。")
    project = _require_project(project_id)
    if project["stages"][stage_id].get("batch_schedule_id"):
        raise HTTPException(
            status_code=409,
            detail="该任务由768P批次管理，请在批次页面暂停或取消。",
        )

    def mutate(project: dict) -> None:
        stage = project["stages"][stage_id]
        if stage["status"] not in {"queued", "running"}:
            raise ValueError("该阶段当前不能取消。")
        stage["cancel_requested"] = True
        stage["detail"] = "正在取消"

    try:
        project = STORE.update(project_id, mutate)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="项目不存在。") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _public_project(project)


@app.get("/api/projects/{project_id}/artifacts/{stage_id}")
def get_artifact(project_id: str, stage_id: str) -> FileResponse:
    project = _require_project(project_id)
    stage = project["stages"].get(stage_id)
    if not stage or not stage.get("artifact"):
        raise HTTPException(status_code=404, detail="视频尚未生成。")
    path = Path(stage["artifact"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="视频文件不存在。")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{project_id}-{stage_id}.mp4",
    )


def _run_context_ir(project_id: str) -> None:
    _set_stage(
        project_id,
        "context_ir",
        status="running",
        progress=3,
        detail="正在上传提示词和参考素材",
        started_at=time.time(),
    )
    try:
        project = _require_project(project_id)
        manual = project.get("prompt_processing") == "manual"
        if manual:
            result = {"prompt": project["prompt_original"]}
        else:
            result = MINIMAX.context_ir(
                project,
                progress=lambda values: _set_stage(project_id, "context_ir", **values),
                cancelled=lambda: _cancelled(project_id, "context_ir"),
            )

        def mutate(item: dict) -> None:
            item["prompt_ir"] = result["prompt"]
            item["stages"]["context_ir"].update(
                status="awaiting_approval",
                progress=100,
                detail="原文已保留，请确认；未调用 Context IR" if manual else "优化完成，请确认提示词",
                prompt_processing="manual" if manual else "cloud",
                remote_task_id=result.get("task_id"),
                usage=result.get("usage"),
                finished_at=time.time(),
                error=None,
            )

        STORE.update(project_id, mutate)
    except Exception as error:
        _fail_stage(project_id, "context_ir", error)


def _run_stage(
    project_id: str,
    stage_id: str,
    manual_reserved: bool = False,
) -> None:
    lock = LOCAL_GPU_LOCK if stage_id in LOCAL_STAGES and not getattr(COMFY, "is_fleet", False) else _NullLock()
    try:
        with lock:
            stage = _require_project(project_id)["stages"][stage_id]
            now = time.time()
            previous_start = stage.get("started_at")
            recovering = (stage_id in LOCAL_STAGES and getattr(COMFY, "is_fleet", False)
                          and stage.get("fleet_pending") is True
                          and isinstance(stage.get("execution_id"), str) and bool(stage["execution_id"])
                          and type(previous_start) in (int, float) and math.isfinite(previous_start)
                          and 0 < previous_start <= now)
            _set_stage(
                project_id,
                stage_id,
                status="running",
                progress=3,
                detail="正在准备任务",
                started_at=previous_start if recovering else now,
            )
            if stage_id in LOCAL_STAGES:
                _run_local_stage(project_id, stage_id)
            elif stage_id == "cloud_768":
                _run_cloud_768(project_id)
            elif stage_id == "regenerate_2k":
                _run_2k(project_id)
            else:
                raise ValueError(f"未知阶段：{stage_id}")
    except Exception as error:
        _fail_stage(project_id, stage_id, error)
    finally:
        if manual_reserved:
            GPU_GATE.release_manual()


def _run_local_stage(project_id: str, stage_id: str) -> None:
    if getattr(COMFY, "is_fleet", False):
        _run_fleet_stage(project_id, stage_id)
        return
    project = _require_project(project_id)
    artifacts = _project_dir(project_id) / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    workflow, template = build_workflow(project, stage_id, SETTINGS.workflow_root)
    workflow_path = artifacts / f"{stage_id}-workflow.json"
    workflow_path.write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output = artifacts / f"{stage_id}.mp4"
    COMFY.free()
    try:
        prompt_id = COMFY.submit(workflow)
        _set_stage(
            project_id,
            stage_id,
            prompt_id=prompt_id,
            workflow_template=template,
            workflow_path=str(workflow_path),
            detail="ComfyUI 已开始执行",
        )
        expected_key = (
            "preview_turbo"
            if stage_id == "preview" and uses_turbo_preview(project)
            else "preview_quality"
            if stage_id == "preview"
            else stage_id
        )
        result = COMFY.wait_for_output(
            prompt_id,
            output,
            progress=lambda values: _set_stage(project_id, stage_id, **values),
            cancelled=lambda: _cancelled(project_id, stage_id),
            expected_seconds=EXPECTED_SECONDS[expected_key],
        )
    finally:
        COMFY.free()
    _complete_stage(project_id, stage_id, output, result)


def _run_fleet_stage(project_id: str, stage_id: str) -> None:
    if getattr(COMFY, "is_multifleet", False):
        with COMFY.execution_scope(sys.modules[__name__], project_id, stage_id):
            return _run_bound_fleet_stage(project_id, stage_id)
    return _run_bound_fleet_stage(project_id, stage_id)


def _run_bound_fleet_stage(project_id: str, stage_id: str) -> None:
    project = _require_project(project_id)
    stage = project["stages"][stage_id]
    execution_id = stage.get("execution_id")
    output = _project_dir(project_id) / "artifacts" / f"{stage_id}.mp4"
    if stage.get("output_path"):
        output = Path(stage["output_path"])
    if not execution_id or stage.get("fleet_prepared"):
        COMFY.upload_assets(project["assets"])
        recipe = None
        if recipe_scope(project, stage_id):
            recipe = {"recipe_id": require_recipe_id(project.get("recipe_id")),
                      "prompt": project["prompt_approved"], "seed": project["seed"],
                      "filename_prefix": f"video/h3-video-studio/{project_id}/{stage_id}"}
            workflow, template = {}, "fleet-recipe:" + recipe["recipe_id"]
        else:
            workflow, template = build_workflow(project, stage_id, SETTINGS.workflow_root)
        prefix = "studio_batch_" + stage["batch_schedule_id"] if stage.get("batch_schedule_id") else "studio"
        execution_id = execution_id or prefix + "_" + project_id + "_" + stage_id + "_" + uuid.uuid4().hex
        if recipe:
            output = output.with_name(execution_id + ".mp4")
        _set_stage(project_id, stage_id, execution_id=execution_id, fleet_pending=True, workflow_template=template)
        profile = "preview" if stage_id == "preview" and uses_turbo_preview(project) else "quality"
        try:
            def prepared(graph, binding):
                output.parent.mkdir(parents=True, exist_ok=True)
                workflow_path = output.with_suffix(".json")
                workflow_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
                _set_stage(project_id, stage_id, workflow_path=str(workflow_path), output_path=str(output),
                           execution={"contract": binding, "recipe_id": binding["recipe_id"],
                                      "recipe_version": binding["recipe_version"]})

            options = {"recipe": recipe, "prepared": prepared} if recipe else {}
            if _cancelled(project_id, stage_id):
                raise RuntimeError("任务在提交前已取消。")
            _set_stage(project_id, stage_id, fleet_prepared=False)
            prompt_id = COMFY.submit_stage(workflow, execution_id, stage_id, profile, **options)
            _set_stage(project_id, stage_id, prompt_id=prompt_id)
        except SubmissionUnknown as error:
            _set_stage(project_id, stage_id, submission_unknown=True, detail=str(error))
        except RuntimeError:
            _set_stage(project_id, stage_id, fleet_pending=False)
            raise
    try:
        result = COMFY.wait_execution(execution_id, output,
            progress=lambda values: _set_stage(project_id, stage_id, **values),
            cancelled=lambda: _cancelled(project_id, stage_id))
    except SubmissionUnknown as error:
        _set_stage(project_id, stage_id, submission_unknown=True, detail=str(error))
        raise
    except RuntimeError:
        _set_stage(project_id, stage_id, fleet_pending=False)
        raise
    _set_stage(project_id, stage_id, fleet_pending=False, submission_unknown=False)
    _complete_stage(project_id, stage_id, output, result)


def _run_cloud_768(project_id: str) -> None:
    project = _require_project(project_id)
    output = _project_dir(project_id) / "artifacts" / "cloud_768.mp4"
    result = MINIMAX.generate_768(
        project,
        output,
        progress=lambda values: _set_stage(project_id, "cloud_768", **values),
        cancelled=lambda: _cancelled(project_id, "cloud_768"),
    )
    _complete_stage(project_id, "cloud_768", output, result)


def _run_2k(project_id: str) -> None:
    project = _require_project(project_id)
    source_stage = "cloud_768" if project["strategy"] == "cloud" else "local_768"
    source = Path(project["stages"][source_stage]["artifact"])
    source_spec = validate_2k_source(source)
    _set_stage(
        project_id,
        "regenerate_2k",
        source_spec=source_spec,
        detail="768P 规格校验通过，正在提交官方 2K",
    )
    output = _project_dir(project_id) / "artifacts" / "regenerate_2k.mp4"
    result = MINIMAX.regenerate_2k(
        project,
        source,
        output,
        progress=lambda values: _set_stage(project_id, "regenerate_2k", **values),
        cancelled=lambda: _cancelled(project_id, "regenerate_2k"),
    )
    _complete_stage(project_id, "regenerate_2k", output, result)


def _complete_stage(project_id: str, stage_id: str, output: Path, result: dict) -> None:
    _set_stage(
        project_id,
        stage_id,
        status="awaiting_approval",
        progress=100,
        detail="生成完成，请检查结果",
        artifact=str(output),
        artifact_bytes=output.stat().st_size,
        finished_at=time.time(),
        error=None,
        **result,
    )


def _fail_stage(project_id: str, stage_id: str, error: Exception) -> None:
    if _require_project(project_id)["stages"][stage_id].get("fleet_pending"):
        _set_stage(project_id, stage_id, status="failed", progress=1,
                   detail="结果未知，请继续对账；未重新生成", error=str(error), submission_unknown=True)
        return
    cancelled = _cancelled(project_id, stage_id)
    _set_stage(
        project_id,
        stage_id,
        status="cancelled" if cancelled else "failed",
        progress=100,
        detail="任务已取消" if cancelled else "任务失败",
        error=None if cancelled else str(error),
        finished_at=time.time(),
    )


def _stage_unlocked(project: dict, stage_id: str) -> bool:
    if project["stages"]["context_ir"]["status"] != "approved":
        return False
    pipeline = [item["id"] for item in pipeline_for(project)]
    index = pipeline.index(stage_id)
    previous = pipeline[index - 1]
    return project["stages"][previous]["status"] == "approved"


def _reset_downstream(project: dict, *, keep_context: bool) -> None:
    context = project["stages"]["context_ir"] if keep_context else _new_stage()
    project["stages"] = {stage: _new_stage() for stage in STAGE_IDS}
    project["stages"]["context_ir"] = context


def _unique_project_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        project_id = str(item).strip()
        if project_id and project_id not in result:
            result.append(project_id)
    return result


def _require_batch_schedule(schedule_id: str) -> dict:
    schedule = BATCH_STORE.get(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="768P批次不存在。")
    return schedule


def _active_batch_for_project(project_id: str) -> dict | None:
    for schedule in BATCH_STORE.list(limit=500):
        if schedule["status"] not in ACTIVE_SCHEDULE_STATUSES:
            continue
        for item in schedule["items"]:
            if (
                item["project_id"] == project_id
                and item["status"] not in {"completed", "cancelled", "skipped"}
            ):
                return schedule
    return None


def _replace_schedule_projects(schedule_id: str, project_ids: list[str]) -> dict:
    schedule = _require_batch_schedule(schedule_id)
    if any(item["status"] == "running" for item in schedule["items"]):
        raise HTTPException(status_code=409, detail="原批次任务尚未对账完成，不能更换项目。")
    existing = {
        item["project_id"]: item
        for item in schedule["items"]
        if item["status"] != "completed"
    }
    next_items: list[dict] = []
    new_items: list[tuple[dict, dict]] = []
    for position, project_id in enumerate(project_ids):
        if project_id in existing:
            item = existing[project_id]
            item["position"] = position
            next_items.append(item)
            continue
        project = STORE.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")
        active = _active_batch_for_project(project_id)
        if active and active["id"] != schedule_id:
            raise HTTPException(
                status_code=409,
                detail=f"项目“{project['name']}”已在其他批次中。",
            )
        eligible, reason = is_local_768_candidate(project)
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=f"项目“{project['name']}”不能加入：{reason}",
            )
        profile = runtime_profile(project, "local_768")
        item = {
            "id": uuid.uuid4().hex[:12],
            "project_id": project_id,
            "position": position,
            "status": "pending",
            "previous_stage": json.loads(
                json.dumps(project["stages"]["local_768"])
            ),
            "estimate_low_seconds": profile["low_seconds"],
            "estimate_high_seconds": profile["high_seconds"],
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        new_items.append((project, item))
        next_items.append(item)

    for item in schedule["items"]:
        if item["project_id"] not in project_ids and item["status"] != "completed":
            _restore_project_from_batch_item(schedule, item)
    for project, item in new_items:
        _mark_project_scheduled(project, schedule, item)

    schedule["items"] = next_items
    schedule["status"] = "scheduled" if next_items else "waiting_for_items"
    schedule["detail"] = (
        f"已选择 {len(next_items)} 个项目"
        if next_items
        else "等待加入项目"
    )
    BATCH_STORE.save(schedule)
    return schedule


def _mark_project_scheduled(project: dict, schedule: dict, item: dict) -> None:
    stage = project["stages"]["local_768"]
    stage.update(
        status="scheduled",
        progress=0,
        detail=f"已加入768P批次：{schedule['name']}",
        error=None,
        cancel_requested=False,
        queued_at=None,
        started_at=None,
        finished_at=None,
        batch_schedule_id=schedule["id"],
        batch_item_id=item["id"],
        scheduled_for=schedule.get("next_run_at"),
    )
    STORE.save(project)


def _restore_project_from_batch_item(schedule: dict, item: dict) -> None:
    project = STORE.get(item["project_id"])
    if not project:
        return
    stage = project["stages"]["local_768"]
    if stage.get("fleet_pending"):
        raise HTTPException(status_code=409, detail="原执行ID尚未对账，不能恢复旧阶段。")
    if stage.get("batch_schedule_id") != schedule["id"]:
        return
    project["stages"]["local_768"] = json.loads(
        json.dumps(item.get("previous_stage") or _new_stage())
    )
    STORE.save(project)


def _restore_schedule_projects(schedule: dict) -> None:
    for item in schedule.get("items", []):
        if item["status"] in {"pending", "failed", "skipped", "cancelled"}:
            _restore_project_from_batch_item(schedule, item)


def _refresh_scheduled_project_times(schedule: dict) -> None:
    for item in schedule["items"]:
        if item["status"] != "pending":
            continue
        project = STORE.get(item["project_id"])
        if not project:
            continue
        stage = project["stages"]["local_768"]
        if stage.get("batch_schedule_id") == schedule["id"]:
            stage["scheduled_for"] = schedule.get("next_run_at")
            stage["detail"] = f"已加入768P批次：{schedule['name']}"
            STORE.save(project)


def _current_batch_item(schedule: dict) -> dict | None:
    current_id = schedule.get("current_item_id")
    return next(
        (item for item in schedule["items"] if item["id"] == current_id or item["status"] == "running"),
        None,
    )


def _cancel_pending_batch_items(schedule_id: str) -> None:
    schedule = _require_batch_schedule(schedule_id)
    for item in schedule["items"]:
        if item["status"] != "pending":
            continue
        _restore_project_from_batch_item(schedule, item)
        item.update(
            status="cancelled",
            finished_at=time.time(),
            error=None,
        )
    BATCH_STORE.save(schedule)


def _public_batch_schedule(schedule: dict) -> dict:
    public = json.loads(json.dumps(schedule))
    projects = []
    low = 0
    high = 0
    completed = failed = 0
    for item in public["items"]:
        project = STORE.get(item["project_id"])
        item["project_name"] = project["name"] if project else "项目已删除"
        item["mode"] = project["mode"] if project else None
        item["duration"] = project.get("actual_duration") if project else None
        if project:
            stage = project["stages"]["local_768"]
            item["stage_status"] = stage["status"]
            item["stage_progress"] = stage.get("progress", 0)
            item["stage_detail"] = stage.get("detail")
            item["artifact_url"] = (
                f"/api/projects/{project['id']}/artifacts/local_768"
                if stage.get("artifact")
                else None
            )
        if item["status"] in ACTIVE_ITEM_STATUSES:
            low += int(item["estimate_low_seconds"])
            high += int(item["estimate_high_seconds"])
            projects.append(project)
        completed += item["status"] == "completed"
        failed += item["status"] == "failed"
    public["summary"] = {
        "count": len(public["items"]),
        "pending_count": sum(
            item["status"] == "pending" for item in public["items"]
        ),
        "running_count": sum(
            item["status"] == "running" for item in public["items"]
        ),
        "completed_count": completed,
        "failed_count": failed,
        "low_seconds": low,
        "high_seconds": high,
        "expected_finish_low": (
            public["next_run_at"] + low
            if public.get("next_run_at") is not None
            else None
        ),
        "expected_finish_high": (
            public["next_run_at"] + high
            if public.get("next_run_at") is not None
            else None
        ),
    }
    return public


def _recover_batch_schedules() -> None:
    now = time.time()
    for schedule in BATCH_STORE.list(limit=500):
        if schedule["status"] in {"waiting_for_gpu", "running", "pausing"}:
            schedule.update(
                status="scheduled",
                next_run_at=now,
                detail="服务重启，正在恢复未完成批次",
                pause_requested=False,
            )
        elif schedule["status"] == "scheduled":
            apply_missed_schedule_policy(schedule, now)
        BATCH_STORE.save(schedule)


def _batch_scheduler_loop() -> None:
    while not BATCH_SCHEDULER_STOP.is_set():
        try:
            if not BATCH_RUN_ACTIVE.is_set():
                due = BATCH_STORE.due(time.time())
                if due:
                    schedule_id = due[0]["id"]

                    def reserve(item: dict) -> None:
                        if item["status"] != "scheduled":
                            raise ValueError("批次状态已经变化。")
                        if not any(
                            queue_item["status"] in {"pending", "running"}
                            for queue_item in item["items"]
                        ):
                            item["status"] = "waiting_for_items"
                            item["detail"] = "计划到点，但没有待运行项目"
                            if item["kind"] == "daily":
                                item["next_run_at"] = next_daily_run(
                                    item["daily_time"],
                                    time.time(),
                                    include_today=False,
                                )
                            return
                        item.update(
                            status="waiting_for_gpu",
                            detail="等待本地GPU空闲",
                            scheduled_for=item.get("next_run_at"),
                        )

                    try:
                        reserved = BATCH_STORE.update(schedule_id, reserve)
                    except (KeyError, ValueError):
                        reserved = None
                    if reserved and reserved["status"] == "waiting_for_gpu":
                        BATCH_RUN_ACTIVE.set()
                        _spawn(_run_batch_schedule, schedule_id)
        except Exception:
            pass
        BATCH_SCHEDULER_STOP.wait(5)


def _run_batch_schedule(schedule_id: str) -> None:
    try:
        with BATCH_EXECUTION_LOCK:
            try:
                if not getattr(COMFY, "is_multifleet", False):
                    GPU_GATE.reserve_batch(schedule_id)
            except Exception as error:
                _pause_batch_for_infrastructure(schedule_id, error)
                return
            try:
                schedule = BATCH_STORE.get(schedule_id)
                if not schedule or schedule["status"] in {"paused", "cancelled"}:
                    return
                if getattr(COMFY, "is_fleet", False):
                    try:
                        _begin_fleet_batch(schedule_id)
                    except Exception as error:
                        _pause_batch_for_infrastructure(schedule_id, error)
                        return
                if schedule["status"] == "pausing":
                    BATCH_STORE.update(
                        schedule_id,
                        lambda item: item.update(
                            status="paused",
                            detail="批次已暂停",
                            next_run_at=None,
                        ),
                    )
                    return
                run_id = (
                    schedule.get("current_run", {}).get("id")
                    or uuid.uuid4().hex[:12]
                )

                def begin(item: dict) -> None:
                    item.update(
                        status="running",
                        detail="768P批次正在运行",
                        started_at=item.get("started_at") or time.time(),
                        current_run=item.get("current_run")
                        or {
                            "id": run_id,
                            "scheduled_for": item.get("scheduled_for")
                            or item.get("next_run_at"),
                            "started_at": time.time(),
                            "items": [],
                        },
                    )

                BATCH_STORE.update(schedule_id, begin)
                with LOCAL_GPU_LOCK:
                    while True:
                        schedule = _require_batch_schedule(schedule_id)
                        if schedule["status"] == "cancelled":
                            break
                        if (
                            schedule.get("pause_requested")
                            or schedule["status"] == "pausing"
                        ):
                            BATCH_STORE.update(
                                schedule_id,
                                lambda item: item.update(
                                    status="paused",
                                    detail="批次已暂停，可稍后继续",
                                    next_run_at=None,
                                    pause_requested=False,
                                    current_item_id=None,
                                ),
                            )
                            break
                        item = next(
                            (
                                queue_item
                                for queue_item in sorted(
                                    schedule["items"],
                                    key=lambda value: value["position"],
                                )
                                if queue_item["status"] in {"running", "pending"}
                            ),
                            None,
                        )
                        if item is None:
                            _finish_batch_schedule(schedule_id)
                            break
                        try:
                            _execute_batch_item(schedule_id, item["id"])
                        except Exception as error:
                            schedule = _require_batch_schedule(schedule_id)
                            project = STORE.get(item["project_id"])
                            if project and project["stages"]["local_768"].get("fleet_pending"):
                                _fail_stage(item["project_id"], "local_768", error)
                                _pause_batch_for_infrastructure(schedule_id, error)
                                break
                            if schedule.get("cancel_requested"):
                                _fail_stage(
                                    item["project_id"],
                                    "local_768",
                                    RuntimeError("任务已取消。"),
                                )
                                _mark_batch_item(
                                    schedule_id,
                                    item["id"],
                                    status="cancelled",
                                    error=None,
                                    finished_at=time.time(),
                                )
                                break
                            category = classify_batch_error(error)
                            item_status = (
                                "skipped"
                                if isinstance(error, BatchItemError)
                                else "failed"
                            )
                            if item_status == "skipped":
                                _restore_project_from_batch_item(schedule, item)
                            else:
                                _fail_stage(
                                    item["project_id"],
                                    "local_768",
                                    error,
                                )
                            _mark_batch_item(
                                schedule_id,
                                item["id"],
                                status=item_status,
                                error=str(error),
                                finished_at=time.time(),
                            )
                            if category == "infrastructure":
                                _pause_batch_for_infrastructure(
                                    schedule_id,
                                    error,
                                )
                                break
            finally:
                if getattr(COMFY, "is_fleet", False):
                    try:
                        COMFY.end_batch(schedule_id)
                    except Exception as error:
                        _pause_batch_for_infrastructure(schedule_id, error)
                if not getattr(COMFY, "is_multifleet", False):
                    GPU_GATE.release_batch(schedule_id)
    finally:
        BATCH_RUN_ACTIVE.clear()


def _begin_fleet_batch(schedule_id: str) -> None:
    if not getattr(COMFY, "is_multifleet", False):
        COMFY.begin_batch(schedule_id)
        return
    schedule = _require_batch_schedule(schedule_id)
    recovering = []
    for item in schedule["items"]:
        if item.get("status") != "running":
            continue
        project = STORE.get(item["project_id"])
        stage = (project or {}).get("stages", {}).get("local_768", {})
        if stage.get("execution_id"):
            recovering.append(stage)
    COMFY.begin_batch(schedule_id, recovery_stages=recovering)


def _execute_batch_item(schedule_id: str, item_id: str) -> None:
    if getattr(COMFY, "is_fleet", False):
        _begin_fleet_batch(schedule_id)
    schedule = _require_batch_schedule(schedule_id)
    item = next(entry for entry in schedule["items"] if entry["id"] == item_id)
    project = STORE.get(item["project_id"])
    if project is None:
        raise BatchItemError("项目已被删除。")
    stage = project["stages"]["local_768"]

    if item["status"] == "running" and stage.get("execution_id") and getattr(COMFY, "is_fleet", False):
        _run_fleet_stage(project["id"], "local_768")
        _mark_batch_item(schedule_id, item_id, status="completed", finished_at=time.time(), error=None)
        return
    if item["status"] == "running" and stage.get("prompt_id"):
        prompt_state = COMFY.prompt_state(stage["prompt_id"])
        if prompt_state in {"active", "completed"}:
            output = _project_dir(project["id"]) / "artifacts" / "local_768.mp4"
            result = COMFY.wait_for_output(
                stage["prompt_id"],
                output,
                progress=lambda values: _set_stage(
                    project["id"],
                    "local_768",
                    **values,
                ),
                cancelled=lambda: _cancelled(project["id"], "local_768"),
                expected_seconds=EXPECTED_SECONDS["local_768"],
            )
            _complete_stage(project["id"], "local_768", output, result)
            _mark_batch_item(
                schedule_id,
                item_id,
                status="completed",
                finished_at=time.time(),
                error=None,
            )
            return
        raise BatchInfrastructureError(
            "服务重启后无法在ComfyUI中找到原768P任务，批次已暂停。"
        )

    _validate_batch_item(project, schedule, item)
    check_disk_space(SETTINGS.data_root)
    try:
        COMFY.health()
    except Exception as error:
        raise BatchInfrastructureError(f"ComfyUI 离线：{error}") from error

    _mark_batch_item(
        schedule_id,
        item_id,
        status="running",
        started_at=time.time(),
        error=None,
    )

    def start_project_stage(value: dict) -> None:
        current = value["stages"]["local_768"]
        value["stages"]["local_768"] = {
            **_new_stage(),
            "status": "running",
            "progress": 3,
            "detail": "夜间批次正在生成本地768P",
            "queued_at": time.time(),
            "started_at": time.time(),
            "batch_schedule_id": schedule_id,
            "batch_item_id": item_id,
        }
        if current.get("batch_schedule_id") != schedule_id:
            raise ValueError("项目不再属于当前768P批次。")

    STORE.update(project["id"], start_project_stage)
    _set_batch_current_item(schedule_id, item_id)
    _run_local_stage(project["id"], "local_768")
    _mark_batch_item(
        schedule_id,
        item_id,
        status="completed",
        finished_at=time.time(),
        error=None,
    )


def _validate_batch_item(project: dict, schedule: dict, item: dict) -> None:
    pipeline = [stage["id"] for stage in pipeline_for(project)]
    if "local_768" not in pipeline:
        raise BatchItemError("项目已经切换为云端768P策略。")
    previous = pipeline[pipeline.index("local_768") - 1]
    if project["stages"][previous]["status"] != "approved":
        raise BatchItemError("本地768P的前置低清阶段不再是已确认状态。")
    stage = project["stages"]["local_768"]
    if stage.get("batch_schedule_id") != schedule["id"]:
        raise BatchItemError("项目已经从当前批次移除。")
    if not project.get("prompt_approved"):
        raise BatchItemError("优化提示词已失效。")


def _set_batch_current_item(schedule_id: str, item_id: str) -> None:
    BATCH_STORE.update(
        schedule_id,
        lambda item: item.update(
            current_item_id=item_id,
            detail="正在生成当前768P项目",
        ),
    )


def _mark_batch_item(schedule_id: str, item_id: str, **values: object) -> None:
    project_id: str | None = None

    def mutate(schedule: dict) -> None:
        nonlocal project_id
        item = next(
            entry for entry in schedule["items"] if entry["id"] == item_id
        )
        project_id = item["project_id"]
        item.update(values)
        if values.get("status") != "running":
            schedule["current_item_id"] = None

    BATCH_STORE.update(schedule_id, mutate)
    if values.get("status") in {"failed", "skipped", "cancelled"} and project_id:
        project = STORE.get(project_id)
        if project:
            stage = project["stages"]["local_768"]
            if stage.get("batch_schedule_id") == schedule_id:
                stage.pop("batch_schedule_id", None)
                stage.pop("batch_item_id", None)
                stage.pop("scheduled_for", None)
                STORE.save(project)


def _pause_batch_for_infrastructure(schedule_id: str, error: Exception) -> None:
    try:
        BATCH_STORE.update(
            schedule_id,
            lambda item: item.update(
                status="paused",
                next_run_at=None,
                pause_requested=False,
                current_item_id=None,
                detail="设备或服务异常，批次已暂停",
                error=str(error),
            ),
        )
    except KeyError:
        pass


def _finish_batch_schedule(schedule_id: str) -> None:
    now = time.time()

    def mutate(schedule: dict) -> None:
        run = schedule.get("current_run") or {
            "id": uuid.uuid4().hex[:12],
            "started_at": schedule.get("started_at"),
            "items": [],
        }
        run.update(
            finished_at=now,
            status="completed",
            items=json.loads(json.dumps(schedule["items"])),
        )
        schedule["runs"] = (schedule.get("runs") or [])[-19:] + [run]
        schedule["current_run"] = None
        schedule["current_item_id"] = None
        schedule["finished_at"] = now
        schedule["error"] = None
        schedule["cancel_requested"] = False
        schedule["last_run_date"] = datetime.fromtimestamp(
            now,
            TIMEZONE,
        ).date().isoformat()
        if schedule["kind"] == "once":
            schedule.update(
                status="completed",
                next_run_at=None,
                detail="全部768P项目已完成，等待人工检查",
            )
        else:
            schedule["items"] = [
                item
                for item in schedule["items"]
                if item["status"] == "failed"
            ]
            schedule.update(
                status="waiting_for_items",
                next_run_at=next_daily_run(
                    schedule["daily_time"],
                    now,
                    include_today=False,
                ),
                detail="今日批次完成，等待加入下一批项目",
            )

    BATCH_STORE.update(schedule_id, mutate)


def _public_project(project: dict) -> dict:
    public = json.loads(json.dumps(project))
    for stage_id, stage in public["stages"].items():
        if stage.get("artifact"):
            stage["artifact_url"] = f"/api/projects/{project['id']}/artifacts/{stage_id}"
        stage.pop("cancel_requested", None)
    public["pipeline"] = pipeline_for(public)
    public["runtime_summary"] = project_runtime_summary(public)
    for previous in public.get("stage_history", []):
        if previous.get("artifact"):
            previous["artifact_url"] = f"/api/projects/{project['id']}/history/{previous['id']}/artifact"
    return public


@app.get("/api/projects/{project_id}/history/{history_id}/artifact")
def historical_artifact(project_id: str, history_id: str):
    for previous in _require_project(project_id).get("stage_history", []):
        if previous["id"] == history_id and previous.get("artifact"):
            return FileResponse(previous["artifact"], media_type="video/mp4")
    raise HTTPException(404, "历史成片不存在。")


def _new_stage() -> dict:
    return {
        "status": "pending",
        "progress": 0,
        "detail": None,
        "error": None,
        "artifact": None,
        "cancel_requested": False,
        "started_at": None,
        "queued_at": None,
        "finished_at": None,
        "approved_at": None,
    }


def _require_project(project_id: str) -> dict:
    project = STORE.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在。")
    return project


def _set_stage(project_id: str, stage_id: str, **values: object) -> None:
    def mutate(project: dict) -> None:
        updates = dict(values)
        if "execution" in updates:
            updates["execution"] = {**project["stages"][stage_id].get("execution", {}), **updates["execution"]}
        project["stages"][stage_id].update(updates)

    try:
        STORE.update(project_id, mutate)
    except KeyError:
        return


def _cancelled(project_id: str, stage_id: str) -> bool:
    project = STORE.get(project_id)
    return bool(
        project
        and project["stages"].get(stage_id, {}).get("cancel_requested", False)
    )


def _has_running_stage(project: dict) -> bool:
    return any(
        stage["status"] in {"queued", "running"} or stage.get("fleet_pending")
        for stage in project["stages"].values()
    )


def _project_dir(project_id: str) -> Path:
    return SETTINGS.data_root / "projects" / project_id


def _remove_project_files(project_dir: Path) -> None:
    import shutil

    shutil.rmtree(project_dir, ignore_errors=True)


def _mark_interrupted_stages() -> None:
    for project in STORE.list(limit=500):
        changed = False
        for stage_id, stage in project["stages"].items():
            if stage["status"] in {"queued", "running"} or stage.get("fleet_pending"):
                schedule_id = stage.get("batch_schedule_id")
                schedule = BATCH_STORE.get(schedule_id) if schedule_id else None
                if schedule and schedule["status"] in ACTIVE_SCHEDULE_STATUSES:
                    continue
                if stage_id in LOCAL_STAGES and getattr(COMFY, "is_fleet", False):
                    manual = not getattr(COMFY, "is_multifleet", False)
                    if manual:
                        GPU_GATE.reserve_manual()
                    _spawn(_run_stage, project["id"], stage_id, manual)
                    continue
                stage.update(
                    status="failed",
                    progress=100,
                    detail="服务曾重启，请重试该阶段",
                    error="H3 Video Studio restarted while this stage was active.",
                    finished_at=time.time(),
                    cancel_requested=False,
                )
                changed = True
        if changed:
            STORE.save(project)


def _spawn(target, *args) -> None:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


import sys
from .router_contract import install as install_router_contract
from .script_api import install_script_api
install_router_contract(sys.modules[__name__])
install_script_api(sys.modules[__name__])


@app.api_route("/comparison-planned.json", methods=["GET", "HEAD"])
def comparison_planned():
    if not SETTINGS.comparison_planned_path.is_file():
        raise HTTPException(404, "Comparison plan not found")
    return FileResponse(SETTINGS.comparison_planned_path, media_type="application/json",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.api_route("/comparison-results/{filename:path}", methods=["GET", "HEAD"])
def comparison_result(filename: str):
    root = SETTINGS.comparison_results_root.resolve()
    target = (root / filename).resolve()
    media_types = {".json": "application/json", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
    if (not filename or ".." in filename.split("/") or "\\" in filename or "%" in filename
            or not target.is_relative_to(root) or target.suffix.lower() not in media_types
            or not target.is_file()):
        raise HTTPException(404, "Comparison resource not found")
    return FileResponse(target, media_type=media_types[target.suffix.lower()],
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


app.mount(
    "/",
    StaticFiles(directory=SETTINGS.frontend_root, html=True),
    name="frontend",
)
