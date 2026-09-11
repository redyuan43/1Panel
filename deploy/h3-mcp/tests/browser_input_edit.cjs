"use strict";
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");

async function main() {
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--disable-gpu"]});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, "http://localhost:12345");
      if (url.pathname.endsWith(".js")) return route.fulfill({contentType: "text/javascript", body: await fs.readFile(path.join(__dirname, "../frontend", path.basename(url.pathname)))});
      const inputs = ["name", "prompt", "duration", "orientation", "recipe_id", "prompt_processing", "first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"];
      return route.fulfill({contentType: "text/html", body: `<html><body><form id="projectForm">${inputs.map(name => `<input name="${name}">`).join("")}<input name="seed" id="seedInput"><input id="manualSeedInput"><input name="mode" id="modeInput"><input name="audio_policy" id="audioPolicyInput"></form><div id="formError"></div><textarea id="optimizedPrompt"></textarea><div id="stageMetrics"></div><script>
window.state={};window.calls=[];window.showSetup=()=>window.dispatchEvent(new Event("h3-input-edit-reset"));
window.setActiveButtons=window.renderAssetFields=window.renderCreateRecipes=()=>{};
window.api=async(url,options)=>{window.calls.push({url,options});if(!options){let error=new Error("not found");error.status=404;throw error;}if(url.includes("assets/uploads")){let meta=JSON.parse(options.headers["X-H3-Upload-Metadata"]);return {...meta,state:"ready",asset_id:"asset_uploaded"};}return JSON.parse(options.body);};
</script><script src="/input-assets.js"></script><script src="/input-edit.js"></script></body></html>`});
    });
    await page.goto("http://localhost:12345/");
    const created = await page.evaluate(async () => {
      const form = new FormData();
      for (const [key, value] of Object.entries({mode: "i2v", prompt: "unchanged scene", duration: "15", orientation: "portrait", audio_policy: "native", seed: "123"})) form.set(key, value);
      form.set("first_frame", new File(["source-photo"], "original.png", {type: "image/png"}));
      return window.h3SaveBrowserDraft(form);
    });
    assert.deepEqual(created.assets, {first_frame: "asset_uploaded"});
    assert.equal(created.mode, "i2v");
    assert.equal(created.prompt, "unchanged scene");
    assert.equal(created.recipe_id, undefined);
    const project = {id: "abc123", connector_owner: "owner", connector_revision: "v1", mode: "i2v", audio_policy: "native", duration: 15,
      orientation: "portrait", prompt_ir: "old prompt", prompt_original: "original request", assets: {first_frame: {asset_id: "asset_retained"}},
      input_assets: [{kind: "first_frame", asset_id: "asset_retained"}], stages: {preview: {status: "pending"}}, seed: "2696045020911358409"};
    const render = value => page.evaluate(detail => { window.state.project = detail; window.dispatchEvent(new CustomEvent("h3-project-rendered", {detail})); }, value);
    await render(project);
    await render({...project, connector_revision: "v2", prompt_ir: "new prompt"});
    await page.locator("[data-edit-inputs]").click();
    assert.equal(await page.locator('[name="prompt"]').inputValue(), "new prompt");
    const edited = await page.evaluate(() => window.h3SaveBrowserDraft(new FormData(document.getElementById("projectForm"))));
    assert.equal(edited.expected_revision, "v2");
    assert.deepEqual(edited.assets, {first_frame: "asset_retained"});
    assert.equal(edited.original_prompt, "original request");
    assert.equal(edited.seed, undefined);
    await render({...project, stages: {preview: {status: "running"}}});
    assert(await page.locator("[data-edit-inputs]").isDisabled());
    assert.deepEqual(errors, []);
    assert(!(await page.evaluate(() => window.calls)).some(call => /start_preview|\/prompt$/.test(call.url)));
    console.log("PASS: upload binding, original prompt, latest history revision, retained asset, exact seed preserved, active edit blocked, no inference");
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
