from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1] / "frontend"


def main():
    parser = argparse.ArgumentParser(description="Offline real-browser recipe checks; all requests are intercepted.")
    parser.add_argument("--chromium", default="/usr/bin/google-chrome")
    args = parser.parse_args()
    catalog = {"enabled": False, "default_recipe_id": "A4", "recipes": [
        {"recipe_id": identifier, "label": identifier, "version": "offline-v1"}
        for identifier in ("A4", "A4_C0", "A4_C1", "B8")]}
    runtime = {"label": "离线夹具", "runner": "未运行GPU", "billing": "无"}
    context = {"id": "context_ir", "label": "Context IR", "status": "approved", "runtime": runtime}
    preview = {"id": "preview", "label": "低清预览", "status": "pending", "runtime": runtime}
    project = {"id": "legacy", "name": "离线历史项目", "mode": "t2v", "duration": 15,
        "actual_duration": 362 / 24, "orientation": "portrait", "strategy": "fast",
        "audio_policy": "native", "seed": 20260910, "prompt_original": "原提示词不能覆盖",
        "prompt_approved": "共同IR", "prompt_ir": "共同IR", "assets": {},
        "stages": {"context_ir": context, "preview": preview}, "pipeline": [context, preview]}
    requests, errors = [], []

    def intercept(route):
        parsed = urlsplit(route.request.url)
        if parsed.netloc != "studio.offline":
            route.abort()
            return
        path = parsed.path
        if route.request.method != "GET":
            requests.append({"path": path, "body": route.request.post_data_json})
            route.fulfill(json=project)
            return
        responses = {"/api/recipes": catalog, "/api/health": {"ok": True, "execution_mode": "preview"},
            "/api/capacity": {"available": False}, "/api/projects": {"projects": []},
            "/api/projects/legacy": project, "/api/768-queue/candidates": {"candidates": []},
            "/api/768-queue/schedules": {"schedules": []},
            "/api/script/options": {"capability_manifest": {"configured": False}, "skills": []}}
        if path in responses:
            route.fulfill(json=responses[path])
        elif path.startswith("/api/"):
            route.fulfill(status=503, json={"detail": "offline fixture"})
        else:
            local = (ROOT / (path.lstrip("/") or "index.html")).resolve()
            if local.is_relative_to(ROOT) and local.is_file():
                route.fulfill(body=local.read_bytes(), content_type=mimetypes.guess_type(local.name)[0] or "application/octet-stream")
            else:
                route.fulfill(status=404, body="offline")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox", "--disable-gpu"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, service_workers="block")
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", intercept)
        page.goto("http://studio.offline/", wait_until="networkidle")
        page.locator("#orientationInput").select_option("portrait")
        expect(page.locator("#recipeInput")).to_be_enabled()
        expect(page.locator("#recipeInput")).to_have_value("A4")
        assert page.locator("#recipeInput option").count() == 4
        page.locator("#promptInput").fill("不被配方覆盖的原始人物描述")
        page.locator("#recipeInput").select_option("A4_C1")
        expect(page.locator("#recipeHint")).to_contain_text("不指定或替换为新人物")
        expect(page.locator("#recipeHint")).to_contain_text("配方调度未就绪")
        expect(page.locator("#promptInput")).to_have_value("不被配方覆盖的原始人物描述")
        page.locator("#durationInput").fill("5")
        expect(page.locator("#recipeInput")).to_be_disabled()
        page.locator("#durationInput").fill("15")
        catalog["enabled"] = True
        page.evaluate("refreshRecipes()")
        page.evaluate("openProject('legacy', 'preview')")
        select = page.get_by_role("combobox", name="本次生成配方")
        expect(select).to_have_value("")
        page.get_by_role("button", name="开始低清预览").click()
        expect(page.locator("#commonStageError")).to_contain_text("显式选择")
        assert not requests
        select.select_option("B8")
        page.get_by_role("button", name="开始低清预览").click()
        page.wait_for_function("state.project.id === 'legacy'")
        assert requests == [{"path": "/api/projects/legacy/stages/preview/start",
                            "body": {"new_seed": False, "recipe_id": "B8"}}]
        page.evaluate("state.project.pipeline[1].execution = {recipe_id:'B8', recipe_version:'v1', backend_id:'offline-vdn', runtime_version:'offline-r2', gpu_uuid:'GPU-fixture', execution_seconds:789.079}; renderStage()")
        expect(page.locator("#stageMetrics")).to_contain_text("GPU-fixture")
        expect(page.locator("#stageFacts")).to_contain_text("VDN 8")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert not errors, errors
        browser.close()
    print(json.dumps({"passed": True, "external_requests": 0, "gpu_inference": False,
                      "mocked_stage_requests": requests}, ensure_ascii=False))


if __name__ == "__main__":
    main()
