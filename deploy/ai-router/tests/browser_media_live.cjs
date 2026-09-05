"use strict";

// Real Router only. No fixtures, response mocks, task creation, stage starts,
// general configuration changes, HAR, traces, storage-state exports, or
// credential logs. --exercise-grant permits one dedicated client's reversible
// media-grant change, never general settings or existing production clients.
// Credentials and assigned jobs come from the private acceptance environment.
// --approve-stage is an explicit, one-shot approval of an existing output only.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const {execFile} = require("node:child_process");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {parseArgs, promisify} = require("node:util");
const execFileAsync = promisify(execFile);

process.umask(0o077);
const HOME = os.homedir();
const ENV_FILE = path.join(HOME, ".config/ai-router-media/acceptance.env");
const OUTPUT = path.join(HOME, ".local/state/ai-router-acceptance/20260904-media-deploy/browser");
const WIDTHS = [1440, 1101, 1100, 768, 390];
const ID = /^(img|vid)_[a-f0-9]{32}$/;
const secrets = new Set();
const report = {
  schema_version: 1, started_at: new Date().toISOString(), status: "running",
  real_network: true, mocked_responses: 0, generation_requests_sent: 0,
  settings_and_grants: "read_only", checks: [], network: [], blocked_requests: [],
  page_errors: [], screenshots: [], pending: [],
};
let reportPath;
let runDirectory;
let config;
let browser;
let armedApproval = null;
let approvalsSent = 0;
let armedGrant = null;
let grantWritesSent = 0;

function requireThat(condition, code) {
  if (!condition) throw Object.assign(new Error(code), {code});
}

