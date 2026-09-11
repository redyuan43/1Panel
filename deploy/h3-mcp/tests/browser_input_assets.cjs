"use strict";
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--disable-gpu"]});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 900}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const requests = [];
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "http://h3.test");
      requests.push(url.pathname);
      if (url.pathname.startsWith("/api/projects/")) return route.fulfill({status: 404});
      const name = path.basename(url.pathname);
      if (["input-assets.js", "input-assets.css"].includes(name)) return route.fulfill({body: await fs.readFile(path.join(__dirname, "../frontend", name)), contentType: name.endsWith(".js") ? "text/javascript" : "text/css"});
      return route.fulfill({contentType: "text/html; charset=utf-8", body: '<html><head><meta charset="utf-8"><link rel="stylesheet" href="/input-assets.css"></head><body><div id="stageMetrics"></div><script src="/input-assets.js"></script></body></html>'});
    });
    await page.goto("http://h3.test/");
    const project = {id: "abc123", mode: "i2v", input_sha256: "first", connector_owner: "owner", connector_generation_enabled: false,
      execution_profile: {family: "FL2VA", steps: 14, profile_id: "H3_I2V_QUALITY14"},
      input_assets: [{kind: "first_frame", asset_id: "asset_first", sha256: "first-sha", name: "photo.png", preview_url: "/api/projects/abc123/input-assets/first_frame"}]};
    const render = value => page.evaluate(detail => window.dispatchEvent(new CustomEvent("h3-project-rendered", {detail})), value);
    const firstImage = page.waitForResponse(response => response.url().includes("/abc123/input-assets/first_frame"));
    await render(project);
    await firstImage;
    await page.locator(".input-material-panel img").waitFor();
    assert.match(await page.locator(".input-material-panel").innerText(), /首帧生成/);
    assert.match(await page.locator(".input-material-panel").innerText(), /任务尚未提交，不是 GPU 生成失败/);
    assert.equal(await page.locator("img").evaluate(node => getComputedStyle(node).objectFit), "contain");
    const before = requests.filter(value => value.includes("input-assets/first_frame")).length;
    await render(project);
    assert.equal(requests.filter(value => value.includes("input-assets/first_frame")).length, before);
    await render({...project, id: "def456", input_sha256: "second", input_assets: [{...project.input_assets[0], sha256: "second-sha", preview_url: "/api/projects/def456/input-assets/first_frame"}]});
    assert.equal(await page.locator("img").getAttribute("src"), "/api/projects/def456/input-assets/first_frame");
    assert(!(await page.locator(".input-material-panel").innerText()).includes("first-sha"));
    await render({id: "old123", mode: "t2v", recipe_id: "A4", input_assets: []});
    assert.equal(await page.locator("img").count(), 0);
    assert.match(await page.locator(".input-material-panel").innerText(), /未绑定图片/);
    await render({id: "vid123", mode: "reference", input_assets: [{kind: "reference_video", name: "original.mp4", metadata: {is_cfr_24: false}}]});
    assert.match(await page.locator(".input-material-panel").innerText(), /未自动转换或提交生成/);
    await render({id: "vid123", mode: "reference", input_assets: [{kind: "reference_video", name: "original.mp4", metadata: {
      is_cfr_24: true, frame_count: 360, width: 480, height: 864}}]});
    assert.match(await page.locator(".input-material-panel").innerText(), /原件直接输入.*360帧.*解码内存单独准入/);
    assert.deepEqual(errors, []);
    console.log("PASS: 9 checks; input roles, full-frame contain, history switch, no stale media, stable polling, text-only warning, no generation calls, non24 rejection, verified input decode metadata");
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
