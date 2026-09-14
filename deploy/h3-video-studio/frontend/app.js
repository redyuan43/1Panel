const state = {
  project: null,
  projectViewVersion: 0,
  scriptSource: null,
  promptSkills: null,
  creating: false,
  scriptLoading: false,
  scriptLoadError: false,
  executionMode: null,
  recipeCatalog: null,
  recipeCapacity: {},
  recipeSelections: {},
  recipeSelectionScopes: {},
  nodeSelections: {},
  fleetNodes: [],
  recipeUIReady: false,
  projects: [],
  selectedStage: "context_ir",
  mode: "t2v",
  strategy: "fast",
  audioPolicy: "native",
  seedMode: "random",
  pollTimer: null,
  queuePollTimer: null,
  queueCandidates: [],
  schedules: [],
  selectedQueueIds: [],
  scheduleKind: "once",
  editingScheduleId: null,
};

const STUDIO_BASE = window.location.pathname.startsWith("/h3-studio/") ? "/h3-studio" : "";
let sessionRequest = null;
const studioUrl = (path) => STUDIO_BASE && !path.startsWith(STUDIO_BASE + "/") ? STUDIO_BASE + path : path;

const MODE_LABELS = {
  t2v: "文生视频",
  i2v: "首帧生视频",
  l2v: "尾帧生视频",
  fl2v: "首尾帧生视频",
  reference: "参考素材",
  hybrid: "Hybrid",
};

const STRATEGY_LABELS = {
  fast: "快速策略",
  safe: "稳妥策略",
  cloud: "云端加速",
};

const STATUS_LABELS = {
  pending: "等待",
  scheduled: "已定时",
  queued: "排队中",
  running: "运行中",
  awaiting_approval: "等待确认",
  approved: "已确认",
  failed: "失败",
  cancelled: "已取消",
};

const SCHEDULE_STATUS_LABELS = {
  scheduled: "等待定时",
  waiting_for_items: "等待加入项目",
  waiting_for_gpu: "等待GPU",
  running: "运行中",
  pausing: "即将暂停",
  paused: "已暂停",
  completed: "已完成",
  cancelled: "已取消",
};

const ITEM_STATUS_LABELS = {
  pending: "待运行",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
};

const $ = (id) => document.getElementById(id);

const RECIPE_OPTIONS = [
  {recipe_id: "A4", label: "A4 · 默认4步", description: "基础配方，不添加人物设定。"},
  {recipe_id: "A4_C0", label: "A4_C0 · Realism", description: "触发词 r34l1sm 由服务端加入执行图；不覆盖原提示词。"},
  {recipe_id: "A4_C1", label: "A4_C1 · 人像 Realism", description: "人物 LoRA 配方，不指定或替换为新人物；不覆盖原提示词。"},
  {recipe_id: "B8", label: "B8 · VDN 8步", description: "使用服务端冻结的 VDN 图，不是给 T8 图换标签。"},
];

function recipeScope(project, stage = "preview") {
  return stage === "preview" && project.mode === "t2v" && Number(project.duration) === 15
    && project.orientation === "portrait" && project.audio_policy === "native";
}

function recipeEntry(identifier) {
  const matches = (Array.isArray(state.recipeCatalog?.recipes) ? state.recipeCatalog.recipes : [])
    .filter(entry => entry && entry.recipe_id === identifier);
  return state.recipeCatalog?.enabled === true && matches.length === 1
    && typeof matches[0].version === "string" && matches[0].version ? matches[0] : null;
}

function recipeDescription(identifier) {
  const fallback = RECIPE_OPTIONS.find(entry => entry.recipe_id === identifier);
  if (!fallback) return "历史项目请显式选择新配方，原成片保留，不静默替换。";
  const entry = recipeEntry(identifier);
  if (!entry) return `${fallback.description} 配方调度未就绪；可离线创建，不能执行。`;
  const capacity = state.recipeCapacity[identifier];
  const confirmed = capacity?.recipe_version === entry.version
    && Number.isInteger(capacity.available_slots) && capacity.available_slots >= 0
    && Array.isArray(capacity.eligible_lanes) && Array.isArray(capacity.reasons);
  const status = confirmed
    ? `可启动 ${capacity.available_slots} 路；合格通道 ${capacity.eligible_lanes.join("、") || "无"}；${capacity.reasons.join("、") || "由调度器最终准入"}`
    : "配方容量未确认，不沿用显卡空闲数。";
  return `${entry.description || fallback.description} 版本 ${entry.version}。${status}`;
}

function recipeOptions(selected, legacy = false) {
  return (legacy ? '<option value="">请显式选择新配方</option>' : "") + RECIPE_OPTIONS.map(entry =>
    `<option value="${entry.recipe_id}"${entry.recipe_id === selected ? " selected" : ""}>${escapeHtml(recipeEntry(entry.recipe_id)?.label || entry.label)}</option>`).join("");
}

function renderCreateRecipes() {
  const select = $("recipeInput");
  if (!select) return;
  const selected = select.value || "A4";
  const firstFrame = $("previewRecipeInput");
  if (firstFrame) {
    $("previewRecipeField").classList.toggle("hidden", state.mode !== "i2v");
    firstFrame.disabled = state.mode !== "i2v";
    $("previewRecipeHint").textContent = firstFrame.value
      ? "已明确选择首帧加速配方；仅15秒竖版原生音轨，执行前核实节点资格。"
      : "完整模型14步；不会自动换成4步，执行前核实节点资格。";
  }
  select.innerHTML = recipeOptions(selected);
  const inScope = recipeScope({mode: state.mode, duration: $("durationInput").value,
    orientation: $("orientationInput").value, audio_policy: state.audioPolicy});
  select.disabled = !inScope;
  $("recipeHint").textContent = inScope ? recipeDescription(selected)
    : "仅适用15秒、480×864、24fps、原生音频文生视频预览；其他模式不变。";
}

async function refreshRecipes() {
  try { state.recipeCatalog = await api("/api/recipes"); }
  catch { state.recipeCatalog = null; }
  renderCreateRecipes();
  if (state.project) renderStage();
}

document.addEventListener("DOMContentLoaded", init);

function init() {
  if (window.PromptSkills && $("promptSkills")) {
    state.promptSkills = new window.PromptSkills({api, readBrief: promptSkillsBrief,
      apply: applyApprovedScript, restore: restoreOriginalPrompt, changed: updateCreateAvailability});
  }
  bindControls();
  state.recipeUIReady = true;
  renderCreateRecipes();
  refreshRecipes();
  window.setInterval(refreshRecipes, 15000);
  renderAssetFields();
  refreshHealth();
  refreshCapacity();
  refreshProjects();
  refreshQueueData(false);
  window.setInterval(refreshLiveTiming, 1000);
  refreshIcons();
  initializeScriptEntry();
}

async function initializeScriptEntry() {
  const query = new URLSearchParams(location.search);
  const identifier = query.get("script_plan") || query.get("script_draft");
  const projectIdentifier = query.get("project");
  state.scriptLoading = Boolean(identifier || projectIdentifier);
  updateCreateAvailability();
  try {
    const options = await api("/api/scripts/options");
    state.promptSkills?.configure(options);
    if (options) $("scriptStudioLink").classList.remove("hidden");
    if (!identifier) {
      if (projectIdentifier) {
        await openProject(projectIdentifier, query.get("stage") || "context_ir");
      }
      return;
    }
    const script = await api(`/api/scripts/${encodeURIComponent(identifier)}`);
    showSetup();
    restoreOriginalPrompt(script.brief.prompt, script.brief);
    const applied = Boolean(query.get("script_plan") && script.approved && script.status === "completed" && script.revision === Number(query.get("revision")));
    if (applied) applyApprovedScript(script);
    else if (query.get("script_plan")) $("formError").textContent = "原批准版本已失效，已恢复最新脚本；请在原始提示词下重新确认。";
    if (state.promptSkills) {
      $("promptSkillsNotes").value = script.brief.asset_notes;
      state.promptSkills.notesSnapshot = script.brief.asset_notes;
      state.promptSkills.receive(script, applied);
    }
  } catch (error) {
    state.promptSkills?.configure(null);
    if (identifier || projectIdentifier) {
      state.scriptLoadError = true;
      $("formError").textContent = error.message + "；可刷新页面重读，或点击新建项目。";
    }
  } finally {
    state.scriptLoading = false;
    updateCreateAvailability();
  }
}

