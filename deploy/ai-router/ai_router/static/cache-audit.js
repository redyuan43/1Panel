/* Shared audit navigation; no prompt content in polling responses or storage. */
const cacheView = {view: "conversation", stage: null, request: null, offset: 0, next: null, sequence: 0, loading: false, rows: new Map(), contentSequence: 0};
const cacheStageNames = {workbuddy_history_preserved:"历史位置保全后",tools_stabilized:"工具序列化稳定化",normalized:"协议规范化",received:"入口摘要（正文未归档）",after_directives:"指令清理后",workbuddy_reordered:"WorkBuddy 重排后",effective:"有效上下文",legacy_after_directives:"旧归档：指令清理后"};
const cacheCheckNames = {workbuddy_history:"历史前缀保全",tool_serialization_stability:"工具集合与参数保全",workbuddy_reorder:"WorkBuddy 重排",message_order_and_tool_history:"消息顺序与工具历史",user_content_and_images:"用户内容与图片",tool_definitions_and_parameters:"工具定义与参数",dynamic_tool_content_once:"工具动态说明保全",workspace_memory_and_stable_content:"工作区记忆与稳定内容",single_dynamic_block:"动态内容未重复插入"};
const cacheEventNames = {hot:"内存状态可用",disk:"磁盘恢复",miss_saved:"计算后保存",miss_memory_only:"计算后保留内存",bypass:"未使用快照"};
const cacheStatusNames = {hit:"已命中",miss:"未命中",estimated:"仅有估算",unknown:"数据不足",running:"执行中"};
function cacheBrief(m) {
  if(!m)return "数据不足";
  const label=cacheStatusNames[m.cache_status]||"数据不足";
  if(m.backend_cached_tokens!=null)return `${label} · 复用 ${cacheFormat(m.backend_cached_tokens,"tokens")}${m.backend_reuse_ratio!=null?` / ${cacheFormat(m.backend_reuse_ratio,"ratio")}`:""}${m.prime_tokens>0?" · 含准备阶段重算":""}`;
  if(m.estimated_cached_tokens!=null)return `${label} · ${cacheFormat(m.estimated_cached_tokens,"tokens")}${m.estimated_reuse_ratio!=null?` / ${cacheFormat(m.estimated_reuse_ratio,"ratio")}`:""}`;
  return label;
}
function cacheDisclosure(key,title,body,defaultOpen=false) {
  cacheView.disclosures ||= new Map();
  const open=cacheView.disclosures.has(key)?cacheView.disclosures.get(key):defaultOpen;
  return `<details class="audit-fold" data-cache-detail="${escapeHtml(key)}" ${open?"open":""}><summary>${escapeHtml(title)}</summary>${body}</details>`;
}
const cacheFormat = (value, unit="ms") => value == null ? "后端未提供" : unit === "ratio" ? `${(value*100).toFixed(1)}%` : unit === "ms" ? `${(value/1000).toFixed(2)} 秒` : `${Number(value).toLocaleString(undefined,{maximumFractionDigits:1})}${unit === "tokens" ? " tokens" : unit}`;
const cacheCell = (label,value,unit="ms") => `<div class="cache-value"><span>${escapeHtml(label)}</span><strong>${escapeHtml(cacheFormat(value,unit))}</strong></div>`;

function setAuditSubview(view) {
  cacheView.view = view;
  byId("cache-overview").hidden = view !== "overview";
  byId("audit-conversation").hidden = view !== "conversation";
  document.querySelectorAll("[data-audit-view]").forEach(b => b.classList.toggle("active",b.dataset.auditView===view));
  if (view === "overview") void loadCacheOverview();
  else if (!state.routeTraces.length) void loadRouteAudit();
}

