// Exercise the actual settings render/collect functions without production calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ai_router/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../ai_router/static/index.html'), 'utf8');
const nodes = new Map();
const byId = id => {
  if (!nodes.has(id)) nodes.set(id, {value:'', checked:false, replaceChildren(){}, append(){}});
  return nodes.get(id);
};
const state = {settings:{identity:{review:{}}, routing:{}, context_policy:{
  mode:'extended', endpoint_id:'codex-pro-gpt-6-astra', extended_context_tokens:500000,
}}};
const context = vm.createContext({state, byId, console,
  document:{querySelectorAll:()=>[], querySelector:()=>null},
  RoutingModeUI:{render(){},collect(){return {};}},
  value:(key,fallback='')=>key.split('.').reduce((value,part)=>value?.[part],state.settings) ?? fallback,
  promptDirectiveSettings:()=>({routes:{},reset:{}}), NEW_PROMPT_DIRECTIVES:{},
  csv:value=>value.split(',').filter(Boolean), cssEscape:String, weightLabels:{},
  validateReviewBaseUrl:()=>'',
  collectRemoteFallbackOrder:()=>({}),
  renderPromptDirectives(){}, renderRemoteFallbackOrder(){}, renderLmcacheRuntimeStatus(){},
  updateStrategyBranchVisibility(){}, updateWeightsTotal(){}, renderPolicyState(){},
});
vm.runInContext(source.slice(source.indexOf('function renderSettings()'), source.indexOf('function renderPolicyState()')), context);
vm.runInContext(source.slice(source.indexOf('function collectSettings()'), source.indexOf('async function saveSettings(')), context);
vm.runInContext(source.slice(source.indexOf('function validateSettingsDraft('), source.indexOf('function collectSettings()')), context);
for (const mode of ['legacy','compact','extended']) {
  state.settings.context_policy.mode = mode;
  vm.runInContext('renderSettings()', context);
  assert.equal(byId('context-policy-mode').value, mode);
  assert.equal(byId('context-policy-limit').value, 500000);
  const result = vm.runInContext('collectSettings().context_policy', context);
  assert.equal(result.mode, mode);
  assert.equal(result.endpoint_id, 'codex-pro-gpt-6-astra');
  assert.equal(result.extended_context_tokens, 500000);
}
assert(html.includes('id="prompt-enhancement-enabled"'));
assert.equal(byId('prompt-enhancement-enabled').checked, false);
byId('prompt-enhancement-enabled').checked = true;
assert.equal(vm.runInContext('collectSettings().routing.prompt_enhancement.enabled', context), true);
state.settings.routing.prompt_enhancement = {enabled: true};
vm.runInContext('renderSettings()', context);
assert.equal(byId('prompt-enhancement-enabled').checked, true);
delete state.settings.context_policy;
vm.runInContext('renderSettings()', context);
assert.equal(byId('context-policy-mode').value, 'legacy');
byId('context-policy-limit').value = '1050001';
assert(vm.runInContext('validateSettingsDraft(collectSettings()).some(error => error.includes("扩展上下文预算"))', context));
for (const id of ['context-policy-mode','context-policy-endpoint','context-policy-limit']) {
  assert(html.includes(`id="${id}"`));
}
console.log('PASS: context strategy settings render/collect round-trip and legacy defaults');
assert.equal(byId('compaction-background').checked, false);
assert.equal(byId('history-query-rewrite').checked, false);
byId('history-query-rewrite').checked = true;
assert.equal(vm.runInContext('collectSettings().compaction.history_query_rewrite_enabled', context), true);
byId('compaction-background').checked = true;
assert.equal(vm.runInContext('collectSettings().compaction.background_enabled', context), true);
for (const [id, field, value] of [
  ['compaction-budget-seconds', 'max_seconds', 30],
  ['compaction-budget-calls', 'max_calls', 2],
  ['compaction-budget-input', 'max_input_tokens', 50000],
  ['compaction-budget-output', 'max_output_tokens', 16384],
]) {
  assert(html.includes(`id="${id}"`));
  byId(id).value = String(value);
  assert.equal(vm.runInContext(`collectSettings().compaction.background_limits.${field}`, context), value);
}
byId('compaction-budget-calls').value = '33';
assert(vm.runInContext('validateSettingsDraft(collectSettings()).some(error => error.includes("后台压缩预算 max_calls"))', context));
byId('compaction-budget-calls').value = '2';
context.publicIdentityModelId = () => 'siyuan/auto';
vm.runInContext(source.slice(source.indexOf('function collectClient()'), source.indexOf('async function saveClient(')), context);
vm.runInContext(source.slice(source.indexOf('function syncHistoryGrants()'), source.indexOf('async function refreshHistoryStatus(')), context);
byId('client-disclosure-mode').value = 'public';
assert(!html.includes('id="client-history-owner"'));
byId('client-history-recall').checked = true;
byId('client-history-cloud').checked = true;
byId('client-history-legacy-cloud').checked = true;
vm.runInContext('syncHistoryGrants()', context);
assert.equal(vm.runInContext('collectClient().history_cloud_allowed', context), true);
assert.equal(vm.runInContext('collectClient().history_legacy_cloud_allowed', context), true);
byId('client-history-recall').checked = false;
vm.runInContext('syncHistoryGrants()', context);
assert.equal(byId('client-history-recall').checked, false);
assert.equal(byId('client-history-cloud').checked, false);
assert.equal(byId('client-history-legacy-cloud').checked, false);
assert.equal(byId('client-history-legacy-cloud').disabled, true);
byId('client-history-recall').checked = true;
vm.runInContext('syncHistoryGrants()', context);
assert.equal(byId('client-history-cloud').checked, false);
assert.equal(byId('client-history-recall').disabled, false);
byId('client-history-cloud').checked = true;
byId('client-local-only').checked = true;
vm.runInContext('syncHistoryGrants()', context);
assert.equal(vm.runInContext('collectClient().history_cloud_allowed', context), false);
console.log('PASS: history grant revocation, no implicit cloud regrant, background settings');
assert.equal(byId('history-index-enabled').checked, true);
byId('history-index-enabled').checked = false;
byId('history-index-records').value = '4';
byId('history-index-seconds').value = '0.1';
byId('history-index-duty').value = '10';
assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('collectSettings().compaction.history_indexing', context))),
  {enabled:false, batch_records:4, max_batch_seconds:0.1, duty_cycle:0.1});
byId('history-index-duty').value = '51';
assert(vm.runInContext('validateSettingsDraft(collectSettings()).some(error => error.includes("历史整理参数"))', context));
for (const id of ['history-index-enabled','history-index-records','history-index-seconds','history-index-duty']) {
  assert(html.includes(`id="${id}"`));
}
console.log('PASS: shared history indexing defaults, pause and bounded settings');