function promptSkillsBrief() {
  const assets = [...$("assetFields").querySelectorAll('input[type="file"]')].flatMap((input) =>
    [...input.files].map((file) => `${input.name}: ${file.name}`));
  const notes = $("promptSkillsNotes")?.value.trim() || "";
  return {prompt: $("promptInput").value, duration: Number($("durationInput").value), mode: state.mode,
    audio_policy: state.audioPolicy, skill_ids: [],
    asset_notes: [notes, ...(assets.length ? ["已选文件名（未上传、未解析）：" + assets.join("；")] : [])].filter(Boolean).join("\n")};
}

function updateCreateAvailability() {
  $("createButton").disabled = state.creating || state.scriptLoading || state.scriptLoadError || Boolean(state.promptSkills?.blocked);
  $("createButtonLabel").textContent = $("promptProcessingInput").value === "manual"
    ? "创建并确认原始提示词" : "创建并运行 Context IR";
}

function restoreOriginalPrompt(prompt, brief = null) {
  if (brief) {
    state.mode = brief.mode;
    state.strategy = ["reference", "hybrid"].includes(state.mode) ? "safe" : "fast";
    $("modeInput").value = state.mode;
    $("strategyInput").value = state.strategy;
    setActiveButtons("modeControl", state.mode);
    renderAssetFields();
    if (brief.audio_policy === "lock_source") {
      $("audioLockToggle").checked = true;
      $("audioLockToggle").dispatchEvent(new Event("change"));
    }
    state.audioPolicy = brief.audio_policy;
    $("audioPolicyInput").value = state.audioPolicy;
    updateStrategyAvailability();
    setActiveButtons("strategyControl", state.strategy);
    $("durationInput").value = brief.duration;
    $("durationOutput").textContent = `${brief.duration} 秒`;
  }
  state.scriptSource = null;
  $("promptInput").value = prompt;
  $("promptCount").textContent = prompt.length;
  $("scriptSourceNotice").classList.add("hidden");
}

function applyApprovedScript(script) {
  state.scriptSource = {id: script.id, revision: script.revision};
  $("projectName").value = script.draft.title.slice(0, 80);
  $("promptInput").value = script.generation_prompt;
  $("promptCount").textContent = script.generation_prompt.length;
  $("scriptSourceNotice").textContent = `已填入确认脚本 v${script.revision}，尚未创建生成任务。原始需求与 Skills 选择依据保留在提示词下方；可继续用自然语言修改。`;
  $("scriptSourceNotice").classList.remove("hidden");
}

function bindControls() {
  $("projectForm").addEventListener("change", renderCreateRecipes);
  $("projectForm").addEventListener("input", renderCreateRecipes);
  $("projectForm").addEventListener("click", renderCreateRecipes);
  $("modeControl").addEventListener("click", (event) => {
    const button = event.target.closest("[data-value]");
    if (!button) return;
    state.mode = button.dataset.value;
    $("modeInput").value = state.mode;
    setActiveButtons("modeControl", state.mode);
    if (state.mode === "reference") {
      state.strategy = "safe";
    }
    if (state.mode === "hybrid") {
      state.strategy = "safe";
    }
    updateStrategyAvailability();
    setActiveButtons("strategyControl", state.strategy);
    $("strategyInput").value = state.strategy;
    renderAssetFields();
  });

  $("strategyControl").addEventListener("click", (event) => {
    const button = event.target.closest("[data-value]");
    if (!button || button.disabled) return;
    state.strategy = button.dataset.value;
    $("strategyInput").value = state.strategy;
    setActiveButtons("strategyControl", state.strategy);
  });

  $("durationInput").addEventListener("input", () => {
    $("durationOutput").value = `${$("durationInput").value} 秒`;
  });
  $("promptProcessingInput").addEventListener("change", updateCreateAvailability);
  $("promptInput").addEventListener("input", () => {
    $("promptCount").textContent = String($("promptInput").value.length);
  });
  $("randomSeedButton").addEventListener("click", () => {
    const value = Math.floor(Math.random() * 2147483647);
    $("manualSeedInput").value = String(value);
    $("seedInput").value = String(value);
  });
  $("seedModeControl").addEventListener("click", (event) => {
    const button = event.target.closest("[data-seed-mode]");
    if (!button) return;
    state.seedMode = button.dataset.seedMode;
    $("seedModeControl")
      .querySelectorAll("[data-seed-mode]")
      .forEach((item) => item.classList.toggle("active", item === button));
    $("manualSeedLine").classList.toggle("hidden", state.seedMode !== "manual");
    $("seedInput").value =
      state.seedMode === "random" ? "-1" : $("manualSeedInput").value;
  });
  $("manualSeedInput").addEventListener("input", () => {
    if (state.seedMode === "manual") {
      $("seedInput").value = $("manualSeedInput").value || "0";
    }
  });
  $("projectForm").addEventListener("submit", createProject);
  $("newProjectButton").addEventListener("click", () => {
    if (state.creating || state.scriptLoading || state.promptSkills?.working) {
      $("formError").textContent = "请先等待或取消当前策划/创建操作，再新建项目。";
      return;
    }
    if (state.promptSkills && !state.promptSkills.discard()) return;
    state.scriptLoadError = false;
    $("formError").textContent = "";
    showSetup();
    updateCreateAvailability();
  });
  for (const eventName of ["input", "change", "click"]) {
    $("projectForm").addEventListener(eventName, (event) => {
      if (!event.target.closest("#promptSkills")) state.promptSkills?.controls();
    });
  }
  $("queueButton").addEventListener("click", openQueue);
  $("historyButton").addEventListener("click", openHistory);
  $("closeHistoryButton").addEventListener("click", closeHistory);
  $("drawerBackdrop").addEventListener("click", closeHistory);
  $("newScheduleButton").addEventListener("click", resetScheduleBuilder);
  $("cancelScheduleEdit").addEventListener("click", resetScheduleBuilder);
  $("selectAllCandidates").addEventListener("click", selectAllCandidates);
  $("scheduleKindControl").addEventListener("click", selectScheduleKind);
  $("onceTimeInput").addEventListener("change", renderSelectedQueue);
  $("dailyTimeInput").addEventListener("change", renderSelectedQueue);
  $("scheduleForm").addEventListener("submit", saveSchedule);
  setDefaultScheduleTime();
}

function renderAssetFields() {
  const fields = [];
  if (["i2v", "fl2v", "hybrid"].includes(state.mode)) {
    fields.push(fileField("first_frame", "首帧图片", "image/*"));
  }
  if (["l2v", "fl2v", "hybrid"].includes(state.mode)) {
    fields.push(fileField("last_frame", "尾帧图片", "image/*"));
  }
  if (["reference", "hybrid"].includes(state.mode)) {
    fields.push(fileField("reference_image", "参考图片", "image/*", state.mode === "reference"));
  }
  if (state.mode === "reference") {
    fields.push(fileField("reference_video", "参考视频", "video/*", true));
    fields.push(fileField("reference_audio", "参考音频", "audio/*", true));
    fields.push(`
      <div class="asset-options">
        <label><input id="embeddedAudio" name="use_embedded_video_audio" type="checkbox" /> 使用参考视频内置音频</label>
      </div>
    `);
    state.audioPolicy = "reference";
  } else if (state.mode === "i2v") {
    fields.push(`
      <div class="asset-options">
        <label><input id="audioLockToggle" type="checkbox" /> 锁定源音频</label>
      </div>
    `);
    state.audioPolicy = "native";
  } else {
    state.audioPolicy = "native";
  }
  $("assetFields").innerHTML = fields.join("");
  $("audioPolicyInput").value = state.audioPolicy;

  const audioLock = $("audioLockToggle");
  if (audioLock) {
    audioLock.addEventListener("change", () => {
      state.audioPolicy = audioLock.checked ? "lock_source" : "native";
      $("audioPolicyInput").value = state.audioPolicy;
      const existing = $("audioLockFile");
      if (audioLock.checked && !existing) {
        $("assetFields").insertAdjacentHTML(
          "beforeend",
          fileField("reference_audio", "源音频", "audio/*", false, "audioLockFile"),
        );
      } else if (!audioLock.checked && existing) {
        existing.remove();
      }
      if (audioLock.checked && state.strategy === "cloud") {
        state.strategy = "safe";
        $("strategyInput").value = state.strategy;
      }
      updateStrategyAvailability();
      setActiveButtons("strategyControl", state.strategy);
    });
  }
  updateStrategyAvailability();
  refreshIcons();
}

