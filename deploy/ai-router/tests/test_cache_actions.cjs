// CPU-only: execute the shipped handler with isolated state and mocked transport.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[2] || require("node:path").join(__dirname, "../ai_router/static/app.js"), "utf8");
const endpointHandler = source.slice(source.indexOf("async function runEndpointAction("), source.indexOf("function renderWorkerTable("));
const handler = source.slice(source.indexOf("async function runCacheDeploymentAction("), source.indexOf("function selectedCacheDeployment("));
async function check(actionId, conflict = false) {
  const calls = [], refreshes = [], notices = [];
  const row = {id: "edge", management: {endpoint_id: "edge-qwen38-flash", revision: 9,
    actions: [{id: actionId, kind: "endpoint", label: actionId}]}};
  const context = {state: {cacheDeployments: {deployments: [row]},
    dashboard: {endpoints: [{endpoint: {id: "edge-qwen38-flash", enabled: false, auto_candidate: false}, management: {revision: 10}}]}},
    window: {confirm: () => true},
    api: async (url, options) => {calls.push([url, JSON.parse(options.body)]); if (conflict) throw Object.assign(new Error("conflict"), {status: 409});},
    loadDashboard: async () => refreshes.push("dashboard"), loadCacheDeployments: async () => refreshes.push("cache"),
    notice: (...args) => notices.push(args), cacheDeploymentActionLabel: a => a.id};
  vm.createContext(context); vm.runInContext(handler + endpointHandler + "function endpointItem(id) { return state.dashboard.endpoints.find(x => x.endpoint.id === id); }", context);
  await context.runCacheDeploymentAction("edge", actionId, "9");
  assert.equal(calls.length, 1); assert.equal(calls[0][0], `/api/endpoints/edge-qwen38-flash/actions/${actionId}`);
  assert.equal(calls[0][1].expected_revision, 9);
  assert.deepEqual(refreshes.sort(), ["cache", "dashboard"]);
  assert.equal(context.state.selectedCacheDeploymentId, "edge");
  if (conflict) assert.match(notices[0][0], /未自动重试/);
  calls.length = 0;
  await context.runCacheDeploymentAction("edge", actionId, "");
  assert.equal(calls.length, 0, "missing revision cannot bypass optimistic locking");
  row.management.actions = [];
  await context.runCacheDeploymentAction("edge", actionId, "9");
  assert.equal(calls.length, 0, "read-only deployment cannot act");
}
function checkUnknownSummary() {
  const context = {formatTokens: x => String(x)};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf("function cacheDeploymentObservedSummary("), source.indexOf("function cacheDeploymentPersistence(")), context);
  for (const reason of ["cache_probe_unavailable", "cache_evidence_stale"]) {
    const text = context.cacheDeploymentObservedSummary({id: "ai-v100-tp2", desired: {lmcache: {enabled: true}},
      observed: {cache_health: {healthy: null, reason}, cache: {lmcache: {healthy: true}}}});
    assert.match(text[0], /健康未确认/);
    assert.match(text[1], /—\/—/);
    assert.doesNotMatch(text[1], /0\/0/);
  }
}
(async () => {
  checkUnknownSummary();
  for (const action of ["disable", "enable", "auto-disable", "auto-enable"]) {
    await check(action); await check(action, true);
  }
  console.log("8 cache action scenarios and 2 unknown-summary cases passed (snapshot disagreement, revision conflicts, read-only, missing revision)");
})().catch(error => {console.error(error); process.exitCode = 1;});
