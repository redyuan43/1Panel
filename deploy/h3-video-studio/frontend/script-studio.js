const scriptBase = location.pathname.startsWith("/h3-studio/") ? "/h3-studio" : "";
const element = (identifier) => document.getElementById(identifier);
const activeStates = new Set(["created", "preflight", "matched", "dispatched", "running", "validating"]);
const statusNames = {created: "等待策划", preflight: "检查约束", matched: "Skills 已匹配", dispatched: "已组合规则", running: "文字策划中", validating: "校验脚本", completed: "脚本待确认", needs_context: "需要补充信息", failed: "策划未完成", cancelled: "已取消"};
let plan = null;
let options = null;
let polling = null;
let dirty = false;
let busy = false;

async function request(path, init = {}) {
  let response = await fetch(scriptBase + path, init);
  if (response.status === 401) {
    const key = sessionStorage.getItem("ai-router-admin-key") || window.prompt("请输入工作室访问密钥（1Panel入口使用管理密钥）");
    if (!key) throw new Error("需要访问密钥");
    const login = await fetch(scriptBase + "/session", {method: "POST", headers: {Authorization: `Bearer ${key}`}});
    if (!login.ok) throw new Error("认证失败");
    response = await fetch(scriptBase + path, init);
  }
  const payload = (response.headers.get("content-type") || "").includes("application/json") ? await response.json() : null;
  if (!response.ok || !payload) throw new Error(typeof payload?.detail === "string" ? payload.detail : `请求未完成（HTTP ${response.status}）`);
  return payload;
}

async function mutation(path, body) {
  const fingerprint = JSON.stringify({path, body});
  const saved = JSON.parse(sessionStorage.getItem("h3-script-pending") || "null");
  const operation = saved?.fingerprint === fingerprint ? saved.operation : crypto.randomUUID();
  sessionStorage.setItem("h3-script-pending", JSON.stringify({fingerprint, operation}));
  const result = await request(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({...body, operation_id: operation})});
  sessionStorage.removeItem("h3-script-pending");
  return result;
}

async function guarded(action) {
  if (busy) return;
  busy = true;
  element("error").textContent = "";
  controls();
  try { await action(); } catch (error) { element("error").textContent = error.message; }
  finally { busy = false; controls(); }
}

function controls() {
  const active = plan && activeStates.has(plan.status);
  const editable = plan?.draft && !active;
  element("createScript").disabled = busy || !options?.capability_manifest.configured;
  element("draftFields").disabled = busy || !editable;
  element("save").disabled = busy || !editable || !dirty;
  element("revise").disabled = busy || !plan || active || dirty || !options?.capability_manifest.configured;
  element("approve").disabled = busy || !plan || plan.status !== "completed" || dirty || plan.approved;
  element("cancel").hidden = !active;
  element("cancel").disabled = busy;
  element("handoff").hidden = !plan?.approved || dirty || active;
  element("dirty").textContent = dirty ? "有未保存修改；请先保存，再继续策划或确认。" : "";
}

function field(label, value, key, numeric = false) {
  const wrapper = document.createElement("label");
  wrapper.className = "field";
  const title = document.createElement("span");
  title.textContent = label;
  const input = document.createElement(numeric ? "input" : "textarea");
  input.dataset.key = key;
  input.value = value;
  if (numeric) { input.type = "number"; input.step = "0.01"; input.min = "0"; input.max = plan.brief.duration; }
  else { input.rows = 2; input.maxLength = 2000; }
  wrapper.append(title, input);
  return wrapper;
}

function appendShot(shot) {
  const card = document.createElement("section");
  card.className = "script-shot";
  const time = document.createElement("div");
  time.className = "script-columns";
  time.append(field("开始（秒）", shot.start, "start", true), field("结束（秒）", shot.end, "end", true));
  card.append(time);
  for (const [key, label] of Object.entries({visual: "画面与动作", camera: "镜头与光线", dialogue: "对白 / 旁白", sound: "环境声与动作声"})) card.append(field(label, shot[key], key));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "移除此镜";
  remove.onclick = () => { card.remove(); markDirty(); };
  card.append(remove);
  element("shots").append(card);
}

function markDirty() { dirty = true; controls(); }