function fileField(name, label, accept, optional = false, id = "") {
  return `
    <label class="asset-field"${id ? ` id="${id}"` : ""}>
      <span>${escapeHtml(label)}${optional ? "（可选）" : ""}</span>
      <input name="${name}" type="file" accept="${accept}" ${optional ? "" : "required"} />
    </label>
  `;
}

function updateStrategyAvailability() {
  const cloud = document.querySelector('#strategyControl [data-value="cloud"]');
  cloud.disabled = state.mode === "hybrid" || state.audioPolicy === "lock_source";
}

async function createProject(event) {
  event.preventDefault();
  $("formError").textContent = "";
  if (state.creating || state.scriptLoading || state.scriptLoadError || state.promptSkills?.blocked) {
    $("formError").textContent = "请先确认并填入当前脚本，或明确恢复原始需求、不使用此脚本。";
    return;
  }
  state.creating = true;
  updateCreateAvailability();
  const formData = new FormData($("projectForm"));
  formData.set("mode", state.mode);
  formData.set("strategy", state.strategy);
  formData.set("audio_policy", state.audioPolicy);
  if (!recipeScope({mode: state.mode, duration: formData.get("duration"),
    orientation: formData.get("orientation"), audio_policy: state.audioPolicy})) formData.delete("recipe_id");
  if (state.scriptSource) {
    formData.set("script_plan_id", state.scriptSource.id);
    formData.set("script_plan_revision", state.scriptSource.revision);
  }
  if (!$("embeddedAudio")?.checked) {
    formData.set("use_embedded_video_audio", "false");
  }
  try {
    state.project = await api("/api/projects", {
      method: "POST",
      body: formData,
    });
    state.selectedStage = "context_ir";
    showStudio();
    await api(`/api/projects/${state.project.id}/context-ir`, { method: "POST" });
    await refreshCurrentProject();
  } catch (error) {
    $("formError").textContent = error.message;
    if (error.status === 409 && state.scriptSource && state.promptSkills) {
      try { await state.promptSkills.refresh(); } catch { }
    }
  } finally {
    state.creating = false;
    updateCreateAvailability();
  }
}

function healthPresentation(result) {
  const localFleet = result.execution_mode === "ivan-fleet";
  const ready = result.ok === true && (localFleet || result.minimaxConfigured === true);
  return {ready, label: ready ? (localFleet ? "本地执行服务就绪" : "执行服务就绪") : "执行服务需检查",
    cloudNote: result.minimaxConfigured === true ? "云端已配置，调用可能产生费用" : "云端未配置，不影响本地原文生成"};
}

async function refreshHealth() {
  try {
    const result = await api("/api/health");
    state.executionMode = result.execution_mode;
    const health = healthPresentation(result);
    $("executionMode").textContent = result.execution_mode === "preview"
      ? "交互体验模式 · 视频产物均为合成测试素材，不调用成片 GPU/API；文字策划状态见原始提示词下方"
      : "真实生成模式 · 本地任务由各节点 Fleet 调度；" + health.cloudNote;
    const badge = $("healthBadge");
    badge.classList.toggle("ok", health.ready);
    badge.classList.toggle("error", !health.ready);
    badge.querySelector("span:last-child").textContent =
      health.label;
  } catch {
    $("healthBadge").classList.add("error");
    $("healthBadge").querySelector("span:last-child").textContent = "执行服务离线";
  }
}

async function refreshProjects() {
  try {
    const payload = await api("/api/projects");
    state.projects = payload.projects;
    renderHistory();
  } catch {
    state.projects = [];
  }
}

async function refreshCapacity() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const capacity = await api("/api/capacity", {signal: controller.signal});
    if (!capacity.available) throw new Error(capacity.reason || "实时容量不可用");
    state.recipeCapacity = capacity.recipe_capacity || {};
    state.fleetNodes = capacity.nodes || [];
    const currentLanes = capacity.lanes.filter((lane) => lane.enabled !== false);
    const idle = currentLanes.filter((lane) => lane.status === "idle").length;
    $("capacitySummary").textContent = `设备状态 · 空闲 ${idle}/${currentLanes.length} 张 · ${capacity.counts_complete === false ? "已知" : ""}执行 ${capacity.active} 个 · 排队 ${capacity.queued} 个${capacity.counts_complete === false ? " · 部分节点状态待核实" : ""}`;
    $("capacityUpdated").textContent = ` · 采样于 ${new Date(capacity.sampled_at * 1000).toLocaleTimeString()}，每15秒刷新`;
    const labels = {idle: "空闲", busy: "占用", unknown: "离线/未知"};
    $("capacityLanes").innerHTML = currentLanes.map((lane) => `
      <div class="capacity-lane" data-status="${escapeHtml(lane.status)}">
        <strong>${escapeHtml(lane.name)} · ${labels[lane.status] || "未知"}</strong>
        <small>${escapeHtml(lane.id)}${lane.preview_only ? " · 仅预览" : ""}${lane.external_tasks?.length ? ` · 验证任务 ${escapeHtml(lane.external_tasks.join("、"))}` : ""} · ${lane.node_id === "edge" ? "GB10 共享内存；以节点资源检查为准" : `可用显存 ${formatBytes(lane.vram_free)} / ${formatBytes(lane.vram_total)}`}</small>
      </div>`).join("");
    const limits = capacity.limits;
    if (capacity.nodes) {
      $("capacityPolicy").textContent = capacity.nodes.map(node =>
        `${node.id}：${node.enabled ? "最多" + node.max_parallel + "路" : "尚未开放"}，${node.available ? "运行 " + node.active + " 个，可用内存 " + formatBytes(node.memory_available_bytes) : "离线或状态未知"}${node.model_lifecycle ? "；" + ({idle: "等待视频准备", draining: "等待原服务空闲", stopping_qwen: "暂停 Qwen / 准备 H3", waiting_memory: "等待内存释放", ready: "视频模型流程已准备", restoring_h3: "正在卸载视频模型", restoring_qwen: "恢复 Qwen", unfencing: "正在恢复原服务", blocked: "恢复需处理"}[node.model_lifecycle.state] || "模型状态未知") : ""}`
      ).join("；") + "。第二路须通过配方组合验证及实时资源检查；排队任务不会自动降低配方要求。";
      return;
    }
    const recovery = capacity.swap_recovery;
    const warning = !capacity.resources_ok ? "资源监测异常，不能确认可提交容量。"
      : capacity.exclusive_window ? "当前有独占验证窗口。"
      : recovery && recovery.ready === false && (!capacity.studio_preview || capacity.studio_preview.available === 0) ? `资源保护检查中（${recovery.reason || "等待空闲观察"}），空闲卡不代表立即可运行。`
      : "空闲卡数不是剩余可接任务数；显存可能驻留模型，最终由调度器检查资源后准入。";
    const preview = capacity.studio_preview;
    const reasons = {controlled_validation: "独占验证中", studio_batch_active: "批处理占用中", fleet_draining: "调度器维护中",
      earlier_job_queued: "已有任务排队", untracked_upstream_work: "GPU存在外部任务", exclusive_workload_active: "不兼容任务正在运行",
      capacity_full: "并发名额已满", ram_headroom: "主机内存余量不足", cgroup_headroom: "服务内存预算不足",
      swap_recovery_single_only: "Swap保护，仅允许单路", swap_recovery_fast_only: "Swap保护，仅允许4060 Ti", swap_pressure: "Swap压力保护", swap_growth_limit: "Swap增长超限",
      swap_hard_limit: "Swap已达硬限制", resource_telemetry_unavailable: "资源状态未知", no_eligible_lane: "没有空闲的适配GPU",
      root_disk_headroom: "系统盘余量不足", offload_disk_headroom: "模型盘余量不足", swap_recovery_known_shape_required: "任务尚未验证"};
    const previewStatus = preview ? `15秒文生视频低清：当前还可启动 ${preview.available} 路（策略上限 ${preview.max_parallel} 路）${preview.reason ? `，${reasons[preview.reason] || preview.reason}` : ""}。` : "15秒低清即时容量尚未提供。";
    $("capacityPolicy").textContent = `${previewStatus}其他长任务 ${limits.long} 路，高清与低清混跑尚未开放。主机可用内存 ${formatBytes(capacity.memory_available_bytes)}，Swap ${formatBytes(capacity.swap_used_bytes)}。${warning}`;
  } catch (error) {
    $("capacitySummary").textContent = "设备实时容量暂不可用";
    state.recipeCapacity = {};
    $("capacityUpdated").textContent = "";
    $("capacityLanes").innerHTML = "";
    $("capacityPolicy").textContent = "未读取到最新状态，不显示旧的空闲数；稍后自动重试。";
  } finally {
    clearTimeout(timeout);
    if (state.recipeUIReady) {
      renderCreateRecipes();
      if (state.project) renderStage();
    }
    setTimeout(refreshCapacity, 15000);
  }
}

