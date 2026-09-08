// Run against tests/ui_preview.py, never against a production console.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");

const base = process.env.UI_PREVIEW_URL || "http://127.0.0.1:14821";
const output = process.env.UI_TEST_OUTPUT
  || path.resolve("browser-cache-deployments");
const key = "ui-preview-only";

async function run() {
  assert.equal(new URL(base).hostname, "127.0.0.1");
  const marker = await fetch(base + "/__ui_preview__")
    .then((response) => response.json());
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
    viewport: {width: 1440, height: 1000},
  });
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.addInitScript((value) => {
    sessionStorage.setItem("ai-router-admin-key", value);
  }, key);

  try {
    await page.goto(base);
    await page.waitForFunction(() => state.dashboard && state.settings);
    await page.locator("#auto-refresh").uncheck();
    await page.locator('[data-view="cache-deployments"]').click();
    await page.waitForFunction(
      () => state.cacheDeployments?.deployments?.length === 6,
    );

    assert.equal(
      await page.locator(
        "#cache-deployment-table tr[data-cache-deployment-id]",
      ).count(),
      6,
    );
    assert.equal(
      await page.locator("#cache-deployment-summary .metric").count(),
      4,
    );
    assert.match(
      await page.locator(
        'tr[data-cache-deployment-id="ai-v100-tp2"]',
      ).innerText(),
      /LMCache 健康[\s\S]*Connector 未连接 0\/2/,
    );

    await page.locator(
      'tr[data-cache-deployment-id="nx4-qwen36"]',
    ).click();
    assert.match(
      await page.locator("#cache-deployment-detail-title").innerText(),
      /NX4 Qwen3\.6 · qwen36-nx4/,
    );
    await page.locator(
      '[data-cache-layer-id="gateway-snapshot"]',
    ).click();
    assert.match(
      await page.locator(
        "#cache-deployment-layer-detail",
      ).innerText(),
      /Planned Snapshot[\s\S]*\/mnt\/data/,
    );
    await page.locator(
      '[data-cache-deployment-tab="validated"]',
    ).click();
    assert.match(
      await page.locator(
        "#cache-deployment-tab-content",
      ).innerText(),
      /待验证[\s\S]*20K、40K、50K/,
    );

    await page.locator("#cache-deployment-anomalies").check();
    assert.equal(
      await page.locator(
        "#cache-deployment-table tr[data-cache-deployment-id]",
      ).count(),
      2,
    );
    await page.locator("#cache-deployment-anomalies").uncheck();

    await page.locator(
      'tr[data-cache-deployment-id="ai-v100-tp2"]',
    ).click();
    await page.locator(
      '[data-cache-deployment-action="ai-v100-tp2"]'
      + '[data-cache-action-id="lmcache-settings"]',
    ).click();
    assert.equal(
      await page.evaluate(() => state.view),
      "settings",
    );
    assert.match(
      await page.locator("#notice").innerText(),
      /策略草稿、验证和激活/,
    );

    await page.locator('[data-view="cache-deployments"]').click();
    await page.waitForFunction(
      () => state.view === "cache-deployments",
    );
    await page.screenshot({
      path: path.join(output, "desktop.png"),
      fullPage: true,
    });

    await page.setViewportSize({width: 390, height: 844});
    await page.waitForTimeout(100);
    const layout = await page.evaluate(() => {
      const navItem = document.querySelector(
        '[data-view="cache-deployments"]',
      ).getBoundingClientRect();
      const tableWrap = document.querySelector(
        "#cache-deployments-view .table-wrap",
      );
      return {
        viewport: document.documentElement.clientWidth,
        body: document.body.scrollWidth,
        navHeight: navItem.height,
        tableClient: tableWrap.clientWidth,
        tableScroll: tableWrap.scrollWidth,
      };
    });
    assert.equal(layout.body, layout.viewport);
    assert(layout.navHeight <= 50);
    assert(layout.tableScroll > layout.tableClient);
    await page.screenshot({
      path: path.join(output, "mobile.png"),
      fullPage: true,
    });
    assert.deepEqual(errors, []);
    console.log("PASS cache deployment console browser checks");
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
