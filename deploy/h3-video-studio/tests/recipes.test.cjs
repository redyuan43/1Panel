const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function harness() {
  const elements = new Map();
  const context = vm.createContext({
    window: {location: {pathname: "/"}},
    document: {addEventListener() {}, getElementById(identifier) {
      if (!elements.has(identifier)) elements.set(identifier, {classList: {remove() {}}});
      return elements.get(identifier);
    }},
    console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8"), context);
  return {context, elements, run: source => vm.runInContext(source, context)};
}

test("only the four formal recipes are selectable, with no legacy replacement", () => {
  const {run} = harness();
  assert.equal(run("RECIPE_OPTIONS.map(entry => entry.recipe_id).join(',')"), "A4,A4_C0,A4_C1,B8");
  assert.match(run("recipeOptions('', true)"), /请显式选择新配方/);
  assert.doesNotMatch(run("recipeOptions('D4', true)"), / selected/);
  assert.match(run("recipeDescription('A4_C0')"), /r34l1sm/);
  assert.match(run("recipeDescription('A4_C1')"), /不指定或替换为新人物/);
});

test("catalog requires literal enabled true, idle GPUs never imply recipe slots", () => {
  const {run} = harness();
  for (const enabled of ["false", "null", '"true"', "1"]) {
    run(`state.recipeCatalog = {enabled: ${enabled}, recipes: [{recipe_id: 'B8', version: 'v1'}]}`);
    assert.equal(run("recipeEntry('B8')"), null);
    assert.match(run("recipeDescription('B8')"), /配方调度未就绪/);
  }
  run("state.recipeCatalog.enabled = true");
  assert.match(run("recipeDescription('B8')"), /容量未确认/);
  run("state.recipeCapacity = {B8: {available_slots: 0, eligible_lanes: [], reasons: ['not_qualified'], recipe_version: 'v1'}}");
  assert.match(run("recipeDescription('B8')"), /可启动 0 路；合格通道 无；not_qualified/);
  run("state.recipeCapacity.B8.recipe_version = 'other-version'");
  assert.match(run("recipeDescription('B8')"), /容量未确认/);
});

test("scope is limited to native portrait full-duration t2v preview", () => {
  const {run} = harness();
  run("const project = {mode:'t2v', duration:15, orientation:'portrait', audio_policy:'native'}");
  assert.equal(run("recipeScope(project)"), true);
  for (const fields of ["duration:5", "orientation:'landscape'", "mode:'i2v'", "audio_policy:'reference'"]) {
    assert.equal(run(`recipeScope({...project, ${fields}})`), false);
  }
  assert.equal(run("recipeScope(project, 'local_768')"), false);
});

test("rerun explicitly forwards recipe without replacing the prompt or legacy artifact", async () => {
  const {context, elements, run} = harness();
  context.sent = [];
  run(`state.project = {id:'legacy', mode:'t2v', duration:15, orientation:'portrait', audio_policy:'native',
    prompt_original:'原文', stages:{preview:{artifact_url:'/old.mp4'}}};
    state.recipeCatalog = {enabled:true, recipes:[{recipe_id:'A4_C1',version:'v1'}]};
    runAction = callback => callback();
    api = async (path, options) => sent.push({path, options});`);
  await run("startStage('preview', false)");
  assert.equal(context.sent.length, 0);
  assert.match(elements.get("commonStageError").textContent, /显式选择/);
  run("state.recipeSelections.legacy = 'A4_C1'");
  await run("startStage('preview', false)");
  assert.deepEqual(JSON.parse(context.sent[0].options.body), {new_seed: false, recipe_id: "A4_C1"});
  assert.equal(run("state.project.prompt_original"), "原文");
  assert.equal(run("state.project.stages.preview.artifact_url"), "/old.mp4");
  run("state.project.stages.preview.fleet_pending = true; state.recipeCatalog = null");
  await run("startStage('preview', false)");
  assert.deepEqual(JSON.parse(context.sent[1].options.body), {new_seed: false});
});

test("execution facts use actual server metadata rather than selected recipe", () => {
  const {context, elements, run} = harness();
  context.stage = {id:"preview", status:"awaiting_approval", runtime:{label:"待测", runner:"Fleet", billing:"本地"},
    execution:{recipe_id:"B8", recipe_version:"v1", backend_id:"vdn-r2", runtime_version:"core-250b2e9",
      gpu_uuid:"GPU-actual", execution_seconds:789.079}};
  run("renderStageMetrics(stage)");
  const html = elements.get("stageMetrics").innerHTML;
  for (const value of ["B8", "v1", "vdn-r2", "core-250b2e9", "GPU-actual", "789.079"]) assert.ok(html.includes(value));
});