async function refreshCurrentProject() {
  if (!state.project) return;
  const current = state.project;
  const version = state.projectViewVersion;
  try {
    const project = await api(`/api/projects/${current.id}`);
    if (version !== state.projectViewVersion || state.project !== current) return;
    state.project = project;
    renderStudio();
    schedulePolling();
  } catch (error) {
    console.error(error);
  }
}

function schedulePolling() {
  clearTimeout(state.pollTimer);
  if (!state.project) return;
  const active = Object.values(state.project.stages).some((stage) =>
    ["queued", "running"].includes(stage.status),
  );
  if (active) {
    state.pollTimer = setTimeout(refreshCurrentProject, 3000);
  }
}

function showSetup() {
  state.projectViewVersion += 1;
  if (state.scriptSource) {
    state.scriptSource = null;
    $("promptInput").value = "";
    $("promptCount").textContent = "0";
    $("scriptSourceNotice").classList.add("hidden");
    state.promptSkills?.reset();
  }
  clearTimeout(state.pollTimer);
  clearTimeout(state.queuePollTimer);
  state.project = null;
  $("studioView").classList.add("hidden");
  $("queueView").classList.add("hidden");
  $("setupView").classList.remove("hidden");
  closeHistory();
}

function showStudio() {
  $("setupView").classList.add("hidden");
  $("queueView").classList.add("hidden");
  $("studioView").classList.remove("hidden");
  renderStudio();
  schedulePolling();
}

async function openProject(identifier, selectedStage = null) {
  const version = ++state.projectViewVersion;
  clearTimeout(state.pollTimer);
  const project = await api(`/api/projects/${encodeURIComponent(identifier)}`);
  if (version !== state.projectViewVersion) return;
  state.project = project;
  state.selectedStage = selectedStage || project.pipeline.find((stage) =>
    ["running", "queued", "awaiting_approval"].includes(stage.status),
  )?.id || project.pipeline.at(-1).id;
  closeHistory();
  showStudio();
}

function renderStudio() {
  if (!state.project) return;
  const project = state.project;
  $("projectTitle").textContent = project.name;
  $("projectMode").textContent = `${MODE_LABELS[project.mode]} · ${project.orientation === "portrait" ? "竖版" : "横版"}`;
  $("projectStrategy").textContent = STRATEGY_LABELS[project.strategy];
  $("projectDuration").textContent = `${project.actual_duration.toFixed(2)} 秒`;
  $("projectEstimate").textContent = projectEstimateLabel(project.runtime_summary);
  $("projectSeed").textContent = `Seed ${project.seed}`;
  renderPipeline();
  renderStage();
  refreshIcons();
}

function renderPipeline() {
  const pipeline = state.project.pipeline;
  if (!pipeline.some((stage) => stage.id === state.selectedStage)) {
    state.selectedStage = pipeline[0].id;
  }
  $("pipeline").innerHTML = pipeline
    .map((stage, index) => {
      const selected = stage.id === state.selectedStage ? "selected" : "";
      const arrow = index < pipeline.length - 1 ? '<span class="pipeline-arrow"></span>' : "";
      return `
        <div class="pipeline-node-wrap">
          <button class="pipeline-node ${stage.status} ${selected}" data-stage="${stage.id}" type="button">
            <span class="node-index">${String(index + 1).padStart(2, "0")}</span>
            <strong>${escapeHtml(stage.label)}</strong>
            <span class="node-status">${STATUS_LABELS[stage.status] || stage.status} · ${state.executionMode === "preview" ? "模拟阶段" : "预计 " + escapeHtml(stage.runtime.label)}</span>
          </button>
          ${arrow}
        </div>
      `;
    })
    .join("");
  $("pipeline").querySelectorAll("[data-stage]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedStage = button.dataset.stage;
      renderPipeline();
      renderStage();
      refreshIcons();
    });
  });
}

function renderStage() {
  const project = state.project;
  const stage = project.pipeline.find((item) => item.id === state.selectedStage);
  if (!stage) return;
  const index = project.pipeline.findIndex((item) => item.id === stage.id);
  $("stageIndex").textContent = String(index + 1).padStart(2, "0");
  $("stageTitle").textContent = stage.label;
  $("stageStatus").textContent = STATUS_LABELS[stage.status] || stage.status;
  $("stageStatus").className = `status-pill ${stage.status}`;
  $("contextPanel").classList.toggle("hidden", stage.id !== "context_ir");
  $("mediaPanel").classList.toggle("hidden", stage.id === "context_ir");
  $("commonStageError").textContent = stage.error || "";
  $("commonStageError").classList.toggle("hidden", !stage.error);
  renderStageMetrics(stage);

  if (stage.id === "context_ir") {
    $("originalPrompt").value = project.prompt_original;
    $("optimizedPrompt").value = project.prompt_ir || "";
    $("optimizedPrompt").readOnly = stage.status !== "awaiting_approval";
  } else {
    renderMediaStage(stage);
  }
  renderStageActions(stage);
}

function renderMediaStage(stage) {
  const video = $("stageVideo");
  const placeholder = $("videoPlaceholder");
  if (stage.artifact_url) {
    const nextUrl = `${studioUrl(stage.artifact_url)}?v=${stage.finished_at || Date.now()}`;
    if (video.dataset.src !== nextUrl) {
      video.src = nextUrl;
      video.dataset.src = nextUrl;
    }
    video.classList.remove("hidden");
    placeholder.classList.add("hidden");
  } else {
    video.removeAttribute("src");
    video.dataset.src = "";
    video.classList.add("hidden");
    placeholder.classList.remove("hidden");
  }
  const project = state.project;
  let resolution =
    stage.simulated
      ? "合成素材 1344 × 768（非真实成片）"
      : stage.id === "regenerate_2k"
      ? "2560 × 1440"
      : ["local_768", "cloud_768"].includes(stage.id)
        ? "1344 × 768"
        : "864 × 480";
  if (!stage.simulated && project.orientation === "portrait") {
    resolution = resolution.split(" × ").reverse().join(" × ");
  }
  const steps =
    stage.simulated
      ? "未调用模型"
      : stage.execution?.recipe_id === "B8"
      ? "VDN 8"
      : stage.id === "preview" && project.audio_policy === "native" && ["t2v", "i2v", "l2v", "fl2v"].includes(project.mode)
      ? "Turbo 4"
      : ["cloud_768", "regenerate_2k"].includes(stage.id)
        ? "官方云端"
        : "完整模型 14";
  const taskId = stage.remote_task_id || stage.prompt_id || "—";
  const queueWait = stage.started_at && stage.queued_at
    ? formatSeconds(Math.max(0, stage.started_at - stage.queued_at))
    : stage.status === "queued"
      ? "排队中"
      : "—";
  $("stageFacts").innerHTML = `
    <dt>输出规格</dt><dd>${resolution}</dd>
    <dt>采样</dt><dd>${steps}</dd>
    <dt>帧率</dt><dd>24 fps</dd>
    <dt>有效时长</dt><dd>${stage.simulated ? "合成素材 5.17" : project.actual_duration.toFixed(2)} 秒</dd>
    <dt>Seed</dt><dd>${project.seed}</dd>
    <dt>排队等待</dt><dd>${queueWait}</dd>
    <dt>输出大小</dt><dd>${formatBytes(stage.artifact_bytes)}</dd>
    <dt>任务编号</dt><dd title="${escapeHtml(taskId)}">${escapeHtml(shortId(taskId))}</dd>
  `;
  $("stageDetail").textContent = stage.detail || "等待开始";
  const view = stageProgressView(stage);
  $("stageProgress").textContent = view.label;
  $("progressFill").style.width = view.width;
  $("progressFill").classList.toggle("is-indeterminate", view.indeterminate);
}