function render(value) {
  plan = value;
  dirty = false;
  element("empty").hidden = true;
  element("workspace").classList.remove("hidden");
  element("planTitle").textContent = `脚本 · v${plan.revision}`;
  element("status").textContent = plan.approved ? "已确认" : statusNames[plan.status] || plan.status;
  element("planBrief").textContent = plan.brief.prompt;
  element("selectionReason").textContent = plan.selection_reason || "等待模型按意图选择 Skills；不是关键词模板。";
  element("selectedSkills").replaceChildren(...plan.skills.map((skill) => {
    const chip = document.createElement("span"); chip.textContent = skill.name; chip.title = skill.sources.map((source) => source.directory).join(" / "); return chip;
  }));
  element("planError").textContent = plan.error?.message || "";
  element("editor").hidden = !plan.draft;
  if (plan.draft) {
    for (const key of ["title", "summary", "music"]) element(key).value = plan.draft[key];
    for (const key of ["continuity", "questions", "assumptions"]) element(key).value = plan.draft[key].join("\n");
    element("shots").replaceChildren();
    plan.draft.shots.forEach(appendShot);
  }
  element("generationPrompt").textContent = plan.generation_prompt || "尚未产出";
  element("approvalHint").textContent = plan.approved ? "仅批准本版本脚本。点击带入后，仍需在生成页补充素材并手动创建任务。" : "修改后旧确认自动失效；待补充问题必须解决。若本轮未完成，下方可能仍显示上一版草稿，不能用于生成。";
  element("handoff").href = `./?script_plan=${encodeURIComponent(plan.id)}&revision=${plan.revision}`;
  for (const [identifier, format] of [["exportMarkdown", "markdown"], ["exportJson", "json"]]) {
    element(identifier).hidden = !plan.draft || plan.draft_revision !== plan.revision;
    element(identifier).href = `${scriptBase}/api/scripts/${plan.id}/export?format=${format}`;
  }
  element("evidence").textContent = JSON.stringify({id: plan.id, revision: plan.revision, model_calls: plan.model_calls, traces: plan.traces, events: plan.events, previous_versions: plan.history.map((entry) => ({revision: entry.revision, title: entry.draft.title, approved: entry.approved}))}, null, 2);
  const url = new URL(location.href); url.searchParams.set("script", plan.id); history.replaceState(null, "", url);
  controls();
  clearTimeout(polling);
  if (activeStates.has(plan.status)) {
    const identifier = plan.id;
    polling = setTimeout(async () => {
      try { const latest = await request(`/api/scripts/${identifier}`); if (plan.id === identifier) render(latest); }
      catch (error) { element("error").textContent = error.message + "；可刷新页面读取任务，不会自动重发策划。"; }
    }, 1200);
  }
}

function draftFromEditor() {
  const draft = {};
  for (const key of ["title", "summary", "music"]) draft[key] = element(key).value;
  for (const key of ["continuity", "questions", "assumptions"]) draft[key] = element(key).value.split("\n").map((value) => value.trim()).filter(Boolean);
  draft.shots = [...element("shots").children].map((card) => Object.fromEntries([...card.querySelectorAll("[data-key]")].map((input) => [input.dataset.key, input.type === "number" ? Number(input.value) : input.value])));
  return draft;
}

async function action(name, extra = {}) {
  render(await mutation(`/api/scripts/${plan.id}/${name}`, {revision: plan.revision, ...extra}));
  await loadHistory();
}

async function loadHistory() {
  const result = await request("/api/scripts");
  element("history").replaceChildren(...result.scripts.map((entry) => {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = `${entry.title} · v${entry.revision} · ${statusNames[entry.status] || entry.status}`;
    button.onclick = () => guarded(async () => { if (!dirty || window.confirm("放弃未保存的修改，打开此脚本？")) render(await request(`/api/scripts/${entry.id}`)); });
    return button;
  }));
}

function syncAudio() {
  const mode = element("mode").value;
  for (const option of element("audio").options) option.disabled = option.value === "reference" ? mode !== "reference" : option.value === "lock_source" ? mode !== "i2v" : mode === "reference";
  if (element("audio").selectedOptions[0].disabled) element("audio").value = mode === "reference" ? "reference" : "native";
}

document.addEventListener("DOMContentLoaded", () => {
  element("mode").onchange = syncAudio;
  syncAudio();
  element("editor").oninput = markDirty;
  element("addShot").onclick = () => { if (element("shots").children.length < 12) { appendShot({start: 0, end: plan.brief.duration, visual: "", camera: "", dialogue: "", sound: ""}); markDirty(); } };
  element("briefForm").onsubmit = (event) => { event.preventDefault(); guarded(async () => {
    if (dirty && !window.confirm("放弃未保存的修改，新建脚本？")) return;
    const brief = {prompt: element("brief").value, duration: Number(element("duration").value), mode: element("mode").value, audio_policy: element("audio").value, asset_notes: element("assetNotes").value, skill_ids: [...document.querySelectorAll("#skillOptions input:checked")].map((input) => input.value)};
    render(await mutation("/api/scripts", {brief})); await loadHistory();
  }); };
  element("editor").onsubmit = (event) => { event.preventDefault(); guarded(() => action("save", {draft: draftFromEditor()})); };
  element("revisionForm").onsubmit = (event) => { event.preventDefault(); guarded(async () => { await action("revise", {instruction: element("instruction").value}); element("instruction").value = ""; }); };
  element("approve").onclick = () => guarded(() => action("approve"));
  element("cancel").onclick = () => guarded(() => action("cancel"));
  element("refreshHistory").onclick = () => guarded(loadHistory);
  window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  guarded(async () => {
    options = await request("/api/scripts/options");
    const capability = options.capability_manifest;
    element("capability").textContent = `${capability.configured ? `文字策划：已配置 ${capability.model}（${capability.validated ? "本进程已验证" : "尚未实测"}）` : "文字策划：尚未接入只读模型，暂不可提交。不会用固定模板冒充 Skills 调用。"} · ${options.generation_mode === "preview" ? "视频生成：模拟素材，不调用成片 GPU/API。" : "视频生成：真实模式，后续启动可能消耗资源或费用。"}`;
    for (const skill of options.skills) {
      const label = document.createElement("label"); label.className = "script-skill";
      const input = document.createElement("input"); input.type = "checkbox"; input.value = skill.id; input.disabled = !skill.available;
      const title = document.createElement("span"); title.textContent = skill.name + (skill.available ? "" : "（仅归档）");
      const description = document.createElement("small"); description.textContent = skill.description + " 来源：" + skill.sources.map((source) => source.directory).join(" / ");
      label.append(input, title, description); element("skillOptions").append(label);
    }
    await loadHistory();
    const identifier = new URLSearchParams(location.search).get("script");
    if (identifier) render(await request(`/api/scripts/${encodeURIComponent(identifier)}`));
  });
});
