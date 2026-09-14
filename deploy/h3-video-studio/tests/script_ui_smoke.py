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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def serve(directory, port, configured, inline_fixtures=False):
    from preview import configure_preview, deny_network
    from app.script_planner import RouterPlanner
    from test_script_planner import sample_draft
    import urllib.request
    import uvicorn

    module = configure_preview(directory / "state", .05)
    urllib.request.urlopen = deny_network
    if configured:
        credentials = directory / "fake-credentials.json"
        credentials.write_text(json.dumps({"router_key": "test-key", "readonly_secret": "test-signature"}))
        os.environ["H3_SCRIPT_CREDENTIALS_FILE"] = str(credentials)

        def respond(request):
            phase = request.headers["x-request-id"].rsplit("_", 1)[-1]
            result = {"skill_ids": ["seeding-video"], "reason": "测试夹具的语义选择，不代表真实模型调用"} if phase == "skills" else sample_draft()
            return httpx.Response(200, json={"model": "TEST-FIXTURE-NOT-REAL-MODEL", "choices": [{"message": {"content": json.dumps(result)}}]})

        if inline_fixtures:
            from prompt_skills_fixtures import respond
        module.SCRIPTS.planner = RouterPlanner(httpx.MockTransport(respond))
    else:
        os.environ.pop("H3_SCRIPT_CREDENTIALS_FILE", None)
    uvicorn.run(module.app, host="127.0.0.1", port=port, access_log=False)


def checks(url, directory, chromium, configured):
    headers = {"Authorization": "Bearer script-browser-test"}
    with httpx.Client(base_url=url, headers=headers, timeout=20, trust_env=False) as client:
        for attempt in range(100):
            try:
                if client.get("/api/scripts/options").is_success:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        else:
            raise AssertionError("隔离测试服务未启动，请检查 server.log")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 1050}, extra_http_headers=headers)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "/")
            expect(page.locator("#scriptStudioLink")).to_be_visible()
            page.locator("#scriptStudioLink").click()
            expect(page.locator("#capability")).to_contain_text("视频生成：模拟")
            if not configured:
                expect(page.locator("#createScript")).to_be_disabled()
                expect(page.locator("#capability")).to_contain_text("尚未接入")
                page.screenshot(path=str(directory / "unconfigured.png"), full_page=True)
                assert not errors
                browser.close()
                return
            page.locator("#brief").fill("做一个15秒咖啡杯种草视频，不编造产品功效。")
            page.locator("#createScript").click()
            expect(page.locator("#status")).to_have_text("脚本待确认", timeout=15000)
            expect(page.locator("#selectedSkills")).to_contain_text("种草视频")
            page.locator("#title").fill("我的通勤咖啡杯")
            expect(page.locator("#approve")).to_be_disabled()
            page.locator("#save").click()
            expect(page.locator("#planTitle")).to_have_text("脚本 · v2")
            page.locator("#instruction").fill("结尾更轻松一些。")
            page.locator("#revise").click()
            expect(page.locator("#planTitle")).to_have_text("脚本 · v3")
            expect(page.locator("#status")).to_have_text("脚本待确认", timeout=15000)
            page.locator("#approve").click()
            expect(page.locator("#handoff")).to_be_visible()
            assert client.get("/api/projects").json()["projects"] == []
            with page.expect_download() as download:
                page.locator("#exportMarkdown").click()
            download.value.save_as(str(directory / "approved-script.md"))
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(directory / "desktop-script.png"), full_page=True)
            script_url = page.url
            page.locator("#handoff").click()
            expect(page.locator("#scriptSourceNotice")).to_contain_text("v3")
            assert "integrated_multimodal_description" in page.locator("#promptInput").input_value()
            assert client.get("/api/projects").json()["projects"] == []
            page.screenshot(path=str(directory / "handoff-settings.png"), full_page=True)
            page.locator("#createButton").click()
            page.wait_for_function("document.getElementById('formError').textContent || !document.getElementById('studioView').classList.contains('hidden')")
            assert not page.locator("#formError").text_content(), page.locator("#formError").text_content()
            expect(page.get_by_role("button", name="确认提示词", exact=True)).to_be_visible(timeout=15000)
            projects = client.get("/api/projects").json()["projects"]
            assert len(projects) == 1 and projects[0]["script_source"]["revision"] == 3
            assert projects[0]["stages"]["preview"]["status"] == "pending"
            mobile = browser.new_page(viewport={"width": 390, "height": 844}, extra_http_headers=headers)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(script_url)
            expect(mobile.locator("#handoff")).to_be_visible()
            assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            mobile.screenshot(path=str(directory / "mobile-script.png"), full_page=True)
            assert not errors, errors
            report = {"planner": "httpx MockTransport fixture; NOT live inference", "video": "synthetic preview",
                      "checks": ["semantic-selection-contract", "edit", "revision", "approval", "markdown-download", "handoff-without-autostart", "explicit-project-creation", "mobile"],
                      "browser_errors": errors, "project_id": projects[0]["id"], "real_model_calls": 0, "gpu_calls": 0}
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--chromium", default="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")
    parser.add_argument("--server", type=int)
    parser.add_argument("--unconfigured", action="store_true")
    parser.add_argument("--inline-fixtures", action="store_true")
    arguments = parser.parse_args()
    directory = arguments.evidence_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if arguments.server:
        serve(directory, arguments.server, not arguments.unconfigured, arguments.inline_fixtures)
    else:
        key = directory / "browser.key"
        key.write_text("script-browser-test")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {**os.environ, "H3_STUDIO_KEY_FILE": str(key), "H3_STUDIO_TAILSCALE_USERS": ""}
        with (directory / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--evidence-dir", str(directory), "--server", str(port),
                                        *(["--unconfigured"] if arguments.unconfigured else [])], env=environment, stdout=log, stderr=log)
            try:
                checks(f"http://127.0.0.1:{port}", directory, arguments.chromium, not arguments.unconfigured)
            finally:
                process.terminate()
                process.wait(timeout=15)
