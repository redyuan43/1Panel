from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.output_dir.resolve()
    if str(root).startswith(("/tmp/", "/var/tmp/")):
        raise ValueError("Production evidence requires persistent storage")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"started_at": time.time(), "status": "preflight", "projects": [], "page_errors": []}
    if (root / "production-ui.json").exists():
        if not args.resume:
            raise ValueError("Existing execution evidence must be resumed, not overwritten")
        report = json.loads((root / "production-ui.json").read_text())
        if any(record.get("execution_id") or record["status"] == "submitted" for record in report["projects"]):
            raise ValueError("Existing submissions must be reconciled without another generation POST")
        report.pop("error", None)

    def normalized(prompt):
        return prompt.replace("\r\n", "\n").replace("\r", "\n").strip()

    def save():
        temporary = root / "production-ui.part"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(root / "production-ui.json")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
            headless=True, args=["--no-sandbox"],
        )
        context = browser.new_context(viewport={"width": 1700, "height": 1212})

        def read(path):
            response = context.request.get(args.url.rstrip("/") + path)
            if not response.ok:
                raise RuntimeError(f"Read failed: {path}: {response.status}")
            return response.json()

        try:
            capacity = read("/api/capacity")
            if capacity.get("exclusive_window") or capacity.get("studio_preview", {}).get("available", 0) < 3:
                raise RuntimeError("Three admitted full-duration preview slots are required")
            coser = read("/api/projects/e5364e7e9e14")
            dance = read("/api/projects/098326acfce5")
            cases = [
                ("并行验收 · CCD Coser 原提示词", "portrait", coser["prompt_original"]),
                ("并行验收 · 舞台舞蹈", "landscape", dance["prompt_approved"] or dance["prompt_original"]),
                ("并行验收 · 桌面小火车", "portrait", "integrated_multimodal_description: A red toy train slowly travels across a wooden tabletop, a continuous wide shot, stable soft daylight, consistent train and tracks. overall_soundscape: Quiet wheel sounds. non_diegetic_music: None."),
            ]
            pages = []
            report["status"] = "preparing"
            save()
            for index, (name, orientation, prompt) in enumerate(cases):
                page = context.new_page()
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                if index < len(report["projects"]):
                    record = report["projects"][index]
                    project = read("/api/projects/" + record["id"])
                    assert project["name"] == name and normalized(project["prompt_original"]) == normalized(prompt)
                    assert project["stages"]["preview"]["status"] == "pending"
                    page.goto(args.url.rstrip("/") + "/?project=" + record["id"] + "&stage=context_ir", wait_until="networkidle")
                else:
                    page.goto(args.url, wait_until="networkidle")
                    page.locator("#projectName").fill(name)
                    page.locator("#promptInput").fill(prompt)
                    page.locator("#orientationInput").select_option(orientation)
                    page.locator("#promptProcessingInput").select_option("manual")
                    assert page.locator("#durationInput").input_value() == "15"
                    with page.expect_response(lambda response: urlparse(response.url).path == "/api/projects"
                                              and response.request.method == "POST") as created:
                        page.locator("#createButton").click()
                    if not created.value.ok:
                        raise RuntimeError("Project creation failed; do not retry automatically")
                    project = created.value.json()
                    record = {"id": project["id"], "name": name, "orientation": orientation, "status": "created",
                              "url": args.url.rstrip("/") + "/?project=" + project["id"] + "&stage=preview"}
                    report["projects"].append(record)
                record["prompt_sha256_lf"] = hashlib.sha256(normalized(prompt).encode()).hexdigest()
                save()
                if project["stages"]["context_ir"]["status"] != "approved":
                    page.get_by_role("button", name="确认提示词", exact=True).wait_for(timeout=30000)
                    assert normalized(page.locator("#optimizedPrompt").input_value()) == normalized(prompt)
                    with page.expect_response(lambda response: response.url.endswith("/context-ir/approve")) as approved:
                        page.get_by_role("button", name="确认提示词", exact=True).click()
                    if not approved.value.ok:
                        raise RuntimeError("Prompt approval failed")
                page.locator('[data-stage="preview"]').click()
                page.get_by_role("button", name="开始低清预览", exact=True).wait_for()
                pages.append((page, record))
            for page, record in pages:
                with page.expect_response(lambda response: response.url.endswith("/stages/preview/start")) as started:
                    page.get_by_role("button", name="开始低清预览", exact=True).click()
                if not started.value.ok:
                    raise RuntimeError("Preview start failed; retain existing IDs and do not retry")
                record.update(status="submitted", submitted_at=time.time())
                save()
                page.screenshot(path=str(root / (record["id"] + "-submitted.png")), full_page=True)
            for record in report["projects"]:
                current = read("/api/projects/" + record["id"])
                record["execution_id"] = current["stages"]["preview"].get("execution_id")
                if not record["execution_id"]:
                    raise RuntimeError("Execution identity is not yet available; reconcile the existing project")
            report["status"] = "submitted_waiting_for_real_outputs"
        except Exception as error:
            report.update(status="failed", error=str(error))
            raise
        finally:
            report["updated_at"] = time.time()
            save()
            browser.close()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
