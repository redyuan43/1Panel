// Exercise the shipped request-page functions with controlled HTTP completion.
// No service, browser dependency, or real model is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../ai_router/static/app.js'), 'utf8');
const names = ['requestFilterQuery', 'loadRequestTraces', 'resetRequestPagination',
  'requestIsTerminal', 'mergeRequestItems', 'refreshExpandedRequestConversations',
  'requestScrollAnchor', 'restoreRequestScrollAnchor', 'requestRoundTable',
  'requestTokenSummary', 'healthEvidenceLabel', 'fetchConversationTurns', 'fetchConversationTurnsPage'];
const shippedFunctions = names.map(name => {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert(start >= 0, `Missing shipped function ${name}`);
  const end = source.slice(start).search(/^\}$/m);
  assert(end >= 0, `Missing closing brace for ${name}`);
  return source.slice(start, start + end + 1);
}).join('\n');
const tick = () => new Promise(resolve => setImmediate(resolve));
const row = (n, status = 'succeeded', updated_at = n) => ({
  request_id: `r-${String(n).padStart(3, '0')}`, conversation_id: 'conversation-a',
  started_at: n, updated_at, status,
});
const page = (items, total_count = items.length, next_cursor = null) => ({items, total_count, next_cursor});

function harness(handler) {
  const requests = [], elements = new Map(), scrolls = [];
  let renders = 0, rowTop = 80;
  const byId = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', disabled: false});
    return elements.get(id);
  };
  const state = {key: 'synthetic-admin', requestTraces: [], requestTracePage: 1,
    requestTraceCursors: [null], requestConversationSummaries: new Map(),
    requestExpandedConversations: new Set(), requestExpandedRequests: new Set(),
    requestConversationPages: new Map()};
  const context = vm.createContext({URLSearchParams, Set, Map, Date, Number, String, Math, state, byId,
    api: url => handler ? handler(url) : new Promise((resolve, reject) => requests.push({url, resolve, reject})),
    document: {querySelectorAll: selector => selector === '[data-request-round-id]'
      ? [{dataset: {requestRoundId: 'r-001'}, getBoundingClientRect: () => ({top: rowTop, bottom: rowTop + 20})}] : []},
    window: {scrollBy: value => scrolls.push(value.top)},
    renderRequestTable: () => {renders++; rowTop += 40;},
    setConnected: () => {}, notice: () => {}, formatTime: String, formatTokens: String,
    requestRoundRow: item => `<tr data-request-round-id="${item.request_id}"></tr>`,
  });
  vm.runInContext('let requestTraceLoadSequence = 0, requestPageGeneration = 0;'
    + 'const conversationTurnLoads = new Map(), REQUEST_PAGE_SIZE = 50;\n' + shippedFunctions, context);
  return {context, state, requests, scrolls, renders: () => renders,
    call: (name, ...args) => context[name](...args)};
}

test('request history is newest first while shared audit cache stays chronological', () => {
  const h = harness();
  const cache = h.call('mergeRequestItems', [row(2), row(1)], [row(3)]);
  assert.deepEqual(Array.from(cache, x => x.request_id), ['r-001', 'r-002', 'r-003']);
  const html = h.call('requestRoundTable', cache);
  assert(html.indexOf('r-003') < html.indexOf('r-002'));
  assert(html.indexOf('r-002') < html.indexOf('r-001'));
  assert.deepEqual(Array.from(cache, x => x.request_id), ['r-001', 'r-002', 'r-003']);
});

