const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../frontend/comparison-status.js'), 'utf8');

async function render(data, ok = true) {
  const element = {textContent: ''};
  vm.runInNewContext(source, {
    document: {querySelector: () => element}, Date, Number, Array,
    fetch: async () => ({ok, json: async () => data}), setInterval: () => {},
  });
  await new Promise(resolve => setImmediate(resolve));
  return element.textContent;
}

test('empty comparisons never claim the studio has no running tasks', async () => {
  const text = await render({available: true, observed_at: Date.now() / 1000, cases: []});
  assert.match(text, /暂无运行中的比较实验/);
  assert.match(text, /不含工作室生成任务/);
  assert.doesNotMatch(text, /当前无已确认运行的任务/);
});

test('running comparisons remain identified separately', async () => {
  const text = await render({available: true, observed_at: Date.now() / 1000, cases: [{id: 'B8', status: 'running', lane: 'fast'}]});
  assert.match(text, /比较实验正在生成.*B8.*fast/);
});

test('unavailable and stale samples do not imply idle', async () => {
  for (const sample of [{available: false}, {available: true, observed_at: 0, cases: []}, {available: true, cases: []}]) {
    assert.match(await render(sample), /状态暂不可核实（不代表工作室无任务）/);
  }
});

test('small cross-host clock skew does not disable the banner', async () => {
  const text = await render({available: true, observed_at: Date.now() / 1000 + 2,
    cases: [{id: 'A4_C1', status: 'running', lane: 'fast'}]});
  assert.match(text, /比较实验正在生成.*A4_C1.*fast/);
  assert.doesNotMatch(text, /状态暂不可核实/);
});

test('clearly future samples stay unreconciled instead of claiming progress', async () => {
  const text = await render({available: true, observed_at: Date.now() / 1000 + 60,
    cases: [{id: 'B8', status: 'running', lane: 'fast'}]});
  assert.match(text, /状态暂不可核实（不代表工作室无任务）/);
});

test('future started_at never renders negative elapsed minutes', async () => {
  const text = await render({available: true, observed_at: Date.now() / 1000,
    cases: [{id: 'B8', status: 'running', lane: 'fast', started_at: Date.now() / 1000 + 5}]});
  assert.match(text, /（0分钟）/);
  assert.doesNotMatch(text, /-\d+分钟/);
});