function stageProgressView(stage) {
  if (["approved", "awaiting_approval", "completed"].includes(stage.status)) {
    return {label: "100%", width: "100%", indeterminate: false};
  }
  const execution = stage.execution || {};
  if (["preview", "proof", "local_768"].includes(stage.id) &&
      ["queued", "running", "cancelling"].includes(stage.status) &&
      (stage.execution || stage.fleet_pending || stage.node_id)) {
    const labels = {resource_waiting: "等待资源", backend_starting: "启动中",
      input_preparing: "准备输入", model_preparing: "加载模型", sampling: "采样中",
      decoding: "解码中", saving: "保存中"};
    let label = labels[execution.phase] || (stage.status === "queued" ? "排队中" : "执行中");
    const steps = execution.sampler_progress;
    if (steps?.basis === "confirmed_owned_sampler_events" && Number.isInteger(steps.completed) &&
        Number.isInteger(steps.total) && steps.completed > 0 && steps.completed <= steps.total) {
      label += ` · 采样 ${steps.completed}/${steps.total} 步`;
    }
    return {label, width: "100%", indeterminate: true};
  }
  const value = Number.isFinite(stage.progress) ? stage.progress : 0;
  return {label: `${value}%`, width: `${value}%`, indeterminate: false};
}

function renderStageMetrics(stage) {
  const elapsed = elapsedSeconds(stage);
  let elapsedLabel = "尚未开始";
  if (stage.status === "queued") {
    elapsedLabel = stage.queued_at
      ? `已排队 ${formatSeconds(Date.now() / 1000 - stage.queued_at)}`
      : "排队中";
  } else if (stage.started_at || stage.queued_at) {
    elapsedLabel = stage.finished_at
      ? `本次耗时 ${formatSeconds(elapsed)}`
      : `已用时 ${formatSeconds(elapsed)}`;
  }
  const taskId = stage.remote_task_id || stage.prompt_id;
  const metrics = [
    ["预计耗时", state.executionMode === "preview" ? "模拟阶段，数秒" : stage.runtime.label, stage.runtime.basis],
    ["本次总耗时（含排队）", elapsedLabel, "从提交到结束，包含排队、计算及传输；不是纯GPU计算时间"],
    ["运行位置", state.executionMode === "preview" ? "AI 合成素材模拟" : stage.node_id || stage.execution?.node_id || stage.runtime.runner, ""],
    ["资源费用", state.executionMode === "preview" ? "无 GPU / 付费 API 调用" : stage.runtime.billing, ""],
  ];
  if (taskId) {
    metrics.push(["任务编号", shortId(taskId), taskId]);
  }
  const execution = stage.execution || {};
  for (const [key, label] of [["recipe_id", "执行配方"], ["recipe_version", "配方版本"],
    ["backend_id", "执行后端"], ["runtime_version", "运行时版本"], ["gpu_uuid", "实际显卡 UUID"],
    ["execution_seconds", "实际执行秒数（不含排队）"]]) {
    if (execution[key] != null) metrics.push([label, String(execution[key]), "服务端执行记录"]);
  }
  $("stageMetrics").innerHTML = metrics
    .map(
      ([label, value, title]) => `
        <div class="stage-metric" ${title ? `title="${escapeHtml(title)}"` : ""}>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `,
    )
    .join("");
}

function refreshLiveTiming() {
  if (!state.project || $("studioView").classList.contains("hidden")) return;
  const stage = state.project.pipeline.find((item) => item.id === state.selectedStage);
  if (stage) renderStageMetrics(stage);
}

function renderStageActions(stage) {
  syncRecipeSelectionScope(state.project);
  const actions = [];
  const unlocked = isStageUnlocked(stage.id);
  if (stage.id === "context_ir") {
    if (["pending", "failed", "cancelled"].includes(stage.status)) {
      actions.push(actionButton("重新运行 Context IR", "sparkles", "primary-action", () => startContextIR()));
    } else if (["queued", "running"].includes(stage.status)) {
      actions.push(actionButton("取消", "square", "danger-action", () => cancelStage(stage.id)));
    } else if (stage.status === "awaiting_approval") {
      actions.push(actionButton("重新优化", "refresh-cw", "", () => startContextIR()));
      actions.push(
        actionButton("确认提示词", "check", "primary-action", () => approveContextIR()),
      );
    }
  } else if (["queued", "running"].includes(stage.status)) {
    actions.push(actionButton("取消任务", "square", "danger-action", () => cancelStage(stage.id)));
  } else if (stage.status === "awaiting_approval") {
    actions.push(downloadLink(stage));
    actions.push(actionButton("同 Seed 重试", "rotate-ccw", "", () => startStage(stage.id, false)));
    actions.push(actionButton("换 Seed 重试", "dice-5", "", () => startStage(stage.id, true)));
    actions.push(actionButton("确认并继续", "check", "primary-action", () => approveStage(stage.id)));
  } else if (["failed", "cancelled"].includes(stage.status)) {
    actions.push(actionButton(stage.fleet_pending ? "继续对账（不重新生成）" : "重试", "rotate-ccw", "primary-action", () => startStage(stage.id, false)));
  } else if (stage.status === "pending" && unlocked) {
    const label = stage.id === "regenerate_2k" ? "确认调用官方 2K" : `开始${stage.label}`;
    actions.push(actionButton(label, "play", "primary-action", () => startStage(stage.id, false)));
  } else if (stage.status === "scheduled") {
    actions.push(actionButton("查看768P队列", "calendar-clock", "primary-action", openQueue));
  } else if (stage.status === "approved" && stage.artifact_url) {
    actions.push(downloadLink(stage));
  }
  $("stageActions").innerHTML = "";
  if (["preview", "proof", "local_768"].includes(stage.id) && state.fleetNodes.length) {
    const field = document.createElement("label");
    field.className = "field";
    const span = document.createElement("span");
    span.textContent = "执行设备";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "执行设备");
    for (const node of [{id: "auto", enabled: true}, ...state.fleetNodes]) {
      const option = document.createElement("option");
      option.value = node.id;
      option.textContent = node.id === "auto" ? "自动选择可用设备" : `${node.id}${node.enabled ? "" : "（尚未开放）"}`;
      option.disabled = node.enabled !== true;
      select.appendChild(option);
    }
    select.value = state.nodeSelections[state.project.id] ?? stage.target_node ?? "auto";
    select.disabled = ["running", "queued", "scheduled"].includes(stage.status) || Boolean(stage.fleet_pending);
    select.addEventListener("change", () => { state.nodeSelections[state.project.id] = select.value; });
    field.append(span, select);
    if (stage.node_id) {
      const actual = document.createElement("small");
      actual.textContent = `本次执行归属：${stage.node_id}`;
      field.appendChild(actual);
    }
    $("stageActions").appendChild(field);
  }
  if (recipeScope(state.project, stage.id)) {
    const selected = state.recipeSelections[state.project.id] ?? state.project.recipe_id ?? "";
    const field = document.createElement("label");
    field.className = "field";
    field.innerHTML = `<span>本次生成配方</span><select aria-label="本次生成配方">${recipeOptions(selected, true)}</select><small></small>`;
    const select = field.querySelector("select");
    select.value = RECIPE_OPTIONS.some(entry => entry.recipe_id === selected) ? selected : "";
    select.disabled = ["running", "queued", "scheduled"].includes(stage.status) || Boolean(stage.fleet_pending);
    field.querySelector("small").textContent = recipeDescription(select.value);
    select.addEventListener("change", () => {
      state.recipeSelections[state.project.id] = select.value;
      field.querySelector("small").textContent = recipeDescription(select.value);
    });
    $("stageActions").appendChild(field);
    for (const previous of state.project.stage_history || []) {
      if (previous.stage_id !== stage.id || !previous.artifact_url) continue;
      const link = downloadLink(previous);
      link.textContent = `历史成片 · ${previous.execution?.recipe_id || previous.recipe_id || "原配方"} · Seed ${previous.seed}`;
      $("stageActions").appendChild(link);
    }
  }
  const unavailable = stage.id === "preview" && !stage.fleet_pending ? previewUnavailable(state.project) : "";
  if (unavailable && !["running", "queued", "scheduled"].includes(stage.status)) {
    const reason = document.createElement("small");
    reason.className = "input-material-warning";
    reason.textContent = unavailable;
    $("stageActions").appendChild(reason);
    if (["pending", "failed", "cancelled"].includes(stage.status)) actions.forEach(button => { button.disabled = true; });
  }
  actions.forEach((element) => $("stageActions").appendChild(element));
  refreshIcons();
}

