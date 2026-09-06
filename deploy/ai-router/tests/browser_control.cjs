// Run against tests/ui_preview.py, never against a production console.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:14801';
const output = process.env.UI_TEST_OUTPUT || path.resolve('browser-artifacts');
const key = 'ui-preview-only';
const checks = [];

async function run() {
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const marker = await fetch(base + '/__ui_preview__').then(response => response.json());
  assert.equal(marker.isolated, true);
  await fs.mkdir(output, {recursive: true});
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {}),
  });
  let page;
  const errors = [];
  const check = async (name, fn) => {
    await fn();
    checks.push(name);
    console.log('PASS ' + name);
  };
  const graphReady = async count => {
    await page.waitForFunction(n => document.querySelectorAll('#trace-graph svg g.node').length === n, count);
    await page.waitForFunction(() => !document.querySelector('#trace-graph').classList.contains('loading'));
  };
  const selectTrace = async id => {
    await page.locator(`#trace-list [data-trace-id="${id}"]`).click();
    await page.waitForFunction(value => state.selectedTrace?.request_id === value, id);
  };
  try {
    page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.on('pageerror', error => errors.push(String(error)));
    await page.addInitScript(value => sessionStorage.setItem('ai-router-admin-key', value), key);
    await page.goto(base);
    await page.waitForFunction(() => state.settings && state.dashboard);
    await page.locator('#auto-refresh').uncheck();
    await page.locator('[data-view="audit"]').click();
    await page.locator('#trace-review-filter').selectOption('');
    await selectTrace('preview-local');
    await check('simple graph and chronological timeline', async () => {
      await graphReady(12);
      await page.waitForFunction(() => document.querySelectorAll('#trace-timeline [data-trace-id]').length === 4);
      assert.deepEqual(await page.locator('#trace-timeline [data-trace-id]').evaluateAll(
        nodes => nodes.map(node => node.dataset.traceId),
      ), ['preview-local', 'preview-cloud', 'preview-identity', 'preview-rejected']);
    });
    await check('selected evidence node survives refresh', async () => {
      await page.locator('[data-trace-node-id="candidate_scope"]').click();
      await page.evaluate(() => loadRouteTraces(true));
      assert.equal(await page.evaluate(() => state.selectedTraceNodeId), 'candidate_scope');
      assert.equal(await page.locator('#trace-node-inspector').isVisible(), true);
    });
    await check('privacy save preserves routing draft', async () => {
      await page.locator('#trace-review-verdict').selectOption('incorrect');
      await page.locator('#trace-expected-model').fill('preview-correct-model');
      await page.locator('#trace-review-note').fill('Unsaved routing evidence');
      await page.locator('#privacy-feedback-decision').selectOption('normal');
      await page.locator('#privacy-feedback-note').fill('Privacy saved');
      await page.locator('#privacy-feedback-submit').click();
      await page.waitForFunction(() => state.selectedTrace.privacy_feedback?.[0]?.note === 'Privacy saved');
      assert.equal(await page.locator('#trace-review-note').inputValue(), 'Unsaved routing evidence');
      assert.equal(await page.locator('#trace-review-verdict').inputValue(), 'incorrect');
      assert.equal(await page.locator('#trace-expected-model').inputValue(), 'preview-correct-model');
    });
    await check('routing save preserves privacy draft', async () => {
      await page.locator('#privacy-feedback-decision').selectOption('uncertain');
      await page.locator('#privacy-feedback-note').fill('Unsaved privacy evidence');
      await page.locator('#trace-review-submit').click();
      await page.waitForFunction(() => state.selectedTrace.current_review?.note === 'Unsaved routing evidence');
      assert.equal(await page.locator('#privacy-feedback-note').inputValue(), 'Unsaved privacy evidence');
      await page.locator('#trace-detail-title').click();
      await page.evaluate(() => loadRouteTraces(true));
      assert.equal(await page.locator('#privacy-feedback-note').inputValue(), 'Unsaved privacy evidence');
    });
    await check('edits during submission are not overwritten', async () => {
      await page.route('**/api/route-traces/preview-local/privacy-feedback', async route => {
        await new Promise(resolve => setTimeout(resolve, 350));
        await route.continue();
      });
      const request = page.waitForRequest(r => r.url().endsWith('/preview-local/privacy-feedback'));
      await page.locator('#privacy-feedback-submit').click();
      await request;
      await page.locator('#privacy-feedback-note').fill('New draft during submission');
      await page.waitForFunction(() => !document.querySelector('#privacy-feedback-submit').disabled);
      assert.equal(await page.locator('#privacy-feedback-note').inputValue(), 'New draft during submission');
      await page.unroute('**/api/route-traces/preview-local/privacy-feedback');
    });
    await check('timeline selection and stale response isolation', async () => {
      await page.locator('#trace-timeline [data-trace-id="preview-cloud"]').click();
      await page.waitForFunction(() => state.selectedTrace?.request_id === 'preview-cloud');
      assert.equal(await page.locator('#privacy-feedback-note').inputValue(), '');
      await page.route('**/api/route-traces/preview-local', async route => {
        await new Promise(resolve => setTimeout(resolve, 400));
        await route.continue();
      });
      await page.evaluate(() => { void selectRouteTrace('preview-local'); void selectRouteTrace('preview-cloud'); });
      await page.waitForTimeout(600);
      assert.equal(await page.evaluate(() => state.selectedTrace.request_id), 'preview-cloud');
      await page.unroute('**/api/route-traces/preview-local');
    });
    await check('simple and detailed graph modes and cached labels', async () => {
      await page.locator('#trace-graph-mode-detailed').click();
      await graphReady(17);
      await page.locator('#trace-graph-mode-simple').click();
      await graphReady(12);
      assert.equal(await page.evaluate(() => traceNodeLabels.intelligent_v2_dispatch),
        await page.evaluate(() => state.routeGraph.nodes.find(node => node.id === 'intelligent_v2_dispatch').label));
    });
    await check('identity graph and rejected-request fallback', async () => {
      await selectTrace('preview-identity');
      await graphReady(12);
      assert.equal(await page.locator('#trace-graph-viewport').isVisible(), true);
      assert(await page.evaluate(() => traceGraphSteps().some(step => step.node_id === 'identity_answered')));
      await selectTrace('preview-rejected');
      assert.equal(await page.locator('#trace-graph-viewport').isVisible(), false);
      assert.equal(await page.locator('#trace-routing-state').isVisible(), true);
      await selectTrace('preview-local');
    });
    await check('slow timeline never leaves another round review in the form', async () => {
      await page.waitForLoadState('networkidle');
      const expected = await page.evaluate(() => ({
        privacy: state.selectedTrace.privacy_feedback[0].note,
        routing: state.selectedTrace.current_review.note,
      }));
      let timelineRequests = 0;
      await page.route('**/api/route-traces?*', async route => {
        if (new URL(route.request().url()).searchParams.get('conversation_id') === 'preview-conversation') {
          timelineRequests++;
          await new Promise(resolve => setTimeout(resolve, 500));
        }
        await route.continue();
      });
      try {
        await page.evaluate(async () => {
          notice('');
          state.requestConversationPages.delete('preview-conversation');
          await selectRouteTrace('preview-cloud');
          await selectRouteTrace('preview-local');
        });
        assert.equal(await page.locator('#privacy-feedback-note').inputValue(), expected.privacy);
        assert.equal(await page.locator('#trace-review-note').inputValue(), expected.routing);
        assert.equal(await page.evaluate(() => traceReviewHasDraft('privacy')), false);
        assert.equal(await page.evaluate(() => traceReviewHasDraft('routing')), false);
        assert.doesNotMatch(await page.locator('#notice').innerText(), /Cannot read properties/);
        await page.waitForFunction(() => !conversationTurnLoads.has('preview-conversation'));
        await page.waitForLoadState('networkidle');
        assert.equal(timelineRequests, 1);
        assert.equal(await page.locator('#privacy-feedback-note').inputValue(), expected.privacy);
      } finally {
        await page.unroute('**/api/route-traces?*');
      }
    });
    await check('long timeline pagination and retained rounds survive refresh', async () => {
      await page.evaluate(() => selectRouteTrace('preview-long-104'));
      await page.waitForFunction(() => document.querySelectorAll('#trace-timeline [data-trace-id]').length === 100);
      assert.match(await page.locator('#trace-timeline-state').innerText(), /100 \/ 105/);
      await page.locator('#trace-timeline-earlier').click();
      await page.waitForFunction(() => document.querySelectorAll('#trace-timeline [data-trace-id]').length === 105);
      assert.equal(await page.locator('#trace-timeline-earlier').isVisible(), false);
      await page.evaluate(() => loadRouteTraces(true));
      await page.waitForTimeout(150);
      assert.equal(await page.locator('#trace-timeline [data-trace-id]').count(), 105);
      assert.equal(await page.locator('#trace-timeline [data-trace-id]').first().getAttribute('data-trace-id'), 'preview-long-0');
      await selectTrace('preview-local');
    });
    for (const olderFirst of [false, true]) {
      await check(`timeline refresh and older page stay ordered (${olderFirst ? 'older' : 'refresh'} first)`, async () => {
        await page.waitForLoadState('networkidle');
        await page.evaluate(() => fetchConversationTurns('preview-long-conversation'));
        let requests = 0;
        let active = 0;
        let peak = 0;
        await page.route('**/api/route-traces?*', async route => {
          if (new URL(route.request().url()).searchParams.get('conversation_id') !== 'preview-long-conversation') {
            return route.continue();
          }
          const first = ++requests === 1;
          active++;
          peak = Math.max(peak, active);
          try {
            const response = await route.fetch();
            if (first) await new Promise(resolve => setTimeout(resolve, 350));
            await route.fulfill({response});
          } finally {
            active--;
          }
        });
        try {
          const result = await page.evaluate(async olderFirst => {
            const id = 'preview-long-conversation';
            const cursor = state.requestConversationPages.get(id).nextCursor;
            if (!cursor) throw new Error('Expected an older-page cursor');
            const first = loadTraceTimeline(id, olderFirst ? cursor : null);
            await new Promise(resolve => setTimeout(resolve, 50));
            const second = loadTraceTimeline(id, olderFirst ? null : cursor);
            await Promise.all([first, second]);
            const cached = state.requestConversationPages.get(id);
            return {ids: cached.items.map(item => item.request_id), cursor: cached.nextCursor};
          }, olderFirst);
          assert.equal(requests, 2);
          assert.equal(peak, 1);
          assert.deepEqual(result.ids, Array.from({length: 105}, (_, i) => `preview-long-${i}`));
          assert.equal(result.cursor, null);
        } finally {
          await page.unroute('**/api/route-traces?*');
        }
      });
    }
    await check('failed timeline fetch releases queued pages without blocking other conversations', async () => {
      await page.evaluate(() => fetchConversationTurns('preview-long-conversation'));
      let requests = 0;
      await page.route('**/api/route-traces?*', async route => {
        if (new URL(route.request().url()).searchParams.get('conversation_id') === 'preview-long-conversation'
            && ++requests === 1) {
          await new Promise(resolve => setTimeout(resolve, 350));
          return route.fulfill({status: 503, contentType: 'application/json',
            body: JSON.stringify({error: {message: 'Synthetic timeline failure'}})});
        }
        return route.continue();
      });
      try {
        const result = await page.evaluate(async () => {
          const id = 'preview-long-conversation';
          const cursor = state.requestConversationPages.get(id).nextCursor;
          let finished = false;
          const first = loadTraceTimeline(id).then(() => { finished = true; });
          const other = loadTraceTimeline('preview-conversation');
          const older = loadTraceTimeline(id, cursor);
          await other;
          const independent = !finished;
          await Promise.all([first, older]);
          await loadTraceTimeline(id);
          const cached = state.requestConversationPages.get(id);
          return {independent, count: cached.items.length, error: cached.error,
            loading: cached.loading, queued: conversationTurnLoads.size};
        });
        assert.equal(result.independent, true);
        assert.equal(result.count, 105);
        assert.equal(result.error, null);
        assert.equal(result.loading, false);
        assert.equal(result.queued, 0);
      } finally {
        await page.unroute('**/api/route-traces?*');
      }
    });
    let puts = 0;
    page.on('request', request => {
      if (request.method() === 'PUT' && request.url().endsWith('/api/settings')) puts++;
    });
    await page.locator('[data-view="settings"]').click();
    await check('prompt directive random preview and confirmed save', async () => {
      assert.equal(await page.locator('[data-directive-phrase]').count(), 5);
      const input = page.locator('[data-directive-phrase="beichen"]');
      const original = await input.inputValue();
      const revision = await page.evaluate(() => state.settings.routing.prompt_directives.revision);
      const generation = await page.evaluate(() => state.settings.routing.prompt_directives.routes.beichen.generation);
      const before = puts;
      const suggested = page.waitForResponse(response =>
        response.url().endsWith('/api/prompt-directives/suggest')
        && response.request().method() === 'POST');
      await page.locator('[data-directive-random="beichen"]').click();
      assert.equal((await suggested).status(), 200);
      assert.notEqual(await input.inputValue(), original);
      assert.equal(puts, before);
      page.once('dialog', async dialog => {
        assert.match(dialog.message(), /旧暗语和旧会话定向立即失效/);
        await dialog.accept();
      });
      const saved = page.waitForResponse(response =>
        response.url().endsWith('/api/settings')
        && response.request().method() === 'PUT');
      await page.locator('#settings-form button[type="submit"]').click();
      assert.equal((await saved).status(), 200);
      assert.equal(await page.evaluate(() => state.settings.routing.prompt_directives.revision), revision + 1);
      assert.equal(await page.evaluate(() => state.settings.routing.prompt_directives.routes.beichen.generation), generation + 1);
    });
    await check('strategy branches and fallback-order controls', async () => {
      await page.locator('#routing-strategy').selectOption('legacy_v1');
      assert.equal(await page.locator('#branch-intelligent_v2').evaluate(node => node.classList.contains('dimmed')), true);
      await page.locator('#routing-strategy').selectOption('intelligent_v2');
      const original = await page.evaluate(() => [...state.settings.routing.remote_fallback_order.general]);
      await page.locator('[data-fallback-down="general"][data-fallback-index="0"]').click();
      assert.equal(await page.evaluate(() => state.settings.routing.remote_fallback_order.general[1]), original[0]);
      await page.locator('[data-fallback-remove="general"][data-fallback-index="0"]').click();
      await page.locator('[data-fallback-add-select="general"]').selectOption(original[1]);
      await page.locator('[data-fallback-add="general"]').click();
      assert.equal(await page.evaluate(() => state.settings.routing.remote_fallback_order.general.at(-1)), original[1]);
      assert.equal(await page.locator('[data-fallback-up="general"][data-fallback-index="0"]').isDisabled(), true);
      assert.equal(await page.locator('#fallback-chain-warning').isVisible(), false);
    });
    await check('last fallback entry is protected and unknown endpoints are warned', async () => {
      const original = await page.evaluate(() => [...state.settings.routing.remote_fallback_order.general]);
      while (await page.locator('[data-fallback-remove="general"]').count() > 1) {
        await page.locator('[data-fallback-remove="general"]').last().click();
      }
      assert.equal(await page.locator('[data-fallback-remove="general"]').isDisabled(), true);
      await page.evaluate(order => {
        state.settings.routing.remote_fallback_order.general = [...order, 'preview-missing-endpoint'];
        renderRemoteFallbackOrder();
      }, original);
      assert.equal(await page.locator('#fallback-chain-warning').isVisible(), true);
      assert.match(await page.locator('#fallback-chain-warning').innerText(), /preview-missing-endpoint/);
      await page.evaluate(order => {
        state.settings.routing.remote_fallback_order.general = order;
        renderRemoteFallbackOrder();
      }, original);
    });
    await check('invalid drafts never reach the settings API', async () => {
      const before = puts;
      const quality = page.locator('[data-weight="quality"]');
      const saved = await quality.inputValue();
      await quality.fill('0.99');
      await page.locator('#settings-form button[type="submit"]').click();
      await page.waitForTimeout(150);
      assert.equal(puts, before);
      assert.match(await page.locator('#notice').innerText(), /总和/);
      await quality.fill(saved);
      await page.locator('#review-base-url').fill('https://example.com');
      await page.locator('#settings-form button[type="submit"]').click();
      await page.waitForTimeout(150);
      assert.equal(puts, before);
      assert.match(await page.locator('#notice').innerText(), /私网|base_url/);
      await page.locator('#review-backend').selectOption('ollama');
      await page.locator('#review-base-url').fill('http://agx.taild500c8.ts.net:11434');
      await page.locator('#review-rpm').evaluate(select => {
        select.add(new Option('3', '3'));
        select.value = '3';
      });
      await page.locator('#settings-form button[type="submit"]').click();
      await page.waitForTimeout(150);
      assert.equal(puts, before);
      assert.match(await page.locator('#notice').innerText(), /每分钟/);
      await page.locator('#review-rpm').selectOption('2');
      assert.equal(await page.evaluate(() => validateReviewBaseUrl('http://[::1]:11434', 'ollama')), null);
    });
    await check('settings round trip persists fallback order and shadow configuration', async () => {
      await page.locator('#review-backend').selectOption('router');
      await page.locator('#review-model').fill('preview-explicit-model');
      await page.locator('#review-mode').selectOption('shadow');
      const expected = await page.evaluate(() => [...state.settings.routing.remote_fallback_order.general]);
      const saved = page.waitForResponse(response => response.url().endsWith('/api/settings') && response.request().method() === 'PUT');
      await page.locator('#settings-form button[type="submit"]').click();
      assert.equal((await saved).status(), 200);
      await page.reload();
      await page.waitForFunction(() => state.settings && state.dashboard);
      await page.locator('#auto-refresh').uncheck();
      await page.locator('[data-view="settings"]').click();
      assert.deepEqual(await page.evaluate(() => state.settings.routing.remote_fallback_order.general), expected);
      assert.equal(await page.locator('#review-backend').inputValue(), 'router');
      assert.equal(await page.locator('#review-mode').inputValue(), 'shadow');
    });
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({width, height: 1000});
      await check(`settings layout ${width}px`, async () => {
        await page.locator('[data-view="settings"]').click();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        const overflow = await page.locator('#settings-view input, #settings-view select, #settings-view textarea').evaluateAll(nodes =>
          nodes.filter(node => {
            const rect = node.getBoundingClientRect();
            return rect.width > 0 && (rect.left < -1 || rect.right > innerWidth + 1);
          }).map(node => node.id));
        assert.deepEqual(overflow, []);
        await page.locator('[data-fallback-down="general"][data-fallback-index="0"]').click();
        await page.locator('[data-fallback-up="general"][data-fallback-index="1"]').click();
        await page.screenshot({path: path.join(output, `settings-${width}.png`), fullPage: true});
      });
      await check(`audit layout ${width}px`, async () => {
        await page.locator('[data-view="audit"]').click();
        await page.locator('#trace-review-filter').selectOption('');
        await selectTrace('preview-local');
        await graphReady(12);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        await page.locator('#trace-fit').click();
        await page.waitForTimeout(200);
        assert.equal(await page.locator('#trace-graph-viewport').evaluate(node => node.scrollWidth > node.clientWidth + 1), false);
        const before = await page.locator('#trace-graph svg').evaluate(node => node.getBoundingClientRect().width);
        await page.locator('#trace-zoom-in').click();
        await page.waitForTimeout(200);
        assert(await page.locator('#trace-graph svg').evaluate(node => node.getBoundingClientRect().width) > before);
        await page.locator('#trace-fit').click();
        await page.waitForTimeout(200);
        await page.screenshot({path: path.join(output, `audit-${width}.png`), fullPage: true});
      });
    }
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({passed: true, checks, errors}, null, 2));
  } catch (error) {
    if (page) await page.screenshot({path: path.join(output, 'failure.png'), fullPage: true});
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({passed: false, checks, errors, error: String(error)}, null, 2));
    throw error;
  } finally {
    await browser.close();
  }
}
run().catch(error => {console.error(error); process.exitCode = 1;});