function cacheQuery() {
  const p = new URLSearchParams({since: String(Date.now()/1000-Number(byId("cache-hours").value)*3600)});
  for (const [id,key] of [["cache-client","client_id"],["cache-device","device"],["cache-model","model"],["cache-conversation","conversation_id"],["cache-status","status"],["cache-event","event"]]) {
    const value=byId(id).value.trim(); if(value) p.set(key,value);
  }
  if(byId("cache-client-group").value&&!byId("cache-client").value.trim())p.set("client_group",byId("cache-client-group").value);
  return p;
}

async function loadCacheOverview({force=false}={}) {
  if (!state.key) return;
  if (cacheView.loading) {
    if(force) {cacheView.sequence++;cacheView.pendingRefresh=true;}
    return;
  }
  const seq=++cacheView.sequence;
  cacheView.loading=true;
  const params=cacheQuery();
  try {
    const [summary,page]=await Promise.all([api(`/api/cache/summary?${params}`),api(`/api/cache/requests?${params}&offset=${cacheView.offset}`)]);
    if(seq!==cacheView.sequence) return;
    cacheView.next=page.next_offset;
    cacheView.rows=new Map(page.items.map(x=>[x.request_id,x]));
    const units={backend_reuse_ratio:"ratio",net_cache_ratio:"ratio",fixed_reuse_ratio:"ratio",prefill_tps:" tokens/s"};
    const labels={backend_reuse_ratio:"后端逐请求复用率",ttft_ms:"首个输出等待",first_text_ms:"首段正文等待",queue_ms:"Router 排队",prefill_ms:"正式 prefill",restore_ms:"磁盘恢复",net_cache_ratio:"净 token 复用率",fixed_reuse_ratio:"固定前缀复用率",prefill_tps:"Prefill 速度"};
    byId("cache-metrics").innerHTML=Object.entries(labels).map(([k,label])=>{
      const v=summary.metrics[k]||{n:0,median:null,p95:null};return `<article class="cache-metric"><span>${label}</span><strong>${escapeHtml(cacheFormat(v.median,units[k]||"ms"))}</strong><small>中位数 · P95 ${escapeHtml(cacheFormat(v.p95,units[k]||"ms"))} · ${v.n} 个有效样本</small></article>`;
    }).join("");
    byId("cache-overview-state").textContent=`${summary.total} 条请求 · ${summary.succeeded} 条成功 · 固定前缀达标 ${summary.fixed_pass.passed}/${summary.fixed_pass.n} 个有效样本${summary.truncated?" · 已达 10,000 条分析上限，请缩短时间范围":""}`;
    byId("cache-trend").innerHTML=cacheTrend(summary.trend);
    byId("cache-request-rows").innerHTML=page.items.map(x=>`<tr><td><button class="text-button" data-cache-request="${escapeHtml(x.request_id)}">${escapeHtml(x.request_id.slice(0,12))}</button><small>${formatTime(x.started_at)}</small></td><td>${escapeHtml(x.device||"未确定")}<small>${escapeHtml(x.model||"")}</small></td><td>${escapeHtml(statusLabels[x.status]||x.status)}<small>${escapeHtml(x.request_kind||"")}</small></td><td>${escapeHtml(cacheBrief(x))}<small>${escapeHtml(x.cache_measurement==="measured"?"逐请求实测":x.cache_measurement==="estimated"?"全局差值估算":"缺少完整计数")}</small></td><td>${escapeHtml(cacheFormat(x.total_prefill_tokens,"tokens"))}</td><td>${escapeHtml(cacheFormat(x.queue_ms))}</td><td>${escapeHtml(cacheFormat(x.ttft_ms))}</td></tr>`).join("")||'<tr><td colspan="7" class="empty">此筛选下没有请求</td></tr>';
    byId("cache-prev").disabled=cacheView.offset===0;
    byId("cache-next").disabled=page.next_offset==null;
    byId("cache-page-state").textContent=`${cacheView.offset+1}–${cacheView.offset+page.items.length} / ${page.total}`;
    byId("cache-request-rows").querySelectorAll("[data-cache-request]").forEach(b=>b.addEventListener("click",()=>{setAuditSubview("conversation"); void selectRouteTrace(b.dataset.cacheRequest);}));
  } catch(e) {if(seq===cacheView.sequence)byId("cache-overview-state").textContent=e.message;}
  finally {
    cacheView.loading=false;
    if(cacheView.pendingRefresh) {cacheView.pendingRefresh=false;void loadCacheOverview();}
  }
}

