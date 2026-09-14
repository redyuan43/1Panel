const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function harness() {
  const timers = new Map();
  const elements = new Map();
  const context = vm.createContext({
    window: {location: {pathname: "/"}, dispatchEvent() {}},
    Event,
    document: {addEventListener() {}, getElementById(identifier) {
      if (!elements.has(identifier)) elements.set(identifier, {classList: {add() {}, remove() {}}, setAttribute() {}});
      return elements.get(identifier);
    }},
    AbortController,
    console,
    setTimeout(callback, delay) { const token = {}; timers.set(token, {callback, delay}); return token; },
    clearTimeout(token) { timers.delete(token); },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8"), context);
  vm.runInContext("renderStudio = () => {}; closeHistory = () => {};", context);
  return {context, timers, elements, run: (source) => vm.runInContext(source, context)};
}

test("capacity separates idle GPUs from memory-admitted full-duration slots", async () => {
  const {context, elements, run} = harness();
  context.snapshot = {available: true, sampled_at: 100, active: 0, queued: 0,
    lanes: ["fast", "main", "preview"].map(id => ({id, name: id, status: "idle"})),
    limits: {long: 1}, resources_ok: true, memory_available_bytes: 35 * 1024 ** 3, swap_used_bytes: 0,
    studio_preview: {max_parallel: 3, available: 1, reason: "ram_headroom"}};
  run("api = async () => snapshot");
  await run("refreshCapacity()");
  assert.match(elements.get("capacitySummary").textContent, /空闲 3\/3/);
  assert.match(elements.get("capacityPolicy").textContent, /当前还可启动 1 路/);
  assert.match(elements.get("capacityPolicy").textContent, /主机内存余量不足/);
  run("api = async () => { throw new Error('unavailable'); }");
  await run("refreshCapacity()");
  assert.equal(elements.get("capacityLanes").innerHTML, "");
  assert.doesNotMatch(elements.get("capacityPolicy").textContent, /还可启动/);
});

test("disabled retired hardware is absent from the current GPU display", async () => {
  const {context, elements, run} = harness();
  context.snapshot = {available: true, sampled_at: 100, active: 0, queued: 0,
    nodes: [{id: "ivan", enabled: false, available: true, active: 0}],
    lanes: [{id: "fast", name: "retired-4060", enabled: false, status: "unknown"},
            {id: "main", name: "current-3060", enabled: true, status: "idle"}],
    limits: {long: 0}, resources_ok: true};
  run("api = async () => snapshot");
  await run("refreshCapacity()");
  assert.match(elements.get("capacitySummary").textContent, /空闲 1\/1/);
  assert.doesNotMatch(elements.get("capacityLanes").innerHTML, /retired-4060/);
  assert.match(elements.get("capacityLanes").innerHTML, /current-3060/);
});

function project(identifier) {
  return {id: identifier, stages: {preview: {status: "running"}}, pipeline: [{id: "preview", status: "running"}]};
}

test("finished duration stays fixed and includes queued time", () => {
  const {context, run} = harness();
  context.stage = {queued_at: 100, started_at: 130, finished_at: 3700};
  assert.equal(run("elapsedSeconds(stage)"), 3600);
  assert.equal(run("formatSeconds(elapsedSeconds(stage))"), "1小时00分00秒");
  context.history = {pipeline: [{...context.stage, label: "低清预览"}]};
  assert.equal(run("historyTimings(history)"), "低清预览：1小时00分00秒");
});

test("memory formatting distinguishes zero swap from unavailable telemetry", () => {
  const {run} = harness();
  assert.equal(run("formatBytes(0)"), "0 B");
  assert.equal(run("formatBytes(null)"), "—");
  assert.equal(run("formatBytes(64 * 1024 ** 3)"), "64.0 GiB");
});

test("opening a project directly starts polling", async () => {
  const {context, timers, run} = harness();
  context.result = project("direct");
  run("api = async () => result");
  await run('openProject("direct", "preview")');
  assert.equal(run("state.project.id"), "direct");
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 3000);
});

test("old project polling cannot overwrite the newly selected project", async () => {
  const {context, run} = harness();
  context.oldProject = project("old");
  context.newProject = project("new");
  run('state.project = oldProject; api = (url) => url.endsWith("/old") ? new Promise(resolve => { globalThis.finishOld = resolve; }) : Promise.resolve(newProject)');
  const pending = run("refreshCurrentProject()");
  await run('openProject("new")');
  context.finishOld(context.oldProject);
  await pending;
  assert.equal(run("state.project.id"), "new");
});

test("rapid history clicks keep the last selection despite response order", async () => {
  const {context, run} = harness();
  context.requests = {};
  run("api = (url) => new Promise(resolve => { requests[url] = resolve; })");
  const first = run('openProject("first")');
  const second = run('openProject("second")');
  context.requests["/api/projects/second"](project("second"));
  await second;
  context.requests["/api/projects/first"](project("first"));
  await first;
  assert.equal(run("state.project.id"), "second");
});

test("leaving for setup invalidates an in-flight project load", async () => {
  const {context, timers, run} = harness();
  run("api = () => new Promise(resolve => { globalThis.finish = resolve; })");
  const pending = run('openProject("old")');
  run("showSetup()");
  context.finish(project("old"));
  await pending;
  assert.equal(run("state.project"), null);
  assert.equal(timers.size, 0);
});
