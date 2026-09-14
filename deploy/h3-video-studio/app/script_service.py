from __future__ import annotations

import asyncio
import copy
import re
import time

from fastapi import HTTPException

from .script_planner import ROLE, RouterPlanner, digest, generation_prompt, text_field, validate_brief, validate_draft
from .storage import ProjectStore


ACTIVE = {"created", "preflight", "matched", "dispatched", "running", "validating"}


class ScriptService:
    def __init__(self, database_path, planner=None):
        self.store = ProjectStore(database_path)
        self.planner = planner or RouterPlanner()
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.tasks = {}

    def startup(self):
        self.store.initialize()
        for plan in self.store.list(limit=500):
            if plan["status"] in ACTIVE:
                plan.update(status="failed", error={"message": "服务重启中断了文字策划；未自动重发，请明确发起新一轮。", "type": "interrupted"})
                self.store.save(plan)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def get(self, identifier):
        if not re.fullmatch(r"script_[a-f0-9]{24}", identifier):
            raise HTTPException(404, "脚本不存在。")
        plan = self.store.get(identifier)
        if plan is None:
            raise HTTPException(404, "脚本不存在。")
        return plan

    def public(self, plan):
        result = {key: value for key, value in plan.items() if key not in {"operations", "request_digest", "previous_draft"}}
        if plan.get("draft"):
            result["generation_prompt"] = generation_prompt(plan["draft"])
        result["approved"] = plan.get("approved_revision") == plan["revision"]
        result["result_envelope"] = {
            "terminal_state": plan["status"] if plan["status"] not in ACTIVE else None,
            "summary": (plan.get("draft") or {}).get("summary"), "evidence": plan["traces"],
            "artifacts": [{"kind": "screenplay", "revision": plan.get("draft_revision")}] if plan.get("draft") else [],
            "metrics": {"model_calls": plan["model_calls"], "elapsed_seconds": plan.get("elapsed_seconds")},
            "failure": plan.get("error"),
        }
        return result

    def operation(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
            raise HTTPException(400, "需要稳定的 operation_id。")
        return value

    def receipt(self, plan, operation, payload):
        self.operation(operation)
        previous = plan["operations"].get(operation)
        if previous and previous != digest(payload):
            raise HTTPException(409, "同一操作标识对应不同内容。")
        return previous is not None

    def ensure_capacity(self):
        if len(self.tasks) >= 20:
            raise HTTPException(429, "待处理策划任务过多，请稍后再试。")
        if not self.planner.capabilities()["configured"]:
            raise HTTPException(503, "文字策划模型尚未接入，未调用任何 Skills 或生成任务。")

    async def create(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"operation_id", "brief"}:
            raise HTTPException(400, "需要 operation_id 和 brief。")
        operation = self.operation(payload["operation_id"])
        brief = validate_brief(payload["brief"])
        identifier = "script_" + digest(operation)[:24]
        async with self.lock:
            existing = self.store.get(identifier)
            if existing:
                if existing["request_digest"] != digest(brief):
                    raise HTTPException(409, "原操作对应不同的策划需求。")
                return self.public(existing)
            self.ensure_capacity()
            plan = {"id": identifier, "revision": 1, "approved_revision": None, "status": "created",
                    "brief": brief, "draft": None, "draft_revision": None, "skills": [], "selection_reason": None, "instruction": "",
                    "history": [], "operations": {operation: digest(payload)}, "request_digest": digest(brief),
                    "traces": [], "model_calls": 0, "error": None, "events": [], "created_at": time.time()}
            self.store.save(plan)
            self.dispatch(plan)
            return self.public(plan)

    def dispatch(self, plan):
        key = (plan["id"], plan["revision"])
        task = asyncio.create_task(self.run(copy.deepcopy(plan)))
        self.tasks[key] = task
        task.add_done_callback(lambda completed: self.tasks.pop(key, None))

    def checkpoint(self, task, status, **values):
        def mutate(plan):
            if plan["revision"] != task["revision"] or plan["status"] == "cancelled":
                raise asyncio.CancelledError()
            if values.get("trace"):
                plan["traces"].append(values.pop("trace"))
            if values.pop("call", None):
                plan["model_calls"] += 1
            plan.update(status=status, **values)
            plan["events"] = [*plan["events"], {"status": status, "at": time.time()}][-20:]
        return self.store.update(task["id"], mutate)

    async def run(self, task):
        started = time.monotonic()
        try:
            async with self.slots:
                self.checkpoint(task, "preflight")
                result = await asyncio.wait_for(self.planner.plan(task, lambda status, **values: self.checkpoint(task, status, **values)), ROLE["timeout_seconds"])
                result = validate_draft(result, task["brief"]["duration"])
                self.checkpoint(task, "needs_context" if result["questions"] else "completed", draft=result, draft_revision=task["revision"],
                                elapsed_seconds=round(time.monotonic() - started, 3), finished_at=time.time())
        except asyncio.CancelledError:
            current = self.get(task["id"])
            if current["revision"] == task["revision"]:
                current.update(status="cancelled", finished_at=time.time())
                self.store.save(current)
        except Exception as error:
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "策划未完成或超时；未自动重试。"
            self.checkpoint(task, "failed", error={"message": message[:1500], "type": type(error).__name__, "at": time.time()}, finished_at=time.time())

    async def action(self, identifier, action, payload):
        fields = {"approve": set(), "cancel": set(), "revise": {"instruction"}, "save": {"draft"}}
        if action not in fields:
            raise HTTPException(404, "未知脚本操作。")
        if not isinstance(payload, dict) or set(payload) != {"operation_id", "revision"} | fields[action]:
            raise HTTPException(400, "不支持的脚本操作。")
        operation = self.operation(payload.get("operation_id"))
        async with self.lock:
            plan = self.get(identifier)
            if self.receipt(plan, operation, {"action": action, **payload}):
                return self.public(plan)
            if type(payload.get("revision")) is not int or payload["revision"] != plan["revision"]:
                raise HTTPException(409, "脚本版本已改变，请刷新后再操作。")
            if action == "cancel":
                if plan["status"] not in ACTIVE:
                    raise HTTPException(409, "该轮策划已结束。")
                task = self.tasks.get((identifier, plan["revision"]))
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                plan = self.get(identifier)
                plan.update(status="cancelled", finished_at=time.time())
            elif plan["status"] in ACTIVE:
                raise HTTPException(409, "请先等待或取消当前策划。")
            elif action == "approve":
                if plan["status"] != "completed" or not plan.get("draft"):
                    raise HTTPException(409, "请先完成脚本并解决待补充问题。")
                plan.update(approved_revision=plan["revision"], approved_hash=digest(plan["draft"]), approved_at=time.time())
            elif action in {"revise", "save"}:
                if action == "revise":
                    self.ensure_capacity()
                    instruction = text_field(payload.get("instruction"), "修改要求", 4000)
                else:
                    draft = validate_draft(payload.get("draft"), plan["brief"]["duration"])
                if plan.get("draft"):
                    plan["history"] = [*plan["history"], {"revision": plan["revision"], "draft": plan["draft"], "approved": plan.get("approved_revision") == plan["revision"]}][-20:]
                plan.update(revision=plan["revision"] + 1, approved_revision=None, error=None)
                if action == "revise":
                    plan.update(previous_draft=plan.get("draft"), instruction=instruction, status="created", traces=[], model_calls=0, events=[], skills=[], selection_reason=None)
                else:
                    plan.update(draft=draft, draft_revision=plan["revision"], status="needs_context" if draft["questions"] else "completed")
            else:
                raise HTTPException(404, "未知脚本操作。")
            plan["operations"][operation] = digest({"action": action, **payload})
            if len(plan["operations"]) > 200:
                raise HTTPException(409, "脚本操作数达到上限，请新建策划。")
            self.store.save(plan)
            if action == "revise":
                self.dispatch(plan)
            return self.public(plan)

    def approved_source(self, identifier, revision, *, prompt, duration, mode, audio_policy):
        plan = self.get(identifier)
        if plan["status"] != "completed" or plan["revision"] != revision or plan.get("approved_revision") != revision or plan.get("approved_hash") != digest(plan["draft"]):
            raise HTTPException(409, "脚本尚未批准，或原批准版本已失效。")
        normalized_prompt = prompt.replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized_prompt != generation_prompt(plan["draft"]) or any(plan["brief"][key] != value for key, value in {"duration": duration, "mode": mode, "audio_policy": audio_policy}.items()):
            raise HTTPException(409, "生成设置与已批准脚本不一致，请修改脚本并重新确认。")
        return {"id": identifier, "revision": revision, "sha256": plan["approved_hash"], "skills": plan["skills"], "original_brief": plan["brief"]["prompt"]}
