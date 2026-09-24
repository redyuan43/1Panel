// Isolated DOM fixture: no production HTTP endpoints or credentials.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs/promises");
const path = require("node:path");
const assert = require("node:assert/strict");
(async()=>{
  const root = path.resolve(__dirname,"../ai_router/static");
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[];page.on("pageerror",e=>errors.push(String(e)));
  await page.route("**/*",route=>route.abort());
  try {
    let html = await fs.readFile(path.join(root,"index.html"),"utf8");
    html = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,"").replace(/<link[^>]+>/g,"");
    await page.setContent(html);
    for (const file of ["style.css", "routing-modes.css", "image-policy.css"]) await page.addStyleTag({path:path.join(root,file)});
    await page.evaluate(()=>{document.querySelectorAll(".view").forEach(node=>node.classList.remove("active"));document.getElementById("settings-view").classList.add("active");});
    await page.addScriptTag({path:path.join(root,"image-policy.js")});
    await page.evaluate(()=>ImagePolicyUI.render({}, async()=>({data:[]})));
    await page.click('[data-policy-kind="image"]');
    assert(await page.locator("#image-policy-panel").isVisible());
    assert(await page.locator("#image-policy-refresh").isVisible());
    assert(await page.locator("#policy-validate").isVisible());
    assert(!(await page.locator("#text-policy-panel").isVisible()));
    assert.equal(await page.locator("#image-policy-when_busy").inputValue(),"cloud");
    await page.locator("#image-policy-allow_cloud").uncheck();
    assert((await page.locator("#image-policy-summary").textContent()).includes("云端已禁止"));
    assert.equal(await page.evaluate(()=>ImagePolicyUI.collect().allow_cloud),false);
    await page.locator("#image-policy-default_route").selectOption("cloud_only");
    assert((await page.evaluate(()=>ImagePolicyUI.validate(ImagePolicyUI.collect()))).length>0);
    await page.locator("#image-policy-refresh").click();
    await page.waitForFunction(()=>document.getElementById("image-policy-resources").textContent.includes("尚未登记"));
    await page.evaluate(()=>ImagePolicyUI.render({}, async()=>({data:[]})));
    const out=process.env.UI_TEST_OUTPUT;
    if(out){await fs.mkdir(out,{recursive:true});await page.screenshot({path:path.join(out,"image-policy.png"),fullPage:true});}
    assert.deepEqual(errors,[]);
    console.log("Image policy DOM, defaults, permissions and resource display passed.");
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
