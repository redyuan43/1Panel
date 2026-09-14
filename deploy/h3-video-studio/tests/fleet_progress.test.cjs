const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function view(stage) {
  const context = vm.createContext({stage, window: {location: {pathname: '/'}},
    document: {addEventListener() {}}, console});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8'), context);
  return JSON.parse(JSON.stringify(vm.runInContext('stageProgressView(stage)', context)));
}

test('four confirmed sampler steps while decoding are not full-video 100 percent', () => {
  const result = view({id: 'preview', status: 'running', progress: 1,
    execution: {phase: 'decoding', sampler_progress: {completed: 4, total: 4,
      basis: 'confirmed_owned_sampler_events'}}});
  assert.equal(result.label, '解码中 · 采样 4/4 步');
  assert.equal(result.indeterminate, true);
  assert.doesNotMatch(result.label, /%/);
});

test('unknown running phase does not display fabricated one percent', () => {
  const result = view({id: 'preview', status: 'running', progress: 1, execution: {}});
  assert.equal(result.label, '执行中');
  assert.equal(result.indeterminate, true);
});

test('complete video is determinate one hundred percent', () => {
  assert.deepEqual(view({id: 'preview', status: 'awaiting_approval', progress: 100}),
    {label: '100%', width: '100%', indeterminate: false});
});

function health(result) {
  const context = vm.createContext({result, window: {location: {pathname: '/'}},
    document: {addEventListener() {}}, console});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8'), context);
  return JSON.parse(JSON.stringify(vm.runInContext('healthPresentation(result)', context)));
}

test('local Fleet stays ready when optional cloud credentials are absent', () => {
  const result = health({execution_mode: 'ivan-fleet', ok: true, minimaxConfigured: false});
  assert.equal(result.ready, true);
  assert.equal(result.label, '本地执行服务就绪');
  assert.equal(result.cloudNote, '云端未配置，不影响本地原文生成');
});

test('cloud configuration does not conceal an actual local Fleet failure', () => {
  const result = health({execution_mode: 'ivan-fleet', ok: false, minimaxConfigured: true});
  assert.equal(result.ready, false);
  assert.equal(result.label, '执行服务需检查');
});