test('periodic refresh updates running rows older than the newest 100 without losing pagination', async () => {
  const h = harness();
  h.state.requestExpandedConversations.add('conversation-a');
  h.state.requestConversationPages.set('conversation-a', {items: Array.from({length: 150}, (_, i) => row(i + 1)),
    nextCursor: 'older-page-cursor', totalCount: 200});
  h.state.requestConversationPages.get('conversation-a').items[0] = row(1, 'running');
  const refresh = h.call('loadRequestTraces', true);
  h.requests[0].resolve(page([row(151)], 201));
  await tick();
  assert(new URLSearchParams(h.requests[1].url.split('?')[1]).has('conversation_id'));
  h.requests[1].resolve(page(Array.from({length: 100}, (_, i) => row(151 - i)), 201, 'new-head-cursor'));
  await tick();
  const batch = new URLSearchParams(h.requests[2].url.split('?')[1]);
  assert.deepEqual(batch.getAll('request_ids'), ['r-001']);
  h.requests[2].resolve(page([row(1, 'succeeded', 999)]));
  await refresh;
  const cached = h.state.requestConversationPages.get('conversation-a');
  assert.equal(cached.items.find(x => x.request_id === 'r-001').status, 'succeeded');
  assert.equal(cached.items.length, 151);
  assert.equal(cached.nextCursor, 'older-page-cursor');
  assert(h.state.requestExpandedConversations.has('conversation-a'));
  assert.equal(h.renders(), 1);
  assert.deepEqual(h.scrolls, [40], 'Keep the visible row at the same screen position');
});

test('refresh splits many pending rows into batches of at most 100', async () => {
  const sizes = [];
  const items = Array.from({length: 205}, (_, i) => row(i + 1, 'running'));
  const h = harness(async url => {
    const query = new URLSearchParams(url.split('?')[1]);
    if (query.has('conversation_id')) return page(items.slice(-100), 205);
    const ids = query.getAll('request_ids');
    sizes.push(ids.length);
    return page(items.filter(x => ids.includes(x.request_id)).map(x => ({...x, status: 'succeeded'})));
  });
  h.state.requestTraces = [items.at(-1)];
  h.state.requestExpandedConversations.add('conversation-a');
  h.state.requestConversationPages.set('conversation-a', {items, totalCount: 205, nextCursor: null});
  await h.call('refreshExpandedRequestConversations', 0, 0);
  assert.deepEqual(sizes, [100, 100, 5]);
  assert(h.state.requestConversationPages.get('conversation-a').items.every(x => x.status === 'succeeded'));
});

test('disjoint new head retains old running turns and exposes a cursor to fill the gap', async () => {
  const queried = [];
  const h = harness(async url => {
    const query = new URLSearchParams(url.split('?')[1]);
    const ids = query.getAll('request_ids');
    if (ids.length) { queried.push(...ids); return page([row(1, 'succeeded', 400)]); }
    if (query.get('cursor') === 'gap') return page(Array.from({length: 100}, (_, i) => row(200 - i)), 300, 'older');
    return page(Array.from({length: 100}, (_, i) => row(300 - i)), 300, 'gap');
  });
  h.state.requestTraces = [row(300)];
  h.state.requestExpandedConversations.add('conversation-a');
  h.state.requestConversationPages.set('conversation-a', {items: Array.from({length: 150}, (_, i) => row(i + 1, i ? 'succeeded' : 'running')),
    nextCursor: null, totalCount: 150});
  await h.call('refreshExpandedRequestConversations', 0, 0);
  let cached = h.state.requestConversationPages.get('conversation-a');
  assert.equal(cached.items.length, 250);
  assert.equal(cached.nextCursor, 'gap');
  assert(queried.includes('r-001'));
  assert.equal(cached.items[0].status, 'succeeded');
  await h.call('fetchConversationTurns', 'conversation-a', 'gap');
  cached = h.state.requestConversationPages.get('conversation-a');
  assert.equal(cached.items.length, 300);
  assert.equal(cached.items[0].request_id, 'r-001');
  assert.equal(cached.items.at(-1).request_id, 'r-300');
});

test('late main-list response cannot overwrite the newer result', async () => {
  const h = harness();
  const old = h.call('loadRequestTraces', true);
  const fresh = h.call('loadRequestTraces', true);
  h.requests[1].resolve(page([row(1, 'succeeded', 30)]));
  await fresh;
  h.requests[0].resolve(page([row(1, 'running', 10)]));
  await old;
  assert.equal(h.state.requestTraces[0].status, 'succeeded');
  assert.equal(h.renders(), 1);
});

test('old snapshots never revive a terminal request or replace a newer update', () => {
  const h = harness();
  const merged = h.call('mergeRequestItems', [row(1, 'succeeded', 20), row(2, 'running', 30)],
    [row(1, 'running', 40), row(2, 'queued', 10)]);
  assert.equal(merged[0].status, 'succeeded');
  assert.equal(merged[1].status, 'running');
});