function cacheTrend(rows) {
  if(!rows.length) return '<p class="empty">暂无趋势样本</p>';
  const keys=[["backend_reuse_ratio","后端逐请求复用"],["ttft_ms","首个输出"],["queue_ms","排队"],["prefill_ms","Prefill"],["fixed_reuse_ratio","固定前缀复用"]];
  return keys.map(([key,label])=>{
    const values=rows.map(r=>r[key]?.median??null); const max=Math.max(...values.filter(v=>v!=null),1);
    const points=values.map((v,i)=>v==null?null:[20+i*320/Math.max(1,rows.length-1),65-v/max*50]);
    let segments=[], part=[]; points.forEach(p=>{if(p)part.push(p.join(","));else if(part.length){segments.push(part.join(" "));part=[];}});if(part.length)segments.push(part.join(" "));
    return `<article class="cache-trend-item"><strong>${label}</strong><svg viewBox="0 0 360 85" role="img" aria-label="${label}每小时中位数">${segments.map(p=>`<polyline points="${p}" fill="none" stroke="var(--cyan)" stroke-width="2"/>`).join("")}${points.map((p,i)=>p?`<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="var(--cyan)"><title>${escapeHtml(formatTime(rows[i].at))} · ${escapeHtml(cacheFormat(values[i],key.endsWith("ratio")?"ratio":"ms"))} · ${rows[i][key]?.n??0} 样本</title></circle>`:"").join("")}</svg><small>${formatTime(rows[0].at)} — ${formatTime(rows.at(-1).at)}</small></article>`;
  }).join("");
}

function renderUnifiedAudit() {
  const trace=state.selectedTrace; if(!trace)return;
  if(cacheView.request!==trace.request_id){cacheView.request=trace.request_id;cacheView.stage=null;cacheView.disclosures=new Map();cacheView.contentSequence++;byId("cache-content").replaceChildren();}
  const m=trace.cache_audit?.request||{};
  const terminal=["succeeded","failed","interrupted"].includes(trace.status);
  const labels=[["received","接收",null],["content","内容整理",null],["routing","路由选择",null],["queue","排队",m.queue_ms],["execution","模型执行",m.prefill_ms],["completed","完成",m.total_ms]];
  byId("audit-pipeline").innerHTML=labels.map(([id,label,duration],i)=>{
    let status="已记录";
    if(id==="content") status=trace.observation?.content?"可查看转换":"阶段未采集";
    if(id==="routing") status=trace.route_selected?"已选择设备":trace.task?"查看判断":"未进入";
    if(id==="queue") status=m.queue_ms==null?"未采集":cacheFormat(m.queue_ms);
    if(id==="execution") status=cacheBrief(m);
    if(id==="completed") status=terminal?(statusLabels[trace.status]||trace.status):"等待完成";
    return `<button type="button" class="pipeline-node ${cacheView.stage===id?"selected":""}" data-pipeline-stage="${id}" aria-expanded="${cacheView.stage===id}"><span class="pipeline-index">0${i+1}</span><strong>${label}</strong><small>${escapeHtml(status)}</small>${duration!=null&&id!=="queue"?`<small>${escapeHtml(cacheFormat(duration))}</small>`:""}</button>`;
  }).join("");
  byId("audit-pipeline").querySelectorAll("[data-pipeline-stage]").forEach(b=>b.addEventListener("click",()=>selectPipelineStage(b.dataset.pipelineStage)));
  byId("audit-routing-detail").hidden=cacheView.stage!=="routing";
  byId("local-pool-audit").innerHTML=localPoolHtml(trace.local_pool);
  byId("audit-stage-detail").hidden=!cacheView.stage||cacheView.stage==="routing";
  byId("cache-content").hidden=cacheView.stage!=="content";
  if(cacheView.stage&&cacheView.stage!=="routing")renderPipelineDetail();
}

