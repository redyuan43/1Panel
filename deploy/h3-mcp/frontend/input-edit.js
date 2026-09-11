"use strict";
(() => {
  let editing = null;
  const removedAssets = new Set();
  const roles = ["first_frame", "last_frame", "reference_image", "reference_video", "reference_audio"];
  const fields = {first_frame: "first_frame", last_frame: "last_frame", reference_image: "reference_image", reference_video: "reference_video", reference_audio: "reference_audio"};
  const pendingUploads = new Map();
  const pendingDrafts = new Map();
  const pendingActions = new Map();
  window.addEventListener("h3-input-edit-reset", () => {
    editing = null;
    removedAssets.clear();
    document.getElementById("retainedInputAssets")?.remove();
  });

  async function upload(file, role) {
    const checksum = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer())), byte => byte.toString(16).padStart(2, "0")).join("");
    const key = JSON.stringify([editing?.id, role, file.name, file.size, checksum]);
    let operation = pendingUploads.get(key);
    if (!operation) { operation = crypto.randomUUID(); pendingUploads.set(key, operation); }
    const metadata = {operation_id: operation, kind: role, filename: file.name, size: file.size, sha256: checksum};
    const query = editing ? `?task_id=${encodeURIComponent(editing.id)}` : "";
    let receipt;
    try { receipt = await api(`/api/h3-browser/assets/uploads/${operation}${query}`); }
    catch (error) { if (error.status !== 404) throw error; }
    if (!receipt) receipt = await api(`/api/h3-browser/assets/uploads${query}`, {method: "POST", body: file,
      headers: {"Content-Type": "application/octet-stream", "X-H3-Upload-Metadata": JSON.stringify(metadata).replace(/[\u007f-\uffff]/g, character => "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0"))}});
    if (receipt.state !== "ready" || receipt.sha256 !== checksum || receipt.kind !== role) throw new Error("素材回执尚未就绪或不一致；请保留原操作并查询，不要重复创建上传。");
    return receipt.asset_id;
  }

  window.h3SaveBrowserDraft = async function(form) {
    const mode = form.get("mode");
    const allowed = {t2v: [], i2v: ["first_frame"], l2v: ["last_frame"], fl2v: ["first_frame", "last_frame"],
      reference: ["reference_image", "reference_video", "reference_audio"], hybrid: ["first_frame", "last_frame", "reference_image"]}[mode];
    if (!allowed) throw new Error("未知输入模式");
    if (form.get("audio_policy") === "lock_source") allowed.push("reference_audio");
    const assets = {};
    for (const role of roles) {
      const file = form.get(fields[role]);
      if (!allowed.includes(role)) continue;
      if (file instanceof File && file.size > 0) {
        if (file.size > 128 * 1024 * 1024) throw new Error("单个素材不能超过128MiB");
        assets[role] = await upload(file, role);
      } else if (!removedAssets.has(role) && editing?.assets?.[role]?.asset_id) assets[role] = editing.assets[role].asset_id;
    }
    const prompt = String(form.get("prompt") || "");
    const body = {original_prompt: editing?.prompt_original || prompt, prompt,
      name: String(form.get("name") || "H3 视频项目"), mode, assets, duration: Number(form.get("duration")),
      orientation: form.get("orientation"), audio_policy: form.get("audio_policy"),
      use_embedded_video_audio: form.get("use_embedded_video_audio") === "true"};
    if (!editing && state.scriptSource) {
      const plan = state.promptSkills?.plan;
      if (!plan || plan.id !== state.scriptSource.id || plan.revision !== state.scriptSource.revision || plan.generation_prompt !== prompt) throw new Error("当前创作脚本来源未核实，请先刷新脚本，不丢弃原始需求继续生成。");
      body.original_prompt = plan.brief.prompt;
      body.skill_sources = [{name: `Studio 已确认脚本 ${plan.id} v${plan.revision}`, source: "server_guidance"}];
    }
    if (mode === "t2v") body.recipe_id = form.get("recipe_id") || "A4";
    if (editing) Object.assign(body, {task_id: editing.id, expected_revision: editing.connector_revision,
      verbatim: prompt === editing.prompt_original && editing.connector_verbatim === true});
    const seedText = String(form.get("seed") ?? "").trim();
    if (seedText) {
      const seed = Number(seedText);
      if (!Number.isSafeInteger(seed) || seed < (editing ? 0 : -1)) throw new Error("请输入安全范围内的非负整数种子；修改任务时留空会保留原种子。");
      body.seed = seed;
    }
    const key = JSON.stringify(body);
    if (!pendingDrafts.has(key)) pendingDrafts.set(key, crypto.randomUUID());
    body.operation_id = pendingDrafts.get(key);
    try {
      let result;
      try { result = await api(`/api/h3-browser/operations/${body.operation_id}${editing ? `?task_id=${editing.id}` : ""}`); }
      catch (error) { if (error.status !== 404) throw error; }
      if (!result) result = await api("/api/h3-browser/call/h3_save_draft", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      editing = null;
      return result;
    } catch (error) { error.message += ` 操作编号 ${body.operation_id}；结果未知时先对账，不要再次点击创建。`; throw error; }
  };

  window.h3BrowserAction = async function(tool, extra = {}) {
    const project = state.project;
    const body = {task_id: project.id, expected_revision: project.connector_revision, ...extra};
    const key = JSON.stringify([tool, body]);
    if (!pendingActions.has(key)) pendingActions.set(key, crypto.randomUUID());
    body.operation_id = pendingActions.get(key);
    return api(`/api/h3-browser/call/${tool}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  };

  window.addEventListener("h3-project-rendered", event => {
    const project = event.detail;
    const prompt = document.getElementById("optimizedPrompt");
    if (prompt) prompt.readOnly = Boolean(project?.connector_owner);
    if (!project?.connector_owner) return;
    const panel = document.querySelector(".input-material-panel");
    if (!panel) return;
    const previous = panel.querySelector("[data-edit-inputs]");
    if (previous) previous.remove();
    const button = document.createElement("button"); button.type = "button"; button.dataset.editInputs = "true";
    button.textContent = "修改输入 · 新版本需重新确认";
    button.disabled = Object.values(project.stages).some(stage => ["running", "queued", "cancelling", "scheduled"].includes(stage.status) || stage.fleet_pending);
    button.addEventListener("click", () => {
      const project = state.project;
      if (!project?.connector_owner || Object.values(project.stages).some(stage => ["running", "queued", "cancelling", "scheduled"].includes(stage.status) || stage.fleet_pending)) return;
      showSetup();
      editing = project;
      state.mode = project.mode; state.audioPolicy = project.audio_policy; state.strategy = "fast";
      const form = document.getElementById("projectForm");
      for (const [name, value] of Object.entries({name: project.name, prompt: project.prompt_ir, duration: project.duration,
        orientation: project.orientation, recipe_id: project.recipe_id, prompt_processing: "manual"})) {
        if (value != null && form.elements.namedItem(name)) form.elements.namedItem(name).value = value;
      }
      for (const role of roles) if (form.elements.namedItem(role)) form.elements.namedItem(role).value = "";
      document.getElementById("formError").textContent = "正在修改已有任务；未更换的同用途素材保留，保存后旧批准失效。";
      document.getElementById("seedInput").value = "";
      document.getElementById("manualSeedInput").value = "";
      document.getElementById("manualSeedInput").placeholder = "留空保留原种子";
      document.getElementById("modeInput").value = project.mode;
      setActiveButtons("modeControl", project.mode);
      renderAssetFields();
      state.audioPolicy = project.audio_policy;
      document.getElementById("audioPolicyInput").value = project.audio_policy;
      if (document.getElementById("embeddedAudio")) document.getElementById("embeddedAudio").checked = project.use_embedded_video_audio === true;
      if (project.audio_policy === "lock_source" && document.getElementById("audioLockToggle")) {
        document.getElementById("audioLockToggle").checked = true;
        document.getElementById("audioLockToggle").dispatchEvent(new Event("change"));
      }
      for (const role of roles) if (form.elements.namedItem(role)) form.elements.namedItem(role).required = false;
      const retained = document.createElement("fieldset");
      retained.id = "retainedInputAssets";
      const legend = document.createElement("legend"); legend.textContent = "已有素材：默认保留；取消勾选即移除，必需素材仍需重新提供"; retained.append(legend);
      for (const asset of project.input_assets || []) {
        const label = document.createElement("label");
        const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = true;
        checkbox.addEventListener("change", () => checkbox.checked ? removedAssets.delete(asset.kind) : removedAssets.add(asset.kind));
        label.append(checkbox, document.createTextNode(`${asset.kind} · ${asset.filename || asset.asset_id}`));
        retained.append(label);
      }
      if (retained.children.length > 1) form.append(retained);
      renderCreateRecipes();
    });
    panel.append(button);
  });
})();
