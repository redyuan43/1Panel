from __future__ import annotations

import shutil
import threading
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .workflows import pipeline_for, runtime_profile


TIMEZONE_NAME = "Asia/Shanghai"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)
DAILY_MISSED_GRACE_SECONDS = 6 * 3600
MIN_FREE_DISK_BYTES = 20 * 1024 * 1024 * 1024

ACTIVE_SCHEDULE_STATUSES = {
    "scheduled",
    "waiting_for_items",
    "waiting_for_gpu",
    "running",
    "pausing",
    "paused",
}
FINAL_SCHEDULE_STATUSES = {"completed", "cancelled"}
ACTIVE_ITEM_STATUSES = {"pending", "running"}


class BatchInfrastructureError(RuntimeError):
    pass


class BatchItemError(RuntimeError):
    pass


class LocalGpuGate:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._batch_id: str | None = None
        self._manual_count = 0

    def reserve_manual(self) -> None:
        with self._condition:
            if self._batch_id is not None:
                raise BatchInfrastructureError(
                    "768P批次正在等待或独占本地GPU，请等待批次完成或暂停。"
                )
            self._manual_count += 1

    def release_manual(self) -> None:
        with self._condition:
            self._manual_count = max(0, self._manual_count - 1)
            self._condition.notify_all()

    def reserve_batch(self, batch_id: str) -> None:
        with self._condition:
            if self._batch_id not in {None, batch_id}:
                raise BatchInfrastructureError("另一个768P批次已占用本地GPU。")
            self._batch_id = batch_id
            while self._manual_count:
                self._condition.wait(timeout=2)

    def release_batch(self, batch_id: str) -> None:
        with self._condition:
            if self._batch_id == batch_id:
                self._batch_id = None
                self._condition.notify_all()

    def active_batch_id(self) -> str | None:
        with self._condition:
            return self._batch_id


def parse_once_local(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("单次计划时间格式无效。") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(timezone.utc).timestamp()


def parse_daily_time(value: str) -> datetime_time:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as error:
        raise ValueError("每日时间必须使用 HH:MM 格式。") from error
    return parsed.time()


def next_daily_run(
    daily_time: str,
    now: float,
    *,
    include_today: bool = True,
) -> float:
    clock = parse_daily_time(daily_time)
    local_now = datetime.fromtimestamp(now, TIMEZONE)
    candidate = datetime.combine(local_now.date(), clock, TIMEZONE)
    if candidate.timestamp() < now or (
        candidate.timestamp() == now and not include_today
    ):
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).timestamp()


def apply_missed_schedule_policy(schedule: dict, now: float) -> dict:
    next_run_at = schedule.get("next_run_at")
    if next_run_at is None or next_run_at > now:
        return schedule
    if schedule["kind"] == "once":
        schedule["next_run_at"] = now
        schedule["detail"] = "单次计划曾错过时间，Edge恢复后立即补跑"
        return schedule
    overdue = now - float(next_run_at)
    if overdue <= DAILY_MISSED_GRACE_SECONDS:
        schedule["next_run_at"] = now
        schedule["detail"] = "每日计划错过时间不超过6小时，立即补跑"
    else:
        schedule["next_run_at"] = next_daily_run(
            schedule["daily_time"],
            now,
            include_today=False,
        )
        schedule["detail"] = "每日计划错过超过6小时，等待下一天"
    return schedule


def is_local_768_candidate(project: dict) -> tuple[bool, str]:
    pipeline = [stage["id"] for stage in pipeline_for(project)]
    if "local_768" not in pipeline:
        return False, "当前项目使用官方云端768P策略"
    stage = project["stages"]["local_768"]
    if stage.get("batch_schedule_id"):
        return False, "已加入其他768P批次"
    if stage["status"] in {"queued", "running", "scheduled"} or stage.get("fleet_pending"):
        return False, "本地768P已在排队或运行"
    if stage["status"] in {"awaiting_approval", "approved"}:
        return False, "本地768P已经生成"
    index = pipeline.index("local_768")
    previous = pipeline[index - 1]
    if project["stages"][previous]["status"] != "approved":
        return False, f"请先确认{previous}阶段"
    if not project.get("prompt_approved"):
        return False, "优化提示词尚未确认"
    return True, ""


def estimate_projects(projects: list[dict]) -> dict:
    low = 0
    high = 0
    for project in projects:
        profile = runtime_profile(project, "local_768")
        low += int(profile["low_seconds"])
        high += int(profile["high_seconds"])
    return {
        "low_seconds": low,
        "high_seconds": high,
        "count": len(projects),
    }


def classify_batch_error(error: Exception) -> str:
    if isinstance(error, BatchInfrastructureError):
        return "infrastructure"
    if isinstance(error, (BatchItemError, ValueError, FileNotFoundError)):
        return "item"
    message = str(error).lower()
    infrastructure_markers = {
        "connection refused",
        "urlopen error",
        "comfyui 离线",
        "cuda",
        "out of memory",
        "no space left",
        "disk full",
        "driver",
        "nvml",
        "device-side assert",
    }
    return (
        "infrastructure"
        if any(marker in message for marker in infrastructure_markers)
        else "item"
    )


def check_disk_space(path: Path) -> None:
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_DISK_BYTES:
        raise BatchInfrastructureError(
            f"可用磁盘空间不足20GB，当前剩余 {free / 1024**3:.1f}GB。"
        )


def local_datetime_label(timestamp: float | None) -> str:
    if timestamp is None:
        return "未设置"
    return datetime.fromtimestamp(timestamp, TIMEZONE).strftime("%Y-%m-%d %H:%M")