async function selectPipelineStage(stage) {
  cacheView.stage=cacheView.stage===stage?null:stage;
  renderUnifiedAudit();
  if(cacheView.stage==="routing"&&traceEnteredRouting(state.selectedTrace)) {await ensureTraceGraphRendered();fitTraceGraph(false);}
}

function prefixBreakHtml(audit) {
  const row=audit.prefix_breaks?.at(-1);
  if(!row)return '<p class="section-meta">Prefix Break：暂无后台诊断。历史请求缺少记录时不推算。</p>';
  const label=row.state==="compared"?(row.prefix_preserved?"历史前缀保留":"检测到前缀分叉"):row.state==="baseline"?"首条基线":row.state==="structural"?"仅结构对比":row.reason==="raw_history_association_unconfirmed"?"关联不确定":"诊断不可用";
  const stages=(row.stages||[]).map(s=>`<p>${escapeHtml(cacheStageNames[s.stage]||s.stage)} · ${s.state==="missing"?"阶段缺失":s.state==="same"?"相同":escapeHtml((s.fields||[]).join("；"))}</p>`).join("");
  const tools=row.tools?`<p>Tools：${Number(row.tools.previous_count)} → ${Number(row.tools.current_count)}；新增 ${Number(row.tools.added_count)}，移除 ${Number(row.tools.removed_count)}。不保留已删除工具。</p>`:"";
  return `<section class="cache-prefix-break"><strong>Prefix Break · ${label}</strong><p class="section-meta">${row.previous_request_id?`对比请求 ${escapeHtml(row.previous_request_id)}${row.association==="verified_raw_prefix"?"（原始内容前缀匹配）":""}`:""} · 执行尝试 ${Number(row.attempt)}</p>${row.state==="compared"?`<div class="cache-value-grid">${cacheCell("共同前缀",row.common_tokens,"tokens")}${cacheCell("首次不同 token 位置（从 0 计数）",row.first_different_token,"tokens")}</div><p>关联字段：${escapeHtml(row.cause_field||(row.prefix_preserved?"正常追加内容":"未确定"))} · ${row.attribution==="native_tokenize_counterfactual"?"替换 Tools 后分叉位置后移":row.attribution==="structural_candidate"?"结构候选，非精确 token 映射":""}</p>`:`<p>${escapeHtml(row.reason==="raw_history_association_unconfirmed"?"未找到可确认的原始历史前缀，不按新会话或未命中下结论":row.reason||"")}</p>`}<p class="section-meta">后台使用原生模板只分词重建，不调用模型推理；位置不等于实际缓存恢复边界。模型版本或模板变化会影响重建结果。</p>${cacheDisclosure("prefix-break","各阶段变化字段",stages+tools)}</section>`;
}

