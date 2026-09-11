"""OpenAI-compatible conversation adapter for server-owned creative workflows."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from uuid import uuid4

from fastapi.responses import JSONResponse, StreamingResponse

from .contracts import MediaError
from .creative import classify, nonrendering_text, readonly_signature
from .gateway import connection, rpc


def messages(body, kind):
    if kind == "responses":
        value = body.get("input", [])
        return [{"role": "user", "content": value}] if isinstance(value, str) else value
    return body.get("messages", [])


def text_content(message):
    value = message.get("content", "")
    if isinstance(value, str):
        return value
    return "\n".join(p.get("text", "") for p in value if isinstance(p, dict) and isinstance(p.get("text"), str)) if isinstance(value, list) else ""


def extract_spec(text):
    result = {}
    duration = re.search(r"(\d+)\s*(?:秒|seconds?|s\b)", text, re.I)
    if duration:
        result["duration"] = int(duration[1])
    count = re.search(r"(?<!\d)(\d+|一|二|两|三)\s*(?:个|种|条)?\s*(?:方向|候选|方案|样片)", text)
    if count:
        result["candidate_count"] = {"一": 1, "二": 2, "两": 2, "三": 3}.get(count[1], int(count[1]) if count[1].isdigit() else 1)
    if re.search(r"横屏|16[:：]9|landscape", text, re.I):
        result["aspect_ratio"] = "16:9"
    elif re.search(r"竖屏|9[:：]16|portrait", text, re.I):
        result["aspect_ratio"] = "9:16"
    if re.search(r"先.*方向图|生成方向图|先.*首帧", text):
        result["direction_images"] = True
    return result


def response_text(workflow):
    status = workflow["status"]
    names = {"planning": "正在整理创作方向；尚未启动视频生成", "draft": "方案待确认", "direction_images": "方向图生成中", "awaiting_frames": "方向图待确认",
             "reviewing": "正在重新检查已归档的视频；不会重新生成视频",
             "sampling": "五秒候选样片生成中", "awaiting_selection": "请选择一个样片继续", "finishing": "正在自动完成低清检查与本地成片",
             "imaging": "图片生成中", "completed": "创作任务已完成", "needs_attention": "创作任务已暂停，需要处理", "cancelling": "正在取消本任务", "cancelled": "已取消"}
    lines = [names.get(status, status), f"工作流：{workflow['id']}；版本：{workflow['revision']}"]
    spec = workflow["spec"]
    if spec["kind"] == "video":
        lines.append(f"目标 {spec['duration']} 秒 · {spec['aspect_ratio']} · 原生音频 · {spec['candidate_count']} 个候选")
    if status == "draft":
        for index, direction in enumerate(workflow["directions"], 1):
            lines.append(f"{index}. {direction['title']}：{direction['prompt']}")
        lines.extend(workflow.get("questions", []))
        lines.append("可以补充要求，或说“确认方案，生成样片”。")
    if status == "awaiting_selection":
        for index, direction in enumerate(workflow["directions"], 1):
            lines.append(f"{index}. {direction['title']} · {direction.get('sample_output_id', '生成失败')}")
        lines.append("说“选择第一个继续”即可授权一个完整低清版和一个本地成片；不会自动重生成。")
    if workflow.get("error"):
        lines.append(workflow["error"].get("message", "请查看任务详情。"))
    for job in workflow.get("jobs", {}).values():
        outputs = ([job["output"]] if job.get("output") else []) + [s["output"] for s in job.get("stages", []) if s.get("output") and s["output"].get("content_type", "").startswith(("image/", "video/"))]
        for output in outputs:
            lines.append(f"产物：{output['id']}（可通过 /v1/media/outputs/{output['id']}/content 鉴权下载）")
    if status not in {"completed", "cancelled", "draft", "awaiting_selection", "awaiting_frames", "needs_attention"}:
        lines.append("生成任务在后台继续。可说“查看进度”，或在1Panel媒体工作室查看。")
    return "\n\n".join(lines)


async def maybe_creative_chat(request, body, authenticated, kind):
    # Trust is established by the server secret, never by a user message or a
    # model-produced flag. Existing internal review accounts have no media grant.
    secret = os.environ.get("AI_ROUTER_MEDIA_INTERNAL_KEY", "")
    supplied = request.headers.get("x-siyuan-media-read-only", "")
    if secret and supplied and hmac.compare_digest(supplied, readonly_signature(body, secret)):
        return None
    if os.environ.get("AI_ROUTER_CREATIVE_CHAT_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return None
    grants = authenticated.policy.media_models
    if not set(grants) & {"siyuan-image", "siyuan-video"}:
        return None
    history = [m for m in messages(body, kind) if isinstance(m, dict)]
    latest = next((m for m in reversed(history) if m.get("role") == "user"), None)
    if not latest:
        return None
    text = text_content(latest).strip()
    # The same intent boundary applies inside an existing workflow. Discussion
    # containing "confirm" or "cancel" must not become an execution command.
    if nonrendering_text(text):
        return None
    metadata = body.get("media") or {}
    if not isinstance(metadata, dict) or set(metadata) - {"workflow_id", "revision", "asset_ids"}:
        raise MediaError("invalid_workflow", "不支持的媒体会话字段。")
    workflow_id, revision = metadata.get("workflow_id"), metadata.get("revision")
    response_owner = hashlib.sha256(authenticated.policy.id.encode()).hexdigest()
    previous = body.get("previous_response_id") if kind == "responses" else None
    if not workflow_id and isinstance(previous, str) and previous.startswith("resp_media_"):
        stored = await request.app.state.runtime.store.get_json("router:creative-response:" + response_owner + ":" + previous)
        if not stored:
            raise MediaError("workflow_context_required", "该回复的会话索引已过期，请提供原回复中的工作流ID和当前版本恢复。", 409)
        workflow_id, revision = stored["workflow_id"], stored["revision"]
    if not workflow_id:
        for message in reversed(history):
            if message.get("role") != "assistant":
                continue
            found = re.search(r"工作流[：:]\s*(wf_[a-f0-9]{32})[；;]\s*版本[：:]\s*(\d+)", text_content(message))
            if found:
                workflow_id, revision = found[1], int(found[2])
                break
    intent = classify(text)
    mentioned = set(re.findall(r"wf_[a-f0-9]{32}", text))
    if len(mentioned) > 1 or mentioned and workflow_id and workflow_id not in mentioned:
        raise MediaError("workflow_target_ambiguous", "请指定本次操作的唯一工作流ID和版本。", 409)
    if mentioned and not workflow_id:
        workflow_id = next(iter(mentioned))
        version = re.search(r"(?:版本|version)\s*[：:]?\s*(\d+)", text, re.I)
        revision = int(version[1]) if version else None
    if re.search(r"新建|新的任务|new (?:task|project)", text, re.I):
        workflow_id = None
    if not workflow_id and not intent:
        return None
    if workflow_id and not re.fullmatch(r"wf_[a-f0-9]{32}", str(workflow_id)):
        raise MediaError("invalid_workflow", "创作任务编号无效。")
    principal = {"owner": authenticated.policy.id, "admin": False, "models": grants}
    mutation_admitted = False
    async def admit_mutation():
        nonlocal mutation_admitted
        if mutation_admitted:
            return
        runtime = request.app.state.runtime
        if getattr(runtime, "draining", False):
            raise MediaError("router_draining", "服务正在切换，请用原操作标识重试。", 503)
        count = await runtime.store.increment_window(f"router:media-submit:{principal['owner']}", 1, 60)
        if count > min(authenticated.policy.rpm_limit, 30):
            raise MediaError("media_rate_limit", "提交过于频繁。", 429)
        mutation_admitted = True
    idem = request.headers.get("idempotency-key") or hashlib.sha256((principal["owner"] + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    async with connection(request, principal) as client:
        client.headers["Idempotency-Key"] = idem
        async def resolve_assets():
            if not isinstance(metadata.get("asset_ids", []), list):
                raise MediaError("invalid_asset", "素材编号必须是数组。")
            assets = list(metadata.get("asset_ids", []))
            for part in latest.get("content", []) if isinstance(latest.get("content"), list) else []:
                if not isinstance(part, dict) or part.get("type") not in {"image_url", "input_image"}:
                    continue
                url = part.get("image_url", "")
                url = url.get("url", "") if isinstance(url, dict) else url
                match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,(.+)", url, re.S) if isinstance(url, str) else None
                if not match:
                    raise MediaError("asset_upload_required", "请先上传图片取得素材ID，或使用内嵌图片内容。")
                asset = await rpc(client, "POST", "/creative/assets", json={"content_type": match[1], "data": match[2], "role": "reference"})
                assets.append(asset["id"])
            return assets
        has_assets = "asset_ids" in metadata or any(isinstance(part, dict) and part.get("type") in {"image_url", "input_image"} for part in (latest.get("content", []) if isinstance(latest.get("content"), list) else []))
        if not workflow_id:
            await admit_mutation()
            assets = await resolve_assets()
            if "siyuan-" + intent not in grants:
                raise MediaError("media_forbidden", "当前账户未获此媒体能力授权。", 403)
            value = {"kind": intent, "prompt": text, "asset_ids": assets, **extract_spec(text)}
            if intent == "image":
                value["start"] = True
            workflow = await rpc(client, "POST", "/creative/workflows", json=value)
        else:
            workflow = await rpc(client, "GET", "/creative/workflows/" + workflow_id)
            action = None
            if re.search(r"取消|停止|cancel|stop", text, re.I):
                action = {"action": "cancel"}
            elif has_assets:
                if workflow["status"] in {"sampling", "finishing", "imaging", "direction_images", "reviewing", "cancelling"}:
                    raise MediaError("workflow_busy", "请先取消正在执行的版本，再替换参考素材。", 409)
                await admit_mutation()
                action = {"action": "message", "message": text, "spec": {**extract_spec(text), "asset_ids": await resolve_assets()}}
            elif re.search(r"进度|状态|结果|查询|查看|status|progress|result", text, re.I):
                pass
            elif re.search(r"只.*提示词|不要.*生成|分析|解释|如何|怎么", text):
                return None
            elif workflow["status"] == "draft" and re.search(r"确认|开始|按.*来|同意|confirm|start", text, re.I):
                action = {"action": "confirm", "convert_references": bool(re.search(r"转换|生成.*首帧|方向图", text))}
            elif workflow["status"] == "awaiting_frames" and re.search(r"确认|继续|同意|approve|continue", text, re.I):
                action = {"action": "approve_frames", "output_ids": [workflow["jobs"][d["image_job_id"]]["output"]["output_id"] for d in workflow["directions"]]}
            elif workflow["status"] == "needs_attention" and re.search(r"复核.*(?:通过|继续)|确认.*继续", text):
                job = workflow.get("jobs", {}).get(workflow.get("final_job_id"), {})
                stage = next((s for s in reversed(job.get("stages", [])) if s.get("output_id") and s["status"] == "awaiting_approval"), None)
                if stage:
                    action = {"action": "resume", "output_id": stage["output_id"], "review_id": (stage.get("review") or {}).get("review_id")}
            elif workflow["status"] == "needs_attention" and re.search(r"重新.*(?:检查|评审)|recheck|review again", text, re.I):
                job = workflow.get("jobs", {}).get(workflow.get("final_job_id"), {})
                stage = next((s for s in reversed(job.get("stages", [])) if s.get("output_id") and s["status"] == "awaiting_approval"), None)
                if stage:
                    action = {"action": "recheck", "output_id": stage["output_id"], "review_id": (stage.get("review") or {}).get("review_id")}
            elif workflow["status"] == "awaiting_selection" and re.search(r"增加|新增|再加", text):
                action = {"action": "add_direction", "prompt": workflow["spec"]["prompt"] + "\n新方向要求：" + text}
            elif workflow["status"] == "awaiting_selection" and re.search(r"选|继续|重跑|重试|select|continue|rerun", text, re.I):
                matches = re.findall(r"第\s*([123一二两三])|(?:选择|选|重跑|重试|select|rerun)\s*([123一二两三])(?=\b|个|条|号|$)|\b(first|second|third)\b", text, re.I)
                choices = {next(value for value in match if value).lower() for match in matches}
                if len(choices) > 1 or re.search(r"一起|全部|都选|分别|\b(?:both|all)\b|[123一二两三]\s*(?:和|或|、|and|or)\s*(?:第)?[123一二两三]", text, re.I):
                    raise MediaError("workflow_target_ambiguous", "请明确选择或重跑一个候选方向。", 409)
                if choices:
                    choice = next(iter(choices))
                    index = {"一": 1, "二": 2, "两": 2, "三": 3, "first": 1, "second": 2, "third": 3}.get(choice, int(choice) if choice.isdigit() else 0) - 1
                    if 0 <= index < len(workflow["directions"]):
                        direction = workflow["directions"][index]
                        action = {"action": "rerun" if re.search(r"重跑|重试|rerun", text, re.I) else "select", "direction_id": direction["id"], "output_id": direction.get("sample_output_id")}
            elif workflow["status"] not in {"planning", "sampling", "finishing", "imaging", "direction_images", "reviewing", "cancelling"}:
                action = {"action": "message", "message": text, "spec": extract_spec(text)}
            if action:
                await admit_mutation()
                action["revision"] = revision
                workflow = await rpc(client, "POST", f"/creative/workflows/{workflow_id}/actions", json=action)
    response = conversation_response(workflow, body, kind)
    if kind == "responses":
        await request.app.state.runtime.store.set_json("router:creative-response:" + response_owner + ":" + response.headers["X-Media-Response-ID"],
                                                      {"workflow_id": workflow["id"], "revision": workflow["revision"]}, ttl_seconds=30*86400)
    return response


def conversation_response(workflow, body, kind):
    text = response_text(workflow)
    identifier, created = "chatcmpl_" + uuid4().hex, int(time.time())
    metadata = {"workflow_id": workflow["id"], "revision": workflow["revision"], "status": workflow["status"], "next_actions": workflow.get("next_actions", []),
                "status_url": "/v1/media/workflows/" + workflow["id"], "generation_completed": workflow["status"] == "completed"}
    result = {"id": identifier, "object": "chat.completion", "created": created, "model": body["model"],
              "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "media": metadata}
    if kind == "responses":
        from ..responses_adapter import chat_response_to_responses
        result = json.loads(chat_response_to_responses(json.dumps(result).encode(), model=body["model"]))
        result["id"] = "resp_media_" + uuid4().hex
        result["media"] = metadata
    headers = {"Cache-Control": "no-store", "X-Media-Response-ID": result["id"]}
    if not body.get("stream"):
        return JSONResponse(result, headers=headers)
    async def stream():
        if kind == "responses":
            item = result["output"][0]
            part = item["content"][0]
            events = [
                {"type": "response.created", "response": {**result, "status": "in_progress", "output": []}},
                {"type": "response.in_progress", "response": {**result, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": {**part, "text": ""}},
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": text},
                {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": part},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": result},
            ]
            for sequence, event in enumerate(events):
                yield "event: " + event["type"] + "\ndata: " + json.dumps({**event, "sequence_number": sequence}, ensure_ascii=False) + "\n\n"
        else:
            for delta, finish in (({"role": "assistant", "content": text}, None), ({}, "stop")):
                yield "data: " + json.dumps({"id": identifier, "object": "chat.completion.chunk", "created": created, "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "media": metadata}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={**headers, "X-Accel-Buffering": "no"})
