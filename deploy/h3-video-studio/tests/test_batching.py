from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.batching import (
    BatchInfrastructureError,
    LocalGpuGate,
    TIMEZONE,
    apply_missed_schedule_policy,
    classify_batch_error,
    is_local_768_candidate,
    next_daily_run,
    parse_once_local,
)


def project(*, strategy: str = "fast", proof: bool = False) -> dict:
    return {
        "id": "candidate",
        "name": "Candidate",
        "mode": "t2v",
        "strategy": strategy,
        "duration": 15,
        "actual_duration": 362 / 24,
        "seed": 1,
        "audio_policy": "native",
        "prompt_approved": "approved",
        "stages": {
            "context_ir": {"status": "approved"},
            "preview": {"status": "approved"},
            "proof": {"status": "approved" if proof else "pending"},
            "local_768": {"status": "pending"},
            "cloud_768": {"status": "pending"},
            "regenerate_2k": {"status": "pending"},
        },
    }


def test_candidate_requires_last_local_stage_approval() -> None:
    eligible, reason = is_local_768_candidate(project())
    assert eligible
    assert reason == ""

    eligible, reason = is_local_768_candidate(project(strategy="safe"))
    assert not eligible
    assert "proof" in reason

    eligible, reason = is_local_768_candidate(project(strategy="safe", proof=True))
    assert eligible
    assert reason == ""

    eligible, reason = is_local_768_candidate(project(strategy="cloud"))
    assert not eligible
    assert "云端" in reason


def test_once_and_daily_time_calculation() -> None:
    once = parse_once_local("2026-08-11T23:00")
    local = datetime.fromtimestamp(once, TIMEZONE)
    assert local.strftime("%Y-%m-%d %H:%M") == "2026-08-11 23:00"

    now = datetime(2026, 8, 10, 21, 0, tzinfo=TIMEZONE).timestamp()
    same_day = next_daily_run("23:00", now)
    assert datetime.fromtimestamp(same_day, TIMEZONE).day == 10
    tomorrow = next_daily_run("20:00", now)
    assert datetime.fromtimestamp(tomorrow, TIMEZONE).day == 11


def test_missed_schedule_policy() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    once = {
        "kind": "once",
        "next_run_at": now - 24 * 3600,
    }
    assert apply_missed_schedule_policy(once, now)["next_run_at"] == now

    daily_recent = {
        "kind": "daily",
        "daily_time": "17:00",
        "next_run_at": now - 2 * 3600,
    }
    assert apply_missed_schedule_policy(daily_recent, now)["next_run_at"] == now

    daily_old = {
        "kind": "daily",
        "daily_time": "01:00",
        "next_run_at": now - 8 * 3600,
    }
    next_run = apply_missed_schedule_policy(daily_old, now)["next_run_at"]
    assert next_run > now


def test_error_classification() -> None:
    assert classify_batch_error(ValueError("bad input")) == "item"
    assert classify_batch_error(RuntimeError("CUDA out of memory")) == "infrastructure"
    assert (
        classify_batch_error(BatchInfrastructureError("ComfyUI offline"))
        == "infrastructure"
    )


def test_gpu_gate_blocks_manual_jobs_during_batch() -> None:
    gate = LocalGpuGate()
    gate.reserve_manual()
    acquired = threading.Event()

    def reserve_batch() -> None:
        gate.reserve_batch("batch-1")
        acquired.set()

    thread = threading.Thread(target=reserve_batch)
    thread.start()
    time.sleep(0.05)
    assert not acquired.is_set()
    with pytest.raises(BatchInfrastructureError):
        gate.reserve_manual()
    gate.release_manual()
    assert acquired.wait(1)
    gate.release_batch("batch-1")
    thread.join(timeout=1)