function renderPipelineDetail() {
  const trace=state.selectedTrace, audit=trace.cache_audit||{}, m=audit.request||{}, stage=cacheView.stage;
  const target=byId("audit-stage-values");
  if(stage==="execution") {
    const measured=m.cache_measurement==="measured", estimated=m.cache_measurement==="estimated";
    const source=measured?(m.backend_usage_source==="gateway_native"?"网关原生实测":"后端逐请求 usage 实测"):estimated?"全局计数差值估算":"无完整逐请求计数";
    const cached=measured?m.backend_cached_tokens:estimated?m.estimated_cached_tokens:null;
    const ratio=measured?m.backend_reuse_ratio:estimated?m.estimated_reuse_ratio:null;
    const input=m.backend_input_tokens??m.input_tokens;
    const inputSource=m.input_measurement==="measured"?"实测":m.input_measurement==="estimated"?"估算":"未知";
    const native=`<p class="section-meta">${audit.operations?.length?"包含准备阶段的计算量单独核算。":"该后端尚未提供固定边界、准备、恢复与原生 prefill 阶段遥测。"}</p><div class="cache-value-grid">${cacheCell("固定前缀复用",m.fixed_reuse_ratio,"ratio")}${cacheCell("净 token 复用",m.net_cache_ratio,"ratio")}${cacheCell("准备阶段重算",m.prime_tokens,"tokens")}${cacheCell("正式请求重算",m.prompt_tokens,"tokens")}${cacheCell("本次尝试总 prefill",m.total_prefill_tokens,"tokens")}${cacheCell("所有尝试总 prefill",m.all_attempts_prefill_tokens,"tokens")}${cacheCell("动态尾部",m.dynamic_tokens,"tokens")}${cacheCell("正式 prefill",m.prefill_ms)}${cacheCell("Prefill 速度",m.prefill_tps," tokens/s")}${cacheCell("网关首个输出",m.gateway_ttft_ms)}</div>`;
    const operations=(audit.operations||[]).map(o=>cacheDisclosure(o.operation_id,`${o.kind==="prewarm"?"后台预热（独立操作）":"前台执行"} · 尝试 ${Number(o.attempt)} · ${o.deployment_id||""} · ${o.status||""}`,`<div class="cache-value-grid">${cacheCell("模板与运行版本检查",o.cache?.template_ms)}${cacheCell("磁盘校验及恢复",o.cache?.restore_ms)}${cacheCell("预热计算",o.cache?.prime_ms)}${cacheCell("保存",o.cache?.save_ms)}${cacheCell("准备阶段合计",o.cache?.seconds==null?null:o.cache.seconds*1000)}${cacheCell("网关等待",o.queue_ms)}</div><p class="section-meta">操作 ${escapeHtml(o.operation_id)} · ${escapeHtml(o.error||o.cache?.restore_error||"")}</p>`)).join("");
    target.innerHTML=`<div class="cache-verdict ${measured?"measured":estimated?"estimated":"unknown"}"><strong>${escapeHtml(cacheBrief(m))}</strong><p>${escapeHtml(m.cache_reason||"后端未提供完整逐请求计数")} · ${escapeHtml(source)}</p></div><div class="cache-value-grid">${cacheCell(`总输入 · ${inputSource}`,input,"tokens")}${cacheCell(`缓存复用 · ${measured?"实测":estimated?"估算":"未知"}`,cached,"tokens")}${cacheCell(`后端复用比例 · ${measured?"实测":estimated?"估算":"未知"}`,ratio,"ratio")}${cacheCell("未复用输入 · 总输入减复用",m.uncached_input_tokens,"tokens")}${cacheCell("Router 首个输出 · 实测",m.ttft_ms)}${cacheCell("Router 首段正文 · 实测",m.first_text_ms)}${cacheCell("Router 排队 · 实测",m.queue_ms)}${cacheCell("总耗时 · 实测",m.total_ms)}</div><p class="section-meta">未复用输入仅表示本次输入中尚需计算的 token，不包含准备、重试或抢占带来的全部重算。后端复用汇总本地及外部缓存，不区分 GPU 与 LMCache。首个输出等待包括入口处理和排队，不能作为 prefill 耗时；非流式请求没有首个输出计时。</p>${m.prime_tokens>0?`<p class="section-meta">本次准备阶段还重算了 ${escapeHtml(cacheFormat(m.prime_tokens,"tokens"))}，扣除后的净复用为 ${escapeHtml(cacheFormat(m.net_cache_ratio,"ratio"))}。</p>`:""}${prefixBreakHtml(audit)}${cacheDisclosure("native","固定前缀、准备与 Prefill 详情",native,m.measurement==="measured")}${operations}`;
    target.querySelectorAll("details[data-cache-detail]").forEach(el=>el.addEventListener("toggle",()=>cacheView.disclosures.set(el.dataset.cacheDetail,el.open)));

  } else if(stage==="queue")target.innerHTML=`<div class="cache-value-grid">${cacheCell("Router 排队",m.queue_ms)}${cacheCell("网关排队",m.gateway_queue_ms)}</div>${localPoolHtml(trace.local_pool)}`;
  else if(stage==="completed")target.innerHTML=`<div class="cache-value-grid">${cacheCell("请求总耗时",m.total_ms)}${cacheCell("输入",m.input_tokens,"tokens")}${cacheCell("输出",m.output_tokens,"tokens")}</div><p>${escapeHtml(trace.error?.message||statusLabels[trace.status]||trace.status)}</p>`;
  else if(stage==="received")target.innerHTML=`<p>请求 ${escapeHtml(trace.request_id)}</p><p>客户端 ${escapeHtml(trace.client_id)} · ${escapeHtml(trace.protocol)} · ${formatTime(trace.started_at)}</p>`;
  else if(stage==="content") {
    const content=trace.observation?.content;
    target.innerHTML=`${prefixBreakHtml(audit)}<p class="section-meta">规则检查与人工原文对比。转换、恢复历史和协议适配分别保留阶段；规则通过不能代替语义审核。</p><div class="cache-checks">${(content?.checks||[]).map(c=>`<span class="badge ${c.status==="passed"?"success":c.status==="failed"?"danger":"neutral"}">${escapeHtml(cacheCheckNames[c.check]||c.check)} · ${c.status==="passed"?"通过":c.status==="failed"?"异常":"跳过 / 证据不足"}</span>`).join("")||"此请求尚无转换检查记录"}</div><div class="cache-stage-hashes">${(content?.stages||[]).map(s=>`<div>${escapeHtml(cacheStageNames[s.stage]||s.stage)} · ${s.bytes} bytes · <code>${escapeHtml(s.sha256.slice(0,16))}</code></div>`).join("")}</div><button type="button" class="secondary" id="cache-load-content">按需查看原文与差异</button>`;
    target.querySelectorAll("details[data-cache-detail]").forEach(el=>el.addEventListener("toggle",()=>cacheView.disclosures.set(el.dataset.cacheDetail,el.open)));
    byId("cache-load-content").addEventListener("click",()=>void loadContentInspector());
  }
}

