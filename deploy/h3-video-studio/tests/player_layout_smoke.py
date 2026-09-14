import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
            headless=True, args=["--no-sandbox"],
        )
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def readonly(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                errors.append("Unexpected mutation: " + route.request.method)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", readonly)
        for name, viewport in (("desktop", {"width": 1700, "height": 1212}),
                               ("mobile", {"width": 390, "height": 844})):
            page.set_viewport_size(viewport)
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_function("document.getElementById('stageVideo').videoHeight > 0")
            geometry = page.locator("#stageVideo").evaluate("""video => {
                video.pause();
                return {video: video.getBoundingClientRect().toJSON(),
                    container: video.parentElement.getBoundingClientRect().toJSON(),
                    width: video.videoWidth, height: video.videoHeight,
                    fit: getComputedStyle(video).objectFit};
            }""")
            video, container = geometry["video"], geometry["container"]
            assert geometry["fit"] == "contain", geometry
            assert video["width"] > 0 and video["height"] > 0, geometry
            for edge in ("top", "left"):
                assert video[edge] >= container[edge] - 1, geometry
            for edge in ("bottom", "right"):
                assert video[edge] <= container[edge] + 1, geometry
            page.locator(".player-area").screenshot(path=str(args.evidence_dir / f"{name}.png"))
            results.append({"viewport": name, **geometry})
        assert not errors, errors
        browser.close()
    (args.evidence_dir / "report.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == "__main__":
    main()