function isStageUnlocked(stageId) {
  if (stageId === "context_ir") return true;
  const pipeline = state.project.pipeline;
  const index = pipeline.findIndex((stage) => stage.id === stageId);
  if (index <= 0) return false;
  return pipeline[index - 1].status === "approved";
}

function actionButton(label, icon, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.innerHTML = `<i data-lucide="${icon}"></i>${escapeHtml(label)}`;
  button.addEventListener("click", handler);
  return button;
}

function downloadLink(stage) {
  const link = document.createElement("a");
  link.href = studioUrl(stage.artifact_url);
  link.download = "";
  link.innerHTML = '<i data-lucide="download"></i>下载视频';
  return link;
}

async function startContextIR() {
  await runAction(() =>
    api(`/api/projects/${state.project.id}/context-ir`, { method: "POST" }),
  );
}

async function approveContextIR() {
  const prompt = $("optimizedPrompt").value.trim();
  await runAction(() =>
    api(`/api/projects/${state.project.id}/context-ir/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    }),
  );
}

function syncRecipeSelectionScope(project) {
  const scope = JSON.stringify([project.mode, project.connector_revision, project.recipe_id]);
  if (project.mode !== "t2v" || state.recipeSelectionScopes[project.id] !== scope) delete state.recipeSelections[project.id];
  state.recipeSelectionScopes[project.id] = scope;
}

function previewUnavailable(project) {
  if (!project.connector_owner || project.mode === "t2v") return "";
  if (project.orientation !== "portrait" || Number(project.duration) !== 15) return "当前节点执行仅验证15秒竖版；其他规格尚未开放，没有修改输入。";
  const profile = project.execution_profile;
  if (!profile?.profile_id || !profile.version) return "专用执行配置未确认，请保存输入新版本。";
  const registered = state.recipeCatalog?.multimodal_profiles || [];
  const match = registered.find(item => item.profile_id === profile.profile_id && item.version === profile.version);
  if (!match || !(match.qualified === true || (match.acceptance_tasks || []).includes(project.id))) {
    return (profile.label || profile.profile_id) + " 尚无匹配的节点运行资格，暂不能开始；可保留草稿等待验证。";
  }
  return "";
}

function selectedTargetNode(stageId) {
  return state.project.stages[stageId].fleet_pending
    ? state.project.stages[stageId].target_node ?? "auto"
    : state.nodeSelections[state.project.id] ?? state.project.stages[stageId].target_node ?? "auto";
}

async function startStage(stageId, newSeed) {
  const payload = {new_seed: newSeed};
  if (["preview", "proof", "local_768"].includes(stageId) && state.fleetNodes.length) {
    payload.target_node = selectedTargetNode(stageId);
  }
  if (recipeScope(state.project, stageId) && !state.project.stages[stageId].fleet_pending) {
    const identifier = state.recipeSelections[state.project.id] ?? state.project.recipe_id;
    if (!RECIPE_OPTIONS.some(entry => entry.recipe_id === identifier) || !recipeEntry(identifier)) {
      $("commonStageError").textContent = "请显式选择新配方；配方调度未就绪时不能执行。";
      $("commonStageError").classList.remove("hidden");
      return;
    }
    payload.recipe_id = identifier;
  }
  if (stageId === "regenerate_2k") {
    const confirmed = window.confirm("确认调用官方 2K 再生成并消耗 API 额度？");
    if (!confirmed) return;
  }
  await runAction(() =>
    api(`/api/projects/${state.project.id}/stages/${stageId}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  );
}

async function approveStage(stageId) {
  await runAction(() =>
    api(`/api/projects/${state.project.id}/stages/${stageId}/approve`, {
      method: "POST",
    }),
  );
  const next = state.project.pipeline.findIndex((stage) => stage.id === stageId) + 1;
  if (state.project.pipeline[next]) {
    state.selectedStage = state.project.pipeline[next].id;
  }
  renderStudio();
}

async function cancelStage(stageId) {
  await runAction(() =>
    api(`/api/projects/${state.project.id}/stages/${stageId}/cancel`, {
      method: "POST",
    }),
  );
}

async function runAction(action) {
  try {
    await action();
    await refreshCurrentProject();
  } catch (error) {
    window.alert(error.message);
  }
}

function openHistory() {
  refreshProjects();
  $("historyDrawer").classList.add("open");
  $("historyDrawer").setAttribute("aria-hidden", "false");
  $("drawerBackdrop").classList.remove("hidden");
}

function openQueue() {
  state.projectViewVersion += 1;
  clearTimeout(state.pollTimer);
  $("setupView").classList.add("hidden");
  $("studioView").classList.add("hidden");
  $("queueView").classList.remove("hidden");
  closeHistory();
  refreshQueueData(true);
}

function closeHistory() {
  $("historyDrawer").classList.remove("open");
  $("historyDrawer").setAttribute("aria-hidden", "true");
  $("drawerBackdrop").classList.add("hidden");
}

function renderHistory() {
  const root = $("historyList");
  if (!state.projects.length) {
    root.innerHTML = '<div class="history-item"><span>暂无项目</span></div>';
    return;
  }
  root.innerHTML = state.projects
    .map(
      (project) => `
        <div class="history-row">
          <button class="history-item" type="button" data-project="${project.id}">
            <strong>${escapeHtml(project.name)}</strong>
            <span>${MODE_LABELS[project.mode]} · ${STRATEGY_LABELS[project.strategy]} · ${formatDate(project.updated_at)}</span>
            <span>${escapeHtml(historyTimings(project))}</span>
          </button>
          <button class="history-delete" type="button" data-delete-project="${project.id}" title="删除项目">
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      `,
    )
    .join("");
  root.querySelectorAll("[data-project]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await openProject(button.dataset.project);
      } catch (error) {
        window.alert(error.message);
      }
    });
  });
  root.querySelectorAll("[data-delete-project]").forEach((button) => {
    button.addEventListener("click", async () => {
      const projectId = button.dataset.deleteProject;
      const project = state.projects.find((item) => item.id === projectId);
      if (!window.confirm(`删除项目“${project?.name || projectId}”及其输出文件？`)) return;
      try {
        await api(`/api/projects/${projectId}`, { method: "DELETE" });
        if (state.project?.id === projectId) showSetup();
        await refreshProjects();
      } catch (error) {
        window.alert(error.message);
      }
    });
  });
  refreshIcons();
}

async function refreshQueueData(render = true) {
  try {
    const [candidatePayload, schedulePayload] = await Promise.all([
      api("/api/768-queue/candidates"),
      api("/api/768-queue/schedules"),
    ]);
    state.queueCandidates = candidatePayload.candidates;
    state.schedules = schedulePayload.schedules;
    renderQueueBadge();
    if (render && !$("queueView").classList.contains("hidden")) {
      renderCandidates();
      renderSelectedQueue();
      renderSchedules();
      scheduleQueuePolling();
      refreshIcons();
    }
  } catch (error) {
    if (render) $("scheduleError").textContent = error.message;
  }
}

function renderQueueBadge() {
  const pending = state.schedules
    .filter((schedule) =>
      ["scheduled", "waiting_for_gpu", "running", "pausing", "paused"].includes(
        schedule.status,
      ),
    )
    .reduce(
      (total, schedule) =>
        total +
        schedule.summary.pending_count +
        (schedule.summary.running_count || 0),
      0,
    );
  $("queueBadge").textContent = String(pending);
  $("queueBadge").classList.toggle("hidden", pending === 0);
}

function scheduleQueuePolling() {
  clearTimeout(state.queuePollTimer);
  if ($("queueView").classList.contains("hidden")) return;
  state.queuePollTimer = setTimeout(() => refreshQueueData(true), 5000);
}

function setDefaultScheduleTime() {
  const now = new Date();
  const target = new Date(now);
  target.setHours(23, 0, 0, 0);
  if (target <= now) target.setDate(target.getDate() + 1);
  $("onceTimeInput").value = toLocalInputValue(target);
}