async function loadContentInspector() {
  const rid=state.selectedTraceId, seq=++cacheView.contentSequence, container=byId("cache-content");
  container.textContent="正在读取加密归档…";
  try {
    const meta=await api(`/api/route-traces/${encodeURIComponent(rid)}/content`);
    if(seq!==cacheView.contentSequence||rid!==state.selectedTraceId)return;
    const options=meta.stages.filter(s=>s.archived!==false).map(s=>`<option value="${escapeHtml(s.stage)}">${escapeHtml(cacheStageNames[s.stage]||s.stage)}</option>`).join("");
    const turns=state.requestConversationPages.get(state.selectedTrace.conversation_id)?.items||[];
    const previous=turns.filter(x=>x.request_id!==rid&&x.started_at<state.selectedTrace.started_at).sort((a,b)=>b.started_at-a.started_at)[0];
    container.innerHTML=`<p class="section-meta">${meta.legacy?"旧归档缺少入口原文或中间阶段，以下仅展示实际保存内容。":"完整内容仅在本次查看时加载。"} 每次加载最多 16,384 字符，差异只针对当前已加载范围。</p><div class="cache-diff-controls"><label>左侧来源<select id="cache-compare-request"><option value="${escapeHtml(rid)}">当前请求</option>${previous?`<option value="${escapeHtml(previous.request_id)}">同会话上一条请求</option>`:""}</select></label><label>左侧阶段<select id="cache-stage-left">${options}</select></label><label>右侧阶段<select id="cache-stage-right">${options}</select></label><button class="secondary" id="cache-compare">查看对比</button></div><p id="cache-diff-state" class="section-meta"></p><div class="cache-diff-grid"><section><strong>左侧原文</strong><pre id="cache-text-left"></pre><button class="secondary" id="cache-more-left" hidden>加载下一段</button></section><section><strong>右侧原文</strong><pre id="cache-text-right"></pre><button class="secondary" id="cache-more-right" hidden>加载下一段</button></section></div><details class="audit-fold"><summary>当前范围的差异片段</summary><pre id="cache-diff-text"></pre></details>`;
    byId("cache-stage-right").value=meta.stages.at(-1)?.stage||"";
    let leftMeta=meta, loaded={}, comparison=0, loadingSides=new Set();
    function invalidateComparison() {
      comparison++; loaded={};
      for(const side of ["left","right"]) {byId("cache-more-"+side).hidden=true;byId("cache-text-"+side).textContent="";}
      byId("cache-diff-text").textContent="";
      byId("cache-diff-state").textContent="选择已变化，请点击查看对比。";
    }
    for(const side of ["left","right"])byId("cache-stage-"+side).addEventListener("change",invalidateComparison);
    byId("cache-compare-request").addEventListener("change",async()=>{
      invalidateComparison();
      const id=byId("cache-compare-request").value;
      byId("cache-compare").disabled=true;
      try{leftMeta=await api(`/api/route-traces/${encodeURIComponent(id)}/content`);if(seq!==cacheView.contentSequence||byId("cache-compare-request").value!==id)return;byId("cache-stage-left").innerHTML=leftMeta.stages.filter(s=>s.archived!==false).map(s=>`<option value="${escapeHtml(s.stage)}">${escapeHtml(cacheStageNames[s.stage]||s.stage)}</option>`).join("");}catch(e){if(seq===cacheView.contentSequence)byId("cache-diff-state").textContent=e.message;}finally{if(seq===cacheView.contentSequence&&byId("cache-compare-request").value===id)byId("cache-compare").disabled=false;}
    });
    async function loadSide(side, append=false, version=comparison) {
      if(append&&(!loaded[side]||loaded[side].next_offset==null))return;
      const flight=`${version}:${side}`; if(loadingSides.has(flight))return;
      loadingSides.add(flight);
      const id=append?loaded[side].request_id:(side==="left"?byId("cache-compare-request").value:rid);
      const stage=append?loaded[side].stage:byId("cache-stage-"+side).value;
      const offset=append?loaded[side].next_offset:0;
      try {
      const value=await api(`/api/route-traces/${encodeURIComponent(id)}/content?${new URLSearchParams({stage,offset:String(offset)})}`);
      if(seq!==cacheView.contentSequence||version!==comparison||rid!==state.selectedTraceId)return;
      if(append&&loaded[side]?.next_offset!==offset)return;
      value.text=(append?loaded[side]?.text||"":"")+value.text;loaded[side]=value;
      byId("cache-text-"+side).textContent=value.text;
      byId("cache-more-"+side).hidden=value.next_offset==null;
      if(loaded.left&&loaded.right)renderContentDifference(loaded.left,loaded.right);
      } finally {loadingSides.delete(flight);}
    }
    byId("cache-compare").addEventListener("click",async()=>{
      const version=++comparison; loaded={};
      try {await Promise.all([loadSide("left",false,version),loadSide("right",false,version)]);}catch(e){if(seq===cacheView.contentSequence)byId("cache-diff-state").textContent=e.message;}
    });
    for(const side of ["left","right"])byId("cache-more-"+side).addEventListener("click",()=>void loadSide(side,true).catch(e=>{if(seq===cacheView.contentSequence)byId("cache-diff-state").textContent=e.message;}));
  } catch(e){if(seq===cacheView.contentSequence)container.textContent=e.message;}
}

