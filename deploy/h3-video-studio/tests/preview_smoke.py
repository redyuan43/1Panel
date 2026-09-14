from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def wait_project(client, project_id, stage_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        project = client.get(f"/api/projects/{project_id}").raise_for_status().json()
        stage = project["stages"][stage_id]
        if stage["status"] == "awaiting_approval":
            return project
        assert stage["status"] not in {"failed", "cancelled"}, stage
        time.sleep(0.1)
    raise AssertionError(f"Timeout: {project_id}/{stage_id}")


def api_scenario(client, mode, strategy, assets, audio_policy="native", embedded=False, batch=False):
    response = client.post("/api/projects", data={"name": f"Smoke {mode} {strategy}", "mode": mode,
        "strategy": strategy, "prompt": "合成素材流程验证，无真实模型调用", "duration": "5",
        "audio_policy": audio_policy, "use_embedded_video_audio": str(embedded).lower()},
        files={name: (path.name, path.read_bytes()) for name, path in assets.items()})
    assert response.is_success, (mode, strategy, audio_policy, response.text)
    project = response.json()
    project_id = project["id"]
    prefix = f"/api/projects/{project_id}"
    client.post(prefix + "/context-ir").raise_for_status()
    wait_project(client, project_id, "context_ir")
    client.post(prefix + "/context-ir/approve", json={"prompt": "模拟确认提示词"}).raise_for_status()
    for stage in project["pipeline"][1:]:
        stage_id = stage["id"]
        if batch and stage_id == "local_768":
            return project_id
        client.post(prefix + f"/stages/{stage_id}/start", json={}).raise_for_status()
        completed = wait_project(client, project_id, stage_id)
        assert completed["stages"][stage_id]["simulated"] is True
        artifact = client.get(prefix + f"/artifacts/{stage_id}").raise_for_status()
        assert artifact.headers["content-type"] == "video/mp4" and len(artifact.content) > 1024
        client.post(prefix + f"/stages/{stage_id}/approve").raise_for_status()
    return project_id


def run_checks(base_url, state_dir, evidence_dir, chromium):
    headers = {"Authorization": "Bearer isolated-browser-test"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=30, trust_env=False) as client:
        deadline = time.monotonic() + 30
        while True:
            try:
                health = client.get("/api/health").raise_for_status().json()
                assert health["execution_mode"] == "preview"
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1050}, extra_http_headers=headers)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(base_url)
            expect(page.locator("#executionMode")).to_contain_text("交互体验模式")
            assert page.locator("#modeControl button").count() == 6
            page.screenshot(path=str(evidence_dir / "desktop-setup.png"), full_page=True)
            for mode in ("i2v", "l2v", "fl2v", "reference", "hybrid", "t2v"):
                page.locator(f'#modeControl [data-value="{mode}"]').click()
                expect(page.locator("#modeInput")).to_have_value(mode)
            page.locator('#strategyControl [data-value="fast"]').click()
            page.locator("#projectName").fill("浏览器全流程体验")
            page.locator("#promptInput").fill("这是不调用GPU的页面交互测试")
            page.locator("#durationInput").fill("5")
            page.locator("#createButton").click()
            expect(page.get_by_role("button", name="确认提示词", exact=True)).to_be_visible(timeout=15000)
            page.get_by_role("button", name="确认提示词", exact=True).click()
            for stage_id in ("preview", "local_768", "regenerate_2k"):
                page.locator(f'#pipeline [data-stage="{stage_id}"]').click()
                page.locator("#stageActions .primary-action").click()
                expect(page.get_by_role("button", name="确认并继续", exact=True)).to_be_visible(timeout=15000)
                assert page.locator("#stageVideo").get_attribute("src").startswith("/api/projects/")
                page.get_by_role("button", name="确认并继续", exact=True).click()
            expect(page.locator("#stageFacts")).to_contain_text("非真实成片")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(evidence_dir / "desktop-completed.png"), full_page=True)
            page.locator("#historyButton").click()
            page.locator("#closeHistoryButton").click()
            page.locator("#queueButton").click()
            expect(page.locator("#queueView")).to_be_visible()
            page.screenshot(path=str(evidence_dir / "desktop-queue.png"), full_page=True)
            page.locator("#newProjectButton").click()
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(evidence_dir / "mobile-setup.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            browser.close()
        assert not errors, errors
        video = state_dir / "fixtures/synthetic-preview.mp4"
        picture = evidence_dir / "reference.png"
        audio = evidence_dir / "reference.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-frames:v", "1", "-update", "1", str(picture)], check=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-c:a", "pcm_s16le", str(audio)], check=True)
        cases = [
            ("t2v", "safe", {}, "native", False),
            ("t2v", "cloud", {}, "native", False),
            ("i2v", "fast", {"first_frame": picture}, "native", False),
            ("l2v", "fast", {"last_frame": picture}, "native", False),
            ("fl2v", "safe", {"first_frame": picture, "last_frame": picture}, "native", False),
            ("reference", "safe", {"reference_image": picture}, "native", False),
            ("reference", "safe", {"reference_image": picture, "reference_audio": audio}, "reference", False),
            ("reference", "cloud", {"reference_video": video}, "reference", True),
            ("hybrid", "safe", {"first_frame": picture, "last_frame": picture, "reference_image": picture}, "native", False),
            ("i2v", "safe", {"first_frame": picture, "reference_audio": audio}, "lock_source", False),
        ]
        project_ids = [api_scenario(client, *case) for case in cases]
        batch_ids = [api_scenario(client, "t2v", "fast", {}, batch=True) for unused in range(2)]
        future = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
        schedule = client.post("/api/768-queue/schedules", json={"kind": "once", "project_ids": batch_ids,
            "once_local": future.strftime("%Y-%m-%dT%H:%M")}).raise_for_status().json()
        schedule_path = "/api/768-queue/schedules/" + schedule["id"]
        client.post(schedule_path + "/pause").raise_for_status()
        client.post(schedule_path + "/resume").raise_for_status()
        client.post(schedule_path + "/start-now").raise_for_status()
        for project_id in batch_ids:
            wait_project(client, project_id, "local_768")
        schedule = client.get(schedule_path).raise_for_status().json()
        assert schedule["status"] == "completed", schedule
        items = sorted(schedule["items"], key=lambda item: item["position"])
        assert items[0]["finished_at"] <= items[1]["started_at"]
        report = {"mode": "synthetic-only", "browser_errors": errors, "api_cases": len(cases),
                  "project_ids": project_ids, "batch_id": schedule["id"], "batch_serial": True,
                  "real_gpu_calls": 0, "cloud_calls": 0}
        (evidence_dir / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--chromium", required=True)
    arguments = parser.parse_args()
    evidence_dir = arguments.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    state_dir = evidence_dir / "state"
    key_file = evidence_dir / "test.key"
    key_file.write_text("isolated-browser-test")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "H3_STUDIO_KEY_FILE": str(key_file), "H3_STUDIO_TAILSCALE_USERS": ""}
    with (evidence_dir / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/preview.py"), "--state-dir", str(state_dir),
            "--port", str(port), "--step-delay", "0.05"], env=environment, stdout=log, stderr=log)
        try:
            run_checks(f"http://127.0.0.1:{port}", state_dir, evidence_dir, arguments.chromium)
        finally:
            process.terminate()
            process.wait(timeout=10)
