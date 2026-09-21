// Serve the real static console entirely through Playwright interception: no listening port or backend.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const staticDir = path.resolve(__dirname, '../ai_router/static');
const origin = 'http://request-preview.invalid';
const now = Date.now() / 1000;
const rows = Array.from({length: 150}, (_, i) => ({
  request_id: `browser-${String(i + 1).padStart(4, '0')}`, conversation_id: 'synthetic-conversation',
  client_id: 'synthetic-client', requested_model: 'auto', selected_model: 'synthetic/model',
  endpoint_id: 'synthetic-endpoint', deployment_id: 'synthetic-deployment', task: 'code',
  status: i ? 'succeeded' : 'running', started_at: now - 200 + i, updated_at: now - 200 + i,
  prompt_tokens: 386167, excerpt: {text: '隔离页面回归样本'},
  token_summary: {ingress_estimated_input_tokens: 386167, target_input_tokens: 187506,
    target_count_exact: true, measured_input_tokens: i ? 187506 : null, measured_output_tokens: i ? 265 : null,
    output_reserve_tokens: 65536, required_context_tokens: 253042, safe_context_tokens: 262144},
}));

async function main() {
  const browser = await chromium.launch({headless: true, args: ['--disable-gpu'],
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
  const errors = [], batches = [];
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, serviceWorkers: 'block'});
    page.on('pageerror', error => errors.push(String(error)));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) {
        errors.push('Unexpected external URL');
        return route.abort();
      }
      if (url.pathname === '/api/route-traces') {
        const ids = url.searchParams.getAll('request_ids');
        if (ids.length) batches.push(ids);
        const all = rows.filter(row => !ids.length || ids.includes(row.request_id)).slice().reverse();
        const offset = Number(url.searchParams.get('cursor') || 0);
        const limit = Number(url.searchParams.get('limit') || 30);
        const items = ids.length ? all : all.slice(offset, offset + limit);
        return route.fulfill({json: {items, total_count: all.length, conversation_summaries: [],
          next_cursor: !ids.length && offset + limit < all.length ? String(offset + limit) : null}});
      }
      if (url.pathname.startsWith('/api/')) {
        errors.push(`Unexpected API: ${url.pathname}`);
        return route.fulfill({status: 500, json: {error: {message: 'No preview endpoint'}}});
      }
      const file = path.resolve(staticDir, url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/assets\//, ''));
      if (!file.startsWith(staticDir + path.sep)) return route.abort();
      try {
        const contentType = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}[path.extname(file)];
        return route.fulfill({body: await fs.readFile(file), contentType});
      } catch {
        return route.fulfill({status: 404, body: ''});
      }
    });
    await page.goto(origin);
    await page.evaluate(async () => {
      switchView('requests');
      state.key = 'synthetic-browser-key';
      await loadRequestTraces();
    });
    await page.locator('[data-request-conversation="synthetic-conversation"]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-request-round-id]').length === 100);
    const ids = () => page.locator('[data-request-round-id]').evaluateAll(nodes => nodes.map(node => node.dataset.requestRoundId));
    assert.equal((await ids())[0], 'browser-0150');
    assert.equal((await ids()).at(-1), 'browser-0051');
    await page.locator('[data-request-earlier="synthetic-conversation"]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-request-round-id]').length === 150);
    assert.equal((await ids()).at(-1), 'browser-0001');
    const oldest = page.locator('[data-request-round-id="browser-0001"]');
    await oldest.scrollIntoViewIfNeeded();
    assert((await oldest.innerText()).includes('待返回'));
    const horizontal = await page.locator('.request-round-table-wrap').evaluate(node => {
      node.scrollLeft = 700;
      return node.scrollLeft;
    });
    const before = await oldest.evaluate(node => node.getBoundingClientRect().top);
    rows[0] = {...rows[0], status: 'succeeded', updated_at: now + 1,
      token_summary: {...rows[0].token_summary, measured_input_tokens: 187506, measured_output_tokens: 265}};
    rows.push({...rows.at(-1), request_id: 'browser-0151', started_at: now + 1, updated_at: now + 1});
    await page.evaluate(() => loadRequestTraces(true));
    assert.equal((await ids()).length, 151);
    assert.equal((await ids())[0], 'browser-0151');
    assert.equal((await ids()).at(-1), 'browser-0001');
    assert((await oldest.innerText()).includes('成功'));
    assert(!(await oldest.innerText()).includes('待返回'));
    assert(batches.some(batch => batch.includes('browser-0001')));
    const after = await oldest.evaluate(node => node.getBoundingClientRect().top);
    assert(Math.abs(after - before) < 2, `Vertical scroll moved: ${before} -> ${after}`);
    assert.equal(await page.locator('.request-round-table-wrap').evaluate(node => node.scrollLeft), horizontal);
    assert.equal(await page.locator('[data-request-earlier="synthetic-conversation"]').isDisabled(), true);
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      const table = page.locator('.request-round-table-wrap');
      assert(await table.isVisible());
      assert((await table.innerText()).includes('目标输入'));
      assert(await table.evaluate(node => node.clientWidth > 0 && node.scrollWidth >= node.clientWidth));
    }
    assert.deepEqual(errors, []);
    console.log('PASS browser: newest first, older-page completion, token labels, pagination, vertical/horizontal scroll, desktop/mobile');
  } finally {
    await browser.close();
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
