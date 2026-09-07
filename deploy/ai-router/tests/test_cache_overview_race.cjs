// Run actual overview code with controlled network completion. No browser/model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
function byId(id) {
  if (!elements.has(id)) elements.set(id, {value: '', textContent: '', innerHTML: '',
    querySelectorAll: () => [], addEventListener(event, fn) {this[event] = fn;}});
  return elements.get(id);
}
byId('cache-hours').value = '24';
byId('cache-client-group').value = 'workbuddy';
const pending = [];
const context = vm.createContext({URLSearchParams, Map, Set, Date, Number, String, Math,
  byId, state: {key:'synthetic-admin'}, document:{querySelectorAll:()=>[]},
  escapeHtml:String, formatTime:String, statusLabels:{},
  api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../ai_router/static/cache-audit.js'),'utf8'),context);
vm.runInContext('initializeCacheAudit()',context);
function result(label) {
  const metrics=Object.fromEntries(['ttft_ms','first_text_ms','queue_ms','prefill_ms','restore_ms','net_cache_ratio','fixed_reuse_ratio','prefill_tps'].map(k=>[k,{n:0,median:null,p95:null}]));
  return {total:label,succeeded:0,metrics,fixed_pass:{passed:0,n:0},trend:[],items:[],next_offset:null};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const first=vm.runInContext('loadCacheOverview()',context);
  assert.equal(pending.length,2);
  byId('cache-device').value='new-device';
  byId('cache-filter-form').submit({preventDefault(){}});
  // The old result must never render under the newly submitted filter.
  for(const request of pending.slice(0,2)) request.resolve(result(99));
  await first;await tick();
  assert.equal(pending.length,4,'the submitted filter must be fetched after the in-flight request');
  assert(!byId('cache-overview-state').textContent.includes('99 条请求'),'obsolete data must not render');
  assert(pending.slice(2).every(x=>x.url.includes('device=new-device')));
  for(const request of pending.slice(2)) request.resolve(result(7));
  await tick();
  assert(byId('cache-overview-state').textContent.includes('7 条请求'));
  assert.equal(vm.runInContext('cacheView.loading',context),false);
  console.log('PASS: submitted filters supersede in-flight overview data');
})().catch(e=>{console.error(e);process.exitCode=1;});
