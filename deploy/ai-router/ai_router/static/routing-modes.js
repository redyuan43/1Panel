"use strict";
window.RoutingModeUI = (() => {
  const labels = {cost:"成本优先", efficiency:"效率优先", quality:"质量优先", inherit:"继承全局"};
  const defaults = {
    enabled:false, mode:"efficiency", local_only:false, observe_only:false,
    quality_flash_fallback:true, allow_advisory_output_limit:true,
    flash_order:{general:["cloud-deepseek-v4-flash","zhipu-glm-5.3-flash"],code:["zhipu-glm-5.3-flash","cloud-deepseek-v4-flash"],multimodal:["zhipu-glm-5.3-flash"]},
    quality_order:{general:["cloud-deepseek-v4-pro","codex-pro-gpt-6-astra","zhipu-glm-5.3"],code:["codex-pro-gpt-6-astra","cloud-deepseek-v4-pro","zhipu-glm-5.3"],multimodal:["codex-pro-gpt-6-astra"]},
    performance:{max_first_output_seconds:180,min_decode_tps:10,slowdown_ratio:.5,window_seconds:1800,max_samples:100,min_samples:5,min_output_tokens:128,min_decode_seconds:5,min_saving_ratio:.3,min_saving_seconds:15,cooldown_seconds:600}
  };
  const names = {"cloud-deepseek-v4-pro":"DeepSeek V4 Pro","codex-pro-gpt-6-astra":"GPT-6 Astra","zhipu-glm-5.3":"GLM 5.3","cloud-deepseek-v4-flash":"DeepSeek V4 Flash","zhipu-glm-5.3-flash":"GLM 5.3 Flash","codex-pro-gpt-5.6-sol":"GPT-5.6 Sol"};
  const $ = id => document.getElementById(id);
  let value = structuredClone(defaults);
  function orderEditor(target, order, kind) {
    const panel = $(target); panel.replaceChildren();
    for (const [group,title] of Object.entries({general:"通用 / Agent 文本",code:"代码",multimodal:"图像"})) {
      const row = document.createElement("div"); row.className="mode-order-row";
      const heading = document.createElement("strong"); heading.textContent=title; row.append(heading);
      const ids = [...new Set([...Object.keys(names), ...(order[group] || [])])];
      for (let index=0; index<Math.max(3, (order[group] || []).length); index++) {
        const label = document.createElement("label"); label.textContent=index===0?"首选":"备选 "+index;
        const select = document.createElement("select");
        select.dataset.modeOrder=kind; select.dataset.group=group;
        select.append(new Option("不使用", ""));
        for (const id of ids) select.append(new Option(names[id] || id,id));
        select.value=order[group]?.[index] || ""; label.append(select); row.append(label);
      }
      panel.append(row);
    }
  }
  function summary() {
    const live = state.policy?.active?.settings?.routing?.objectives;
    $("routing-live-mode").textContent=live?.enabled?labels[live.mode]+(live.observe_only?" · 仅观察":""):"原有路由规则";
    const mode = document.querySelector('[name="routing-objective"]:checked')?.value || value.mode;
    $("routing-edit-mode").textContent=labels[mode];
    $("routing-inherit-count").textContent=String((state.clients || []).filter(c => !c.routing_mode || c.routing_mode==="inherit").length);
    for (const card of document.querySelectorAll("[data-objective-options]")) card.hidden=card.dataset.objectiveOptions!==mode;
    $("routing-local-notice").hidden=!$("routing-local-only").checked;
  }
  function render(settings) {
    value={...structuredClone(defaults),...structuredClone(settings?.routing?.objectives || {})};
    value.performance={...defaults.performance,...value.performance};
    document.querySelector('[name="routing-objective"][value="'+value.mode+'"]').checked=true;
    $("routing-objectives-enabled").checked=value.enabled;
    $("routing-local-only").checked=value.local_only;
    $("routing-quality-fallback").checked=value.quality_flash_fallback;
    $("routing-advisory").checked=value.allow_advisory_output_limit;
    $("routing-observe-only").checked=value.observe_only;
    for (const [key,v] of Object.entries(value.performance)) {
      const input=document.querySelector('[data-performance="'+key+'"]');
      if (input) input.value=v;
    }
    orderEditor("routing-quality-order",value.quality_order,"quality_order");
    orderEditor("routing-flash-order",value.flash_order,"flash_order");
    summary();
  }
  function collect() {
    const result=structuredClone(value);
    result.enabled=$("routing-objectives-enabled").checked;
    result.mode=document.querySelector('[name="routing-objective"]:checked').value;
    result.local_only=$("routing-local-only").checked;
    result.quality_flash_fallback=$("routing-quality-fallback").checked;
    result.allow_advisory_output_limit=$("routing-advisory").checked;
    result.observe_only=$("routing-observe-only").checked;
    for (const input of document.querySelectorAll("[data-performance]")) result.performance[input.dataset.performance]=Number(input.value);
    for (const kind of ["quality_order","flash_order"]) for (const group of ["general","code","multimodal"]) {
      result[kind][group]=[...document.querySelectorAll('[data-mode-order="'+kind+'"][data-group="'+group+'"]')].map(x=>x.value).filter(Boolean);
    }
    return result;
  }
  async function apply() {
    const button=$("routing-apply");
    if (!$("settings-form").checkValidity()) {
      $("routing-mode-view").value="advanced";
      $("settings-view").dataset.policyView="advanced";
      $("routing-apply-state").textContent="请检查参数范围；尚未应用。";
      $("settings-form").reportValidity();
      return;
    }
    button.disabled=true;
    $("routing-apply-state").textContent="正在保存并验证…";
    try {
      const changes=collectSettings();
      const errors=validateSettingsDraft(changes);
      if(errors.length) throw new Error(errors[0]);
      const prior=state.policy?.draft || state.policy?.active;
      let response=await api("/api/policy/draft",{method:"PATCH",body:JSON.stringify({changes,expected_revision:prior?.revision,expected_fingerprint:prior?.settings_fingerprint})});
      state.policy={...state.policy,draft:response.draft};
      let draft=response.draft;
      response=await api("/api/policy/draft/validate",{method:"POST",body:JSON.stringify({expected_revision:draft.revision,expected_fingerprint:draft.settings_fingerprint,limit:100})});
      state.policy={...state.policy,draft:response.draft};
      draft=response.draft;
      if(draft.status!=="validated") throw new Error("草稿未通过验证，当前生效策略未改变。");
      await api("/api/policy/draft/activate",{method:"POST",body:JSON.stringify({expected_revision:draft.revision,expected_fingerprint:draft.settings_fingerprint})});
      await loadSettings(); await loadClients(true);
      $("routing-apply-state").textContent="已应用，后续请求使用新策略。";
    } catch(error) {
      $("routing-apply-state").textContent="未应用："+error.message;
    } finally {button.disabled=false;}
  }
  function accountLabel(account) {
    const inherited=!account.routing_mode || account.routing_mode==="inherit";
    const live=state.policy?.active?.settings?.routing?.objectives;
    return (live?.enabled?labels[inherited?live.mode:account.routing_mode]:"原有规则")
      +" · "+(inherited?"继承全局":"账号指定")+((account.local_only || live?.local_only)?" · 仅本地":"");
  }
  function audit(diagnosis) {
    let panel=$("routing-audit-evidence");
    if (!panel) {
      panel=document.createElement("div");panel.id="routing-audit-evidence";panel.className="routing-evidence";
      $("trace-diagnosis-verdict").after(panel);
    }
    panel.replaceChildren();
    const mode=diagnosis?.routing_objective || {}, observation=diagnosis?.performance_observation || {};
    const count=diagnosis?.token_counting?.selected;
    const rows=[];
    if(mode.enabled) rows.push(["生效策略",(labels[mode.mode] || mode.mode)+" · "+(mode.source==="account"?"账号指定":"继承全局")+(mode.observe_only?" · 仅观察":"")]);
    if(count) rows.push(["输入计数",count.tokens.toLocaleString()+" Token · "+(count.exact?"目标后端计数":"估算")]);
    if(mode.previous_wait_seconds!=null) rows.push(["上一轮等待",mode.previous_wait_seconds.toFixed(1)+" 秒 · "+(mode.wait_source==="backend_prefill"?"后端 prefill":"上游首个有效输出")]);
    if(mode.recent_decode_tps?.length) rows.push(["近期生成速度",mode.recent_decode_tps.map(x=>x.toFixed(1)).join(" / ")+" Token/秒"]);
    if(mode.baseline_decode_tps!=null) rows.push(["近期速度基线",mode.baseline_decode_tps.toFixed(1)+" Token/秒"]);
    if(mode.target_performance==="unverified") rows.push(["目标性能","尚无足够样本，切换后继续观察"]);
    if(mode.retained_reason) rows.push(["保持原因",mode.retained_reason==="no_eligible_alternative"?"没有合格替代模型":"样本或迁移收益不足"]);
    if(mode.sample_counts) rows.push(["性能样本",Object.entries(mode.sample_counts).map(([id,n])=>(names[id] || id)+"："+n).join("；")]);
    if(Object.keys(mode.estimated_seconds || {}).length) rows.push(["预计耗时",Object.entries(mode.estimated_seconds).map(([id,s])=>(names[id] || id)+"："+s.toFixed(1)+" 秒").join("；")+"（目标按冷缓存估算）"]);
    if(observation.first_output_seconds!=null) rows.push(["本轮首个有效输出",observation.first_output_seconds.toFixed(1)+" 秒"]);
    if(observation.first_text_seconds!=null) rows.push(["本轮首段正文",observation.first_text_seconds.toFixed(1)+" 秒"]);
    if(observation.decode_tps!=null) rows.push(["本轮生成速度",observation.decode_tps.toFixed(1)+" Token/秒"]);
    if(observation.cache) rows.push(["本轮缓存证据",{warm:"上游用量确认命中",cold:"上游用量确认未命中",unknown:"上游未提供可靠缓存用量"}[observation.cache] || "未知"]);
    for (const [title,detail] of rows) {
      const row=document.createElement("p"),label=document.createElement("strong"),text=document.createElement("span");
      label.textContent=title+"：";text.textContent=detail;row.append(label,text);panel.append(row);
    }
    panel.hidden=!rows.length;
  }
  $("routing-mode-view").addEventListener("change",event=>{
    $("settings-view").dataset.policyView=event.target.value;
  });
  for (const input of document.querySelectorAll('[name="routing-objective"],#routing-local-only')) input.addEventListener("change",summary);
  $("routing-apply").addEventListener("click",()=>void apply());
  $("routing-open-clients").addEventListener("click",()=>void switchView("clients"));
  return {render,collect,summary,accountLabel,audit,defaults};
})();
