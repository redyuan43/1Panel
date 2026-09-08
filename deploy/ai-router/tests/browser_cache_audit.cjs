// Isolated CPU fixtures only; never point this test at the production console.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const base=process.env.UI_PREVIEW_URL||'http://127.0.0.1:14802';
const output=process.env.UI_TEST_OUTPUT||path.resolve('browser-artifacts/cache-audit');
(async()=>{
  assert.equal(new URL(base).hostname,'127.0.0.1');
  assert.equal((await fetch(base+'/__ui_preview__').then(r=>r.json())).isolated,true);
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}:{})});
  const page=await browser.newPage({viewport:{width:1600,height:1100}});
  const errors=[],checks=[];let contentReads=0;
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('request',r=>{if(r.url().includes('/content'))contentReads++;});
  await page.addInitScript(()=>sessionStorage.setItem('ai-router-admin-key','ui-preview-only'));
  try{
    await page.goto(base);await page.waitForFunction(()=>state.dashboard&&state.settings);
    await page.locator('#auto-refresh').uncheck();
    await page.locator('[data-view="audit"]').click();
    await page.evaluate(()=>selectRouteTrace('preview-local'));
    await page.waitForSelector('#audit-pipeline .pipeline-node');
    assert.equal(await page.locator('#audit-pipeline .pipeline-node').count(),6);
    assert.equal(await page.locator('#audit-routing-detail').isVisible(),false);
    assert.equal(contentReads,0);checks.push('six stages; details and original content remain collapsed');
    await page.screenshot({path:path.join(output,'conversation.png'),fullPage:true});
    await page.locator('[data-pipeline-stage="execution"]').click();
    assert.match(await page.locator('#audit-stage-values').innerText(),/本次尝试总 prefill/);
    assert.match(await page.locator('#audit-stage-values').innerText(),/986/);
    assert.match(await page.locator('#audit-stage-values').innerText(),/7.29/);
    await page.evaluate(()=>selectRouteTrace('preview-local',{silent:true}));
    assert.equal(await page.evaluate(()=>cacheView.stage),'execution');checks.push('measured prime and prompt counts; selection survives refresh');
    await page.screenshot({path:path.join(output,'execution.png'),fullPage:true});
    await page.locator('[data-pipeline-stage="routing"]').click();
    await page.waitForSelector('#trace-graph svg');
    assert.equal(await page.locator('#trace-graph-viewport').isVisible(),true);checks.push('existing complete route graph still accessible');
    await page.locator('[data-pipeline-stage="content"]').click();
    assert.equal(contentReads,0);
    await page.locator('#cache-load-content').click();
    await page.waitForSelector('#cache-compare');
    await page.locator('#cache-compare').click();
    await page.waitForFunction(()=>document.querySelector('#cache-text-left').textContent.length>0);
    assert.match(await page.locator('#cache-text-left').innerText(),/CPU fixture/);
    assert.equal(await page.evaluate(()=>window.promptInjected),undefined);checks.push('on-demand archive and untrusted content displayed as text');
    await page.locator('#cache-more-left').click();
    await page.waitForFunction(()=>document.querySelector('#cache-text-left').textContent.length>16384);
    const before=await page.locator('#cache-text-left').innerText();
    assert(before.length>16384);checks.push('long Unicode content is paginated without loss');
    await page.locator('#cache-stage-left').selectOption('workbuddy_reordered');
    assert.equal(await page.locator('#cache-more-left').isVisible(),false);
    assert.equal(await page.locator('#cache-text-left').innerText(),'');checks.push('stage changes cannot append another document to old text');
    await page.locator('#cache-compare').click();
    await page.waitForFunction(()=>document.querySelector('#cache-text-left').textContent.length>0);
    const reads=contentReads;await page.evaluate(()=>selectRouteTrace('preview-local',{silent:true}));
    assert.equal(contentReads,reads);assert.match(await page.locator('#cache-text-left').innerText(),/CPU fixture/);checks.push('refresh preserves loaded content and never reloads originals');
    await page.screenshot({path:path.join(output,'content.png'),fullPage:true});
    await page.locator('[data-audit-view="overview"]').click();
    await page.locator('#cache-client-group').selectOption('');await page.locator('#cache-client').fill('');await page.locator('#cache-filter-form button[type="submit"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#cache-request-rows [data-cache-request]').length>0);
    await page.waitForFunction(()=>!cacheView.loading);
    assert.equal(await page.locator('#cache-metrics .cache-metric').count(),9);
    assert.match(await page.locator('#cache-overview-state').innerText(),/有效样本/);checks.push('summary coverage, percentiles, charts and request list');
    await page.screenshot({path:path.join(output,'overview.png'),fullPage:true});
    await page.locator('#cache-conversation').fill('preview-conversation');await page.locator('#cache-filter-form button[type="submit"]').click();
    await page.waitForFunction(()=>!cacheView.loading&&cacheView.rows.size===4);
    await page.locator('[data-cache-request="preview-local"]').click();
    assert.equal(await page.locator('#audit-conversation').isVisible(),true);checks.push('statistics return to shared conversation detail');
    for (const [kind,label] of [['hit','已命中'],['miss','未命中'],['unknown','数据不足'],['estimated','仅有估算'],['running','执行中']]) {
      await page.evaluate(id=>selectRouteTrace(id),'preview-ai-'+kind);
      await page.locator('[data-pipeline-stage="execution"]').click();
      assert.match(await page.locator('.cache-verdict').innerText(),new RegExp(label));
      assert.equal(await page.locator('[data-cache-detail="native"]').getAttribute('open'),null);
      if (kind==='hit') {
        const text=await page.locator('#audit-stage-values').innerText();
        assert.match(text,/42,000/);assert.match(text,/4,768/);assert.match(text,/89.8%/);
        await page.locator('[data-cache-detail="native"] summary').click();
        await page.evaluate(()=>selectRouteTrace('preview-ai-hit',{silent:true}));
        assert.notEqual(await page.locator('[data-cache-detail="native"]').getAttribute('open'),null);
        await page.screenshot({path:path.join(output,'ai-hit.png'),fullPage:true});
      }
    }
    checks.push('AI hit/miss/unknown/estimated/running verdicts, exact counts and persistent disclosure');
    await page.setViewportSize({width:720,height:1000});await page.screenshot({path:path.join(output,'compact.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    await fs.writeFile(path.join(output,'result.json'),JSON.stringify({checks,errors},null,2));
    console.log(JSON.stringify({checks:checks.length,errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
