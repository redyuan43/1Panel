const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

async function main() {
  const origin = 'http://127.0.0.1:14839';
  const root = path.resolve(__dirname, '../frontend');
  const calls = [];
  let keys = [];
  let policy = {revision: 'initial', writes: true, generation: false};
  let backend = {policy: {revision: 'initial', enabled: true}, backends: [{backend_id: 'single-a4', state: 'online_model_unknown', model_state: 'unknown', quarantined: false}]};
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--disable-gpu']});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      assert.equal(url.origin, origin);
      if (url.pathname.startsWith('/api/mcp-admin/')) {
        const action = url.pathname.split('/').at(-1);
        const body = request.method() === 'POST' ? request.postDataJSON() : {};
        calls.push({action, body});
        let result;
        if (action === 'status') result = {endpoint: origin + '/mcp/h3',
          instances: [{instance: 'control-local', healthy: true, release: 'test'}, {instance: 'control-tail', healthy: false, release: 'stale'}],
          studio: {healthy: true}, keys, policy, writes_enabled: policy.writes, generation_enabled: false,
          last_authentication: null, last_tool: null};
        else if (action === 'issue') {
          keys.push({label: body.label, key_id: 'test-key-1', status: 'active'});
          result = {api_key: 'browser-fake-token-never-production', key: keys[0]};
        } else if (action === 'revoke') { keys[0].status = 'revoked'; result = {key: keys[0]}; }
        else if (action === 'policy') { policy = {...body, revision: 'changed'}; result = policy; }
        else if (action === 'backend') { if (request.method() === 'POST') backend.policy = {enabled: body.enabled, revision: 'updated'}; result = backend; }
        else throw new Error('Unexpected management action');
        return route.fulfill({json: result});
      }
      const filename = path.basename(url.pathname);
      const allowed = new Set(['mcp-settings.html', 'mcp-settings.js', 'mcp-settings.css', 'styles.css']);
      if (!allowed.has(filename)) return route.fulfill({status: 404, body: ''});
      const source = filename === 'styles.css' ? path.resolve(__dirname, '../../../experiments/h3-mcp-studio-candidate-20260910-r3/frontend/styles.css') : path.join(root, filename);
      return route.fulfill({contentType: filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(source)});
    });
    await page.goto(origin + '/mcp-settings.html');
    await page.waitForFunction(() => !document.querySelector('#issue').disabled);
    assert.equal(await page.locator('.mcp-service[data-healthy=false]').count(), 1);
    assert.match(await page.locator('#gates').innerText(), /视频生成：关闭/);
    await page.locator('#issue').click();
    await page.locator('#tokenDialog').waitFor({state: 'visible'});
    assert.equal(await page.locator('#token').getAttribute('type'), 'password');
    assert.equal(await page.locator('#token').inputValue(), 'browser-fake-token-never-production');
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    await page.locator('#closeToken').click();
    assert.equal(await page.locator('#token').inputValue(), '');
    assert.equal(calls.filter(item => item.action === 'issue').length, 1);
    await page.locator('#keys button').click();
    await page.waitForFunction(() => document.querySelector('#keys').textContent.includes('已撤销'));
    await page.locator('#pause').click();
    await page.waitForFunction(() => document.querySelector('#gates').textContent.includes('暂停或服务未就绪'));
    assert(!calls.some(item => JSON.stringify(item).includes('h3_start_preview')));
    await page.locator('#backendToggle').click();
    await page.waitForFunction(() => document.querySelector('#backendStatus').textContent.includes('已停用'));
    assert.match(await page.locator('#backends').innerText(), /模型：未知/);
    assert.equal(calls.filter(item => item.action === 'backend' && item.body.operation_id).length, 1);
    await page.setViewportSize({width: 390, height: 844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('PASS: 12 browser checks; masking, clear-on-close, no persistence, stale status, revoke, policy, backend drain, model unknown, no generation, responsive, no JS errors');
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
