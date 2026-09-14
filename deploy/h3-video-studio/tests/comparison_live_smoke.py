import argparse
import datetime
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
            headless=True, args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 1700, "height": 1212})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def readonly(route):
            requests.append({"method": route.request.method, "url": route.request.url})
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                errors.append("Unexpected mutation: " + route.request.method)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", readonly)
        response = page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("""() => {
            const card = document.querySelector('article[data-case="A4_C1"] .run-state');
            return card && card.textContent.includes('GPU-');
        }""", timeout=20000)
        execution = page.locator("#execution").inner_text()
        cards = page.locator("article").evaluate_all("""articles => articles.map(article => ({
            case: article.dataset.case,
            status: article.querySelector('.run-state').textContent,
            video: article.querySelector('video')?.getAttribute('src') || null,
        }))""")
        payload = page.evaluate("""async () => {
            const response = await fetch('comparison-results/live.json', {cache: 'no-store'});
            return {status: response.status, body: await response.json()};
        }""")
        page.screenshot(path=str(args.evidence_dir / "comparison-full.png"), full_page=True)
        page.locator('article[data-case="A4_C05"]').scroll_into_view_if_needed()
        page.screenshot(path=str(args.evidence_dir / "comparison-new-cases.png"))
        for case in ("A4_C1", "A4_C0", "A4_C05"):
            page.locator(f'article[data-case="{case}"]').screenshot(path=str(args.evidence_dir / f"{case}.png"))
        report = {"observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "url": page.url, "http_status": response.status, "execution": execution,
                  "cards": cards, "live_http": payload, "page_errors": errors, "requests": requests}
        (args.evidence_dir / "dom-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        assert response.status == 200 and payload["status"] == 200
        assert payload["body"]["available"] is True
        assert not errors, errors
        replica = next(card for card in cards if card["case"] == "A4_C05")
        assert "本卡预览仍为原成片" in replica["status"]
        assert replica["video"] == "comparison-results/A4_C05.mp4"
        browser.close()
    print(json.dumps({key: report[key] for key in ("observed_at", "url", "http_status", "execution", "cards", "page_errors")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