function selectScheduleKind(event) {
  const button = event.target.closest("[data-schedule-kind]");
  if (!button) return;
  state.scheduleKind = button.dataset.scheduleKind;
  $("scheduleKindControl")
    .querySelectorAll("[data-schedule-kind]")
    .forEach((item) => item.classList.toggle("active", item === button));
  $("onceTimeField").classList.toggle("hidden", state.scheduleKind !== "once");
  $("dailyTimeField").classList.toggle("hidden", state.scheduleKind !== "daily");
  renderSelectedQueue();
}

function renderCandidates() {
  const selected = new Set(state.selectedQueueIds);
  const root = $("candidateList");
  if (!state.queueCandidates.length) {
    root.innerHTML = '<div class="queue-empty">暂无满足条件的项目</div>';
    return;
  }
  root.innerHTML = state.queueCandidates
    .map(
      (candidate) => `
        <label class="candidate-row ${selected.has(candidate.id) ? "selected" : ""}">
          <input type="checkbox" data-candidate-id="${candidate.id}" ${selected.has(candidate.id) ? "checked" : ""} />
          <span class="candidate-main">
            <strong>${escapeHtml(candidate.name)}</strong>
            <small>${MODE_LABELS[candidate.mode]} · ${candidate.duration.toFixed(2)}秒 · Seed ${candidate.seed}</small>
          </span>
          <span class="candidate-time">${escapeHtml(candidate.estimate.label)}</span>
        </label>
      `,
    )
    .join("");
  root.querySelectorAll("[data-candidate-id]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const id = checkbox.dataset.candidateId;
      if (checkbox.checked && !state.selectedQueueIds.includes(id)) {
        state.selectedQueueIds.push(id);
      } else if (!checkbox.checked) {
        state.selectedQueueIds = state.selectedQueueIds.filter((item) => item !== id);
      }
      renderCandidates();
      renderSelectedQueue();
    });
  });
}

function selectAllCandidates() {
  const allIds = state.queueCandidates.map((candidate) => candidate.id);
  const allSelected = allIds.every((id) => state.selectedQueueIds.includes(id));
  if (allSelected) {
    state.selectedQueueIds = state.selectedQueueIds.filter((id) => !allIds.includes(id));
  } else {
    allIds.forEach((id) => {
      if (!state.selectedQueueIds.includes(id)) state.selectedQueueIds.push(id);
    });
  }
  renderCandidates();
  renderSelectedQueue();
}

function selectedProjectInfo(projectId) {
  const candidate = state.queueCandidates.find((item) => item.id === projectId);
  if (candidate) return candidate;
  const schedule = state.schedules.find((item) => item.id === state.editingScheduleId);
  const queueItem = schedule?.items.find((item) => item.project_id === projectId);
  if (!queueItem) return null;
  return {
    id: projectId,
    name: queueItem.project_name,
    mode: queueItem.mode,
    duration: queueItem.duration,
    estimate: {
      low_seconds: queueItem.estimate_low_seconds,
      high_seconds: queueItem.estimate_high_seconds,
      label: `${formatSeconds(queueItem.estimate_low_seconds)}–${formatSeconds(queueItem.estimate_high_seconds)}`,
    },
  };
}

function renderSelectedQueue() {
  const root = $("selectedQueueList");
  $("selectedCount").textContent = `${state.selectedQueueIds.length} 项`;
  if (!state.selectedQueueIds.length) {
    root.innerHTML = '<div class="queue-empty compact">从左侧选择项目</div>';
    $("queueEstimate").textContent = "尚未选择项目";
    $("queueFinishEstimate").textContent = "";
    return;
  }
  root.innerHTML = state.selectedQueueIds
    .map((projectId, index) => {
      const item = selectedProjectInfo(projectId);
      return `
        <div class="selected-queue-row">
          <span class="queue-position">${String(index + 1).padStart(2, "0")}</span>
          <div>
            <strong>${escapeHtml(item?.name || projectId)}</strong>
            <small>${item ? `${MODE_LABELS[item.mode]} · ${item.estimate.label}` : "项目信息不可用"}</small>
          </div>
          <div class="queue-order-actions">
            <button type="button" data-move-up="${projectId}" title="上移" ${index === 0 ? "disabled" : ""}><i data-lucide="chevron-up"></i></button>
            <button type="button" data-move-down="${projectId}" title="下移" ${index === state.selectedQueueIds.length - 1 ? "disabled" : ""}><i data-lucide="chevron-down"></i></button>
            <button type="button" data-remove-selected="${projectId}" title="移除"><i data-lucide="x"></i></button>
          </div>
        </div>
      `;
    })
    .join("");
  root.querySelectorAll("[data-move-up]").forEach((button) => {
    button.addEventListener("click", () => moveSelected(button.dataset.moveUp, -1));
  });
  root.querySelectorAll("[data-move-down]").forEach((button) => {
    button.addEventListener("click", () => moveSelected(button.dataset.moveDown, 1));
  });
  root.querySelectorAll("[data-remove-selected]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedQueueIds = state.selectedQueueIds.filter(
        (id) => id !== button.dataset.removeSelected,
      );
      renderCandidates();
      renderSelectedQueue();
    });
  });
  const info = state.selectedQueueIds.map(selectedProjectInfo).filter(Boolean);
  const low = info.reduce((sum, item) => sum + item.estimate.low_seconds, 0);
  const high = info.reduce((sum, item) => sum + item.estimate.high_seconds, 0);
  $("queueEstimate").textContent = `${formatSeconds(low)}–${formatSeconds(high)}`;
  const start = selectedScheduleStart();
  $("queueFinishEstimate").textContent = start
    ? `预计结束：${formatDateTime(start + low)}–${formatDateTime(start + high)}`
    : "";
  refreshIcons();
}

function moveSelected(projectId, direction) {
  const index = state.selectedQueueIds.indexOf(projectId);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= state.selectedQueueIds.length) return;
  [state.selectedQueueIds[index], state.selectedQueueIds[target]] = [
    state.selectedQueueIds[target],
    state.selectedQueueIds[index],
  ];
  renderSelectedQueue();
}

function selectedScheduleStart() {
  if (state.scheduleKind === "once") {
    const value = $("onceTimeInput").value;
    return value ? new Date(value).getTime() / 1000 : null;
  }
  const [hour, minute] = ($("dailyTimeInput").value || "23:00").split(":").map(Number);
  const target = new Date();
  target.setHours(hour, minute, 0, 0);
  if (target <= new Date()) target.setDate(target.getDate() + 1);
  return target.getTime() / 1000;
}

