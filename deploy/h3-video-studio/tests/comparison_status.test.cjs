const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness(records) {
  const articles = records.map(record => ({
    dataset: {case: record.id}, line: {textContent: ''}, video: {src: 'original.mp4', currentTime: 7},
    querySelector(selector) { return selector === '.run-state' ? this.line : selector === 'video' ? this.video : null; },
  }));
  const execution = {textContent: ''};
  const cards = {querySelectorAll: () => articles};
  const context = vm.createContext({
    document: {querySelector: selector => selector === '#cards' ? cards : execution},
    fetch: async () => ({ok: true, json: async () => ({available: true, observed_at: Date.now() / 1000, cases: records})}),
    setInterval() {},
  });
  return {context, articles, execution};
}

test('comparison displays GPU and waiting reason without replacing original replica video', async () => {
  const {context, articles, execution} = harness([
    {id: 'A4_C1', status: 'running', lane: 'fast', gpu_uuid: 'GPU-fast', prompt_id: 'owned-job'},
    {id: 'A4_C05', status: 'prepared', lane: 'preview', gpu_uuid: 'GPU-preview', replica: true,
      admission_reason: 'admission_cgroup_remaining_peak_budget'},
  ]);
  const html = fs.readFileSync(path.join(__dirname, '../frontend/comparison.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0];
  vm.runInContext(script.slice(0, script.indexOf("document.querySelector('#refresh').addEventListener")), context);
  await vm.runInContext('updateExecution()', context);
  assert.match(execution.textContent, /fast · GPU GPU-fast/);
  assert.match(articles[1].line.textContent, /等待资源准入/);
  assert.match(articles[1].line.textContent, /preview · GPU GPU-preview/);
  assert.match(articles[1].line.textContent, /本卡预览仍为原成片，不代表本轮结果/);
  assert.equal(articles[1].video.src, 'original.mp4');
  assert.equal(articles[1].video.currentTime, 7);
});

test('studio banner labels new cases and prepared replicas without claiming submission', async () => {
  const {context, execution} = harness([
    {id: 'A4_C1', status: 'running', lane: 'fast'},
    {id: 'A4_C0', status: 'prepared', lane: 'main'},
    {id: 'A4_C05', status: 'prepared', lane: 'preview', replica: true},
  ]);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/comparison-status.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(execution.textContent, /A4＋人物 LoRA 1.0/);
  assert.match(execution.textContent, /A4＋人物触发词/);
  assert.match(execution.textContent, /A4＋人物 LoRA 0.5（复验，原预览保留）/);
  assert.match(execution.textContent, /待准入（尚未提交）/);
  assert.doesNotMatch(execution.textContent, /undefined|NaN/);
});

test('retained C1 distinguishes task success from failed batch before and after publication', async () => {
  const {context, articles} = harness([
    {id: 'A4_C1', status: 'completed', lane: 'fast', gpu_uuid: 'GPU-fast', batch_status: 'failed',
      retained_from_failed_batch: true, batch_error: 'C0 CUDA OOM'},
  ]);
  const html = fs.readFileSync(path.join(__dirname, '../frontend/comparison.html'), 'utf8');
  const script = html.split('<script>')[1].split('</script>')[0];
  vm.runInContext(script.slice(0, script.indexOf("document.querySelector('#refresh').addEventListener")), context);
  await vm.runInContext('updateExecution()', context);
  assert.match(articles[0].line.textContent, /本条成片已保留；所属并发批次失败/);
  assert.doesNotMatch(articles[0].line.textContent, /本次执行失败/);
  assert.equal(articles[0].video.src, 'original.mp4');
  articles[0].querySelector = function (selector) {
    return selector === '.run-state' ? this.line : selector === '.pending' ? {textContent: ''} : null;
  };
  await vm.runInContext('updateExecution()', context);
  assert.match(articles[0].line.textContent, /本条已成片，待单独发布；所属并发批次失败/);
});
