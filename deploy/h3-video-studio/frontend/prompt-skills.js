window.PromptSkills = class PromptSkills {
  constructor({api, readBrief, apply, restore, changed}) {
    Object.assign(this, {api, readBrief, apply, restore, changed});
    this.plan = null;
    this.applied = false;
    this.busy = false;
    this.configured = false;
    this.baselines = new Set();
    this.timer = null;
    this.pending = null;
    this.storageKey = "h3-prompt-skills-pending";
    try {
      const saved = JSON.parse(sessionStorage.getItem(this.storageKey) || "null");
      if (typeof saved?.fingerprint === "string" && typeof saved?.operation === "string") this.pending = saved;
    } catch { }
    this.active = new Set(["created", "preflight", "matched", "dispatched", "running", "validating"]);
    this.statuses = {created: "等待策划", preflight: "检查需求", matched: "Skills 已选择", dispatched: "组合 Skills 规则", running: "文字策划中", validating: "校验脚本", completed: "脚本已产出，等待确认", needs_context: "需要补充信息", failed: "策划未完成", cancelled: "本轮已取消"};
    this.element("Start").onclick = () => this.run(() => this.start());
    this.element("Revise").onclick = () => this.run(() => this.revise());
    this.element("Apply").onclick = () => this.run(() => this.approve());
    this.element("Cancel").onclick = () => this.run(() => this.action("cancel"));
    this.element("Refresh").onclick = () => this.run(() => this.refresh());
    this.element("Retry").onclick = () => this.run(() => this.retry());
    this.element("Discard").onclick = () => this.discard();
    this.element("Notes").oninput = () => this.controls();
    this.element("Instruction").oninput = () => this.controls();
    if (this.pending) {
      this.element("Result").hidden = false;
      this.element("Status").textContent = "上次请求结果尚未确认，请重试原请求，不要重复新建。";
    }
  }

  element(name) { return document.getElementById("promptSkills" + name); }
  get working() { return this.busy || Boolean(this.plan && this.active.has(this.plan.status)); }
  get blocked() { return this.busy || Boolean(this.pending) || Boolean(this.plan && (!this.applied || this.stale())); }

  configure(options) {
    this.configured = Boolean(options?.capability_manifest.configured);
    this.baselines = new Set((options?.skills || []).filter((skill) => skill.baseline).map((skill) => skill.id));
    const capability = options?.capability_manifest;
    this.element("Capability").textContent = this.configured
      ? `文字策划：${capability.model}，${capability.validated ? "本进程已验证" : "尚未实测"}。每次点击最多两次文字调用；先展示选择结果，再写脚本，不自动生成视频。`
      : "文字策划尚未接入当前服务，自动选择暂不可用；不会用预设 Skills 冒充结果。你仍可直接手写提示词创建任务。";
    this.controls();
  }

  stale() {
    if (!this.plan) return false;
    const current = this.readBrief();
    const expected = {...this.plan.brief, prompt: this.applied ? this.plan.generation_prompt : this.plan.brief.prompt};
    const normalize = (value) => typeof value === "string" ? value.replace(/\r\n?/g, "\n").trim() : value;
    return ["prompt", "duration", "mode", "audio_policy"].some((key) => normalize(current[key]) !== normalize(expected[key]))
      || (!this.applied && this.notesSnapshot !== this.element("Notes").value);
  }

  controls() {
    const ready = this.plan?.status === "completed" && this.plan.draft_revision === this.plan.revision;
    const stale = this.stale();
    this.element("Start").disabled = !this.configured || this.working;
    this.element("Start").textContent = this.plan ? "重新自动选择 Skills 并策划" : "自动选择 Skills 并生成脚本";
    this.element("Revise").disabled = !this.configured || !this.plan || this.working || stale || !this.element("Instruction").value.trim();
    this.element("Apply").disabled = !ready || this.working || stale || this.applied;
    this.element("Cancel").hidden = !this.plan || !this.active.has(this.plan.status);
    this.element("Cancel").disabled = this.busy;
    this.element("Refresh").disabled = this.busy;
    this.element("Retry").hidden = !this.pending;
    this.element("Retry").disabled = this.busy;
    this.element("Discard").disabled = this.working;
    this.element("Stale").hidden = !stale;
    document.getElementById("projectForm").classList.toggle("has-script-plan", Boolean(this.plan) || Boolean(this.pending));
    this.element("Approval").textContent = this.applied
      ? "已填入批准版本。尚未创建视频任务；请检查素材后，手动点击创建并运行 Context IR。"
      : "确认前不会覆盖原始提示词。问题未解决、脚本未确认或设置已改变时，不会进入生成。";
    if (this.plan) {
      const status = this.plan.status === "running" ? this.plan.skills.length ? "使用选中 Skills 编写脚本" : "模型正在自动选择 Skills" : this.statuses[this.plan.status] || this.plan.status;
      this.element("Status").textContent = `v${this.plan.revision} · ${this.applied ? "已确认并填入" : status}`;
    }
    this.changed();
  }

  async run(action) {
    if (this.busy) return;
    this.busy = true;
    this.element("Error").textContent = "";
    this.controls();
    try { await action(); }
    catch (error) { this.element("Error").textContent = error.message; }
    finally { this.busy = false; this.controls(); }
  }

  async mutate(path, payload) {
    const fingerprint = JSON.stringify({path, payload});
    if (this.pending && this.pending.fingerprint !== fingerprint) {
      throw new Error("上一操作结果尚未确认，请先重试原操作或刷新状态，避免重复调用。");
    }
    this.pending ||= {fingerprint, operation: crypto.randomUUID()};
    sessionStorage.setItem(this.storageKey, JSON.stringify(this.pending));
    this.element("Result").hidden = false;
    if (!this.plan) this.element("Status").textContent = "正在提交策划；等待自动选择结果";
    try {
      const result = await this.api(path, {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({...payload, operation_id: this.pending.operation})});
      this.pending = null;
      sessionStorage.removeItem(this.storageKey);
      return result;
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        this.pending = null;
        sessionStorage.removeItem(this.storageKey);
      }
      throw error;
    }
  }

  async start() {
    if (this.plan && this.active.has(this.plan.status)) throw new Error("请先等待或取消当前策划。");
    if (this.applied) {
      this.restore(this.stale() ? this.readBrief().prompt : this.plan.brief.prompt);
      this.applied = false;
    }
    const brief = this.readBrief();
    if (!brief.prompt.trim()) throw new Error("请先填写上方原始提示词。");
    this.notesSnapshot = this.element("Notes").value;
    this.receive(await this.mutate("/api/scripts", {brief}), false);
  }

  async action(name, extra = {}) {
    const plan = await this.mutate(`/api/scripts/${this.plan.id}/${name}`, {revision: this.plan.revision, ...extra});
    this.receive(plan, false);
    return plan;
  }

  async revise() {
    if (this.stale()) throw new Error("需求或设置已变，请重新策划。");
    const instruction = this.element("Instruction").value.trim();
    if (!instruction) throw new Error("请填写修改要求或补充信息。");
    if (this.applied) {
      this.restore(this.plan.brief.prompt);
      this.applied = false;
    }
    await this.action("revise", {instruction});
    this.element("Instruction").value = "";
  }

  async approve() {
    if (this.stale()) throw new Error("需求或设置已变，不能应用旧脚本。");
    const approved = await this.action("approve");
    if (this.stale()) throw new Error("确认期间输入已改变，已保留你的原文，未回填脚本。");
    this.apply(approved);
    this.applied = true;
    this.persist();
  }

  async refresh() {
    if (!this.plan) return;
    const latest = await this.api(`/api/scripts/${this.plan.id}`);
    const applied = this.applied && latest.revision === this.plan.revision && latest.approved;
    this.receive(latest, applied);
  }

  async retry() {
    if (!this.pending) return;
    const {path, payload} = JSON.parse(this.pending.fingerprint);
    const result = await this.mutate(path, payload);
    const current = this.readBrief();
    if (!this.plan && (!current.prompt.trim() || current.prompt === result.brief.prompt)) {
      const sameSettings = ["duration", "mode", "audio_policy"].every((key) => current[key] === result.brief[key]);
      this.restore(result.brief.prompt, sameSettings ? null : result.brief);
      this.element("Notes").value = result.brief.asset_notes;
      this.notesSnapshot = this.element("Notes").value;
    }
    this.receive(result, false);
  }

  receive(plan, applied = false) {
    clearTimeout(this.timer);
    if (this.applied && !applied) this.restore(this.plan.brief.prompt);
    this.plan = plan;
    this.applied = applied;
    this.element("Result").hidden = false;
    this.element("Status").textContent = `v${plan.revision} · ${applied ? "已确认并填入" : this.statuses[plan.status] || plan.status}`;
    this.element("Error").textContent = plan.error?.message || "";
    this.element("Reason").textContent = plan.selection_reason || "等待模型返回选择理由；尚无自动选择结果。";
    this.element("Selected").replaceChildren(...plan.skills.map((skill) => {
      const chip = document.createElement("span");
      const baseline = skill.baseline ?? this.baselines.has(skill.id);
      chip.dataset.baseline = baseline;
      chip.textContent = `${skill.name} · ${baseline ? "固定基础规则" : plan.brief.skill_ids.length ? "指定选用" : "自动选用"}`;
      chip.title = `${skill.id}\n${skill.sources.map((source) => source.directory).join(" / ")}`;
      return chip;
    }));
    const draft = plan.draft_revision === plan.revision ? plan.draft : null;
    this.element("Draft").hidden = !draft;
    if (draft) this.renderDraft(draft);
    this.element("Evidence").textContent = JSON.stringify({original_prompt: plan.brief.prompt, script_id: plan.id,
      revision: plan.revision, model_calls: plan.model_calls, traces: plan.traces}, null, 2);
    this.persist();
    this.controls();
    if (this.active.has(plan.status)) {
      this.timer = setTimeout(async () => {
        if (this.busy) { this.receive(this.plan, this.applied); return; }
        try {
          const latest = await this.api(`/api/scripts/${plan.id}`);
          if (!this.busy && this.plan?.id === plan.id && latest.updated_at >= this.plan.updated_at) this.receive(latest, false);
        } catch (error) { this.element("Error").textContent = `${error.message}；请刷新状态，不会自动重发策划。`; }
      }, 1000);
    }
  }

  renderDraft(draft) {
    this.element("Title").textContent = draft.title;
    this.element("Summary").textContent = draft.summary;
    this.element("Shots").replaceChildren(...draft.shots.map((shot) => {
      const card = document.createElement("section"); card.className = "prompt-skills-shot";
      const heading = document.createElement("strong"); heading.textContent = `${shot.start}–${shot.end} 秒`; card.append(heading);
      for (const [name, value] of [["画面", shot.visual], ["镜头", shot.camera], ["对白", shot.dialogue || "无"], ["声音", shot.sound || "无"]]) {
        const paragraph = document.createElement("p"); paragraph.textContent = `${name}：${value}`; card.append(paragraph);
      }
      return card;
    }));
    this.element("Music").textContent = "音乐：" + (draft.music || "无");
    const items = (values) => values.map((value) => { const item = document.createElement("li"); item.textContent = value; return item; });
    this.element("Continuity").replaceChildren(...items(draft.continuity));
    for (const [name, values] of [["Questions", draft.questions], ["Assumptions", draft.assumptions]]) {
      this.element(name).hidden = !values.length;
      this.element(name).querySelector("ul").replaceChildren(...items(values));
    }
    this.element("Compiled").value = this.plan.generation_prompt;
  }

  persist() {
    const url = new URL(location.href);
    for (const key of ["script_draft", "script_plan", "revision"]) url.searchParams.delete(key);
    if (this.plan) {
      url.searchParams.set(this.applied ? "script_plan" : "script_draft", this.plan.id);
      if (this.applied) url.searchParams.set("revision", this.plan.revision);
    }
    history.replaceState(null, "", url);
  }

  discard() {
    if (this.working) return false;
    if (this.pending && !window.confirm("上一请求结果未知，清除面板不会取消可能已在后台运行的任务。仍要恢复原始需求？")) return false;
    if (this.plan) this.restore(this.plan.brief.prompt);
    this.reset();
    return true;
  }

  reset() {
    clearTimeout(this.timer);
    this.plan = null;
    this.applied = false;
    this.pending = null;
    sessionStorage.removeItem(this.storageKey);
    this.element("Result").hidden = true;
    this.element("Error").textContent = "";
    this.element("Instruction").value = "";
    this.persist();
    this.controls();
  }
};