async function saveSchedule(event) {
  event.preventDefault();
  $("scheduleError").textContent = "";
  if (!state.selectedQueueIds.length) {
    $("scheduleError").textContent = "请至少选择一个项目。";
    return;
  }
  const payload = {
    name: $("scheduleName").value.trim(),
    kind: state.scheduleKind,
    project_ids: state.selectedQueueIds,
    once_local: $("onceTimeInput").value,
    daily_time: $("dailyTimeInput").value,
  };
  try {
    if (state.editingScheduleId) {
      await api(`/api/768-queue/schedules/${state.editingScheduleId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/768-queue/schedules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    resetScheduleBuilder();
    await refreshQueueData(true);
  } catch (error) {
    $("scheduleError").textContent = error.message;
  }
}

function resetScheduleBuilder() {
  state.editingScheduleId = null;
  state.selectedQueueIds = [];
  state.scheduleKind = "once";
  $("scheduleName").value = "夜间768P批次";
  $("scheduleFormTitle").textContent = "安排运行";
  $("cancelScheduleEdit").classList.add("hidden");
  setDefaultScheduleTime();
  $("dailyTimeInput").value = "23:00";
  $("onceTimeField").classList.remove("hidden");
  $("dailyTimeField").classList.add("hidden");
  $("scheduleKindControl")
    .querySelectorAll("[data-schedule-kind]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.scheduleKind === "once"),
    );
  renderCandidates();
  renderSelectedQueue();
}

function editSchedule(scheduleId) {
  const schedule = state.schedules.find((item) => item.id === scheduleId);
  if (!schedule) return;
  state.editingScheduleId = scheduleId;
  state.selectedQueueIds = schedule.items
    .filter((item) => ["pending", "failed"].includes(item.status))
    .map((item) => item.project_id);
  state.scheduleKind = schedule.kind;
  $("scheduleName").value = schedule.name;
  $("scheduleFormTitle").textContent = "编辑计划";
  $("cancelScheduleEdit").classList.remove("hidden");
  if (schedule.kind === "once" && schedule.next_run_at) {
    $("onceTimeInput").value = toLocalInputValue(new Date(schedule.next_run_at * 1000));
  }
  if (schedule.daily_time) $("dailyTimeInput").value = schedule.daily_time;
  $("onceTimeField").classList.toggle("hidden", schedule.kind !== "once");
  $("dailyTimeField").classList.toggle("hidden", schedule.kind !== "daily");
  $("scheduleKindControl")
    .querySelectorAll("[data-schedule-kind]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.scheduleKind === schedule.kind),
    );
  renderCandidates();
  renderSelectedQueue();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderSchedules() {
  const root = $("scheduleList");
  if (!state.schedules.length) {
    root.innerHTML = '<div class="queue-empty">暂无定时计划</div>';
    return;
  }
  root.innerHTML = state.schedules
    .map((schedule) => renderSchedule(schedule))
    .join("");
  root.querySelectorAll("[data-schedule-action]").forEach((button) => {
    button.addEventListener("click", () =>
      handleScheduleAction(
        button.dataset.scheduleAction,
        button.dataset.scheduleId,
      ),
    );
  });
  refreshIcons();
}

function renderSchedule(schedule) {
  const startLabel =
    schedule.kind === "daily"
      ? `每天 ${schedule.daily_time}`
      : schedule.next_run_at
        ? formatDateTime(schedule.next_run_at)
        : "时间已结束";
  const items = schedule.items.length
    ? schedule.items
        .map(
          (item, index) => `
            <div class="schedule-item-row ${item.status}">
              <span>${String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>${escapeHtml(item.project_name)}</strong>
                <small>${ITEM_STATUS_LABELS[item.status] || item.status} · ${formatSeconds(item.estimate_low_seconds)}–${formatSeconds(item.estimate_high_seconds)}</small>
              </div>
              <div class="item-progress">
                <span style="width:${item.stage_progress || (item.status === "completed" ? 100 : 0)}%"></span>
              </div>
            </div>
          `,
        )
        .join("")
    : '<div class="queue-empty compact">等待加入下一批项目</div>';
  const actions = scheduleActions(schedule);
  return `
    <article class="schedule-record ${schedule.status}">
      <header>
        <div>
          <span class="schedule-kind">${schedule.kind === "daily" ? "每日" : "单次"}</span>
          <h3>${escapeHtml(schedule.name)}</h3>
        </div>
        <span class="status-pill ${schedule.status}">${SCHEDULE_STATUS_LABELS[schedule.status] || schedule.status}</span>
      </header>
      <div class="schedule-metrics">
        <span><small>计划时间</small><strong>${escapeHtml(startLabel)}</strong></span>
        <span><small>待运行</small><strong>${schedule.summary.pending_count} 项</strong></span>
        <span><small>预计耗时</small><strong>${formatSeconds(schedule.summary.low_seconds)}–${formatSeconds(schedule.summary.high_seconds)}</strong></span>
        <span><small>当前状态</small><strong>${escapeHtml(schedule.detail || "—")}</strong></span>
      </div>
      <div class="schedule-items">${items}</div>
      ${schedule.error ? `<div class="common-stage-error">${escapeHtml(schedule.error)}</div>` : ""}
      <footer>${actions}</footer>
    </article>
  `;
}

function scheduleActions(schedule) {
  const actions = [];
  const add = (action, icon, label, className = "") => {
    actions.push(
      `<button type="button" class="${className}" data-schedule-action="${action}" data-schedule-id="${schedule.id}"><i data-lucide="${icon}"></i>${label}</button>`,
    );
  };
  if (["scheduled", "waiting_for_items", "paused"].includes(schedule.status)) {
    add("edit", "list-ordered", "编辑");
  }
  if (["scheduled", "waiting_for_items", "paused"].includes(schedule.status) && schedule.summary.pending_count) {
    add("start-now", "play", "立即开始", "primary-action");
  }
  if (["running", "waiting_for_gpu"].includes(schedule.status)) {
    add("pause", "pause", "当前完成后暂停");
  }
  if (schedule.status === "paused") {
    add("resume", "play", "继续运行", "primary-action");
  }
  if (schedule.summary.failed_count && !["running", "waiting_for_gpu", "pausing"].includes(schedule.status)) {
    add("retry-failed", "rotate-ccw", "重试失败");
  }
  if (!["completed", "cancelled"].includes(schedule.status)) {
    add("cancel", "square", "取消批次", "danger-action");
  }
  if (!["running", "waiting_for_gpu", "pausing"].includes(schedule.status)) {
    add("delete", "trash-2", "删除");
  }
  return actions.join("");
}

async function handleScheduleAction(action, scheduleId) {
  if (action === "edit") {
    editSchedule(scheduleId);
    return;
  }
  if (action === "delete") {
    if (!window.confirm("删除这个计划并释放其中尚未运行的项目？")) return;
    await runQueueAction(() =>
      api(`/api/768-queue/schedules/${scheduleId}`, { method: "DELETE" }),
    );
    return;
  }
  if (action === "cancel" && !window.confirm("停止当前任务并取消剩余768P项目？")) return;
  if (action === "start-now" && !window.confirm("确认现在开始并独占本地GPU？")) return;
  await runQueueAction(() =>
    api(`/api/768-queue/schedules/${scheduleId}/${action}`, {
      method: "POST",
    }),
  );
}

async function runQueueAction(action) {
  try {
    await action();
    await refreshQueueData(true);
  } catch (error) {
    window.alert(error.message);
  }
}

function toLocalInputValue(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatDateTime(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

function setActiveButtons(containerId, value) {
  $(containerId)
    .querySelectorAll("[data-value]")
    .forEach((button) => button.classList.toggle("active", button.dataset.value === value));
}

async function api(path, options = {}) {
  let response = await fetch(studioUrl(path), options);
  if (response.status === 401) {
    if (!sessionRequest) {
      sessionRequest = (async () => {
        const key = sessionStorage.getItem("ai-router-admin-key") || window.prompt("请输入工作室访问密钥（1Panel入口使用管理密钥）");
        if (!key) throw new Error("需要访问密钥");
        const login = await fetch(studioUrl("/session"), {method: "POST", headers: {Authorization: `Bearer ${key}`}});
        if (!login.ok) throw new Error("认证失败，请刷新后重试");
      })();
    }
    try {
      await sessionRequest;
    } finally {
      sessionRequest = null;
    }
    response = await fetch(studioUrl(path), options);
  }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(payload?.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function formatDate(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}

function projectEstimateLabel(summary) {
  if (!summary) return "预计时间未知";
  const range = `${formatSeconds(summary.low_seconds)}–${formatSeconds(summary.high_seconds)}`;
  return summary.dynamic_cloud_stages
    ? `已知阶段 ${range} + 云端队列`
    : `预计 ${range}`;
}

function elapsedSeconds(stage) {
  const start = stage.queued_at || stage.started_at;
  if (!start) return 0;
  const end = stage.finished_at || Date.now() / 1000;
  return Math.max(0, end - start);
}

function historyTimings(project) {
  return project.pipeline.filter((stage) => stage.finished_at && (stage.queued_at || stage.started_at))
    .map((stage) => `${stage.label}：${formatSeconds(elapsedSeconds(stage))}`)
    .join(" · ") || "暂无已结束阶段";
}

function formatSeconds(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  if (seconds < 60) return `${seconds}秒`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  if (hours) return `${hours}小时${String(minutes).padStart(2, "0")}分${String(remaining).padStart(2, "0")}秒`;
  return `${minutes}分${String(remaining).padStart(2, "0")}秒`;
}

function formatBytes(value) {
  const bytes = Number(value);
  if (value == null || value === "" || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes === 0) return "0 B";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortId(value) {
  const text = String(value || "");
  if (!text || text === "—") return "—";
  return text.length > 14 ? `${text.slice(0, 6)}…${text.slice(-5)}` : text;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function refreshIcons() {
  if (window.lucide) {
    window.lucide.createIcons();
  }
}
