"use strict";
const $ = id => document.getElementById(id);
const state = {key: sessionStorage.getItem("ai-router-admin-key") || "", view: "images",
  selected: null, sequence: 0, settings: null, pages: {images: [], videos: []}, cursors: {}, drafts: new Map()};
const labels = {queued:"排队中",running:"执行中",in_progress:"执行中",archiving:"归档中",awaiting_approval:"待批准",
  approved:"已批准",completed:"已完成",failed:"失败",cancelled:"已取消",cancelling:"取消中",reconciling:"核对原任务",pending:"待启动"};
const stageLabels = {context_ir:"Context IR",preview:"预览",proof:"质量验证",local_768:"本地 768P",cloud_768:"云端 768P",regenerate_2k:"2K"};
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function notice(text="", error=false){$("notice").textContent=text;$("notice").classList.toggle("error",error);}
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
  try{const options=await api("/api/media/options");notice(options.enabled?"":"媒体生成未启用");
    for(const field of ["mode","strategy","audio_policy"]){
      const allowed=options.videos?.[field];
      if(Array.isArray(allowed))for(const option of [...$("video-form").elements[field].options])if(!allowed.includes(option.value))option.remove();
    }
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
      job.fallback_applied?"已使用付费回退":"",job.error?.message,job.sync_error?.message].filter(Boolean).join(" · ");
    if(kind==="images"){
      $("stages").replaceChildren();
      $("detail-output").innerHTML=job.output?`<img src="${escapeHtml(job.output.content_url)}" alt="生成结果"><br><a href="${escapeHtml(job.output.content_url)}" download>下载原图</a>`:"";
    }else{
      $("detail-output").replaceChildren();
      // Avoid resetting a playing video or a draft on every status poll.
      const shape=value=>JSON.stringify(value?.stages?.map(s=>[s.id,s.status,s.progress,s.output_id,s.output?.id]));
      const expires=Number($("stages").dataset.expires||0);
      if(!silent||previous?.id!==id||shape(previous)!==shape(job)||expires<Date.now()/1000+30)renderStages(job);
    }
    if(!silent&&innerWidth<761)$("detail").scrollIntoView({behavior:"smooth",block:"start"});
  }catch(error){if(!silent)notice(error.message,true);}
}
function renderStages(job){
  const playing=[...$("stages").querySelectorAll("video")].map(video=>({id:video.dataset.output,time:video.currentTime,paused:video.paused}));
  $("stages").dataset.expires=String(Math.min(...job.stages.map(stage=>stage.output?.expires_at||Infinity)));
  $("stages").innerHTML=job.stages.map((stage,index)=>{
    const output=stage.output, previous=job.stages[index-1];
    const canStart=stage.id==="context_ir" || previous?.status==="approved";
    const draft=state.drafts.get(output?.output_id)??output?.text??"";
    return `<section class="stage">
      <div class="stage-info"><h3>${escapeHtml(stageLabels[stage.id]||stage.id)}</h3><span>${escapeHtml(labels[stage.status]||stage.status)}</span><progress max="100" ${stage.progress==null?"":`value="${Number(stage.progress)||0}"`}></progress></div>
      <div class="stage-output">
      ${output?output.content_type==="text/plain"?`<pre>${escapeHtml(output.text)}</pre>${stage.status==="awaiting_approval"?`<label>批准的提示词<textarea data-draft="${escapeHtml(output.output_id)}">${escapeHtml(draft)}</textarea></label>`:""}`:
        `<video controls preload="metadata" data-output="${escapeHtml(output.output_id)}" src="${escapeHtml(output.content_url)}"></video>`:""}
      ${output?`<code>${escapeHtml(output.output_id)}</code><br><a href="${escapeHtml(output.content_url)}" download>下载阶段产物</a>`:""}
      <div class="actions">
        <button data-action="approve" data-stage="${stage.id}" data-output="${escapeHtml(stage.output_id||"")}" ${stage.status!=="awaiting_approval"||!output?"disabled":""}>批准此版本</button>
        <button data-action="start" data-stage="${stage.id}" data-output="${escapeHtml(previous?.output_id||"")}" ${!canStart||["queued","running","archiving"].includes(stage.status)?"disabled":""}>${["approved","awaiting_approval"].includes(stage.status)?"重新生成":"启动阶段"}</button>
        <button data-action="cancel" data-stage="${stage.id}" ${!["queued","running"].includes(stage.status)?"disabled":""}>取消</button>
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
    const id=job.id, {stage,action,output}=button.dataset;
    if(action==="start"&&["cloud_768","regenerate_2k","context_ir"].includes(stage)
      &&!confirm("确认启动此可能计费的阶段？"))return;
    const body={};
    if(output)body.output_id=output;
    if(action==="approve"&&stage==="context_ir")body.prompt=state.drafts.get(output)??job.stages[0].output.text;
    button.disabled=true;
    try{
      await api(`/api/media/videos/${id}/stages/${stage}/${action}`,{method:"POST",headers:{"Idempotency-Key":button.dataset.key||=crypto.randomUUID()},body:JSON.stringify(body)});
      notice("操作已受理");if(state.selected?.id===id)await select("videos",id);
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
  for(const [name,item] of [...body.entries()])if(item instanceof File&&!item.size)body.delete(name);
  for(const name of ["watermark","use_embedded_video_audio"])body.set(name,String(form.elements[name].checked));
  button.disabled=true;
  try{
    const job=await api("/api/media/videos",{method:"POST",headers:{"Idempotency-Key":form.dataset.key||=crypto.randomUUID()},body});
    delete form.dataset.key;await loadPage("videos");await select("videos",job.id);notice("Context IR 已受理，完成后等待批准");
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
      <a href="${escapeHtml(output.content_url)}" target="_blank" rel="noopener">查看或下载此版本</a></div></section>`).join("");
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
$("key").value=state.key;if(state.key)void connect();
setInterval(()=>{if(state.selected&&state.view!=="settings")void select(state.selected.collection,state.selected.id,true);},5000);
