// Run against tests/ui_preview.py, never against a production console.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:24001';
const output = process.env.UI_TEST_OUTPUT
  || path.resolve('browser-route-diagnosis-artifacts');
const key = 'ui-preview-only';
const requestId = '2de4d089f0e84f24aebd203e56e1693e';
const checks = [];

async function run() {
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const marker = await fetch(base + '/__ui_preview__')
    .then(response => response.json());
  assert.equal(marker.isolated, true);
  await fs.mkdir(output, {recursive: true});
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}
      : {}),
  });
  const errors = [];
  const page = await browser.newPage({
    viewport: {width: 1440, height: 1100},
  });
  page.on('pageerror', error => errors.push(String(error)));
  const check = async (name, action) => {
    await action();
    checks.push(name);
    console.log('PASS ' + name);
  };
  try {
    await page.addInitScript(
      value => sessionStorage.setItem('ai-router-admin-key', value),
      key,
    );
    await page.goto(base);
    await page.waitForFunction(() => state.settings && state.policy);
    await page.locator('#auto-refresh').uncheck();

    await page.locator('[data-view="audit"]').click();
    await page.locator('#trace-review-filter').selectOption('');
    await page.locator('#trace-search').fill(requestId);
    await page.waitForFunction(
      id => document.querySelector(
        `[data-trace-id="${id}"]`,
      ),
      requestId,
    );
    await page.locator(
      `#trace-list [data-trace-id="${requestId}"]`,
    ).click();
    await page.waitForFunction(
      id => state.selectedTrace?.request_id === id
        && state.routeDiagnosis,
      requestId,
    );

    await check('deterministic root cause and non-cause evidence', async () => {
      assert.match(
        await page.locator('#trace-diagnosis-verdict').innerText(),
        /健康状态异常或过期/,
      );
      assert.match(
        await page.locator('#trace-diagnosis-non-causes').innerText(),
        /115,158 < 196,608/,
      );
      assert.equal(
        await page.locator('#trace-diagnosis-alternatives .diagnosis-alternative').count(),
        3,
      );
    });

    await check('conversation phases expose migration and tier lock', async () => {
      assert.equal(
        await page.locator('#trace-diagnosis-phases .diagnosis-phase').count(),
        2,
      );
      assert.match(
        await page.locator('#trace-diagnosis-phases').innerText(),
        /tier_lock/,
      );
    });

    await check('conversation pin and unpin affect next turn only', async () => {
      page.once('dialog', dialog => dialog.accept());
      await page.locator(
        '[data-conversation-action="pin"][data-endpoint-id="ai-qwen38-27b"]',
      ).click();
      await page.waitForFunction(
        () => state.conversationControl?.pin?.endpoint_id
          === 'ai-qwen38-27b',
      );
      assert.match(
        await page.locator('#trace-conversation-actions').innerText(),
        /解除固定 ai-qwen38-27b/,
      );
      page.once('dialog', dialog => dialog.accept());
      await page.locator('[data-conversation-action="unpin"]').click();
      await page.waitForFunction(
        () => !state.conversationControl?.pin,
      );
    });

    await check('diagnosis links to controlled policy fields', async () => {
      await page.locator(
        '[data-policy-ref="conversation-stability"]',
      ).first().click();
      await page.waitForFunction(() => state.view === 'settings');
      assert.equal(
        await page.locator('#conversation-stability').isVisible(),
        true,
      );
    });

    await check('draft validate activate workflow and impact report', async () => {
      await page.locator('#stability-enabled').check();
      await page.locator('#stability-failure-threshold').fill('2');
      await page.locator('#stability-recheck-interval').fill('10');
      const saved = page.waitForResponse(
        response => response.url().endsWith('/api/policy/draft')
          && response.request().method() === 'PATCH',
      );
      await page.locator('#settings-form button[type="submit"]').click();
      assert.equal((await saved).status(), 200);
      assert.match(
        await page.locator('#policy-revision-state').innerText(),
        /待验证/,
      );

      const validated = page.waitForResponse(
        response => response.url().endsWith('/api/policy/draft/validate'),
      );
      await page.locator('#policy-validate').click();
      assert.equal((await validated).status(), 200);
      assert.equal(
        await page.locator('#policy-impact-report').isVisible(),
        true,
      );
      assert.match(
        await page.locator('#policy-impact-report').innerText(),
        /健康复检候选 1/,
      );

      page.once('dialog', dialog => dialog.accept());
      const activated = page.waitForResponse(
        response => response.url().endsWith('/api/policy/draft/activate'),
      );
      await page.locator('#policy-activate').click();
      assert.equal((await activated).status(), 200);
      await page.waitForFunction(() => !state.policy?.draft);
      assert.equal(
        await page.locator('#stability-enabled').isChecked(),
        true,
      );
    });

    await check('rollback creates a new draft without activation', async () => {
      assert.equal(
        await page.locator('#policy-rollback').isDisabled(),
        false,
      );
      const rolledBack = page.waitForResponse(
        response => response.url().includes('/api/policy/revisions/')
          && response.url().endsWith('/rollback'),
      );
      await page.locator('#policy-rollback').click();
      assert.equal((await rolledBack).status(), 200);
      assert.match(
        await page.locator('#policy-revision-state').innerText(),
        /待验证/,
      );
    });

    await page.screenshot({
      path: path.join(output, 'desktop-policy.png'),
      fullPage: true,
    });
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('[data-view="audit"]').click();
    await page.locator('#trace-review-filter').selectOption('');
    await page.locator('#trace-search').fill('');
    await page.locator('#trace-search').fill(requestId);
    await page.waitForFunction(
      id => document.querySelector(
        `#trace-list [data-trace-id="${id}"]`,
      ),
      requestId,
    );
    await page.locator(
      `#trace-list [data-trace-id="${requestId}"]`,
    ).click();
    await page.waitForFunction(
      id => state.selectedTrace?.request_id === id,
      requestId,
    );
    await check('mobile diagnosis has no page overflow', async () => {
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth + 1,
        ),
        false,
      );
      assert.equal(
        await page.locator('#trace-diagnosis-section').isVisible(),
        true,
      );
    });
    await page.screenshot({
      path: path.join(output, 'mobile-diagnosis.png'),
      fullPage: true,
    });
    assert.deepEqual(errors, []);
    await fs.writeFile(
      path.join(output, 'results.json'),
      JSON.stringify({passed: true, checks, errors}, null, 2),
    );
  } catch (error) {
    await page.screenshot({
      path: path.join(output, 'failure.png'),
      fullPage: true,
    });
    await fs.writeFile(
      path.join(output, 'results.json'),
      JSON.stringify({
        passed: false,
        checks,
        errors,
        error: String(error),
      }, null, 2),
    );
    throw error;
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
