from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


OUTPUT = Path("/tmp/h3-video-studio-ui")
OUTPUT.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("H3_STUDIO_URL", "http://127.0.0.1:8791")
CREATE_PROJECT = os.environ.get("H3_UI_CREATE_PROJECT", "1") == "1"
MOCK_QUEUE = os.environ.get("H3_UI_MOCK_QUEUE", "1") == "1"

QUEUE_CANDIDATES = {
    "candidates": [
        {
            "id": "ready-768",
            "name": "夜景列车低清确认版",
            "mode": "t2v",
            "duration": 15.08,
            "seed": 20260810,
            "preview_stage": "preview",
            "preview_artifact_url": None,
            "estimate": {
                "low_seconds": 3600,
                "high_seconds": 4200,
                "label": "1小时–1小时10分",
            },
            "eligible": True,
            "reason": "",
            "updated_at": 0,
        }
    ]
}

QUEUE_SCHEDULES = {
    "schedules": [
        {
            "id": "night-batch",
            "name": "今晚768P",
            "kind": "daily",
            "timezone": "Asia/Shanghai",
            "daily_time": "23:00",
            "status": "scheduled",
            "detail": "等待计划时间",
            "next_run_at": 1786374000,
            "items": [
                {
                    "id": "item-1",
                    "project_id": "scheduled-project",
                    "position": 0,
                    "status": "pending",
                    "project_name": "人物短片低清确认版",
                    "mode": "i2v",
                    "duration": 15.08,
                    "estimate_low_seconds": 3600,
                    "estimate_high_seconds": 4200,
                    "stage_progress": 0,
                }
            ],
            "summary": {
                "count": 1,
                "pending_count": 1,
                "running_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "low_seconds": 3600,
                "high_seconds": 4200,
            },
            "error": None,
        }
    ],
    "active_batch_id": None,
}


def mock_queue_api(page) -> None:
    page.route(
        "**/api/768-queue/candidates",
        lambda route: route.fulfill(json=QUEUE_CANDIDATES),
    )
    page.route(
        "**/api/768-queue/schedules",
        lambda route: route.fulfill(json=QUEUE_SCHEDULES),
    )


def assert_no_body_overflow(page) -> None:
    values = page.evaluate(
        """() => ({
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth
        })"""
    )
    assert values["scrollWidth"] <= values["clientWidth"] + 1, values


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path="/usr/bin/google-chrome",
        args=["--no-sandbox"],
    )
    desktop = browser.new_page(viewport={"width": 1440, "height": 1000})
    if MOCK_QUEUE:
        mock_queue_api(desktop)
    desktop.goto(BASE_URL)
    desktop.wait_for_load_state("networkidle")
    desktop.get_by_role("heading", name="创建视频任务").wait_for()
    desktop.locator('[data-value="hybrid"]').click()
    assert desktop.locator('[data-value="cloud"]').is_disabled()
    desktop.locator('[data-value="t2v"]').click()
    desktop.locator('[data-value="fast"]').click()
    if CREATE_PROJECT:
        desktop.locator("#promptInput").fill(
            "A locked wide shot of a train crossing a bridge at dawn with natural wheel sounds."
        )
        desktop.locator("#createButton").click()
    else:
        desktop.locator("#historyButton").click()
        desktop.locator(".history-item").first.click()
    desktop.locator("#studioView:not(.hidden)").wait_for()
    desktop.locator(".pipeline-node").first.wait_for()
    assert desktop.locator(".pipeline-node").count() >= 4
    assert_no_body_overflow(desktop)
    desktop.screenshot(path=OUTPUT / "desktop.png", full_page=True)
    desktop.locator("#queueButton").click()
    desktop.locator("#queueView:not(.hidden)").wait_for()
    candidate_id = "ready-768" if MOCK_QUEUE else "669ba03ca078"
    desktop.locator(f'[data-candidate-id="{candidate_id}"]').check()
    assert desktop.locator("#selectedCount").inner_text() == "1 项"
    if MOCK_QUEUE:
        assert desktop.locator(".schedule-record").count() == 1
    desktop.locator('[data-schedule-kind="daily"]').click()
    assert desktop.locator("#dailyTimeField").is_visible()
    assert_no_body_overflow(desktop)
    desktop.screenshot(path=OUTPUT / "queue-desktop.png", full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    if MOCK_QUEUE:
        mock_queue_api(mobile)
    mobile.goto(BASE_URL)
    mobile.wait_for_load_state("networkidle")
    mobile.get_by_role("heading", name="创建视频任务").wait_for()
    assert_no_body_overflow(mobile)
    mobile.screenshot(path=OUTPUT / "mobile.png", full_page=True)
    mobile.locator("#queueButton").click()
    mobile.locator("#queueView:not(.hidden)").wait_for()
    mobile.locator(f'[data-candidate-id="{candidate_id}"]').check()
    assert_no_body_overflow(mobile)
    mobile.screenshot(path=OUTPUT / "queue-mobile.png", full_page=True)
    browser.close()
