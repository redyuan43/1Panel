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
    assert.equal(await page.locator('#video-form [name=workflow_mode]').inputValue(),"quality_gate");
    assert.equal(await page.locator('#video-form [name=creative_profile]').count(),1);
    assert.equal(await page.locator('#video-form [name=aspect_ratio]').count(),1);
    await page.locator("#key").fill("ui-preview-only");await page.locator("#login button").click();
    await page.waitForSelector("#settings-fields input",{state:"attached"});
    assert.equal(await page.locator('#video-form [name=workflow_mode]').inputValue(),"quality_gate");
    assert.equal(await page.locator('#video-form [name=workflow_mode]').isDisabled(),false);
    assert.equal(await page.locator("#video-compat").isVisible(),false);
    await page.locator('#image-form [name=prompt]').fill("Media fixture");
    await page.locator("#image-form [type=submit]").click();
    await page.waitForSelector("#detail-output img");
    assert(await page.locator("#detail-output img").evaluate(node=>node.naturalWidth===32));
    await page.locator('[data-view=videos]').click();
    await page.locator('#video-form [name=workflow_mode]').selectOption("legacy_pipeline");
    await page.locator('#video-form [name=prompt]').fill("Fixture scene");
    await page.locator("#cloud-consent").check();await page.locator("#video-form [type=submit]").click();
    await page.waitForSelector('[data-draft]');
    await page.locator("[data-draft]").fill("An edited approval");
    await page.waitForTimeout(5500);
    assert.equal(await page.locator("[data-draft]").inputValue(),"An edited approval");
    await page.locator('[data-stage=context_ir][data-action=approve]').click();
    await page.waitForFunction(()=>document.querySelector('[data-stage=preview][data-action=start]')?.disabled===false);
    assert.equal(await page.locator("#stages video").count(),0);
    let regeneratePayload=null;
    await page.route(/\/api\/media\/videos\/[^/]+(?:\/.*)?$/,async route=>{
      const request=route.request();
      if(request.method()==="POST"&&new URL(request.url()).pathname.endsWith("/regenerate")){
        regeneratePayload=JSON.parse(request.postData()||"{}");
        await route.fulfill({status:202,contentType:"application/json",body:JSON.stringify({id:"vid_fixture",status:"in_progress"})});
        return;
      }
      if(request.method()!=="GET"){await route.continue();return;}
      const response=await route.fetch();
      const value=await response.json();
      for(const stage of value.stages||[]){
        if(stage.output?.content_type==="video/mp4")stage.output.review={
          review_id:"rev_fixture",
          semantic:{
            verdict:"CONDITIONAL_PASS",confidence:0.86,
            scores:{identity:88,motion:74},
            issues:[{start_seconds:0.4,end_seconds:0.7,severity:"warning",message:"边缘轻微抖动"}],
            revised_prompt:"保持主体轮廓稳定，减少边缘抖动。",
          },
        };
      }
      await route.fulfill({status:response.status(),headers:response.headers(),contentType:"application/json",body:JSON.stringify(value)});
    });
    await page.locator('[data-stage=preview][data-action=start]').click();
    await page.waitForSelector("#stages video");
    await page.waitForFunction(()=>document.querySelector("#stages video").readyState>=1);
    assert(await page.locator("#stages video").evaluate(node=>node.videoWidth===320));
    await page.waitForSelector(".review");
    assert((await page.locator(".review").innerText()).includes("0.4–0.7s"));
    assert((await page.locator(".review").innerText()).includes("保持主体轮廓稳定"));
    const reviewedOutput=await page.locator('[data-stage=preview][data-action=regenerate]').getAttribute("data-output");
    page.once("dialog",dialog=>dialog.accept());
    await page.locator('[data-stage=preview][data-action=regenerate]').click();
    await page.waitForFunction(()=>document.querySelector('[data-stage=preview][data-action=regenerate]')?.disabled===false);
    assert.deepEqual(regeneratePayload,{output_id:reviewedOutput,review_id:"rev_fixture",apply_suggestion:true});
    await page.locator('[data-stage=preview][data-action=approve]').click();
    await page.waitForFunction(()=>document.querySelector("#detail-meta").textContent.includes("已完成"));
    for(const width of [1440,768,390]){
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await page.screenshot({path:path.join(output,`media-${width}.png`),fullPage:true});
    }
    assert.deepEqual(errors,[]);
    console.log("PASS legacy options fallback, workflow controls, review/regenerate binding, approval/start separation, media playback, desktop/mobile layout");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