function redact(value) {
  let text = String(value);
  for (const secret of secrets) if (secret.length >= 4) text = text.split(secret).join("[REDACTED]");
  return text.replace(/https?:\/\/[^\s"'<>]+/g, "[URL_REDACTED]")
    .replace(/([?&](?:access|token|key)=)[^\s&"'<>]+/gi, "$1[REDACTED]")
    .replace(/\bBearer\s+\S+/gi, "Bearer [REDACTED]");
}

function errorEvidence(error) {
  // Playwright call logs and assertion diffs can contain complete ticket URLs.
  return {name: error.name || "Error", code: error.code || "check_failed",
    message: redact(String(error.message || error).split("\n")[0]).slice(0, 240)};
}

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function parseEnvironment(text) {
  const values = {};
  for (const [index, source] of text.split(/\r?\n/).entries()) {
    const line = source.trim();
    if (!line || line.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    requireThat(match, `invalid_environment_line_${index + 1}`);
    let value = match[2].trim();
    if (value.startsWith("'")) {
      requireThat(value.endsWith("'"), `invalid_environment_quote_${index + 1}`);
      value = value.slice(1, -1);
    } else if (value.startsWith('"')) {
      requireThat(value.endsWith('"'), `invalid_environment_quote_${index + 1}`);
      value = value.slice(1, -1).replace(/\\(["\\])/g, "$1");
    } else {
      value = value.replace(/\s+#.*$/, "").trim();
    }
    // This is a data parser, never a shell evaluator.
    values[match[1]] = value;
  }
  return values;
}

async function loadConfig(args) {
  let values = {};
  try {
    const info = await fs.lstat(ENV_FILE);
    requireThat(info.isFile() && !info.isSymbolicLink(), "environment_must_be_regular_file");
    requireThat((info.mode & 0o077) === 0, "environment_must_be_private");
    if (process.getuid) requireThat(info.uid === process.getuid(), "environment_wrong_owner");
    values = parseEnvironment(await fs.readFile(ENV_FILE, "utf8"));
    report.private_environment_present = true;
  } catch (error) {
    if (error.code !== "ENOENT" || !args.baseline) throw error;
    report.private_environment_present = false;
  }
  const env = {...values, ...process.env};
  for (const [name, value] of Object.entries(env)) {
    if (/(KEY|TOKEN|PASSWORD|SECRET)/.test(name) && value) secrets.add(value);
  }
  const origin = new URL(env.AI_ROUTER_CONTROL_URL || "http://127.0.0.1:4001");
  requireThat(!origin.username && !origin.password && !origin.search && !origin.hash &&
    origin.pathname === "/", "control_url_must_be_clean_origin");
  requireThat(origin.protocol === "https:" ||
    (origin.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(origin.hostname)),
  "remote_control_requires_tls");
  const publicOrigin = new URL(env.AI_ROUTER_API_URL || "http://127.0.0.1:4000");
  requireThat(!publicOrigin.username && !publicOrigin.password && !publicOrigin.search &&
    !publicOrigin.hash && publicOrigin.pathname === "/", "api_url_must_be_clean_origin");
  requireThat(publicOrigin.protocol === "https:" || (publicOrigin.protocol === "http:" &&
    ["127.0.0.1", "localhost", "[::1]"].includes(publicOrigin.hostname)), "remote_api_requires_tls");
  const number = (name, fallback) => {
    const value = Number(env[name] || fallback);
    requireThat(Number.isFinite(value) && value >= 0 && value <= 14400, `invalid_${name}`);
    return value;
  };
  return {
    origin: origin.origin, key: env.AI_ROUTER_ADMIN_KEY || "",
    publicOrigin: publicOrigin.origin, clientKey: env.MEDIA_CLIENT_KEY || "",
    imageId: env.AI_ROUTER_ACCEPTANCE_IMAGE_JOB_ID || env.AI_ROUTER_IMAGE_JOB_ID || "",
    videoId: env.AI_ROUTER_ACCEPTANCE_VIDEO_JOB_ID || env.AI_ROUTER_VIDEO_JOB_ID || "",
    clientId: env.AI_ROUTER_ACCEPTANCE_CLIENT_ID || "",
    waitSeconds: number("AI_ROUTER_BROWSER_WAIT_SECONDS", 120),
    separationSeconds: Math.max(12, number("AI_ROUTER_BROWSER_SEPARATION_SECONDS", 16)),
    nextStartSeconds: number("AI_ROUTER_BROWSER_NEXT_START_SECONDS", 0),
    playbackObservationSeconds: Math.max(12, number("AI_ROUTER_BROWSER_PLAYBACK_OBSERVATION_SECONDS", 12)),
    approveStage: args["approve-stage"] || "",
    expectedOutputId: env.AI_ROUTER_ACCEPTANCE_OUTPUT_ID || "",
    expectedArtifactId: env.AI_ROUTER_ACCEPTANCE_ARTIFACT_ID || "",
    requireCleanMedia: env.AI_ROUTER_BROWSER_REQUIRE_CLEAN_MEDIA === "1",
    expectedRawStatus: number("AI_ROUTER_BROWSER_EXPECT_RAW_STATUS", 410),
    previousVideoSha256: env.AI_ROUTER_BROWSER_PREVIOUS_VIDEO_SHA256 || "",
    rawPacketHashes: env.AI_ROUTER_BROWSER_RAW_PACKET_HASHES ?
      JSON.parse(env.AI_ROUTER_BROWSER_RAW_PACKET_HASHES) : null,
    playwright: env.PLAYWRIGHT_MODULE || "playwright",
    executable: env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    baseline: Boolean(args.baseline),
    scope: args["console-only"] ? "console" : (args.scope || "all"),
    exerciseGrant: args["exercise-grant"] || "",
  };
}

function rememberTickets(value) {
  if (Array.isArray(value)) return value.forEach(rememberTickets);
  if (!value || typeof value !== "object") return;
  for (const [name, item] of Object.entries(value)) {
    if ((name === "content_url" || name === "url") && typeof item === "string") {
      const ticket = new URL(item, config.origin).searchParams.get("access");
      if (ticket) secrets.add(ticket);
    } else if (typeof item === "object") rememberTickets(item);
  }
}

async function saveReport() {
  if (!reportPath) return;
  report.updated_at = new Date().toISOString();
  const serialized = redact(JSON.stringify(report, null, 2)) + "\n";
  const temporary = reportPath + ".writing";
  await fs.writeFile(temporary, serialized, {mode: 0o600});
  await fs.rename(temporary, reportPath);
}

async function check(name, fn) {
  const started = Date.now();
  try {
    const evidence = await fn();
    report.checks.push({name, status: "passed", elapsed_ms: Date.now() - started, evidence});
    console.log(`PASS ${name}`);
    await saveReport();
    return evidence;
  } catch (error) {
    report.checks.push({name, status: "failed", elapsed_ms: Date.now() - started,
      error: errorEvidence(error)});
    throw error;
  }
}

async function request(endpoint, {auth = true, method = "GET", headers = {}} = {}) {
  const url = new URL(endpoint, config.origin);
  requireThat(url.origin === config.origin, "request_outside_router");
  requireThat(["GET", "HEAD"].includes(method), "api_helper_is_read_only");
  const response = await fetch(url, {
    method, redirect: "manual", signal: AbortSignal.timeout(30000),
    headers: {...headers, ...(auth ? {Authorization: `Bearer ${config.key}`} : {})},
  });
  report.network.push({source: "api", method, path: url.pathname, status: response.status,
    request_id: response.headers.get("x-request-id"), time: new Date().toISOString()});
  return response;
}

async function json(endpoint, {auth = true, status = 200} = {}) {
  const response = await request(endpoint, {auth});
  requireThat(response.status === status, `http_${response.status}_expected_${status}`);
  const value = await response.json();
  rememberTickets(value);
  return value;
}

async function publicRequest(endpoint, {method = "GET"} = {}) {
  requireThat(config.clientKey, "assigned_client_key_missing");
  const url = new URL(endpoint, config.publicOrigin);
  requireThat(url.origin === config.publicOrigin && ["GET", "HEAD"].includes(method),
    "public_api_probe_must_be_same_origin_read_only");
  const response = await fetch(url, {method, redirect: "manual", signal: AbortSignal.timeout(30000),
    headers: {Authorization: `Bearer ${config.clientKey}`}});
  report.network.push({source: "client_api", method, path: url.pathname, status: response.status,
    request_id: response.headers.get("x-request-id"), time: new Date().toISOString()});
  return response;
}

function snapshot(job) {
  return {
    id: job.id, status: job.status,
    stages: (job.stages || []).map(stage => ({
      id: stage.id, status: stage.status, run_id: stage.run_id || null,
      output_id: stage.output_id || null, progress: stage.progress ?? null,
      has_archived_output: Boolean(stage.output),
    })),
  };
}

async function preflight() {
  const marker = await request("/__ui_preview__", {auth: false});
  if (marker.status === 200) {
    const value = await marker.json().catch(() => ({}));
    requireThat(value.isolated !== true, "fixture_server_refused");
  }
  await check("real_control_health", async () => {
    const value = await json("/health", {auth: false});
    requireThat(value.ok === true && value.state_store === true, "control_health_not_ready");
    return {ok: value.ok, state_store: value.state_store};
  });
  await check("deployment_route_baseline", async () => {
    const results = {};
    for (const endpoint of ["/", "/media", "/assets/media.js", "/assets/media.css", "/api/media/options"]) {
      const response = await request(endpoint, {auth: false});
      results[endpoint] = response.status;
      if (endpoint === "/assets/media.js" && response.status === 200) {
        report.served_media_js_sha256 = digest(await response.arrayBuffer().then(Buffer.from));
      } else if (endpoint === "/assets/media.css" && response.status === 200) {
        report.served_media_css_sha256 = digest(await response.arrayBuffer().then(Buffer.from));
      } else {
        await response.body?.cancel();
      }
    }
    if (!config.baseline) {
      requireThat(results["/media"] === 200, "media_console_not_deployed");
      requireThat(results["/assets/media.js"] === 200, "media_script_not_deployed");
      requireThat(results["/assets/media.css"] === 200, "media_styles_not_deployed");
      requireThat([401, 403].includes(results["/api/media/options"]), "media_requires_admin_auth");
    }
    return results;
  });
}

async function guardedBrowser() {
  const {chromium} = require(config.playwright);
  browser = await chromium.launch({headless: true, ...(config.executable ?
    {executablePath: config.executable} : {})});
  const context = await browser.newContext({
    viewport: {width: 1440, height: 1000}, serviceWorkers: "block",
    acceptDownloads: true, locale: "zh-CN",
  });
  await context.route("**/*", async route => {
    const req = route.request();
    const url = new URL(req.url());
    if (["data:", "blob:"].includes(url.protocol)) return route.continue();
    let allowed = url.origin === config.origin && ["GET", "HEAD"].includes(req.method());
    if (!allowed && url.origin === config.origin && req.method() === "POST" && armedApproval &&
      url.pathname === armedApproval.path && approvalsSent === 0) {
      const body = req.postDataJSON();
      allowed = body?.output_id === armedApproval.outputId &&
        (armedApproval.prompt === undefined || body.prompt === armedApproval.prompt);
      if (allowed) {
        approvalsSent++;
        armedApproval = null;
      }
    }
    if (!allowed && url.origin === config.origin && req.method() === "PATCH" && armedGrant &&
      url.pathname === armedGrant.path && grantWritesSent < 2) {
      const body = req.postDataJSON();
      allowed = body && Object.keys(body).length === 1 && Array.isArray(body.media_models) &&
        JSON.stringify([...body.media_models].sort()) === JSON.stringify([...armedGrant.models].sort());
      if (allowed) {
        grantWritesSent++;
        armedGrant = null;
      }
    }
    if (!allowed) {
      report.blocked_requests.push({method: req.method(), path: url.pathname,
        cross_origin: url.origin !== config.origin});
      return route.abort("blockedbyclient");
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  page.on("pageerror", error => report.page_errors.push(errorEvidence(error)));
  page.on("response", response => {
    const req = response.request(), url = new URL(response.url());
    if (url.origin === config.origin && url.pathname.startsWith("/api/")) {
      report.network.push({source: "browser", method: req.method(), path: url.pathname,
        status: response.status(), time: new Date().toISOString()});
    }
  });
  page.on("dialog", dialog => dialog.dismiss());
  return page;
}

async function screenshots(page, view, {checkLayout = true, mediaSelector = null} = {}) {
  const problems = [];
  for (const width of WIDTHS) {
    await page.setViewportSize({width, height: 1000});
    if (mediaSelector) await page.locator(mediaSelector).last().scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    const layout = await page.evaluate(() => {
      const intersects = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
        Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
      const stages = [...document.querySelectorAll("#stages .stage")].filter(stage =>
        stage.getBoundingClientRect().width > 0).map(stage => {
        const output = stage.querySelector(".stage-output").getBoundingClientRect();
        const info = stage.querySelector(".stage-info").getBoundingClientRect();
        const buttons = [...stage.querySelectorAll(".actions button")].map(button =>
          button.getBoundingClientRect());
        return {stage: stage.querySelector("[data-stage]")?.dataset.stage,
          output_width: output.width, video_width: stage.querySelector("video")?.getBoundingClientRect().width ?? null,
          info_content_overlap: intersects(info, output),
          action_outside_output: buttons.some(rect => rect.left < output.left - 1 || rect.right > output.right + 1),
          actions_overlap: buttons.some((rect, index) => buttons.slice(index + 1).some(other => intersects(rect, other)))};
      });
      return {viewport: innerWidth, scroll_width: document.documentElement.scrollWidth, stages};
    });
    if (checkLayout && layout.scroll_width > width + 1) problems.push({view, width, kind: "horizontal_overflow"});
    if (checkLayout) {
      for (const stage of layout.stages) {
        if (stage.output_width < Math.min(240, width - 64)) {
          problems.push({view, width, kind: "stage_content_too_narrow", ...stage});
        }
        if (stage.info_content_overlap || stage.action_outside_output || stage.actions_overlap) {
          problems.push({view, width, kind: "stage_content_or_actions_overlap", ...stage});
        }
      }
    }
    const name = `${view}-${width}.png`;
    await page.screenshot({path: path.join(runDirectory, name), fullPage: true,
      mask: [page.locator("#key"), page.locator("#admin-key")]});
    report.screenshots.push({file: name, view, width, height: 1000, ...layout});
  }
  await page.setViewportSize({width: 1440, height: 1000});
  if (problems.length) {
    report.layout_findings = [...(report.layout_findings || []), ...problems];
    await saveReport();
    requireThat(false, "responsive_layout_failed");
  }
}

async function login(page) {
  await page.goto(config.origin + "/media", {waitUntil: "domcontentloaded"});
  await check("login_rejects_invalid_admin_key", async () => {
    await page.locator("#key").fill("invalid-live-acceptance-credential");
    const [response] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === "/api/media/options"),
      page.locator("#login button").click(),
    ]);
    requireThat([401, 403].includes(response.status()), "invalid_admin_key_accepted");
    requireThat(await page.locator("#settings-fields input").count() === 0,
      "settings_loaded_without_auth");
    return {status: response.status()};
  });
  await check("login_with_private_admin_key", async () => {
    await page.locator("#key").fill(config.key);
    await page.locator("#login button").click();
    await page.waitForSelector("#settings-fields input", {state: "attached"});
    await page.waitForSelector("#client-grants form", {state: "attached"});
    requireThat(await page.locator("#notice.error").count() === 0, "login_notice_error");
    return {settings_loaded: true, grants_loaded: true, credential_recorded: false};
  });
}

async function verifySettings(page) {
  await check("settings_and_grants_match_real_api", async () => {
    await page.locator('[data-view="settings"]').click();
    const settings = await json("/api/media/settings");
    for (const [name, value] of Object.entries(settings)) {
      requireThat(/^[a-z][a-z0-9_]*$/.test(name), "invalid_setting_identifier");
      const input = page.locator(`#settings-fields [name="${name}"]`);
      if (typeof value === "boolean") assert.equal(await input.isChecked(), value, name);
      else assert.equal(Number(await input.inputValue()), value, name);
    }
    const response = await json("/api/clients");
    const clients = response.clients || response.items || response;
    requireThat(Array.isArray(clients) && clients.length > 0, "live_clients_missing");
    const forms = page.locator("#client-grants form");
    assert.equal(await forms.count(), clients.length, "grant_count");
    for (let index = 0; index < clients.length; index++) {
      const client = clients[index], form = forms.nth(index);
      assert.equal(await form.getAttribute("data-client"), client.id, "grant_client_order");
      const actual = await form.locator("input:checked").evaluateAll(inputs =>
        inputs.map(input => input.value).sort());
      assert.deepEqual(actual, [...(client.media_models || [])].sort(), "grant_models");
    }
    if (config.clientId) requireThat(clients.some(client => client.id === config.clientId),
      "assigned_client_missing");
    return {setting_fields: Object.keys(settings).sort(), clients_checked: clients.length,
      assigned_client_found: config.clientId ? true : null, writes_performed: 0};
  });
  await screenshots(page, "settings");
}

async function exerciseGrant(page) {
  if (!config.exerciseGrant) return;
  const clientId = config.exerciseGrant;
  const accounts = async () => (await json("/api/clients")).clients;
  const original = (await accounts()).find(client => client.id === clientId);
  requireThat(original, "dedicated_grant_client_missing");
  const previous = [...(original.media_models || [])].sort();
  const toggled = previous.includes("siyuan-image") ?
    previous.filter(model => model !== "siyuan-image") : [...previous, "siyuan-image"].sort();
  const stableFields = ["id", "name", "enabled", "models", "rpm_limit", "tpm_limit",
    "max_parallel_requests", "allow_compaction", "disclosure_mode", "source", "created_at"];
  const stable = client => Object.fromEntries(stableFields.map(name => [name, client[name]]));
  const form = page.locator(`#client-grants form[data-client="${clientId}"]`);
  const save = async models => {
    for (const input of await form.locator('input[type="checkbox"]').all()) {
      await input.setChecked(models.includes(await input.getAttribute("value")));
    }
    const endpoint = "/api/clients/" + encodeURIComponent(clientId);
    armedGrant = {path: endpoint, models};
    const [response] = await Promise.all([
      page.waitForResponse(response =>
        new URL(response.url()).pathname === endpoint && response.request().method() === "PATCH"),
      form.locator("button").click(),
    ]);
    requireThat(response.status() === 200, "grant_patch_failed");
    const account = (await accounts()).find(client => client.id === clientId);
    requireThat(account, "dedicated_grant_client_disappeared");
    assert.deepEqual([...(account.media_models || [])].sort(), models, "grant_readback");
    assert.deepEqual(stable(account), stable(original), "unrelated_client_fields_changed");
    return {method: response.request().method(), status: response.status(),
      request_id: response.headers()["x-request-id"] || null};
  };
  await check("dedicated_client_grant_save_readback_and_restore", async () => {
    report.settings_and_grants = "settings_read_only_dedicated_grant_roundtrip";
    report.grant_roundtrip = {client_id: clientId, original: previous, temporary: toggled,
      restore_status: "not_needed_yet"};
    await saveReport();
    let changed;
    try {
      changed = await save(toggled);
      report.grant_roundtrip.restore_status = "required";
      await saveReport();
      await screenshots(page, "dedicated-grant-saved");
    } finally {
      // Read the actual current grant before rollback. Never overwrite a third
      // party's concurrent change, and report it instead of silently restoring.
      const current = (await accounts()).find(client => client.id === clientId);
      requireThat(current, "grant_restore_client_missing");
      const actual = [...(current.media_models || [])].sort();
      if (JSON.stringify(actual) === JSON.stringify(toggled)) {
        const restored = await save(previous);
        report.grant_roundtrip.restore_status = "restored_and_read_back";
        report.grant_roundtrip.restore_response = restored;
      } else if (JSON.stringify(actual) === JSON.stringify(previous)) {
        report.grant_roundtrip.restore_status = "original_already_present";
      } else {
        report.grant_roundtrip.restore_status = "concurrent_change_requires_primary_review";
        throw Object.assign(new Error("grant_changed_concurrently"), {code: "grant_changed_concurrently"});
      }
      await saveReport();
    }
    return {...report.grant_roundtrip, change_response: changed, unrelated_fields_preserved: true};
  });
}

async function waitJob(kind, id, predicate) {
  const deadline = Date.now() + config.waitSeconds * 1000;
  for (;;) {
    const job = await json(`/api/media/${kind}/${id}`);
    if (predicate(job)) return job;
    if (["failed", "cancelled"].includes(job.status)) {
      report.job_failure = snapshot(job);
      throw Object.assign(new Error("assigned_job_failed_or_cancelled"), {code: "assigned_job_failed_or_cancelled"});
    }
    if (Date.now() >= deadline) {
      report.pending.push(snapshot(job));
      throw Object.assign(new Error("assigned_job_not_ready"), {code: "assigned_job_not_ready"});
    }
    await saveReport();
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
}

async function selectJob(page, kind, id) {
  const singular = kind === "images" ? "image" : "video";
  await page.locator(`[data-view="${kind}"]`).click();
  await Promise.all([
    page.waitForResponse(response =>
      new URL(response.url()).pathname === `/api/media/${kind}` && response.status() === 200),
    page.locator("#refresh").click(),
  ]);
  await page.waitForTimeout(150);
  for (let pageNumber = 0; pageNumber < 100; pageNumber++) {
    const row = page.locator(`#${singular}-list [data-id="${id}"]`);
    if (await row.count()) {
      await row.click();
      await page.waitForFunction(value => document.querySelector("#detail-id")?.textContent === value, id);
      return;
    }
    const more = page.locator(`#${singular}-more`);
    if (!await more.isVisible()) break;
    await Promise.all([
      page.waitForResponse(response =>
        new URL(response.url()).pathname === `/api/media/${kind}` && response.status() === 200),
      more.click(),
    ]);
    await page.waitForTimeout(150);
  }
  throw Object.assign(new Error("assigned_job_not_in_console_history"), {code: "assigned_job_not_in_console_history"});
}

function routerOutput(output) {
  requireThat(output && output.output_id && output.content_url, "archived_output_missing");
  const url = new URL(output.content_url, config.origin);
  const artifactId = output.id || output.output_id;
  requireThat(url.origin === config.origin &&
    url.pathname === `/api/media/outputs/${encodeURIComponent(artifactId)}/content`,
    "output_not_served_through_router");
  requireThat(url.searchParams.has("access"), "output_preview_ticket_missing");
  return url;
}

async function verifyHistory(page, kind, id) {
  const outputs = await json(`/api/media/${kind}/${id}/outputs`);
  requireThat(outputs.data.length > 0, "archived_history_empty");
  for (const output of outputs.data) routerOutput(output);
  if (!await page.locator("#versions").evaluate(node => node.open)) {
    await page.locator("#versions summary").click();
  }
  await page.waitForFunction(ids => {
    const rendered = [...document.querySelectorAll("#version-list code")].map(node => node.textContent);
    return ids.every(id => rendered.includes(id));
  }, outputs.data.map(output => output.output_id));
  await page.locator("#versions summary").click();
  return {versions: outputs.data.map(item => ({artifact_id: item.id || item.output_id,
    output_id: item.output_id, stage: item.stage,
    sha256: item.sha256})), links_use_router: true};
}

function privateVideoMarkers(bytes) {
  const content = bytes.toString("latin1").toLowerCase();
  return ["comfyui", "workflow", "\"prompt\"", "\"class_type\"", "source_",
    "/home/", "/opt/", "/workspace/"].filter(marker => content.includes(marker));
}

async function inspectPublicVideo(filename, bytes) {
  const {stdout} = await execFileAsync("ffprobe", [
    "-v", "error", "-show_entries",
    "format_tags:stream=index,codec_name,codec_type,width,height,sample_rate,channels:stream_tags",
    "-of", "json", filename,
  ], {timeout: 30000, maxBuffer: 16 * 1024 * 1024});
  const probe = JSON.parse(stdout);
  const formatTags = probe.format?.tags || {};
  const streamTags = (probe.streams || []).map(stream => stream.tags || {});
  const suspicious = /comfyui|workflow|class_type|checkpoint|diffusion_model|model_path|source_|lora|cuda|\/home\/|\/opt\/|\.py\b|\.pyc\b/i;
  const descriptive = /^(?:comment|description|prompt|workflow|title|artist|album|copyright|parameters)$/i;
  const entries = [...Object.entries(formatTags), ...streamTags.flatMap(tags => Object.entries(tags))];
  const forbiddenTagKeys = entries.filter(([key]) => descriptive.test(key)).map(([key]) => key);
  const embeddedWorkflow = entries.some(([key, value]) => suspicious.test(key + " " + String(value)));
  const metadata = {
    format_tag_keys: Object.keys(formatTags).sort(),
    stream_tag_keys: streamTags.map(tags => Object.keys(tags).sort()),
    forbidden_tag_keys: [...new Set(forbiddenTagKeys)].sort(),
    workflow_or_backend_marker_found: embeddedWorkflow,
    private_binary_markers: privateVideoMarkers(bytes),
  };
  if (config.requireCleanMedia) {
    report.public_video_metadata = metadata;
    requireThat(forbiddenTagKeys.length === 0 && !embeddedWorkflow &&
      metadata.private_binary_markers.length === 0, "public_video_metadata_not_clean");
  }
  const packets = await execFileAsync("ffprobe", [
    "-v", "error", "-show_packets", "-show_data_hash", "sha256",
    "-show_entries", "packet=stream_index,data_hash", "-of", "json", filename,
  ], {timeout: 30000, maxBuffer: 16 * 1024 * 1024});
  const hashes = new Map();
  for (const packet of JSON.parse(packets.stdout).packets || []) {
    requireThat(/^SHA256:[a-f0-9]{64}$/i.test(packet.data_hash || ""), "packet_payload_hash_missing");
    if (!hashes.has(packet.stream_index)) hashes.set(packet.stream_index, []);
    hashes.get(packet.stream_index).push(packet.data_hash.slice(7).toLowerCase());
  }
  const packetFingerprints = [...hashes.entries()].map(([index, values]) => ({
    stream_index: index, packets: values.length, ordered_packet_sha256: digest(values.join("\n")),
  }));
  requireThat(packetFingerprints.length > 0, "video_packet_fingerprints_missing");
  const streams = (probe.streams || []).map(stream => ({
    index: stream.index, codec_type: stream.codec_type, codec_name: stream.codec_name,
    width: stream.width, height: stream.height, sample_rate: stream.sample_rate, channels: stream.channels,
  }));
  if (config.rawPacketHashes) {
    for (const stream of packetFingerprints) {
      requireThat(config.rawPacketHashes[String(stream.stream_index)] === stream.ordered_packet_sha256,
        "public_av_packet_hash_differs_from_raw");
    }
    requireThat(Object.keys(config.rawPacketHashes).length === packetFingerprints.length,
      "raw_public_stream_count_mismatch");
  }
  return {metadata, streams, packet_fingerprints: packetFingerprints,
    raw_packet_comparison: config.rawPacketHashes ? "identical" : "not_supplied_not_proven"};
}

async function verifyImage(page) {
  const job = await waitJob("images", config.imageId, value => value.status === "completed" && value.output);
  routerOutput(job.output);
  await selectJob(page, "images", job.id);
  await check("assigned_image_decodes_in_browser", async () => {
    await page.waitForFunction(() => {
      const image = document.querySelector("#detail-output img");
      return image?.complete && image.naturalWidth > 0 && image.naturalHeight > 0;
    });
    const decoded = await page.locator("#detail-output img").evaluate(image => ({
      natural_width: image.naturalWidth, natural_height: image.naturalHeight,
      loaded_from_router: new URL(image.currentSrc).origin === location.origin,
      source_path: new URL(image.currentSrc).pathname,
    }));
    requireThat(decoded.loaded_from_router, "image_loaded_outside_router");
    requireThat(decoded.source_path === routerOutput(job.output).pathname, "displayed_image_output_mismatch");
    if (job.output.width) assert.equal(decoded.natural_width, job.output.width, "image_width");
    if (job.output.height) assert.equal(decoded.natural_height, job.output.height, "image_height");
    const response = await request(`/api/media/images/${job.id}/content`);
    requireThat(response.status === 200, "image_download_failed");
    const data = Buffer.from(await response.arrayBuffer());
    const hash = digest(data);
    requireThat(hash === job.output.sha256, "downloaded_image_digest_mismatch");
    return {job_id: job.id, output_id: job.output.output_id, ...decoded,
      bytes: data.length, sha256: hash, status: job.status};
  });
  await check("image_download_button_saves_verified_artifact", async () => {
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#detail-output a[download]").click(),
    ]);
    requireThat(!await download.failure(), "browser_image_download_failed");
    const extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[job.output.content_type];
    requireThat(extension, "unexpected_downloaded_image_format");
    const filename = `${job.id}-browser-download.${extension}`;
    await download.saveAs(path.join(runDirectory, filename));
    const bytes = await fs.readFile(path.join(runDirectory, filename));
    requireThat(digest(bytes) === job.output.sha256, "browser_image_download_digest_mismatch");
    return {job_id: job.id, output_id: job.output.output_id, file: filename,
      bytes: bytes.length, sha256: digest(bytes), initiated_by: "console_download_link"};
  });
  await check("image_archived_versions", () => verifyHistory(page, "images", job.id));
  await screenshots(page, "image-result");
}

async function verifyApproval(page) {
  if (!config.approveStage) return;
  const job = await waitJob("videos", config.videoId, value => value.stages?.some(stage =>
    stage.id === config.approveStage && stage.status === "awaiting_approval" && stage.output));
  const index = job.stages.findIndex(stage => stage.id === config.approveStage);
  const stage = job.stages[index], next = job.stages[index + 1];
  if (config.expectedOutputId) {
    requireThat(stage.output_id === config.expectedOutputId, "assigned_approval_output_changed");
  }
  requireThat(next && next.status === "pending" && !next.output_id && !next.run_id,
    "approval_test_requires_unstarted_successor");
  await selectJob(page, "videos", job.id);
  if (stage.id === "context_ir") {
    await check("context_ir_display_and_console_download", async () => {
      routerOutput(stage.output);
      const section = page.locator('#stages .stage:has([data-stage="context_ir"][data-action="approve"])');
      const text = await section.locator("pre").textContent();
      requireThat(text === stage.output.text && text.length > 0, "context_ir_display_mismatch");
      const [download] = await Promise.all([
        page.waitForEvent("download"),
        section.locator("a[download]").click(),
      ]);
      requireThat(!await download.failure(), "context_ir_browser_download_failed");
      const filename = `${job.id}-context-ir-browser-download.txt`;
      await download.saveAs(path.join(runDirectory, filename));
      const bytes = await fs.readFile(path.join(runDirectory, filename));
      requireThat(bytes.toString("utf8") === text, "context_ir_download_text_mismatch");
      requireThat(digest(bytes) === stage.output.sha256, "context_ir_download_digest_mismatch");
      return {job_id: job.id, output_id: stage.output_id, file: filename,
        bytes: bytes.length, sha256: digest(bytes), text_unchanged: true,
        initiated_by: "console_stage_download_link"};
    });
    await screenshots(page, "context-ir-awaiting-approval");
  }
  await check("stage_approval_does_not_start_successor", async () => {
    const before = snapshot(job);
    const draft = page.locator(`[data-draft="${stage.output_id}"]`);
    if (await draft.count()) {
      const text = await draft.inputValue();
      await page.waitForTimeout(5500);
      requireThat(await draft.inputValue() === text, "approval_draft_lost_on_poll");
      requireThat(text === stage.output.text, "approval_would_change_existing_prompt");
    }
    const endpoint = `/api/media/videos/${job.id}/stages/${stage.id}/approve`;
    armedApproval = {path: endpoint, outputId: stage.output_id,
      ...(stage.id === "context_ir" ? {prompt: stage.output.text} : {})};
    const [response] = await Promise.all([
      page.waitForResponse(response =>
        new URL(response.url()).pathname === endpoint && response.request().method() === "POST"),
      page.locator(`[data-stage="${stage.id}"][data-action="approve"]`).click(),
    ]);
    requireThat(response.status() === 200, "stage_approval_failed");
    const returned = await response.json();
    rememberTickets(returned);
    const observations = [];
    const deadline = Date.now() + config.separationSeconds * 1000;
    do {
      const current = await json(`/api/media/videos/${job.id}`);
      const approved = current.stages.find(item => item.id === stage.id);
      const pending = current.stages.find(item => item.id === next.id);
      requireThat(approved.status === "approved" && approved.output_id === stage.output_id,
        "approval_did_not_bind_existing_output");
      requireThat(pending.status === "pending" && !pending.output_id && !pending.run_id,
        "successor_started_during_approval_only_window");
      observations.push({at: new Date().toISOString(), ...snapshot(current)});
      if (Date.now() >= deadline) break;
      await new Promise(resolve => setTimeout(resolve, 4000));
    } while (true);
    const start = page.locator(`[data-stage="${next.id}"][data-action="start"]`);
    await start.waitFor({state: "visible"});
    requireThat(await start.isEnabled(), "successor_start_button_not_available");
    return {before, operation_id: returned.operation_id || null, observations,
      observation_seconds: config.separationSeconds, successor_start_enabled: true,
      start_requests_sent: 0, approval_requests_sent: approvalsSent};
  });
  await screenshots(page, "approved-successor-pending");
  if (config.nextStartSeconds > 0) {
    await check("externally_started_successor_observed", async () => {
      console.log("WAIT approval verified; successor may now be explicitly started by the primary agent.");
      const original = config.waitSeconds;
      config.waitSeconds = config.nextStartSeconds;
      try {
        const started = await waitJob("videos", job.id, value => value.stages.some(item =>
          item.id === next.id && (item.run_id || item.output_id || ["queued", "running"].includes(item.status))));
        return {observed: snapshot(started), source: "external_actor",
          own_start_requests: 0, causality_limit: "External request receipt must be supplied by its caller."};
      } finally {
        config.waitSeconds = original;
      }
    });
  }
}

async function verifyVideo(page) {
  const eligible = stage => stage.output?.content_type?.startsWith("video/") &&
    (!config.expectedOutputId || stage.output.output_id === config.expectedOutputId) &&
    (!config.expectedArtifactId || stage.output.id === config.expectedArtifactId);
  const job = await waitJob("videos", config.videoId, value =>
    value.stages?.some(eligible));
  await selectJob(page, "videos", job.id);
  for (const stage of job.stages.filter(eligible)) {
    routerOutput(stage.output);
    if (config.requireCleanMedia) {
      await check(`video_stage_${stage.id}_authorized_client_clean_only`, async () => {
        requireThat([404, 410].includes(config.expectedRawStatus), "invalid_expected_private_raw_status");
        const detail = await publicRequest(`/v1/videos/${job.id}`);
        requireThat(detail.status === 200, "assigned_client_cannot_read_video");
        const publicJob = await detail.json();
        rememberTickets(publicJob);
        const publicStage = publicJob.stages.find(item => item.id === stage.id);
        requireThat(publicStage?.output && publicStage.output_id === stage.output_id &&
          publicStage.output.id === stage.output.id && publicStage.output.sha256 === stage.output.sha256,
        "client_delivery_metadata_differs_from_console");
        const forbidden = Object.keys(publicStage.output).filter(key =>
          key.startsWith("source_") || ["path", "workflow", "prompt", "job_id"].includes(key));
        requireThat(forbidden.length === 0, "private_source_fields_in_public_output");
        const delivery = new URL(publicStage.output.content_url, config.publicOrigin);
        requireThat(delivery.origin === config.publicOrigin &&
          delivery.pathname === `/v1/media/outputs/${stage.output.id}/content`, "client_delivery_url_mismatch");
        const clean = await publicRequest(`/v1/media/outputs/${stage.output.id}/content`, {method: "HEAD"});
        requireThat(clean.status === 200, "authorized_clean_artifact_unavailable");
        const raw = await publicRequest(`/v1/media/outputs/${stage.output.output_id}/content`);
        if (raw.status !== config.expectedRawStatus) {
          await raw.body?.cancel();
          throw Object.assign(new Error("private_raw_access_status_mismatch"),
            {code: "private_raw_access_status_mismatch"});
        }
        const error = await raw.json();
        return {artifact_id: stage.output.id, output_id: stage.output.output_id,
          public_sha256: publicStage.output.sha256, private_source_fields: forbidden,
          authorized_clean_head_status: clean.status, authorized_raw_get_status: raw.status,
          expected_raw_get_status: config.expectedRawStatus,
          raw_error_code: error.error?.code || null};
      });
    }
    await check(`video_stage_${stage.id}_public_download`, async () => {
      const artifactId = stage.output.id || stage.output.output_id;
      if (config.requireCleanMedia) {
        requireThat(artifactId !== stage.output.output_id, "clean_delivery_artifact_not_distinct");
      }
      const section = page.locator(
        `#stages .stage:has(video[data-output="${stage.output.output_id}"])`);
      const [download] = await Promise.all([
        page.waitForEvent("download"),
        section.locator("a[download]").click(),
      ]);
      requireThat(!await download.failure(), "browser_video_download_failed");
      const filename = `${job.id}-${stage.id}-public-browser-download.mp4`;
      await download.saveAs(path.join(runDirectory, filename));
      const bytes = await fs.readFile(path.join(runDirectory, filename));
      const hash = digest(bytes);
      requireThat(hash === stage.output.sha256, "public_video_download_digest_mismatch");
      if (config.previousVideoSha256) {
        requireThat(hash !== config.previousVideoSha256, "public_video_still_matches_previous_raw_sha");
      }
      const inspected = await inspectPublicVideo(path.join(runDirectory, filename), bytes);
      return {artifact_id: artifactId, output_id: stage.output.output_id, bytes: bytes.length,
        sha256: hash, previous_raw_sha256: config.previousVideoSha256 || null,
        raw_container_changed: config.previousVideoSha256 ? hash !== config.previousVideoSha256 : null,
        initiated_by: "console_stage_download_link", file: filename, ...inspected};
    });
    await check(`video_stage_${stage.id}_real_playback`, async () => {
      const selector = `#stages video[data-output="${stage.output.output_id}"]`;
      await page.waitForFunction(query => {
        const video = document.querySelector(query);
        return video?.readyState >= 2 && video.videoWidth > 0;
      }, selector, {timeout: 60000});
      const playback = await page.locator(selector).evaluate(async video => {
        const canvas = document.createElement("canvas");
        canvas.width = 96;
        canvas.height = 54;
        const context = canvas.getContext("2d", {willReadFrequently: true});
        const pixels = () => {
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          const rgba = context.getImageData(0, 0, canvas.width, canvas.height).data;
          let sum = 0, sumSquared = 0, nonblack = 0;
          const colors = new Set();
          for (let index = 0; index < rgba.length; index += 4) {
            const brightness = (rgba[index] + rgba[index + 1] + rgba[index + 2]) / 3;
            sum += brightness;
            sumSquared += brightness * brightness;
            if (rgba[index + 3] > 0 && brightness > 5) nonblack++;
            colors.add(`${rgba[index] >> 4},${rgba[index + 1] >> 4},${rgba[index + 2] >> 4}`);
          }
          const count = rgba.length / 4, mean = sum / count;
          return {media_time: video.currentTime, pixels: count, nonblack_pixels: nonblack,
            mean_brightness: mean, brightness_stddev: Math.sqrt(Math.max(0, sumSquared / count - mean * mean)),
            distinct_quantized_colors: colors.size};
        };
        video.muted = true;
        video.loop = true;
        video.currentTime = 0;
        const started = performance.now();
        const frameCount = () => typeof video.webkitDecodedFrameCount === "number" ?
          video.webkitDecodedFrameCount : (video.getVideoPlaybackQuality?.().totalVideoFrames ?? null);
        const qualityBefore = frameCount();
        const frames = [];
        let frameHandle, observing = true;
        const onFrame = (_now, metadata) => {
          if (!observing) return;
          frames.push({media_time: metadata.mediaTime, presented_frames: metadata.presentedFrames});
          frameHandle = video.requestVideoFrameCallback(onFrame);
        };
        if (video.requestVideoFrameCallback) frameHandle = video.requestVideoFrameCallback(onFrame);
        await video.play();
        const initial = video.currentTime;
        const needed = Math.min(0.8, video.duration / 3);
        while ((video.currentTime < initial + needed ||
          (video.requestVideoFrameCallback && frames.length < 3)) && performance.now() - started < 12000) {
          await new Promise(resolve => setTimeout(resolve, 80));
        }
        observing = false;
        if (frameHandle !== undefined) video.cancelVideoFrameCallback(frameHandle);
        const result = {video_width: video.videoWidth, video_height: video.videoHeight,
          duration: video.duration, current_time_before: initial, current_time_after: video.currentTime,
          elapsed_ms: Math.round(performance.now() - started), ready_state: video.readyState,
          decoded_frames_before: qualityBefore,
          decoded_frames_after: frameCount(),
          decoded_counter_source: typeof video.webkitDecodedFrameCount === "number" ?
            "webkitDecodedFrameCount" : "getVideoPlaybackQuality.totalVideoFrames",
          presented_frame_callbacks: frames.length, first_presented_frame: frames[0] || null,
          last_presented_frame: frames.at(-1) || null, pixel_sample: pixels(),
          source_path: new URL(video.currentSrc).pathname, media_error: video.error?.code || null};
        video.pause();
        return result;
      });
      requireThat(Number.isFinite(playback.duration) && playback.duration > 0, "video_duration_invalid");
      requireThat(playback.current_time_after > playback.current_time_before + 0.1, "video_time_did_not_advance");
      requireThat(!playback.media_error, "video_decode_error");
      requireThat(playback.source_path === routerOutput(stage.output).pathname, "displayed_video_output_mismatch");
      requireThat(playback.presented_frame_callbacks >= 3 &&
        playback.last_presented_frame.media_time > playback.first_presented_frame.media_time,
      "video_frames_not_presented");
      requireThat(playback.decoded_frames_after > playback.decoded_frames_before, "video_frames_not_decoded");
      requireThat(playback.pixel_sample.nonblack_pixels > playback.pixel_sample.pixels * 0.02 &&
        playback.pixel_sample.brightness_stddev > 1 && playback.pixel_sample.distinct_quantized_colors > 8,
      "video_frame_blank_or_uniform");
      const frameFile = `video-${stage.id}-decoded-frame.png`;
      await page.locator(selector).screenshot({path: path.join(runDirectory, frameFile)});
      const endpoint = `/api/media/videos/${job.id}/stages/${stage.id}/content`;
      const head = await request(endpoint, {method: "HEAD"});
      requireThat(head.status === 200 && Number(head.headers.get("content-length")) > 0,
        "video_head_failed");
      const range = await request(endpoint, {headers: {Range: "bytes=0-1023"}});
      requireThat(range.status === 206, "video_range_not_supported");
      const chunk = Buffer.from(await range.arrayBuffer());
      requireThat(chunk.length > 0 && chunk.length <= 1024, "video_range_length_invalid");
      const length = Number(head.headers.get("content-length"));
      requireThat(range.headers.get("content-range") === `bytes 0-${Math.min(1023, length - 1)}/${length}`,
        "video_range_offsets_invalid");
      const tail = await request(endpoint, {headers: {Range: "bytes=-1024"}});
      requireThat(tail.status === 206 && tail.headers.get("content-range") ===
        `bytes ${Math.max(0, length - 1024)}-${length - 1}/${length}`, "video_tail_range_offsets_invalid");
      const tailChunk = Buffer.from(await tail.arrayBuffer());
      requireThat(tailChunk.length === Math.min(1024, length), "video_tail_range_length_invalid");
      return {job_id: job.id, artifact_id: stage.output.id || stage.output.output_id,
        output_id: stage.output.output_id, stage: stage.id, ...playback,
        decoded_frame_screenshot: frameFile,
        content_length: length, range_status: range.status,
        content_range: range.headers.get("content-range"), range_bytes: chunk.length,
        tail_range_status: tail.status, tail_content_range: tail.headers.get("content-range"),
        tail_range_bytes: tailChunk.length};
    });
    await check(`video_stage_${stage.id}_survives_live_updates`, async () => {
      const selector = `#stages video[data-output="${stage.output.output_id}"]`;
      const observations = [];
      await page.locator(selector).evaluate(video => {video.muted = true; video.loop = true; return video.play();});
      const deadline = Date.now() + config.playbackObservationSeconds * 1000;
      try {
        do {
          await page.waitForTimeout(3000);
          const sample = await page.locator(selector).evaluate(video => ({
            current_time: video.currentTime, paused: video.paused, ready_state: video.readyState,
            video_width: video.videoWidth, video_height: video.videoHeight,
            decoded_frames: video.getVideoPlaybackQuality?.().totalVideoFrames ?? null,
            source_path: new URL(video.currentSrc).pathname, media_error: video.error?.code || null,
          }));
          const current = await json(`/api/media/videos/${job.id}`);
          requireThat(!sample.paused && sample.ready_state >= 2 && !sample.media_error &&
            sample.source_path === routerOutput(stage.output).pathname, "preview_interrupted_during_live_update");
          observations.push({at: new Date().toISOString(), playback: sample, job: snapshot(current)});
        } while (Date.now() < deadline);
      } finally {
        await page.locator(selector).evaluate(video => video.pause()).catch(() => {});
      }
      const index = job.stages.findIndex(item => item.id === stage.id);
      const laterIds = job.stages.slice(index + 1).map(item => item.id);
      const laterActive = observations.some(item => item.job.stages.some(value =>
        laterIds.includes(value.id) && ["queued", "running", "archiving"].includes(value.status)));
      return {output_id: stage.output.output_id, observation_seconds: config.playbackObservationSeconds,
        observations, later_stage_active_observed: laterActive, own_start_requests: 0};
    });
  }
  await check("video_archived_versions", async () => {
    const history = await verifyHistory(page, "videos", job.id);
    for (const stage of job.stages.filter(eligible)) {
      requireThat(history.versions.some(output => output.output_id === stage.output.output_id),
        "existing_preview_missing_from_history");
    }
    return history;
  });
  await screenshots(page, "video-result");
  report.video_state = snapshot(await json(`/api/media/videos/${job.id}`));
}

async function main() {
  const {values: args} = parseArgs({options: {
    baseline: {type: "boolean"}, "console-only": {type: "boolean"},
    scope: {type: "string"}, "approve-stage": {type: "string"},
    "exercise-grant": {type: "string"}, help: {type: "boolean"},
  }});
  if (args.help) {
    console.log([
      "Usage: node tests/browser_media_live.cjs [--baseline | --console-only] [--approve-stage context_ir]",
      "Private file: ~/.config/ai-router-media/acceptance.env (owner-only permissions)",
      "Required live: AI_ROUTER_ADMIN_KEY, AI_ROUTER_ACCEPTANCE_IMAGE_JOB_ID, AI_ROUTER_ACCEPTANCE_VIDEO_JOB_ID",
      "Optional: AI_ROUTER_CONTROL_URL (default http://127.0.0.1:4001), AI_ROUTER_ACCEPTANCE_CLIENT_ID",
      "Optional: PLAYWRIGHT_MODULE, PLAYWRIGHT_CHROMIUM_EXECUTABLE, AI_ROUTER_BROWSER_WAIT_SECONDS",
      "--scope all|console|image|approval|video|layout selects independently verifiable checks.",
      "Approval: an existing awaiting_approval stage with a never-started successor is required.",
      "AI_ROUTER_ACCEPTANCE_OUTPUT_ID optionally pins the exact assigned approval or playback output.",
      "AI_ROUTER_ACCEPTANCE_ARTIFACT_ID pins a specific public delivery ID without changing its H3 approval version.",
      "AI_ROUTER_BROWSER_REQUIRE_CLEAN_MEDIA=1 requires a distinct public artifact without workflow metadata.",
      "AI_ROUTER_BROWSER_EXPECT_RAW_STATUS=410 tests retired legacy raw IDs; use 404 for never-public new raw IDs.",
      "AI_ROUTER_BROWSER_PREVIOUS_VIDEO_SHA256 compares the clean delivery against the previous raw container.",
      "AI_ROUTER_BROWSER_RAW_PACKET_HASHES can provide private per-stream fingerprints to verify stream-copy equivalence.",
      "AI_ROUTER_BROWSER_NEXT_START_SECONDS allows observing an external start after approval.",
      "No task creation, stage starts, general settings, production-client writes, cancellation, deletion or restart.",
      "--console-only tests real login/options/settings/grants without requiring generated jobs.",
      "--exercise-grant media-other-20260904 changes and restores only that dedicated client's image grant.",
      "Exit 0: requested checks pass; 1: failure; 2: assigned live job is not ready.",
    ].join("\n"));
    return;
  }
  requireThat(!args["approve-stage"] || /^[a-z][a-z0-9_]*$/.test(args["approve-stage"]),
    "invalid_approval_stage");
  requireThat(!args.scope || ["all", "console", "image", "approval", "video", "layout"].includes(args.scope),
    "invalid_test_scope");
  requireThat(!(args.scope && args["console-only"]), "choose_scope_or_console_only");
  requireThat(!(args["approve-stage"] && (args.baseline || args["console-only"] ||
    ["image", "console", "layout"].includes(args.scope))),
    "approval_requires_artifact_mode");
  requireThat(args.scope !== "approval" || args["approve-stage"], "approval_scope_requires_assigned_stage");
  requireThat(!args["exercise-grant"] || args["exercise-grant"] === "media-other-20260904",
    "only_dedicated_acceptance_client_can_be_modified");
  requireThat(!(args.baseline && args["exercise-grant"]), "baseline_is_read_only");
  config = await loadConfig(args);
  report.mode = config.baseline ? "baseline" : "live";
  report.scope = config.scope;
  report.control_hostname = new URL(config.origin).hostname;
  report.control_port = new URL(config.origin).port;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  runDirectory = path.join(OUTPUT, `${report.mode}-${stamp}`);
  await fs.mkdir(runDirectory, {recursive: true, mode: 0o700});
  reportPath = path.join(runDirectory, "report.json");
  await saveReport();
  await preflight();
  const page = await guardedBrowser();
  if (config.baseline) {
    await page.goto(config.origin, {waitUntil: "domcontentloaded"});
    await screenshots(page, "control-baseline", {checkLayout: false});
    await page.goto(config.origin + "/media", {waitUntil: "domcontentloaded"});
    await screenshots(page, "media-baseline", {checkLayout: false});
    report.status = "baseline_recorded";
    report.live_functional_acceptance = "not_run";
  } else {
    requireThat(config.key, "private_admin_key_missing");
    if (["all", "image"].includes(config.scope)) {
      requireThat(ID.test(config.imageId) && config.imageId.startsWith("img_"), "assigned_image_id_missing_or_invalid");
    }
    if (["all", "approval", "video", "layout"].includes(config.scope)) {
      requireThat(ID.test(config.videoId) && config.videoId.startsWith("vid_"), "assigned_video_id_missing_or_invalid");
    }
    report.assigned_jobs = {image: config.imageId || null, video: config.videoId || null};
    await login(page);
    await check("authenticated_media_options", async () => {
      const options = await json("/api/media/options");
      requireThat(typeof options.enabled === "boolean", "options_enabled_flag_missing");
      requireThat(options.images && options.videos, "media_options_incomplete");
      return {enabled: options.enabled, image_option_fields: Object.keys(options.images).sort(),
        video_option_fields: Object.keys(options.videos).sort()};
    });
    await verifySettings(page);
    await exerciseGrant(page);
    if (config.scope === "console") {
      for (const view of ["images", "videos"]) {
        await page.locator(`[data-view="${view}"]`).click();
        await screenshots(page, `${view}-console`);
      }
      report.pending.push("Real image/video artifacts and stage approval/start separation are not exercised in console-only mode.");
    } else if (config.scope === "layout") {
      await selectJob(page, "videos", config.videoId);
      const mediaSelector = config.expectedOutputId ?
        `#stages video[data-output="${config.expectedOutputId}"]` : "#stages video";
      const target = page.locator(mediaSelector).last();
      await check("layout_target_renders_actual_video_frame", async () => {
        await target.scrollIntoViewIfNeeded();
        const frame = await target.evaluate(async video => {
          video.muted = true;
          video.currentTime = 0;
          await video.play();
          await new Promise(resolve => setTimeout(resolve, 650));
          const result = {output_id: video.dataset.output, width: video.videoWidth,
            height: video.videoHeight, current_time: video.currentTime,
            decoded_frames: video.webkitDecodedFrameCount ?? video.getVideoPlaybackQuality?.().totalVideoFrames,
            ready_state: video.readyState, media_error: video.error?.code || null};
          video.pause();
          return result;
        });
        requireThat(frame.width > 0 && frame.height > 0 && frame.current_time > 0.1 &&
          frame.decoded_frames > 0 && !frame.media_error, "layout_target_video_frame_not_ready");
        return frame;
      });
      await check("assigned_video_responsive_layout", () => screenshots(page, "video-layout", {mediaSelector}));
    } else {
      if (["all", "image"].includes(config.scope)) await verifyImage(page);
      await verifyApproval(page);
      // Waiting for and decoding the generated video is intentionally last.
      if (["all", "video"].includes(config.scope)) await verifyVideo(page);
    }
    requireThat(report.page_errors.length === 0, "browser_page_errors");
    requireThat(report.blocked_requests.length === 0, "unexpected_browser_requests_blocked");
    report.status = "passed";
    report.live_functional_acceptance = {
      console: "console_only", image: "assigned_image_only",
      layout: "assigned_video_layout_only",
      approval: "assigned_stage_approval_only", video: "assigned_video_playback_only",
      all: config.approveStage ? "assigned_artifacts_and_approval_verified" :
        "assigned_artifacts_verified_approval_not_exercised",
    }[config.scope];
    if (config.scope !== "all") report.pending.push("Other acceptance scopes must be verified in separate runs.");
    if (config.scope !== "console" && !config.approveStage) {
      report.pending.push("Stage approval/start separation requires a coordinated --approve-stage run.");
    }
  }
}

if (require.main === module) {
  main().catch(error => {
    report.status = error.code === "assigned_job_not_ready" ? "waiting_for_assigned_job" : "failed";
    report.error = errorEvidence(error);
    console.error(`${report.status.toUpperCase()} ${report.error.code}: ${report.error.message}`);
    process.exitCode = error.code === "assigned_job_not_ready" ? 2 : 1;
  }).finally(async () => {
    if (browser) await browser.close();
    report.approval_requests_sent = approvalsSent;
    report.grant_patch_requests_sent = grantWritesSent;
    report.finished_at = new Date().toISOString();
    await saveReport();
    if (reportPath) console.log("Report: " + reportPath);
  });
}

module.exports = {parseEnvironment, redact, snapshot, errorEvidence, privateVideoMarkers};
