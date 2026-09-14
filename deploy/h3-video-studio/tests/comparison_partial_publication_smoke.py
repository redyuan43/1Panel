import argparse
import datetime
import hashlib
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


PROTECTED = {
    "R0": "0eff3c5bc4a053f35cbf8e90d6507a3f6834536dbb0e01eba989defe719022d1",
    "A4": "cf884a83c5c8b6c6bf010f020ae3590ebe850b1379963a1ae510983337ca7b89",
    "A8": "9f0c051d29a85bcef3bb8077d2704710b48e0715d08559db69366da99b84b04e",
    "B8": "aafa318836c47f64e4ec2018518f8216502ff057e097ec1e8dfa8df8df5c1d0a",
    "C0": "d2700faf4c60fada6d153418bf1a3a189b0ba02169b008990390d136eec304d0",
    "C1": "c3cbc6e818c442cbb2210a313e78b0410bc63face9e11e1daacbd2704d1a1be9",
    "D4": "8eef0b2c9d66c486741ec511a20bcdfbbefd573d3ee508f34c2b4252fa791d1b",
    "A4_C05": "5ab3eccc9fd3815b4e52a30551c260247cb774003baebdb37a11313da55a23a8",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--case", choices=("A4_C1", "A4_C0"), default="A4_C1")
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    errors, responses, geometry = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/home/ai/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome",
            headless=True, args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 1700, "height": 1212})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: responses.append({"url": response.url, "status": response.status}))

        def readonly(route):
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                errors.append("Unexpected mutation: " + route.request.method)
                route.abort()
            else:
                route.continue_()

        page.route("**/*", readonly)
        response = page.goto(args.url, wait_until="domcontentloaded")
        assert response.status == 200
        card = page.locator(f'article[data-case="{args.case}"]')
        video = card.locator("video")
        video.wait_for()
        page.wait_for_function("""() => document.querySelector('article[data-case="A4_C1"] .run-state')
            .textContent.includes('本条成片已保留；所属并发批次失败')""")
        initial_state = card.locator(".run-state").inner_text()
        metadata = page.evaluate("""async () => {
            const response = await fetch('comparison-results/index.json', {cache:'no-store'});
            return await response.json();
        }""")
        protected_records = [record for record in metadata["cases"] if record["id"] in PROTECTED]
        canonical = json.dumps(protected_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == "e0a834cdd77c84d649df03643a6142dbd5bbcccb741c780ac3d786d8423aed1a"
        published = next(record for record in metadata["cases"] if record["id"] == args.case)
        assert published["prompt_id"] == {"A4_C1": "f5feede7-b73c-4a1d-8bb1-62c1de08ab81",
                                          "A4_C0": "c238c443-0560-4548-a7ec-4cae57cfc65a"}[args.case]
        root = Path(__file__).resolve().parents[1] / "frontend/comparison-results"
        actual_hashes = {case: hashlib.sha256((root / (case + ".mp4")).read_bytes()).hexdigest() for case in PROTECTED}
        assert actual_hashes == PROTECTED
        assert hashlib.sha256((root / (args.case + ".mp4")).read_bytes()).hexdigest() == published["artifact_sha256"]
        awaitable_play = """async video => {
            video.muted = true; video.currentTime = 0; await video.play();
        }"""
        video.evaluate(awaitable_play)
        page.wait_for_function("""caseId => document.querySelector(`article[data-case="${caseId}"] video`).currentTime > 1""", arg=args.case)
        for name, viewport in (("desktop", {"width": 1700, "height": 1212}),
                               ("mobile", {"width": 390, "height": 844})):
            page.set_viewport_size(viewport)
            video.scroll_into_view_if_needed()
            detail = video.evaluate("""video => ({width:video.videoWidth, height:video.videoHeight,
                duration:video.duration, currentTime:video.currentTime, paused:video.paused,
                error:video.error?.message || null, fit:getComputedStyle(video).objectFit,
                box:video.getBoundingClientRect().toJSON(), parent:video.parentElement.getBoundingClientRect().toJSON(),
                pageWidth:document.documentElement.scrollWidth, viewportWidth:innerWidth})""")
            assert detail["width"] == 480 and detail["height"] == 864 and detail["fit"] == "contain"
            assert detail["error"] is None and not detail["paused"] and abs(detail["duration"] - 362 / 24) < 0.1
            assert detail["box"]["height"] <= viewport["height"] and detail["pageWidth"] <= detail["viewportWidth"]
            for edge in ("left", "top"):
                assert detail["box"][edge] >= detail["parent"][edge] - 1
            for edge in ("right", "bottom"):
                assert detail["box"][edge] <= detail["parent"][edge] + 1
            geometry.append({"viewport": name, **detail})
            card.screenshot(path=str(args.evidence_dir / (name + "-" + args.case + ".png")))
        page.wait_for_function("""caseId => document.querySelector(`article[data-case="${caseId}"] video`).ended""", arg=args.case, timeout=30000)
        ended = video.evaluate("video => ({ended:video.ended,currentTime:video.currentTime,error:video.error?.message || null})")
        assert ended["ended"] and ended["error"] is None
        live = page.evaluate("""async () => (await fetch('comparison-results/live.json', {cache:'no-store'})).json()""")
        cards = page.locator("article").evaluate_all("""articles => articles.map(article => ({
            case:article.dataset.case, status:article.querySelector('.run-state').textContent,
            video:article.querySelector('video')?.getAttribute('src') || null,
        }))""")
        if args.final:
            assert len(metadata["cases"]) == len(cards) == page.locator("article video").count() == 10
            assert len({entry["case"] for entry in cards}) == 10
            page.wait_for_function("""() => [...document.querySelectorAll('article video')]
                .every(video => video.videoWidth > 0 && video.videoHeight > 0 && !video.error)""")
            assert live["available"] and not live["running_ids"]
            assert all(entry["status"] in {"completed", "not_run"} for entry in live["cases"])
            replica = next(entry for entry in live["cases"] if entry["id"] == "A4_C05")
            assert replica["status"] == "not_run" and replica["submit_attempted"] is False and replica["started_at"] is None
            replica_card = next(entry for entry in cards if entry["case"] == "A4_C05")
            assert "本轮未提交、未运行" in replica_card["status"] and "本卡预览仍为原成片" in replica_card["status"]
            assert replica_card["video"] == "comparison-results/A4_C05.mp4"
            retained = next(entry for entry in cards if entry["case"] == "A4_C1")
            assert "本条成片已保留；所属并发批次失败" in retained["status"]
            page.set_viewport_size({"width": 1700, "height": 1212})
            page.screenshot(path=str(args.evidence_dir / "final-ten-cards.png"), full_page=True)
            page.locator('article[data-case="A4_C05"]').screenshot(path=str(args.evidence_dir / "C05-not-run-old-video.png"))
        assert not errors, errors
        report = {"observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "url": page.url,
                  "http_status": response.status, "card_count": page.locator("article").count(),
                  "manifest_count": len(metadata["cases"]), "state": initial_state, "published": published,
                  "protected_video_hashes": actual_hashes, "protected_records_unchanged": True,
                  "geometry": geometry, "playback": ended, "responses": responses, "page_errors": errors,
                  "live": live, "cards": cards,
                  "quality_review_performed": False}
        (args.evidence_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        browser.close()
    print(json.dumps({key: value for key, value in report.items() if key not in ("responses", "protected_video_hashes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
