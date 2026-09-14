import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BRIEF = "15秒蓝牙音箱广告：双角色、六镜头，悬疑交接反转为排练，最后3秒固定产品展示。不编造性能。"


def checks(url, directory, configured):
    headers = {"Authorization": "Bearer inline-script-test"}
    with httpx.Client(base_url=url, headers=headers, trust_env=False, timeout=20) as client:
        for attempt in range(100):
            try:
                if client.get("/api/scripts/options").is_success:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        else:
            raise AssertionError("隔离服务未启动，请检查 server.log")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome", headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1050}, extra_http_headers=headers)
            errors, operations = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator("#promptSkills")).to_be_visible()
            page.locator("#promptInput").fill(BRIEF)
            if not configured:
                expect(page.locator("#promptSkillsStart")).to_be_disabled()
                expect(page.locator("#promptSkillsCapability")).to_contain_text("尚未接入")
                expect(page.locator("#createButton")).to_be_enabled()
                page.screenshot(path=str(directory / "unconfigured-inline.png"), full_page=True)
                assert not errors
                browser.close()
                return

            def lost_response(route):
                payload = route.request.post_data_json
                operations.append(payload["operation_id"])
                assert payload["brief"]["skill_ids"] == []
                assert payload["brief"]["prompt"] == BRIEF
                if len(operations) == 1:
                    route.fetch()
                    route.abort("failed")
                else:
                    route.continue_()

            page.route("**/api/scripts", lost_response)
            page.locator("#promptSkillsStart").click()
            expect(page.locator("#promptSkillsError")).not_to_be_empty()
            expect(page.locator("#createButton")).to_be_disabled()
            page.reload()
            expect(page.locator("#promptSkillsRetry")).to_be_visible()
            expect(page.locator("#createButton")).to_be_disabled()
            page.locator("#promptSkillsRetry").click()
            expect(page.locator("#promptSkillsStatus")).to_contain_text("脚本已产出", timeout=15000)
            assert operations[0] == operations[1]
            page.unroute("**/api/scripts", lost_response)
            assert len(client.get("/api/scripts").json()["scripts"]) == 1
            assert page.url.startswith(url + "/?script_draft=") and "script-studio.html" not in page.url
            expect(page.locator("#promptInput")).to_have_value(BRIEF)
            assert page.locator('#promptSkillsSelected [data-baseline="true"]').count() == 2
            assert page.locator('#promptSkillsSelected [data-baseline="false"]').count() == 3
            expect(page.locator("#promptSkillsReason")).to_contain_text("测试预设")
            assert page.locator(".prompt-skills-shot").count() == 6
            expect(page.locator("#createButton")).to_be_disabled()
            assert client.get("/api/projects").json()["projects"] == []

            page.locator("#promptInput").fill(BRIEF + "增加一个要求")
            expect(page.locator("#promptSkillsApply")).to_be_disabled()
            expect(page.locator("#promptSkillsStale")).to_be_visible()
            page.locator("#promptInput").fill(BRIEF)
            page.locator("#promptSkillsApply").click()
            expect(page.locator("#scriptSourceNotice")).to_contain_text("v1")
            assert "integrated_multimodal_description" in page.locator("#promptInput").input_value()
            expect(page.locator("#createButton")).to_be_enabled()
            assert client.get("/api/projects").json()["projects"] == []

            page.locator("#promptSkillsInstruction").fill("六镜都保留，每镜至少2秒，末镜6秒，总长15秒。")
            page.locator("#promptSkillsRevise").click()
            expect(page.locator("#promptSkillsQuestions")).to_be_visible(timeout=15000)
            expect(page.locator("#promptSkillsQuestions")).to_contain_text("16秒")
            expect(page.locator("#promptInput")).to_have_value(BRIEF)
            expect(page.locator("#promptSkillsApply")).to_be_disabled()
            expect(page.locator("#createButton")).to_be_disabled()
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(directory / "conflict-inline.png"), full_page=True)
            page.locator("#promptSkillsInstruction").fill("同意改成五镜，保留15秒与末镜6秒。")
            page.locator("#promptSkillsRevise").click()
            expect(page.locator("#promptSkillsStatus")).to_contain_text("v3 · 脚本已产出", timeout=15000)
            assert page.locator(".prompt-skills-shot").count() == 5
            page.reload()
            expect(page.locator("#promptSkillsStatus")).to_contain_text("v3 · 脚本已产出")
            expect(page.locator("#promptInput")).to_have_value(BRIEF)
            page.locator("#promptSkillsApply").click()
            expect(page.locator("#scriptSourceNotice")).to_contain_text("v3")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(directory / "approved-inline-desktop.png"), full_page=True)
            approved_url = page.url

            scripts = client.get("/api/scripts").json()["scripts"]
            plan = client.get("/api/scripts/" + scripts[0]["id"]).json()
            plan["draft"]["title"] = "其他页面修改了这个脚本"
            client.post(f'/api/scripts/{plan["id"]}/save', json={"revision": 3, "operation_id": "external-change", "draft": plan["draft"]}).raise_for_status()
            page.locator("#createButton").click()
            expect(page.locator("#promptSkillsStatus")).to_contain_text("v4 · 脚本已产出")
            expect(page.locator("#createButton")).to_be_disabled()
            assert client.get("/api/projects").json()["projects"] == []
            page.locator("#promptSkillsApply").click()
            expect(page.locator("#scriptSourceNotice")).to_contain_text("v4")
            approved_url = page.url
            page.locator("#createButton").click()
            expect(page.get_by_role("button", name="确认提示词", exact=True)).to_be_visible(timeout=15000)
            projects = client.get("/api/projects").json()["projects"]
            assert len(projects) == 1 and projects[0]["script_source"]["revision"] == 4
            assert projects[0]["stages"]["preview"]["status"] == "pending"

            mobile = browser.new_page(viewport={"width": 390, "height": 844}, extra_http_headers=headers)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(approved_url)
            expect(mobile.locator("#promptSkillsStatus")).to_contain_text("已确认并填入")
            assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            mobile.screenshot(path=str(directory / "approved-inline-mobile.png"), full_page=True)
            mobile.locator("#promptSkillsDiscard").click()
            expect(mobile.locator("#promptInput")).to_have_value(BRIEF)
            expect(mobile.locator("#createButton")).to_be_enabled()
            mobile.locator("#promptInput").fill("慢请求测试：仍是15秒音箱广告")
            mobile.locator("#promptSkillsStart").click()
            expect(mobile.locator("#promptSkillsCancel")).to_be_visible()
            mobile.locator("#promptSkillsCancel").click()
            expect(mobile.locator("#promptSkillsStatus")).to_contain_text("本轮已取消")
            mobile.locator("#promptSkillsDiscard").click()
            expect(mobile.locator("#promptSkillsResult")).to_be_hidden()
            mobile.locator('#modeControl [data-value="i2v"]').click()
            mobile.locator('#assetFields input[name="first_frame"]').set_input_files({"name": "actor-reference.png", "mimeType": "image/png", "buffer": b"fixture-not-uploaded-during-planning"})
            mobile.locator("#promptInput").fill("素材保留测试：15秒广告，不解析素材")
            mobile.locator("#promptSkillsStart").click()
            expect(mobile.locator("#promptSkillsStatus")).to_contain_text("脚本已产出", timeout=15000)
            mobile.locator("#promptSkillsApply").click()
            expect(mobile.locator("#promptSkillsStatus")).to_contain_text("已确认并填入")
            assert mobile.locator('#assetFields input[name="first_frame"]').evaluate("input => input.files[0].name") == "actor-reference.png"
            assert "actor-reference.png" in mobile.locator("#promptSkillsEvidence").text_content() or "素材保留测试" in mobile.locator("#promptSkillsEvidence").text_content()
            assert len(client.get("/api/projects").json()["projects"]) == 1
            assert not errors, errors
            report = {"planner": "explicit test fixtures; not live inference", "entry": "original prompt, same page", "shots": [6, 5],
                      "checks": ["automatic-selection-request", "baseline-labels", "selection-reason", "lost-response-idempotency", "preserve-original", "conflict-block", "revision", "reload", "approve-and-fill", "stale-version-block", "explicit-create", "mobile", "cancel", "discard", "keep-selected-files-without-upload"],
                      "project_id": projects[0]["id"], "browser_errors": errors, "external_model_calls": 0, "gpu_calls": 0}
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--unconfigured", action="store_true")
    arguments = parser.parse_args()
    directory = arguments.evidence_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "access.key"
    key.write_text("inline-script-test")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "H3_STUDIO_KEY_FILE": str(key), "H3_STUDIO_TAILSCALE_USERS": ""}
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "tests/script_ui_smoke.py"), "--server", str(port),
            "--evidence-dir", str(directory), "--inline-fixtures", *(["--unconfigured"] if arguments.unconfigured else [])], env=environment, stdout=log, stderr=log)
        try:
            checks(f"http://127.0.0.1:{port}", directory, not arguments.unconfigured)
        finally:
            process.terminate()
            process.wait(timeout=15)
