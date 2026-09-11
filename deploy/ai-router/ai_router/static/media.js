"use strict";
const $ = id => document.getElementById(id);
const state = ViewPreferences.state("media", {key: sessionStorage.getItem("ai-router-admin-key") || "", view: "images",
  selected: null, sequence: 0, settings: null, mediaOptions: null, videoCapabilities: {},
  pages: {images: [], videos: []}, cursors: {}, drafts: new Map()}, {view:["images","videos","settings"]});
const labels = {queued:"排队中",running:"执行中",in_progress:"执行中",archiving:"归档中",awaiting_approval:"待批准",
  approved:"已批准",completed:"已完成",failed:"失败",cancelled:"已取消",cancelling:"取消中",reconciling:"核对原任务",pending:"待启动"};
const stageLabels = {context_ir:"Context IR",preview:"预览",proof:"质量验证",local_768:"本地 768P",cloud_768:"云端 768P",regenerate_2k:"2K"};
const workflowLabels = {quality_gate:"方案 → 预览 → 成片",duration_ladder:"约 5 秒 → 约 10 秒 → 约 15 秒",legacy_pipeline:"旧版阶段流程"};
const aspectLabels = {"9:16":"9:16 竖屏","16:9":"16:9 横屏",auto:"自动"};
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function notice(text="", error=false){$("notice").textContent=text;$("notice").classList.toggle("error",error);}
function own(value,key){return Boolean(value)&&Object.prototype.hasOwnProperty.call(value,key);}
function optionValues(value){
  if(Array.isArray(value))return value.filter(item=>typeof item==="string"&&item);
  if(value&&typeof value==="object"){
    for(const key of ["values","options","allowed"]){
      if(Array.isArray(value[key]))return value[key].filter(item=>typeof item==="string"&&item);
    }
  }
  return [];
}
function optionDefault(videos,name,fallback){
  const field=videos?.[name];
  return field&&typeof field==="object"&&!Array.isArray(field)&&typeof field.default==="string"?field.default:
    videos?.defaults?.[name]??videos?.["default_"+name]??fallback;
}
function fillSelect(select,values,preferred,labeler=value=>value){
  const available=[...new Set(values)];
  select.innerHTML=available.map(value=>`<option value="${escapeHtml(value)}">${escapeHtml(labeler(value))}</option>`).join("");
  if(available.includes(preferred))select.value=preferred;
}
function configureVideoOptions(options){
  const videos=options?.videos||{},form=$("video-form");
  state.mediaOptions=options;
  state.videoCapabilities={};
  for(const name of ["workflow_mode","creative_profile","aspect_ratio"]){
    const supported=own(videos,name);
    state.videoCapabilities[name]=supported;
    const select=form.elements[name];
    select.disabled=!supported;
    if(!supported)continue;
    const values=optionValues(videos[name]);
    const fallback=name==="workflow_mode"?"quality_gate":select.value;
    const preferred=optionDefault(videos,name,fallback);
    if(values.length)fillSelect(select,values,preferred,name==="workflow_mode"?
      value=>workflowLabels[value]||value:name==="aspect_ratio"?value=>aspectLabels[value]||value:value=>value);
  }
  const modern=state.videoCapabilities.workflow_mode;
  if(!modern){
    fillSelect(form.elements.workflow_mode,[],"");
    form.elements.workflow_mode.disabled=true;
  }
  form.querySelector('button[type="submit"]').disabled=!modern;
  const compatibility=$("video-compat");
  compatibility.hidden=modern;
  compatibility.textContent=modern?"":"当前服务器未发布可用的工作流能力，暂不能创建视频；请检查媒体服务。";
}
async function api(path, options={}){
  const headers = {Authorization:`Bearer ${state.key}`, ...options.headers};
  if(options.body && !(options.body instanceof FormData)) headers["Content-Type"]="application/json";
  const response=await fetch(path,{...options,headers});
  const value=await response.json();
  if(!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
  return value;
}
async function connect(){
  state.key=$("key").value;sessionStorage.setItem("ai-router-admin-key",state.key);
  try{const options=await api("/api/media/options");notice(options.enabled?"":"媒体生成未启用");configureVideoOptions(options);
    for(const field of ["mode","strategy","audio_policy"]){
      const allowed=options.videos?.[field];
      if(Array.isArray(allowed))for(const option of [...$("video-form").elements[field].options])if(!allowed.includes(option.value))option.remove();
    }
    $("image-form").elements.operation.onchange?.();
    updateAssets();
    await loadPage("images");await loadPage("videos");await loadSettings();
  }catch(error){notice(error.message,true);}
}
async function loadPage(kind, more=false){
  const page=await api(`/api/media/${kind}${more && state.cursors[kind] ? "?after="+encodeURIComponent(state.cursors[kind]):""}`);
  state.pages[kind]=more?[...state.pages[kind],...page.data]:page.data;
  state.cursors[kind]=page.next_cursor;
  const prefix=kind==="images"?"image":"video";
  $(prefix+"-more").hidden=!page.next_cursor;
  $(prefix+"-list").innerHTML=state.pages[kind].map(job=>`
    <button class="task ${state.selected?.id===job.id?"selected":""}" data-id="${escapeHtml(job.id)}">
      ${kind==="images"&&job.output?`<img src="${escapeHtml(job.output.content_url)}" alt="生成图片">`:'<span class="placeholder">'+(kind==="images"?"IMG":"VIDEO")+"</span>"}
      <span class="body"><code>${escapeHtml(job.id)}</code><time>${new Date(job.created_at*1000).toLocaleString("zh-CN")}</time></span>
      <span class="status">${escapeHtml(labels[job.status]||job.status)}</span>
    </button>`).join("") || "<p>暂无任务</p>";
  $(prefix+"-list").querySelectorAll("[data-id]").forEach(button=>button.onclick=()=>select(kind,button.dataset.id));
}
async function select(kind,id,silent=false){
  const sequence=++state.sequence;
  try{
    const job=await api(`/api/media/${kind}/${encodeURIComponent(id)}`);
    if(sequence!==state.sequence)return;
    const previous=state.selected;
    state.selected={...job,collection:kind};
    if(previous?.id!==id){$("versions").open=false;$("version-list").replaceChildren();}
    $(kind).querySelector(".history").append($("detail"));
    document.querySelectorAll(".task").forEach(row=>{
      row.classList.toggle("selected",row.dataset.id===id);
      if(row.dataset.id===id)row.querySelector(".status").textContent=labels[job.status]||job.status;
    });
    $("detail").hidden=false;$("detail-title").textContent=kind==="images"?"图片结果":"视频阶段";
    $("detail-id").textContent=id;
    $("delete").disabled=!["completed","failed","cancelled"].includes(job.status);
    $("cancel-image").hidden=kind!=="images"||["completed","failed","cancelled"].includes(job.status);
    $("detail-meta").textContent=[labels[job.status]||job.status,job.model,job.provider,
      workflowLabels[job.workflow_mode]||job.workflow_mode,job.creative_profile,aspectLabels[job.aspect_ratio]||job.aspect_ratio,
      job.fallback_applied?"已使用付费回退":"",job.error?.message,job.sync_error?.message].filter(Boolean).join(" · ");
    if(kind==="images"){
      $("stages").replaceChildren();
      $("detail-output").innerHTML=job.output?`<img src="${escapeHtml(job.output.content_url)}" alt="生成结果"><br><a href="${escapeHtml(job.output.content_url)}" download>下载原图</a>`:"";
    }else{
      $("detail-output").replaceChildren();
      // Avoid resetting a playing video or a draft on every status poll.
      const shape=value=>JSON.stringify(value?.stages?.map(s=>[
        s.id,s.status,s.progress,s.output_id,s.output?.id,s.label,s.actions,s.review,s.output?.review,
      ]));
      const expires=Number($("stages").dataset.expires||0);
      if(!silent||previous?.id!==id||shape(previous)!==shape(job)||expires<Date.now()/1000+30)renderStages(job);
    }
    if(!silent&&innerWidth<761)$("detail").scrollIntoView({behavior:"smooth",block:"start"});
  }catch(error){if(!silent)notice(error.message,true);}
}
function reviewIdentifier(review){return review?.review_id||review?.id||"";}
function reviewIssueTime(issue){
  if(typeof issue!=="object"||!issue)return "";
  if(issue.time_range)return String(issue.time_range);
  if(issue.timestamp_sec!=null)return `${issue.timestamp_sec}s`;
  if(issue.start_seconds!=null||issue.end_seconds!=null)return `${issue.start_seconds??"?"}–${issue.end_seconds??"?"}s`;
  if(issue.start_sec!=null||issue.end_sec!=null)return `${issue.start_sec??"?"}–${issue.end_sec??"?"}s`;
  return "";
}
function renderReview(review){
  if(!review||typeof review!=="object")return "";
  const semantic=review.semantic&&typeof review.semantic==="object"?review.semantic:review;
  const scores=semantic.scores&&typeof semantic.scores==="object"?Object.entries(semantic.scores):[];
  const issues=Array.isArray(semantic.issues)?semantic.issues:[];
  const suggestion=semantic.revised_prompt||semantic.suggested_prompt||
    (Array.isArray(semantic.recommendations)?semantic.recommendations.join("\n"):semantic.recommendation);
  const issueItems=issues.map(issue=>{
    if(typeof issue==="string")return `<li>${escapeHtml(issue)}</li>`;
    const time=reviewIssueTime(issue),severity=issue?.severity?`[${issue.severity}] `:"";
    const message=issue?.message||issue?.issue||issue?.description||JSON.stringify(issue);
    return `<li>${time?`<span class="review-time">${escapeHtml(time)}</span> `:""}${escapeHtml(severity+message)}</li>`;
  }).join("");
  return `<section class="review">
    <div class="review-heading"><h4>SIYUAN 质量评审</h4><span class="review-verdict">${escapeHtml(semantic.verdict||semantic.decision||semantic.status||"待判断")}${semantic.confidence!=null?` · 置信度 ${escapeHtml(semantic.confidence)}`:""}</span></div>
    ${review.manual_review_required?`<p class="warning">此版本需要人工复核；评审不会自动批准或重新生成。</p>`:""}
    ${scores.length?`<div class="review-scores">${scores.map(([name,value])=>`<div class="review-score"><span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div>`:""}
    ${issues.length?`<ul class="review-issues">${issueItems}</ul>`:""}
    ${suggestion?`<div class="review-suggestion"><strong>修改建议</strong><br>${escapeHtml(suggestion)}</div>`:""}
    ${reviewIdentifier(review)?`<code class="review-id">review_id: ${escapeHtml(reviewIdentifier(review))}</code>`:""}
  </section>`;
}
function actionAllowed(stage,action,fallback){
  const actions=stage.actions??stage.allowed_actions;
  if(Array.isArray(actions))return actions.includes(action);
  if(actions&&typeof actions==="object"&&own(actions,action))return Boolean(actions[action]);
  return fallback;
}
function renderStages(job){
  const stages=Array.isArray(job.stages)?job.stages:[];
  const playing=[...$("stages").querySelectorAll("video")].map(video=>({id:video.dataset.output,time:video.currentTime,paused:video.paused}));
  $("stages").dataset.expires=String(Math.min(...stages.map(stage=>stage.output?.expires_at||Infinity)));
  $("stages").innerHTML=stages.map((stage,index)=>{
    const output=stage.output,previous=stages[index-1],review=stage.review||output?.review;
    const canStart=stage.id==="context_ir" || previous?.status==="approved";
    const draft=state.drafts.get(output?.output_id)??output?.text??"";
    const startable=canStart&&["pending","failed","cancelled"].includes(stage.status)&&actionAllowed(stage,"start",true);
    const reviewId=reviewIdentifier(review);
    return `<section class="stage">
      <div class="stage-info"><h3>${escapeHtml(stage.label||stage.title||stageLabels[stage.id]||stage.id)}</h3><span>${escapeHtml(labels[stage.status]||stage.status)}</span><progress max="100" ${stage.progress==null?"":`value="${Number(stage.progress)||0}"`}></progress></div>
      <div class="stage-output">
      ${output?output.content_type==="text/plain"?`<pre>${escapeHtml(output.text)}</pre>${stage.status==="awaiting_approval"?`<label>批准的提示词<textarea data-draft="${escapeHtml(output.output_id)}">${escapeHtml(draft)}</textarea></label>`:""}`:
        `<video controls preload="metadata" data-output="${escapeHtml(output.output_id)}" src="${escapeHtml(output.content_url)}"></video>`:""}
      ${output?`<code>${escapeHtml(output.output_id)}</code><br><a href="${escapeHtml(output.content_url)}" download>下载阶段产物</a>`:""}
      ${renderReview(review)}
      <div class="actions">
        <button data-action="approve" data-stage="${escapeHtml(stage.id)}" data-output="${escapeHtml(stage.output_id||output?.output_id||"")}" ${stage.status!=="awaiting_approval"||!output||!actionAllowed(stage,"approve",true)?"disabled":""}>批准此版本</button>
        <button data-action="start" data-stage="${escapeHtml(stage.id)}" data-output="${escapeHtml(previous?.output_id||previous?.output?.output_id||"")}" ${!startable?"disabled":""}>启动阶段</button>
        <button data-action="regenerate" data-stage="${escapeHtml(stage.id)}" data-output="${escapeHtml(stage.output_id||output?.output_id||"")}" data-review="${escapeHtml(reviewId)}" ${!output||(!reviewId&&stage.id!=="plan")||!actionAllowed(stage,"regenerate",true)?"disabled":""}>${stage.id==="plan"?"重新生成方案":"按评审建议重新生成"}</button>
        <button data-action="cancel" data-stage="${escapeHtml(stage.id)}" ${!["queued","running"].includes(stage.status)||!actionAllowed(stage,"cancel",true)?"disabled":""}>取消</button>
      </div></div></section>`;
  }).join("");
  $("stages").querySelectorAll("video").forEach(video=>{
    const previous=playing.find(item=>item.id===video.dataset.output);
    if(previous)video.addEventListener("loadedmetadata",()=>{
      video.currentTime=previous.time;if(!previous.paused)void video.play().catch(()=>{});
    },{once:true});
  });
  $("stages").querySelectorAll("[data-draft]").forEach(input=>input.oninput=()=>state.drafts.set(input.dataset.draft,input.value));
  $("stages").querySelectorAll("[data-action]").forEach(button=>button.onclick=async()=>{
    const id=job.id,{stage,action,output,review}=button.dataset;
    if(action==="regenerate"&&!confirm("确认依据当前评审建议重新生成这个阶段？这不会批准当前版本，也不会启动下一阶段。"))return;
    if(action==="start"&&(["cloud_768","regenerate_2k","context_ir"].includes(stage)
      ||stages.find(item=>item.id===stage)?.billable)
      &&!confirm("确认启动此可能计费的阶段？"))return;
    const body={};
    if(output)body.output_id=output;
    if(action==="regenerate"&&review){body.review_id=review;body.apply_suggestion=true;}
    const current=stages.find(item=>item.id===stage);
    if(action==="approve"&&stage==="context_ir")
      body.prompt=state.drafts.get(output)??current.output.text;
    button.disabled=true;
    try{
      await api(`/api/media/videos/${id}/stages/${stage}/${action}`,{method:"POST",headers:{"Idempotency-Key":button.dataset.key||=crypto.randomUUID()},body:JSON.stringify(body)});
      notice(action==="regenerate"?"重新生成请求已受理，当前版本未被批准":"操作已受理");
      if(state.selected?.id===id)await select("videos",id);
    }catch(error){notice(error.message,true);button.disabled=false;}
  });
}
async function submitImage(event){
  event.preventDefault();const form=event.currentTarget,button=form.querySelector("[type=submit]");
  const operation=form.elements.operation.value;
  const body=new FormData();
  for(const name of ["model","prompt","use_case","aspect_ratio","background"])body.set(name,form.elements[name].value);
  body.set("response_format","url");
  if(operation==="edits"){
    const files=form.elements.image.files;
    if(!files.length||files.length>5){notice("编辑需要 1–5 张参考图",true);return;}
    for(const file of files)body.append("image",file);
  }
  const plain=Object.fromEntries(body.entries());
  button.disabled=true;notice("图片任务已提交，正在等待结果");
  try{
    const job=await api(`/api/media/images/${operation}`,{method:"POST",headers:{"Idempotency-Key":form.dataset.key||=crypto.randomUUID()},
      body:operation==="edits"?body:JSON.stringify(plain)});
    delete form.dataset.key;await loadPage("images");await select("images",job.id);notice("");
  }catch(error){notice(error.message,true);}finally{button.disabled=false;}
}
async function submitVideo(event){
  event.preventDefault();const form=event.currentTarget,button=form.querySelector("[type=submit]");
  const body=new FormData(form);body.set("model","siyuan-video");
  for(const name of ["workflow_mode","creative_profile","aspect_ratio"])if(!state.videoCapabilities[name])body.delete(name);
  for(const [name,item] of [...body.entries()])if(item instanceof File&&!item.size)body.delete(name);
  for(const name of ["watermark","use_embedded_video_audio"])body.set(name,String(form.elements[name].checked));
  button.disabled=true;
  try{
    const job=await api("/api/media/videos",{method:"POST",headers:{"Idempotency-Key":form.dataset.key||=crypto.randomUUID()},body});
    delete form.dataset.key;await loadPage("videos");await select("videos",job.id);notice("首个阶段已受理，完成后等待客户批准");
  }catch(error){notice(error.message,true);}finally{button.disabled=false;}
}
const settingLabels={enabled:"媒体总开关",images_enabled:"图片生成",videos_enabled:"视频生成",paid_fallback:"额度不足时付费回退",
  daily_paid_images:"每日付费图片上限",queue_limit:"图片队列上限",queue_timeout:"排队期限（秒）",image_timeout:"图片执行期限（秒）",
  poll_interval:"视频同步间隔（秒）",min_free_bytes:"最低剩余存储（字节）",codex_ready:"Codex 契约及隔离验收通过",h3_ready:"H3 契约验收通过"};
async function loadSettings(){
  state.settings=await api("/api/media/settings");
  $("settings-fields").innerHTML=Object.entries(state.settings).map(([name,value])=>typeof value==="boolean"?
    `<label class="check"><input name="${name}" type="checkbox" ${value?"checked":""}>${settingLabels[name]}</label>`:
    `<label>${settingLabels[name]}<input name="${name}" type="number" min="0" value="${value}"></label>`).join("");
  const clients=await api("/api/clients");
  const items=clients.clients||clients.items||clients;
  $("client-grants").innerHTML=items.map(client=>`<form class="grant" data-client="${escapeHtml(client.id)}"><strong>${escapeHtml(client.name||client.id)}</strong>
    ${["siyuan-image","siyuan-video",...(client.disclosure_mode==="internal"?["qwen-image-3.0-pro"]:[])].map(model=>
      `<label class="check"><input type="checkbox" value="${model}" ${client.media_models?.includes(model)?"checked":""}>${model}</label>`).join("")}
    <button>保存授权</button></form>`).join("");
  $("client-grants").querySelectorAll("form").forEach(form=>form.onsubmit=async event=>{
    event.preventDefault();try{await api("/api/clients/"+encodeURIComponent(form.dataset.client),{method:"PATCH",body:JSON.stringify({
      media_models:[...form.querySelectorAll("input:checked")].map(input=>input.value)})});notice("媒体授权已保存");}catch(error){notice(error.message,true);}
  });
}
$("login").onsubmit=event=>{event.preventDefault();void connect();};
$("image-form").onsubmit=submitImage;$("video-form").onsubmit=submitVideo;
function updateAssets(){
  const form=$("video-form"),mode=form.elements.mode.value,audio=form.elements.audio_policy.value;
  const required={i2v:["first_frame"],l2v:["last_frame"],fl2v:["first_frame","last_frame"],hybrid:["first_frame","last_frame","reference_image"]}[mode]||[];
  const visible=new Set(required);
  if(mode==="reference")["reference_image","reference_video","reference_audio"].forEach(name=>visible.add(name));
  if(audio==="reference")visible.add("reference_audio");
  if(audio==="lock_source")["first_frame","reference_audio"].forEach(name=>visible.add(name));
  for(const name of ["first_frame","last_frame","reference_image","reference_video","reference_audio"]){
    const input=form.elements[name];input.closest("label").hidden=!visible.has(name);input.disabled=!visible.has(name);
    input.required=required.includes(name)||(audio==="lock_source"&&["first_frame","reference_audio"].includes(name));
  }
  form.elements.use_embedded_video_audio.closest("label").hidden=mode!=="reference";
  if(mode!=="reference")form.elements.use_embedded_video_audio.checked=false;
}
$("video-form").elements.mode.onchange=updateAssets;
$("video-form").elements.audio_policy.onchange=updateAssets;
updateAssets();
for(const id of ["image-form","video-form"])$(id).oninput=()=>delete $(id).dataset.key;
$("image-form").elements.operation.onchange=()=>{$("image-upload").hidden=$("image-form").elements.operation.value!=="edits";};
let referenceUrls=[];
$("image-form").elements.image.onchange=()=>{
  referenceUrls.forEach(URL.revokeObjectURL);referenceUrls=[...$("image-form").elements.image.files].slice(0,5).map(URL.createObjectURL);
  $("reference-previews").innerHTML=referenceUrls.map(url=>`<img src="${url}" alt="参考图">`).join("");
};
document.querySelectorAll("[data-view]").forEach(button=>button.onclick=()=>{
  state.view=button.dataset.view;document.querySelectorAll(".view").forEach(view=>view.hidden=view.id!==state.view);
  document.querySelectorAll("[data-view]").forEach(tab=>tab.setAttribute("aria-selected",String(tab===button)));
  $("detail").hidden=state.view==="settings"||!state.selected;
});
$("refresh").onclick=async()=>{try{if(state.view==="settings")await loadSettings();else await loadPage(state.view);}catch(error){notice(error.message,true);}};
$("image-more").onclick=()=>loadPage("images",true);$("video-more").onclick=()=>loadPage("videos",true);
$("delete").onclick=async()=>{
  const job=state.selected;if(!job||!confirm("确认删除此任务记录？归档文件不会自动清理。"))return;
  try{await api(`/api/media/${job.collection}/${job.id}`,{method:"DELETE"});state.sequence++;state.selected=null;$("detail").hidden=true;await loadPage(job.collection);}catch(error){notice(error.message,true);}
};
$("cancel-image").onclick=async()=>{
  const job=state.selected;if(!job)return;
  try{await api(`/api/media/images/${job.id}/cancel`,{method:"POST"});await select("images",job.id);notice("取消已请求，等待上游确认");}catch(error){notice(error.message,true);}
};
$("versions").ontoggle=async()=>{
  const job=state.selected;if(!$("versions").open||!job)return;
  try{
    const page=await api(`/api/media/${job.collection}/${job.id}/outputs`);
    if(state.selected?.id!==job.id)return;
    $("version-list").innerHTML=page.data.map(output=>`<section class="stage">
      <div class="stage-info"><strong>${escapeHtml(stageLabels[output.stage]||"图片")}</strong><time>${new Date(output.created_at*1000).toLocaleString("zh-CN")}</time></div>
      <div class="stage-output"><code>${escapeHtml(output.output_id)}</code>${output.text?`<pre>${escapeHtml(output.text)}</pre>`:""}<br>
      <a href="${escapeHtml(output.content_url)}" target="_blank" rel="noopener">查看或下载此版本</a>${renderReview(output.review)}</div></section>`).join("");
  }catch(error){notice(error.message,true);}
};
$("purge-form").onsubmit=async event=>{
  event.preventDefault();const id=event.currentTarget.elements.job_id.value;
  if(!confirm("确认永久清理该已删除任务在 AI 主机的归档文件？"))return;
  try{await api(`/api/media/jobs/${encodeURIComponent(id)}/purge`,{method:"POST",body:JSON.stringify({confirm:id})});notice("归档文件已清理");}catch(error){notice(error.message,true);}
};
$("settings-form").onsubmit=async event=>{
  event.preventDefault();const value={};
  for(const [name,old] of Object.entries(state.settings))value[name]=typeof old==="boolean"?event.currentTarget.elements[name].checked:Number(event.currentTarget.elements[name].value);
  try{state.settings=await api("/api/media/settings",{method:"PUT",body:JSON.stringify(value)});notice("媒体设置已保存");}catch(error){notice(error.message,true);}
};
ViewPreferences.fields(["#image-form select", "#video-form select"].flatMap(selector=>[...document.querySelectorAll(selector)].map(el=>`#${el.form.id} select[name="${el.name}"]`)));
$("image-form").elements.operation.onchange?.();
ViewPreferences.details();
document.querySelector(`[data-view="${state.view}"]`)?.click();
$("key").value=state.key;if(state.key)void connect();
setInterval(()=>{if(state.selected&&state.view!=="settings")void select(state.selected.collection,state.selected.id,true);},5000);