function renderContentDifference(left,right) {
  const a=left.text,b=right.text;let start=0,end=0;
  while(start<Math.min(a.length,b.length)&&a[start]===b[start])start++;
  while(end<Math.min(a.length,b.length)-start&&a[a.length-1-end]===b[b.length-1-end])end++;
  const complete=left.next_offset==null&&right.next_offset==null;
  byId("cache-diff-state").textContent=`左 ${a.length}/${left.total_chars} 字符 · 右 ${b.length}/${right.total_chars} 字符 · ${a===b?(complete?"完整内容一致":"已加载范围一致，剩余内容未检查"):`首个文本差异位于第 ${start+1} 个字符（不是 token 分叉位置）`}`;
  byId("cache-diff-text").textContent=a===b?"当前范围无差异":`移出 / 修改前：\n${a.slice(start,a.length-end)}\n\n移入 / 修改后：\n${b.slice(start,b.length-end)}`;
}

function initializeCacheAudit() {
  document.querySelectorAll("[data-audit-view]").forEach(b=>b.addEventListener("click",()=>setAuditSubview(b.dataset.auditView)));
  byId("cache-refresh").addEventListener("click",()=>{cacheView.offset=0;void loadCacheOverview({force:true});});
  byId("cache-prev").addEventListener("click",()=>{cacheView.offset=Math.max(0,cacheView.offset-50);void loadCacheOverview({force:true});});
  byId("cache-next").addEventListener("click",()=>{if(cacheView.next!=null){cacheView.offset=cacheView.next;void loadCacheOverview({force:true});}});
  byId("cache-filter-form").addEventListener("submit",e=>{e.preventDefault();cacheView.offset=0;void loadCacheOverview({force:true});});
}

