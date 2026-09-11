"use strict";
const element = (name) => document.getElementById(name);
let currentStatus = null;
let backendStatus = null;
let writing = false;
let tokenTimer = null;
function notice(text, error = false) { element("message").textContent = text; element("message").dataset.error = String(error); }
function when(value) { return value ? new Date(value * 1000).toLocaleString() : "尚无记录"; }
async function api(action, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`./api/mcp-admin/${action}`, { method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined, signal: controller.signal });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || result.error?.message || "管理请求失败，请刷新核对。");
    return result;
  } finally { clearTimeout(timer); }
}
function service(name, value) {
  const card = document.createElement("div"); card.className = "mcp-service"; card.dataset.healthy = String(value.healthy);
  const title = document.createElement("strong"); title.textContent = `${name} · ${value.healthy ? "在线" : "未知 / 心跳过期"}`;
  const detail = document.createElement("p"); detail.textContent = value.release || "当前响应正常";
  card.append(title, detail); return card;
}
function lockButtons() {
  for (const name of ["issue", "pause", "resume", "generation"]) element(name).disabled = writing || !currentStatus;
  element("backendToggle").disabled = writing || !backendStatus;
  for (const button of element("keys").querySelectorAll("button")) button.disabled = writing;
}
async function refresh() {
  try {
    const value = await api("status"); currentStatus = value;
    element("endpoint").textContent = value.endpoint;
    element("instances").replaceChildren(...value.instances.map((item) => service(item.instance, item)), service("Studio", value.studio));
    element("lastAuth").textContent = when(value.last_authentication?.at);
    element("lastTool").textContent = value.last_tool ? `${when(value.last_tool.at)} · ${value.last_tool.tool} · ${value.last_tool.outcome}` : "尚无记录";
    element("gates").textContent = `新草稿操作：${value.writes_enabled ? "允许" : "暂停或服务未就绪"}；视频生成：${value.generation_enabled ? "允许，但仍需明确确认并启动" : "关闭，未开放本轮发布门禁"}。`;
    element("generation").textContent = value.policy.generation ? "暂停新视频生成" : "允许视频生成";
    element("keys").replaceChildren();
    for (const key of value.keys) {
      const row = document.createElement("div"); row.className = "mcp-key";
      const text = document.createElement("span"); text.textContent = `${key.label} · ${key.key_id} · ${key.status === "active" ? "有效" : "已撤销"}`; row.append(text);
      if (key.status === "active") {
        const button = document.createElement("button"); button.type = "button"; button.textContent = "撤销";
        button.addEventListener("click", () => { if (confirm(`撤销密钥 ${key.key_id}？客户端之后需要重新认证；不会取消已提交任务。`)) mutate("revoke", {key_id: key.key_id}); }); row.append(button);
      }
      element("keys").append(row);
    }
    notice("已更新服务端状态；没有启动任何视频。");
  } catch (error) { currentStatus = null; notice(error.message, true); }
  lockButtons();
}
async function refreshBackend() {
  try {
    backendStatus = await api("backend");
    element("backendStatus").textContent = backendStatus.policy.enabled ? "后台已启用：允许已提交任务按需自动准备服务" : "后台已停用：不再派发新任务，已有任务完成后排空停止";
    element("backendToggle").textContent = backendStatus.policy.enabled ? "停用后台 · 等待健康任务完成" : "启用后台 · 按任务唤醒";
    const states = {starting: "启动中", stopped: "已停止", online_model_unknown: "在线，模型状态待核实", waiting_for_verified_unload: "等待确认卸载", stop_failed_requires_operator: "停止未确认，需要处理", unknown: "状态未知"};
    element("backends").replaceChildren(...backendStatus.backends.map(item => {
      const card = document.createElement("div"); card.className = "mcp-service";
      card.textContent = `${item.backend_id} · ${states[item.state] || "状态待核实"} · ${item.quarantined ? "配置已暂停准入" : "仍需资源检查"} · 模型：${item.model_state === "unknown" ? "未知" : item.model_state}`;
      return card;
    }));
  } catch { backendStatus = null; element("backendStatus").textContent = "后台管理未就绪，不能确认或修改运行策略；不显示旧状态。"; element("backends").replaceChildren(); }
  lockButtons();
}
function clearToken() { clearTimeout(tokenTimer); element("token").value = ""; element("tokenDialog").close(); }
async function mutate(action, body) {
  if (writing || !currentStatus) return;
  writing = true; lockButtons();
  try {
    const result = await api(action, {...body, operation_id: crypto.randomUUID()});
    if (action === "issue") {
      element("token").value = result.api_key; result.api_key = ""; element("tokenDialog").showModal();
      tokenTimer = setTimeout(clearToken, 300000);
    }
    await refresh();
  } catch (error) { notice(`${error.message} 不自动重试；请先刷新核对结果。`, true); }
  finally { writing = false; lockButtons(); }
}
element("refresh").addEventListener("click", refresh);
element("issue").addEventListener("click", () => mutate("issue", {label: element("label").value}));
element("pause").addEventListener("click", () => mutate("policy", {revision: currentStatus.policy.revision, writes: false, generation: false}));
element("resume").addEventListener("click", () => mutate("policy", {revision: currentStatus.policy.revision, writes: true, generation: false}));
element("generation").addEventListener("click", () => {
  if (!currentStatus) return;
  const enabled = !currentStatus.policy.generation;
  if (confirm(enabled ? "允许明确批准并提交的新视频请求？不会自动生成旧草稿，也不会绕过模式资格和资源保护。" : "暂停新视频请求？已提交的健康任务不会取消。")) mutate("policy", {revision: currentStatus.policy.revision, writes: currentStatus.policy.writes, generation: enabled});
});
element("backendToggle").addEventListener("click", async () => {
  if (!backendStatus || writing) return;
  const enabled = !backendStatus.policy.enabled;
  if (!confirm(enabled ? "启用按任务唤醒后台？没有明确提交的草稿不会生成。" : "停用后台？立即暂停新增派发，保留队列，健康任务结束后再停止 H3 后端。")) return;
  writing = true; lockButtons();
  try { await api("backend", {enabled, expected_revision: backendStatus.policy.revision, operation_id: crypto.randomUUID()}); await refreshBackend(); notice("后台策略已保存，没有创建或取消任务。"); }
  catch (error) { notice(`${error.message} 请先刷新对账，不自动重试。`, true); }
  finally { writing = false; lockButtons(); }
});
element("closeToken").addEventListener("click", clearToken);
element("tokenDialog").addEventListener("cancel", clearToken);
element("copyToken").addEventListener("click", async () => { try { await navigator.clipboard.writeText(element("token").value); notice("已复制，仅粘贴到 WorkBuddy 原生密码表单。"); } catch { notice("浏览器禁止复制，请检查剪贴板权限；不要截图密钥。", true); } });
element("copyConfig").addEventListener("click", async () => {
  try { const response = await fetch("./h3-workbuddy-auth.json", {cache: "no-store"}); if (!response.ok) throw new Error(); const config = await response.json(); await navigator.clipboard.writeText(JSON.stringify(config, null, 2)); notice("已复制不含密钥的专家依赖声明；不是向普通 MCP 编辑器直接写入 Token。"); }
  catch { notice("连接声明复制失败，请下载向导包。", true); }
});
window.addEventListener("pagehide", () => { element("token").value = ""; });
refresh();
refreshBackend();
setInterval(() => { if (!document.hidden && !writing && !element("tokenDialog").open) { refresh(); refreshBackend(); } }, 15000);