test('filter reset invalidates in-flight conversation pages', async () => {
  const h = harness();
  const old = h.call('fetchConversationTurns', 'conversation-a');
  await tick();
  h.call('resetRequestPagination');
  const current = h.call('fetchConversationTurns', 'conversation-a');
  h.requests[0].resolve(page([row(1)]));
  await old;
  await tick();
  assert.equal(h.state.requestConversationPages.get('conversation-a').items.length, 0);
  h.requests[1].resolve(page([row(2)]));
  await current;
  assert.deepEqual(Array.from(h.state.requestConversationPages.get('conversation-a').items, x => x.request_id), ['r-002']);
});

test('loading an older page appends it below newer history', async () => {
  const h = harness(async () => page([row(2), row(1)], 4, null));
  h.state.requestConversationPages.set('conversation-a', {items: [row(3), row(4)], nextCursor: 'older', totalCount: 4});
  await h.call('fetchConversationTurns', 'conversation-a', 'older');
  const cached = h.state.requestConversationPages.get('conversation-a');
  const html = h.call('requestRoundTable', cached.items);
  assert(html.indexOf('r-004') < html.indexOf('r-003'));
  assert(html.indexOf('r-003') < html.indexOf('r-002'));
  assert(html.indexOf('r-002') < html.indexOf('r-001'));
  assert.equal(cached.nextCursor, null);
});

test('pagination failure does not undo a concurrent completion update', async () => {
  const h = harness();
  h.state.requestConversationPages.set('conversation-a', {items: [row(1, 'running')], nextCursor: 'older', totalCount: 2});
  const request = h.call('fetchConversationTurns', 'conversation-a', 'older');
  await tick();
  h.state.requestConversationPages.get('conversation-a').items = [row(1, 'succeeded', 10)];
  h.requests[0].reject(new Error('Synthetic pagination failure'));
  await request;
  const cached = h.state.requestConversationPages.get('conversation-a');
  assert.equal(cached.items[0].status, 'succeeded');
  assert.equal(cached.error, 'Synthetic pagination failure');
});

test('token labels separate unknown, pending, measured zero, and estimates', () => {
  const h = harness();
  const legacy = h.call('requestTokenSummary', {...row(1, 'running'), prompt_tokens: 386167, input_tokens: 0});
  assert(legacy.includes('原始估算 386167'));
  assert(legacy.includes('目标输入 未知'));
  assert(legacy.includes('实际输入 待返回'));
  const completed = h.call('requestTokenSummary', row(1));
  assert(completed.includes('实际输入 未提供'));
  const unknown = h.call('requestTokenSummary', {...row(1), prompt_tokens: 0,
    token_summary: {ingress_estimated_input_tokens: null}});
  assert(unknown.includes('原始估算 未知'));
  const measured = h.call('requestTokenSummary', {...row(1), token_summary: {
    target_input_tokens: 187506, target_count_exact: true, measured_input_tokens: 187506,
    measured_output_tokens: 0, output_reserve_tokens: 65536, required_context_tokens: 253042, safe_context_tokens: 262144,
  }});
  assert(measured.includes('目标输入 187506（精确）'));
  assert(measured.includes('实际输入 187506 · 输出 0'));
  assert(measured.includes('187506 + 65536 = 253042 / 262144'));
});

test('health labels distinguish expiry, concrete failure, and unavailable historical evidence', () => {
  const h = harness();
  assert.equal(h.call('healthEvidenceLabel', {healthy: false}), '健康异常（原因未留存）');
  const expired = h.call('healthEvidenceLabel', {health_evidence: {healthy: true, stale: true, age_seconds: 20}});
  assert(expired.includes('状态过期'));
  assert(!expired.includes('探测失败'));
  const failed = h.call('healthEvidenceLabel', {health_evidence: {healthy: false, stale: false,
    failure: {category: 'authentication', http_status: 401}}});
  assert.equal(failed, '鉴权失败 · HTTP 401');
});
