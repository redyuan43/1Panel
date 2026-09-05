// This runner refuses to mutate anything except the isolated fixture server.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert=require("node:assert/strict");
const fs=require("node:fs/promises");
const path=require("node:path");
const base=process.env.UI_PREVIEW_URL || "http://127.0.0.1:14822";
const output=process.env.UI_TEST_OUTPUT || "/tmp/router-media-browser";
(async()=>{
  assert.equal(new URL(base).hostname,"127.0.0.1");
  assert.equal((await fetch(base+"/__ui_preview__").then(r=>r.json())).isolated,true);
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on("pageerror",error=>errors.push(String(error)));
  try{
    await page.goto(base+"/media");
    await page.locator("#key").fill("ui-preview-only");await page.locator("#login button").click();
    await page.waitForSelector("#settings-fields input",{state:"attached"});
    await page.locator('#image-form [name=prompt]').fill("Media fixture");
    await page.locator("#image-form [type=submit]").click();
    await page.waitForSelector("#detail-output img");
    assert(await page.locator("#detail-output img").evaluate(node=>node.naturalWidth===32));
    await page.locator('[data-view=videos]').click();
    await page.locator('#video-form [name=prompt]').fill("Fixture scene");
    await page.locator("#cloud-consent").check();await page.locator("#video-form [type=submit]").click();
    await page.waitForSelector('[data-draft]');
    await page.locator("[data-draft]").fill("An edited approval");
    await page.waitForTimeout(5500);
    assert.equal(await page.locator("[data-draft]").inputValue(),"An edited approval");
    await page.locator('[data-stage=context_ir][data-action=approve]').click();
    await page.waitForFunction(()=>document.querySelector('[data-stage=preview][data-action=start]')?.disabled===false);
    assert.equal(await page.locator("#stages video").count(),0);
    await page.locator('[data-stage=preview][data-action=start]').click();
    await page.waitForSelector("#stages video");
    await page.waitForFunction(()=>document.querySelector("#stages video").readyState>=1);
    assert(await page.locator("#stages video").evaluate(node=>node.videoWidth===320));
    await page.locator('[data-stage=preview][data-action=approve]').click();
    await page.waitForFunction(()=>document.querySelector("#detail-meta").textContent.includes("已完成"));
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await page.screenshot({path:path.join(output,`media-${width}.png`),fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log("PASS image artifact, staged video, approval/start separation, draft persistence, playback, desktop/mobile layout");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
