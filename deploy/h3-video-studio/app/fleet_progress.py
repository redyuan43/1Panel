"""Display confirmed sampler observations without inventing whole-job progress."""
from __future__ import annotations

import json
import math


def owned_sampler_progress(job):
    try:
        receipt = job.get("progress_json")
        receipt = json.loads(receipt) if isinstance(receipt, str) else receipt
        upstream = job.get("upstream_prompt_id")
        if (not upstream or not isinstance(receipt, dict) or receipt.get("prompt_id") != upstream
                or receipt.get("error") or receipt.get("cached") is True):
            return None
        # ready gates a SECOND concurrent task, not whether a measured step is
        # valid. It normally becomes false when 4/4 finishes and decoding starts.
        latest = None
        for event in receipt.get("sampler_progress_events", []):
            if (event.get("prompt_id") != upstream or event.get("type") != "progress"
                    or event.get("confirmed_sampler_progress") is not True):
                return None
            value, maximum, node = event.get("value"), event.get("max"), event.get("node")
            stamp = event.get("received_at")
            if (type(value) is not int or type(maximum) is not int or not 0 < value <= maximum
                    or not isinstance(node, str) or not node or type(stamp) not in (int, float)
                    or not math.isfinite(stamp)):
                return None
            if latest and (stamp < latest["observed_at"] or node == latest["node"] and
                           (maximum != latest["total"] or value < latest["completed"])):
                return None
            latest = {"node": node, "completed": value, "total": maximum,
                      "observed_at": stamp, "basis": "confirmed_owned_sampler_events"}
        return latest
    except (ValueError, TypeError, AttributeError):
        return None


def execution_detail(job, execution):
    node = job.get("node_id") or "执行节点"
    phase = execution.get("phase")
    label = {"resource_waiting": "等待资源准入", "backend_starting": "启动运行环境",
             "backend_disabled": "运行环境尚未开放", "input_preparing": "准备输入",
             "model_preparing": "加载模型与准备条件", "sampling": "采样中",
             "decoding": "解码中", "saving": "保存视频中"}.get(phase,
             "等待资源准入" if job.get("status") == "queued" else "执行中")
    if job.get("status") == "queued":
        reason = job.get("admission_reason") or ""
        switching = {"edge_model_draining": "等待原服务空闲，暂停 Qwen / 准备 H3",
                     "edge_model_stopping_qwen": "暂停 Qwen / 准备 H3",
                     "edge_model_waiting_memory": "准备 H3，等待内存释放",
                     "edge_model_restoring_qwen": "恢复 Qwen，暂不接下一条视频"}
        label = switching.get(reason.removeprefix("admission_waiting:"), label)
    observed = execution.get("sampler_progress")
    if observed:
        label += f"；采样最近确认 {observed['completed']}/{observed['total']} 步"
    return f"{node}：{label}；完成后提供视频"
