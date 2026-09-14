// Only tests/ui_preview.py: no production endpoints or model calls.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function main() {
  const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:14871';
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const marker = await fetch(base + '/__ui_preview__').then(r => r.json());
  assert.equal(marker.isolated, true);
  const output = process.env.UI_TEST_OUTPUT;
  assert(output, 'UI_TEST_OUTPUT must identify isolated test artifacts');
  await fs.mkdir(output, {recursive:true});
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport:{width:1440, height:1000}});
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await page.route('**/*', route => new URL(route.request().url()).origin === new URL(base).origin
      ? route.continue() : route.abort());
    await page.addInitScript(() => sessionStorage.setItem('ai-router-admin-key', 'ui-preview-only'));
    await page.goto(base);
    await page.waitForFunction(() => state.settings && state.dashboard);
    await page.locator('#auto-refresh').uncheck();
    await page.locator('[data-view="clients"]').click();
    await page.locator('[data-client-edit="1panel"]').click();
    assert.equal(await page.locator('#client-history-owner').count(), 0);
    await page.locator('#client-history-recall').check();
    await page.locator('#client-local-only').uncheck();
    await page.locator('#client-history-cloud').check();
    await page.locator('#client-history-recall').uncheck();
    assert.equal(await page.locator('#client-history-recall').isChecked(), false);
    assert.equal(await page.locator('#client-history-cloud').isChecked(), false);
    await page.locator('#client-history-recall').check();
    assert.equal(await page.locator('#client-history-cloud').isChecked(), false);
    await page.locator('#client-history-status-refresh').click();
    await page.locator('#client-history-status').filter({hasText:'索引尚未建立'}).waitFor();
    await page.route('**/api/clients/1panel/history-memory', route => route.fulfill({json:{
      state:'available', indexing_enabled:true, chunks:12, conversations:3,
      excluded_conversations:0, archive_cursor:80, progress:{caught_up:false,
        remaining_events:120, events_per_second:1, estimated_remaining_seconds:120},
    }}));
    await page.locator('#client-history-status-refresh').click();
    await page.locator('#client-history-status').filter({hasText:'粗估剩余 2 分钟'}).waitFor();
    await page.locator('#client-compaction-refresh').click();
    await page.locator('#client-compaction-jobs').filter({hasText:'暂无后台压缩任务'}).waitFor();
    await page.locator('#client-history-recall').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output, 'context-memory-desktop.png')});
    await page.setViewportSize({width:390, height:844});
    await page.locator('#client-compaction-refresh').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#client-compaction-refresh').isVisible(), true);
    await page.screenshot({path:path.join(output, 'context-memory-mobile.png')});
    await page.keyboard.press('Escape');
    await page.locator('#client-dialog').waitFor({state:'hidden'});
    await page.locator('[data-view="settings"]').click();
    await page.locator('#routing-mode-view').selectOption('advanced');
    await page.locator('#compaction-budget-seconds').scrollIntoViewIfNeeded();
    for (const [id, value] of [['compaction-budget-seconds', '45'], ['compaction-budget-calls', '2'],
                               ['compaction-budget-input', '100000'], ['compaction-budget-output', '16384']]) {
      assert.equal(await page.locator('#' + id).isVisible(), true);
      await page.locator('#' + id).fill(value);
    }
    const limits = await page.evaluate(() => collectSettings().compaction.background_limits);
    assert.deepEqual(limits, {max_seconds:45, max_calls:2, max_input_tokens:100000, max_output_tokens:16384});
    await page.locator('#compaction-budget-calls').fill('33');
    assert(await page.evaluate(() => validateSettingsDraft(collectSettings()).some(error => error.includes('后台压缩预算 max_calls'))));
    await page.locator('#compaction-budget-calls').fill('2');
    await page.locator('#history-index-enabled').uncheck();
    await page.locator('#history-index-records').fill('4');
    await page.locator('#history-index-seconds').fill('0.1');
    await page.locator('#history-index-duty').fill('10');
    assert.deepEqual(await page.evaluate(() => collectSettings().compaction.history_indexing),
      {enabled:false,batch_records:4,max_batch_seconds:0.1,duty_cycle:0.1});
    await page.locator('#history-index-duty').fill('51');
    assert(await page.evaluate(() => validateSettingsDraft(collectSettings()).some(error => error.includes('历史整理参数'))));
    await page.locator('#history-index-duty').fill('10');
    await page.locator('#history-index-enabled').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output, 'history-index-mobile.png')});
    await page.screenshot({path:path.join(output, 'context-budget-mobile.png')});
    await page.setViewportSize({width:1440, height:1000});
    await page.locator('#history-index-enabled').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output, 'history-index-desktop.png')});
    await page.locator('#compaction-budget-seconds').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output, 'context-budget-desktop.png')});
    assert.deepEqual(errors, []);
    console.log('PASS browser: authorization dependency, index/task status, budget collection/ranges, desktop/mobile reachability');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