function localPoolHtml(pool) {
  if (!pool) return "";
  const labels={allocation_lock_busy:"分配锁繁忙，按既有容量机制选型；未保证分散",spread_new_conversations:"分散分配新会话",estimated_faster_first_output:"预计更快的首个输出",insufficient_cost_evidence_keep_affinity:"估算证据不足，保留原设备",original_device_available_keep_affinity:"原设备可用，保持会话亲和",migration_not_materially_faster:"异机收益不足，保持会话亲和",insufficient_matching_samples:"匹配样本不足",unknown_remaining_service_time:"剩余执行时间未知",capacity_timeout_cold_fallback:"等待达到上限，按容量回退；异机缓存未保证"};
  const candidates=(pool.candidates||[]).map(c=>`<tr><td>${escapeHtml(c.endpoint_id)}</td><td>${c.available?"有容量":"占用中"}</td><td>${Number(c.running)} / ${Number(c.capacity)}</td><td>${Number(c.recent_conversations)} / ${Number(c.capacity)}</td></tr>`).join("");
  const costs=(pool.costs||[]).map(c=>`<tr><td>${escapeHtml(c.endpoint_id)}</td><td>${c.cache_assumption==="hot"?"近期同会话高复用（估算假设）":"冷计算（保守估算）"}</td><td>${Number(c.sample_count)}</td><td>${c.queue_s==null?"未知":escapeHtml(cacheFormat(c.queue_s*1000))}</td><td>${c.total_s==null?escapeHtml(labels[c.unavailable_reason]||"未知"):escapeHtml(cacheFormat(c.total_s*1000))}</td></tr>`).join("");
  return `<section class="cache-prefix-break"><strong>本地候选组 · AMD / AI / Edge</strong><p>${escapeHtml(labels[pool.selection]||labels[pool.wait_reason]||"按实际容量选择设备")}${pool.target?` → ${escapeHtml(pool.target)}`:""}</p>${pool.estimated_saving_s!=null?`<p>预计减少等待 ${escapeHtml(cacheFormat(pool.estimated_saving_s*1000))}（历史估算）</p>`:""}${candidates?`<div class="table-wrap"><table><thead><tr><th>候选设备</th><th>实际容量</th><th>在途 / 并发</th><th>近期会话 / 并发</th></tr></thead><tbody>${candidates}</tbody></table></div>`:""}${costs?`<div class="table-wrap"><table><thead><tr><th>设备</th><th>计算成本假设</th><th>有效样本</th><th>预计排队</th><th>预计首个输出总等待</th></tr></thead><tbody>${costs}</tbody></table></div>`:""}<p class="section-meta">容量不满足的端点及原始模型评分见下方完整路由判断。预计总等待包含排队与输入准备；路由亲和和估算均不能证明实际缓存命中。</p></section>`;
}
